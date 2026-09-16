"""
SimState - shared in-memory state for the runtime transceiver simulator.

A single :class:`SimState` instance owns:

* the presence map of every simulated physical port,
* the EEPROM blob for each present module,
* a thread-safe queue of pending change events (insert / remove) that the
  next ``Chassis.get_change_event()`` call will drain,
* a pending error map keyed by physical port.

All state mutation is guarded by an :class:`threading.RLock` because
``xcvrd`` reads from multiple threads concurrently (the event-poll thread,
``DomInfoUpdateTask`` and ``CmisManagerTask``).
"""

from __future__ import annotations

import os
import queue
import threading
import time
from typing import Dict, List, Optional, Tuple

# Status codes that match sfp_status_helper in xcvrd. We re-declare them here
# instead of importing so the mock package has no build-time dependency on
# sonic-platform-daemons.
SFP_STATUS_REMOVED = "0"
SFP_STATUS_INSERTED = "1"

# Default EEPROM size for SFF-8636 / CMIS (page 0 lower + upper = 256 bytes).
# For CMIS we keep four banks of upper memory; the layout below is sufficient
# for xcvrd's parsing of identifier / vendor / serial fields and for CMIS
# write_eeprom round-tripping (state machine writes to lower page registers).
_EEPROM_SIZE = 640

# Whitelist of supported EEPROM profile names. Profiles are baked-in below so
# the mock works without any on-disk template files; the external package
# version may load them from ``profiles/<name>.bin``.
_SUPPORTED_PROFILES = (
    "qsfp28-100g-cwdm4",
    "qsfpdd-400g-dr4",
    "sfp-1g-t",
    "qsfp-passive-copper",
)


def _build_qsfp28_100g_cwdm4() -> bytearray:
    blob = bytearray(_EEPROM_SIZE)
    # SFF-8636 identifier byte 0 = 0x11 (QSFP28)
    blob[0] = 0x11
    # Status byte
    blob[2] = 0x00
    # Bytes 148..163: vendor name (16 ASCII, space padded)
    vendor = b"MOCK VENDOR     "
    blob[148:148 + len(vendor)] = vendor
    # Bytes 168..175: vendor OUI (3 bytes) - leave zero
    # Bytes 168..183: vendor part number
    pn = b"MOCK-100G-CWDM4 "
    blob[168:168 + len(pn)] = pn
    # Bytes 184..185: revision
    blob[184:186] = b"01"
    # Bytes 196..211: vendor serial number
    sn = b"MOCKSN0000000001"
    blob[196:196 + len(sn)] = sn
    # Bytes 212..217: date code
    blob[212:218] = b"260101"
    return blob


def _build_qsfpdd_400g_dr4() -> bytearray:
    blob = bytearray(_EEPROM_SIZE)
    # CMIS identifier byte 0 = 0x18 (QSFP-DD/CMIS)
    blob[0] = 0x18
    # Bytes 129..144: vendor name (CMIS lower page is different layout, but
    # xcvrd's SFF parser tolerates either; the real external package ships
    # full CMIS-compliant profiles).
    vendor = b"MOCK VENDOR     "
    blob[129:129 + len(vendor)] = vendor
    pn = b"MOCK-400G-DR4   "
    blob[148:148 + len(pn)] = pn
    sn = b"MOCKSN0000000002"
    blob[166:166 + len(sn)] = sn
    return blob


def _build_sfp_1g_t() -> bytearray:
    blob = bytearray(_EEPROM_SIZE)
    # SFF-8472 identifier byte 0 = 0x03 (SFP)
    blob[0] = 0x03
    # Vendor name at bytes 20..35
    blob[20:36] = b"MOCK VENDOR     "
    # PN at 40..55
    blob[40:56] = b"MOCK-1000BASE-T "
    # SN at 68..83
    blob[68:84] = b"MOCKSN0000000003"
    return blob


def _build_qsfp_passive_copper() -> bytearray:
    blob = bytearray(_EEPROM_SIZE)
    blob[0] = 0x0D  # QSFP+
    blob[148:164] = b"MOCK VENDOR     "
    blob[168:184] = b"MOCK-QSFP-DAC1M "
    blob[196:212] = b"MOCKSN0000000004"
    return blob


_PROFILE_BUILDERS = {
    "qsfp28-100g-cwdm4": _build_qsfp28_100g_cwdm4,
    "qsfpdd-400g-dr4": _build_qsfpdd_400g_dr4,
    "sfp-1g-t": _build_sfp_1g_t,
    "qsfp-passive-copper": _build_qsfp_passive_copper,
}


# Valid error codes accepted by ``set_error`` and surfaced to xcvrd through
# ``Chassis.get_change_event()``. Mirrors sfp_status_helper constants.
_SUPPORTED_ERROR_CODES = (
    "blocking",
    "i2c_stuck",
    "bad_eeprom",
    "unsupported_cable",
    "high_temp",
    "bad_cable",
)


class SimState:
    """Process-wide singleton holding simulated transceiver state."""

    _instance: Optional["SimState"] = None
    _instance_lock = threading.Lock()

    @classmethod
    def instance(cls, port_count: Optional[int] = None) -> "SimState":
        """Return the process-wide :class:`SimState`, creating it on first use."""
        with cls._instance_lock:
            if cls._instance is None:
                if port_count is None:
                    port_count = int(os.environ.get("MOCK_SFP_COUNT", "32"))
                cls._instance = cls(port_count)
            return cls._instance

    @classmethod
    def reset_for_tests(cls) -> None:
        """Tear down the singleton. Test-only helper."""
        with cls._instance_lock:
            cls._instance = None

    def __init__(self, port_count: int) -> None:
        if port_count <= 0:
            raise ValueError("port_count must be positive")
        self._lock = threading.RLock()
        self._port_count = port_count
        self._presence: Dict[int, bool] = {p: False for p in range(1, port_count + 1)}
        self._eeprom: Dict[int, bytearray] = {}
        self._pending: "queue.Queue[Tuple[int, str]]" = queue.Queue()
        # Buffered errors: physical_port -> error code string. Drained on the
        # next call to ``drain_events``.
        self._pending_errors: Dict[int, str] = {}
        # DOM telemetry, keyed by (port, field). Populated by ``dom_set``.
        self._dom: Dict[Tuple[int, str], float] = {}

    # ----- introspection ---------------------------------------------------

    @property
    def port_count(self) -> int:
        return self._port_count

    def supported_profiles(self) -> List[str]:
        return list(_SUPPORTED_PROFILES)

    def list_state(self) -> Dict[int, Dict[str, object]]:
        """Return a snapshot of presence/profile state. Used by ``list`` op."""
        with self._lock:
            out: Dict[int, Dict[str, object]] = {}
            for port, present in self._presence.items():
                out[port] = {
                    "present": present,
                    "eeprom_bytes": len(self._eeprom.get(port, b"")),
                }
            return out

    # ----- presence / EEPROM operations -----------------------------------

    def _validate_port(self, port: int) -> None:
        if not isinstance(port, int):
            raise TypeError("port must be int")
        if port < 1 or port > self._port_count:
            raise ValueError(
                "port {} out of range [1, {}]".format(port, self._port_count)
            )

    def insert(self, port: int, profile: str = "qsfp28-100g-cwdm4") -> None:
        """Mark *port* as present, load its EEPROM, enqueue an insert event."""
        self._validate_port(port)
        if profile not in _PROFILE_BUILDERS:
            raise ValueError("unknown profile {!r}".format(profile))
        with self._lock:
            self._eeprom[port] = _PROFILE_BUILDERS[profile]()
            self._presence[port] = True
        self._pending.put((port, SFP_STATUS_INSERTED))

    def remove(self, port: int) -> None:
        """Mark *port* as absent and enqueue a remove event."""
        self._validate_port(port)
        with self._lock:
            self._presence[port] = False
            # Zero the EEPROM so subsequent reads return all 0xFF (absent),
            # matching real hardware behavior on most platforms.
            self._eeprom.pop(port, None)
        self._pending.put((port, SFP_STATUS_REMOVED))

    def set_error(self, port: int, code: str) -> None:
        self._validate_port(port)
        if code not in _SUPPORTED_ERROR_CODES:
            raise ValueError("unsupported error code {!r}".format(code))
        with self._lock:
            self._pending_errors[port] = code

    def dom_set(self, port: int, field: str, value: float) -> None:
        self._validate_port(port)
        if not isinstance(field, str) or not field:
            raise ValueError("field must be a non-empty string")
        with self._lock:
            self._dom[(port, field)] = float(value)

    def dom_get(self, port: int, field: str) -> Optional[float]:
        with self._lock:
            return self._dom.get((port, field))

    def is_present(self, port: int) -> bool:
        with self._lock:
            return self._presence.get(port, False)

    def read_eeprom(self, port: int, offset: int, num_bytes: int) -> Optional[bytearray]:
        """Return ``num_bytes`` from the EEPROM, or ``None`` if not present."""
        if offset < 0 or num_bytes < 0:
            raise ValueError("offset and num_bytes must be non-negative")
        with self._lock:
            blob = self._eeprom.get(port)
            if blob is None:
                return None
            end = min(offset + num_bytes, len(blob))
            return bytearray(blob[offset:end])

    def write_eeprom(self, port: int, offset: int, data: bytes) -> bool:
        """Persist a write into the simulated EEPROM. Returns success."""
        if offset < 0:
            raise ValueError("offset must be non-negative")
        with self._lock:
            blob = self._eeprom.get(port)
            if blob is None:
                return False
            end = offset + len(data)
            if end > len(blob):
                # Grow the blob so CMIS state-machine writes to upper banks work.
                blob.extend(b"\x00" * (end - len(blob)))
            blob[offset:end] = data
            return True

    # ----- event queue API used by Chassis.get_change_event ----------------

    def drain_events(self, timeout_ms: int) -> Tuple[Dict[str, str], Dict[str, str]]:
        """
        Drain pending events for up to ``timeout_ms`` milliseconds.

        Semantics match the documented xcvrd contract:

        * ``timeout_ms == 0`` blocks until at least one event is available.
        * a positive timeout returns whatever has accumulated by the deadline
          (possibly an empty dict).
        * later events for the same port supersede earlier ones, so a rapid
          insert-remove sequence collapses to a single final state.

        Returns ``(port_dict, err_dict)`` where each dict is keyed by the
        physical port number formatted as a string, matching the format
        ``xcvrd._wrapper_get_transceiver_change_event`` expects.
        """
        port_dict: Dict[str, str] = {}
        err_dict: Dict[str, str] = {}

        # Pop any pending errors first so they are reported synchronously.
        with self._lock:
            if self._pending_errors:
                for port, code in self._pending_errors.items():
                    err_dict[str(port)] = code
                self._pending_errors.clear()

        block_forever = timeout_ms == 0
        deadline = None if block_forever else time.monotonic() + timeout_ms / 1000.0

        # First wait: block (with or without timeout) for the first event.
        if not port_dict:
            try:
                if block_forever:
                    port, status = self._pending.get(block=True)
                else:
                    remaining = max(0.0, deadline - time.monotonic())
                    port, status = self._pending.get(block=True, timeout=remaining)
                port_dict[str(port)] = status
            except queue.Empty:
                return port_dict, err_dict

        # Now drain anything else that has accumulated, non-blocking.
        while True:
            try:
                port, status = self._pending.get_nowait()
            except queue.Empty:
                break
            port_dict[str(port)] = status

        return port_dict, err_dict
