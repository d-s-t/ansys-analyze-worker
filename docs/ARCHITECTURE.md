# ansys-analyze-worker — Architecture & Authoring Guide

This document explains how the pipeline fits together and, most importantly,
how to write a **client pipeline** for future experiments. The worker
(this repo) is meant to stay generic and should not need to change when
you add a new design or solution type.

---

## 0. Two repos, two installable packages

This repo, `ansys-analyze-worker`, holds **two separate installable
Python packages** in two subdirectories, so a project can depend on just
the one it actually needs instead of pulling in the other's dependencies
(or its files) at all:

| Package | Subdirectory | Contains | Depends on |
|---|---|---|---|
| `ansys-analyze-common` | `common/` | `queue_common.py` only -- the shared, dependency-free (stdlib only) task/result schema. | nothing |
| `ansys-analyze-worker` | `worker/` | The worker itself: `run_service.py`, `supervisor.py`, `worker.py`, `tray_app.py`, `handlers/`. | `ansys-analyze-common`, `pyaedt`, `pystray`, `Pillow` |

Not published to PyPI -- install straight from GitHub, using pip's
`#subdirectory=` fragment to pick which of the two you want:

```
# On the AEDT machine (the worker itself):
pip install "git+https://github.com/d-s-t/ansys-analyze-worker.git#subdirectory=worker"

# In a client pipeline repo -- just the schema, none of the worker's
# files or dependencies:
pip install "git+https://github.com/d-s-t/ansys-analyze-worker.git#subdirectory=common"
```

`worker/pyproject.toml` declares `ansys-analyze-common` as a normal
dependency (via the same git URL), so installing the worker package
alone pulls the common one in automatically -- you never need both lines
for the worker machine.

The client pipeline -- the project that builds AEDT models, queues them
for analysis, monitors progress, and plots results -- lives in a
**separate repo per experiment** (e.g. `microwave-package`), which
depends on `ansys-analyze-common` via `requirements.txt`:

```
git+https://github.com/d-s-t/ansys-analyze-worker.git#subdirectory=common
```

and then imports the schema normally:

```python
from ansys_analyze_common import queue_common
```

Pin either install to a specific tag/branch/commit by appending `@<ref>`
right before the `#subdirectory=...` fragment, e.g.
`git+https://github.com/d-s-t/ansys-analyze-worker.git@v0.1.0#subdirectory=common`.

For local development on this repo (edits take effect immediately, no
reinstall needed -- pairs well with the tray app's "Reset" option):

```
pip install -e common/
pip install -e worker/       # pulls in common/ automatically too
```

§8 is written from the point of view of the separate, client-pipeline
repo; §10 covers this repo's own internals.

---

## 1. The two sides, at a glance

| Side | Lives in | Runs on | License needed | Job | Changes per experiment? |
|---|---|---|---|---|---|
| **Client pipeline** | a separate experiment repo (e.g. `microwave-package`) | Your modeling PC (and anywhere you check on progress or view plots) | Student version -- covers building geometry, creating the analysis setup, AND post-processing an already-solved project | Build the geometry, assign materials/boundaries, create the (single) analysis setup, queue an analysis task per model, wait for and monitor results, reopen each analyzed project to pull out its data and field plots, then plot the combined results | **Yes** -- new/adjusted script per experiment/design |
| **Worker** | **this repo**, `ansys-analyze-worker` (the `worker/` package) | The PC with the full license, AEDT already open | Full version -- needed for `analyze`/`analyze_setup` only | Sits in the background, watches a queue folder, picks up task files, opens the project, runs the ONE setup the client already created, hands it back | **No** -- generic engine + a small "handler" module per analysis type |

They are decoupled by a **task queue folder** on disk (`ANSYS_ANALYZE_QUEUE_PATH`):

```
Client pipeline (student AEDT)      Worker (full AEDT, background service)
   builds model, queues task  -->     watches queue, runs analysis
   monitors + plots results   <--     writes result.json + images
```

The client pipeline and the worker can run on the **same machine** or
**different machines**. If they're on different machines,
`ANSYS_ANALYZE_QUEUE_PATH` needs to point to a location both machines can
see (e.g. a shared/network drive) — see §10 for the details and the one
thing this implies for file paths.

---


## 2. On-disk layout of the queue

`ANSYS_ANALYZE_QUEUE_PATH` (an environment variable — see §10) points at a
root folder. Both the client pipeline and the worker need read/write
access to it — in the common case, they run on **different machines**,
so this needs to be a shared/network location. The worker creates and
manages these subfolders:

```
<ANSYS_ANALYZE_QUEUE_PATH>/
├── pending/       the client pipeline drops new *.task.json files here
├── in_progress/   the worker moves a task here while it's being processed
├── done/          successfully completed tasks end up here
├── failed/        tasks that raised an error end up here, plus a
│                   "<name>.task.json.error.log" with the traceback
├── projects/      <task_id>/ subfolders -- each one holds the .aedt +
│                   .aedb project bundle the client pipeline built AND
│                   everything the worker produces for that task
│                   (result.json, field-plot images, ...) -- see §2.1
└── _logs/         the worker's own log file
```

This gives you crash recovery (a task stuck in `in_progress/` after a
crash is easy to spot and re-queue) and a simple visual status board —
you can just look at the folder to see what's pending, running, done, or
failed.

Task files must be written **atomically** (write to a temp file, then
rename into place) so the worker never reads a half-written file. The
shared `queue_common.py` module (see §4) does this for you — always use
`queue_common.write_task_atomically()` rather than writing JSON directly
into `pending/`.

### 2.1 Why project files live *inside* the queue, and nowhere else

Early on it's tempting to have the task JSON just point at wherever the
client pipeline happened to save the `.aedt` file locally, and have the
worker write results back to some other local `analyzed/` folder. **That
breaks the moment the two sides run on different PCs** — a path like
`C:\Users\david\...\built_models\run_003\4mm_pedestal.aedt` only means
something on the machine that has that `C:\Users\david\...` folder; the
worker on the other machine can't open it, and even if it somehow could,
it would have nowhere valid to write results back to either.

The fix used throughout this pipeline: **the queue folder is the only
location guaranteed to be reachable from both sides**, so:

- The client pipeline **copies** the entire built project (`.aedt` file
  *and* its `.aedb` companion folder — the `.aedt` file alone isn't a
  complete, openable project) into `queue/projects/<task_id>/` before it
  ever writes the task file. See `queue_common.copy_project_to_queue()`.
- The worker writes everything it produces — `result.json`, field-plot
  images, the per-mode subfolders — back into that **same**
  `queue/projects/<task_id>/` folder, in place. There is deliberately no
  separate `results/` tree: an earlier version of this pipeline had one,
  and it just produced a second, redundant copy of the project data
  sitting next to the first with nothing pointing between them. `task
  ["output_dir"]` is set to that same folder (see
  `queue_common.project_dir_for_task()`), not somewhere else.
- The task JSON's `project_file` and `output_dir` fields are paths
  **relative to the queue root** (e.g.
  `"projects/eigenmode_analyze_20260813_.../0mm_pedestal.aedt"` and
  `"projects/eigenmode_analyze_20260813_.../"`), which either machine can
  turn into a real path with `queue_common.resolve_path(queue, path)`.

If you ever *do* run both sides on the same machine and want to skip the
copy step, `build_task()` will also accept absolute paths —
`resolve_path()` leaves anything already absolute untouched. That's the
exception, though; the default is always to copy into the queue and use
relative paths.

Because each task gets its own `<task_id>` subfolder under `projects/`,
and `task_id` already includes a timestamp (`queue_common.new_task_id()`),
there's no risk of two runs with the same base filename (e.g. two
different `0mm_pedestal.aedt`, from two separate batches) colliding.

### 2.2 Tracing a local folder back to its queued task

Since the project bundle gets copied away from the client pipeline's
local build folder, that local folder no longer *is* anything the worker
looks at directly. To keep it useful for tracing status later, the
client pipeline also drops a small `queue_task.json` marker into it (see
`queue_common.write_local_marker()`), recording the `task_id`. A status
check or monitoring loop (§9) reads these markers back to find and act
on tasks.

### 2.3 Cleaning up failed tasks

Failed tasks don't disappear on their own — they sit in `failed/` (plus
their `queue/projects/<task_id>/` folder and the local `queue_task.json`-
marked folder) until you deal with them. A cleanup flow in the client
pipeline can walk your local build folder tree, and for every task it
finds in the `failed` state, show the error and ask:

```
FAILED: pedestal_experiment\built_models\run_003\2mm_pedestal
  task_id: eigenmode_analyze_20260813_173805_009120
  error:   RuntimeError: Simulation Setup_1 failed. Check the HFSS message manager.
  [R]emove / [T]ry again / [S]kip?
```

- **Remove** (`queue_common.remove_task()`) deletes `failed/<task_id>
  .task.json` + its `.error.log`, the whole `queue/projects/<task_id>/`
  folder, AND the local build folder that queued it — cleaning up both
  sides in one step.
- **Try again** (`queue_common.retry_task()`) moves the task file back
  into `pending/` and clears the old error log, leaving the project
  bundle in `queue/projects/<task_id>/` exactly as it was. The worker
  picks it straight back up. This is safe to do repeatedly: the client
  creates its setup(s) once, up front (§8.1), and the worker never
  creates one itself, so re-running `analyze()` just re-solves the same
  setup(s) — there's no setup-creation logic on a retry that could pile
  up duplicates.
- **Skip** leaves it untouched for next time.

---

## 3. The task file (what the client pipeline writes)

Each task is one JSON file, `<task_id>.task.json`, in `pending/`.

### Required fields

| Field | Type | Meaning |
|---|---|---|
| `schema_version` | int | Always `1` for now. Lets the worker refuse task files from a future/incompatible format instead of guessing. |
| `task_id` | str | Unique ID. `queue_common.new_task_id()` generates one for you. |
| `task_type` | str | Which **handler** in the worker should process this task (e.g. `"eigenmode_analyze"`). This is the whole mechanism that lets the worker stay generic — see §6. |
| `project_file` | str | Path to the `.aedt` file, **relative to the queue root** (e.g. `"projects/<task_id>/0mm_pedestal.aedt"`) — see §2.1. Resolve with `queue_common.resolve_path()`. |
| `output_dir` | str | Path to the folder the worker should write everything it produces into, **relative to the queue root** — normally the SAME folder as `project_file`'s directory (e.g. `"projects/<task_id>"`, via `queue_common.project_dir_for_task()`), not a separate location. Same resolution. |

### Optional fields

| Field | Type | Meaning |
|---|---|---|
| `design_name` | str or null | Which design inside the project to open. Omit to use the default/only design. |
| `parameters` | object | Handler-specific extras, if any. `eigenmode_analyze` doesn't currently need any — the client already created the setup with every property it needs (frequency, modes, passes, ...) before queuing, so there's nothing left for the worker to configure. Kept in the schema for future handlers that might need it. |
| `objects` | object | Logical name → actual AEDT object name, e.g. `{"chip": "chip_1", "vacuum": "vacuum_1"}`. Not used by the worker — it's read back by the client pipeline's own post-processing step (§9), which uses it the same way the worker used to, to refer to "the chip" without knowing exact import-generated object names. |
| `post_processing` | list | Which generic post-processing operations to run per result (e.g. field plots). Not run by the worker — see §7, this is entirely a client-side concept now, carried on the task file just so the client's post-processing step can read back what it originally asked for. |
| `metadata` | object | Free-form data that has nothing to do with the analysis itself but that the client pipeline will want later (e.g. `{"pedestal_depth": 4.0}`). Copied through untouched into the results. |

### Example

```json
{
  "schema_version": 1,
  "task_id": "eigenmode_analyze_20260813_173801_412933",
  "task_type": "eigenmode_analyze",
  "created_at": "2026-08-13T17:38:01",
  "project_file": "projects\\eigenmode_analyze_20260813_173801_412933\\4mm_pedestal.aedt",
  "design_name": null,
  "solution_type": "Eigenmode",
  "output_dir": "projects\\eigenmode_analyze_20260813_173801_412933",
  "parameters": {},
  "objects": {
    "chip": "chip_1",
    "vacuum": "vacuum_1",
    "box": "box_1"
  },
  "post_processing": [
    {"op": "field_plot_surface", "params": {"object_key": "chip", "quantity": "Mag_E"}},
    {"op": "field_plot_volume", "params": {"object_key": "vacuum", "quantity": "Mag_E"}}
  ],
  "metadata": {
    "run": 3,
    "pedestal_depth": 4.0,
    "source_step_file": "C:\\...\\pedestal_experiment\\4mm_pedestal.STEP"
  }
}
```

The project already has its Eigenmode setup (created by the client before
it was ever saved and queued — see §8.1) with its `MinimumFrequency`/
`NumModes`/pass-count properties already set; `parameters` is empty
because there's nothing left for the worker to configure. The worker
doesn't need to be told the setup's name — it just runs whatever
setup(s) it finds (§6) — so nothing above records it either.

`queue_common.build_task(...)` builds and validates a dict in exactly this
shape for you — a client pipeline should always go through it rather than
building the dict by hand. The full sequence to follow:

```python
setup = hfss.create_setup(name="Setup_1")
setup.props["MinimumFrequency"] = "2.1GHz"
setup.props["NumModes"] = 20
# ... rest of the setup's properties ...

hfss.save_project(project_path)
hfss.close_project(name=hfss.project_name)   # release the .aedb before copying it

task_id = queue_common.new_task_id("eigenmode_analyze")
relative_project_file = queue_common.copy_project_to_queue(queue, task_id, project_dir)
relative_output_dir = queue_common.project_dir_for_task(task_id)

task = queue_common.build_task(
    task_type="eigenmode_analyze",
    project_file=relative_project_file,
    output_dir=relative_output_dir,
    task_id=task_id,
    solution_type="Eigenmode",
    objects={...}, post_processing=[...], metadata={...},
)
queue_common.write_task_atomically(queue, task)
queue_common.write_local_marker(project_dir, task_id, queue.root)
```

---

## 4. `common/ansys_analyze_common/queue_common.py` — the shared contract

`queue_common.py` is a single, dependency-free (stdlib only) module that
defines the task schema and the queue folder helpers. **Both** the
client pipeline and the worker (which depends on `ansys-analyze-common`)
use it — but now there's exactly one copy, living here, imported as a
real dependency rather than copy-pasted:

```python
from ansys_analyze_common import queue_common
```

Because it's an editable install during development (`pip install -e
common/` — §0), there's no "keep copies in sync" step: change the schema
here, and every project that depends on it sees the change the moment
they next import it (immediately for an editable install; on their next
`pip install --upgrade` otherwise). If you do change the schema, bump
`SCHEMA_VERSION` and update this doc.

Key functions:

- `QueuePaths.from_env()` — reads `ANSYS_ANALYZE_QUEUE_PATH` and returns
  an object with `.pending`, `.in_progress`, `.done`, `.failed`,
  `.projects` paths, creating them if needed.
- `build_task(...)` — assembles and validates a task dict.
- `write_task_atomically(queue, task)` — writes it into `pending/` safely.
- `validate_task(task)` — checked automatically by the above two; call it
  yourself if you're doing something unusual.
- `copy_project_to_queue(queue, task_id, local_project_dir)` /
  `project_dir_for_task(task_id)` — copy a project into the queue and
  compute the queue-relative folder both `project_file` and `output_dir`
  should point at (§2.1).
- `write_local_marker(...)` / `get_task_status(...)` — the local↔task_id
  tracing mechanism (§2.2) and status lookup.
- `read_task(queue, task_id)` — reads a task's JSON back from whichever
  stage folder it's currently in (done/failed/in_progress/pending).
  Used by the client pipeline's post-processing step (§9) to get back
  `project_file`/`output_dir`/`objects`/`post_processing` after the
  worker has already moved the task file out of `pending/`.
- `remove_task(...)` / `retry_task(...)` / `read_error_log(...)` — the
  cleanup primitives behind a client pipeline's cleanup flow (§2.3).
- `recover_orphaned_tasks(queue)` — moves anything stuck in
  `in_progress/` back to `pending/`; called automatically at worker
  startup, including after the tray's "Reset" (§10).

---

## 5. What the worker writes back (the results)

The worker's job ends the moment the setup is solved — it does NOT
extract mode data, build a CSV, or run any field plots. That all used to
happen here; it's moved to the client pipeline's post-processing step
(§9), because none of it needs the full license, only `analyze`/
`analyze_setup` does (§12). So for a successful task, the worker writes
just ONE small thing into `output_dir` (i.e. back into
`queue/projects/<task_id>/` itself, once resolved — §2.1):

- `result.json` — `status: "success"`, `task_id`, `task_type`,
  `metadata` (copied through from the task file unchanged), and a
  handler-specific `result` block. For `eigenmode_analyze`, `result` is
  just `{"setup_name": ..., "solved": true}` — confirmation that the
  client's setup ran and solved, nothing more.

The client pipeline reopens the project itself afterward (student
version) and writes its OWN `client_result.json` into the same folder,
with the actual mode data and post-processing output — see §9. That
split keeps the worker a thin "run the one setup that's already there,
hand back a yes/no" service; every plot-shaping and column decision lives
in the one client pipeline that actually cares about them.

For a **failed** task, `result.json` still gets written (with
`status: "failed"` and an `error` message) if `output_dir` was resolvable
at the point of failure, and the full traceback is always saved next to
the task file in `failed/<task_id>.task.json.error.log` regardless.

---

## 6. How the worker stays generic: the handler registry

`ansys_analyze_worker/worker.py` contains **zero** knowledge of eigenmodes,
pedestals, chips, or anything experiment-specific. All it does is:

1. Watch `pending/` for `*.task.json` files.
2. Move a task to `in_progress/`, load and validate the JSON.
3. Resolve `project_file`/`output_dir` against the queue root
   (`queue_common.resolve_path`) and overwrite them in the in-memory task
   dict with real absolute paths — everything downstream of this point
   (handlers, post-processing) only ever sees absolute paths and doesn't
   need to know relative-to-queue-root is even a concept.
4. Look up `HANDLERS[task["task_type"]]` in `ansys_analyze_worker/handlers/`.
5. Open the project (`Hfss(project=task["project_file"], ...)`).
6. Call `handler(hfss, task, log)`.
7. File the task into `done/` or `failed/` and write `result.json`.

**To support a new solution type or design in the future:**

1. Add a new module in `ansys_analyze_worker/handlers/`, e.g.
   `handlers/driven_modal_sweep.py`.
2. Implement `run(hfss, task, log) -> dict` in it. It receives the open
   `Hfss`/`Maxwell3d`/etc. object, the full task dict, and a logging
   function. Following `eigenmode_analyze.py`'s lead, it should find the
   setup(s) the client already created, run them, save the project, and
   return a small dict describing what happened (this becomes
   `result.json`'s `"result"` block) — it should NOT create a setup or
   extract/post-process results itself (§7, §12).
3. Register it: in `handlers/__init__.py`, add
   `"driven_modal_sweep": driven_modal_sweep.run` to `HANDLERS`.

Nothing in `worker.py`, `tray_app.py`, `supervisor.py`, or the queue
plumbing needs to change. This is also why `task_type` exists as a
separate field from `solution_type` — `task_type` names a whole
*workflow*, not just an AEDT solution type, even though for
`eigenmode_analyze` that workflow is now deliberately as small as
possible: open, run the one existing setup, hand it back.

---

## 7. Post-processing lives in the client pipeline now

There is no `post_processing.py` in this repo anymore. Extracting mode
data and running field plots against a solved project doesn't need the
full license — it's read-only export from solution data that's already
on disk — so it moved to the client pipeline, which reopens the analyzed
project itself (student version) right after the worker reports a task
done. See the *client* repo's `eigen_mode_analyze/post_processing.py`
(the generic ops, moved here basically unchanged) and
`eigen_mode_analyze/postprocess.py` (the eigenmode-result extraction plus
the per-run orchestration, adapted from what used to be
`handlers/eigenmode_chain.py`) — and §9 below for how it fits into the
pipeline. The task's `post_processing` list (§3) still describes which
ops to run per mode; the worker just carries that field through
untouched now instead of acting on it.

---

## 8. Authoring a client pipeline

This is the project you'll be writing/adjusting most often, so here's the
full recipe. A client pipeline is a single project (e.g.
`microwave-package`) that, end to end: builds geometry for each point in
a parameter sweep, queues an analysis task per point, waits for and
monitors the worker's progress, and plots the combined results once
they're in. It needs `ansys-analyze-common` installed as a dependency
(§0) and starts with:

```python
from ansys_analyze_common import queue_common
```

### 8.1 What the model-building step must do, per geometry

1. **Launch/attach to AEDT in student mode.** Pass
   `student_version=True` to `Hfss(...)`. This matters even if you only
   have the student version installed, because it tells PyAEDT which
   installation and licensing path to use.
2. **Build or import the geometry.** Either import a STEP/model file
   (`hfss.modeler.import_3d_cad(path)`) or build it parametrically with
   the modeler API.
3. **Assign materials and boundary conditions.** This is genuinely
   design-specific — write whatever logic identifies your objects (by
   name pattern, by a manifest file, however makes sense for your model)
   and assigns `material_name`, boundaries (`assign_perfect_e`,
   `assign_radiation_boundary`, ports, etc.).
4. **Create the analysis setup(s) — but do NOT call `analyze_setup` /
   `analyze`.** The student license covers `create_setup` and setting its
   properties just fine; it's only running the analysis that needs the
   full license/compute, which is the worker's entire job now (§6). This
   experiment only ever needs one setup, and `model_builder.py` creates
   just the one -- but the worker itself doesn't enforce that; it simply
   runs whatever setup(s) it finds (§6), so there's no need to route a
   future multi-setup experiment around it. Either way, the worker is the
   one that creates none and analyzes what's already there.
5. **Save and close the project** (`hfss.save_project(project_path)`,
   then `hfss.close_project(...)`) — close it *before* copying, so the
   `.aedb` folder isn't mid-write when it gets copied.
6. **Copy the project bundle into the queue and build the task file** —
   see §2.1/§3 for why this copy step exists and the exact call sequence
   (`copy_project_to_queue`, `project_dir_for_task`, `build_task`,
   `write_task_atomically`, `write_local_marker`).
7. Move to the next geometry. There's no separate "close" step here
   since the project was already closed in step 5, before the copy.

### 8.2 Checklist for the model-building step

- [ ] Uses `student_version=True`.
- [ ] Creates exactly one analysis setup and sets every property it
      needs (frequency, modes, passes, ...) — but never calls
      `analyze_setup` / `analyze`.
- [ ] Saves and closes the project, THEN copies it into the queue via
      `copy_project_to_queue()` — never queue a task while the project
      is still open (the `.aedb` folder may be mid-write).
- [ ] Uses the **relative-to-queue-root** paths that
      `copy_project_to_queue()`/`project_dir_for_task()` hand back for
      `project_file`/`output_dir` — not local absolute paths, and not two
      different folders (§2.1).
- [ ] Picks a `task_type` that either matches an existing worker handler,
      or that you've added a new handler for (§6).
- [ ] Puts everything about *this specific object* the post-processing
      step (§9) will need to find into `objects`, and which ops to run
      into `post_processing`.
- [ ] Puts everything the plotting step will need into `metadata`.
- [ ] Writes the task file **last**, only after the project has been
      copied into the queue successfully — never queue a task for a
      project bundle that isn't actually there yet.
- [ ] Calls `write_local_marker()` after queuing, so the local folder
      stays traceable to the task_id (§2.2).

### 8.3 A note on object naming

The mapping from "what got built" to "what the handler's post-processing
needs to find" (the `objects` dict passed into `build_task()`) is the
bridge between the two. Keep your object-naming convention (e.g. `chip`,
`chip_lid`, `box`, `vacuum`, `pcb` substrings) consistent across imported
geometry so this matching logic keeps working without per-file tweaks.

---

## 9. Monitoring, plotting, and cleanup

Once tasks are queued, the client pipeline needs to wait for and collect
results -- this part never touches AEDT, it's pure data wrangling,
plotting, and queue housekeeping. It does need the queue mounted, though,
since that's where results live (§2.1) — it doesn't need
`ANSYS_ANALYZE_QUEUE_PATH` to be set to the exact same value the
model-building step used, just to point at the same shared location.

### Monitoring

Poll `queue_common.get_task_status(queue, task_id)` for each queued
task_id and report transitions as they happen
(`pending`/`in_progress`/`done`/`failed`) rather than only at the very
end -- this is what lets you see progress live instead of just "it's
done or it isn't" once everything finishes. Treat an unreachable queue
path (e.g. a dropped network share) as transient: wait and retry rather
than crashing, so an interrupted run can pick back up once the share is
back.

### Post-processing (the reopen-and-extract step)

Once a task's `result.json` says `status: "success"`, the worker has done
its part — the project has a solved setup and nothing else. The client
pipeline is what turns that into data: reopen the same project
(`Hfss(project=..., student_version=True, ...)`, using the `objects`/
`post_processing`/`design_name`/`solution_type` fields off the ORIGINAL
task file — `queue_common.read_task(queue, task_id)` gets it back, since
by now it's moved out of `pending/`), pull the mode frequencies/Q out of
the solved setup, and run whatever `post_processing` ops the task asked
for (field plots, exports). Write the result as `client_result.json`
into that same `queue/projects/<task_id>/` folder, right next to the
worker's own `result.json`.

**Do this one task at a time, never in parallel.** There's no benefit to
overlapping it (it's quick, read-only solution-data export, run right
after the worker's `analyze()` for that same task), and a student AEDT
session isn't something you want two overlapping post-processing steps
driving at once.

`microwave-package`'s `eigen_mode_analyze/postprocess.py` is the
reference implementation of this whole step.

### Building and merging results

Turning each task's `client_result.json` into a CSV, and merging every
task's CSV into one table, is the client pipeline's job, same as before —
this part is pure data wrangling, no AEDT involved:

1. **Glob for `client_result.json` files** under the queue's `projects/`
   tree:
   ```python
   queue = queue_common.QueuePaths(queue_root)
   result_json_paths = glob.glob(os.path.join(queue.projects, "*", "client_result.json"))
   ```
2. **For each successful one**, build a table from
   `summary["result"]["modes"]`, attach every key in the ORIGINAL task's
   `metadata` (e.g. `pedestal_depth`) as a column, and write it out as a
   per-task CSV **in that same `projects/<task_id>/` folder**. Skip (and
   report) tasks that have no `client_result.json` yet, or whose status
   isn't `"success"`, or that have no mode data.
3. **Concatenate** every per-task table into one combined table and
   write it out alongside the plot.
4. **Plot.** Adapt the plotting code freely per experiment — this part
   is entirely yours.

### Status checking

Walk a local build-folder tree for the `queue_task.json` markers left
behind (§2.2), and for each one call
`queue_common.get_task_status(queue, task_id)` to report
`pending`/`in_progress`/`done`/`failed`/`unknown` without needing to dig
through the queue folders by hand.

### Cleanup

Same walk, but for every task found in the `failed` state, print the
error and ask [R]emove/[T]ry again/[S]kip, using
`queue_common.remove_task()` / `retry_task()` — see §2.3 for exactly
what each option deletes/re-queues.

### Checklist

- [ ] Post-processes (reopens + extracts) one task at a time, never in
      parallel.
- [ ] Reads `client_result.json` (its own, not the worker's `result.json`)
      from `queue.projects` (via `QueuePaths`) when building the combined
      results.
- [ ] Builds its own per-task CSV(s) from the raw `client_result.json`
      data — don't expect the worker to have already produced one.
- [ ] Handles the case where some tasks failed (missing/partial data)
      without crashing the whole plot.
- [ ] Saves a combined CSV alongside the plot(s) so you can re-plot later
      without re-reading every individual result.
- [ ] Treats a temporarily-unreachable queue path as something to wait
      out, not a fatal error, so an interrupted run can resume.

---

## 10. Worker internals reference (for maintenance)

### Files

`queue_common.py` lives in the separate `common/ansys_analyze_common/`
package (§0/§4) — everything below is under `worker/ansys_analyze_worker/`,
this repo's second importable package:

| File | Responsibility |
|---|---|
| `__init__.py` | Package marker + version. |
| `run_service.py` | Entry point. In tray mode (default), sets up logging, hands off to `supervisor.py`, and handles the Reset self-restart. In `--no-tray` mode, runs the worker loop directly on a background thread instead (§10.1 — no supervisor/child-process split needed when there's no tray to keep responsive). |
| `supervisor.py` | Tray mode only. Runs the worker in its own child process (`multiprocessing`) and exposes the release/resume/stop-current-run/reset control surface `tray_app.py` drives — see §10.1 for why. |
| `worker.py` | The generic scan/dispatch/file-away loop described in §6, plus the pause/release/stop-event handling that both `run_service.py` (threading.Event, no-tray mode) and `supervisor.py` (multiprocessing.Event, tray mode) drive it with. |
| `tray_app.py` | The taskbar icon (via `pystray`) — release/resume, stop current run, open queue folder, open log, reset, exit. |
| `handlers/` | One module per `task_type` (§6). |

### Import mechanics (why this matters if you add files)

This is a real, installed Python package now (`pip install -e worker/` —
§0), so most modules inside `ansys_analyze_worker/` use normal
**relative** imports for their own siblings (`from .handlers import
get_handler`, `from .worker import Worker` inside `supervisor.py`), and
an **absolute** import for the schema module, since that now lives
in the separate `ansys_analyze_common` package this one depends on:
`from ansys_analyze_common.queue_common import ...`. That means
`run_service.py` can no longer be run as a bare script (`python
run_service.py`) — relative imports need real package context. Always
launch it one of these two ways:

```
ansys-analyze-worker                        # the installed console-script
python -m ansys_analyze_worker.run_service   # equivalent, no console-script needed
```

Both work identically; the console-script (defined in `worker/pyproject.toml`'s
`[project.scripts]`) is just a shortcut for the `-m` form. **Never** run
`worker.py`, `supervisor.py`, `tray_app.py`, or a handler module directly
— same "attempted relative import with no known parent package" error
you'd get running any submodule of any package directly.

### Environment variables

| Variable | Required | Meaning |
|---|---|---|
| `ANSYS_ANALYZE_QUEUE_PATH` | Yes | Root folder for the task queue (§2). Must be reachable — read/write — from every machine involved. In the common two-machine setup this needs to be a shared/network path. Because project bundles and results both live *inside* this folder (§2.1), you don't need any other shared location — just this one. |

### Command-line options

| Flag | Default | Meaning |
|---|---|---|
| `--poll-interval` | `5.0` | Seconds between scans of `pending/`. |
| `--aedt-version` | auto | e.g. `"2024.1"`, if you need to pin a version. |
| `--non-graphical` | off | Run AEDT non-graphical (usually you want the opposite here, since you're attaching to an already-open, visible session). |
| `--no-tray` | off | Run as a plain console loop, no tray icon (useful for debugging or running under a service manager that doesn't want a GUI). |

### Running it in the background like OneDrive

Put a shortcut to the console-script (or to `pythonw.exe -m
ansys_analyze_worker.run_service`, so no console window appears) in your
Windows Startup folder (`shell:startup`), or register it as a Scheduled
Task set to run at logon. Either way, once running you get the tray icon
in the notification area with Stop current run/Release (or Resume)/Open
queue folder/Open log/**Reset**/Exit.

### 10.1 Why the tray runs in a separate process from the worker

In tray mode, `run_service.py` doesn't run the scan loop itself — it
starts `supervisor.py`'s `WorkerSupervisor`, which spawns the actual
`Worker.run_forever()` loop in its own **child process**
(`multiprocessing.Process`), and only then starts the tray icon's event
loop on the parent process's main thread.

This used to be a single process (worker loop on a background thread,
tray on the main thread), and on paper that should have kept the tray
responsive too — but it didn't, because pyaedt's calls into AEDT go
through pywin32's *synchronous* COM invocation, which blocks the calling
thread inside one C call for as long as AEDT takes to answer. That call
doesn't hand the GIL back partway through, so Python's normal
thread-switch-on-a-timer scheduling never gets a chance to run the tray's
own thread (and its Win32 message pump) while an analysis is in flight —
the whole process, tray included, froze solid for the duration. Two
separate OS processes don't share a GIL, so this can't happen anymore:
the tray's event loop is only ever competing with its own (light) work.

Pause/release/stop signal across the process boundary via
`multiprocessing.Event`s; "what's currently running" comes back the
other way via a small JSON status file (`_logs/worker_status.json`) the
worker keeps overwritten — see `Worker._set_status()` and
`WorkerSupervisor._read_status()`.

### Release (replacing Pause) and Stop current run

**Release** (`WorkerSupervisor.release()`) replaces the old plain Pause:
it stops the worker from picking up new tasks (same effect Pause used to
have) AND, once whatever task is currently running finishes — or right
away, if none is — detaches from AEDT (`Desktop.release_desktop
(close_projects=False, close_desktop=False)`) without closing it. It
does **not** interrupt a task that's already running; it just won't grab
another one, and it lets AEDT go once it's idle. **Resume** reconnects
(the next task simply reattaches, same as worker startup) and starts
scanning again.

**Stop current run** (`WorkerSupervisor.stop_current_run()`) is the hard
version: it kills the whole worker child process immediately, whatever
it's doing, and starts a fresh one in its place (reusing the same
pause/release state, so it doesn't silently resume from a released or
paused state). There's no safe way to interrupt a blocking COM call from
inside the thread it's blocking, which is exactly the process-separation
problem above all over again — so this doesn't try to; it just removes
the whole process that's stuck in one. AEDT itself is never touched (the
worker only ever *attaches* to an already-open session — §10 hasn't
changed that), only this process's automation connection to it dies. The
aborted task's file, left behind in `in_progress/`, is recovered back to
`pending/` by the fresh process's startup (see below) — by default it's
simply retried; Release first if you don't want that.

### The "Reset" option

Editing files in this repo while the worker is already running (it's an
editable install, so those edits are live on disk immediately) doesn't,
by itself, change what the running process has loaded into memory —
Python doesn't hot-swap already-imported code. The tray's **Reset
(reload code)** menu item closes that gap: it triggers a full **parent**
process restart (`WorkerSupervisor.request_restart()` in `supervisor.py`
tears down the child; `os.execv` in `run_service.py` then replaces the
parent process image), which re-imports every module in the package from
disk — the parent's own code (`tray_app.py`, `run_service.py`,
`supervisor.py`) as well as the worker's, once the fresh process spawns
its own new child.

It does **not** wait for an in-flight task to finish first. If a task is
mid-analysis in AEDT when you click Reset, the worker process is killed
immediately; that task's file is left behind in `in_progress/`, and the
moment the new worker child's `run_forever()` starts, `queue_common.
recover_orphaned_tasks()` moves it straight back to `pending/` to be
reprocessed from scratch — the same recovery path a genuine crash would
trigger, just on purpose. This is safe for `eigenmode_analyze` because
re-running `analyze()` on the same already-created setup is idempotent —
there's no leftover-setup cleanup to worry about the way the old
multi-setup `eigenmode_chain` handler needed (§6); keep that in mind if
you write a new handler that creates state of its own mid-run.

---

## 11. The eigenmode retrieval fix (why it matters)

This logic now lives in the **client pipeline**
(`microwave-package`'s `eigen_mode_analyze/postprocess.py`), not in this
repo — it's part of the post-processing step described in §7/§9, which
reopens the solved project after the worker hands it back. It's kept
here because the underlying AEDT/PyAEDT gotchas it works around have
nothing to do with which side of the queue folder runs the code.

The original script tried to read mode frequencies like this:

```python
freqs_data = hfss.post.get_solution_data(setup_sweep_name=f"{setup_name} : LastAdaptive")
freqs = freqs_data.intrinsics['Freq']
```

This is wrong for an **Eigenmode** solution. `Freq` is an *intrinsic
sweep variable* — it only exists for solution types that actually sweep
frequency (Driven Modal/Terminal). Eigenmode solutions don't sweep
frequency at all; instead, **each mode's resonant frequency is itself a
report quantity** (`Mode(1)`, `Mode(2)`, ...), and quality factor is a
separate quantity category (`"Eigen Q"`). Pulling `intrinsics['Freq']`
from an Eigenmode setup returns nothing useful — which is exactly the
symptom you were seeing.

The fix, used in `postprocess.py`:

```python
mode_exprs = hfss.post.available_report_quantities()               # e.g. "Mode(1)", "Mode(2)", ...
q_exprs = hfss.post.available_report_quantities(quantities_category="Eigen Q")

for mode_expr, q_expr in zip(mode_exprs, q_exprs):
    data = hfss.post.get_solution_data(
        expressions=mode_expr,
        setup_sweep_name=f"{setup_name} : LastAdaptive",
        report_category="Eigenmode",
    )
    freq_hz = _get_real_values(data, mode_expr)[0]   # see the note below
```

This matches Ansys' own PyAEDT eigenmode-filter example, and the pairing
between mode and Q values is **positional** (same list index = same
mode) rather than parsed out of the expression text, since AEDT returns
both lists in matching order and the expression string isn't guaranteed
to contain a parseable mode number.

**A THIRD bug, found after the above**: an earlier version of this fix
passed `report_category="Eigenmode"` and `solution=<setup>:LastAdaptive`
to the LISTING call, `available_report_quantities(...)`, reasoning by
analogy with `get_solution_data()` (which does need `report_category`).
Against this PyAEDT/AEDT version, filtering the listing call that way
silently returns an empty list — no error, it just finds nothing, which
is exactly the "the function returns nothing even though I can see the
modes in AEDT" symptom. `available_report_quantities()` should be called
with NO category/solution filters for the mode list, and only
`quantities_category="Eigen Q"` for the Q list — `report_category`
belongs on `get_solution_data()` only, never on the listing call. This
is now fixed in `postprocess.py`; if a future PyAEDT version changes
this behavior again, `get_eigenmode_results()` is the one place to
revisit.

**A fourth, PyAEDT-version-specific gotcha on top of this**: the method
used to pull the actual number out of the returned `SolutionData` object
has changed between PyAEDT releases -- older versions expose
`SolutionData.data_real()` (optionally taking the expression name),
while the version this pipeline is pinned to (`pyaedt==1.0.0`) uses
`get_expression_data(expression, formula="real")` instead, which also
returns a different shape (an `(x, y)` array pair rather than a flat
list). Calling the wrong one raises `AttributeError: 'SolutionData'
object has no attribute 'data_real'` (or the reverse, on an older
install). `get_eigenmode_results()`'s inner `first_real(expression)`
helper checks what the object actually exposes at runtime and adapts, so
post-processing works whichever of the two APIs the installed PyAEDT
version has -- if you hit this `AttributeError` again on a future PyAEDT
upgrade, that helper is the one place to extend rather than patching
call sites throughout the module.

---

## 12. Student vs. full license — what's actually restricted

Roughly: the **student version can build and save models, create an
analysis setup, and read/export from an already-solved one** (import
geometry, assign materials, define boundaries, `create_setup`, field
plots, solution-data export) -- it's only running the analysis itself
(`analyze`/`analyze_setup`) that's restricted (reduced problem-size
limits, and it's a separate license pool from the full/commercial one
your organization has for the heavy compute).

That's the reasoning behind the client-pipeline/worker split, and it's
deliberately as narrow as it can be: the full-license worker does exactly
one thing -- `analyze()` the setup the client already created -- and
everything else (`create_setup`, and all the post-processing that reads
back a solved setup) runs on the student-license client. Earlier versions
of this pipeline drew the line differently (setup creation AND
post-processing both behind the worker, with the worker chaining several
setups per project to search for a target frequency range); that turned
out to be more than the license boundary actually requires, and it meant
post-processing could never overlap with the worker analyzing a new
setup. Narrowing the worker down to just the compute-bound step removes
that limitation entirely -- there's nothing left inside the worker that
post-processing could conflict with.

### A note on the PyAEDT version

Both the worker's and the client's dependencies pin `pyaedt==1.0.0`
deliberately -- this is the confirmed-working version for both machines.
A newer PyAEDT release has already been observed to break the
`SolutionData` API the post-processing step relies on (§11's `first_real`
note), so don't `pip install --upgrade pyaedt` on either machine without
re-testing the whole pipeline against a small batch first.

---

## 13. Troubleshooting

| Symptom | Likely cause |
|---|---|
| Task sits in `pending/` forever | The worker isn't running, or is paused/released (check the tray icon's status line), or `ANSYS_ANALYZE_QUEUE_PATH` differs between the two machines/processes. |
| Task appears in `failed/` immediately | Check `<task>.task.json.error.log` next to it — usually a bad `project_file` path, a `task_type` with no registered handler, or a project with no setup at all (`eigenmode_analyze` runs whatever setup(s) it finds — see §6 — but needs at least one). |
| Worker can't connect to AEDT | Make sure the full AEDT session is already open on that machine before starting `run_service.py` — it attaches to an existing session (`new_desktop=False`), it does not launch one. If you just clicked Release, that's expected — it detaches on purpose; click Resume. |
| Post-processing (client pipeline) finds no modes, or fails to reopen the project | Check that the worker's `result.json` for that task actually says `status: "success"` first — post-processing assumes the setup solved. If it did solve but extraction still fails, see §11's PyAEDT-version notes. |
| "attempted relative import with no known parent package" | You ran a module inside `ansys_analyze_worker/` directly instead of via `ansys-analyze-worker` / `python -m ansys_analyze_worker.run_service` — see §10. |
| `ModuleNotFoundError: No module named 'ansys_analyze_common'` in a client pipeline | The client repo's dependency on the common package isn't installed — run `pip install -r requirements.txt` in that repo (§0), and check its `requirements.txt` line points at `...#subdirectory=common` (not `...#subdirectory=worker`, and not a stale local path). |
| `FileNotFoundError` / "project not found" when the worker opens a project | `ANSYS_ANALYZE_QUEUE_PATH` doesn't point at the same physical location on both machines (e.g. different drive letters for the same network share), or the client pipeline queued the task before the copy into `projects/<task_id>/` finished — check that the copy step (§2.1) completed without error before the task file was written. |
| A local build folder has no `queue_task.json` | Either that build failed before reaching the queuing step, or it's from before this marker mechanism existed — check the client pipeline's console output from when it was built. |
| The client pipeline says "No client_result.json files found" | Nothing has been post-processed yet (check a status report — a `done` worker task still needs its post-processing pass, §9), or the queue path points somewhere other than where the worker/client are actually writing. There's no per-task CSV to find directly — the client pipeline builds it itself from `client_result.json` (§5/§9), so an empty `projects/` tree means there's nothing to build from yet, not a bug. |
| A task stays in `failed/` after you thought you fixed the problem | A cleanup flow's "Try again" only re-queues the task file; it doesn't re-copy the project. If the fix required changing the model itself, remove the failed task and rebuild+requeue that model instead of retrying. |
| Cleanup's "Remove" didn't delete the local folder | It only deletes what the `queue_task.json` marker's containing folder points at — if you moved or renamed the local build folder after building, `remove_task()` deletes the queue side but can't find the (now-elsewhere) local folder. |
| Clicked "Stop current run" and the same task immediately started running again | Expected — by default the aborted task is just re-queued and picked back up (§10.1). Click Release first if you want scanning to stay paused after stopping it. |
