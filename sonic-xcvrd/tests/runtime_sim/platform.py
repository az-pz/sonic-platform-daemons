"""Platform factory - the entry point that ``xcvrd`` imports.

In production the xcvrd daemon does::

    import sonic_platform.platform
    chassis = sonic_platform.platform.Platform().get_chassis()

The external ``sonic-platform-mock`` package satisfies that import path by
shipping ``sonic_platform/platform.py``. This in-tree reference module is
used by ``tests/test_mock_runtime_simulation.py`` to instantiate the same
object directly.
"""

from __future__ import annotations

from .chassis import MockChassis


class Platform:
    """Returns a singleton :class:`MockChassis`."""

    _chassis: MockChassis = None

    def __init__(self, start_control_server: bool = False) -> None:
        if Platform._chassis is None:
            Platform._chassis = MockChassis(start_control_server=start_control_server)

    def get_chassis(self) -> MockChassis:
        return Platform._chassis
