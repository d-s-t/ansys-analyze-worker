"""
ansys_analyze_worker
=====================

A generic background worker that watches a task queue and runs Ansys
Electronics Desktop (AEDT) analyses on it, plus the shared task/result
schema (`queue_common`) that any "Part 1" (model-building) and "Part 3"
(results-consuming) project depends on to talk to it.

See docs/ARCHITECTURE.md in this repo for the full design doc, task/
result JSON schema, and a guide to writing new Part 1/Part 3 scripts
against this package.

Typical usage from a separate, dependent project (after
`pip install -e path/to/this/repo`, or a git/PyPI install):

    from ansys_analyze_worker import queue_common

    queue = queue_common.QueuePaths.from_env()
    task = queue_common.build_task(...)
    queue_common.write_task_atomically(queue, task)

To run the worker itself, see run_service.py's module docstring.
"""
from __future__ import annotations

__version__ = "0.1.0"
