"""
System tray ("taskbar notification area") icon for the background worker,
similar to OneDrive/other Windows background apps: right-click for a menu
to release/resume processing, stop whatever's currently running, open the
queue folder or log, reset, or exit.

This runs in the parent (supervisor) process, on the main thread -- the
actual scanning/analyzing work happens in a separate child process (see
supervisor.py's module docstring for why), so this event loop is never
blocked by a slow AEDT call. `controller` is a WorkerSupervisor.

Requires: pystray, Pillow  (pip install pystray pillow)
"""
from __future__ import annotations

import os


def _make_icon_image(color: str = "#2E86AB"):
    from PIL import Image, ImageDraw

    size = 64
    img = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    draw = ImageDraw.Draw(img)
    draw.ellipse((4, 4, size - 4, size - 4), fill=color)
    draw.text((24, 20), "A", fill="white")
    return img


def run_tray_app(controller, queue_root: str, log_file_path: str) -> None:
    """
    Blocks the calling (main) thread running the tray icon's event loop.
    `controller` (a WorkerSupervisor) must already have its worker child
    process running before this is called (see run_service.py).
    """
    import pystray

    def on_toggle_release(icon, item):
        if controller.is_paused:
            controller.resume()
        else:
            controller.release()
        icon.update_menu()

    def release_label(item):
        return "Resume processing" if controller.is_paused else "Release (pause + detach from AEDT)"

    def status_label(item):
        return f"Status: {controller.state_label}"

    def stop_current_run_enabled(item):
        return controller.current_task_id is not None

    def on_stop_current_run(icon, item):
        controller.stop_current_run()
        icon.update_menu()

    def on_open_queue_folder(icon, item):
        os.startfile(queue_root)  # Windows only, by design

    def on_open_log(icon, item):
        if os.path.exists(log_file_path):
            os.startfile(log_file_path)  # Windows only, by design

    def on_exit(icon, item):
        controller.exit()
        icon.stop()

    def on_reset(icon, item):
        # Full process restart (see run_service.py / WorkerSupervisor.
        # request_restart) -- this is how code changes anywhere in the
        # package get picked up without re-launching by hand. Doesn't wait
        # for an in-flight task; see request_restart()'s docstring for
        # what happens to one if it's mid-analysis when this fires.
        controller.request_restart()
        icon.stop()

    menu = pystray.Menu(
        pystray.MenuItem(status_label, None, enabled=False),
        pystray.MenuItem("Stop current run", on_stop_current_run, enabled=stop_current_run_enabled),
        pystray.MenuItem(release_label, on_toggle_release),
        pystray.MenuItem("Open queue folder", on_open_queue_folder),
        pystray.MenuItem("Open log file", on_open_log),
        pystray.Menu.SEPARATOR,
        pystray.MenuItem("Reset (reload code)", on_reset),
        pystray.Menu.SEPARATOR,
        pystray.MenuItem("Exit", on_exit),
    )

    icon = pystray.Icon("ansys_analyze_worker", _make_icon_image(), "Ansys Analyze Worker", menu)
    icon.run()
