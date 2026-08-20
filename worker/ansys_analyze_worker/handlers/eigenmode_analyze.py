"""
Handler for task_type == "eigenmode_analyze".

The client pipeline builds the model AND creates its Eigenmode setup(s)
(student license covers `create_setup`, just not the compute-heavy
`analyze`/`analyze_setup` call -- see docs/ARCHITECTURE.md section 8).
This handler's whole job is therefore: open the project and run whatever
setup(s) the client already created. It does not create any setup
itself, does not chain new setups searching for a target frequency, and
does not extract mode data or run field plots -- the client pipeline
reopens the analyzed project afterward (student version) and pulls
everything it needs straight out of the solved project. See
ARCHITECTURE.md sections 6 and 7 for why that split exists.

A setup that fails to solve does NOT abort the task or block the other
setups -- it's just recorded as `{"success": False}` for that setup name,
and the task still files into `done/`. Whether a particular setup
actually solved is the caller's concern, not this handler's: the client
pipeline's post-processing step already surfaces it clearly when it
tries to read solution data off an unsolved setup (see
ARCHITECTURE.md section 5/13). Only a structural problem -- no setup at
all -- is worth failing the whole task over.
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
