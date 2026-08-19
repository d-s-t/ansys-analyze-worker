"""
ansys_analyze_worker
=====================

A generic background worker that watches a task queue and runs Ansys
Electronics Desktop (AEDT) analyses on it. It depends on the
`ansys-analyze-common` package (the `queue_common` task/result schema)
rather than including it, so a client pipeline project can depend on
just that lightweight package without pulling this one in.

See docs/ARCHITECTURE.md in this repo for the full design doc, task/
result JSON schema, and a guide to writing a client pipeline against
this worker.

To run the worker itself, see run_service.py's module docstring.
"""
from __future__ import annotations

__version__ = "0.1.0"
