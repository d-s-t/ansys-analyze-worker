"""
Generic queue-scanning worker: watches ANSYS_ANALYZE_QUEUE_PATH/pending for
new *.task.json files, dispatches each one to the right handler (by
task_type), and files the task away into done/ or failed/ when finished.

This module knows NOTHING about eigenmodes, pedestals, or any other
task-specific concept -- all of that lives in handlers/*.py. To support a
new solution type or design in the future, add a handler module and
register it in handlers/__init__.py; nothing here needs to change. See
docs/ARCHITECTURE.md section 6 and 10.

`pause_event`/`release_event`/`stop_event` are accepted rather than always
created internally so run_service.py can hand this class
`multiprocessing.Event`s instead of `threading.Event`s when it runs the
worker in its own child process (the normal case -- see run_service.py's
module docstring for why the tray needs that separation). The two Event
types share the same set()/clear()/is_set() interface, so this class
doesn't need to know or care which one it was given; plain
`threading.Event()`s (the default) are enough for `--no-tray` mode, where
everything runs in one process/thread anyway.
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

from ansys_analyze_common.queue_common import QueuePaths, recover_orphaned_tasks, resolve_path, validate_task

from .handlers import get_handler

logger = logging.getLogger("ansys_analyze_worker")


class Worker:
    def __init__(
        self,
        queue: QueuePaths,
        poll_interval_seconds: float = 5.0,
        aedt_version: Optional[str] = None,
        non_graphical: bool = False,
        pause_event=None,
        release_event=None,
        stop_event=None,
        status_path: Optional[str] = None,
    ):
        self.queue = queue
        self.poll_interval_seconds = poll_interval_seconds
        self.aedt_version = aedt_version
        self.non_graphical = non_graphical

        self._pause_event = pause_event if pause_event is not None else threading.Event()  # set == paused
        self._release_event = release_event if release_event is not None else threading.Event()  # set == release requested
        self._stop_event = stop_event if stop_event is not None else threading.Event()
        self._desktop = None
        # Optional path to a small JSON file this worker keeps overwritten
        # with its current state -- the supervisor process (which owns the
        # tray icon) polls it to label the tray menu, since it can't just
        # read attributes off this object when the worker is running in a
        # separate process (see supervisor.py). Every write goes through
        # _set_status() so a transient write failure (e.g. the network
        # share hiccuping) can never crash the scan loop.
        self.status_path = status_path

    # -- lifecycle --------------------------------------------------------
    def pause(self) -> None:
        self._pause_event.set()
        logger.info("Worker paused.")

    def release(self) -> None:
        """
        Stop picking up new tasks (like pause()) AND, once whatever task is
        currently running finishes (or immediately, if none is), detach
        from AEDT without closing it -- see _maybe_release_desktop(). Does
        NOT interrupt a task that's already running; hard-aborting a
        running task isn't something this class can do to itself (there's
        no safe point to interrupt a blocking AEDT call from) -- that's
        WorkerSupervisor.stop_current_run() in supervisor.py, which kills
        and restarts the whole process instead.
        """
        self._release_event.set()
        logger.info("Release requested -- will detach from AEDT once the current task (if any) finishes.")

    def resume(self) -> None:
        self._pause_event.clear()
        self._release_event.clear()
        logger.info("Worker resumed.")

    @property
    def is_paused(self) -> bool:
        # Released implies paused: there's no "released but still picking
        # up new tasks" state, since a task needs a live AEDT connection.
        return self._pause_event.is_set() or self._release_event.is_set()

    @property
    def is_released(self) -> bool:
        return self._release_event.is_set() and self._desktop is None

    def stop(self) -> None:
        self._stop_event.set()

    # -- status reporting -------------------------------------------------
    def _set_status(self, **fields) -> None:
        if self.status_path is None:
            return
        self._status_cache = {**getattr(self, "_status_cache", {}), **fields}
        try:
            tmp_path = self.status_path + ".tmp"
            with open(tmp_path, "w") as f:
                json.dump(self._status_cache, f)
            os.replace(tmp_path, self.status_path)
        except OSError:
            # Status reporting is a nice-to-have (it only feeds the tray's
            # menu labels) -- never worth crashing the scan loop over a
            # transient write failure (e.g. the network share hiccuping).
            pass

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

    def _maybe_release_desktop(self) -> None:
        """
        Called between tasks, never during one -- see is_paused, which
        release() piggybacks on to make _process_pending_tasks() stop
        after whatever task is currently in flight rather than draining
        the rest of pending/ first. By the time this runs, nothing is
        mid-analysis, so it's always safe to drop the connection.
        """
        if not self._release_event.is_set() or self._desktop is None:
            return
        logger.info("Releasing the AEDT desktop connection (AEDT itself stays open)...")
        try:
            self._desktop.release_desktop(close_projects=False, close_desktop=False)
        except Exception:
            logger.warning("Failed to release the AEDT desktop connection cleanly.", exc_info=True)
        finally:
            self._desktop = None
        self._set_status(state="released")

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
            self._maybe_release_desktop()
            if self.is_paused:
                self._set_status(state="released" if self.is_released else "paused")
                time.sleep(self.poll_interval_seconds)
                continue
            self._set_status(state="running")
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
            # root (the client pipeline copies the project bundle into
            # queue/projects/<task_id>/, since it usually runs on a
            # different machine than this worker). Resolve them to real
            # absolute paths ONCE here, so every handler and post-
            # processing op downstream only ever sees absolute paths and
            # never has to think about this.
            task["project_file"] = resolve_path(self.queue, task["project_file"])
            task["output_dir"] = resolve_path(self.queue, task["output_dir"])

            logger.info("Processing task %s (%s)", task["task_id"], task["task_type"])
            handler = get_handler(task["task_type"])
            self._set_status(current_task_id=task["task_id"])

            hfss = self._open_project(task)
            try:
                result = handler(hfss, task, log=logger.info)
            finally:
                self._close_project(hfss)

            self._finish_success(in_progress_path, task, result)

        except Exception as exc:
            self._finish_failure(in_progress_path, task, exc)
        finally:
            self._set_status(current_task_id=None)

    def _open_project(self, task: dict):
        from ansys.aedt.core import Hfss

        self._ensure_desktop()
        kwargs = dict(project=task["project_file"], new_desktop=False)
        if task.get("design_name"):
            kwargs["design"] = task["design_name"]
        if task.get("solution_type"):
            # Passing this explicitly lets PyAEDT skip auto-detecting
            # the solution type via a GetSolutionType() round-trip to
            # the AEDT session -- see queue_common.build_task()'s
            # docstring for why that round-trip is worth avoiding.
            kwargs["solution_type"] = task["solution_type"]
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
