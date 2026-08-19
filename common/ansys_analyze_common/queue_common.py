"""
Shared, dependency-free (stdlib only) helpers for the Ansys analysis task
queue. Used by BOTH the client pipeline (model-building, student AEDT
version) and the worker (the generic analysis service, full AEDT
version, in the sibling `ansys-analyze-worker` package) so the task file
format can never drift between the two sides.

This lives in its own installable package, `ansys-analyze-common`, so a
client pipeline project can depend on just this file's contents without
pulling in the AEDT/tray-icon/plotting libraries that only the worker
package needs. See docs/ARCHITECTURE.md (in the `ansys-analyze-worker`
repo) for the full schema reference and install instructions.
"""
from __future__ import annotations

import datetime as _dt
import glob
import json
import os
import tempfile
from typing import Any, Dict, List, Optional

SCHEMA_VERSION = 1

REQUIRED_TOP_LEVEL_FIELDS = (
    "schema_version",
    "task_id",
    "task_type",
    "project_file",
    "output_dir",
)


class TaskValidationError(ValueError):
    """Raised when a task JSON file is missing required fields or malformed."""


def validate_task(task: Dict[str, Any]) -> None:
    missing = [f for f in REQUIRED_TOP_LEVEL_FIELDS if f not in task]
    if missing:
        raise TaskValidationError(f"Task is missing required field(s): {missing}")

    if task["schema_version"] != SCHEMA_VERSION:
        raise TaskValidationError(
            f"Unsupported schema_version {task['schema_version']!r}; "
            f"this tool understands version {SCHEMA_VERSION}."
        )

    if "post_processing" in task and not isinstance(task["post_processing"], list):
        raise TaskValidationError("'post_processing' must be a list if present.")

    for dict_field in ("parameters", "objects", "metadata"):
        if dict_field in task and not isinstance(task[dict_field], dict):
            raise TaskValidationError(f"'{dict_field}' must be an object if present.")


def new_task_id(prefix: str) -> str:
    stamp = _dt.datetime.now().strftime("%Y%m%d_%H%M%S_%f")
    safe_prefix = "".join(c if (c.isalnum() or c in "-_") else "_" for c in prefix)
    return f"{safe_prefix}_{stamp}"


def build_task(
    task_type: str,
    project_file: str,
    output_dir: str,
    *,
    design_name: Optional[str] = None,
    solution_type: Optional[str] = None,
    parameters: Optional[Dict[str, Any]] = None,
    objects: Optional[Dict[str, str]] = None,
    post_processing: Optional[List[Dict[str, Any]]] = None,
    metadata: Optional[Dict[str, Any]] = None,
    task_id: Optional[str] = None,
) -> Dict[str, Any]:
    """Convenience builder used by the client pipeline to assemble a valid task dict.

    Always prefer this over hand-building the dict -- it fills in
    schema_version/created_at/task_id consistently and validates the
    result before it's ever written to disk.

    `project_file` and `output_dir` are stored EXACTLY as given -- pass
    paths that are relative to the queue root (the normal case, see
    copy_project_to_queue() below) if the client pipeline and the worker
    run on different machines, since an absolute path from one machine is
    meaningless on the other. Pass absolute paths only if both sides
    genuinely share the same filesystem. Either way, resolve_path() is
    what turns whatever is stored here back into a real, openable path.

    `solution_type` (e.g. "Eigenmode") should be set whenever the client
    pipeline already knows it -- it built the project with that solution
    type, after all. The worker passes it straight through when opening
    the project, which lets PyAEDT skip auto-detecting it via a
    `GetSolutionType()` round-trip to the AEDT session. That round-trip
    is worth avoiding: it's an extra point of failure, and at least one
    PyAEDT version has a bug in its own fallback path when that call
    fails, masking the real error behind an unrelated AttributeError.
    """
    task = {
        "schema_version": SCHEMA_VERSION,
        "task_id": task_id or new_task_id(task_type),
        "task_type": task_type,
        "created_at": _dt.datetime.now().isoformat(timespec="seconds"),
        "project_file": project_file,
        "design_name": design_name,
        "solution_type": solution_type,
        "output_dir": output_dir,
        "parameters": parameters or {},
        "objects": objects or {},
        "post_processing": post_processing or [],
        "metadata": metadata or {},
    }
    validate_task(task)
    return task


class QueuePaths:
    """
    On-disk layout under ANSYS_ANALYZE_QUEUE_PATH:

        <root>/
            pending/       the client pipeline drops new *.task.json files here
            in_progress/   the worker moves a task here while processing it
            done/          successfully completed tasks end up here
            failed/        tasks that raised an error end up here
                           (+ "<name>.task.json.error.log")
            projects/      <task_id>/ subfolders -- each one holds the
                           .aedt/.aedb project bundle the client pipeline
                           queued AND everything the worker produces for
                           that task (result.json, field-plot images,
                           ...). There is deliberately no separate
                           "results/" tree: the worker updates files in
                           place inside the same projects/<task_id>/
                           folder rather than copying anything to a
                           second location.
            _logs/         the worker's own log file

    ANSYS_ANALYZE_QUEUE_PATH itself must be a location BOTH the modeling
    machine and the analysis machine can read and write (a shared/network
    drive, in the two-machine case). project_file/output_dir inside task
    JSON files are stored as paths relative to this root for exactly that
    reason -- see resolve_path().
    """

    def __init__(self, root: str):
        self.root = root
        self.pending = os.path.join(root, "pending")
        self.in_progress = os.path.join(root, "in_progress")
        self.done = os.path.join(root, "done")
        self.failed = os.path.join(root, "failed")
        self.projects = os.path.join(root, "projects")
        self.logs = os.path.join(root, "_logs")

    def ensure_exist(self) -> None:
        for d in (
            self.pending,
            self.in_progress,
            self.done,
            self.failed,
            self.projects,
            self.logs,
        ):
            os.makedirs(d, exist_ok=True)

    @classmethod
    def from_env(cls, env_var: str = "ANSYS_ANALYZE_QUEUE_PATH") -> "QueuePaths":
        root = os.environ.get(env_var)
        if not root:
            raise EnvironmentError(
                f"Environment variable {env_var} is not set. Point it at a "
                "folder both the modeling machine and the analysis machine "
                "can reach (e.g. a shared/network drive) that should hold "
                "the task queue."
            )
        qp = cls(root)
        qp.ensure_exist()
        return qp


def resolve_path(queue: "QueuePaths", path: str) -> str:
    """
    Turn a project_file/output_dir value from a task JSON into a real,
    openable absolute path.

    - If it's already absolute, it's used as-is (the "client pipeline and
      worker share a filesystem" case).
    - Otherwise it's treated as relative to the queue root (the normal,
      cross-machine case) -- e.g. "projects/<task_id>/0mm_pedestal.aedt"
      resolves to "<queue_root>/projects/<task_id>/0mm_pedestal.aedt",
      which is reachable from any machine that has the queue mounted.
    """
    return path if os.path.isabs(path) else os.path.join(queue.root, path)


def copy_project_to_queue(queue: "QueuePaths", task_id: str, local_project_dir: str) -> str:
    """
    Copy an entire local AEDT project folder (the .aedt file AND its
    .aedb companion folder -- the .aedt file alone is not a complete,
    openable project) into the shared queue, under a task_id-named
    subfolder so concurrent/repeated runs never collide even if they
    share a base filename like "0mm_pedestal". This same folder is where
    the worker will write everything it produces for the task too -- see
    project_dir_for_task().

    Returns the path to the copied .aedt file, RELATIVE to the queue
    root -- pass this straight into build_task(project_file=...).
    """
    import shutil

    dest_dir = os.path.join(queue.projects, task_id)
    if os.path.exists(dest_dir):
        shutil.rmtree(dest_dir)
    shutil.copytree(local_project_dir, dest_dir)

    aedt_files = [f for f in os.listdir(dest_dir) if f.lower().endswith(".aedt")]
    if not aedt_files:
        raise FileNotFoundError(f"No .aedt file found in {local_project_dir} after copying to {dest_dir}")
    if len(aedt_files) > 1:
        raise FileNotFoundError(
            f"Expected exactly one .aedt file in {local_project_dir}, found {aedt_files}"
        )

    return os.path.join("projects", task_id, aedt_files[0])


def project_dir_for_task(task_id: str) -> str:
    """
    The standard, queue-root-relative folder for a task -- both its copied
    project bundle AND everything the worker produces for it. Use this
    for `output_dir` when building the task so the worker updates files
    in place rather than writing to a second, separate location.
    """
    return os.path.join("projects", task_id)


def write_local_marker(local_project_dir: str, task_id: str, queue_root: str, extra: Optional[Dict[str, Any]] = None) -> str:
    """
    Drop a small `queue_task.json` marker file into the LOCAL run folder
    the client pipeline built the model in, recording which queued task
    it turned into. This is what lets you (or a status check) look at a
    local build folder later and know exactly which task_id to look for
    in the queue's pending/in_progress/done/failed folders and results
    tree.
    """
    marker = {
        "task_id": task_id,
        "queue_root": queue_root,
        "queued_at": _dt.datetime.now().isoformat(timespec="seconds"),
    }
    if extra:
        marker.update(extra)

    marker_path = os.path.join(local_project_dir, "queue_task.json")
    with open(marker_path, "w") as f:
        json.dump(marker, f, indent=2)
    return marker_path


def get_task_status(queue: "QueuePaths", task_id: str) -> str:
    """
    Look up where a task currently stands by checking for its task file
    in each stage folder. Returns one of:
    "pending", "in_progress", "done", "failed", or "unknown" (not found
    in any stage -- e.g. the queue was cleaned up, or the task_id is
    wrong).
    """
    for stage_name, stage_dir in (
        ("done", queue.done),
        ("failed", queue.failed),
        ("in_progress", queue.in_progress),
        ("pending", queue.pending),
    ):
        if os.path.exists(os.path.join(stage_dir, f"{task_id}.task.json")):
            return stage_name
    return "unknown"


def read_error_log(queue: "QueuePaths", task_id: str) -> Optional[str]:
    """Read back the traceback written for a failed task, if any."""
    error_log_path = os.path.join(queue.failed, f"{task_id}.task.json.error.log")
    if os.path.exists(error_log_path):
        with open(error_log_path) as f:
            return f.read()
    return None


def remove_task(queue: "QueuePaths", task_id: str, local_project_dir: Optional[str] = None) -> None:
    """
    Delete a task's failed/*.task.json + error log, its whole
    queue/projects/<task_id>/ folder, and (if given) the local build
    folder that queued it. Used by cleanup flows in a client pipeline --
    see docs/ARCHITECTURE.md section 2.3.
    """
    import shutil

    failed_task_file = os.path.join(queue.failed, f"{task_id}.task.json")
    failed_error_log = failed_task_file + ".error.log"
    for p in (failed_task_file, failed_error_log):
        if os.path.exists(p):
            os.remove(p)

    project_dir = os.path.join(queue.projects, task_id)
    if os.path.exists(project_dir):
        shutil.rmtree(project_dir)

    if local_project_dir and os.path.exists(local_project_dir):
        shutil.rmtree(local_project_dir)


def retry_task(queue: "QueuePaths", task_id: str) -> bool:
    """
    Move a failed task back into pending/ so the worker picks it up
    again, and clear its old error log. The project bundle in
    queue/projects/<task_id>/ is left in place -- the eigenmode_chain
    handler already clears any leftover setups from a previous attempt
    at the start of its run(), so re-running is safe. Returns False if
    the task wasn't actually in failed/ (nothing to retry).
    """
    import shutil

    failed_task_file = os.path.join(queue.failed, f"{task_id}.task.json")
    if not os.path.exists(failed_task_file):
        return False

    failed_error_log = failed_task_file + ".error.log"
    if os.path.exists(failed_error_log):
        os.remove(failed_error_log)

    pending_task_file = os.path.join(queue.pending, f"{task_id}.task.json")
    shutil.move(failed_task_file, pending_task_file)
    return True


def recover_orphaned_tasks(queue: "QueuePaths") -> List[str]:
    """
    Move every task file sitting in in_progress/ back into pending/.

    Only one worker process is expected to run against a given queue at a
    time, so anything still in in_progress/ when a worker starts up
    (whether after a crash, a manual restart, or the tray app's "Reset")
    was abandoned mid-task by a previous run -- it isn't actually being
    worked on right now. Moving it back to pending/ lets it be picked up
    and reprocessed from scratch, which is safe for eigenmode_chain
    because it already clears any leftover setups from a prior attempt
    at the start of its run(). Call this once, at worker startup.

    Returns the task_ids that were recovered.
    """
    import shutil

    recovered = []
    for path in glob.glob(os.path.join(queue.in_progress, "*.task.json")):
        filename = os.path.basename(path)
        shutil.move(path, os.path.join(queue.pending, filename))
        recovered.append(filename[: -len(".task.json")])
    return recovered


def write_task_atomically(queue: "QueuePaths", task: Dict[str, Any]) -> str:
    """
    Write a task dict as JSON into pending/ without the worker ever being
    able to see a half-written file: write to a temp file in the same
    folder, then atomically rename (os.replace) it into place.
    """
    validate_task(task)
    filename = f"{task['task_id']}.task.json"
    final_path = os.path.join(queue.pending, filename)

    fd, tmp_path = tempfile.mkstemp(prefix=".tmp_", suffix=".json", dir=queue.pending)
    try:
        with os.fdopen(fd, "w") as f:
            json.dump(task, f, indent=2)
        os.replace(tmp_path, final_path)
    except Exception:
        if os.path.exists(tmp_path):
            os.remove(tmp_path)
        raise
    return final_path
