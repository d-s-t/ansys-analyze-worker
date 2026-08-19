"""
System tray ("taskbar notification area") icon for the background worker,
similar to OneDrive/other Windows background apps: right-click for a menu
with the option to pause/resume processing or exit the service.

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


def run_tray_app(worker, queue_root: str, log_file_path: str) -> None:
    """
    Blocks the calling (main) thread running the tray icon's event loop.
    The worker's scanning loop must already be running on its own thread
    before this is called (see run_service.py).
    """
    import pystray

    def on_toggle_pause(icon, item):
        if worker.is_paused:
            worker.resume()
        else:
            worker.pause()
        icon.update_menu()

    def pause_label(item):
        return "Resume processing" if worker.is_paused else "Pause processing"

    def status_label(item):
        return "Status: Paused" if worker.is_paused else "Status: Running"

    def on_open_queue_folder(icon, item):
        os.startfile(queue_root)  # Windows only, by design

    def on_open_log(icon, item):
        if os.path.exists(log_file_path):
            os.startfile(log_file_path)  # Windows only, by design

    def on_exit(icon, item):
        worker.stop()
        icon.stop()

    def on_reset(icon, item):
        # Full process restart (see run_service.py / Worker.request_restart)
        # -- this is how code changes to any file in part2_worker get
        # picked up without re-launching by hand. Doesn't wait for an
        # in-flight task; see request_restart()'s docstring for what
        # happens to one if it's mid-analysis when this fires.
        worker.request_restart()
        icon.stop()

    menu = pystray.Menu(
        pystray.MenuItem(status_label, None, enabled=False),
        pystray.MenuItem(pause_label, on_toggle_pause),
        pystray.MenuItem("Open queue folder", on_open_queue_folder),
        pystray.MenuItem("Open log file", on_open_log),
        pystray.Menu.SEPARATOR,
        pystray.MenuItem("Reset (reload code)", on_reset),
        pystray.Menu.SEPARATOR,
        pystray.MenuItem("Exit", on_exit),
    )

    icon = pystray.Icon("ansys_analyze_worker", _make_icon_image(), "Ansys Analyze Worker", menu)
    icon.run()
