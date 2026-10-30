"""
Tests for `parameters.analyze: false` on `run_setups`: open the project, skip
the solve and the save, and let the task's `field_exports` run against what
is already solved (the request on
https://github.com/d-s-t/ansys-analyze-worker/pull/13 -- `run_setups` always
re-solves an Eigenmode project, which took 17 to 54 minutes per test).

Same stand-ins as test_field_exports.py; no AEDT needed:

    python -m unittest tests.test_analyze_flag -v   # from worker/
"""
from __future__ import annotations

import json
import logging
import os
import tempfile
import unittest
from unittest import mock

from ansys_analyze_common import queue_common
from ansys_analyze_common.queue_common import QueuePaths
from ansys_analyze_worker.handlers import get_handler, run_setups
from ansys_analyze_worker.worker import Worker, _is_read_only
from tests.test_field_exports import DRIVEN_ENTRY, FakeFieldsReporter, FakeHfss

logging.getLogger("ansys_analyze_worker").addHandler(logging.NullHandler())


class FakeSetup:
    """analyze() succeeds (returns None, like PyAEDT 1.0.0's `-> None`); is_solved is whatever it is told."""

    def __init__(self, solved=True):
        self._solved = solved
        self.analyze = mock.Mock(return_value=None)

    @property
    def is_solved(self):
        if isinstance(self._solved, Exception):
            raise self._solved
        return self._solved


class FakeProject(FakeHfss):
    def __init__(self, reporter, calls, setups, **kwargs):
        super().__init__(reporter, calls, **kwargs)
        self.setups_by_name = setups
        self.setup_names = list(setups)
        self.save_project = mock.Mock()

    def get_setup(self, name):
        return self.setups_by_name.get(name)


def task_with(**parameters):
    return {"project_file": "p.aedt", "parameters": parameters}


def entry(name):
    result = {**DRIVEN_ENTRY, "name": name}
    result.pop("sources")
    return result


class HandlerTests(unittest.TestCase):
    def project(self, **setups):
        return FakeProject(FakeFieldsReporter([], rows=3), [], setups)

    def test_by_default_and_when_true_the_setups_are_analyzed_and_saved(self):
        for task in ({"project_file": "p.aedt"}, task_with(), task_with(analyze=True)):
            with self.subTest(task=task):
                setup = FakeSetup(True)
                hfss = self.project(Setup_1=setup)
                with mock.patch.object(run_setups, "resolve_hpc_options", return_value={"cores": 4}):
                    result = run_setups.run(hfss, task, log=lambda m: None)
                setup.analyze.assert_called_once_with(cores=4)
                hfss.save_project.assert_called_once_with("p.aedt")
                self.assertEqual(result, {"Setup_1": {"success": True}})

    def test_analyze_false_never_solves_never_saves_and_never_touches_hpc_options(self):
        setup = FakeSetup(True)
        hfss = self.project(Setup_1=setup)
        with mock.patch.object(run_setups, "resolve_hpc_options", side_effect=AssertionError("hpc touched")):
            result = run_setups.run(hfss, task_with(analyze=False), log=lambda m: None)
        setup.analyze.assert_not_called()
        hfss.save_project.assert_not_called()
        self.assertEqual(result, {"Setup_1": {"success": True, "analyzed": False}})

    def test_every_setup_is_reported(self):
        result = run_setups.run(self.project(A=FakeSetup(True), B=FakeSetup(False)), task_with(analyze=False),
                                log=lambda m: None)
        self.assertEqual(result, {"A": {"success": True, "analyzed": False}, "B": {"success": False, "analyzed": False}})

    def test_an_unsolved_looking_setup_is_logged_and_reported_not_refused(self):
        with self.assertLogs("ansys_analyze_worker", level="WARNING") as logs:
            result = run_setups.run(self.project(Setup_1=FakeSetup(False)), task_with(analyze=False), log=lambda m: None)
        self.assertEqual(result["Setup_1"]["success"], False)
        self.assertIn("Setup_1 reports no solution", logs.output[0])

    def test_a_setup_that_cannot_be_asked_is_unknown_not_unsolved(self):
        for setups in ({"Setup_1": None}, {"Setup_1": FakeSetup(RuntimeError("report failed"))}):
            with self.subTest(setups=setups):
                result = run_setups.run(self.project(**setups), task_with(analyze=False), log=lambda m: None)
                self.assertEqual(result["Setup_1"], {"success": None, "analyzed": False})

    def test_a_project_with_no_setup_still_fails_the_same_way(self):
        with self.assertRaisesRegex(RuntimeError, "Project has no analysis setup"):
            run_setups.run(self.project(), task_with(analyze=False), log=lambda m: None)

    def test_only_a_real_false_turns_the_solve_off(self):
        # queue_common rejects non-booleans before queuing; the handler itself must not read "false" as off or on.
        for value in (None, 0, "", "false", "no", [], 1):
            with self.subTest(analyze=value):
                self.assertTrue(run_setups._analyze_requested(task_with(analyze=value)))
        self.assertFalse(run_setups._analyze_requested(task_with(analyze=False)))


class ReadOnlyHookTests(unittest.TestCase):
    def test_run_setups_is_read_only_only_when_analyze_is_false(self):
        self.assertIs(get_handler("run_setups").read_only, run_setups._is_read_only)
        self.assertIs(get_handler("eigenmode_analyze").read_only, run_setups._is_read_only)
        self.assertTrue(run_setups._is_read_only(task_with(analyze=False)))
        for task in ({}, task_with(), task_with(analyze=True)):
            self.assertFalse(run_setups._is_read_only(task))

    def test_the_worker_only_honours_a_real_true_from_a_callable(self):
        handler = mock.Mock()
        self.assertFalse(_is_read_only(handler, {}))  # a Mock's auto-created attribute must not count
        self.assertFalse(_is_read_only(object(), {}))  # no attribute at all

        def handler_function(hfss, task, log): ...

        handler_function.read_only = lambda task: True
        self.assertTrue(_is_read_only(handler_function, {}))
        handler_function.read_only = lambda task: "yes"
        self.assertFalse(_is_read_only(handler_function, {}))
        handler_function.read_only = mock.Mock(side_effect=RuntimeError("boom"))
        self.assertFalse(_is_read_only(handler_function, {}))
        handler_function.read_only = True  # the old non-callable form is no longer a thing
        self.assertFalse(_is_read_only(handler_function, {}))


class WorkerIntegrationTests(unittest.TestCase):
    """Worker._process_one with the real run_setups handler."""

    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.queue = QueuePaths(tmp.name)
        self.queue.ensure_exist()
        self.calls = []

    def run_task(self, entries, solved=True, **parameters):
        task = queue_common.build_task("run_setups", "projects/t1/p.aedt", "projects/t1", task_id="t1",
                                       parameters=parameters, field_exports=entries)
        os.makedirs(os.path.join(self.queue.root, "projects", "t1"))
        queue_common.write_task_atomically(self.queue, task)
        setup = FakeSetup(solved)
        hfss = FakeProject(FakeFieldsReporter(self.calls, rows=4), self.calls, {"Setup_F": setup})
        with mock.patch.object(Worker, "_open_project", return_value=hfss), \
                mock.patch.object(run_setups, "resolve_hpc_options", return_value={"cores": 2}):
            Worker(self.queue)._process_one(os.path.join(self.queue.pending, "t1.task.json"))
        return hfss, setup

    def result_json(self):
        with open(os.path.join(self.queue.root, "projects", "t1", "result.json")) as f:
            return json.load(f)

    def test_analyze_false_exports_without_solving_and_closes_without_saving(self):
        hfss, setup = self.run_task([entry("plane")], analyze=False)

        setup.analyze.assert_not_called()
        hfss.save_project.assert_not_called()
        self.assertEqual(hfss.closed_with, ("proj", False))
        self.assertTrue(os.path.exists(os.path.join(self.queue.done, "t1.task.json")))
        summary = self.result_json()
        self.assertEqual(summary["task_type"], "run_setups")
        self.assertEqual(summary["result"], {"Setup_F": {"success": True, "analyzed": False}})
        self.assertEqual(summary["field_exports"], {"plane": {
            "success": True, "file": "fields/plane.fld", "points": 4}})

    def test_an_unsolved_looking_setup_still_exports(self):
        with self.assertLogs("ansys_analyze_worker", level="WARNING"):
            self.run_task([entry("plane")], solved=False, analyze=False)
        self.assertTrue(os.path.exists(os.path.join(self.queue.done, "t1.task.json")))
        self.assertTrue(self.result_json()["field_exports"]["plane"]["success"])

    def test_the_default_still_solves_saves_and_closes_with_save(self):
        # The read-only rule must not leak into the normal path.
        for parameters in ({}, {"analyze": True}):
            with self.subTest(parameters=parameters):
                self.setUp()
                hfss, setup = self.run_task([entry("plane")], **parameters)
                setup.analyze.assert_called_once_with(cores=2)
                hfss.save_project.assert_called_once()
                self.assertEqual(hfss.closed_with, ("proj", True))
                self.assertEqual(self.result_json()["result"], {"Setup_F": {"success": True}})


if __name__ == "__main__":
    unittest.main()
