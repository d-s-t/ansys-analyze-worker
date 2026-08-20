"""
Handler for task_type == "eigenmode_analyze".

The client pipeline builds the model AND creates its Eigenmode setup(s)
(student license covers `create_setup`, just not the compute-heavy
`analyze`/`analyze_setup` call -- see docs/ARCHITECTURE.md section 8).
This handler's whole job is therefore: open the project, run whatever
setup(s) the client already created, and confirm they solved. It does
not create any setup itself, does not chain new setups searching for a
target frequency, and does not extract mode data or run field plots --
the client pipeline reopens the analyzed project afterward (student
version) and pulls everything it needs straight out of the solved
project. See ARCHITECTURE.md sections 6 and 7 for why that split exists.
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

    for setup_name in setup_names:
        setup = hfss.get_setup(setup_name)
        log(f"Analyzing {setup_name}...")
        success = setup.analyze() or setup.is_solved
        if not success:
            raise RuntimeError(f"Simulation {setup_name} failed. Check the HFSS message manager.")

    hfss.save_project(task["project_file"])

    return {
        "setup_names": setup_names,
        "solved": True,
    }
