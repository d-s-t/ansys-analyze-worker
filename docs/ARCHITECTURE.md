# ansys-analyze-worker — Architecture & Authoring Guide

This document explains how the pipeline fits together and, most importantly,
how to write new **Part 1** and **Part 3** scripts for future experiments.
Part 2 (the background worker, this repo) is meant to stay generic and
should not need to change when you add a new design or solution type.

---

## 0. Two repos

This repo, `ansys-analyze-worker`, **is** Part 2 — the generic background
worker plus the `queue_common` schema module that Part 1/Part 3 scripts
import. It's meant to be installed as a dependency, not copied around:

```
pip install -e path/to/ansys-analyze-worker
```

Everything experiment-specific — Part 1 and Part 3 scripts — lives in a
**separate repo per experiment** (e.g. `pedestal_experiment`), which
depends on this one via `requirements.txt`:

```
-e path/to/ansys-analyze-worker
```

and then imports the schema normally:

```python
from ansys_analyze_worker import queue_common
```

This is a real dependency relationship, not a shared-folder convention —
editing this repo (this is an editable install) changes what every
depending project sees immediately, no copying or reinstalling. §8/§9
are written from the point of view of that separate, experiment-specific
repo; §10 covers this repo's own internals.

---

## 1. The three parts, at a glance

| Part | Lives in | Runs on | License needed | Job | Changes per experiment? |
|---|---|---|---|---|---|
| **Part 1** | the experiment repo (e.g. `pedestal_experiment`) | Your modeling PC | Student version | Build the geometry, assign materials/boundaries, save the `.aedt` file, and drop a **task file** describing what analysis to run | **Yes** — new script per experiment/design |
| **Part 2** | **this repo**, `ansys-analyze-worker` | The PC with the full license, AEDT already open | Full version | Sits in the background, watches a queue folder, picks up task files, runs whatever analysis they describe, writes results | **No** — generic engine + a small "handler" module per analysis type |
| **Part 3** | the experiment repo | Anywhere (just needs the result files) | None (no AEDT needed) | Reads the result files Part 2 produced and turns them into the plots/tables you actually want | **Yes** — new script per experiment |

They are decoupled by a **task queue folder** on disk (`ANSYS_ANALYZE_QUEUE_PATH`):

```
Part 1 (student AEDT)          Part 2 (full AEDT, background service)      Part 3
   builds model         -->        watches queue, runs analysis      -->   reads results
   writes task.json               writes result.json + images              makes plots
```

Part 1 and Part 2 can run on the **same machine** or **different machines**.
If they're on different machines, `ANSYS_ANALYZE_QUEUE_PATH` needs to point
to a location both machines can see (e.g. a shared/network drive) — see
§10 for the details and the one thing this implies for file paths.

---


## 2. On-disk layout of the queue

`ANSYS_ANALYZE_QUEUE_PATH` (an environment variable — see §10) points at a
root folder. Both Part 1 and Part 2 need read/write access to it — in the
common case, Part 1 (modeling) and Part 2 (analysis) run on **different
machines**, so this needs to be a shared/network location. The worker
creates and manages these subfolders:

```
<ANSYS_ANALYZE_QUEUE_PATH>/
├── pending/       Part 1 drops new *.task.json files here
├── in_progress/   the worker moves a task here while it's being processed
├── done/          successfully completed tasks end up here
├── failed/        tasks that raised an error end up here, plus a
│                   "<name>.task.json.error.log" with the traceback
├── projects/      <task_id>/ subfolders -- each one holds the .aedt +
│                   .aedb project bundle Part 1 built AND everything
│                   Part 2 produces for that task (result.json, field-
│                   plot images, ...) -- see §2.1
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

Early on it's tempting to have the task JSON just point at wherever Part 1
happened to save the `.aedt` file locally, and have Part 2 write results
back to some other local `analyzed/` folder. **That breaks the moment
Part 1 and Part 2 run on different PCs** — a path like
`C:\Users\david\...\built_models\run_003\4mm_pedestal.aedt` only means
something on the machine that has that `C:\Users\david\...` folder; the
worker on the other machine can't open it, and even if it somehow could,
it would have nowhere valid to write results back to either.

The fix used throughout this pipeline: **the queue folder is the only
location guaranteed to be reachable from both sides**, so:

- Part 1 **copies** the entire built project (`.aedt` file *and* its
  `.aedb` companion folder — the `.aedt` file alone isn't a complete,
  openable project) into `queue/projects/<task_id>/` before it ever
  writes the task file. See `queue_common.copy_project_to_queue()`.
- Part 2 writes everything it produces — `result.json`, field-plot
  images, the per-mode subfolders — back into that **same**
  `queue/projects/<task_id>/` folder, in place. There is deliberately no
  separate `results/` tree: an earlier version of this pipeline had one,
  and it just produced a second, redundant copy of the project data
  sitting next to the first with nothing pointing between them. `task
  ["output_dir"]` is set to that same folder (see
  `queue_common.project_dir_for_task()`), not somewhere else.
- The task JSON's `project_file` and `output_dir` fields are paths
  **relative to the queue root** (e.g.
  `"projects/eigenmode_chain_20260813_.../0mm_pedestal.aedt"` and
  `"projects/eigenmode_chain_20260813_.../"`), which either machine can
  turn into a real path with `queue_common.resolve_path(queue, path)`.

If you ever *do* run Part 1 and Part 2 on the same machine and want to
skip the copy step, `build_task()` will also accept absolute paths —
`resolve_path()` leaves anything already absolute untouched. That's the
exception, though; the default (and what `part1_build_model.py` does) is
always to copy into the queue and use relative paths.

Because each task gets its own `<task_id>` subfolder under `projects/`,
and `task_id` already includes a timestamp (`queue_common.new_task_id()`),
there's no risk of two runs with the same base filename (e.g. two
different `0mm_pedestal.aedt`, from two separate batches) colliding.

### 2.2 Tracing a local folder back to its queued task

Since the project bundle gets copied away from Part 1's local
`built_models/` folder, that local folder no longer *is* anything Part 2
looks at directly. To keep it useful for tracing status later, Part 1
also drops a small `queue_task.json` marker into it (see
`queue_common.write_local_marker()`), recording the `task_id`. Part 3's
`--check_status` and `--cleanup` modes (§9) read these markers back to
find and act on tasks.

### 2.3 Cleaning up failed tasks

Failed tasks don't disappear on their own — they sit in `failed/` (plus
their `queue/projects/<task_id>/` folder and the local `queue_task.json`-
marked folder) until you deal with them. `part3_plot_results.py
--cleanup` walks your local `built_models/` tree, and for every task it
finds in the `failed` state, shows the error and asks:

```
FAILED: pedestal_experiment\built_models\run_003\2mm_pedestal
  task_id: eigenmode_chain_20260813_173805_009120
  error:   RuntimeError: Simulation Setup_1 failed. Check the HFSS message manager.
  [R]emove / [T]ry again / [S]kip?
```

- **Remove** (`queue_common.remove_task()`) deletes `failed/<task_id>
  .task.json` + its `.error.log`, the whole `queue/projects/<task_id>/`
  folder, AND the local `built_models/...` folder that queued it —
  cleaning up both sides in one step.
- **Try again** (`queue_common.retry_task()`) moves the task file back
  into `pending/` and clears the old error log, leaving the project
  bundle in `queue/projects/<task_id>/` exactly as it was. The worker
  picks it straight back up. This is safe to do repeatedly because
  `eigenmode_chain.run()` clears any leftover setups from a previous
  attempt at the very start (§6/handler code) — a retry never piles up
  duplicate setups on the same project.
- **Skip** leaves it untouched for next time.

---

## 3. The task file (what Part 1 writes)

Each task is one JSON file, `<task_id>.task.json`, in `pending/`.

### Required fields

| Field | Type | Meaning |
|---|---|---|
| `schema_version` | int | Always `1` for now. Lets the worker refuse task files from a future/incompatible format instead of guessing. |
| `task_id` | str | Unique ID. `queue_common.new_task_id()` generates one for you. |
| `task_type` | str | Which **handler** in Part 2 should process this task (e.g. `"eigenmode_chain"`). This is the whole mechanism that lets Part 2 stay generic — see §6. |
| `project_file` | str | Path to the `.aedt` file, **relative to the queue root** (e.g. `"projects/<task_id>/0mm_pedestal.aedt"`) — see §2.1. Resolve with `queue_common.resolve_path()`. |
| `output_dir` | str | Path to the folder Part 2 should write everything it produces into, **relative to the queue root** — normally the SAME folder as `project_file`'s directory (e.g. `"projects/<task_id>"`, via `queue_common.project_dir_for_task()`), not a separate location. Same resolution. |

### Optional fields

| Field | Type | Meaning |
|---|---|---|
| `design_name` | str or null | Which design inside the project to open. Omit to use the default/only design. |
| `parameters` | object | Handler-specific analysis parameters (for `eigenmode_chain`: starting frequency, target max frequency, number of modes, pass counts, etc. — see `handlers/eigenmode_chain.py`). |
| `objects` | object | Logical name → actual AEDT object name, e.g. `{"chip": "chip_1", "vacuum": "vacuum_1"}`. Lets post-processing steps refer to "the chip" without knowing exact import-generated object names. |
| `post_processing` | list | Which generic post-processing operations to run per result (e.g. field plots) — see §7. |
| `metadata` | object | Free-form data that has nothing to do with the analysis itself but that Part 3 will want later (e.g. `{"pedestal_depth": 4.0}`). Copied through untouched into the results. |

### Example (what `part1_build_model.py` writes for one pedestal depth)

```json
{
  "schema_version": 1,
  "task_id": "eigenmode_chain_20260813_173801_412933",
  "task_type": "eigenmode_chain",
  "created_at": "2026-08-13T17:38:01",
  "project_file": "projects\\eigenmode_chain_20260813_173801_412933\\4mm_pedestal.aedt",
  "design_name": null,
  "output_dir": "projects\\eigenmode_chain_20260813_173801_412933",
  "parameters": {
    "start_min_freq_ghz": 2.1,
    "target_max_freq_ghz": 20.0,
    "modes": 20,
    "max_passes": 99,
    "min_passes": 5,
    "min_converged": 3,
    "max_delta_f": 1.0
  },
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

`queue_common.build_task(...)` builds and validates a dict in exactly this
shape for you — Part 1 scripts should always go through it rather than
building the dict by hand. The full sequence a Part 1 script should
follow (see `part1_build_model.py`'s `build_one()`):

```python
hfss.save_project(project_path)
hfss.close_project(name=hfss.project_name)   # release the .aedb before copying it

task_id = queue_common.new_task_id("eigenmode_chain")
relative_project_file = queue_common.copy_project_to_queue(queue, task_id, project_dir)
relative_output_dir = queue_common.project_dir_for_task(task_id)

task = queue_common.build_task(
    task_type="eigenmode_chain",
    project_file=relative_project_file,
    output_dir=relative_output_dir,
    task_id=task_id,
    parameters={...}, objects={...}, post_processing=[...], metadata={...},
)
queue_common.write_task_atomically(queue, task)
queue_common.write_local_marker(project_dir, task_id, queue.root)
```

---

## 4. `ansys_analyze_worker/queue_common.py` — the shared contract

`queue_common.py` is a single, dependency-free (stdlib only) module that
defines the task schema and the queue folder helpers. **Both** Part 1/
Part 3 scripts (in the experiment repo) and Part 2 (this repo) use it —
but now there's exactly one copy, living here, imported as a real
dependency rather than copy-pasted:

```python
from ansys_analyze_worker import queue_common
```

Because it's an editable install (`pip install -e .` — §0), there's no
"keep copies in sync" step anymore: change the schema here, and every
project that depends on this repo sees the change the moment they next
import it. If you do change the schema, bump `SCHEMA_VERSION` and update
this doc.

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
  tracing mechanism (§2.2) and status lookup, used by Part 3.
- `remove_task(...)` / `retry_task(...)` / `read_error_log(...)` — the
  cleanup primitives behind `part3_plot_results.py --cleanup` (§2.3).
- `recover_orphaned_tasks(queue)` — moves anything stuck in
  `in_progress/` back to `pending/`; called automatically at worker
  startup, including after the tray's "Reset" (§10).

---

## 5. What Part 2 writes back (the results)

For a successful task, Part 2 writes ONE thing into `output_dir` (i.e.
back into `queue/projects/<task_id>/` itself, once resolved — §2.1):

- `result.json` — `status: "success"`, `task_id`, `task_type`,
  `metadata` (copied through from the task file unchanged), and a
  handler-specific `result` block. For `eigenmode_chain`, `result`
  contains `modes` — a list of per-mode dicts (`setup`, `mode`,
  `freq_ghz`, `q`, `convergence_error`, plus whatever the
  `post_processing` steps added, e.g. `max_mag_e`, `image_path`,
  `data_path`) — along with `num_modes_found` and `num_setups_run`.
- `<Setup>_Mode_<n>/` subfolders — one per mode, holding whatever images
  and per-mode CSVs the post-processing steps produced.

**Part 2 deliberately does NOT build `modes_results.csv` itself.**
Turning the raw `modes` list in `result.json` into a per-task CSV, and
merging every task's CSV into one combined table, is Part 3's job — see
§9. This keeps the worker a thin, generic "run the analysis, hand back
the numbers" service; all the plot-shaping and column decisions live in
the one script per experiment that actually cares about them, not
duplicated inside every handler.

For a **failed** task, `result.json` still gets written (with
`status: "failed"` and an `error` message) if `output_dir` was resolvable
at the point of failure, and the full traceback is always saved next to
the task file in `failed/<task_id>.task.json.error.log` regardless.

---

## 6. How Part 2 stays generic: the handler registry

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
   function; it should create setups, run the analysis, extract results,
   save the project, and return a small dict describing what it produced
   (this becomes `result.json`'s `"result"` block).
3. Register it: in `handlers/__init__.py`, add
   `"driven_modal_sweep": driven_modal_sweep.run` to `HANDLERS`.

Nothing in `worker.py`, `tray_app.py`, or the queue plumbing needs to
change. This is also why `task_type` exists as a separate field from
`solution_type` — `task_type` names a whole *workflow* (which might create
several setups, run several kinds of post-processing, etc.), not just an
AEDT solution type.

---

## 7. Reusable post-processing operations

`ansys_analyze_worker/post_processing.py` holds small, generic
operations that any handler can invoke per-result, driven entirely by
the task's `post_processing` list — this is what keeps field-plot/export
logic from being duplicated inside every handler.

Currently implemented:

- `field_plot_surface` — params: `object_key`, `quantity` (default
  `"Mag_E"`), `face` (default `"top"`). Creates a surface field plot on
  the named object's largest-area top face, exports a JPG and a CSV, and
  returns the max value found in the CSV as `max_<quantity>`.
- `field_plot_volume` — params: `object_key`, `quantity`. Creates a
  volume field plot and exports a JPG.

To add a new op: write a function
`op_my_thing(hfss, task, mode_context, params, out_dir) -> dict` in
`post_processing.py` and register it in `POST_PROCESSING_OPS`. Any
handler that calls `run_post_processing(...)` automatically gets access
to it via the task file — no handler code changes needed.

---

## 8. Authoring a Part 1 script

This is the part you'll be writing most often, so here's the full recipe.
Use `part1_build_model.py` (in an experiment repo, e.g.
`pedestal_experiment`) as the template — copy it and adjust the
model-specific bits. It needs this repo installed as a dependency
(§0) and starts with:

```python
from ansys_analyze_worker import queue_common
```

### 8.1 What Part 1 must do

1. **Launch/attach to AEDT in student mode.** Pass
   `student_version=True` to `Hfss(...)`. This matters even if you only
   have the student version installed, because it tells PyAEDT which
   installation and licensing path to use.
2. **Build or import the geometry.** Either import a STEP/model file
   (`hfss.modeler.import_3d_cad(path)`, as in the pedestal script) or
   build it parametrically with the modeler API.
3. **Assign materials and boundary conditions.** This is genuinely
   design-specific — write whatever logic identifies your objects (by
   name pattern, by a manifest file, however makes sense for your model)
   and assigns `material_name`, boundaries (`assign_perfect_e`,
   `assign_radiation_boundary`, ports, etc.).
4. **Do NOT create an analysis setup and do NOT call `analyze_setup` /
   `analyze`.** That is Part 2's job, and it's the operation that
   actually needs the full license/compute. Part 1 should only ever
   touch modeling, materials, and boundaries.
5. **Save and close the project** (`hfss.save_project(project_path)`,
   then `hfss.close_project(...)`) — close it *before* copying, so the
   `.aedb` folder isn't mid-write when it gets copied.
6. **Copy the project bundle into the queue and build the task file** —
   see §2.1/§3 for why this copy step exists and the exact call sequence
   (`copy_project_to_queue`, `project_dir_for_task`, `build_task`,
   `write_task_atomically`, `write_local_marker`). Compute whatever
   analysis parameters the handler needs first (e.g. a sensible starting
   frequency — see the note in §11 on why this needs care).
7. Move to the next model. There's no separate "close" step here since
   the project was already closed in step 5, before the copy.

### 8.2 Checklist for a new Part 1 script

- [ ] Uses `student_version=True`.
- [ ] Never calls `create_setup` / `analyze_setup` / `analyze`.
- [ ] Saves and closes the project, THEN copies it into the queue via
      `copy_project_to_queue()` — never queue a task while the project
      is still open (the `.aedb` folder may be mid-write).
- [ ] Uses the **relative-to-queue-root** paths that
      `copy_project_to_queue()`/`project_dir_for_task()` hand back for
      `project_file`/`output_dir` — not local absolute paths, and not two
      different folders (§2.1).
- [ ] Picks a `task_type` that either matches an existing Part 2 handler,
      or that you've added a new handler for (§6).
- [ ] Puts everything Part 2's handler needs into `parameters`, and
      everything about *this specific object* it might need to
      post-process into `objects`.
- [ ] Puts everything Part 3 will need to plot/tabulate into `metadata`.
- [ ] Writes the task file **last**, only after the project has been
      copied into the queue successfully — never queue a task for a
      project bundle that isn't actually there yet.
- [ ] Calls `write_local_marker()` after queuing, so the local folder
      stays traceable to the task_id (§2.2).

### 8.3 A note on object naming

`assign_materials_and_boundaries` (and the `objects` dict it returns) is
the bridge between "what Part 1 built" and "what Part 2's post-processing
needs to find." Keep your object-naming convention (e.g. `chip`,
`chip_lid`, `box`, `vacuum`, `pcb` substrings) consistent across STEP
exports so this matching logic keeps working without per-file tweaks.

---

## 9. Authoring a Part 3 script

Part 3 doesn't touch AEDT at all — it's pure data wrangling, plotting,
and queue housekeeping. Same as Part 1, it needs this repo installed as
a dependency (§0) and starts with `from ansys_analyze_worker import
queue_common`. It does need the queue mounted, though, since that's
where results live (§2.1) — it doesn't need `ANSYS_ANALYZE_QUEUE_PATH`
to be set to the exact same value Part 1/2 used, just to point at the
same shared location.

### Building and merging results

Part 2 only ever writes raw mode data into `result.json` (§5) — turning
that into a CSV, and merging every task's CSV into one table, is Part 3's
job. `build_and_load_results()` in `part3_plot_results.py` is the
reference implementation:

1. **Glob for `result.json` files** under the queue's `projects/` tree:
   ```python
   queue = queue_common.QueuePaths(queue_root)
   result_json_paths = glob.glob(os.path.join(queue.projects, "*", "result.json"))
   ```
2. **For each successful one**, build a DataFrame from
   `summary["result"]["modes"]`, attach every key in
   `summary["metadata"]` (e.g. `pedestal_depth`) as a column, and write
   it out as `modes_results.csv` **in that same `projects/<task_id>/`
   folder** — this is the "Part 3 creates the CSV files" step. Skip (and
   report) tasks whose `result.json` says `status != "success"`, or that
   have no mode data.
3. **Concatenate** every per-task DataFrame into one combined table and
   write it out (e.g. `all_results.csv`) alongside the plot.
4. **Plot.** `part3_plot_results.py` reproduces the original
   frequency-vs-depth scatter plot; adapt the plotting code freely for
   new experiments — this part is entirely yours.

### Selecting which runs to include

Every task's `metadata` carries a `run` number (the same integer as its
local `built_models/run_<NNN>/` folder — set by `part1_build_model.py`
via `get_next_run_dir()`). `--runs` filters `build_and_load_results()` by
it, using `parse_run_selector()`:

| Syntax | Meaning |
|---|---|
| `:` (default) | every run |
| `start:stop:step` | Python list-slice syntax, applied **positionally** over the sorted list of run numbers that actually exist — not over the numbers' values. `-2:` means "the last 2 runs that exist" even if their numbers aren't contiguous (e.g. runs 1, 2, 5 → `-2:` selects 2 and 5); `::2` means "every other run"; any of start/stop/step may be omitted, same as a real Python slice. |
| `2,5,7` | exactly those run numbers |
| `4` | exactly that run number |

```
> python part3_plot_results.py --runs -2:
--runs '-2:' -> runs [2, 3] (available: [1, 2, 3])
Skipping 2 run(s):
  - ...\projects\eigenmode_chain_..._1  (excluded by --runs (run=1))
  - ...\projects\eigenmode_chain_..._2  (excluded by --runs (run=1))
Combined 4 rows from 2 run(s) into ...\all_results.csv
```

Tasks with no `run` key in their metadata (e.g. from a `task_type` that
doesn't set one) are always included when `--runs` is left at its
default `:`, and always excluded whenever a specific selection is given
— there's no run number to match against.

### Status checking

`part3_plot_results.py --check_status` walks a local `built_models/` tree
for the `queue_task.json` markers Part 1 left behind (§2.2), and for each
one calls `queue_common.get_task_status(queue, task_id)` to report
`pending`/`in_progress`/`done`/`failed`/`unknown` without you needing to
dig through the queue folders by hand:

```
> python part3_plot_results.py --check_status --built_models_dir pedestal_experiment\built_models
...\built_models\run_003\0mm_pedestal   done         eigenmode_chain_20260813_173801_412933
...\built_models\run_003\2mm_pedestal   in_progress  eigenmode_chain_20260813_173805_009120
...\built_models\run_003\4mm_pedestal   pending      eigenmode_chain_20260813_173809_552011

Summary: done=1, in_progress=1, pending=1
```

### Cleanup

`part3_plot_results.py --cleanup` is the same walk, but for every task
found in the `failed` state it prints the error and asks
[R]emove/[T]ry again/[S]kip, using `queue_common.remove_task()` /
`retry_task()` — see §2.3 for exactly what each option deletes/re-queues.
Reuse `cleanup_failed_tasks()` as-is in new Part 3 scripts; nothing about
it is eigenmode/pedestal-specific.

### Checklist for a new Part 3 script

- [ ] Reads `result.json` from `queue.projects` (via `QueuePaths`), never
      touches AEDT.
- [ ] Builds its own per-task CSV(s) from the raw `result.json` data —
      don't expect Part 2 to have already produced one.
- [ ] Handles the case where some runs failed (missing/partial data)
      without crashing the whole plot.
- [ ] Saves a combined CSV alongside the plot(s) so you can re-plot later
      without re-reading every individual result.
- [ ] Reuses `check_status()`/`cleanup_failed_tasks()` (or copies the
      pattern) if a status report or cleanup flow is useful for the new
      experiment too.

---

## 10. Part 2 internals reference (for maintenance)

### Files

All under `ansys_analyze_worker/` (this repo's importable package — see §0):

| File | Responsibility |
|---|---|
| `__init__.py` | Package marker + version. |
| `queue_common.py` | The shared task/result schema and queue helpers (§4) — also what Part 1/Part 3 import. |
| `run_service.py` | Entry point. Sets up logging, starts the worker loop on a background thread, starts the tray icon on the main thread, handles the Reset self-restart. |
| `worker.py` | The generic scan/dispatch/file-away loop described in §6. |
| `tray_app.py` | The taskbar icon (via `pystray`) — pause/resume, open queue folder, open log, reset, exit. |
| `post_processing.py` | Reusable post-processing operations (§7). |
| `handlers/` | One module per `task_type` (§6). |

### Import mechanics (why this matters if you add files)

This is a real, installed Python package now (`pip install -e .` — §0),
so every module inside `ansys_analyze_worker/` uses normal **relative**
imports (`from .queue_common import ...`, `from .handlers import
get_handler`, `from ..post_processing import ...` inside `handlers/`).
That means `run_service.py` can no longer be run as a bare script
(`python run_service.py`) — relative imports need real package context.
Always launch it one of these two ways:

```
ansys-analyze-worker                        # the installed console-script
python -m ansys_analyze_worker.run_service   # equivalent, no console-script needed
```

Both work identically; the console-script (defined in `pyproject.toml`'s
`[project.scripts]`) is just a shortcut for the `-m` form. **Never** run
`worker.py`, `tray_app.py`, or a handler module directly — same
"attempted relative import with no known parent package" error you'd get
running any submodule of any package directly.

### Environment variables

| Variable | Required | Meaning |
|---|---|---|
| `ANSYS_ANALYZE_QUEUE_PATH` | Yes | Root folder for the task queue (§2). Must be reachable — read/write — from every machine involved (Part 1's, Part 2's, and whatever runs Part 3). In the common two-machine setup this needs to be a shared/network path. Because project bundles and results both live *inside* this folder (§2.1), you don't need any other shared location — just this one. |

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
in the notification area with Pause/Resume/Open queue folder/Open log/
**Reset**/Exit.

### The "Reset" option

Editing files in this repo while the worker is already running (it's an
editable install, so those edits are live on disk immediately) doesn't,
by itself, change what the running process has loaded into memory —
Python doesn't hot-swap already-imported code. The tray's **Reset
(reload code)** menu item closes that gap: it triggers a full process
restart (`Worker.request_restart()` in `worker.py`, actually performed
via `os.execv` in `run_service.py`), which re-imports every module in
the package from disk.

It does **not** wait for an in-flight task to finish first. If a task is
mid-analysis in AEDT when you click Reset, the process restarts
immediately; that task's file is left behind in `in_progress/`, and the
moment the new process's `run_forever()` starts, `queue_common.
recover_orphaned_tasks()` moves it straight back to `pending/` to be
reprocessed from scratch — the same recovery path a genuine crash would
trigger, just on purpose. This is safe for `eigenmode_chain` because it
already clears any leftover setups from a prior attempt at the start of
`run()` (§6); keep that pattern in mind if you write a new handler that
might get interrupted mid-run this way.

---

## 11. The eigenmode retrieval fix (why it matters)

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

The fix, used in `handlers/eigenmode_chain.py`:

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
is now fixed in `eigenmode_chain.py`; if a future PyAEDT version changes
this behavior again, `get_eigenmode_results()` is the one place to
revisit.

**A fourth, PyAEDT-version-specific gotcha on top of this**: the method
used to pull the actual number out of the returned `SolutionData` object
has changed between PyAEDT releases -- older versions expose
`SolutionData.data_real()` (optionally taking the expression name),
while the version this pipeline is pinned to (`requirements.txt`,
`pyaedt==1.0.0`) uses `get_expression_data(expression, formula="real")`
instead, which also returns a different shape (an `(x, y)` array pair
rather than a flat list). Calling the wrong one raises
`AttributeError: 'SolutionData' object has no attribute 'data_real'` (or
the reverse, on an older install). `eigenmode_chain.py`'s
`_get_real_values(data, expression)` helper checks what the object
actually exposes at runtime and adapts, so the handler works whichever of
the two APIs the installed PyAEDT version has -- if you hit this
`AttributeError` again on a future PyAEDT upgrade, that helper is the one
place to extend rather than patching call sites throughout the handler.

---

## 12. Student vs. full license — what's actually restricted

Roughly: the **student version can build and save models** (import
geometry, assign materials, define boundaries) but is limited on solving
(reduced problem-size limits, and it's a separate license pool from the
full/commercial one your organization has for the heavy compute). That's
the reasoning behind the Part 1/Part 2 split — put everything that needs
compute (`create_setup`, `analyze_setup`, and by extension all the
post-processing that depends on a solved setup) behind the full-license
worker, and keep the student-license machine doing pure modeling.

### A note on the PyAEDT version

`requirements.txt` pins `pyaedt==1.0.0` deliberately -- this is the
confirmed-working version for both machines. A newer PyAEDT release has
already been observed to break the `SolutionData` API this pipeline
relies on (§11's `_get_real_values` note), so don't
`pip install --upgrade pyaedt` on either machine without re-testing the
whole pipeline against a small batch first.

---

## 13. Troubleshooting

| Symptom | Likely cause |
|---|---|
| Task sits in `pending/` forever | Part 2 isn't running, or is paused (check the tray icon), or `ANSYS_ANALYZE_QUEUE_PATH` differs between the two machines/processes. |
| Task appears in `failed/` immediately | Check `<task>.task.json.error.log` next to it — usually a bad `project_file` path or a `task_type` with no registered handler. |
| Worker can't connect to AEDT | Make sure the full AEDT session is already open on that machine before starting `run_service.py` — it attaches to an existing session (`new_desktop=False`), it does not launch one. |
| Eigenmode chain finds suspiciously few / high-frequency-only modes | Check `start_min_freq_ghz` in the task's `parameters` — see §11's companion fix in Part 1, §8's note on `min_freq_safety_factor`. |
| "attempted relative import with no known parent package" | You ran a module inside `ansys_analyze_worker/` directly instead of via `ansys-analyze-worker` / `python -m ansys_analyze_worker.run_service` — see §10. |
| `ModuleNotFoundError: No module named 'ansys_analyze_worker'` in a Part 1/Part 3 script | The experiment repo's dependency on this one isn't installed — run `pip install -r requirements.txt` in the experiment repo (§0), and check its `-e path/to/ansys-analyze-worker` line points at the right local path. |
| `FileNotFoundError` / "project not found" when Part 2 opens a project | `ANSYS_ANALYZE_QUEUE_PATH` doesn't point at the same physical location on both machines (e.g. different drive letters for the same network share), or Part 1 queued the task before the copy into `projects/<task_id>/` finished — check that Part 1's copy step (§2.1) completed without error before the task file was written. |
| A local `built_models/...` folder has no `queue_task.json` | Either that build failed before reaching the queuing step, or it's from before this marker mechanism existed — check the script's console output from when it was built. |
| Part 3 says "No result.json files found" | Nothing has completed yet (check `--check_status`), or `--queue_dir`/`ANSYS_ANALYZE_QUEUE_PATH` points somewhere other than where Part 2 is actually writing. There's no `modes_results.csv` to find directly — Part 3 builds it itself from `result.json` (§5/§9), so an empty `projects/` tree means there's nothing to build from yet, not a Part 3 bug. |
| A task stays in `failed/` after you thought you fixed the problem | `--cleanup`'s "Try again" only re-queues the task file; it doesn't re-copy the project from Part 1's machine. If the fix required changing the model itself, remove the failed task and re-run Part 1 for that model instead of retrying. |
| Cleanup's "Remove" didn't delete the local folder | It only deletes what the `queue_task.json` marker's containing folder points at — if you moved or renamed the local `built_models/...` folder after building, `remove_task()` deletes the queue side but can't find the (now-elsewhere) local folder. |
