"""
Generic, reusable post-processing operations that any task handler can
invoke via a task's `post_processing` list, e.g.:

    "post_processing": [
        {"op": "field_plot_surface", "params": {
            "object_key": "chip", "quantity": "Mag_E"
        }},
        {"op": "field_plot_volume", "params": {
            "object_key": "vacuum", "quantity": "Mag_E"
        }}
    ]

Each op is a function `(hfss, task, mode_context, params, out_dir) -> dict`
that returns whatever it produced (paths, extracted values, ...); the
calling handler merges that dict into the result row for that mode.

To add a new op: write a function here with that signature and register
it in POST_PROCESSING_OPS. Every handler that calls run_post_processing()
automatically gets access to it via the task file -- no handler code
changes needed.
"""
from __future__ import annotations

import os
from typing import Any, Callable, Dict

import pandas as pd


def _resolve_object(hfss, task: Dict[str, Any], object_key: str):
    obj_name = task.get("objects", {}).get(object_key)
    if not obj_name:
        return None
    return hfss.modeler[obj_name]


def _top_face(obj):
    faces = sorted(obj.faces, key=lambda f: f.area, reverse=True)
    return max(faces[:2], key=lambda f: f.center[2])


def op_field_plot_surface(hfss, task, mode_context, params, out_dir) -> Dict[str, Any]:
    object_key = params["object_key"]
    quantity = params.get("quantity", "Mag_E")
    obj = _resolve_object(hfss, task, object_key)
    if obj is None:
        return {"skipped": True, "reason": f"object '{object_key}' not found"}

    face = _top_face(obj) if params.get("face", "top") == "top" else obj.faces[0]

    setup_name = mode_context["setup_name"]
    mode_idx = mode_context["mode"]
    plot_name = f"{setup_name}_Mode_{mode_idx}_{object_key}_surface_{quantity}"

    hfss.post.create_fieldplot_surface(
        [face.id],
        quantity,
        setup_name=f"{setup_name} : LastAdaptive",
        plot_name=plot_name,
        intrinsics={"Mode": str(mode_idx), "Phase": "0deg"},
    )

    img_path = os.path.join(out_dir, f"{object_key}_surface_{quantity}.jpg")
    hfss.post.export_field_jpg(img_path, plot_name)

    csv_path = os.path.join(out_dir, f"{object_key}_surface_{quantity}.csv")
    hfss.post.export_field_plot(plot_name, csv_path)

    max_value = None
    if os.path.exists(csv_path):
        df_field = pd.read_csv(csv_path, skiprows=1)
        if not df_field.empty:
            max_value = float(df_field[df_field.columns[-1]].max())

    return {
        "image_path": img_path,
        "data_path": csv_path,
        f"max_{quantity.lower()}": max_value,
    }


def op_field_plot_volume(hfss, task, mode_context, params, out_dir) -> Dict[str, Any]:
    object_key = params["object_key"]
    quantity = params.get("quantity", "Mag_E")
    obj = _resolve_object(hfss, task, object_key)
    if obj is None:
        return {"skipped": True, "reason": f"object '{object_key}' not found"}

    setup_name = mode_context["setup_name"]
    mode_idx = mode_context["mode"]
    plot_name = f"{setup_name}_Mode_{mode_idx}_{object_key}_volume_{quantity}"

    hfss.post.create_fieldplot_volume(
        [obj.name],
        quantity,
        setup_name=f"{setup_name} : LastAdaptive",
        plot_name=plot_name,
        intrinsics={"Mode": str(mode_idx), "Phase": "0deg"},
    )

    img_path = os.path.join(out_dir, f"{object_key}_volume_{quantity}.jpg")
    hfss.post.export_field_jpg(img_path, plot_name)

    return {"image_path": img_path}


POST_PROCESSING_OPS: Dict[str, Callable[..., Dict[str, Any]]] = {
    "field_plot_surface": op_field_plot_surface,
    "field_plot_volume": op_field_plot_volume,
}


def run_post_processing(hfss, task: Dict[str, Any], mode_context: Dict[str, Any], out_dir: str) -> Dict[str, Any]:
    """Run every op listed in task['post_processing'] for a single mode."""
    combined: Dict[str, Any] = {}
    for step in task.get("post_processing", []):
        op_name = step.get("op")
        op_fn = POST_PROCESSING_OPS.get(op_name)
        if op_fn is None:
            combined.setdefault("post_processing_errors", []).append(
                f"Unknown post_processing op '{op_name}'"
            )
            continue
        try:
            result = op_fn(hfss, task, mode_context, step.get("params", {}), out_dir)
            combined.update(result or {})
        except Exception as exc:  # keep going so one bad op doesn't kill the rest
            combined.setdefault("post_processing_errors", []).append(f"{op_name} failed: {exc}")
    return combined
