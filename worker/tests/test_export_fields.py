"""
Tests for the `export_fields` task type: open the project, run no analysis,
and export the task's `field_exports` from what is already solved (the
request on https://github.com/d-s-t/ansys-analyze-worker/pull/13 -- an
eigenmode project is always re-solved by `run_setups`, which took 17 to 54
minutes per test).

Same stand-ins as test_field_exports.py; no AEDT needed:

    python -m unittest tests.test_export_fields -v   # from worker/
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
from ansys_analyze_worker.handlers import HANDLERS, export_fields, get_handler, run_setups
from ansys_analyze_worker.worker import Worker
from tests.test_field_exports import DRIVEN_ENTRY, FakeFieldsReporter, FakeHfss

logging.getLogger("ansys_analyze_worker").addHandler(logging.NullHandler())


class FakeSetup:
    def __init__(self, solved):
        self._solved = solved
        self.analyze = mock.Mock(side_effect=AssertionError("export_fields must never analyze"))

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

    def get_setup(self, name):
        return self.setups_by_name.get(name)


def entry(name, solution="Setup_F : LastAdaptive"):
    result = {**DRIVEN_ENTRY, "name": name, "solution": solution}
    result.pop("sources")
    return result


class RegistryTests(unittest.TestCase):
    def test_export_fields_is_its_own_task_type_beside_run_setups(self):
        self.assertIs(get_handler("export_fields"), export_fields.run)
        self.assertIn("run_setups", HANDLERS)
        self.assertIsNot(HANDLERS["export_fields"], HANDLERS["run_setups"])

    def test_only_export_fields_is_read_only(self):
        self.assertIs(export_fields.run.read_only, True)
        self.assertFalse(getattr(run_setups.run, "read_only", False))


class HandlerTests(unittest.TestCase):
    def run_handler(self, setups, *entries, task=None):
        hfss = FakeProject(FakeFieldsReporter([], rows=3), [], setups)
        logged = []
        result = export_fields.run(hfss, task or {"field_exports": list(entries)}, log=logged.append)
        return result, logged

    def test_a_solved_setup_is_reported_and_nothing_is_analyzed(self):
        setup = FakeSetup(True)
        result, logged = self.run_handler({"Setup_F": setup}, entry("a"))
        self.assertEqual(result, {"analyzed": False, "setups": {"Setup_F": {"solved": True}}})
        setup.analyze.assert_not_called()
        self.assertIn("Setup_F", logged[0])

    def test_an_unsolved_looking_setup_is_logged_and_recorded_not_refused(self):
        with self.assertLogs("ansys_analyze_worker", level="WARNING") as logs:
            result, _ = self.run_handler({"Setup_F": FakeSetup(False)}, entry("a"))
        self.assertEqual(result["setups"]["Setup_F"], {"solved": False})
        self.assertIn("Setup_F reports no solution", logs.output[0])

    def test_a_setup_that_cannot_be_asked_is_unknown_not_unsolved(self):
        for setups in ({}, {"Setup_F": None}, {"Setup_F": FakeSetup(RuntimeError("report failed"))}):
            with self.subTest(setups=setups):
                result, _ = self.run_handler(setups, entry("a"))
                self.assertEqual(result["setups"]["Setup_F"], {"solved": None})

    def test_each_setup_is_checked_once_whatever_the_number_of_exports(self):
        result, _ = self.run_handler({"Setup_1": FakeSetup(True), "Setup_2": FakeSetup(True)},
                                     entry("a", "Setup_1 : LastAdaptive"), entry("b", "Setup_1 : Sweep"),
                                     entry("c", "Setup_2 : LastAdaptive"))
        self.assertEqual(sorted(result["setups"]), ["Setup_1", "Setup_2"])

    def test_a_task_without_exports_fails_with_a_clear_message(self):
        for task in ({}, {"field_exports": []}):
            with self.subTest(task=task):
                with self.assertRaisesRegex(RuntimeError, "needs a non-empty 'field_exports' list"):
                    export_fields.run(FakeProject(FakeFieldsReporter([], rows=1), [], {}), task, log=print)


class WorkerIntegrationTests(unittest.TestCase):
    """Worker._process_one with the real handler: no analysis, exports run, project closed without saving."""

    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.queue = QueuePaths(tmp.name)
        self.queue.ensure_exist()
        self.calls = []

    def run_task(self, task_type, entries, solved=True):
        task = queue_common.build_task(task_type, "projects/t1/p.aedt", "projects/t1", task_id="t1",
                                       field_exports=entries)
        os.makedirs(os.path.join(self.queue.root, "projects", "t1"))
        queue_common.write_task_atomically(self.queue, task)
        setup = FakeSetup(solved)
        hfss = FakeProject(FakeFieldsReporter(self.calls, rows=4), self.calls, {"Setup_F": setup})
        with mock.patch.object(Worker, "_open_project", return_value=hfss):
            Worker(self.queue)._process_one(os.path.join(self.queue.pending, "t1.task.json"))
        return hfss, setup

    def result_json(self):
        with open(os.path.join(self.queue.root, "projects", "t1", "result.json")) as f:
            return json.load(f)

    def test_exports_run_without_solving_and_the_project_is_not_saved(self):
        hfss, setup = self.run_task("export_fields", [entry("plane")])

        setup.analyze.assert_not_called()
        self.assertEqual(hfss.closed_with, ("proj", False))
        self.assertTrue(os.path.exists(os.path.join(self.queue.done, "t1.task.json")))
        summary = self.result_json()
        self.assertEqual(summary["task_type"], "export_fields")
        self.assertEqual(summary["result"], {"analyzed": False, "setups": {"Setup_F": {"solved": True}}})
        self.assertEqual(summary["field_exports"], {"plane": {
            "success": True, "file": "fields/plane.fld", "points": 4}})

    def test_a_task_without_exports_is_filed_into_failed(self):
        self.run_task("export_fields", [])
        self.assertTrue(os.path.exists(os.path.join(self.queue.failed, "t1.task.json")))
        self.assertEqual(self.result_json()["status"], "failed")
        self.assertIn("non-empty 'field_exports'", self.result_json()["error"])

    def test_an_unsolved_looking_setup_still_exports(self):
        with self.assertLogs("ansys_analyze_worker", level="WARNING"):
            self.run_task("export_fields", [entry("plane")], solved=False)
        self.assertTrue(os.path.exists(os.path.join(self.queue.done, "t1.task.json")))
        self.assertTrue(self.result_json()["field_exports"]["plane"]["success"])

    def test_run_setups_still_saves_the_project_on_close(self):
        # The read-only rule must not leak into the handler that does change the project.
        handler = mock.Mock(return_value={"Setup_F": {"success": True}})
        task = queue_common.build_task("run_setups", "projects/t1/p.aedt", "projects/t1", task_id="t1")
        os.makedirs(os.path.join(self.queue.root, "projects", "t1"))
        queue_common.write_task_atomically(self.queue, task)
        hfss = FakeProject(FakeFieldsReporter(self.calls), self.calls, {})
        with mock.patch.object(Worker, "_open_project", return_value=hfss), \
                mock.patch("ansys_analyze_worker.worker.get_handler", return_value=handler):
            Worker(self.queue)._process_one(os.path.join(self.queue.pending, "t1.task.json"))
        self.assertEqual(hfss.closed_with, ("proj", True))


if __name__ == "__main__":
    unittest.main()
