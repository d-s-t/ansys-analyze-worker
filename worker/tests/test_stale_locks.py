"""
Tests for ansys_analyze_worker.stale_locks -- clearing the project lock a
dead AEDT leaves behind so a recovered task can be opened again (the
"stale project lock" follow-up in
https://github.com/d-s-t/ansys-analyze-worker/issues/11).

No AEDT, PyAEDT or Windows needed: the lock files are written by the tests,
and process liveness is injected (or `psutil` replaced by a stand-in) so
nothing depends on which processes happen to be running.

    python -m unittest tests.test_stale_locks -v   # from worker/
"""
from __future__ import annotations

import json
import logging
import os
import tempfile
import threading
import types
import unittest
from unittest import mock

from ansys_analyze_common import queue_common
from ansys_analyze_common.queue_common import QueuePaths
from ansys_analyze_worker import stale_locks
from ansys_analyze_worker.worker import Worker

# Failure-path tests log warnings on purpose; without a handler Python's
# last-resort one would print them all to stderr during the run.
logging.getLogger("ansys_analyze_worker").addHandler(logging.NullHandler())

DEAD_PID = 4242
LIVE_PID = 777
LOCK_TEXT = "DesktopProcessID=%d\r\nHostName=WORKER-PC\r\nUserName=someone\r\n"


def is_running(pid: int) -> bool:
    return pid == LIVE_PID


class ProjectTestCase(unittest.TestCase):
    """A queue root with one project: projects/t1/pkg.aedt (+ lock, results, semaphores)."""

    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = tmp.name
        self.project_dir = os.path.join(self.root, "projects", "t1")
        self.project_file = os.path.join(self.project_dir, "pkg.aedt")
        self.lock = self.project_file + ".lock"
        self.results = self.project_file + "results"
        self.semaphores = [
            os.path.join(self.results, ".asol.semaphore"),
            os.path.join(self.results, "HFSSDesign1", ".asol12.semaphore"),
        ]
        self.keepers = [
            self.project_file,
            os.path.join(self.project_dir, "pkg.aedb", "edb.def"),
            os.path.join(self.results, "keep.txt"),
            os.path.join(self.results, ".other.semaphore"),
        ]
        for path in self.semaphores + self.keepers:
            self.write(path, "x")

    def write(self, path: str, text: str) -> None:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w") as f:
            f.write(text)

    def write_lock(self, pid: int = DEAD_PID) -> None:
        self.write(self.lock, LOCK_TEXT % pid)

    def assertKept(self, *paths):
        for path in paths:
            self.assertTrue(os.path.exists(path), f"{path} should still exist")

    def assertGone(self, *paths):
        for path in paths:
            self.assertFalse(os.path.exists(path), f"{path} should be gone")


class ReadLockPidTests(ProjectTestCase):
    def test_reads_the_pid_from_the_format_in_the_issue(self):
        self.write_lock(1234)
        self.assertEqual(stale_locks.read_lock_pid(self.lock), 1234)

    def test_tolerates_colon_case_spacing_and_surrounding_lines(self):
        for text in ("DesktopProcessID: 55", "desktopprocessid = 55\n", "[Lock]\nA=1\nDesktopProcessID=55\nB=2\n",
                     "DesktopProcessID=55"):
            with self.subTest(text=text):
                self.write(self.lock, text)
                self.assertEqual(stale_locks.read_lock_pid(self.lock), 55)

    def test_none_when_there_is_no_pid_or_no_file(self):
        for text in ("", "HostName=PC\n", "DesktopProcessID=\n", "DesktopProcessID=abc", "\x00\xff\xfe garbage"):
            with self.subTest(text=text):
                self.write(self.lock, text)
                self.assertIsNone(stale_locks.read_lock_pid(self.lock))
        self.assertIsNone(stale_locks.read_lock_pid(os.path.join(self.root, "missing.lock")))


class IsAedtRunningTests(unittest.TestCase):
    class NoSuchProcess(Exception):
        pass

    def fake_psutil(self, name=None, error=None):
        def process(pid):
            if error is not None:
                raise error
            return types.SimpleNamespace(name=lambda: name)

        return types.SimpleNamespace(Process=process, NoSuchProcess=self.NoSuchProcess)

    def check(self, **kwargs):
        with mock.patch.object(stale_locks, "psutil", self.fake_psutil(**kwargs)):
            return stale_locks.is_aedt_running(123)

    def test_ansysedt_is_running_whatever_the_case(self):
        self.assertTrue(self.check(name="ansysedt.exe"))
        self.assertTrue(self.check(name="AnsysEDT.exe"))

    def test_no_such_process_is_not_running(self):
        self.assertFalse(self.check(error=self.NoSuchProcess()))

    def test_a_pid_reused_by_another_program_is_not_aedt(self):
        # Windows hands dead pids to new processes; the lock's pid alone proves nothing.
        self.assertFalse(self.check(name="chrome.exe"))

    def test_when_psutil_cannot_tell_the_lock_is_kept(self):
        self.assertTrue(self.check(error=PermissionError("access denied")))
        with mock.patch.object(stale_locks, "psutil", None):
            self.assertTrue(stale_locks.is_aedt_running(123))


class ClearStaleLockTests(ProjectTestCase):
    def clear(self):
        return stale_locks.clear_stale_lock(self.project_file, is_running)

    def test_dead_aedt_lock_and_semaphores_are_removed_and_nothing_else(self):
        self.write_lock(DEAD_PID)
        removed = self.clear()
        self.assertEqual(sorted(removed), sorted([self.lock] + self.semaphores))
        self.assertGone(self.lock, *self.semaphores)
        self.assertKept(*self.keepers)

    def test_a_running_aedt_keeps_its_lock_and_semaphores(self):
        self.write_lock(LIVE_PID)
        self.assertEqual(self.clear(), [])
        self.assertKept(self.lock, *self.semaphores, *self.keepers)

    def test_no_lock_means_nothing_is_touched(self):
        self.assertEqual(self.clear(), [])
        self.assertKept(*self.semaphores, *self.keepers)

    def test_a_lock_naming_no_process_is_kept_with_a_warning(self):
        self.write(self.lock, "something else entirely")
        with self.assertLogs("ansys_analyze_worker", level="WARNING") as logs:
            self.assertEqual(self.clear(), [])
        self.assertIn("names no AEDT process", logs.output[0])
        self.assertKept(self.lock, *self.semaphores)

    def test_a_lock_that_cannot_be_removed_leaves_the_semaphores_too(self):
        self.write_lock(DEAD_PID)
        real_remove = os.remove

        def deny_lock(path):
            if path == self.lock:
                raise PermissionError("in use")
            return real_remove(path)

        with mock.patch.object(os, "remove", deny_lock):
            with self.assertLogs("ansys_analyze_worker", level="WARNING"):
                self.assertEqual(self.clear(), [])
        self.assertKept(self.lock, *self.semaphores)

    def test_a_semaphore_that_cannot_be_removed_does_not_stop_the_rest(self):
        self.write_lock(DEAD_PID)
        real_remove = os.remove

        def deny_first_semaphore(path):
            if path == self.semaphores[0]:
                raise PermissionError("in use")
            return real_remove(path)

        with mock.patch.object(os, "remove", deny_first_semaphore):
            with self.assertLogs("ansys_analyze_worker", level="WARNING"):
                removed = self.clear()
        self.assertEqual(sorted(removed), sorted([self.lock, self.semaphores[1]]))
        self.assertGone(self.lock, self.semaphores[1])
        self.assertKept(self.semaphores[0])

    def test_never_raises(self):
        self.write_lock(DEAD_PID)
        with mock.patch.object(stale_locks, "read_lock_pid", side_effect=RuntimeError("boom")):
            with self.assertLogs("ansys_analyze_worker", level="WARNING"):
                self.assertEqual(self.clear(), [])

    def test_uses_the_real_liveness_check_by_default(self):
        self.write_lock(DEAD_PID)
        with mock.patch.object(stale_locks, "is_aedt_running", return_value=False) as check:
            removed = stale_locks.clear_stale_lock(self.project_file)
        check.assert_called_once_with(DEAD_PID)
        self.assertIn(self.lock, removed)


class ClearForTasksTests(ProjectTestCase):
    def setUp(self):
        super().setUp()
        self.queue = QueuePaths(self.root)
        self.queue.ensure_exist()

    def queue_task(self, task_id: str, project_file: str) -> None:
        task = queue_common.build_task("run_setups", project_file, "projects/" + task_id, task_id=task_id)
        queue_common.write_task_atomically(self.queue, task)

    def clear(self, task_ids):
        with mock.patch.object(stale_locks, "is_aedt_running", side_effect=is_running):
            return stale_locks.clear_stale_locks_for_tasks(self.queue, task_ids)

    def test_a_queue_relative_project_file_is_resolved_against_the_queue_root(self):
        self.write_lock(DEAD_PID)
        self.queue_task("t1", "projects/t1/pkg.aedt")
        self.assertIn(self.lock, self.clear(["t1"]))
        self.assertGone(self.lock)

    def test_an_absolute_project_file_is_used_as_is(self):
        self.write_lock(DEAD_PID)
        self.queue_task("t1", self.project_file)
        self.assertIn(self.lock, self.clear(["t1"]))

    def test_only_the_given_tasks_are_touched(self):
        other_lock = os.path.join(self.root, "projects", "other", "o.aedt.lock")
        self.write(other_lock, LOCK_TEXT % DEAD_PID)
        self.write_lock(DEAD_PID)
        self.queue_task("t1", "projects/t1/pkg.aedt")
        self.queue_task("other", "projects/other/o.aedt")
        self.clear(["t1"])
        self.assertGone(self.lock)
        self.assertKept(other_lock)

    def test_an_unreadable_task_file_is_skipped_and_the_rest_still_cleared(self):
        self.write_lock(DEAD_PID)
        self.queue_task("t1", "projects/t1/pkg.aedt")
        with open(os.path.join(self.queue.pending, "broken.task.json"), "w") as f:
            f.write("{not json")
        with self.assertLogs("ansys_analyze_worker", level="WARNING"):
            removed = self.clear(["missing", "broken", "t1"])
        self.assertIn(self.lock, removed)


class WorkerStartupTests(ProjectTestCase):
    """Worker.run_forever: recover orphaned tasks, then clear their stale locks."""

    def setUp(self):
        super().setUp()
        self.queue = QueuePaths(self.root)
        self.queue.ensure_exist()
        task = queue_common.build_task("run_setups", "projects/t1/pkg.aedt", "projects/t1", task_id="t1")
        queue_common.write_task_atomically(self.queue, task)
        os.replace(os.path.join(self.queue.pending, "t1.task.json"), os.path.join(self.queue.in_progress, "t1.task.json"))

    def start_worker(self, alive: bool):
        stop = threading.Event()
        stop.set()  # run the startup, skip the loop
        worker = Worker(self.queue, stop_event=stop)
        with mock.patch.object(stale_locks, "is_aedt_running", return_value=alive):
            worker.run_forever()

    def test_a_recovered_task_comes_back_with_its_stale_lock_gone(self):
        self.write_lock(DEAD_PID)
        self.start_worker(alive=False)
        self.assertTrue(os.path.exists(os.path.join(self.queue.pending, "t1.task.json")))
        self.assertGone(self.lock, *self.semaphores)
        self.assertKept(*self.keepers)

    def test_a_live_aedts_lock_survives_the_restart(self):
        self.write_lock(LIVE_PID)
        self.start_worker(alive=True)
        self.assertTrue(os.path.exists(os.path.join(self.queue.pending, "t1.task.json")))
        self.assertKept(self.lock, *self.semaphores)

    def test_nothing_to_recover_checks_no_locks(self):
        os.remove(os.path.join(self.queue.in_progress, "t1.task.json"))
        self.write_lock(DEAD_PID)
        self.start_worker(alive=False)
        self.assertKept(self.lock)


if __name__ == "__main__":
    unittest.main()
