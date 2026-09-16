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

**A `None` for `tasks`/`gpus` alone is a different, narrower thing --
it does NOT mean "keep AEDT's active setting" once `cores` is set (the
default).** `set_custom_hpc_options()` always starts from a *fresh copy*
of PyAEDT's bundled template, never from whatever config is currently
active, and only overwrites the fields it was actually given; a field
passed as `None` is simply skipped, so it keeps the *template's* value
(`NumEngines=1`, `NumGPUs=0`), not the dialog's. For `tasks` this is
usually moot -- the template ships `UseAutoSettings=true`, so AEDT
distributes tasks itself regardless of `NumEngines`. For `gpus` it is
NOT moot: leaving `ANSYS_ANALYZE_GPUS` unset means every solve runs with
**zero GPUs**, same as it always did (a bare `setup.analyze()` passed
`gpus=0` too), but now there is finally a way to ask for GPUs by setting
`ANSYS_ANALYZE_GPUS` explicitly.

**The reverse combination is unsupported by PyAEDT and worth calling out
explicitly: `ANSYS_ANALYZE_CORES=aedt` together with a real
`ANSYS_ANALYZE_TASKS`/`ANSYS_ANALYZE_GPUS` value cannot mean "leave
cores alone, just add GPUs".** A truthy `tasks`/`gpus` still makes
`analyze_setup()` call `set_custom_hpc_options()` regardless of what
`cores` is, and that rebuild starts from the fresh template every time
-- so `cores=None` in that combination doesn't preserve the dialog's
real core count, it silently resets cores to the template's own
`NumCores=4`. There is no PyAEDT API this module could call to read the
dialog's actual core count and pass it through instead. Rather than let
that happen quietly (exactly the kind of silent under-utilization this
module exists to prevent), `resolve_hpc_options()` detects the
combination and overrides `cores` back to its normal default (every
logical processor) with a warning -- see below. If cores genuinely must
be left at whatever the dialog has configured, GPUs/tasks can't be
requested through this worker in the same solve; set them by hand in
AEDT's own "HPC and Analysis Options" dialog instead.

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

# Non-numeric spellings (case-insensitive) that mean "pass None, i.e.
# don't let PyAEDT write a pyaedt_config at all -- solve with AEDT's own
# active HPC configuration". Any numeric spelling of zero ("0", "00",
# "+0", "-0", ...) means the same thing -- see _means_use_aedt_settings()
# below, which is what actually decides this everywhere it matters.
#
# This makes "0" ambiguous for TASKS/GPUS specifically, in a way that's
# worth being explicit about rather than "fixing": PyAEDT's own
# analyze_setup() decides whether to touch the HPC config at all with a
# plain Python truthiness check, `gpus or tasks or cores` -- so an
# explicit `gpus=0` and an unset `gpus=None` are *already*
# indistinguishable to PyAEDT itself, before this module's own sentinel
# handling ever enters into it. There is no way through this worker to
# force GPUs/tasks down to zero while leaving `cores` at `aedt` (PyAEDT
# would simply skip the whole HPC override for that solve either way);
# doing that requires changing AEDT's own dialog configuration directly.
USE_AEDT_SETTINGS_VALUES = frozenset({"aedt", "auto", "default", "0"})


def _means_use_aedt_settings(raw: str) -> bool:
    """
    True if the (already-stripped) raw string asks to leave AEDT's own
    HPC settings alone -- one of the canonical spellings above, or any
    numeric spelling of zero. Split out from `_resolve_env_option()` so
    `resolve_hpc_options()`'s coupling-guard warning (below) can ask the
    exact same question when deciding how to describe why `cores` ended
    up `None`, instead of re-deriving its own, slightly different
    judgment call and risking the two disagreeing about what a given
    value meant.
    """
    if raw.lower() in USE_AEDT_SETTINGS_VALUES:
        return True
    try:
        return int(raw) == 0
    except ValueError:
        return False


def _resolve_env_option(env_var: str, default: Optional[int] = None, raw: Optional[str] = None) -> Optional[int]:
    """
    Read one integer HPC option from the environment.

    Returns `default` when the variable is unset/empty or holds
    something unusable, and `None` when it explicitly asks for AEDT's own
    settings. A bad value is a warning, never an exception: a typo in an
    environment variable must not take the whole worker down mid-queue.

    `raw` lets a caller that already read+stripped `os.environ[env_var]`
    for its own purposes (`resolve_hpc_options()`'s coupling-guard
    warning, which needs to describe *why* a value came out the way it
    did) hand that same string in, rather than have this function read
    it again independently -- two readers of the same variable is
    exactly how this module ended up with the same misattribution bug
    fixed in one branch and left standing in a sibling one, twice.
    Passing `None` (the default) reads it here as usual.
    """
    if raw is None:
        raw = os.environ.get(env_var, "").strip()
    if not raw:
        return default
    if _means_use_aedt_settings(raw):
        return None

    try:
        value = int(raw)
    except ValueError:
        logger.warning("%s=%r is not an integer -- ignoring it and using %r instead.", env_var, raw, default)
        return default
    if value < 0:
        logger.warning("%s=%r is negative -- ignoring it and using %r instead.", env_var, raw, default)
        return default
    # value == 0 is impossible here: _means_use_aedt_settings() already
    # caught every spelling that parses to zero and returned None above.
    return value


def resolve_hpc_options() -> Dict[str, Optional[int]]:
    """
    The `cores`/`tasks`/`gpus` keyword arguments to hand `setup.analyze()`.

    Resolved fresh per task rather than once at startup, so a change
    already reflected in THIS process's `os.environ` takes effect on the
    very next task with no restart at all -- a value fixed at import
    time can never go stale. That is a narrower guarantee than it
    sounds, though: editing the underlying OS-level environment variable
    (Windows System Properties, `setx`, a shell profile) while the
    worker is already running is a different story. The tray's "Reset"
    (§10 of the architecture doc) re-execs via `os.execv()`, which has
    no `env` argument and so inherits THIS process's existing
    environment as-is -- it does NOT re-read the OS's environment
    variable store, only re-import this repo's own modules from disk.
    Reset alone will NOT pick up an env var edited after the worker
    started; only fully exiting the tray app (Exit) and relaunching it
    from a shell/session/Startup entry that already has the new value
    will.

    | Variable | Default | Meaning |
    |---|---|---|
    | `ANSYS_ANALYZE_CORES` | every logical processor | Cores for the solve. `aedt` (or `auto`/`default`/`0`) leaves AEDT's own HPC configuration untouched -- but only if `tasks`/`gpus` below are ALSO left unset; see the module docstring's note on the unsupported combination. |
    | `ANSYS_ANALYZE_TASKS` | PyAEDT's template default (`1`, but auto-distributed anyway) | Solve tasks/engines (`NumEngines`). Rarely worth setting: the ACF template ships `UseAutoSettings=true`, so AEDT distributes tasks itself regardless -- that's the `(Auto)` in the dialog. |
    | `ANSYS_ANALYZE_GPUS` | PyAEDT's template default (`0`, i.e. no GPU acceleration) | GPUs to use. Unlike `tasks`, there is no auto-distribution fallback for this one -- set it explicitly on any machine that should solve with GPU acceleration, since leaving it unset means zero GPUs whenever `cores` is also being overridden (the default) rather than inheriting whatever AEDT's dialog has configured. Only `ANSYS_ANALYZE_CORES=aedt` with `tasks` *also* left unset skips this entirely and leaves the dialog's own GPU setting untouched (see the module docstring above). |
    """
    # os.cpu_count() can return None (no way to determine it) -- tracked
    # separately from `cores` below, since once tasks/gpus is also in
    # play, "cores ended up None" and "os.cpu_count() ended up None" need
    # to be told apart to log something that's actually true (see below).
    cpu_count = os.cpu_count()
    # Read once and handed to _resolve_env_option() below, rather than
    # having it read this same variable again independently -- two
    # readers of one variable is exactly how this module ended up with
    # the same misattribution bug fixed in one branch and left standing
    # in a sibling one, twice (see git log). One read, one source of
    # truth for "what the operator actually typed".
    raw_cores = os.environ.get(CORES_ENV_VAR, "").strip()
    cores = _resolve_env_option(CORES_ENV_VAR, default=cpu_count, raw=raw_cores)
    tasks = _resolve_env_option(TASKS_ENV_VAR)
    gpus = _resolve_env_option(GPUS_ENV_VAR)

    if cores is None:
        # Computed once and shared by both warning branches below,
        # rather than each re-deriving its own judgment of why `cores`
        # ended up `None`.
        cores_is_deliberate_aedt = bool(raw_cores) and _means_use_aedt_settings(raw_cores)
        if not raw_cores:
            cores_reason = f"{CORES_ENV_VAR} is unset and os.cpu_count() couldn't determine one"
        elif cores_is_deliberate_aedt:
            cores_reason = f"{CORES_ENV_VAR}={raw_cores!r} asked to leave it alone"
        else:
            # cores is None here despite the variable being set to
            # something that doesn't mean "use AEDT settings" --
            # _resolve_env_option() already warned about *that* (an
            # unparseable/negative value) and fell back to
            # `default=cpu_count`, which then turned out to be None too.
            # Different cause from "unset"; say so, or an operator fixing
            # a typo'd value would be sent looking for a variable that
            # was never missing in the first place.
            cores_reason = f"{CORES_ENV_VAR}={raw_cores!r} was invalid and os.cpu_count() couldn't determine one either"

    if cores is None and not (tasks or gpus) and cpu_count is None and not cores_is_deliberate_aedt:
        # Nothing forced a pyaedt_config rebuild here (tasks/gpus are
        # both None too), so this isn't the silent-regression case the
        # branch below exists for -- passing cores=None with tasks/gpus
        # also None makes PyAEDT skip pyaedt_config entirely and solve
        # with whatever's already active in AEDT's dialog (see the
        # module docstring), which is a perfectly safe fallback. It's
        # just not the *documented* "every logical processor" default,
        # since that default couldn't be computed -- worth one line in
        # the log so that divergence isn't a total surprise. Excluded
        # when `cores_is_deliberate_aedt`: there, using the dialog's own
        # settings IS the request, not an accidental fallback, so
        # warning about it (and telling the operator to set
        # ANSYS_ANALYZE_CORES to undo their own explicit choice) would
        # be actively unhelpful noise.
        logger.warning(
            "cores resolved to None (%s) -- this solve will use whatever HPC "
            "configuration is already active in AEDT's dialog instead of the usual "
            "'every logical processor' default. Set %s to an explicit number to avoid "
            "this.",
            cores_reason,
            CORES_ENV_VAR,
        )

    if cores is None and (tasks or gpus):
        # "cores=aedt" (leave the dialog's core count alone), OR cores
        # simply defaulting to an undetectable os.cpu_count(), can't
        # actually be honored once tasks/gpus asks PyAEDT to rebuild the
        # HPC config anyway -- see the module docstring. Left as None,
        # cores would silently fall back to the *template's* NumCores=4
        # instead of the dialog's real value, which is exactly the kind
        # of silent under-utilization this module exists to prevent.
        overriding_vars = " and ".join(
            var for var, value in ((TASKS_ENV_VAR, tasks), (GPUS_ENV_VAR, gpus)) if value
        )
        if cpu_count is not None:
            # There IS a concrete core count available -- use it instead
            # of letting cores silently fall back to the template's 4,
            # same as the ordinary "unset" default would.
            logger.warning(
                "cores resolved to None (%s), but %s is also set -- PyAEDT can't leave "
                "cores untouched while overriding tasks/gpus (either one forces a full "
                "HPC config rebuild from PyAEDT's own template, which would silently "
                "reset cores to that template's default of 4 instead). Falling back "
                "cores to every logical processor (%d) instead.",
                cores_reason,
                overriding_vars,
                cpu_count,
            )
            cores = cpu_count
        else:
            # No concrete core count to fall back to either -- there's
            # nothing left to do but say plainly what PyAEDT will
            # actually use, rather than claim a fix that didn't happen.
            logger.warning(
                "cores resolved to None (%s), and the logical processor count "
                "couldn't be determined either, while %s is also set -- PyAEDT will "
                "rebuild the HPC config from its own bundled template, so this solve "
                "will silently run on that template's default of 4 cores, not the "
                "dialog's real value. Set %s to an explicit number to avoid this.",
                cores_reason,
                overriding_vars,
                CORES_ENV_VAR,
            )

    return {"cores": cores, "tasks": tasks, "gpus": gpus}


def describe_hpc_options(options: Dict[str, Optional[int]]) -> str:
    """One short human-readable line for the log, e.g. `16 core(s), 2 GPU(s)`."""
    described = [
        f"{options[key]} {unit}"
        for key, unit in (("cores", "core(s)"), ("tasks", "task(s)"), ("gpus", "GPU(s)"))
        if options.get(key)
    ]
    return ", ".join(described) if described else "AEDT's own HPC settings (nothing overridden)"
