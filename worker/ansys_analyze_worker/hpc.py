"""
How many cores (and tasks/GPUs) the worker solves with.

**Why this file exists.** PyAEDT's `Setup.analyze()` -- the single call
this whole worker exists to make -- does NOT inherit AEDT's own HPC
settings. In PyAEDT 1.0.0 its signature is:

    Setup.analyze(cores=1, tasks=1, gpus=0, ..., use_auto_settings=True)

and any truthy `cores`/`tasks`/`gpus` sends `Analysis.analyze_setup()`
into `set_custom_hpc_options()`, which copies PyAEDT's bundled
`ansys/aedt/core/misc/pyaedt_local_config.acf` template, overwrites
`NumCores` with whatever was passed, registers it under the name
**`pyaedt_config`**, and makes it the *active* configuration for the
design type for the duration of the solve. So `setup.analyze()` with no
arguments doesn't mean "use this machine's HPC settings" -- it means
"solve on exactly one core", silently overriding whatever is configured
in AEDT's "HPC and Analysis Options" dialog. That is where a
`pyaedt_config (Auto)` entry reading `localhost:(Auto):1:90%:0` (one
core, 90% RAM, no GPUs) comes from, and it is a pure PyAEDT default --
nothing in this repo ever asked for one core.

Confusingly, the `Hfss.analyze()` method one level up defaults to
`cores=None`, which leaves AEDT's settings alone; only the per-`Setup`
wrapper hard-codes 1. PyAEDT has no environment variable for this
either: `settings.num_cores` (`PYAEDT_*`/`pyaedt_settings.yaml`) only
feeds LSF/scheduler job submission, never the interactive
`oDesign.Analyze` path this worker uses.

So the worker passes `cores`/`tasks`/`gpus` **explicitly** on every
solve, defaulting `cores` to every logical processor on the machine, and
lets the environment override that per machine -- the worker machine's
core count isn't something a client pipeline (on a different machine)
can sensibly decide, and it isn't worth a code edit or a redeploy
either.

Passing an explicit `None` for all three is meaningful and supported: it
makes PyAEDT skip the `pyaedt_config` machinery entirely and solve with
whatever configuration is already active in AEDT's "HPC and Analysis
Options" dialog. That's what `ANSYS_ANALYZE_CORES=aedt` selects, for a
machine whose HPC options are already tuned by hand (or by an HPC pack
license's own configuration) and should be left alone.

A note on core counts and licensing: `os.cpu_count()` counts *logical*
processors (hyperthreading included), and AEDT's HPC licensing caps how
many cores a solve may actually use. If the message manager starts
complaining about cores or HPC licenses after this change, set
`ANSYS_ANALYZE_CORES` to what the license actually allows (or to the
physical core count) -- that's exactly what the variable is for.
"""
from __future__ import annotations

import logging
import os
from typing import Dict, Optional

logger = logging.getLogger("ansys_analyze_worker")

CORES_ENV_VAR = "ANSYS_ANALYZE_CORES"
TASKS_ENV_VAR = "ANSYS_ANALYZE_TASKS"
GPUS_ENV_VAR = "ANSYS_ANALYZE_GPUS"

# Values (case-insensitive) that mean "pass None, i.e. don't let PyAEDT
# write a pyaedt_config at all -- solve with AEDT's own active HPC
# configuration". `0` is spelled out here too since "zero cores" has no
# other sensible reading.
USE_AEDT_SETTINGS_VALUES = frozenset({"aedt", "auto", "default", "0"})


def _resolve_env_option(env_var: str, default: Optional[int] = None) -> Optional[int]:
    """
    Read one integer HPC option from the environment.

    Returns `default` when the variable is unset/empty or holds
    something unusable, and `None` when it explicitly asks for AEDT's own
    settings. A bad value is a warning, never an exception: a typo in an
    environment variable must not take the whole worker down mid-queue.
    """
    raw = os.environ.get(env_var, "").strip()
    if not raw:
        return default
    if raw.lower() in USE_AEDT_SETTINGS_VALUES:
        return None

    try:
        value = int(raw)
    except ValueError:
        logger.warning("%s=%r is not an integer -- ignoring it and using %r instead.", env_var, raw, default)
        return default
    if value < 0:
        logger.warning("%s=%r is negative -- ignoring it and using %r instead.", env_var, raw, default)
        return default
    return value or None


def resolve_hpc_options() -> Dict[str, Optional[int]]:
    """
    The `cores`/`tasks`/`gpus` keyword arguments to hand `setup.analyze()`.

    Resolved fresh per task rather than once at startup, so editing the
    variables and clicking the tray's "Reset" (which re-execs the
    service, §10 of the architecture doc) is enough to change them --
    and so a value fixed at import time can never go stale.

    | Variable | Default | Meaning |
    |---|---|---|
    | `ANSYS_ANALYZE_CORES` | every logical processor | Cores for the solve. `aedt` (or `auto`/`default`/`0`) leaves AEDT's own HPC configuration untouched. |
    | `ANSYS_ANALYZE_TASKS` | AEDT's own | Solve tasks/engines (`NumEngines`). Rarely worth setting: the ACF template ships `UseAutoSettings=true`, so AEDT distributes tasks itself -- that's the `(Auto)` in the dialog. |
    | `ANSYS_ANALYZE_GPUS` | AEDT's own | GPUs to use, for a solver and license that support GPU acceleration. |
    """
    return {
        # os.cpu_count() can return None (no way to determine it), which
        # is exactly the "let AEDT decide" value anyway -- so an
        # undetectable core count needs no special casing here.
        "cores": _resolve_env_option(CORES_ENV_VAR, default=os.cpu_count()),
        "tasks": _resolve_env_option(TASKS_ENV_VAR),
        "gpus": _resolve_env_option(GPUS_ENV_VAR),
    }


def describe_hpc_options(options: Dict[str, Optional[int]]) -> str:
    """One short human-readable line for the log, e.g. `16 core(s), 2 GPU(s)`."""
    described = [
        f"{options[key]} {unit}"
        for key, unit in (("cores", "core(s)"), ("tasks", "task(s)"), ("gpus", "GPU(s)"))
        if options.get(key)
    ]
    return ", ".join(described) if described else "AEDT's own HPC settings (nothing overridden)"
