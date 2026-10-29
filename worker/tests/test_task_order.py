"""
Tests for the order the worker takes pending tasks in: oldest arrival first,
not alphabetical by file name (the "order of tasks" follow-up in
https://github.com/d-s-t/ansys-analyze-worker/issues/11, where every
`driven_*` task ran before an older `launch_*` one).

No AEDT needed -- `_process_one` is replaced by a recorder:

    python -m unittest tests.test_task_order -v   # from worker/
"""
from __future__ import annotations

import os
import shutil
import tempfile
import unittest
from unittest import mock

from ansys_analyze_common import queue_common
from ansys_analyze_common.queue_common import QueuePaths
from ansys_analyze_worker.worker import Worker


class TaskOrderTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.queue = QueuePaths(tmp.name)
        self.queue.ensure_exist()
        self.worker = Worker(self.queue)

    def add(self, task_id: str, mtime: float, directory: str = None) -> str:
        path = os.path.join(directory or self.queue.pending, f"{task_id}.task.json")
        with open(path, "w") as f:
            f.write("{}")
        os.utime(path, (mtime, mtime))
        return path

    def names(self):
        return [os.path.basename(p)[: -len(".task.json")] for p in self.worker._pending_task_files()]

    def test_oldest_arrival_first_not_alphabetical(self):
        # The reported case: driven_* sorts before launch_* by name, but launch_* arrived first.
        self.add("driven_20261029_100000", mtime=2000)
        self.add("launch_20261029_090000", mtime=1000)
        self.add("eigenmode_20261029_110000", mtime=3000)
        self.assertEqual(self.names(), ["launch_20261029_090000", "driven_20261029_100000",
                                        "eigenmode_20261029_110000"])

    def test_same_arrival_time_falls_back_to_name_order(self):
        for task_id in ("b", "c", "a"):
            self.add(task_id, mtime=1000)
        self.assertEqual(self.names(), ["a", "b", "c"])

    def test_only_task_files_are_listed(self):
        self.add("a", mtime=1000)
        with open(os.path.join(self.queue.pending, "notes.txt"), "w") as f:
            f.write("x")
        with open(os.path.join(self.queue.pending, ".tmp_half_written.json"), "w") as f:
            f.write("{")
        self.assertEqual(self.names(), ["a"])

    def test_empty_queue(self):
        self.assertEqual(self.names(), [])

    def test_a_file_taken_since_the_listing_is_skipped(self):
        gone = self.add("gone", mtime=1000)
        self.add("here", mtime=2000)
        real_getmtime = os.path.getmtime

        def getmtime(path):
            if path == gone:
                raise FileNotFoundError(path)
            return real_getmtime(path)

        with mock.patch.object(os.path, "getmtime", getmtime):
            self.assertEqual(self.names(), ["here"])

    def test_a_recovered_task_keeps_its_place_in_line(self):
        self.add("zzz_recovered", mtime=1000, directory=self.queue.in_progress)
        self.add("aaa_new", mtime=2000)
        queue_common.recover_orphaned_tasks(self.queue)
        self.assertEqual(self.names(), ["zzz_recovered", "aaa_new"])

    def test_a_retried_task_keeps_its_original_place_in_line(self):
        self.add("zzz_failed_earlier", mtime=1000, directory=self.queue.failed)
        self.add("aaa_new", mtime=2000)
        self.assertTrue(queue_common.retry_task(self.queue, "zzz_failed_earlier"))
        self.assertEqual(self.names(), ["zzz_failed_earlier", "aaa_new"])

    def test_tasks_are_processed_in_that_order(self):
        self.add("driven_b", mtime=2000)
        self.add("launch_a", mtime=1000)
        order = []
        with mock.patch.object(Worker, "_process_one", side_effect=lambda path: order.append(os.path.basename(path))):
            self.worker._process_pending_tasks()
        self.assertEqual(order, ["launch_a.task.json", "driven_b.task.json"])

    def test_a_task_built_by_the_queue_helpers_arrives_after_an_older_one(self):
        # End to end with the real writer: write_task_atomically's rename keeps the write time.
        older = queue_common.build_task("run_setups", "p/a.aedt", "p", task_id="launch_1")
        newer = queue_common.build_task("run_setups", "p/b.aedt", "p", task_id="driven_1")
        queue_common.write_task_atomically(self.queue, older)
        os.utime(os.path.join(self.queue.pending, "launch_1.task.json"), (1000, 1000))
        queue_common.write_task_atomically(self.queue, newer)
        self.assertEqual(self.names(), ["launch_1", "driven_1"])


if __name__ == "__main__":
    unittest.main()
