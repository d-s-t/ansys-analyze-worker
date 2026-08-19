"""
ansys_analyze_worker
=====================

A generic background worker that watches a task queue and runs Ansys
Electronics Desktop (AEDT) analyses on it -- Part 2 of the pipeline. It
depends on the `ansys-analyze-common` package (the `queue_common` task/
result schema) rather than including it, so a Part 1/Part 3 project can
depend on just that lightweight package without pulling this one in.

See docs/ARCHITECTURE.md in this repo for the full design doc, task/
result JSON schema, and a guide to writing new Part 1/Part 3 scripts
against this pipeline.

To run the worker itself, see run_service.py's module docstring.
"""
from __future__ import annotations

__version__ = "0.1.0"
