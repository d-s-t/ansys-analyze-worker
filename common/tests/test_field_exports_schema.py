"""
Tests for the optional `field_exports` task field in
ansys_analyze_common.queue_common (https://github.com/d-s-t/ansys-analyze-worker/issues/10):
`build_task` accepts it and `validate_task` checks its shape, so a client
gets an error before queuing instead of a failed export on the worker.

Pure stdlib, nothing else to install:

    python -m unittest tests.test_field_exports_schema -v   # from common/
"""
from __future__ import annotations

import copy
import unittest

from ansys_analyze_common import queue_common
from ansys_analyze_common.queue_common import TaskValidationError, build_task, validate_task

ENTRY = {
    "name": "substrate_mid_E",
    "solution": "Setup_F : LastAdaptive",
    "intrinsics": {"Freq": "12.29GHz", "Phase": "0deg"},
    "sources": {"port_1:1": ["1W", "0deg"], "port_2:1": ["0W", "0deg"]},
    "expression": ["E", "CmplxMag"],
    "grid": {"type": "Cartesian", "start": [-25, 1.19, -12.5], "stop": [25, 1.19, 12.5], "step": [0.25, 1, 0.25],
             "units": "mm"},
}


def task_with(*entries):
    return build_task("run_setups", "projects/t/p.aedt", "projects/t", task_id="t", field_exports=list(entries))


def entry_with(**changes):
    entry = copy.deepcopy(ENTRY)
    for key, value in changes.items():
        if value is ...:
            del entry[key]
        else:
            entry[key] = value
    return entry


def grid_with(**changes):
    grid = copy.deepcopy(ENTRY["grid"])
    for key, value in changes.items():
        if value is ...:
            del grid[key]
        else:
            grid[key] = value
    return grid


class BuildTaskTests(unittest.TestCase):
    def test_accepts_the_example_from_the_issue(self):
        task = task_with(ENTRY)
        self.assertEqual(task["field_exports"], [ENTRY])
        validate_task(task)

    def test_is_absent_when_not_given_so_existing_tasks_are_unchanged(self):
        for kwargs in ({}, {"field_exports": None}, {"field_exports": []}):
            task = build_task("run_setups", "projects/t/p.aedt", "projects/t", task_id="t", **kwargs)
            self.assertNotIn("field_exports", task)

    def test_rejects_a_bad_entry_before_anything_is_queued(self):
        with self.assertRaises(TaskValidationError):
            task_with(entry_with(expression=[]))

    def test_schema_version_is_unchanged(self):
        # An optional field is backward compatible; bumping it would make older workers refuse every task.
        self.assertEqual(queue_common.SCHEMA_VERSION, 1)


class ValidateTaskTests(unittest.TestCase):
    def base_task(self, field_exports):
        task = build_task("run_setups", "projects/t/p.aedt", "projects/t", task_id="t")
        task["field_exports"] = field_exports
        return task

    def assertInvalid(self, field_exports, fragment):
        with self.assertRaises(TaskValidationError) as ctx:
            validate_task(self.base_task(field_exports))
        self.assertIn(fragment, str(ctx.exception))

    def test_valid_minimal_entry(self):
        validate_task(self.base_task([entry_with(intrinsics=..., sources=...)]))

    def test_empty_list_is_valid(self):
        validate_task(self.base_task([]))

    def test_must_be_a_list(self):
        self.assertInvalid({"name": "x"}, "'field_exports' must be a list")
        self.assertInvalid("x", "'field_exports' must be a list")

    def test_entry_must_be_an_object(self):
        self.assertInvalid(["x"], "'field_exports'[0] must be an object")

    def test_error_names_the_entry(self):
        self.assertInvalid([ENTRY, entry_with(name="second", solution="")], "'field_exports'[1] ('second')")

    def test_required_keys(self):
        for key in ("name", "solution", "expression", "grid"):
            with self.subTest(key=key):
                self.assertInvalid([entry_with(**{key: ...})], f"missing required key(s): ['{key}']")

    def test_unknown_keys_are_rejected_rather_than_silently_ignored(self):
        self.assertInvalid([entry_with(exresssion=["E"])], "unknown key(s) ['exresssion']")

    def test_points_is_called_out_as_not_supported(self):
        self.assertInvalid([entry_with(points=[[0, 0, 0]])], "'points' is not supported yet")

    def test_name_is_safe_for_a_file_name(self):
        for bad in ("", "a/b", "..", "a b", "a.fld", "x\\y", 5, None):
            with self.subTest(name=bad):
                self.assertInvalid([entry_with(name=bad)], "'name' must be")
        for good in ("a", "substrate_mid_E", "E-1", "0"):
            with self.subTest(name=good):
                validate_task(self.base_task([entry_with(name=good)]))

    def test_names_must_be_unique(self):
        self.assertInvalid([ENTRY, entry_with()], "duplicate 'name'")

    def test_solution(self):
        for bad in ("", "  ", 3, None):
            with self.subTest(solution=bad):
                self.assertInvalid([entry_with(solution=bad)], "'solution' must be")

    def test_intrinsics(self):
        validate_task(self.base_task([entry_with(intrinsics={"Mode": 1})]))
        for bad in ([], "Freq", {"Freq": None}, {"Freq": True}, {"Freq": ["1GHz"]}, {1: "x"}):
            with self.subTest(intrinsics=bad):
                self.assertInvalid([entry_with(intrinsics=bad)], "'intrinsics' must be")

    def test_sources(self):
        validate_task(self.base_task([entry_with(sources={"port_1:1": "1W"})]))
        for bad in ([], {"p": ["1W"]}, {"p": ["1W", "0deg", "x"]}, {"p": [1, 2]}, {"p": 1}, {"p": None}):
            with self.subTest(sources=bad):
                self.assertInvalid([entry_with(sources=bad)], "'sources' must be")

    def test_expression(self):
        validate_task(self.base_task([entry_with(expression=["Mag_E"])]))
        for bad in ([], "E", ["E", ""], ["E", 1], None):
            with self.subTest(expression=bad):
                self.assertInvalid([entry_with(expression=bad)], "'expression' must be")

    def test_grid_must_be_an_object_with_known_keys(self):
        self.assertInvalid([entry_with(grid=[0, 0, 0])], "'grid' must be an object")
        self.assertInvalid([entry_with(grid=grid_with(points=[]))], "'grid' has unknown key(s) ['points']")

    def test_grid_type_defaults_to_cartesian_and_only_cartesian_is_supported(self):
        validate_task(self.base_task([entry_with(grid=grid_with(type=...))]))
        self.assertInvalid([entry_with(grid=grid_with(type="Spherical"))], "'grid.type' must be one of ['Cartesian']")

    def test_grid_vectors(self):
        for key in ("start", "stop", "step"):
            for bad in (..., [0, 0], [0, 0, 0, 0], [0, 0, "1"], [0, 0, None], [0, 0, True], [0, 0, float("nan")],
                        [0, 0, float("inf")], "0 0 0"):
                with self.subTest(key=key, value=bad):
                    self.assertInvalid([entry_with(grid=grid_with(**{key: bad}))], f"'grid.{key}' must be a list of three numbers")

    def test_every_step_must_be_positive_even_on_a_plane(self):
        # A plane is start == stop on one axis, but that axis still needs a non-zero step.
        plane = grid_with(start=[-25, 1.19, -12.5], stop=[25, 1.19, 12.5], step=[0.25, 1, 0.25])
        validate_task(self.base_task([entry_with(grid=plane)]))
        for bad in ([0, 1, 1], [1, 0, 1], [1, 1, -1]):
            with self.subTest(step=bad):
                self.assertInvalid([entry_with(grid=grid_with(step=bad))], "must be greater than zero")

    def test_grid_units(self):
        validate_task(self.base_task([entry_with(grid=grid_with(units=...))]))
        validate_task(self.base_task([entry_with(grid=grid_with(units="um"))]))
        for bad in ("", "mm2", "m m", 1):
            with self.subTest(units=bad):
                self.assertInvalid([entry_with(grid=grid_with(units=bad))], "'grid.units' must be")


if __name__ == "__main__":
    unittest.main()
