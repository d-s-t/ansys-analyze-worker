"""
Workarounds for third-party behaviour the worker depends on. Each one is
small, guarded so it can never stop the worker from starting, and meant to
be deleted once the upstream package catches up.

**psutil on Windows 11 26H2 (build 26300).** On that build
`GetExtendedTcpTable` reports *listening* TCP sockets with state `0`.
psutil 7.2.2 has no mapping for 0 and reports the status `NONE`, while
PyAEDT 1.0.0 recognises a running AEDT gRPC session only by a socket whose
status is `LISTEN` (`_check_connection_grpc_port` in
`ansys/aedt/core/generic/general_methods.py`). So PyAEDT never sees the AEDT
it has just launched (or one that is already open), and
`Desktop(...)` gives up after `desktop_launch_timeout` (120 s) with
"Failed to start new AEDT gRPC session on port ...". Every task then fails
after about two minutes. Windows 11 build 26200 with the same psutil
reports `LISTEN`. Full diagnosis:
https://github.com/d-s-t/ansys-analyze-worker/issues/11

Two pieces here: `apply_psutil_listen_fix()` maps state 0 to `LISTEN`, and
`check_psutil_listen_status()` is a startup self-check that turns a
recurrence of this class of problem into one log line instead of a day of
diagnosis.
"""
from __future__ import annotations

import logging
import socket
import sys
from typing import Optional

logger = logging.getLogger("ansys_analyze_worker")

ISSUE_URL = "https://github.com/d-s-t/ansys-analyze-worker/issues/11"

# The raw TCP state Windows build 26300 returns for a listening socket.
# Every state psutil knows about (MIB_TCP_STATE_*) starts at 1, so 0 is never
# a legitimate state that this could shadow.
_LISTEN_RAW_STATE = 0

_fix_applied = False


def apply_psutil_listen_fix() -> bool:
    """
    Make psutil report raw TCP state 0 as `LISTEN` (Windows only).

    Idempotent and cheap, so callers don't need to track whether it already
    ran. Returns True if the mapping is in place after the call, False if it
    isn't (not Windows, psutil missing, or psutil's internals changed shape).
    Never raises: a missing shim only means the original bug stays, which the
    startup self-check then reports.

    Remove when psutil handles Windows 26H2.
    """
    global _fix_applied
    if _fix_applied:
        return True
    if sys.platform != "win32":
        return False
    try:
        from psutil import _common, _pswindows

        _pswindows.TCP_STATUSES.setdefault(_LISTEN_RAW_STATE, _common.CONN_LISTEN)
    except Exception:
        logger.warning("Could not apply the psutil listening-socket workaround.", exc_info=True)
        return False
    _fix_applied = True
    logger.info(
        "psutil reports TCP state 0 for listening sockets on some Windows builds (e.g. 26300 / 26H2; "
        "this is build %s): mapping it to LISTEN so PyAEDT can find AEDT. See %s",
        _windows_build(),
        ISSUE_URL,
    )
    return True


def check_psutil_listen_status() -> Optional[str]:
    """
    Startup self-check: open a loopback listener, ask psutil what status it
    has, and log an ERROR pointing at the issue if it isn't `LISTEN`.

    Run it after `apply_psutil_listen_fix()`. Returns the status psutil
    reported, or None if the check itself couldn't run (that is logged as a
    warning, never raised -- it must not stop the worker from starting).
    """
    try:
        import psutil

        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as server:
            server.bind(("127.0.0.1", 0))
            server.listen(1)
            port = server.getsockname()[1]
            # psutil renamed Process.connections() to net_connections() in 6.0.
            list_connections = getattr(psutil.Process(), "net_connections", None) or psutil.Process().connections
            statuses = [c.status for c in list_connections("inet") if c.laddr and c.laddr.port == port]
    except Exception:
        logger.warning("Could not run the psutil listening-socket self-check.", exc_info=True)
        return None

    status = statuses[0] if statuses else None
    if status == psutil.CONN_LISTEN:
        logger.info("psutil listening-socket self-check passed.")
    else:
        logger.error(
            "psutil reports a plain listening socket as %r instead of 'LISTEN' (Windows build %s). "
            "PyAEDT will not be able to find or start AEDT and every task will fail after about two "
            "minutes with 'Failed to start new AEDT gRPC session'. See %s",
            status,
            _windows_build(),
            ISSUE_URL,
        )
    return status


def _windows_build() -> str:
    try:
        return str(sys.getwindowsversion().build)  # type: ignore[attr-defined]
    except AttributeError:
        return "n/a"
