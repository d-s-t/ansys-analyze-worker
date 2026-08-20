"""
Handler for task_type == "eigenmode_chain".

Runs a sequence of HFSS Eigenmode setups, each one picking up where the
previous left off in frequency, until modes are found at/above a target
maximum frequency (the same "sweep with growing MinimumFrequency"
strategy as the original monolithic script, made generic/data-driven
here).

Expected `task["parameters"]` keys:
    start_min_freq_ghz   float  starting MinimumFrequency for setup 1
    target_max_freq_ghz  float  stop once modes reach/exceed this
    modes                int    NumModes per setup
    max_passes           int
    min_passes           int
    min_converged        int
    max_delta_f          float  MaximumDeltaFreqPerPass (%)
    next_setup_margin    float  fraction of the last-mode gap added when
                                 picking the next setup's MinimumFrequency
                                 (default 0.3, matches the original script)

`task["objects"]` (optional): logical name -> AEDT object name, consumed
by whichever post_processing ops the task requests.
"""
from __future__ import annotations

import os
from typing import Any, Dict, List, Optional

from ..post_processing import run_post_processing


def _get_real_values(data, expression: str) -> List[float]:
    """
    Extract the real numeric value(s) for `expression` out of a
    SolutionData object, tolerant of the PyAEDT version in use.

    Newer PyAEDT (the version this pipeline currently targets) dropped
    the old zero-argument `SolutionData.data_real()` method in favor of
    `get_expression_data(expression, formula="real")`, which also returns
    a different shape -- an (x, y) pair of arrays rather than a flat list
    (that mismatch is exactly what produced
    `AttributeError: 'SolutionData' object has no attribute 'data_real'`
    when running against a newer PyAEDT). Older PyAEDT releases still
    expose `data_real()`, optionally taking the expression name. This
    helper checks what's actually available on the object at hand so the
    handler keeps working across PyAEDT versions without needing to pin
    one exactly.
    """
    if hasattr(data, "get_expression_data"):
        _, y = data.get_expression_data(expression=expression, formula="real")
        return list(y)
    if hasattr(data, "data_real"):
        try:
            return data.data_real(expression)
        except TypeError:
            return data.data_real()
    raise AttributeError(
        f"SolutionData object has neither get_expression_data() nor data_real() "
        f"(available attributes: {[a for a in dir(data) if not a.startswith('_')]})"
    )


def _fetch_solution_data(hfss, setup, expressions: List[str], log):
    """
    Fetch ONE SolutionData object covering every expression in
    `expressions`, going through the setup object itself.

    `setup.get_solution_data()` works out its own setup_sweep_name from
    the setup, so the handler no longer has to hand-build the
    "<setup> : LastAdaptive" string. If that comes back empty anyway
    (the setup-object path picking the wrong solution for an Eigenmode
    setup is the plausible failure here, since Eigenmode has no
    frequency sweep for it to match on), fall back to the explicit
    post.get_solution_data(setup_sweep_name=...) form and say so in the
    log, so a real run tells us which path actually works.

    `expressions` must always be passed explicitly and non-empty: when
    it's None/empty, PyAEDT fills it in internally by calling
    `available_report_quantities(report_category=..., solution=...,
    context=...)` -- the filtered form of the listing call, which
    silently returns an empty list on this AEDT/PyAEDT version (see
    get_eigenmode_results' docstring). Passing the expressions we
    already listed ourselves keeps that broken path out of the picture.
    """
    if not expressions:
        return None

    data = setup.get_solution_data(expressions=expressions, report_category="Eigenmode")
    if data is not None:
        return data

    solution_name = f"{setup.name} : LastAdaptive"
    log(
        f"setup.get_solution_data() returned no data for {setup.name}; retrying via "
        f"post.get_solution_data(setup_sweep_name={solution_name!r})"
    )
    return hfss.post.get_solution_data(
        expressions=expressions, setup_sweep_name=solution_name, report_category="Eigenmode"
    )


def get_eigenmode_results(hfss, setup, log, export_csv_path: Optional[str] = None) -> List[Dict[str, Any]]:
    """
    Retrieve the resonant frequency (and Q, if available) for every mode
    of a solved Eigenmode setup.

    Eigenmode solutions do not have a frequency sweep, so `Freq` is *not*
    an intrinsic you can read back the way you would for a Driven Modal
    solution -- `intrinsics.get('Freq')` is always empty for an Eigenmode
    setup. Instead, each mode's resonant frequency is exposed as its own
    report quantity, and quality factor lives in a separate quantity
    category, "Eigen Q".

    Listing those quantities has to be done with the UNFILTERED call:
    `available_report_quantities(report_category="Eigenmode",
    solution=...)` silently returns an empty list against this
    PyAEDT/AEDT version, which is why this used to come back with
    nothing even though the modes were plainly visible in AEDT. Ansys'
    own PyAEDT eigenmode example calls it with no filters at all for the
    mode quantities, and only `quantities_category="Eigen Q"` for the Q
    quantities -- `report_category="Eigenmode"` belongs on the
    get_solution_data call that fetches values, never on the listing
    call.

    Values are then fetched in ONE call per category (all mode
    expressions together, all Q expressions together) via the setup
    object, rather than one round-trip per mode per quantity as before --
    for 20 modes that's 2 calls instead of 40, so far fewer chances for a
    flaky AEDT round-trip to take the whole task down. Everything after
    that is pulled out of the returned SolutionData in memory.

    Mode and Q values are paired POSITIONALLY (same list index = same
    mode), matching Ansys' example, rather than regex-matching a mode
    number out of the expression text -- that number isn't guaranteed to
    be there.
    """
    # A setup that isn't actually solved has no data to read back, and
    # every downstream symptom of that is confusing -- check up front and
    # say so plainly instead.
    try:
        solved = setup.is_solved
    except Exception as exc:  # pragma: no cover - depends on live AEDT
        log(f"Could not read {setup.name}.is_solved ({exc}); attempting to read results anyway.")
        solved = True
    if not solved:
        raise RuntimeError(
            f"Setup {setup.name} reports is_solved=False -- AEDT has no solution data for it, "
            "so there are no eigenmodes to read back. Check the HFSS message manager on the "
            "worker machine for why the solve produced no solution."
        )

    mode_exprs = hfss.post.available_report_quantities()
    q_exprs = hfss.post.available_report_quantities(quantities_category="Eigen Q")

    if len(mode_exprs) != len(q_exprs):
        # Pair up what we can rather than erroring out on a partial
        # mismatch -- but say so, since silently dropping quantities here
        # would otherwise look like "the solve just found fewer modes".
        pair_count = min(len(mode_exprs), len(q_exprs))
        log(
            f"{setup.name}: found {len(mode_exprs)} mode quantity/quantities but "
            f"{len(q_exprs)} 'Eigen Q' quantity/quantities; using the first {pair_count} of each."
        )
        mode_exprs = mode_exprs[:pair_count]
        q_exprs = q_exprs[:pair_count]

    if not mode_exprs:
        return []

    freq_data = _fetch_solution_data(hfss, setup, mode_exprs, log)
    if freq_data is None:
        log(f"{setup.name}: no solution data returned for mode quantities {mode_exprs}.")
        return []

    q_data = _fetch_solution_data(hfss, setup, q_exprs, log)
    if q_data is None and q_exprs:
        log(f"{setup.name}: no 'Eigen Q' data returned; reporting modes without Q values.")

    # Dump the raw solution alongside the results as an artifact. This is
    # not what the numbers below are read from (they come straight out of
    # the SolutionData object in memory) -- it's here so a run that looks
    # wrong later can be checked against exactly what AEDT handed back.
    if export_csv_path:
        try:
            freq_data.export_data_to_csv(export_csv_path)
        except Exception as exc:  # pragma: no cover - depends on live AEDT
            log(f"Could not export raw solution data to {export_csv_path}: {exc}")

    results: List[Dict[str, Any]] = []
    for i, mode_expr in enumerate(mode_exprs, start=1):
        try:
            freq_values = _get_real_values(freq_data, mode_expr)
        except Exception as exc:  # pragma: no cover - depends on live AEDT
            log(f"{setup.name}: could not read values for {mode_expr!r}: {exc}")
            continue
        if not freq_values:
            continue

        q_value = None
        if q_data is not None:
            try:
                q_values = _get_real_values(q_data, q_exprs[i - 1])
                q_value = q_values[0] if q_values else None
            except Exception as exc:  # pragma: no cover - depends on live AEDT
                log(f"{setup.name}: could not read Q for {q_exprs[i - 1]!r}: {exc}")

        freq_hz = freq_values[0]
        results.append(
            {
                "mode": i,
                "freq_hz": freq_hz,
                "freq_ghz": freq_hz / 1e9,
                "q": q_value,
            }
        )

    results.sort(key=lambda r: r["freq_hz"])
    return results


def get_convergence_data(setup):
    try:
        return setup.props.get("MaxDeltaFreq", "N/A")
    except Exception:
        return "N/A"


def run(hfss, task: Dict[str, Any], log) -> Dict[str, Any]:
    params = task.get("parameters", {})

    current_min_freq = float(params.get("start_min_freq_ghz", 1.0))
    target_max_freq = float(params.get("target_max_freq_ghz", 20.0))
    num_modes = int(params.get("modes", 20))
    max_passes = int(params.get("max_passes", 99))
    min_passes = int(params.get("min_passes", 5))
    min_converged = int(params.get("min_converged", 3))
    max_delta_f = float(params.get("max_delta_f", 1.0))
    next_setup_margin = float(params.get("next_setup_margin", 0.3))

    out_dir = task["output_dir"]
    os.makedirs(out_dir, exist_ok=True)

    # Clear any leftover setups from a previous attempt at this task so a
    # retry doesn't pile up duplicate setups in the same project.
    try:
        for existing in list(getattr(hfss, "setup_names", []) or []):
            hfss.delete_setup(existing)
    except Exception:
        pass

    modes_data: List[Dict[str, Any]] = []
    setup_idx = 1

    while current_min_freq < target_max_freq:
        setup_name = f"Setup_{setup_idx}"
        setup = hfss.create_setup(name=setup_name)
        setup.props["MinimumFrequency"] = f"{current_min_freq}GHz"
        setup.props["NumModes"] = num_modes
        setup.props["MaximumPasses"] = max_passes
        setup.props["MinimumPasses"] = min_passes
        setup.props["MinimumConvergedPasses"] = min_converged
        setup.props["MaximumDeltaFreqPerPass"] = max_delta_f

        log(f"Running {setup_name} starting at {current_min_freq:.3f} GHz...")
        success = hfss.analyze_setup(setup_name)
        if not success:
            raise RuntimeError(f"Simulation {setup_name} failed. Check the HFSS message manager.")

        eigen_results = get_eigenmode_results(
            hfss,
            setup,
            log,
            export_csv_path=os.path.join(out_dir, f"{setup_name}_raw_solution.csv"),
        )
        if not eigen_results:
            log(f"No modes found in {setup_name}. Stopping.")
            break

        conv_error = str(get_convergence_data(setup))

        for mode_result in eigen_results:
            row = {
                "setup": setup_name,
                "mode": mode_result["mode"],
                "freq_ghz": mode_result["freq_ghz"],
                "q": mode_result["q"],
                "convergence_error": conv_error,
            }

            mode_out_dir = os.path.join(out_dir, f"{setup_name}_Mode_{mode_result['mode']}")
            os.makedirs(mode_out_dir, exist_ok=True)
            pp_result = run_post_processing(
                hfss,
                task,
                {"setup_name": setup_name, "mode": mode_result["mode"]},
                mode_out_dir,
            )
            row.update(pp_result)
            modes_data.append(row)

        freqs_ghz = sorted(r["freq_ghz"] for r in eigen_results)
        last_freq = freqs_ghz[-1]

        if last_freq >= target_max_freq:
            break

        if len(freqs_ghz) > 1:
            diff = last_freq - freqs_ghz[-2]
            current_min_freq = last_freq + (next_setup_margin * diff)
        else:
            # Only one mode found -- no gap to extrapolate a safe next
            # starting point from, so stop rather than guess.
            break

        setup_idx += 1

    # Building the per-task CSV is the client pipeline's job (it also
    # merges every task's data into one combined CSV for plotting) --
    # this handler just hands back the raw per-mode data via result.json,
    # which worker.py writes into this same project folder
    # (task["output_dir"] == the projects/<task_id>/ folder).
    hfss.save_project(task["project_file"])

    return {
        "modes": modes_data,
        "num_modes_found": len(modes_data),
        "num_setups_run": setup_idx,
    }
