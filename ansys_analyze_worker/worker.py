"""
Generic queue-scanning worker: watches ANSYS_ANALYZE_QUEUE_PATH/pending for
new *.task.json files, dispatches each one to the right handler (by
task_type), and files the task away into done/ or failed/ when finished.

This module knows NOTHING about eigenmodes, pedestals, or any other
task-specific concept -- all of that lives in handlers/*.py and
post_processing.py. To support a new solution type or design in the
future, add a handler module and register it in handlers/__init__.py;
nothing here needs to change. See docs/ARCHITECTURE.md section 6 and 10.
"""
from __future__ import annotations

import glob
import json
import logging
import os
import shutil
import threading
import time
import traceback
from typing import Optional

from .handlers import get_handler
from .queue_common import QueuePaths, recover_orphaned_tasks, resolve_path, validate_task

logger = logging.getLogger("ansys_analyze_worker")


class Worker:
    def __init__(
        self,
        queue: QueuePaths,
        poll_interval_seconds: float = 5.0,
        aedt_version: Optional[str] = None,
        non_graphical: bool = False,
    ):
        self.queue = queue
        self.poll_interval_seconds = poll_interval_seconds
        self.aedt_version = aedt_version
        self.non_graphical = non_graphical

        self._pause_event = threading.Event()  # set == paused
        self._stop_event = threading.Event()
        self._restart_requested = False
        self._desktop = None

    # -- lifecycle --------------------------------------------------------
    def pause(self) -> None:
        self._pause_event.set()
        logger.info("Worker paused.")

    def resume(self) -> None:
        self._pause_event.clear()
        logger.info("Worker resumed.")

    @property
    def is_paused(self) -> bool:
        return self._pause_event.is_set()

    def stop(self) -> None:
        self._stop_event.set()

    def request_restart(self) -> None:
        """
        Ask the process to restart once the current tray/console loop
        returns -- see run_service.py, which checks `restart_requested`
        right after run_tray_app() and performs the actual process
        replacement (os.execv) if it's set. This is how the tray's
        "Reset" menu item works.

        This does NOT wait for an in-flight task to finish -- if one is
        mid-analysis in AEDT when this fires, the process restarts
        immediately, and that task's file (left behind in in_progress/)
        gets automatically moved back to pending/ and reprocessed from
        scratch the moment the new process's run_forever() starts (see
        recover_orphaned_tasks()). Same recovery path as an outright
        crash -- just triggered on purpose.
        """
        self._restart_requested = True
        self.stop()

    @property
    def restart_requested(self) -> bool:
        return self._restart_requested

    # -- AEDT connection ----------------------------------------------------
    def _ensure_desktop(self):
        if self._desktop is not None:
            return self._desktop
        from ansys.aedt.core import Desktop

        logger.info("Connecting to an already-open AEDT session...")
        self._desktop = Desktop(
            version=self.aedt_version,
            new_desktop=False,
            non_graphical=self.non_graphical,
        )
        return self._desktop

    # -- main loop ------------------------------------------------------------
    def run_forever(self) -> None:
        recovered = recover_orphaned_tasks(self.queue)
        if recovered:
            logger.info(
                "Recovered %d orphaned in-progress task(s) back to pending/ (from a previous "
                "crash or restart): %s", len(recovered), recovered
            )

        logger.info("Worker started. Watching %s", self.queue.pending)
        while not self._stop_event.is_set():
            if self.is_paused:
                time.sleep(self.poll_interval_seconds)
                continue
            try:
                self._process_pending_tasks()
            except Exception:
                logger.exception("Unhandled error in worker loop iteration")
            time.sleep(self.poll_interval_seconds)

    def _process_pending_tasks(self) -> None:
        task_files = sorted(glob.glob(os.path.join(self.queue.pending, "*.task.json")))
        for path in task_files:
            if self.is_paused or self._stop_event.is_set():
                break
            self._process_one(path)

    def _process_one(self, pending_path: str) -> None:
        filename = os.path.basename(pending_path)
        in_progress_path = os.path.join(self.queue.in_progress, filename)
        try:
            shutil.move(pending_path, in_progress_path)
        except FileNotFoundError:
            # another worker instance / manual intervention got to it first
            return

        task = None
        try:
            with open(in_progress_path, "r") as f:
                task = json.load(f)
            validate_task(task)

            # project_file/output_dir are normally relative to the queue
            # root (Part 1 copies the project bundle into
            # queue/projects/<task_id>/, since it usually runs on a
            # different machine than this worker). Resolve them to real
            # absolute paths ONCE here, so every handler and post-
            # processing op downstream only ever sees absolute paths and
            # never has to think about this.
            task["project_file"] = resolve_path(self.queue, task["project_file"])
            task["output_dir"] = resolve_path(self.queue, task["output_dir"])

            logger.info("Processing task %s (%s)", task["task_id"], task["task_type"])
            handler = get_handler(task["task_type"])

            hfss = self._open_project(task)
            try:
                result = handler(hfss, task, log=logger.info)
            finally:
                self._close_project(hfss)

            self._finish_success(in_progress_path, task, result)

        except Exception as exc:
            self._finish_failure(in_progress_path, task, exc)

    def _open_project(self, task: dict):
        from ansys.aedt.core import Hfss

        self._ensure_desktop()
        kwargs = dict(project=task["project_file"], new_desktop=False)
        if task.get("design_name"):
            kwargs["design"] = task["design_name"]
        return Hfss(**kwargs)

    def _close_project(self, hfss) -> None:
        try:
            if hfss and getattr(hfss, "project_name", None):
                hfss.close_project(name=hfss.project_name)
        except Exception:
            logger.warning("Failed to close project cleanly.", exc_info=True)

    def _finish_success(self, in_progress_path: str, task: dict, result: dict) -> None:
        out_dir = task["output_dir"]
        os.makedirs(out_dir, exist_ok=True)
        summary = {
            "status": "success",
            "task_id": task["task_id"],
            "task_type": task["task_type"],
            "metadata": task.get("metadata", {}),
            "result": result,
        }
        with open(os.path.join(out_dir, "result.json"), "w") as f:
            json.dump(summary, f, indent=2, default=str)

        done_path = os.path.join(self.queue.done, os.path.basename(in_progress_path))
        shutil.move(in_progress_path, done_path)
        logger.info("Task %s completed successfully.", task["task_id"])

    def _finish_failure(self, in_progress_path: str, task: Optional[dict], exc: Exception) -> None:
        logger.error("Task failed: %s", exc, exc_info=True)
        failed_path = os.path.join(self.queue.failed, os.path.basename(in_progress_path))
        try:
            shutil.move(in_progress_path, failed_path)
        except Exception:
            failed_path = in_progress_path

        error_text = f"{exc}\n\n{traceback.format_exc()}"
        with open(failed_path + ".error.log", "w") as f:
            f.write(error_text)

        if task and task.get("output_dir"):
            try:
                os.makedirs(task["output_dir"], exist_ok=True)
                with open(os.path.join(task["output_dir"], "result.json"), "w") as f:
                    json.dump(
                        {
                            "status": "failed",
                            "task_id": task.get("task_id"),
                            "task_type": task.get("task_type"),
                            "error": str(exc),
                        },
                        f,
                        indent=2,
                        default=str,
                    )
            except Exception:
                pass
