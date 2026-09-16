"""MockSfp - mock implementation of the SONiC SFP API for the simulator.

This class deliberately does NOT subclass ``SfpOptoeBase`` so the package can
be imported in environments that don't have ``sonic_platform_base`` installed
(such as a bare CI runner). The external ``sonic-platform-mock`` package
should subclass ``SfpOptoeBase`` for full CMIS/SFF parsing - see
``docs/runtime-simulation.md``.

Only the subset of the API that ``xcvrd`` calls during the
insert/remove/DOM/CMIS flows is implemented here. Any extra calls fall back
to ``NotImplementedError`` so the missing surface is loud and obvious.
"""

from __future__ import annotations

from typing import Optional

from .sim_state import SimState


class MockSfp:
    """Mock SFP that proxies all hardware access to :class:`SimState`."""

    def __init__(self, index: int, name: Optional[str] = None) -> None:
        if index < 1:
            raise ValueError("SFP index is 1-based and must be >= 1")
        self._index = index
        # Default naming follows ``Ethernet<4*(idx-1)>`` which matches the
        # ``t0-sample-port-config.ini`` shipped with xcvrd tests; the external
        # package reads ``port_config.ini`` to honor real port aliases.
        self._name = name or "Ethernet{}".format((index - 1) * 4)
        self._sim = SimState.instance()

    # ----- identity --------------------------------------------------------

    def get_name(self) -> str:
        return self._name

    def get_position_in_parent(self) -> int:
        return self._index

    def is_replaceable(self) -> bool:
        return True

    # ----- presence / EEPROM ----------------------------------------------

    def get_presence(self) -> bool:
        return self._sim.is_present(self._index)

    def read_eeprom(self, offset: int, num_bytes: int) -> Optional[bytearray]:
        return self._sim.read_eeprom(self._index, offset, num_bytes)

    def write_eeprom(self, offset: int, num_bytes: int, write_buffer) -> bool:
        # ``write_buffer`` may be a list of ints or a bytes-like; normalize.
        data = bytes(write_buffer[:num_bytes])
        return self._sim.write_eeprom(self._index, offset, data)

    # ----- module controls -------------------------------------------------
    #
    # These store flags in dedicated bytes of the EEPROM blob so subsequent
    # reads are consistent with previous writes. Offsets are chosen above the
    # SFF/CMIS reserved ranges so they don't collide with parsed fields.

    _TX_DISABLE_OFFSET = 600
    _RESET_OFFSET = 601
    _LPMODE_OFFSET = 602

    def tx_disable(self, tx_disable: bool) -> bool:
        return self._sim.write_eeprom(
            self._index, self._TX_DISABLE_OFFSET, bytes([1 if tx_disable else 0])
        )

    def reset(self) -> bool:
        return self._sim.write_eeprom(self._index, self._RESET_OFFSET, bytes([1]))

    def get_lpmode(self) -> bool:
        data = self._sim.read_eeprom(self._index, self._LPMODE_OFFSET, 1)
        return bool(data and data[0])

    def set_lpmode(self, lpmode: bool) -> bool:
        return self._sim.write_eeprom(
            self._index, self._LPMODE_OFFSET, bytes([1 if lpmode else 0])
        )

    # ----- DOM ------------------------------------------------------------

    def get_transceiver_bulk_status(self):
        """Return DOM readings sourced from :class:`SimState`."""
        if not self.get_presence():
            return None
        return {
            "temperature": self._sim.dom_get(self._index, "temperature") or 35.0,
            "voltage": self._sim.dom_get(self._index, "voltage") or 3.3,
            "rx1power": self._sim.dom_get(self._index, "rx1power") or -2.0,
            "tx1bias": self._sim.dom_get(self._index, "tx1bias") or 7.5,
            "tx1power": self._sim.dom_get(self._index, "tx1power") or -1.5,
        }
