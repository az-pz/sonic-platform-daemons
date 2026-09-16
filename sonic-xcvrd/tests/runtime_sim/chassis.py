"""MockChassis - mock SONiC chassis exposing the API used by xcvrd."""

from __future__ import annotations

from typing import Dict, List, Tuple

from .sfp import MockSfp
from .sim_state import SimState


class MockChassis:
    """In-memory chassis driving the runtime simulator.

    The class is intentionally compatible with the small subset of
    ``ChassisBase`` that ``xcvrd`` uses. Subclassing the real ``ChassisBase``
    is unnecessary for the simulator to function and would force
    ``sonic_platform_base`` to be installed in every test environment.
    """

    def __init__(self, port_count: int = None, start_control_server: bool = False) -> None:
        # Singleton - ``port_count`` is honored only the first time. This
        # matches how SONiC instantiates a single Chassis per process.
        self._sim = SimState.instance(port_count=port_count)
        self._sfp_list: List[MockSfp] = [
            MockSfp(i) for i in range(1, self._sim.port_count + 1)
        ]
        self._control_server = None
        if start_control_server:
            # Imported lazily so unit tests that don't need a UDS don't pay
            # the cost of opening one.
            from .control_server import ControlServer

            self._control_server = ControlServer(self._sim)
            self._control_server.start()

    # ----- SFP accessors --------------------------------------------------

    def get_num_sfps(self) -> int:
        return len(self._sfp_list)

    def get_all_sfps(self) -> List[MockSfp]:
        return list(self._sfp_list)

    def get_sfp(self, index: int) -> MockSfp:
        # SONiC's SFP indices are 1-based (matching physical port numbering).
        if index < 1 or index > len(self._sfp_list):
            raise IndexError("SFP index {} out of range".format(index))
        return self._sfp_list[index - 1]

    # ----- ASIC topology --------------------------------------------------

    def is_modular_chassis(self) -> bool:
        return False

    # ----- event polling --------------------------------------------------

    def get_change_event(self, timeout: int = 0) -> Tuple[bool, Dict[str, Dict[str, str]]]:
        """
        Implements the contract documented in ``xcvrd._wrapper_get_transceiver_change_event``:

        * Returns ``(True, {'sfp': port_dict, 'sfp_error': err_dict})``
        * ``port_dict[str(physical_port)]`` is ``'1'`` for insert, ``'0'`` for remove.
        * Returning ``True`` with an empty ``port_dict`` is a successful idle
          poll - xcvrd must NOT interpret this as ``SYSTEM_FAIL``.
        """
        if timeout < 0:
            raise ValueError("timeout must be non-negative")
        port_dict, err_dict = self._sim.drain_events(timeout)
        return True, {"sfp": port_dict, "sfp_error": err_dict}

    # ----- lifecycle ------------------------------------------------------

    def stop(self) -> None:
        if self._control_server is not None:
            self._control_server.stop()
            self._control_server = None
