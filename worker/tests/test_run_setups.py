"""
Integration test for the `run_setups` handler's HPC wiring: confirms
`setup.analyze(**hpc_options)` is actually called with keyword names
PyAEDT's real `Setup.analyze()` accepts (`cores`/`tasks`/`gpus`), not
just that `hpc.resolve_hpc_options()` computes the right dict in
isolation (see test_hpc.py for that). If a future PyAEDT release
renames or drops one of those keywords, this is what would catch it
before a real solve does, with a `TypeError` at the call site instead
of a green test suite.

The stub setup/hfss objects below mirror PyAEDT 1.0.0's actual
`Setup.analyze()` signature (`ansys.aedt.core.modules.solve_setup.
Setup.analyze`) closely enough to catch a keyword mismatch, without
depending on `ansys.aedt.core`/AEDT/a license being available -- see
this repo's docs/ARCHITECTURE.md §12 for why pyaedt==1.0.0 is pinned.

    python -m unittest tests.test_run_setups -v   # from worker/
"""
from __future__ import annotations

import os
import unittest
from typing import Optional
from unittest import mock

from ansys_analyze_worker.handlers import run_setups
from ansys_analyze_worker import hpc


class StubSetup:
    """Mirrors PyAEDT 1.0.0's Setup.analyze() signature closely enough
    to catch a keyword-argument mismatch at the call site."""

    def __init__(self, name: str):
        self.name = name
        self.calls: list = []
        self.is_solved = True

    def analyze(
        self,
        cores: Optional[int] = 1,
        tasks: Optional[int] = 1,
        gpus: Optional[int] = 0,
        acf_file: Optional[str] = None,
        use_auto_settings: bool = True,
        solve_in_batch: bool = False,
        machine: str = "localhost",
        run_in_thread: bool = False,
        revert_to_initial_mesh: bool = False,
        blocking: bool = True,
    ) -> None:
        # PyAEDT 1.0.0 declares this -> None -- run_setups relies on
        # that (`setup.analyze() or setup.is_solved`), see its own
        # docstring's "Caveat on `success`" section.
        self.calls.append({"cores": cores, "tasks": tasks, "gpus": gpus})
        return None


class StubHfss:
    def __init__(self, setup_names):
        self._setups = {name: StubSetup(name) for name in setup_names}
        self.setup_names = list(self._setups)
        self.saved_to = None

    def get_setup(self, name: str) -> StubSetup:
        return self._setups[name]

    def save_project(self, path: str) -> None:
        self.saved_to = path


class RunSetupsHpcWiringTests(unittest.TestCase):
    def setUp(self):
        # mock.patch.dict snapshots os.environ now and restores it
        # verbatim in addCleanup (run even on failure/error) -- a plain
        # `os.environ.pop(...)` in setUp alone only cleans the slate
        # *before* each test, not after, so a value a test method sets
        # (ANSYS_ANALYZE_CORES="8" below, for instance) would otherwise
        # leak into every test that runs after it in the same process.
        self._patcher = mock.patch.dict(os.environ, {}, clear=False)
        self._patcher.start()
        self.addCleanup(self._patcher.stop)
        for var in (hpc.CORES_ENV_VAR, hpc.TASKS_ENV_VAR, hpc.GPUS_ENV_VAR):
            os.environ.pop(var, None)
        self.logs: list = []

    def test_analyze_is_called_with_resolved_cores(self):
        hfss = StubHfss(["Setup1"])
        with mock.patch.object(hpc.os, "cpu_count", return_value=16):
            result = run_setups.run(hfss, {"project_file": "p.aedt"}, log=self.logs.append)

        setup = hfss.get_setup("Setup1")
        self.assertEqual(setup.calls, [{"cores": 16, "tasks": None, "gpus": None}])
        self.assertEqual(result, {"Setup1": {"success": True}})
        self.assertEqual(hfss.saved_to, "p.aedt")

    def test_analyze_is_called_per_setup_with_the_same_options(self):
        hfss = StubHfss(["Setup1", "Setup2"])
        os.environ[hpc.CORES_ENV_VAR] = "8"
        os.environ[hpc.GPUS_ENV_VAR] = "1"
        result = run_setups.run(hfss, {"project_file": "p.aedt"}, log=self.logs.append)

        for name in ("Setup1", "Setup2"):
            self.assertEqual(hfss.get_setup(name).calls, [{"cores": 8, "tasks": None, "gpus": 1}])
        self.assertEqual(result, {"Setup1": {"success": True}, "Setup2": {"success": True}})

    def test_log_line_mentions_the_resolved_cores(self):
        hfss = StubHfss(["Setup1"])
        os.environ[hpc.CORES_ENV_VAR] = "16"
        run_setups.run(hfss, {"project_file": "p.aedt"}, log=self.logs.append)
        self.assertTrue(any("16 core(s)" in line for line in self.logs))

    def test_no_setups_raises_before_touching_hpc_options(self):
        hfss = StubHfss([])
        with self.assertRaises(RuntimeError):
            run_setups.run(hfss, {"project_file": "p.aedt"}, log=self.logs.append)


if __name__ == "__main__":
    unittest.main()
