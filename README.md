# ansys-analyze-worker

A generic background service that watches a task queue and runs Ansys
Electronics Desktop (AEDT) analyses on it -- plus the shared `queue_common`
schema that any "Part 1" (model-building) and "Part 3" (results-consuming)
project uses to talk to it. See [`docs/ARCHITECTURE.md`](docs/ARCHITECTURE.md)
for the full design doc, task/result JSON schema, and a guide to writing
new Part 1/Part 3 scripts against this package.

## Install

This package is not published to PyPI. Install it straight from GitHub
with `pip`, in whichever of the two forms below matches the machine.

### On the AEDT machine (the worker itself)

Pull in the `server` extra too -- it's what brings in `pyaedt`, `pystray`,
`Pillow`, `pandas`, and `matplotlib`, none of which a client project needs
(see below):

```
pip install "ansys-analyze-worker[server] @ git+https://github.com/d-s-t/ansys-analyze-worker.git"
```

This makes `from ansys_analyze_worker import queue_common` importable
from any other project on the machine, and installs the
`ansys-analyze-worker` console command.

For local development instead (edits to files in this repo take effect
immediately, no reinstall needed -- pairs well with the tray app's
"Reset" option, see below):

```
pip install -e ".[server]"
```

### On a client machine/project (Part 1 or Part 3 scripts)

A client only ever needs `ansys_analyze_worker.queue_common`, which is
pure stdlib -- no need to drag in AEDT or plotting libraries it never
imports. Install the base package, no extra:

```
pip install git+https://github.com/d-s-t/ansys-analyze-worker.git
```

or add that same line to the client project's `requirements.txt` (see
e.g. `microwave-package`'s `requirements.txt`).

### Pinning a specific version

Once this repo has tagged releases, pin to one by appending `@<ref>`
(a tag, branch, or commit SHA) to the URL, e.g.:

```
pip install "ansys-analyze-worker[server] @ git+https://github.com/d-s-t/ansys-analyze-worker.git@v0.1.0"
pip install git+https://github.com/d-s-t/ansys-analyze-worker.git@v0.1.0
```

Omitting `@<ref>` (as in the commands above) tracks the default branch,
which is convenient while this is still moving fast but means `pip
install --upgrade` can pick up breaking changes -- pin once things
stabilize. To cut a release to pin to: `git tag v0.1.0 && git push --tags`.

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
git+https://github.com/d-s-t/ansys-analyze-worker.git
```

and then:

```python
from ansys_analyze_worker import queue_common
```
