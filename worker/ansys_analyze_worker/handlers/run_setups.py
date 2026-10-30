"""
Handler for task_type == "run_setups" (and "eigenmode_analyze", its
legacy alias -- see handlers/__init__.py).

This handler is **solution-type agnostic**. Eigenmode, DrivenModal,
DrivenTerminal -- it opens the project and runs whatever setup(s) the
client already created, whatever kind they are. Nothing below knows what
an eigenmode is. For a driven setup, `setup.analyze()` reaches
`oDesign.Analyze(<setup>)`, which solves the adaptive passes *and* the
frequency sweeps defined under that setup, so an S-parameter run needs
no special casing here.

The client pipeline builds the model AND creates its setup(s) (student
license covers `create_setup`, just not the compute-heavy
`analyze`/`analyze_setup` call -- see docs/ARCHITECTURE.md section 8).
This handler's whole job is therefore: open the project and run whatever
setup(s) the client already created. It never creates a setup itself,
never adds a frequency sweep to one, never creates ports, does not chain
new setups searching for a target frequency, and does not extract mode
data, S-parameters, or run field plots -- the client pipeline reopens the
analyzed project afterward (student version) and pulls everything it
needs straight out of the solved project. See ARCHITECTURE.md sections 6,
7, 8 and 12 for why that split exists. (The one thing the client cannot
read back itself -- field values of a mesh over the Student size limit --
is exported by `field_exports.py` after this handler returns, when the
task asks for it; it is not this handler's business.)

A setup that fails to solve does NOT abort the task or block the other
setups -- it's just recorded as `{"success": False}` for that setup name,
and the task still files into `done/`. Whether a particular setup
actually solved is the caller's concern, not this handler's: the client
pipeline's post-processing step already surfaces it clearly when it
tries to read solution data off an unsolved setup (see
ARCHITECTURE.md section 5/13). Only a structural problem -- no setup at
all -- is worth failing the whole task over.

Caveat on `success`: PyAEDT 1.0.0 declares `setup.analyze()` as
`-> None`, so `setup.analyze() or setup.is_solved` is in practice just
`setup.is_solved` -- and `is_solved` queries the *adaptive* solution
(`<setup> : LastAdaptive`), not the frequency sweep. A driven setup whose
adaptive passes converged but whose sweep failed is therefore still
reported `success: true` and still files into `done/`. That is
deliberate: the worker reports what it ran, and the client's
post-processing step is what surfaces missing solution data
(ARCHITECTURE.md section 5/13). Verifying sweeps here would drag
solution-type-specific knowledge back into the worker, which is exactly
what section 6 of the architecture exists to prevent.

`parameters.analyze: false` skips the solve (and the save): the same
workflow, minus `analyze()`. For an Eigenmode project `analyze()` re-solves
from scratch even when a solution is saved (17 to 54 minutes in testing,
where a solved driven setup is skipped in seconds), so a client that only
wants `field_exports` from a project that is already solved sets it. The
project is then closed without saving. See `_report_without_solving`.

How many cores each solve gets is NOT decided here -- see hpc.py, which
also explains why `setup.analyze()` must never be called bare (its
`cores` argument defaults to 1, and PyAEDT quietly overrides AEDT's
"HPC and Analysis Options" with that). Default: every logical processor
on the worker machine, overridable with `ANSYS_ANALYZE_CORES`.
"""
from __future__ import annotations

import logging
from typing import Any, Dict, Optional

from ..hpc import describe_hpc_options, resolve_hpc_options

logger = logging.getLogger("ansys_analyze_worker")


def _analyze_requested(task: Dict[str, Any]) -> bool:
    """`parameters.analyze`, default True. Only a real `False` turns the solve off (queue_common checks the type)."""
    return (task.get("parameters") or {}).get("analyze", True) is not False


def _is_read_only(task: Dict[str, Any]) -> bool:
    """worker.py closes the project without saving when this is True: a task that solved nothing changed nothing."""
    return not _analyze_requested(task)


def _looks_solved(hfss, setup_name: str) -> Optional[bool]:
    """PyAEDT's `Setup.is_solved`, or None when it can't be asked (no such setup, or it raised)."""
    try:
        setup = hfss.get_setup(setup_name)
        if not setup:
            return None
        return bool(setup.is_solved)
    except Exception:
        return None


def run(hfss, task: Dict[str, Any], log) -> Dict[str, Any]:
    setup_names = list(getattr(hfss, "setup_names", []) or [])
    if not setup_names:
        raise RuntimeError(
            "Project has no analysis setup. The client pipeline must create the "
            "setup(s) before queuing the task -- this worker only ever runs "
            "existing ones, it never creates one itself."
        )

    if not _analyze_requested(task):
        return _report_without_solving(hfss, setup_names, log)

    # Never call setup.analyze() bare: its cores/tasks/gpus arguments
    # default to 1/1/0, and a truthy one makes PyAEDT override AEDT's
    # "HPC and Analysis Options" with a generated `pyaedt_config` -- so
    # the bare call pins every solve to a single core. See hpc.py.
    hpc_options = resolve_hpc_options()

    result: Dict[str, Any] = {}
    for setup_name in setup_names:
        setup = hfss.get_setup(setup_name)
        log(f"Analyzing {setup_name} with {describe_hpc_options(hpc_options)}...")
        result[setup_name] = {"success": setup.analyze(**hpc_options) or setup.is_solved}

    hfss.save_project(task["project_file"])

    return result


# worker.py reads this to decide whether to close the project without saving it.
run.read_only = _is_read_only


def _report_without_solving(hfss, setup_names, log) -> Dict[str, Any]:
    """
    `parameters.analyze == false`: skip the solve and the save, and report
    per setup whether it looks solved. The task's `field_exports` still run
    afterwards, against what is already solved.

    `success` here means "PyAEDT's `Setup.is_solved` says there is a
    solution" (None if it couldn't be asked), nothing more -- no setup was
    run. A False is logged but never stops the task: `is_solved` is a
    report-based probe that has been seen to say False for an eigenmode setup
    AEDT's own log reported as solved, so refusing on it could block a valid
    export.
    """
    log("parameters.analyze is false: not solving, using what is already solved.")
    result: Dict[str, Any] = {}
    for setup_name in setup_names:
        solved = _looks_solved(hfss, setup_name)
        if solved is False:
            logger.warning(
                "Setup %s reports no solution and analyze is false, so nothing will solve it; "
                "exports against it will probably fail.", setup_name)
        result[setup_name] = {"success": solved, "analyzed": False}
    return result
