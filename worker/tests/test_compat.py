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
        fake_pswindows = types.ModuleType("psutil._pswindows")
        fake_pswindows.TCP_STATUSES = self.status_map
        for patcher in (
            mock.patch.object(sys, "platform", "win32"),
            mock.patch.dict(sys.modules, {"psutil._pswindows": fake_pswindows}),
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
        sys.modules["psutil._pswindows"] = types.ModuleType("psutil._pswindows")  # no TCP_STATUSES
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


@unittest.skipUnless(psutil is not None and sys.platform == "win32", "needs psutil on Windows")
class RealWindowsTests(unittest.TestCase):
    def test_a_plain_listener_is_listen_after_the_fix(self):
        # The end-to-end check of the issue: on build 26300 this is "NONE"
        # without the shim and "LISTEN" with it.
        self.assertTrue(compat.apply_psutil_listen_fix())
        self.assertEqual(compat.check_psutil_listen_status(), "LISTEN")


if __name__ == "__main__":
    unittest.main()
