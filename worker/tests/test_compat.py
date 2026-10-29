"""
Tests for ansys_analyze_worker.compat -- the psutil workaround for Windows
11 26H2 (build 26300), where listening TCP sockets come back from
`GetExtendedTcpTable` with state 0 and psutil reports them as `NONE`
instead of `LISTEN`, so PyAEDT can never find the AEDT it just launched
(see compat.py's module docstring and
https://github.com/d-s-t/ansys-analyze-worker/issues/11).

The mapping tests run on any platform: they swap in a stand-in for
`psutil._pswindows` (which can't be imported off Windows) and push a raw
state 0 through the real `psutil._common.conn_to_ntuple`, the function that
turns psutil's C rows into the `status` strings PyAEDT reads. The last test
class runs against the real thing and is skipped off Windows.

Needs `psutil` (a dependency of PyAEDT, and so always installed next to the
worker); the whole module is skipped without it. No AEDT, PyAEDT or license
needed:

    python -m unittest tests.test_compat -v   # from worker/
"""
from __future__ import annotations

import os
import socket
import sys
import types
import unittest
from unittest import mock

try:
    import psutil
    from psutil import _common
except ImportError:  # pragma: no cover
    psutil = None

from ansys_analyze_worker import compat

# psutil 7.2.2's `_pswindows.TCP_STATUSES`, as printed in the issue: no entry
# for 0, which is what Windows build 26300 returns for a listening socket.
PSUTIL_7_2_2_TCP_STATUSES = {
    5: "ESTABLISHED",
    3: "SYN_SENT",
    4: "SYN_RECV",
    6: "FIN_WAIT1",
    7: "FIN_WAIT2",
    11: "TIME_WAIT",
    1: "CLOSE",
    8: "CLOSE_WAIT",
    10: "LAST_ACK",
    2: "LISTEN",
    9: "CLOSING",
    12: "DELETE_TCB",
    128: "NONE",
}


def _status_of(raw_state: int, status_map: dict) -> str:
    """What psutil would report for a raw TCP state, via the real converter."""
    conn = _common.conn_to_ntuple(
        -1, socket.AF_INET, socket.SOCK_STREAM, ("127.0.0.1", 50000), (), raw_state, status_map
    )
    return conn.status


@unittest.skipIf(psutil is None, "psutil is not installed")
class ApplyPsutilListenFixTests(unittest.TestCase):
    def setUp(self):
        self.status_map = dict(PSUTIL_7_2_2_TCP_STATUSES)
        self.fake_pswindows = types.ModuleType("psutil._pswindows")
        self.fake_pswindows.TCP_STATUSES = self.status_map
        for patcher in (
            mock.patch.object(sys, "platform", "win32"),
            # Patch the attribute, not sys.modules: `from psutil import _pswindows` looks at the
            # package attribute first, and on Windows that is already the real module. create=True
            # because off Windows the attribute does not exist at all.
            mock.patch.object(psutil, "_pswindows", self.fake_pswindows, create=True),
            mock.patch.object(compat, "_fix_applied", False),
        ):
            patcher.start()
            self.addCleanup(patcher.stop)

    def test_without_the_fix_state_0_is_none(self):
        # Pins the premise of the whole workaround.
        self.assertEqual(_status_of(0, self.status_map), "NONE")

    def test_state_0_is_listen_after_the_fix(self):
        self.assertTrue(compat.apply_psutil_listen_fix())
        self.assertEqual(_status_of(0, self.status_map), "LISTEN")

    def test_other_states_are_untouched(self):
        before = dict(self.status_map)
        compat.apply_psutil_listen_fix()
        self.assertEqual({k: v for k, v in self.status_map.items() if k != 0}, before)
        self.assertEqual(_status_of(5, self.status_map), "ESTABLISHED")
        self.assertEqual(_status_of(128, self.status_map), "NONE")

    def test_does_not_override_an_existing_mapping(self):
        # A future psutil that maps 0 itself must win over the shim.
        self.status_map[0] = "SOMETHING_ELSE"
        compat.apply_psutil_listen_fix()
        self.assertEqual(self.status_map[0], "SOMETHING_ELSE")

    def test_second_call_is_a_no_op_and_logs_nothing(self):
        compat.apply_psutil_listen_fix()
        with mock.patch.object(compat.logger, "info") as info:
            self.assertTrue(compat.apply_psutil_listen_fix())
        info.assert_not_called()

    def test_logs_once_with_the_issue_link(self):
        with self.assertLogs(compat.logger, level="INFO") as logs:
            compat.apply_psutil_listen_fix()
        self.assertEqual(len(logs.records), 1)
        self.assertIn(compat.ISSUE_URL, logs.output[0])

    def test_not_windows_changes_nothing(self):
        with mock.patch.object(sys, "platform", "linux"):
            self.assertFalse(compat.apply_psutil_listen_fix())
        self.assertNotIn(0, self.status_map)

    def test_unexpected_psutil_internals_return_false_instead_of_raising(self):
        del self.fake_pswindows.TCP_STATUSES
        with self.assertLogs(compat.logger, level="WARNING"):
            self.assertFalse(compat.apply_psutil_listen_fix())


@unittest.skipIf(psutil is None, "psutil is not installed")
class CheckPsutilListenStatusTests(unittest.TestCase):
    def test_passes_when_psutil_reports_listen(self):
        # On Linux and on Windows builds without the bug this is psutil as
        # shipped; on an affected Windows build the real-Windows test below
        # covers the shimmed case.
        if sys.platform == "win32":
            compat.apply_psutil_listen_fix()
        with self.assertLogs(compat.logger, level="INFO") as logs:
            self.assertEqual(compat.check_psutil_listen_status(), "LISTEN")
        self.assertFalse([r for r in logs.records if r.levelname == "ERROR"])

    def test_logs_an_error_pointing_at_the_issue_when_status_is_not_listen(self):
        original = psutil.Process.net_connections

        def as_none(process, kind="inet"):
            return [c._replace(status=psutil.CONN_NONE) for c in original(process, kind)]

        with mock.patch.object(psutil.Process, "net_connections", as_none):
            with self.assertLogs(compat.logger, level="ERROR") as logs:
                self.assertEqual(compat.check_psutil_listen_status(), "NONE")
        self.assertEqual(len(logs.records), 1)
        self.assertIn(compat.ISSUE_URL, logs.output[0])

    def test_errors_and_returns_none_when_the_listener_is_not_listed_at_all(self):
        with mock.patch.object(psutil.Process, "net_connections", lambda process, kind="inet": []):
            with self.assertLogs(compat.logger, level="ERROR"):
                self.assertIsNone(compat.check_psutil_listen_status())

    def test_a_failing_check_warns_and_returns_none(self):
        with mock.patch.object(psutil, "Process", side_effect=psutil.AccessDenied()):
            with self.assertLogs(compat.logger, level="WARNING"):
                self.assertIsNone(compat.check_psutil_listen_status())


def _fake_pyaedt(desktop_class):
    """sys.modules entries that make `from ansys.aedt.core.desktop import Desktop` give `desktop_class`."""
    modules = {}
    for name in ("ansys", "ansys.aedt", "ansys.aedt.core", "ansys.aedt.core.desktop"):
        module = types.ModuleType(name)
        module.__path__ = []
        modules[name] = module
    modules["ansys.aedt.core.desktop"].Desktop = desktop_class
    return modules


class SilenceFailedDesktopCleanupTests(unittest.TestCase):
    """PyAEDT's Desktop.__del__ must not run for a Desktop whose constructor failed (odesktop is None)."""

    def setUp(self):
        self.released = []
        released = self.released

        class Desktop:
            def __init__(self, odesktop):
                self._odesktop = odesktop

            @property
            def odesktop(self):
                if self._odesktop == "explode":
                    raise RuntimeError("property blew up")
                return self._odesktop

            def __del__(self):
                released.append(self._odesktop)

        self.Desktop = Desktop
        patcher = mock.patch.dict(sys.modules, _fake_pyaedt(Desktop))
        patcher.start()
        self.addCleanup(patcher.stop)

    def drop(self, odesktop):
        """Build a Desktop and let it be garbage collected, so __del__ runs once, as it does for real."""
        import gc

        desktop = self.Desktop(odesktop)
        del desktop
        gc.collect()

    def test_a_desktop_that_never_got_a_session_is_not_released(self):
        self.assertTrue(compat.silence_failed_desktop_cleanup())
        self.drop(None)
        self.assertEqual(self.released, [])

    def test_a_desktop_with_a_session_is_released_exactly_as_before(self):
        compat.silence_failed_desktop_cleanup()
        self.drop("session")
        self.assertEqual(self.released, ["session"])

    def test_an_odesktop_property_that_raises_is_skipped_not_raised(self):
        compat.silence_failed_desktop_cleanup()
        self.drop("explode")
        self.assertEqual(self.released, [])

    def test_applying_twice_does_not_wrap_twice(self):
        self.assertTrue(compat.silence_failed_desktop_cleanup())
        guard = self.Desktop.__dict__["__del__"]
        self.assertTrue(compat.silence_failed_desktop_cleanup())
        self.assertIs(self.Desktop.__dict__["__del__"], guard)
        self.drop("session")
        self.assertEqual(self.released, ["session"])

    def test_a_desktop_without_del_is_left_alone(self):
        del self.Desktop.__del__
        self.assertFalse(compat.silence_failed_desktop_cleanup())

    def test_pyaedt_not_importable_returns_false_with_a_warning(self):
        with mock.patch.dict(sys.modules, {"ansys.aedt.core.desktop": None}):
            with self.assertLogs(compat.logger, level="WARNING"):
                self.assertFalse(compat.silence_failed_desktop_cleanup())


class WorkerWiringTests(unittest.TestCase):
    """Worker._ensure_desktop applies both PyAEDT workarounds before it builds the Desktop."""

    def test_workarounds_are_applied_before_desktop_is_constructed(self):
        import tempfile

        from ansys_analyze_common.queue_common import QueuePaths
        from ansys_analyze_worker.worker import Worker

        order = []

        class Desktop:
            def __init__(self, **kwargs):
                order.append(("Desktop", kwargs))

        with tempfile.TemporaryDirectory() as tmp:
            worker = Worker(QueuePaths(tmp), aedt_version="2025.2", non_graphical=True)
            with mock.patch.dict(sys.modules, _fake_pyaedt(Desktop)), \
                    mock.patch.object(compat, "apply_psutil_listen_fix", lambda: order.append("psutil")), \
                    mock.patch.object(compat, "silence_failed_desktop_cleanup", lambda: order.append("del-guard")):
                sys.modules["ansys.aedt.core"].Desktop = Desktop
                self.assertIsInstance(worker._ensure_desktop(), Desktop)
        self.assertEqual(order, ["psutil", "del-guard",
                                 ("Desktop", {"version": "2025.2", "new_desktop": False, "non_graphical": True})])


def _real_pyaedt_without_aedt():
    """Real PyAEDT importable, and no AEDT installed (so building a Desktop fails before it can attach to anything)."""
    if any(name.startswith(("ANSYSEM_ROOT", "AWP_ROOT")) for name in os.environ):
        return False
    try:
        import ansys.aedt.core  # noqa: F401
    except Exception:
        return False
    return True


@unittest.skipUnless(_real_pyaedt_without_aedt(), "needs real PyAEDT on a machine with no AEDT installed")
class RealPyaedtDesktopTests(unittest.TestCase):
    """The reported log noise, against the real class. Skipped wherever AEDT is installed, so it can never attach to a session."""

    def test_a_failed_desktop_leaves_no_cleanup_traceback(self):
        import gc

        from ansys.aedt.core import Desktop

        self.addCleanup(setattr, Desktop, "__del__", Desktop.__dict__["__del__"])
        unraisable = []
        previous_hook = sys.unraisablehook
        sys.unraisablehook = unraisable.append
        self.addCleanup(setattr, sys, "unraisablehook", previous_hook)

        compat.silence_failed_desktop_cleanup()
        with self.assertRaises(Exception):
            Desktop(version="2025.2", new_desktop=False, non_graphical=True)
        gc.collect()

        self.assertEqual([u.exc_value for u in unraisable], [])


@unittest.skipUnless(psutil is not None and sys.platform == "win32", "needs psutil on Windows")
class RealWindowsTests(unittest.TestCase):
    def test_a_plain_listener_is_listen_after_the_fix(self):
        # The end-to-end check of the issue: on build 26300 this is "NONE"
        # without the shim and "LISTEN" with it.
        self.assertTrue(compat.apply_psutil_listen_fix())
        self.assertEqual(compat.check_psutil_listen_status(), "LISTEN")


if __name__ == "__main__":
    unittest.main()
