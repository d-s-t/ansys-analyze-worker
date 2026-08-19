"""
Registry of task handlers, keyed by task_type.

To support a new solution type or design in the future:
  1. Add a new module in this folder, e.g. handlers/driven_modal_sweep.py.
  2. Implement `run(hfss, task, log) -> dict` in it (see
     eigenmode_chain.py for the reference implementation and the expected
     return shape).
  3. Register it in HANDLERS below.

worker.py never needs to change -- it only ever calls
`get_handler(task["task_type"])`. See docs/ARCHITECTURE.md section 6.
"""
from __future__ import annotations

from typing import Any, Callable, Dict

from . import eigenmode_chain

TaskHandler = Callable[..., Dict[str, Any]]

HANDLERS: Dict[str, TaskHandler] = {
    "eigenmode_chain": eigenmode_chain.run,
}


def get_handler(task_type: str) -> TaskHandler:
    try:
        return HANDLERS[task_type]
    except KeyError as exc:
        raise KeyError(
            f"No handler registered for task_type={task_type!r}. "
            f"Known task types: {sorted(HANDLERS)}"
        ) from exc
