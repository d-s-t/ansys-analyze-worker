"""
Tests for the startup log line that says which build of the worker is
running. The version alone can't: several commits share one number, and
"was the worker restarted on the new code?" is the first question when a
fix doesn't seem to take effect (as with the field_exports error texts on
https://github.com/d-s-t/ansys-analyze-worker/pull/13).

    python -m unittest tests.test_describe_installation -v   # from worker/
"""
from __future__ import annotations

import json
import logging
import tempfile
import threading
import unittest
from unittest import mock

from ansys_analyze_common.queue_common import QueuePaths
from ansys_analyze_worker import __version__
from ansys_analyze_worker.worker import Worker, describe_installation

logging.getLogger("ansys_analyze_worker").addHandler(logging.NullHandler())


def fake_distribution(direct_url_text):
    return mock.Mock(read_text=mock.Mock(return_value=direct_url_text))


class DescribeInstallationTests(unittest.TestCase):
    def describe(self, direct_url_text=None, error=None):
        distribution = mock.Mock(side_effect=error) if error else mock.Mock(return_value=fake_distribution(direct_url_text))
        with mock.patch("importlib.metadata.distribution", distribution):
            return describe_installation()

    def test_a_pip_install_from_git_shows_the_commit(self):
        text = json.dumps({"url": "https://github.com/d-s-t/ansys-analyze-worker.git",
                           "vcs_info": {"vcs": "git", "commit_id": "33dd406e0b7975eef545de0645aa7c7ba6d312ca"}})
        self.assertEqual(self.describe(text), f"ansys-analyze-worker {__version__} (git 33dd406)")

    def test_an_editable_install_says_so(self):
        text = json.dumps({"url": "file:///C:/src/worker", "dir_info": {"editable": True}})
        self.assertEqual(self.describe(text), f"ansys-analyze-worker {__version__} (editable install)")

    def test_a_plain_install_shows_just_the_version(self):
        self.assertEqual(self.describe(json.dumps({"url": "file:///x", "dir_info": {}})),
                         f"ansys-analyze-worker {__version__}")
        self.assertEqual(self.describe(None), f"ansys-analyze-worker {__version__}")

    def test_never_raises(self):
        self.assertEqual(self.describe(error=RuntimeError("no metadata")), f"ansys-analyze-worker {__version__}")
        self.assertEqual(self.describe("{not json"), f"ansys-analyze-worker {__version__}")


class StartupLogTests(unittest.TestCase):
    def test_the_first_startup_line_names_the_build(self):
        with tempfile.TemporaryDirectory() as tmp:
            queue = QueuePaths(tmp)
            queue.ensure_exist()
            stop = threading.Event()
            stop.set()  # run the startup, skip the loop
            with mock.patch("ansys_analyze_worker.worker.describe_installation", return_value="ansys-analyze-worker X (git abc1234)"):
                with self.assertLogs("ansys_analyze_worker", level="INFO") as logs:
                    Worker(queue, stop_event=stop).run_forever()
        self.assertEqual(logs.records[0].getMessage(), "Starting ansys-analyze-worker X (git abc1234)")


if __name__ == "__main__":
    unittest.main()
