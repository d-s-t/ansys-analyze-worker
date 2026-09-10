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
7, 8 and 12 for why that split exists.

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
"""
from __future__ import annotations

from typing import Any, Dict


def run(hfss, task: Dict[str, Any], log) -> Dict[str, Any]:
    setup_names = list(getattr(hfss, "setup_names", []) or [])
    if not setup_names:
        raise RuntimeError(
            "Project has no analysis setup. The client pipeline must create the "
            "setup(s) before queuing the task -- this worker only ever runs "
            "existing ones, it never creates one itself."
        )

    result: Dict[str, Any] = {}
    for setup_name in setup_names:
        setup = hfss.get_setup(setup_name)
        log(f"Analyzing {setup_name}...")
        result[setup_name] = {"success": setup.analyze() or setup.is_solved}

    hfss.save_project(task["project_file"])

    return result
