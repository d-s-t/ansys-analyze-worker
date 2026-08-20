"""
Handler for task_type == "eigenmode_analyze".

The client pipeline builds the model AND creates the single Eigenmode
setup (student license covers `create_setup`, just not the compute-heavy
`analyze`/`analyze_setup` call -- see docs/ARCHITECTURE.md section 8).
This handler's whole job is therefore: open the project, find the setup
the client already created, run it, and confirm it solved. It does not
create any setup itself, does not chain multiple setups, and does not
extract mode data or run field plots -- the client pipeline reopens the
analyzed project afterward (student version) and pulls everything it
needs straight out of the solved project. See ARCHITECTURE.md sections 6
and 7 for why that split exists.
"""
from __future__ import annotations

from typing import Any, Dict


def run(hfss, task: Dict[str, Any], log) -> Dict[str, Any]:
    setup_names = list(getattr(hfss, "setup_names", []) or [])
    if not setup_names:
        raise RuntimeError(
            "Project has no analysis setup. The client pipeline must create the "
            "setup before queuing the task -- this worker only ever runs an "
            "existing one, it never creates one itself."
        )
    if len(setup_names) > 1:
        raise RuntimeError(
            f"Expected exactly one setup, found {setup_names}. One setup per "
            "project is the whole point of this handler -- if the project needs "
            "more than one, delete the extras before queuing."
        )

    setup_name = setup_names[0]
    setup = hfss.get_setup(setup_name)

    log(f"Analyzing {setup_name}...")
    success = setup.analyze() or setup.is_solved
    if not success:
        raise RuntimeError(f"Simulation {setup_name} failed. Check the HFSS message manager.")

    hfss.save_project(task["project_file"])

    return {
        "setup_name": setup_name,
        "solved": True,
    }
