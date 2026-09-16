"""
Tests for ansys_analyze_worker.hpc -- the env-var-driven cores/tasks/gpus
resolution that decides what `setup.analyze()` gets called with (see
hpc.py's module docstring for why this matters: PyAEDT defaults to
solving on a single core unless the worker passes these explicitly).

Pure stdlib `unittest`, no AEDT/PyAEDT dependency -- `hpc.py` itself
doesn't import anything from `ansys.aedt.core`, so this can run anywhere
without a license or an open AEDT session:

    python -m unittest worker.tests.test_hpc -v

(from the repo root, with `worker/` on the path -- e.g. after
`pip install -e worker/`, or simply `cd worker && python -m unittest
tests.test_hpc -v`).
"""
from __future__ import annotations

import os
import unittest
from unittest import mock

from ansys_analyze_worker import hpc

ENV_VARS = (hpc.CORES_ENV_VAR, hpc.TASKS_ENV_VAR, hpc.GPUS_ENV_VAR)


class HpcOptionEnvTestCase(unittest.TestCase):
    """Base class that guarantees a clean slate for the three env vars."""

    def setUp(self):
        self._patcher = mock.patch.dict(os.environ, {}, clear=False)
        self._patcher.start()
        for var in ENV_VARS:
            os.environ.pop(var, None)
        self.addCleanup(self._patcher.stop)


class MeansUseAedtSettingsTests(unittest.TestCase):
    def test_canonical_spellings(self):
        for spelling in ("aedt", "AEDT", "auto", "AUTO", "default", "DEFAULT", "0"):
            with self.subTest(spelling=spelling):
                self.assertTrue(hpc._means_use_aedt_settings(spelling))

    def test_other_zero_spellings(self):
        for spelling in ("00", "+0", "-0", "000"):
            with self.subTest(spelling=spelling):
                self.assertTrue(hpc._means_use_aedt_settings(spelling))

    def test_nonzero_numbers_are_not_the_sentinel(self):
        for spelling in ("1", "16", "-4"):
            with self.subTest(spelling=spelling):
                self.assertFalse(hpc._means_use_aedt_settings(spelling))

    def test_garbage_is_not_the_sentinel(self):
        self.assertFalse(hpc._means_use_aedt_settings("banana"))


class ResolveEnvOptionTests(HpcOptionEnvTestCase):
    def test_unset_returns_default(self):
        self.assertEqual(hpc._resolve_env_option(hpc.CORES_ENV_VAR, default=7), 7)

    def test_explicit_raw_overrides_the_environment(self):
        # A caller that already read+stripped the env var itself (as
        # resolve_hpc_options() does, to describe *why* cores ended up
        # the way it did) can hand that same string in via `raw=`
        # instead of this function reading the variable again
        # independently -- and `raw` wins even if the environment says
        # something else, so the two can never disagree about which
        # value is authoritative.
        os.environ[hpc.CORES_ENV_VAR] = "16"
        self.assertEqual(hpc._resolve_env_option(hpc.CORES_ENV_VAR, default=1, raw="8"), 8)

    def test_raw_none_falls_back_to_reading_the_environment(self):
        os.environ[hpc.CORES_ENV_VAR] = "16"
        self.assertEqual(hpc._resolve_env_option(hpc.CORES_ENV_VAR, default=1, raw=None), 16)

    def test_empty_string_returns_default(self):
        os.environ[hpc.CORES_ENV_VAR] = "   "
        self.assertEqual(hpc._resolve_env_option(hpc.CORES_ENV_VAR, default=7), 7)

    def test_numeric_value_is_used(self):
        os.environ[hpc.CORES_ENV_VAR] = "16"
        self.assertEqual(hpc._resolve_env_option(hpc.CORES_ENV_VAR, default=1), 16)

    def test_numeric_value_is_stripped_of_whitespace(self):
        os.environ[hpc.CORES_ENV_VAR] = "  12  "
        self.assertEqual(hpc._resolve_env_option(hpc.CORES_ENV_VAR, default=1), 12)

    def test_zero_means_use_aedt_settings(self):
        os.environ[hpc.CORES_ENV_VAR] = "0"
        self.assertIsNone(hpc._resolve_env_option(hpc.CORES_ENV_VAR, default=99))

    def test_aedt_sentinel_is_case_insensitive(self):
        for spelling in ("aedt", "AEDT", "AeDt", "auto", "AUTO", "default", "DEFAULT"):
            with self.subTest(spelling=spelling):
                os.environ[hpc.CORES_ENV_VAR] = spelling
                self.assertIsNone(hpc._resolve_env_option(hpc.CORES_ENV_VAR, default=99))

    def test_non_integer_value_warns_and_falls_back(self):
        os.environ[hpc.CORES_ENV_VAR] = "lots"
        with self.assertLogs(hpc.logger, level="WARNING"):
            result = hpc._resolve_env_option(hpc.CORES_ENV_VAR, default=4)
        self.assertEqual(result, 4)

    def test_negative_value_warns_and_falls_back(self):
        os.environ[hpc.CORES_ENV_VAR] = "-4"
        with self.assertLogs(hpc.logger, level="WARNING"):
            result = hpc._resolve_env_option(hpc.CORES_ENV_VAR, default=4)
        self.assertEqual(result, 4)

    def test_default_of_none_is_returned_untouched(self):
        self.assertIsNone(hpc._resolve_env_option(hpc.TASKS_ENV_VAR))


class ResolveHpcOptionsTests(HpcOptionEnvTestCase):
    def test_default_uses_cpu_count_for_cores_and_none_for_the_rest(self):
        with mock.patch.object(hpc.os, "cpu_count", return_value=8):
            options = hpc.resolve_hpc_options()
        self.assertEqual(options, {"cores": 8, "tasks": None, "gpus": None})

    def test_explicit_values_pass_through(self):
        os.environ[hpc.CORES_ENV_VAR] = "12"
        os.environ[hpc.TASKS_ENV_VAR] = "2"
        os.environ[hpc.GPUS_ENV_VAR] = "1"
        options = hpc.resolve_hpc_options()
        self.assertEqual(options, {"cores": 12, "tasks": 2, "gpus": 1})

    def test_cores_aedt_alone_passes_none_for_everything(self):
        os.environ[hpc.CORES_ENV_VAR] = "aedt"
        options = hpc.resolve_hpc_options()
        self.assertEqual(options, {"cores": None, "tasks": None, "gpus": None})

    def test_cores_aedt_combined_with_gpus_falls_back_cores_with_a_warning(self):
        # Regression test: PyAEDT has no way to leave `cores` alone while
        # overriding `gpus`/`tasks` -- either one forces a full HPC
        # config rebuild from PyAEDT's own template, which would
        # silently reset cores to that template's NumCores=4 instead of
        # the dialog's real value if left as None. See the module
        # docstring's "unsupported combination" note.
        os.environ[hpc.CORES_ENV_VAR] = "aedt"
        os.environ[hpc.GPUS_ENV_VAR] = "2"
        with mock.patch.object(hpc.os, "cpu_count", return_value=8):
            with self.assertLogs(hpc.logger, level="WARNING") as log:
                options = hpc.resolve_hpc_options()
        self.assertEqual(options, {"cores": 8, "tasks": None, "gpus": 2})
        self.assertTrue(any("cores" in message for message in log.output))

    def test_cores_aedt_combined_with_tasks_falls_back_cores_with_a_warning(self):
        os.environ[hpc.CORES_ENV_VAR] = "aedt"
        os.environ[hpc.TASKS_ENV_VAR] = "4"
        with mock.patch.object(hpc.os, "cpu_count", return_value=8):
            with self.assertLogs(hpc.logger, level="WARNING"):
                options = hpc.resolve_hpc_options()
        self.assertEqual(options, {"cores": 8, "tasks": 4, "gpus": None})

    def test_undetectable_cpu_count_combined_with_gpus_warns_but_cannot_fix_cores(self):
        # Regression test: the coupling-bug fallback used to recompute
        # os.cpu_count() as ITS "fallback" value too, so when cpu_count()
        # is undetectable (returns None), the "fix" silently produced
        # None again -- the exact bug the fallback exists to prevent,
        # just for this one edge case. There is nothing left this module
        # can do without a concrete core count, so cores staying None
        # here is correct as long as the warning says so honestly
        # instead of claiming a fix that didn't happen (see the next
        # test for that).
        os.environ[hpc.GPUS_ENV_VAR] = "2"
        with mock.patch.object(hpc.os, "cpu_count", return_value=None):
            with self.assertLogs(hpc.logger, level="WARNING"):
                options = hpc.resolve_hpc_options()
        self.assertEqual(options, {"cores": None, "tasks": None, "gpus": 2})

    def test_warning_does_not_misattribute_an_unset_cores_variable(self):
        # Regression test: the warning used to hardcode
        # "ANSYS_ANALYZE_CORES=aedt was requested" even when that
        # variable was never set at all (cores was None only because
        # os.cpu_count() itself returned None) -- misleading anyone
        # reading the log about what actually happened.
        os.environ[hpc.GPUS_ENV_VAR] = "2"
        with mock.patch.object(hpc.os, "cpu_count", return_value=None):
            with self.assertLogs(hpc.logger, level="WARNING") as log:
                hpc.resolve_hpc_options()
        message = " ".join(log.output)
        self.assertNotIn("=aedt", message)
        self.assertIn("unset", message)

    def test_warning_correctly_attributes_an_explicit_aedt_request(self):
        os.environ[hpc.CORES_ENV_VAR] = "aedt"
        os.environ[hpc.GPUS_ENV_VAR] = "2"
        with mock.patch.object(hpc.os, "cpu_count", return_value=8):
            with self.assertLogs(hpc.logger, level="WARNING") as log:
                hpc.resolve_hpc_options()
        message = " ".join(log.output)
        self.assertIn("asked to leave it alone", message)

    def test_warning_distinguishes_invalid_cores_value_from_unset(self):
        # Regression test: a garbage ANSYS_ANALYZE_CORES value combined
        # with an undetectable os.cpu_count() used to be reported as
        # "unset", even though the variable was set (just to something
        # unparseable) -- sending anyone debugging it looking for a
        # missing variable instead of a typo'd one.
        os.environ[hpc.CORES_ENV_VAR] = "banana"
        os.environ[hpc.GPUS_ENV_VAR] = "2"
        with mock.patch.object(hpc.os, "cpu_count", return_value=None):
            with self.assertLogs(hpc.logger, level="WARNING") as log:
                options = hpc.resolve_hpc_options()
        message = " ".join(log.output)
        self.assertIn("was invalid", message)
        self.assertNotIn("is unset", message)
        self.assertIsNone(options["cores"])

    def test_non_canonical_zero_spelling_is_correctly_attributed(self):
        # Regression test: "00"/"+0"/"-0" all parse to zero (meaning
        # "use AEDT's own settings", same as the canonical "0"), but
        # aren't literally in USE_AEDT_SETTINGS_VALUES -- the warning
        # used to call these "invalid" even though they parsed fine and
        # were honored correctly; it just described why incorrectly.
        for spelling in ("00", "+0", "-0"):
            with self.subTest(spelling=spelling):
                os.environ[hpc.CORES_ENV_VAR] = spelling
                os.environ[hpc.GPUS_ENV_VAR] = "2"
                with mock.patch.object(hpc.os, "cpu_count", return_value=16):
                    with self.assertLogs(hpc.logger, level="WARNING") as log:
                        options = hpc.resolve_hpc_options()
                message = " ".join(log.output)
                self.assertIn("asked to leave it alone", message)
                self.assertNotIn("was invalid", message)
                self.assertEqual(options, {"cores": 16, "tasks": None, "gpus": 2})

    def test_undetectable_cpu_count_with_no_override_still_warns(self):
        # Regression test: this combination used to log nothing at all,
        # silently diverging from the documented "every logical
        # processor" default with no trace in the log.
        with mock.patch.object(hpc.os, "cpu_count", return_value=None):
            with self.assertLogs(hpc.logger, level="WARNING") as log:
                options = hpc.resolve_hpc_options()
        self.assertIn(hpc.CORES_ENV_VAR, " ".join(log.output))
        self.assertEqual(options, {"cores": None, "tasks": None, "gpus": None})

    def test_undetectable_cpu_count_with_invalid_cores_is_correctly_attributed(self):
        # Regression test: this "no tasks/gpus override" warning branch
        # used to hardcode "isn't set to a specific number" even when
        # ANSYS_ANALYZE_CORES WAS set, just to something invalid --
        # exactly the misattribution bug already fixed in the sibling
        # (tasks/gpus-set) branch, left standing here.
        os.environ[hpc.CORES_ENV_VAR] = "banana"
        with mock.patch.object(hpc.os, "cpu_count", return_value=None):
            with self.assertLogs(hpc.logger, level="WARNING") as log:
                options = hpc.resolve_hpc_options()
        message = " ".join(log.output)
        self.assertIn("was invalid", message)
        self.assertNotIn("isn't set", message)
        self.assertEqual(options, {"cores": None, "tasks": None, "gpus": None})

    def test_deliberate_aedt_with_undetectable_cpu_count_does_not_warn(self):
        # Regression test: ANSYS_ANALYZE_CORES=aedt with no tasks/gpus
        # override is a deliberate, fully-honored request to use
        # whatever's active in AEDT's dialog -- os.cpu_count() being
        # undetectable is irrelevant to it (cores would be None either
        # way). The "no override" warning above used to fire here too,
        # telling the operator to "set ANSYS_ANALYZE_CORES explicitly"
        # to undo the exact thing they explicitly asked for.
        os.environ[hpc.CORES_ENV_VAR] = "aedt"
        # unittest.TestCase.assertNoLogs() would be the natural fit here,
        # but it's 3.10+ and this package declares `requires-python =
        # ">=3.9"` -- mock.patch.object + assert_not_called() works the
        # same way on 3.9 too.
        with mock.patch.object(hpc.os, "cpu_count", return_value=None):
            with mock.patch.object(hpc.logger, "warning") as warning:
                options = hpc.resolve_hpc_options()
        warning.assert_not_called()
        self.assertEqual(options, {"cores": None, "tasks": None, "gpus": None})

    def test_warning_names_both_overriding_variables_when_both_are_set(self):
        # Regression test: the warning used to name only whichever of
        # tasks/gpus happened to be checked first, silently dropping the
        # other one even when both were set.
        os.environ[hpc.CORES_ENV_VAR] = "aedt"
        os.environ[hpc.TASKS_ENV_VAR] = "2"
        os.environ[hpc.GPUS_ENV_VAR] = "1"
        with mock.patch.object(hpc.os, "cpu_count", return_value=8):
            with self.assertLogs(hpc.logger, level="WARNING") as log:
                options = hpc.resolve_hpc_options()
        message = " ".join(log.output)
        self.assertIn(hpc.TASKS_ENV_VAR, message)
        self.assertIn(hpc.GPUS_ENV_VAR, message)
        self.assertEqual(options, {"cores": 8, "tasks": 2, "gpus": 1})


class DescribeHpcOptionsTests(unittest.TestCase):
    def test_all_none_describes_aedt_default(self):
        description = hpc.describe_hpc_options({"cores": None, "tasks": None, "gpus": None})
        self.assertIn("AEDT's own", description)

    def test_cores_only(self):
        description = hpc.describe_hpc_options({"cores": 16, "tasks": None, "gpus": None})
        self.assertEqual(description, "16 core(s)")

    def test_all_three_set(self):
        description = hpc.describe_hpc_options({"cores": 12, "tasks": 2, "gpus": 1})
        self.assertEqual(description, "12 core(s), 2 task(s), 1 GPU(s)")


if __name__ == "__main__":
    unittest.main()
