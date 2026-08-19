# ansys-analyze-worker

A generic background service that watches a task queue and runs Ansys
Electronics Desktop (AEDT) analyses on it -- plus the shared `queue_common`
schema that any "Part 1" (model-building) and "Part 3" (results-consuming)
project uses to talk to it. See [`docs/ARCHITECTURE.md`](docs/ARCHITECTURE.md)
for the full design doc, task/result JSON schema, and a guide to writing
new Part 1/Part 3 scripts against this package.

## Install

```
pip install -e .
```

This makes `from ansys_analyze_worker import queue_common` importable
from any other project on the machine, and installs the
`ansys-analyze-worker` console command. Being an editable install, edits
to files in this repo take effect immediately -- no reinstall needed,
which pairs well with the tray app's "Reset" option (see below).

## Run

On the machine with the full AEDT license, with AEDT already open:

```
ansys-analyze-worker
```

(equivalently: `python -m ansys_analyze_worker.run_service`)

This attaches to the running AEDT session, watches
`$ANSYS_ANALYZE_QUEUE_PATH/pending` for new task files, and shows a tray
icon (bottom-right of the Windows taskbar) with:

- **Pause / Resume processing**
- **Open queue folder** / **Open log file**
- **Reset (reload code)** -- restarts the service process so any edits
  made to files in this repo since it started take effect. Doesn't wait
  for an in-flight task to finish; see `Worker.request_restart()`'s
  docstring in `ansys_analyze_worker/worker.py` for exactly what happens
  to one if it's mid-analysis when you click this.
- **Exit**

Run with `--no-tray` for a plain console service instead (useful for
debugging, or running under a service manager that doesn't want a GUI).

## Extending

To support a new solution type or design, add a module to
`ansys_analyze_worker/handlers/` and register it in
`ansys_analyze_worker/handlers/__init__.py` -- see
[`docs/ARCHITECTURE.md`](docs/ARCHITECTURE.md) section 6. Nothing else in
the worker needs to change.

## Depending on this from another project

See [`docs/ARCHITECTURE.md`](docs/ARCHITECTURE.md) sections 8-9 for the
full guide to authoring Part 1/Part 3 scripts. In short, in the other
project's `requirements.txt`:

```
-e path/to/ansys-analyze-worker
```

and then:

```python
from ansys_analyze_worker import queue_common
```
