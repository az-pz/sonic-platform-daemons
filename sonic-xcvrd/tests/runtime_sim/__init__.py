"""
Reference implementation of the runtime transceiver simulation plugin.

This package is the in-tree prototype of the standalone ``sonic-platform-mock``
package described in ``sonic-xcvrd/docs/runtime-simulation.md``. It is used by
``tests/test_mock_runtime_simulation.py`` to exercise the contract that
``xcvrd`` expects from a platform plugin.

The module layout mirrors the layout of the future external package so the
files can be copied verbatim into ``sonic_platform/`` when the package is
extracted to its own repo:

    sim_state.py       - in-memory state + EEPROM blobs + event queue
    sfp.py             - MockSfp implementing the SFP API surface
    chassis.py         - MockChassis implementing get_change_event/get_sfp
    platform.py        - Platform() factory returning a Chassis
    control_server.py  - JSON-over-UDS control server for xcvrd_simctl
    simctl.py          - thin client (the planned `xcvrd_simctl` console script)
"""
