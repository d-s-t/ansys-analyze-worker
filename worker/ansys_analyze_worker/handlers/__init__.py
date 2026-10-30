"""
Registry of task handlers, keyed by task_type.

`run_setups` already covers any solution type whose setups the client
pre-creates -- Eigenmode, DrivenModal, DrivenTerminal alike -- so a new
solution type on its own warrants NO change here. What warrants a new
handler is a genuinely new *workflow*: one that has to create state,
chain setups, or drive a non-HFSS app.

To add such a workflow in the future:
  1. Add a new module in this folder, e.g. handlers/maxwell_sweep_chain.py.
  2. Implement `run(hfss, task, log) -> dict` in it (see run_setups.py for
     the reference implementation and the expected return shape).
  3. Register it in HANDLERS below.

worker.py never needs to change -- it only ever calls
`get_handler(task["task_type"])`. See docs/ARCHITECTURE.md section 6.
"""
from __future__ import annotations

from typing import Any, Callable, Dict

from . import run_setups

TaskHandler = Callable[..., Dict[str, Any]]

HANDLERS: Dict[str, TaskHandler] = {
    # What every client should send from now on: "open the project and run
    # the setup(s) already in it", whatever solution type they are.
    "run_setups": run_setups.run,
    # Legacy alias. Eigenmode clients predating the driven-solve work send
    # this, and so do any tasks already sitting in the queue when this
    # worker is deployed. Same handler -- the name was always narrower than
    # the behaviour. Do not delete it without checking the queue is drained
    # and every client has been updated.
    "eigenmode_analyze": run_setups.run,
}


def get_handler(task_type: str) -> TaskHandler:
    try:
        return HANDLERS[task_type]
    except KeyError as exc:
        raise KeyError(
            f"No handler registered for task_type={task_type!r}. "
            f"Known task types: {sorted(HANDLERS)}"
        ) from exc
