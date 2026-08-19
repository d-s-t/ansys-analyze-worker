"""
Entry point for the ansys_analyze_worker background service.

Run this on the machine that has the FULL Ansys Electronics Desktop
license, with AEDT already open. It will:
  1. Attach to the running AEDT session.
  2. Watch ANSYS_ANALYZE_QUEUE_PATH/pending for new task files.
  3. Dispatch each task to the handler registered for its task_type.
  4. Write results into the task's output_dir and file the task into
     done/ or failed/.

A tray icon (bottom-right of the Windows taskbar) lets you pause/resume,
reset, or exit without hunting for a console window.

HOW TO RUN THIS (pick one):
  - After `pip install .` (or an editable install) of the worker/ package
    in this repo:
        ansys-analyze-worker
    (the console-script entry point defined in worker/pyproject.toml)
  - Or directly as a module, without needing the console-script:
        python -m ansys_analyze_worker.run_service
  - To run automatically at logon (like OneDrive), put a shortcut to
    EITHER of the above in the Windows Startup folder (shell:startup),
    using pythonw.exe (or the console-script's .exe directly) so no
    console window appears.

This file uses package-relative imports, so it can't be run as a bare
script (`python run_service.py`) -- see docs/ARCHITECTURE.md section 10
for why, and for the editable-install workflow this is designed around.
"""
from __future__ import annotations

import argparse
import logging
import os
import sys
import threading

from ansys_analyze_common.queue_common import QueuePaths

from .worker import Worker


def _setup_logging(log_dir: str) -> str:
    os.makedirs(log_dir, exist_ok=True)
    log_path = os.path.join(log_dir, "ansys_analyze_worker.log")
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s: %(message)s",
        handlers=[
            logging.FileHandler(log_path, encoding="utf-8"),
            logging.StreamHandler(sys.stdout),
        ],
    )
    return log_path


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="ansys_analyze_worker -- the background analysis queue worker")
    parser.add_argument("--poll-interval", type=float, default=5.0, help="Seconds between scans of pending/")
    parser.add_argument("--aedt-version", type=str, default=None, help='e.g. "2024.1"')
    parser.add_argument("--non-graphical", action="store_true")
    parser.add_argument(
        "--no-tray", action="store_true", help="Run without a tray icon (plain console service)"
    )
    return parser.parse_args()


def main() -> None:
    args = parse_args()

    queue = QueuePaths.from_env("ANSYS_ANALYZE_QUEUE_PATH")
    log_path = _setup_logging(queue.logs)
    logger = logging.getLogger("ansys_analyze_worker")
    logger.info("Queue root: %s", queue.root)

    worker = Worker(
        queue,
        poll_interval_seconds=args.poll_interval,
        aedt_version=args.aedt_version,
        non_graphical=args.non_graphical,
    )

    worker_thread = threading.Thread(target=worker.run_forever, daemon=True)
    worker_thread.start()

    if args.no_tray:
        try:
            worker_thread.join()
        except KeyboardInterrupt:
            worker.stop()
        return

    from .tray_app import run_tray_app

    run_tray_app(worker, queue.root, log_path)
    worker.stop()

    if worker.restart_requested:
        logger.info("Reset requested from the tray -- restarting to pick up code changes...")
        # Re-invoke via `-m` explicitly rather than replaying sys.argv
        # verbatim: however this process was originally launched (the
        # installed console-script .exe, `python -m ...`, a Startup
        # shortcut), `-m ansys_analyze_worker.run_service` is guaranteed
        # to work as long as the package is importable -- which it must
        # be, since we're already running inside it. This re-imports
        # every module in the package from disk, so edits made to any
        # file while the service was running take effect. Any task that
        # was mid-analysis when Reset was clicked gets recovered
        # automatically the moment the new process's run_forever()
        # starts -- see queue_common.recover_orphaned_tasks().
        python = sys.executable
        os.execv(python, [python, "-m", "ansys_analyze_worker.run_service"] + sys.argv[1:])


if __name__ == "__main__":
    main()
