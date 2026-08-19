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
from typing import Any, Dict, List

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


def get_eigenmode_results(hfss, setup_name: str) -> List[Dict[str, Any]]:
    """
    Retrieve the resonant frequency (and Q, if available) for every mode
    of a solved Eigenmode setup.

    THIS IS THE FIX for the bug in the original script. Eigenmode
    solutions do not have a frequency sweep, so `Freq` is *not* an
    intrinsic you can read back with a plain
    `hfss.post.get_solution_data(setup_sweep_name=...)` call the way you
    would for a Driven Modal solution -- `freqs_data.intrinsics.get('Freq')`
    is always empty for an Eigenmode setup, since Eigenmode has no such
    intrinsic. Instead, each mode's resonant frequency is exposed as its
    own report quantity, and quality factor lives in a separate quantity
    category, "Eigen Q".

    A SECOND, more subtle bug was in how those quantities were listed:
    `available_report_quantities(report_category="Eigenmode", solution=...)`
    -- passing report_category/solution to the LISTING call -- silently
    returns an empty list against this PyAEDT/AEDT version, which is why
    this returned nothing even though the modes were plainly visible in
    AEDT. Ansys' own PyAEDT eigenmode-filter example calls it with NO
    filters at all (`available_report_quantities()` for the mode
    quantities, `available_report_quantities(quantities_category="Eigen
    Q")` for the Q quantities); report_category="Eigenmode" only belongs
    on the later `get_solution_data(...)` calls that actually fetch each
    quantity's value. This function now follows that exact pattern.

    Ansys' example also doesn't parse a mode number out of the expression
    string -- it pairs the two lists positionally (same index = same
    mode), since AEDT returns them in matching order. This function does
    the same rather than regex-matching "(n)" out of the expression text,
    which isn't guaranteed to be there.
    """
    solution_name = f"{setup_name} : LastAdaptive"

    mode_exprs = hfss.post.available_report_quantities()
    q_exprs = hfss.post.available_report_quantities(quantities_category="Eigen Q")

    if len(mode_exprs) != len(q_exprs):
        # Not necessarily fatal -- pair up what we can and drop the rest
        # rather than erroring out on a partial mismatch.
        pair_count = min(len(mode_exprs), len(q_exprs))
        mode_exprs = mode_exprs[:pair_count]
        q_exprs = q_exprs[:pair_count]

    results: List[Dict[str, Any]] = []
    for i, (mode_expr, q_expr) in enumerate(zip(mode_exprs, q_exprs), start=1):
        freq_data = hfss.post.get_solution_data(
            expressions=mode_expr, setup_sweep_name=solution_name, report_category="Eigenmode"
        )
        freq_values = _get_real_values(freq_data, mode_expr) if freq_data else None
        if not freq_values:
            continue

        q_data = hfss.post.get_solution_data(
            expressions=q_expr, setup_sweep_name=solution_name, report_category="Eigenmode"
        )
        q_values = _get_real_values(q_data, q_expr) if q_data else None

        freq_hz = freq_values[0]
        results.append(
            {
                "mode": i,
                "freq_hz": freq_hz,
                "freq_ghz": freq_hz / 1e9,
                "q": q_values[0] if q_values else None,
            }
        )

    results.sort(key=lambda r: r["freq_hz"])
    return results


def get_convergence_data(hfss, setup_name: str):
    try:
        setup = hfss.get_setup(setup_name)
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

        eigen_results = get_eigenmode_results(hfss, setup_name)
        if not eigen_results:
            log(f"No modes found in {setup_name}. Stopping.")
            break

        conv_error = str(get_convergence_data(hfss, setup_name))

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

    # Building modes_results.csv is Part 3's job (it also merges every
    # task's data into one combined CSV for plotting) -- this handler
    # just hands back the raw per-mode data via result.json, which
    # worker.py writes into this same project folder (task["output_dir"]
    # == the projects/<task_id>/ folder, not a separate results/ tree).
    hfss.save_project(task["project_file"])

    return {
        "modes": modes_data,
        "num_modes_found": len(modes_data),
        "num_setups_run": setup_idx,
    }
