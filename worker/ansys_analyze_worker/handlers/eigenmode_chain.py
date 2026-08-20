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
import re
from typing import Any, Dict, List

from ..post_processing import run_post_processing


def _solution_name(hfss, setup) -> str:
    """
    The "<setup> : LastAdaptive" string that identifies this setup's
    solution in every post-processing call.

    Everything below has to pass this explicitly. `Setup.get_solution_data`
    looks like it scopes itself to its own setup, but it does not: it
    forwards `setup_sweep_name=sweep`, which is None unless the caller
    asked for a named sweep, so AEDT silently answers out of
    `hfss.nominal_adaptive` -- the FIRST setup in the design. In a chain
    of setups every setup after the first would report setup 1's modes.
    """
    for name in hfss.existing_analysis_sweeps:
        if name.split(" : ")[0] == setup.name:
            return name
    raise RuntimeError(
        f"Setup {setup.name} has no solution entry in the design "
        f"(existing_analysis_sweeps={hfss.existing_analysis_sweeps})."
    )


def _mode_index(expression: str) -> int:
    """1 out of 'Mode(1)' / 'Q(1)'."""
    match = re.search(r"\((\d+)\)", expression)
    if not match:
        raise RuntimeError(f"Cannot read a mode number out of report quantity {expression!r}.")
    return int(match.group(1))


def get_eigenmode_results(hfss, setup, log) -> List[Dict[str, Any]]:
    """
    Resonant frequency and Q for every mode of a solved Eigenmode setup.

    Eigenmode has no frequency sweep, so `Freq` is not an intrinsic you
    can read back the way you would for a Driven Modal solution. Each
    mode is its own report quantity -- "Mode(n)", whose real part is the
    resonant frequency in Hz -- and Q is a separate quantity, "Q(n)", in
    the "Eigen Q" category. Both are fetched in one batched call and
    paired by the mode number in the expression text.
    """
    if not setup.is_solved:
        raise RuntimeError(
            f"Setup {setup.name} is not solved -- AEDT has no solution data for it, so there "
            "are no eigenmodes to read back. Check the HFSS message manager on the worker machine."
        )

    solution = _solution_name(hfss, setup)

    # `solution=` is what binds the listing to THIS setup; without it the
    # listing also falls back to the design's first setup. The listing
    # call must stay otherwise unfiltered -- passing report_category here
    # returns the wrong category ("Passivity"), not the mode quantities.
    mode_exprs = hfss.post.available_report_quantities(solution=solution)
    q_exprs = hfss.post.available_report_quantities(solution=solution, quantities_category="Eigen Q")
    if not mode_exprs:
        return []

    data = hfss.post.get_solution_data(
        expressions=mode_exprs + q_exprs,
        setup_sweep_name=solution,
        report_category="Eigenmode Parameters",
    )
    if data is None:
        raise RuntimeError(f"No solution data returned for {solution}.")

    # get_solution_data hands the expressions back in its own order, so
    # index by name rather than by position.
    def first_real(expression):
        if expression not in data.expressions:
            return None
        _, y = data.get_expression_data(expression=expression, formula="real")
        values = list(y)
        return float(values[0]) if values else None

    qs = {_mode_index(e): first_real(e) for e in q_exprs}

    results: List[Dict[str, Any]] = []
    for mode_expr in mode_exprs:
        mode = _mode_index(mode_expr)
        freq_hz = first_real(mode_expr)
        if freq_hz is None:
            log(f"{setup.name}: no data for {mode_expr!r}; skipping.")
            continue
        results.append(
            {
                "mode": mode,
                "freq_hz": freq_hz,
                "freq_ghz": freq_hz / 1e9,
                "q": qs.get(mode),
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
        success = setup.analyze() or setup.is_solved
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
