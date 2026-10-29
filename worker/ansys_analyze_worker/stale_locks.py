"""
Clear the project locks an AEDT that died mid-task leaves behind, so a
recovered task can be opened again.

**The problem.** When AEDT (or the machine) dies in the middle of a solve,
the project in `projects/<task_id>/` keeps `<project>.aedt.lock`, which
records the owning AEDT as `DesktopProcessID=<pid>`, and the
`.asol*.semaphore` files inside `<project>.aedtresults/`.
`recover_orphaned_tasks` puts the task file back into `pending/`, but
PyAEDT refuses to open a project whose `.lock` exists ("Project is locked.
Close or remove the lock before proceeding.") so the recovered task fails
straight away. See https://github.com/d-s-t/ansys-analyze-worker/issues/11.

**What it does.** For each recovered task, if the project's lock names an
AEDT process that is no longer running, remove the lock and the semaphores.

**What it deliberately does not do:**

- Only touch tasks that were *recovered*. Those were in `in_progress/`
  when the worker started, and only one worker runs against a queue, so
  the lock was left by an AEDT on this machine and its PID can be checked
  here. A lock on any other project may belong to an AEDT on another
  machine (the client opening a finished project from the shared queue,
  say) whose PID means nothing locally; deleting it would be unsafe, and
  PyAEDT's "Project is locked" error is the right outcome there.
- Remove anything when the lock's AEDT is still running (the worker died
  but AEDT lives on, or someone has the project open), or when it can't
  tell: an unreadable lock, one with no `DesktopProcessID`, or no psutil.
  Leaving a lock is always safe; deleting a live one is not.
- Ever raise. This runs at worker startup, and a failure to tidy up must
  not stop the worker from serving the queue; problems are logged.

The `DesktopProcessID=<pid>` format is taken from the issue, not from
Ansys documentation, so the parser is deliberately tolerant (`=` or `:`,
any case, any surrounding lines).
"""
from __future__ import annotations

import fnmatch
import json
import logging
import os
import re
from typing import Callable, List, Optional

from ansys_analyze_common.queue_common import QueuePaths, resolve_path

try:
    import psutil
except ImportError:  # pragma: no cover
    psutil = None

logger = logging.getLogger("ansys_analyze_worker")

LOCK_SUFFIX = ".lock"  # "<project>.aedt" -> "<project>.aedt.lock", as PyAEDT's is_project_locked() builds it
RESULTS_SUFFIX = "results"  # "<project>.aedt" -> "<project>.aedtresults"
SEMAPHORE_PATTERN = ".asol*.semaphore"
_PID_PATTERN = re.compile(r"DesktopProcessID\s*[=:]\s*(\d+)", re.IGNORECASE)
_AEDT_PROCESS_PREFIX = "ansysedt"  # ansysedt.exe, for graphical and non-graphical sessions alike


def read_lock_pid(lock_path: str) -> Optional[int]:
    """The `DesktopProcessID` recorded in an AEDT lock file, or None if it can't be read."""
    try:
        with open(lock_path, "r", errors="replace") as f:
            match = _PID_PATTERN.search(f.read())
    except OSError:
        return None
    return int(match.group(1)) if match else None


def is_aedt_running(pid: int) -> bool:
    """
    Whether `pid` is a live AEDT process. A pid that exists but belongs to
    some other program (Windows reuses them) is not AEDT. When psutil can't
    tell (not installed, access denied) this answers True: keep the lock.
    """
    if psutil is None:
        return True
    try:
        return psutil.Process(pid).name().lower().startswith(_AEDT_PROCESS_PREFIX)
    except psutil.NoSuchProcess:
        return False
    except Exception:
        return True


def clear_stale_lock(project_file: str, is_running: Optional[Callable[[int], bool]] = None) -> List[str]:
    """
    Remove `project_file`'s lock and `.asol*.semaphore` files if the AEDT
    that owned the lock is gone. Returns the paths removed ([] when the lock
    is absent, still live, or can't be judged). Never raises.
    """
    is_running = is_running or is_aedt_running
    lock_path = project_file + LOCK_SUFFIX
    try:
        if not os.path.exists(lock_path):
            return []

        pid = read_lock_pid(lock_path)
        if pid is None:
            logger.warning(
                "Leaving %s alone: it names no AEDT process (no DesktopProcessID), so it can't be shown to be stale.",
                lock_path,
            )
            return []
        if is_running(pid):
            logger.info("Leaving %s alone: AEDT process %d is still running.", lock_path, pid)
            return []

        try:
            os.remove(lock_path)
        except FileNotFoundError:
            pass
        except OSError as exc:
            # Without the lock gone the semaphores are moot, and the lock is the evidence they're stale.
            logger.warning("Could not remove stale %s: %s", lock_path, exc)
            return []

        removed = [lock_path]
        for path in _semaphores(project_file + RESULTS_SUFFIX):
            try:
                os.remove(path)
                removed.append(path)
            except FileNotFoundError:
                pass
            except OSError as exc:
                logger.warning("Could not remove stale %s: %s", path, exc)
        logger.info(
            "Removed %d stale lock file(s) left by AEDT process %d, which is no longer running: %s",
            len(removed), pid, removed,
        )
        return removed
    except Exception:
        logger.warning("Could not check %s for a stale lock.", lock_path, exc_info=True)
        return []


def clear_stale_locks_for_tasks(queue: QueuePaths, task_ids: List[str]) -> List[str]:
    """
    `clear_stale_lock` for the project of each recovered task (their task
    files are back in `pending/`). Returns every path removed. Never raises.
    """
    removed: List[str] = []
    for task_id in task_ids:
        task_path = os.path.join(queue.pending, f"{task_id}.task.json")
        try:
            with open(task_path, "r") as f:
                project_file = resolve_path(queue, json.load(f)["project_file"])
        except Exception:
            logger.warning("Could not read the project of recovered task %s to check it for a stale lock.", task_id,
                           exc_info=True)
            continue
        removed.extend(clear_stale_lock(project_file))
    return removed


def _semaphores(results_dir: str) -> List[str]:
    found = []
    for folder, _dirs, files in os.walk(results_dir):
        found.extend(os.path.join(folder, name) for name in files if fnmatch.fnmatch(name, SEMAPHORE_PATTERN))
    return found
