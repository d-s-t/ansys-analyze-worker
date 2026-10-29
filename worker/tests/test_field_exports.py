"""
Tests for ansys_analyze_worker.field_exports -- the worker's one post-solve
step (https://github.com/d-s-t/ansys-analyze-worker/issues/10): evaluate
requested field expressions on a grid and write the raw numbers to
<output_dir>/fields/<name>.fld, because the Student version cannot load a
field solution whose mesh is over its size limit.

Same approach as test_run_setups.py: stand-ins that mirror the PyAEDT 1.0.0
/ AEDT calls closely enough to catch a mismatch at the call site, without
AEDT, PyAEDT or a license. The fake `ofieldsreporter.ExportOnGrid` has the
same ten positional parameters as the real one, so a call with the wrong
shape fails with a TypeError here instead of on a real solve.

    python -m unittest tests.test_field_exports -v   # from worker/
"""
from __future__ import annotations

import json
import logging
import os
import tempfile
import types
import unittest
from unittest import mock

from ansys_analyze_common import queue_common
from ansys_analyze_common.queue_common import QueuePaths
from ansys_analyze_worker import field_exports
from ansys_analyze_worker.worker import Worker

# The failure tests log warnings on purpose; without a handler Python's
# last-resort one would print every traceback to stderr during the run.
logging.getLogger("ansys_analyze_worker").addHandler(logging.NullHandler())

DRIVEN_ENTRY = {
    "name": "substrate_mid_E",
    "solution": "Setup_F : LastAdaptive",
    "intrinsics": {"Freq": "12.29GHz", "Phase": "0deg"},
    "sources": {"port_1:1": ["1W", "0deg"], "port_2:1": ["0W", "0deg"]},
    "expression": ["E", "CmplxMag"],
    "grid": {"type": "Cartesian", "start": [-25, 1.19, -12.5], "stop": [25, 1.19, 12.5], "step": [0.25, 1, 0.25],
             "units": "mm"},
}


class FakeFieldsReporter:
    """Records every call; ExportOnGrid writes a .fld-like file like AEDT does."""

    def __init__(self, calls, rows=5, fail_with=None, write=True, header_only=False, bad_quantities=(),
                 bad_ops=(), no_named_expressions=False):
        self.calls = calls
        self.rows = rows
        self.fail_with = fail_with
        self.write = write
        self.header_only = header_only
        self.bad_quantities = bad_quantities
        self.bad_ops = bad_ops
        self.no_named_expressions = no_named_expressions

    def CalcStack(self, action):
        self.calls.append(("CalcStack", action))

    def EnterQty(self, quantity):
        self.calls.append(("EnterQty", quantity))
        if quantity in self.bad_quantities:
            raise RuntimeError(f"{quantity} is not a quantity")

    def CopyNamedExprToStack(self, name):
        self.calls.append(("CopyNamedExprToStack", name))
        if self.no_named_expressions:
            raise RuntimeError("Failed to execute gRPC AEDT command: CopyNamedExprToStack")

    def CalcOp(self, operation):
        self.calls.append(("CalcOp", operation))
        if operation in self.bad_ops:
            raise RuntimeError("Failed to execute gRPC AEDT command: CalcOp")

    def ExportOnGrid(self, filename, start, stop, step, solution, variation, options, grid_type, center, flag):
        self.calls.append(("ExportOnGrid", filename, start, stop, step, solution, variation, options, grid_type,
                           center, flag))
        if self.fail_with:
            raise RuntimeError(self.fail_with)
        if self.write:
            with open(filename, "w") as f:
                f.write('"X" "Y" "Z" "Mag_E"\n')
                if not self.header_only:
                    for i in range(self.rows):
                        f.write(f"{i}.0 1.19 0.5 {i * 2}.5\n")


class FakeOdesktop:
    def __init__(self, messages):
        self.messages = messages

    def GetMessages(self, project, design, severity):
        assert severity == 2
        return list(self.messages)


class FakeHfss:
    project_name = "proj"
    design_name = "HFSSDesign1"

    def __init__(self, reporter, calls, edit_sources_result=True, messages=None, solutions=None,
                 design_intrinsics=None):
        self.ofieldsreporter = reporter
        self._calls = calls
        self._edit_sources_result = edit_sources_result
        self.odesktop = FakeOdesktop(messages if messages is not None else [])
        self.closed_with = None
        # PyAEDT's Hfss.existing_analysis_sweeps: "<setup> : <sweep>" for every solution in the design.
        self.existing_analysis_sweeps = ["Setup_F : LastAdaptive", "Setup_F : Sweep"] if solutions is None else solutions
        # PyAEDT's Hfss.design_solutions.intrinsics: ["Freq", "Phase"] driven, ["Phase"] eigenmode.
        self.design_solutions = types.SimpleNamespace(
            intrinsics=["Freq", "Phase"] if design_intrinsics is None else design_intrinsics)

    def edit_sources(self, assignment, **kwargs):
        self._calls.append(("edit_sources", assignment))
        return self._edit_sources_result

    def close_project(self, name=None, save=True):
        self.closed_with = (name, save)
        return True


class FieldExportsTestCase(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.output_dir = tmp.name
        self.calls = []
        self.logged = []

    def make_hfss(self, **reporter_kwargs):
        hfss_kwargs = {k: reporter_kwargs.pop(k)
                       for k in ("edit_sources_result", "messages", "solutions", "design_intrinsics")
                       if k in reporter_kwargs}
        return FakeHfss(FakeFieldsReporter(self.calls, **reporter_kwargs), self.calls, **hfss_kwargs)

    def task(self, *entries):
        return {"output_dir": self.output_dir, "field_exports": list(entries)}

    def run_exports(self, hfss, *entries):
        return field_exports.run(hfss, self.task(*entries), log=self.logged.append)

    def export_calls(self):
        return [c for c in self.calls if c[0] == "ExportOnGrid"]


class RunTests(FieldExportsTestCase):
    def test_no_field_exports_does_nothing(self):
        hfss = self.make_hfss()
        for task in ({"output_dir": self.output_dir}, {"output_dir": self.output_dir, "field_exports": []}):
            self.assertEqual(field_exports.run(hfss, task, log=self.logged.append), {})
        self.assertEqual(self.calls, [])
        self.assertFalse(os.path.exists(os.path.join(self.output_dir, "fields")))

    def test_driven_export_makes_the_native_calls_from_the_issue(self):
        result = self.run_exports(self.make_hfss(rows=7), DRIVEN_ENTRY)

        out_file = os.path.join(self.output_dir, "fields", "substrate_mid_E.fld")
        self.assertEqual(result, {"substrate_mid_E": {"success": True, "file": "fields/substrate_mid_E.fld",
                                                      "points": 7}})
        self.assertEqual(self.calls, [
            ("edit_sources", {"port_1:1": ["1W", "0deg"], "port_2:1": ["0W", "0deg"]}),
            ("CalcStack", "clear"),
            ("EnterQty", "E"),
            ("CalcOp", "CmplxMag"),
            ("ExportOnGrid", out_file,
             ["-25mm", "1.19mm", "-12.5mm"], ["25mm", "1.19mm", "12.5mm"], ["0.25mm", "1mm", "0.25mm"],
             "Setup_F : LastAdaptive", ["Freq:=", "12.29GHz", "Phase:=", "0deg"],
             ["NAME:ExportOption", "IncludePtInOutput:=", True, "RefCSName:=", "Global", "PtInSI:=", True,
              "FieldInRefCS:=", True],
             "Cartesian", ["0mm", "0mm", "0mm"], False),
        ])

    def test_intrinsics_are_passed_to_aedt_exactly_as_given(self):
        entry = {**DRIVEN_ENTRY, "name": "mode1", "intrinsics": {"Mode": "1", "Phase": "0deg"}}
        del entry["sources"]
        result = self.run_exports(self.make_hfss(), entry)
        self.assertTrue(result["mode1"]["success"])
        self.assertEqual(self.export_calls()[0][6], ["Mode:=", "1", "Phase:=", "0deg"])
        self.assertNotIn("edit_sources", [c[0] for c in self.calls])

    def test_numeric_intrinsics_are_sent_as_strings(self):
        entry = {**DRIVEN_ENTRY, "intrinsics": {"Mode": 2}}
        self.run_exports(self.make_hfss(), entry)
        self.assertEqual(self.export_calls()[0][6], ["Mode:=", "2"])

    def test_missing_intrinsics_and_units_use_defaults(self):
        entry = {k: v for k, v in DRIVEN_ENTRY.items() if k not in ("intrinsics", "sources")}
        entry["grid"] = {"start": [0, 0, 0], "stop": [1, 1, 1], "step": [1, 1, 1]}
        self.run_exports(self.make_hfss(), entry)
        call = self.export_calls()[0]
        self.assertEqual(call[6], [])
        self.assertEqual(call[2], ["0mm", "0mm", "0mm"])
        self.assertEqual(call[8], "Cartesian")

    def test_other_length_units_are_used_for_the_grid_and_the_origin(self):
        entry = {**DRIVEN_ENTRY, "grid": {**DRIVEN_ENTRY["grid"], "units": "um"}}
        self.run_exports(self.make_hfss(), entry)
        call = self.export_calls()[0]
        self.assertEqual(call[2][0], "-25um")
        self.assertEqual(call[9], ["0um", "0um", "0um"])

    def test_several_ops_are_applied_in_order(self):
        entry = {**DRIVEN_ENTRY, "expression": ["H", "Smooth", "CmplxMag"]}
        self.run_exports(self.make_hfss(), entry)
        ops = [c for c in self.calls if c[0] in ("EnterQty", "CalcOp")]
        self.assertEqual(ops, [("EnterQty", "H"), ("CalcOp", "Smooth"), ("CalcOp", "CmplxMag")])

    def test_a_named_calculator_expression_falls_back_to_copying_it_onto_the_stack(self):
        entry = {**DRIVEN_ENTRY, "expression": ["Mag_E"]}
        result = self.run_exports(self.make_hfss(bad_quantities=("Mag_E",)), entry)
        self.assertTrue(result["substrate_mid_E"]["success"])
        self.assertEqual([c for c in self.calls if c[0] in ("EnterQty", "CopyNamedExprToStack", "CalcOp")],
                         [("EnterQty", "Mag_E"), ("CopyNamedExprToStack", "Mag_E")])

    def test_logs_each_export(self):
        self.run_exports(self.make_hfss(), DRIVEN_ENTRY)
        self.assertEqual(len(self.logged), 1)
        self.assertIn("substrate_mid_E", self.logged[0])


class FailureTests(FieldExportsTestCase):
    def test_a_failed_export_is_recorded_and_the_others_still_run(self):
        failures_left = [1]

        class FlakyReporter(FakeFieldsReporter):
            def ExportOnGrid(inner, *args):
                if failures_left:
                    failures_left.pop()
                    self.calls.append(("ExportOnGrid", args[0]))
                    raise RuntimeError("Mesh size (292978 volume elements) is greater than the limit")
                return super().ExportOnGrid(*args)

        hfss = FakeHfss(FlakyReporter(self.calls), self.calls)
        second = {**DRIVEN_ENTRY, "name": "second"}
        del second["sources"]
        with self.assertLogs("ansys_analyze_worker", level="WARNING"):
            result = self.run_exports(hfss, DRIVEN_ENTRY, second)

        self.assertEqual(result["substrate_mid_E"]["success"], False)
        self.assertIn("Mesh size (292978 volume elements)", result["substrate_mid_E"]["error"])
        self.assertTrue(result["second"]["success"])

    def test_new_aedt_error_messages_are_attached_to_the_failure(self):
        hfss = self.make_hfss(fail_with="Error in performing operation.", messages=["[error] old one"])

        def fail_with_new_message(*args):
            hfss.odesktop.messages.append("[error]  Setup_F: Mesh size is greater than the limit for Student.")
            raise RuntimeError("Script macro error")

        hfss.ofieldsreporter.ExportOnGrid = fail_with_new_message
        with self.assertLogs("ansys_analyze_worker", level="WARNING"):
            result = self.run_exports(hfss, DRIVEN_ENTRY)
        self.assertEqual(result["substrate_mid_E"]["aedt_messages"],
                         ["[error]  Setup_F: Mesh size is greater than the limit for Student."])

    def test_unreadable_aedt_messages_do_not_hide_the_real_error(self):
        hfss = self.make_hfss(fail_with="bad expression")
        hfss.odesktop = None
        with self.assertLogs("ansys_analyze_worker", level="WARNING"):
            result = self.run_exports(hfss, DRIVEN_ENTRY)
        self.assertFalse(result["substrate_mid_E"]["success"])
        self.assertIn("bad expression", result["substrate_mid_E"]["error"])
        self.assertNotIn("aedt_messages", result["substrate_mid_E"])

    def test_aedt_not_raising_but_writing_nothing_is_a_failure(self):
        result = self.run_exports(self.make_hfss(write=False), DRIVEN_ENTRY)
        self.assertFalse(result["substrate_mid_E"]["success"])
        self.assertIn("wrote no file", result["substrate_mid_E"]["error"])

    def test_a_file_with_no_data_rows_is_a_failure(self):
        result = self.run_exports(self.make_hfss(header_only=True), DRIVEN_ENTRY)
        self.assertFalse(result["substrate_mid_E"]["success"])
        self.assertIn("no data rows", result["substrate_mid_E"]["error"])

    def test_a_previous_runs_file_is_not_mistaken_for_this_runs_output(self):
        fields_dir = os.path.join(self.output_dir, "fields")
        os.makedirs(fields_dir)
        with open(os.path.join(fields_dir, "substrate_mid_E.fld"), "w") as f:
            f.write("1.0 2.0 3.0 4.0\n")
        result = self.run_exports(self.make_hfss(write=False), DRIVEN_ENTRY)
        self.assertFalse(result["substrate_mid_E"]["success"])

    def test_edit_sources_failing_skips_that_export(self):
        result = self.run_exports(self.make_hfss(edit_sources_result=False), DRIVEN_ENTRY)
        self.assertFalse(result["substrate_mid_E"]["success"])
        self.assertIn("edit_sources failed", result["substrate_mid_E"]["error"])
        self.assertEqual(self.export_calls(), [])

    def test_an_unknown_solution_error_lists_the_available_ones(self):
        entry = {**DRIVEN_ENTRY, "solution": "NoSuchSetup : LastAdaptive"}
        with self.assertLogs("ansys_analyze_worker", level="WARNING"):
            result = self.run_exports(
                self.make_hfss(fail_with="Failed to execute gRPC AEDT command: ExportOnGrid"), entry)
        error = result["substrate_mid_E"]["error"]
        self.assertIn("Failed to execute gRPC AEDT command: ExportOnGrid", error)  # AEDT's own text is kept
        self.assertIn("'NoSuchSetup : LastAdaptive' was not found among this design's solutions", error)
        self.assertIn("'Setup_F : LastAdaptive', 'Setup_F : Sweep'", error)

    def test_no_solution_hint_when_the_solution_exists(self):
        with self.assertLogs("ansys_analyze_worker", level="WARNING"):
            result = self.run_exports(self.make_hfss(fail_with="some other reason"), DRIVEN_ENTRY)
        self.assertNotIn("available", result["substrate_mid_E"]["error"])

    def test_no_solution_hint_when_the_solutions_cannot_be_listed(self):
        entry = {**DRIVEN_ENTRY, "solution": "NoSuchSetup : LastAdaptive"}
        for broken in (None, []):
            with self.subTest(solutions=broken):
                hfss = self.make_hfss(fail_with="boom", solutions=[] if broken == [] else None)
                if broken is None:
                    del hfss.existing_analysis_sweeps  # an Hfss without the property
                with self.assertLogs("ansys_analyze_worker", level="WARNING"):
                    result = self.run_exports(hfss, entry)
                self.assertEqual(result["substrate_mid_E"]["error"], "RuntimeError: boom")

    def test_a_solution_missing_from_the_list_is_still_exported_not_refused_up_front(self):
        # The hint is for failures only: a valid name PyAEDT's list omits must not be blocked.
        entry = {**DRIVEN_ENTRY, "solution": "Setup_F : SomethingTheListOmits"}
        result = self.run_exports(self.make_hfss(), entry)
        self.assertTrue(result["substrate_mid_E"]["success"])
        self.assertEqual(self.export_calls()[0][5], "Setup_F : SomethingTheListOmits")

    def test_a_failing_calculator_operation_names_the_operation(self):
        entry = {**DRIVEN_ENTRY, "expression": ["E", "NotACalcOp"]}
        with self.assertLogs("ansys_analyze_worker", level="WARNING"):
            result = self.run_exports(self.make_hfss(bad_ops=("NotACalcOp",)), entry)
        error = result["substrate_mid_E"]["error"]
        self.assertIn("calculator operation 'NotACalcOp' failed", error)
        self.assertIn("Failed to execute gRPC AEDT command: CalcOp", error)
        self.assertEqual(self.export_calls(), [])

    def test_a_quantity_that_is_neither_basic_nor_a_named_expression_says_so(self):
        entry = {**DRIVEN_ENTRY, "expression": ["Nope"]}
        hfss = self.make_hfss(bad_quantities=("Nope",), no_named_expressions=True)
        with self.assertLogs("ansys_analyze_worker", level="WARNING"):
            result = self.run_exports(hfss, entry)
        self.assertIn("'Nope' is neither a field quantity nor a named calculator expression",
                      result["substrate_mid_E"]["error"])

    def test_a_failure_records_exactly_what_was_sent_to_exportongrid(self):
        hfss = self.make_hfss(fail_with="Failed to execute gRPC AEDT command: ExportOnGrid")
        with self.assertLogs("ansys_analyze_worker", level="WARNING"):
            result = self.run_exports(hfss, DRIVEN_ENTRY)
        sent = self.export_calls()[0][2:]  # the fake records ("ExportOnGrid", file, *the other nine)
        recorded = result["substrate_mid_E"]["export_on_grid_args"]
        self.assertEqual(recorded[0], "fields/substrate_mid_E.fld")
        self.assertEqual(recorded[1:], list(sent))
        json.dumps(result)  # result.json must be able to hold it

    def test_a_failure_records_what_the_design_declares(self):
        hfss = self.make_hfss(fail_with="boom")
        with self.assertLogs("ansys_analyze_worker", level="WARNING"):
            result = self.run_exports(hfss, DRIVEN_ENTRY)
        self.assertEqual(result["substrate_mid_E"]["design"], {
            "solutions": ["Setup_F : LastAdaptive", "Setup_F : Sweep"], "intrinsics": ["Freq", "Phase"]})

    def test_the_failure_is_logged_with_the_arguments_and_the_design(self):
        hfss = self.make_hfss(fail_with="boom")
        with self.assertLogs("ansys_analyze_worker", level="WARNING") as logs:
            self.run_exports(hfss, DRIVEN_ENTRY)
        self.assertTrue(any("ExportOnGrid arguments" in line and "Setup_F : Sweep" in line for line in logs.output))

    def test_design_is_left_out_when_it_cannot_be_read(self):
        hfss = self.make_hfss(fail_with="boom")
        del hfss.existing_analysis_sweeps
        del hfss.design_solutions
        with self.assertLogs("ansys_analyze_worker", level="WARNING"):
            result = self.run_exports(hfss, DRIVEN_ENTRY)
        self.assertNotIn("design", result["substrate_mid_E"])
        self.assertIn("export_on_grid_args", result["substrate_mid_E"])

    def test_an_intrinsic_the_design_does_not_declare_is_named_in_the_error(self):
        # The eigenmode case seen on real AEDT: PyAEDT lists only Phase, and Mode was sent.
        entry = {**DRIVEN_ENTRY, "intrinsics": {"Mode": "1", "Phase": "0deg"}}
        entry.pop("sources")
        hfss = self.make_hfss(fail_with="Failed to execute gRPC AEDT command: ExportOnGrid",
                              design_intrinsics=["Phase"])
        with self.assertLogs("ansys_analyze_worker", level="WARNING"):
            result = self.run_exports(hfss, entry)
        error = result["substrate_mid_E"]["error"]
        self.assertIn("intrinsic(s) ['Mode'] are not among the intrinsics PyAEDT lists for this design, ['Phase']",
                      error)
        self.assertEqual(result["substrate_mid_E"]["design"]["intrinsics"], ["Phase"])

    def test_no_intrinsic_hint_when_they_are_all_declared(self):
        with self.assertLogs("ansys_analyze_worker", level="WARNING"):
            result = self.run_exports(self.make_hfss(fail_with="boom"), DRIVEN_ENTRY)
        self.assertNotIn("intrinsic(s)", result["substrate_mid_E"]["error"])

    def test_an_undeclared_intrinsic_is_still_tried_not_refused_up_front(self):
        # How AEDT picks an eigenmode for a field export is not established, so the
        # declared list is only ever used to explain a failure.
        entry = {**DRIVEN_ENTRY, "intrinsics": {"Mode": "1", "Phase": "0deg"}}
        entry.pop("sources")
        result = self.run_exports(self.make_hfss(design_intrinsics=["Phase"]), entry)
        self.assertTrue(result["substrate_mid_E"]["success"])
        self.assertEqual(self.export_calls()[0][6], ["Mode:=", "1", "Phase:=", "0deg"])

    def test_the_output_folder_not_being_creatable_fails_the_export_not_the_task(self):
        blocker = os.path.join(self.output_dir, "fields")
        with open(blocker, "w") as f:  # a file where the fields/ folder should go
            f.write("x")
        result = self.run_exports(self.make_hfss(), DRIVEN_ENTRY)
        self.assertFalse(result["substrate_mid_E"]["success"])


class HelperTests(unittest.TestCase):
    def test_edits_sources(self):
        self.assertTrue(field_exports.edits_sources({"field_exports": [DRIVEN_ENTRY]}))
        no_sources = {k: v for k, v in DRIVEN_ENTRY.items() if k != "sources"}
        self.assertFalse(field_exports.edits_sources({"field_exports": [no_sources]}))
        self.assertFalse(field_exports.edits_sources({"field_exports": [{**no_sources, "sources": {}}]}))
        self.assertFalse(field_exports.edits_sources({}))

    def test_count_points_counts_only_numeric_rows(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "a.fld")
            with open(path, "w") as f:
                f.write('"X" "Y" "Z" "Mag_E"\n\n1.0 2.0 3.0 4.0\n-1e-3 0 0 5\n  \n')
            self.assertEqual(field_exports._count_points(path), 2)


class WorkerIntegrationTests(unittest.TestCase):
    """Worker._process_one with a fake project: result.json, filing, and how the project is closed."""

    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.queue = QueuePaths(tmp.name)
        self.queue.ensure_exist()
        self.calls = []

    def run_task(self, field_exports_entries):
        task = queue_common.build_task(
            "run_setups", "projects/t1/p.aedt", "projects/t1", task_id="t1",
            field_exports=field_exports_entries, metadata={"run": 3},
        )
        os.makedirs(os.path.join(self.queue.root, "projects", "t1"))
        queue_common.write_task_atomically(self.queue, task)
        hfss = FakeHfss(FakeFieldsReporter(self.calls, rows=4), self.calls)
        handler = mock.Mock(return_value={"Setup_F": {"success": True}})
        worker = Worker(self.queue)
        with mock.patch.object(Worker, "_open_project", return_value=hfss), \
                mock.patch("ansys_analyze_worker.worker.get_handler", return_value=handler):
            worker._process_one(os.path.join(self.queue.pending, "t1.task.json"))
        return hfss, handler

    def result_json(self):
        with open(os.path.join(self.queue.root, "projects", "t1", "result.json")) as f:
            return json.load(f)

    def test_field_exports_land_beside_result_and_the_task_is_done(self):
        hfss, handler = self.run_task([DRIVEN_ENTRY])

        self.assertTrue(os.path.exists(os.path.join(self.queue.done, "t1.task.json")))
        summary = self.result_json()
        self.assertEqual(summary["result"], {"Setup_F": {"success": True}})
        self.assertEqual(summary["field_exports"], {"substrate_mid_E": {
            "success": True, "file": "fields/substrate_mid_E.fld", "points": 4}})
        self.assertTrue(os.path.exists(os.path.join(self.queue.root, "projects", "t1", "fields",
                                                    "substrate_mid_E.fld")))
        handler.assert_called_once()

    def test_a_failed_export_still_files_the_task_into_done(self):
        entry = {**DRIVEN_ENTRY, "name": "bad"}
        del entry["sources"]
        with mock.patch.object(FakeFieldsReporter, "ExportOnGrid", side_effect=RuntimeError("unknown solution")):
            with self.assertLogs("ansys_analyze_worker", level="WARNING"):
                self.run_task([entry])
        self.assertTrue(os.path.exists(os.path.join(self.queue.done, "t1.task.json")))
        self.assertFalse(os.path.exists(os.path.join(self.queue.failed, "t1.task.json")))
        summary = self.result_json()
        self.assertEqual(summary["status"], "success")
        self.assertFalse(summary["field_exports"]["bad"]["success"])

    def test_no_field_exports_leaves_result_json_unchanged(self):
        self.run_task([])
        self.assertEqual(set(self.result_json()), {"status", "task_id", "task_type", "metadata", "result"})

    def test_project_is_saved_on_close_unless_sources_were_edited(self):
        hfss, _ = self.run_task([{k: v for k, v in DRIVEN_ENTRY.items() if k != "sources"}])
        self.assertEqual(hfss.closed_with, ("proj", True))

    def test_project_is_not_saved_on_close_when_sources_were_edited(self):
        hfss, _ = self.run_task([DRIVEN_ENTRY])
        self.assertEqual(hfss.closed_with, ("proj", False))


if __name__ == "__main__":
    unittest.main()
