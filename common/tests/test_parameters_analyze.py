"""
`parameters.analyze` (run_setups: false skips the solve) must be a real
boolean. A string such as "false" is truthy, and would quietly re-solve an
Eigenmode project for hours, so queue_common rejects it before anything is
queued.

    python -m unittest tests.test_parameters_analyze -v   # from common/
"""
from __future__ import annotations

import unittest

from ansys_analyze_common.queue_common import TaskValidationError, build_task, validate_task


def task_with(parameters):
    return build_task("run_setups", "projects/t/p.aedt", "projects/t", task_id="t", parameters=parameters)


class AnalyzeParameterTests(unittest.TestCase):
    def test_true_and_false_are_accepted(self):
        for value in (True, False):
            with self.subTest(analyze=value):
                self.assertEqual(task_with({"analyze": value})["parameters"], {"analyze": value})

    def test_absent_or_other_parameters_are_unaffected(self):
        validate_task(task_with({}))
        validate_task(task_with({"something_else": "x"}))
        task = task_with({})
        del task["parameters"]
        validate_task(task)

    def test_anything_else_is_refused_before_queuing(self):
        for value in ("false", "true", "no", 0, 1, None, [], {}):
            with self.subTest(analyze=value):
                with self.assertRaisesRegex(TaskValidationError, r"'parameters.analyze' must be true or false"):
                    task_with({"analyze": value})


if __name__ == "__main__":
    unittest.main()
