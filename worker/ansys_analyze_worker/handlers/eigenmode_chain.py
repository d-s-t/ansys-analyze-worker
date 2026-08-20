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


def _real_values(data, expression: str) -> List[float]:
    """Real value(s) for `expression` out of a SolutionData object."""
    _, y = data.get_expression_data(expression=expression, formula="real")
    return list(y)


def get_eigenmode_results(hfss, setup, log) -> List[Dict[str, Any]]:
    """
    Resonant frequency (and Q, where available) for every mode of a
    solved Eigenmode setup.

    Two things here are easy to get wrong:

    1. Eigenmode has no frequency sweep, so `Freq` is not an intrinsic
       you can read back the way you would for a Driven Modal solution.
       Each mode's frequency is its own report quantity, and Q lives in
       a separate quantity category, "Eigen Q".
    2. The quantity LISTING must be unfiltered.
       `available_report_quantities(report_category=..., solution=...)`
       silently returns an empty list on this AEDT/PyAEDT version --
       `report_category="Eigenmode"` belongs on the get_solution_data
       call that fetches values, never on the listing call.

    Mode and Q are paired positionally (same index = same mode), as in
    Ansys' own eigenmode example -- the expression text isn't guaranteed
    to contain a parseable mode number.
    """
    if not setup.is_solved:
        raise RuntimeError(
            f"Setup {setup.name} is not solved -- AEDT has no solution data for it, so there "
            "are no eigenmodes to read back. Check the HFSS message manager on the worker machine."
        )

    mode_exprs = hfss.post.available_report_quantities()
    q_exprs = hfss.post.available_report_quantities(quantities_category="Eigen Q")

    if len(mode_exprs) != len(q_exprs):
        # Pair up what we can rather than failing outright, but say so --
        # silently dropping quantities would look like "the solve just
        # found fewer modes".
        count = min(len(mode_exprs), len(q_exprs))
        log(
            f"{setup.name}: {len(mode_exprs)} mode quantities vs {len(q_exprs)} 'Eigen Q' "
            f"quantities; using the first {count} of each."
        )
        mode_exprs, q_exprs = mode_exprs[:count], q_exprs[:count]

    results: List[Dict[str, Any]] = []
    for i, (mode_expr, q_expr) in enumerate(zip(mode_exprs, q_exprs), start=1):
        freq_data = setup.get_solution_data(expressions=mode_expr, report_category="Eigenmode")
        freqs = _real_values(freq_data, mode_expr) if freq_data is not None else []
        if not freqs:
            log(f"{setup.name}: no data for {mode_expr!r}; skipping.")
            continue

        q_data = setup.get_solution_data(expressions=q_expr, report_category="Eigenmode")
        qs = _real_values(q_data, q_expr) if q_data is not None else []

        results.append(
            {
                "mode": i,
                "freq_hz": freqs[0],
                "freq_ghz": freqs[0] / 1e9,
                "q": qs[0] if qs else None,
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

        eigen_results = get_eigenmode_results(hfss, setup, log)
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
