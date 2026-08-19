"""
ansys_analyze_common
======================

The shared, dependency-free (stdlib only) task/result queue schema for
the ansys-analyze-worker pipeline -- see `queue_common` for the actual
implementation. A client pipeline (the project that builds AEDT models,
queues them for analysis, monitors progress, and plots results) depends
on this package -- NOT on `ansys-analyze-worker` itself, which pulls in
AEDT/plotting/tray-icon libraries it never needs -- to talk to the
worker's task queue.

See docs/ARCHITECTURE.md in the `ansys-analyze-worker` repo for the full
design doc, task/result JSON schema, and a guide to writing a client
pipeline.

Typical usage from a separate, dependent project (after
`pip install git+https://github.com/d-s-t/ansys-analyze-worker.git#subdirectory=common`):

    from ansys_analyze_common import queue_common

    queue = queue_common.QueuePaths.from_env()
    task = queue_common.build_task(...)
    queue_common.write_task_atomically(queue, task)
"""
from __future__ import annotations

__version__ = "0.1.0"
