# ansys-analyze-worker

A generic background service that watches a task queue and runs Ansys
Electronics Desktop (AEDT) analyses on it -- plus the shared `queue_common`
schema that any client pipeline (a project that builds AEDT models,
queues them, monitors progress, and plots results) uses to talk to it.
See [`docs/ARCHITECTURE.md`](docs/ARCHITECTURE.md) for the full design
doc, task/result JSON schema, and a guide to writing a client pipeline
against this worker.

This repo holds **two separate installable packages**, in two
subdirectories, so a client project can depend on just the lightweight
one it actually needs:

| Package | Subdirectory | Contains | Needs |
|---|---|---|---|
| `ansys-analyze-common` | `common/` | `queue_common.py` only -- the shared, dependency-free (stdlib only) task/result schema. | nothing |
| `ansys-analyze-worker` | `worker/` | The worker itself: the background service (runs in its own child process, supervised by the tray app), handlers. | `ansys-analyze-common`, `pyaedt`, `pystray`, `Pillow` |

## Install

Not published to PyPI -- install straight from GitHub with `pip`, using
the `#subdirectory=` fragment to pick which package you want.

### On the AEDT machine (the worker itself)

```
pip install "git+https://github.com/d-s-t/ansys-analyze-worker.git#subdirectory=worker"
```

`worker/pyproject.toml` declares `ansys-analyze-common` as a dependency
(via the same repo), so this pulls that in automatically along with
`pyaedt`/`pystray`/`Pillow`. This makes
`from ansys_analyze_worker import worker` importable and installs the
`ansys-analyze-worker` console command.

For local development instead (edits to files in this repo take effect
immediately, no reinstall needed -- pairs well with the tray app's
"Reset" option, see below):

```
pip install -e common/
pip install -e worker/
```

### On the client pipeline's machine/project

A client pipeline only ever needs `ansys_analyze_common.queue_common`,
which is pure stdlib -- no need to drag in AEDT or plotting libraries it
never imports. Install just the `common/` package:

```
pip install "git+https://github.com/d-s-t/ansys-analyze-worker.git#subdirectory=common"
```

or add that same line to the client project's `requirements.txt`.

### Pinning a specific version

Once this repo has tagged releases, pin to one by adding `@<ref>` (a
tag, branch, or commit SHA) right before the `#subdirectory=...`
fragment, e.g.:

```
pip install "git+https://github.com/d-s-t/ansys-analyze-worker.git@v0.1.0#subdirectory=worker"
pip install "git+https://github.com/d-s-t/ansys-analyze-worker.git@v0.1.0#subdirectory=common"
```

Omitting `@<ref>` (as in the commands above) tracks the default branch,
which is convenient while this is still moving fast but means `pip
install --upgrade` can pick up breaking changes -- pin once things
stabilize. To cut a release to pin to: `git tag v0.1.0 && git push --tags`.
If you pin `worker`'s version this way, also update the
`ansys-analyze-common` dependency URL in `worker/pyproject.toml` to the
same `@<ref>` so the two stay in lockstep.

## Run

On the machine with the full AEDT license, with AEDT already open:

```
ansys-analyze-worker
```

(equivalently: `python -m ansys_analyze_worker.run_service`)

This attaches to the running AEDT session, watches
`$ANSYS_ANALYZE_QUEUE_PATH/pending` for new task files, and shows a tray
icon (bottom-right of the Windows taskbar) with:

- **Status** -- what's currently running (if anything).
- **Stop current run** -- hard-aborts whatever task is running right now
  (the aborted task gets re-queued automatically and picked back up,
  unless you Release first). Only enabled while a task is running.
- **Release / Resume** -- Release stops picking up new tasks and, once
  the current task (if any) finishes, detaches from AEDT without closing
  it; Resume reconnects and starts scanning again.
- **Open queue folder** / **Open log file**
- **Reset (reload code)** -- restarts the whole service so any edits
  made to files in this repo since it started take effect. Doesn't wait
  for an in-flight task to finish; see `WorkerSupervisor.request_restart()`'s
  docstring in `worker/ansys_analyze_worker/supervisor.py` for exactly
  what happens to one if it's mid-analysis when you click this.
- **Exit**

The actual scanning/analyzing work runs in its own child process, not on
the tray's process -- see `docs/ARCHITECTURE.md` section 10.1 for why
(short version: blocking AEDT calls used to freeze the tray solid).

Run with `--no-tray` for a plain console service instead (useful for
debugging, or running under a service manager that doesn't want a GUI) --
that mode runs everything in a single process/thread, since there's no
tray to keep responsive.

## Extending

To support a new solution type or design, add a module to
`worker/ansys_analyze_worker/handlers/` and register it in
`worker/ansys_analyze_worker/handlers/__init__.py` -- see
[`docs/ARCHITECTURE.md`](docs/ARCHITECTURE.md) section 6. Nothing else in
the worker needs to change.

## Depending on this from a client pipeline

See [`docs/ARCHITECTURE.md`](docs/ARCHITECTURE.md) section 8 for the
full guide to authoring a client pipeline. In short, in the other
project's `requirements.txt`:

```
git+https://github.com/d-s-t/ansys-analyze-worker.git#subdirectory=common
```

and then:

```python
from ansys_analyze_common import queue_common
```
