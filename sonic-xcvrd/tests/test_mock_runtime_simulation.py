"""Tests for the runtime transceiver simulation reference implementation.

These tests exercise the in-tree prototype of the planned external
``sonic-platform-mock`` package (see ``docs/runtime-simulation.md``). They
verify the API contract that ``xcvrd`` expects from any platform plugin
driving runtime simulation:

* ``Chassis.get_change_event(timeout)`` returns
  ``(True, {'sfp': {...}, 'sfp_error': {...}})`` with port indices as
  string keys and ``'0'``/``'1'`` status values.
* ``Chassis.get_change_event(0)`` blocks until at least one event arrives.
* An idle poll returns ``(True, {'sfp': {}, 'sfp_error': {}})`` - critically
  NOT ``False``, which xcvrd would interpret as SYSTEM_FAIL.
* ``Sfp.get_presence()``, ``read_eeprom``/``write_eeprom`` round-trip data
  consistent with the inserted profile.
* The xcvrd helper ``_wrapper_get_transceiver_change_event`` decodes our
  payload correctly when ``platform_chassis`` is the mock.
* The JSON-over-UDS control plane dispatches the documented ops, validates
  inputs, and rejects oversized / malformed payloads without crashing.
"""

import json
import os
import socket
import sys
import tempfile
import threading
import time

import pytest

# Make ``runtime_sim`` importable both as ``tests.runtime_sim`` (when pytest
# discovers from the sonic-xcvrd root) and when imported standalone.
_HERE = os.path.dirname(os.path.abspath(__file__))
if _HERE not in sys.path:
    sys.path.insert(0, _HERE)

from runtime_sim import simctl  # noqa: E402
from runtime_sim.chassis import MockChassis  # noqa: E402
from runtime_sim.control_server import ControlServer, MAX_MSG_BYTES  # noqa: E402
from runtime_sim.platform import Platform  # noqa: E402
from runtime_sim.sim_state import (  # noqa: E402
    SFP_STATUS_INSERTED,
    SFP_STATUS_REMOVED,
    SimState,
)


@pytest.fixture(autouse=True)
def _reset_sim_state():
    """Ensure each test starts with a fresh singleton."""
    SimState.reset_for_tests()
    Platform._chassis = None
    yield
    SimState.reset_for_tests()
    Platform._chassis = None


# ---------------------------------------------------------------------------
# SimState core behavior
# ---------------------------------------------------------------------------


def test_sim_state_insert_emits_inserted_event():
    sim = SimState.instance(port_count=8)

    sim.insert(3, "qsfp28-100g-cwdm4")

    port_dict, err_dict = sim.drain_events(timeout_ms=100)
    assert port_dict == {"3": SFP_STATUS_INSERTED}
    assert err_dict == {}
    assert sim.is_present(3) is True


def test_sim_state_remove_emits_removed_event_and_clears_eeprom():
    sim = SimState.instance(port_count=8)
    sim.insert(1, "qsfp28-100g-cwdm4")
    sim.drain_events(timeout_ms=10)

    sim.remove(1)

    port_dict, _ = sim.drain_events(timeout_ms=100)
    assert port_dict == {"1": SFP_STATUS_REMOVED}
    assert sim.is_present(1) is False
    assert sim.read_eeprom(1, 0, 16) is None


def test_sim_state_drain_collapses_repeated_events_for_same_port():
    sim = SimState.instance(port_count=4)
    sim.insert(2, "qsfp28-100g-cwdm4")
    sim.remove(2)
    sim.insert(2, "qsfp28-100g-cwdm4")

    port_dict, _ = sim.drain_events(timeout_ms=50)

    # Final state for port 2 wins.
    assert port_dict == {"2": SFP_STATUS_INSERTED}


def test_sim_state_idle_timeout_returns_empty():
    sim = SimState.instance(port_count=4)
    start = time.monotonic()
    port_dict, err_dict = sim.drain_events(timeout_ms=50)
    elapsed = time.monotonic() - start

    assert port_dict == {}
    assert err_dict == {}
    assert 0.04 <= elapsed < 1.0


def test_sim_state_rejects_invalid_port_and_profile():
    sim = SimState.instance(port_count=4)
    with pytest.raises(ValueError):
        sim.insert(0, "qsfp28-100g-cwdm4")
    with pytest.raises(ValueError):
        sim.insert(5, "qsfp28-100g-cwdm4")
    with pytest.raises(ValueError):
        sim.insert(1, "no-such-profile")
    with pytest.raises(TypeError):
        sim.insert("1", "qsfp28-100g-cwdm4")  # type: ignore[arg-type]


def test_sim_state_set_error_surfaces_via_drain():
    sim = SimState.instance(port_count=4)
    sim.set_error(2, "blocking")

    _, err_dict = sim.drain_events(timeout_ms=20)
    assert err_dict == {"2": "blocking"}

    # Subsequent drain returns no error.
    sim.insert(1, "qsfp28-100g-cwdm4")
    _, err_dict_2 = sim.drain_events(timeout_ms=20)
    assert err_dict_2 == {}


def test_sim_state_eeprom_write_persists():
    sim = SimState.instance(port_count=2)
    sim.insert(1, "qsfp28-100g-cwdm4")

    assert sim.write_eeprom(1, 200, b"\xAA\xBB") is True
    data = sim.read_eeprom(1, 200, 2)
    assert bytes(data) == b"\xAA\xBB"
    # Writing past the end grows the blob, allowing CMIS upper-bank writes.
    assert sim.write_eeprom(1, 700, b"\x55") is True
    assert bytes(sim.read_eeprom(1, 700, 1)) == b"\x55"


# ---------------------------------------------------------------------------
# Chassis contract (the surface xcvrd consumes)
# ---------------------------------------------------------------------------


def test_chassis_get_change_event_matches_xcvrd_contract():
    chassis = MockChassis(port_count=4)
    chassis._sim.insert(2, "qsfp28-100g-cwdm4")

    status, events = chassis.get_change_event(timeout=200)

    assert status is True
    assert set(events.keys()) == {"sfp", "sfp_error"}
    assert events["sfp"] == {"2": SFP_STATUS_INSERTED}
    assert events["sfp_error"] == {}


def test_chassis_idle_poll_returns_true_and_empty():
    """xcvrd's state machine treats False as SYSTEM_FAIL; verify we don't trip it."""
    chassis = MockChassis(port_count=4)

    status, events = chassis.get_change_event(timeout=50)

    assert status is True
    assert events["sfp"] == {}
    assert events["sfp_error"] == {}


def test_chassis_get_change_event_blocks_until_event():
    chassis = MockChassis(port_count=4)

    received = []

    def waiter():
        received.append(chassis.get_change_event(timeout=0))

    t = threading.Thread(target=waiter)
    t.start()
    # Give the waiter time to block on the empty queue.
    time.sleep(0.1)
    assert t.is_alive()

    chassis._sim.insert(1, "qsfp28-100g-cwdm4")
    t.join(timeout=2.0)
    assert not t.is_alive()
    assert received and received[0][0] is True
    assert received[0][1]["sfp"] == {"1": SFP_STATUS_INSERTED}


def test_chassis_get_sfp_round_trips_presence_and_eeprom():
    chassis = MockChassis(port_count=4)
    sfp = chassis.get_sfp(1)

    assert sfp.get_presence() is False
    chassis._sim.insert(1, "qsfp28-100g-cwdm4")
    assert sfp.get_presence() is True

    # SFF-8636 identifier byte should be 0x11 for the QSFP28 profile.
    data = sfp.read_eeprom(0, 1)
    assert data is not None and data[0] == 0x11

    # write_eeprom round-trip via the SFP API.
    assert sfp.write_eeprom(300, 4, b"\x01\x02\x03\x04") is True
    assert bytes(sfp.read_eeprom(300, 4)) == b"\x01\x02\x03\x04"


def test_chassis_get_sfp_index_validation():
    chassis = MockChassis(port_count=2)
    with pytest.raises(IndexError):
        chassis.get_sfp(0)
    with pytest.raises(IndexError):
        chassis.get_sfp(3)


def test_platform_singleton_returns_same_chassis():
    p1 = Platform()
    p2 = Platform()
    assert p1.get_chassis() is p2.get_chassis()


# ---------------------------------------------------------------------------
# xcvrd integration: feed our chassis through the real xcvrd helper
# ---------------------------------------------------------------------------


def test_wrapper_get_transceiver_change_event_decodes_mock_chassis():
    """End-to-end: real xcvrd helper -> MockChassis -> SimState."""
    # xcvrd has heavy transitive deps (natsort, sonic_py_common, swsscommon,
    # sonic_platform_base). Skip cleanly if any are missing in this env;
    # the test is still active in CI where the daemon's deps are installed.
    xcvrd_mod = pytest.importorskip("xcvrd.xcvrd", exc_type=ImportError)

    chassis = MockChassis(port_count=4)
    chassis._sim.insert(2, "qsfp28-100g-cwdm4")
    chassis._sim.set_error(3, "blocking")

    original = xcvrd_mod.platform_chassis
    xcvrd_mod.platform_chassis = chassis
    try:
        status, sfp_events, sfp_errors = xcvrd_mod._wrapper_get_transceiver_change_event(200)
    finally:
        xcvrd_mod.platform_chassis = original

    assert status is True
    assert sfp_events == {"2": SFP_STATUS_INSERTED}
    assert sfp_errors == {"3": "blocking"}


def test_wrapper_soak_handles_mock_events():
    """The insert-event soak path consumes our payload without crashing."""
    xcvrd_mod = pytest.importorskip("xcvrd.xcvrd", exc_type=ImportError)
    sfp_status_helper = pytest.importorskip("xcvrd.xcvrd_utilities.sfp_status_helper", exc_type=ImportError)

    insert_events = {}
    port_dict = {"1": sfp_status_helper.SFP_STATUS_INSERTED}
    xcvrd_mod._wrapper_soak_sfp_insert_event(insert_events, port_dict)

    # Insert events are deferred for MGMT_INIT_TIME_DELAY_SECS.
    assert port_dict == {}
    assert "1" in insert_events


# ---------------------------------------------------------------------------
# Control server (UDS, JSON dispatch)
# ---------------------------------------------------------------------------


@pytest.fixture
def control_socket(tmp_path):
    sim = SimState.instance(port_count=8)
    sock_path = str(tmp_path / "sim.sock")
    server = ControlServer(sim, socket_path=sock_path)
    server.start()
    try:
        yield sock_path, server, sim
    finally:
        server.stop()


def _send_raw(sock_path, payload_bytes, recv_max=4096):
    sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    sock.settimeout(2.0)
    sock.connect(sock_path)
    try:
        sock.sendall(payload_bytes)
        return sock.recv(recv_max)
    finally:
        sock.close()


def test_control_server_insert_remove_roundtrip(control_socket):
    sock_path, _server, sim = control_socket

    raw = _send_raw(sock_path, b'{"op":"insert","port":2,"profile":"qsfp28-100g-cwdm4"}\n')
    resp = json.loads(raw.decode("utf-8"))
    assert resp == {"ok": True, "port": 2, "profile": "qsfp28-100g-cwdm4"}
    assert sim.is_present(2) is True

    raw = _send_raw(sock_path, b'{"op":"remove","port":2}\n')
    resp = json.loads(raw.decode("utf-8"))
    assert resp == {"ok": True, "port": 2}
    assert sim.is_present(2) is False


def test_control_server_validates_inputs(control_socket):
    sock_path, _server, _sim = control_socket

    raw = _send_raw(sock_path, b'{"op":"insert","port":"two"}\n')
    resp = json.loads(raw.decode("utf-8"))
    assert resp["ok"] is False
    assert "port" in resp["error"]

    raw = _send_raw(sock_path, b'{"op":"insert","port":99}\n')
    resp = json.loads(raw.decode("utf-8"))
    assert resp["ok"] is False

    raw = _send_raw(sock_path, b'{"op":"nope"}\n')
    resp = json.loads(raw.decode("utf-8"))
    assert resp["ok"] is False
    assert resp["error"] == "unknown op"


def test_control_server_rejects_malformed_json(control_socket):
    sock_path, _server, _sim = control_socket

    raw = _send_raw(sock_path, b"not json\n")
    resp = json.loads(raw.decode("utf-8"))
    assert resp["ok"] is False
    assert "invalid JSON" in resp["error"]


def test_control_server_rejects_oversized_payload(control_socket):
    sock_path, _server, _sim = control_socket

    # Construct a valid-looking payload larger than MAX_MSG_BYTES.
    blob = b'{"op":"insert","port":1,"profile":"' + (b"a" * (MAX_MSG_BYTES + 16)) + b'"}\n'
    raw = _send_raw(sock_path, blob)
    resp = json.loads(raw.decode("utf-8"))
    assert resp["ok"] is False
    assert "too large" in resp["error"]


def test_control_server_dom_set_and_list(control_socket):
    sock_path, _server, sim = control_socket

    raw = _send_raw(sock_path, b'{"op":"insert","port":1}\n')
    assert json.loads(raw.decode("utf-8"))["ok"] is True

    raw = _send_raw(sock_path, b'{"op":"dom_set","port":1,"field":"temperature","value":71.5}\n')
    resp = json.loads(raw.decode("utf-8"))
    assert resp == {"ok": True, "port": 1, "field": "temperature", "value": 71.5}
    assert sim.dom_get(1, "temperature") == 71.5

    raw = _send_raw(sock_path, b'{"op":"list"}\n')
    resp = json.loads(raw.decode("utf-8"))
    assert resp["ok"] is True
    assert resp["state"]["1"]["present"] is True


def test_control_server_socket_file_permissions(tmp_path):
    """Verify the UDS file mode is 0o660 so non-group members can't connect."""
    sim = SimState.instance(port_count=4)
    sock_path = str(tmp_path / "perm.sock")
    server = ControlServer(sim, socket_path=sock_path, socket_mode=0o660)
    server.start()
    try:
        mode = os.stat(sock_path).st_mode & 0o777
        assert mode == 0o660
    finally:
        server.stop()
    # Socket file must be cleaned up on stop().
    assert not os.path.exists(sock_path)


def test_control_server_refuses_to_overwrite_regular_file(tmp_path):
    sim = SimState.instance(port_count=4)
    regular = tmp_path / "not_a_socket"
    regular.write_text("hello")
    server = ControlServer(sim, socket_path=str(regular))
    with pytest.raises(RuntimeError):
        server.start()


# ---------------------------------------------------------------------------
# simctl CLI
# ---------------------------------------------------------------------------


def test_simctl_insert_returns_zero(control_socket, capsys):
    sock_path, _server, sim = control_socket

    rc = simctl.main(["--socket", sock_path, "insert", "1", "--profile", "qsfp28-100g-cwdm4"])
    out = capsys.readouterr().out.strip()
    assert rc == 0
    assert json.loads(out)["ok"] is True
    assert sim.is_present(1) is True


def test_simctl_unknown_socket_returns_nonzero(tmp_path, capsys):
    missing = str(tmp_path / "absent.sock")
    rc = simctl.main(["--socket", missing, "remove", "1"])
    out = capsys.readouterr().out.strip()
    assert rc == 1
    assert json.loads(out)["ok"] is False


def test_simctl_list_command(control_socket, capsys):
    sock_path, _server, _sim = control_socket
    rc = simctl.main(["--socket", sock_path, "list"])
    out = capsys.readouterr().out.strip()
    assert rc == 0
    parsed = json.loads(out)
    assert parsed["ok"] is True
    assert isinstance(parsed["state"], dict)
