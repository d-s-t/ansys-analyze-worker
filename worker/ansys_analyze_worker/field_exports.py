"""
The one post-solve step the worker does for the client: evaluate requested
field expressions at requested sample points and write the raw numbers into
the task's output folder.

**Why the worker does this at all.** The Student version can post-process
any solved project's *solution data* (S-parameters, eigenmode frequencies
and Q, Touchstone), but it refuses to load a *field* solution once the
solved mesh is past its size limit -- the fields calculator, `ExportOnGrid`
and field plots all go through that check. Package models of about 250-300 k
elements are over it, so the client cannot read any field out of them.
Everything else stays client-side; see docs/ARCHITECTURE.md sections 7 and
12, and https://github.com/d-s-t/ansys-analyze-worker/issues/10.

**What it is not.** No plotting, no statistics, no interpretation, no
setup creation. The client creates the setup (with the frequency or mode it
wants), reads the `.fld` files back, and does all the analysis. The worker
stays solution-type agnostic: `solution` and `intrinsics` are passed to AEDT
as given (`Freq`/`Phase` for a driven run; see below for eigenmode), and
`expression` is a calculator recipe rather than a list of known quantities.

**Eigenmode is not verified.** The issue proposed `Mode`/`Phase` intrinsics
for it, but on real AEDT every export with `Mode` failed ("Script macro
error"), while the identical call works on a driven design. PyAEDT 1.0.0
lists only `Phase` as an intrinsic of an Eigenmode design, so `Mode` is
probably not accepted; how AEDT selects a mode for a field export has not
been established. A failed export therefore records what was sent
(`export_on_grid_args`) and what the design declares (`design`: its
solutions and intrinsics), to be compared with a script recorded from a
manual export in AEDT.

**Failure policy.** An export that fails is recorded as
`{"success": False, "error": ...}` and neither fails the task nor skips the
other exports -- the same rule `run_setups` applies to a setup that fails to
solve. Whether the numbers are usable is the client's concern.

**Sources.** An entry's optional `sources` is applied with `edit_sources`
just before that entry is evaluated, and stays in effect for the entries
after it (an entry without `sources` sees whatever the previous one left).
Because that changes the project, the worker closes it without saving when
any entry used `sources` -- see `edits_sources()` -- so the project the
client gets back is the one `run_setups` saved.

The entries are validated when the task is built and again when the worker
loads it (`queue_common.validate_task`), so this module trusts their shape.
"""
from __future__ import annotations

import logging
import os
from typing import Any, Dict, List

from ansys_analyze_common.queue_common import FIELD_EXPORT_DEFAULT_UNITS

logger = logging.getLogger("ansys_analyze_worker")

# Sub-folder of the task's output_dir the .fld files go into.
FIELDS_SUBDIR = "fields"

# AEDT message-manager severity for errors (PyAEDT uses 2 the same way).
_AEDT_ERROR_SEVERITY = 2


def edits_sources(task: Dict[str, Any]) -> bool:
    """True if running this task's field exports changes the project (see module docstring)."""
    return any(entry.get("sources") for entry in task.get("field_exports") or [])


def run(hfss, task: Dict[str, Any], log) -> Dict[str, Any]:
    """
    Run every entry of `task["field_exports"]` against the open, saved
    project. Returns `{name: {"success": True, "file": ..., "points": ...}}`
    or `{name: {"success": False, "error": ...}}` per entry; `{}` when the
    task asks for none, which is what keeps existing clients unaffected.
    """
    entries: List[Dict[str, Any]] = task.get("field_exports") or []
    results: Dict[str, Any] = {}
    for entry in entries:
        name = entry["name"]
        log(f"Exporting field {name} ({entry['solution']})...")
        messages_before = _aedt_error_messages(hfss)
        try:
            results[name] = _export_one(hfss, entry, task["output_dir"])
        except Exception as exc:
            logger.warning("Field export %s failed: %s", name, exc, exc_info=True)
            design = _design_description(hfss)
            error = f"{type(exc).__name__}: {exc}"
            hints = _failure_hints(entry, design)
            if hints:
                error += " (" + "; ".join(hints) + ")"
            failure: Dict[str, Any] = {"success": False, "error": error}
            new_messages = _aedt_error_messages(hfss)[len(messages_before):]
            if new_messages:
                failure["aedt_messages"] = new_messages
            # AEDT's own error is just "Failed to execute gRPC AEDT command: ExportOnGrid", so say
            # exactly what was sent and what the design declares -- the two things to compare.
            failure["export_on_grid_args"] = _export_on_grid_args(entry, _relative_file(name))
            if design:
                failure["design"] = design
            logger.warning("Field export %s: ExportOnGrid arguments %s; design %s", name,
                           failure["export_on_grid_args"], design or "unreadable")
            results[name] = failure
    return results


def _export_one(hfss, entry: Dict[str, Any], output_dir: str) -> Dict[str, Any]:
    name = entry["name"]
    fields_dir = os.path.join(output_dir, FIELDS_SUBDIR)
    os.makedirs(fields_dir, exist_ok=True)
    out_file = os.path.join(fields_dir, f"{name}.fld")
    if os.path.exists(out_file):
        # A retried task: never mistake a previous run's file for this one's output.
        os.remove(out_file)

    sources = entry.get("sources")
    if sources:
        if hfss.edit_sources(dict(sources)) is False:
            raise RuntimeError("edit_sources failed")

    # Call the fields reporter directly rather than through
    # hfss.post.export_field_file_on_grid: in PyAEDT 1.0.0 that helper's
    # intrinsics handling raises (AttributeError on HfssConstants.default_solution)
    # and it only accepts design variables in `variations`, not Freq/Mode.
    reporter = hfss.ofieldsreporter
    reporter.CalcStack("clear")
    quantity, *operations = entry["expression"]
    try:
        reporter.EnterQty(quantity)
    except Exception:
        # Not a basic quantity: a named expression from the calculator (e.g. Mag_E).
        try:
            reporter.CopyNamedExprToStack(quantity)
        except Exception as exc:
            raise RuntimeError(
                f"{quantity!r} is neither a field quantity nor a named calculator expression: {exc}"
            ) from exc
    for operation in operations:
        try:
            reporter.CalcOp(operation)
        except Exception as exc:
            # AEDT's own message is just "Failed to execute gRPC AEDT command: CalcOp".
            raise RuntimeError(f"calculator operation {operation!r} failed: {exc}") from exc

    reporter.ExportOnGrid(*_export_on_grid_args(entry, out_file))

    # AEDT does not always raise when it refuses (the Student mesh limit only
    # shows up as a message), so the file itself is the evidence.
    if not os.path.exists(out_file):
        raise RuntimeError("AEDT reported no error but wrote no file")
    points = _count_points(out_file)
    if points == 0:
        raise RuntimeError("the exported file has no data rows")
    return {"success": True, "file": _relative_file(name), "points": points}


def _relative_file(name: str) -> str:
    return f"{FIELDS_SUBDIR}/{name}.fld"


def _export_on_grid_args(entry: Dict[str, Any], out_file: str) -> list:
    """The ten positional arguments of `ofieldsreporter.ExportOnGrid` for this entry."""
    grid = entry["grid"]
    units = grid.get("units", FIELD_EXPORT_DEFAULT_UNITS)
    intrinsics: List[Any] = []
    for key, value in (entry.get("intrinsics") or {}).items():
        intrinsics.extend([f"{key}:=", str(value)])
    return [
        out_file,
        _with_units(grid["start"], units),
        _with_units(grid["stop"], units),
        _with_units(grid["step"], units),
        entry["solution"],
        intrinsics,
        ["NAME:ExportOption", "IncludePtInOutput:=", True, "RefCSName:=", "Global", "PtInSI:=", True,
         "FieldInRefCS:=", True],
        grid.get("type", "Cartesian"),
        [f"0{units}"] * 3,
        False,
    ]


def _design_description(hfss) -> Dict[str, Any]:
    """
    What the open design declares, for a failed export to be compared with
    what was asked for: `solutions` (PyAEDT's `existing_analysis_sweeps`,
    "<setup> : <sweep>") and `intrinsics` (names PyAEDT lists for the
    design's solution type -- Freq/Phase for a driven design, only Phase for
    an eigenmode one). Each key is present only if it could be read.
    """
    description: Dict[str, Any] = {}
    try:
        solutions = list(hfss.existing_analysis_sweeps)
        if solutions:
            description["solutions"] = solutions
    except Exception:
        pass
    try:
        intrinsics = list(hfss.design_solutions.intrinsics)
        if intrinsics:
            description["intrinsics"] = intrinsics
    except Exception:
        pass
    return description


def _failure_hints(entry: Dict[str, Any], design: Dict[str, Any]) -> List[str]:
    """
    Plain-language reasons for a failed export that the entry itself gives
    away, drawn from `_design_description`: a `solution` the design doesn't
    have, and intrinsics the design's solution type doesn't declare. Only
    used after a failure, never to refuse an export up front, so a valid
    name that PyAEDT's lists happen to omit can't be blocked.
    """
    hints = []
    solutions = design.get("solutions")
    if solutions and entry["solution"] not in solutions:
        hints.append(f"solution {entry['solution']!r} was not found among this design's solutions; "
                     f"available: {solutions}")
    declared = design.get("intrinsics")
    unknown = [key for key in (entry.get("intrinsics") or {}) if declared and key not in declared]
    if unknown:
        hints.append(f"intrinsic(s) {unknown} are not among the intrinsics PyAEDT lists for this design, "
                     f"{declared}")
    return hints


def _with_units(values: List[float], units: str) -> List[str]:
    return [f"{value}{units}" for value in values]


def _count_points(path: str) -> int:
    """Data rows of a .fld file: every line that starts with a number (the header does not)."""
    count = 0
    with open(path, "r", errors="replace") as f:
        for line in f:
            first = line.split(None, 1)
            if not first:
                continue
            try:
                float(first[0])
            except ValueError:
                continue
            count += 1
    return count


def _aedt_error_messages(hfss) -> List[str]:
    """Best-effort snapshot of the design's AEDT error messages; [] if it cannot be read."""
    try:
        return [str(m) for m in hfss.odesktop.GetMessages(hfss.project_name, hfss.design_name, _AEDT_ERROR_SEVERITY)]
    except Exception:
        return []
