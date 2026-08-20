"""
Runs the actual Worker.run_forever() loop in its own child process, so the
tray icon's message loop (on the parent/main process, see run_service.py)
is never sharing a GIL with whatever blocking AEDT/COM call the worker is
in the middle of.

That's the whole reason the tray used to freeze solid the moment an
analysis started, even though the worker already ran on its own thread:
pyaedt's calls into AEDT go through pywin32's synchronous COM invocation,
which is a single C call that doesn't hand the GIL back until AEDT
actually replies -- Python's normal thread-switch-on-a-timer scheduling
never gets a chance to run in between, so the tray's own thread (and its
Win32 message pump) starves for as long as that call takes. A separate
OS process sidesteps this entirely: the two processes don't share a GIL
at all.

It also gives "stop the current run" (WorkerSupervisor.stop_current_run())
a real, unambiguous implementation. There's no safe way to interrupt a
blocking COM call from inside the same process/thread it's blocking --
but killing the whole worker process is a hard, reliable stop, and it
doesn't touch AEDT itself (this worker only ever *attaches* to an
already-running AEDT session -- see Worker._ensure_desktop() -- so
killing the automation connection leaves the application untouched). The
orphaned task file left behind in in_progress/ is picked back up
automatically the moment the fresh replacement process starts, via the
same queue_common.recover_orphaned_tasks() call an ordinary crash would
trigger.

Status (what's currently running, paused/released/running) crosses the
process boundary via a small JSON file the worker keeps overwritten with
its current state (Worker._set_status()), rather than a
multiprocessing.Manager().dict() -- that would need its own extra server
process for what's really just two string fields, and this project
already leans on "a JSON file on disk" as its status-reporting idiom
everywhere else (see queue_common.py). Pause/release/stop themselves DO
use multiprocessing.Event()s, since those are cheap, don't need a manager
process, and are exactly what they're built for.
"""
from __future__ import annotations

import json
import logging
import multiprocessing
import os
import sys
from typing import Optional

from ansys_analyze_common.queue_common import QueuePaths

from .worker import Worker

logger = logging.getLogger("ansys_analyze_worker")

STATUS_FILENAME = "worker_status.json"


def _worker_process_main(
    queue_root: str,
    poll_interval_seconds: float,
    aedt_version: Optional[str],
    non_graphical: bool,
    log_path: str,
    status_path: str,
    pause_event,
    release_event,
    stop_event,
) -> None:
    """
    Entry point for the worker child process. Has to be a plain,
    top-level, importable-by-reference function (not a bound method or
    closure) -- multiprocessing's 'spawn' start method (the only one
    available on Windows, which is what this app runs on) pickles the
    target by module path + name and re-imports it in the fresh child
    interpreter, rather than pickling a closure's captured state.
    """
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s: %(message)s",
        handlers=[logging.FileHandler(log_path, encoding="utf-8"), logging.StreamHandler(sys.stdout)],
    )

    queue = QueuePaths(queue_root)
    queue.ensure_exist()
    worker = Worker(
        queue,
        poll_interval_seconds=poll_interval_seconds,
        aedt_version=aedt_version,
        non_graphical=non_graphical,
        pause_event=pause_event,
        release_event=release_event,
        stop_event=stop_event,
        status_path=status_path,
    )
    worker.run_forever()


class WorkerSupervisor:
    """
    Lives in the parent (tray) process. Owns the worker child process plus
    the cross-process controls (pause/release/stop events, the status
    file) and exposes the pause/release/resume/... surface tray_app.py
    drives -- tray_app.py doesn't need to know or care that it's talking
    to a separate process rather than an in-process Worker.
    """

    def __init__(
        self,
        queue: QueuePaths,
        poll_interval_seconds: float,
        aedt_version: Optional[str],
        non_graphical: bool,
        log_path: str,
    ):
        self.queue = queue
        self.poll_interval_seconds = poll_interval_seconds
        self.aedt_version = aedt_version
        self.non_graphical = non_graphical
        self.log_path = log_path
        self.status_path = os.path.join(queue.logs, STATUS_FILENAME)

        self._pause_event = multiprocessing.Event()
        self._release_event = multiprocessing.Event()
        self._stop_event = multiprocessing.Event()
        self._process: Optional[multiprocessing.Process] = None
        self.restart_requested = False

    # -- process lifecycle --------------------------------------------------
    def start(self) -> None:
        self._stop_event.clear()
        self._process = multiprocessing.Process(
            target=_worker_process_main,
            args=(
                self.queue.root, self.poll_interval_seconds, self.aedt_version, self.non_graphical,
                self.log_path, self.status_path, self._pause_event, self._release_event, self._stop_event,
            ),
            daemon=True,
            name="ansys-analyze-worker",
        )
        self._process.start()

    def _terminate(self) -> None:
        if self._process is not None and self._process.is_alive():
            self._process.terminate()
            self._process.join(timeout=15)

    def stop_current_run(self) -> None:
        """
        Hard-abort whatever task is currently running by killing the whole
        worker process and starting a fresh one in its place. Does NOT
        pause scanning -- the fresh process comes back up in whatever
        pause/release state was already selected (the same Event objects
        are reused), and its startup recovery moves the aborted task back
        to pending/, so by default it's simply picked up and retried. To
        stop it from being retried immediately, Release first.
        """
        task_id = self.current_task_id
        if task_id is None:
            logger.info("Stop current run requested, but nothing is running right now.")
            return
        logger.info("Stopping current run (task %s) by restarting the worker process...", task_id)
        self._terminate()
        self.start()

    def request_restart(self) -> None:
        """
        Tears down the worker child; run_service.py checks
        `restart_requested` right after the tray's event loop returns and
        replaces the whole parent process (os.execv) if it's set -- that
        reloads tray_app.py's own code too, not just the worker's, which a
        worker-only restart wouldn't. This does NOT wait for an in-flight
        task to finish -- see stop_current_run()'s docstring for why that
        is safe.
        """
        self.restart_requested = True
        self._terminate()

    def exit(self) -> None:
        self._stop_event.set()
        self._terminate()

    # -- control surface tray_app.py drives ----------------------------------
    def release(self) -> None:
        self._release_event.set()

    def resume(self) -> None:
        self._pause_event.clear()
        self._release_event.clear()

    @property
    def is_paused(self) -> bool:
        return self._pause_event.is_set() or self._release_event.is_set()

    def _read_status(self) -> dict:
        try:
            with open(self.status_path) as f:
                return json.load(f)
        except (OSError, ValueError):
            return {}

    @property
    def is_released(self) -> bool:
        return self._release_event.is_set() and self._read_status().get("state") == "released"

    @property
    def current_task_id(self) -> Optional[str]:
        return self._read_status().get("current_task_id")

    @property
    def state_label(self) -> str:
        if not (self._process is not None and self._process.is_alive()):
            return "Stopped"
        if self.is_released:
            return "Released"
        if self.is_paused:
            return "Releasing (finishing current task)..." if self.current_task_id else "Paused"
        task_id = self.current_task_id
        return f"Running (task {task_id})" if task_id else "Running (idle)"
