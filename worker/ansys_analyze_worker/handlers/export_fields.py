"""
Handler for task_type == "export_fields": open the project, run NO analysis,
and let the task's `field_exports` (see field_exports.py) be evaluated
against what is already solved in it.

**Why it exists.** `run_setups` always calls `analyze()`, and for an
Eigenmode project that re-solves from scratch even when a solution is
already saved (a solved driven setup is skipped in seconds, an eigenmode one
took 17 to 54 minutes in testing). When the client only wants field values
out of a project that is already solved, that wait is pure cost; this
workflow skips it. It is a different *workflow*, not a different solution
type, so it is its own `task_type` (docs/ARCHITECTURE.md section 6) rather
than a flag on `run_setups`.

The handler does no export itself -- the worker runs `field_exports` after
every handler -- so all it does is check the task makes sense and report
which of the setups the exports refer to look solved.

**It does not refuse a setup that looks unsolved.** `Setup.is_solved` is
PyAEDT's report-based probe, and it has been seen to say False for an
eigenmode setup AEDT's own log reported as solved. Refusing on it could
block a valid export, so an unsolved-looking setup is only logged and
recorded (`solved: false`); if the exports then fail, that is what they
report, with the failure details field_exports records.

**Read-only.** The project is closed without saving (`run.read_only`, read by
worker.py), so the saved project the client already has is not rewritten
with a new window layout and result ids for no reason.
"""
from __future__ import annotations

import logging
from typing import Any, Dict, Optional

logger = logging.getLogger("ansys_analyze_worker")

_SOLUTION_SEPARATOR = " : "


def run(hfss, task: Dict[str, Any], log) -> Dict[str, Any]:
    exports = task.get("field_exports") or []
    if not exports:
        raise RuntimeError(
            "An export_fields task needs a non-empty 'field_exports' list: it never solves anything, "
            "so without exports there is nothing for it to do."
        )

    setups: Dict[str, Dict[str, Optional[bool]]] = {}
    for name in sorted({str(entry["solution"]).split(_SOLUTION_SEPARATOR)[0].strip() for entry in exports}):
        solved = _looks_solved(hfss, name)
        setups[name] = {"solved": solved}
        if solved is False:
            logger.warning(
                "Setup %s reports no solution. export_fields never solves, so exports against it will "
                "probably fail; if it was solved, run_setups (which solves) is the other option.", name)
        else:
            log(f"Setup {name}: {'solved' if solved else 'solved state unknown'}; exporting without solving.")
    return {"analyzed": False, "setups": setups}


# worker.py closes the project without saving after a read-only handler.
run.read_only = True


def _looks_solved(hfss, setup_name: str) -> Optional[bool]:
    """PyAEDT's `Setup.is_solved`, or None when it can't be asked (no such setup, or it raised)."""
    try:
        setup = hfss.get_setup(setup_name)
        if not setup:
            return None
        return bool(setup.is_solved)
    except Exception:
        return None
