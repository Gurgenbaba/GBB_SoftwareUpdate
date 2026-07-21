from __future__ import annotations

import ctypes
import platform
import subprocess
import sys

from app.ui import UpdaterApp


def _is_admin() -> bool:
    try:
        return bool(ctypes.windll.shell32.IsUserAnAdmin())
    except Exception:
        return False


def _relaunch_as_admin() -> bool:
    if "--elevated" in sys.argv:
        return False
    if getattr(sys, "frozen", False):
        executable = sys.executable
        args = [*sys.argv[1:], "--elevated"]
    else:
        executable = sys.executable
        args = [sys.argv[0], *sys.argv[1:], "--elevated"]
    params = subprocess.list2cmdline(args)
    try:
        rc = ctypes.windll.shell32.ShellExecuteW(None, "runas", executable, params, None, 1)
    except Exception:
        return False
    return int(rc) > 32


def main() -> None:
    if platform.system().lower() != "windows":
        raise RuntimeError("Compexx-InstallTool ist nur fuer Windows ausgelegt.")
    if not _is_admin():
        if _relaunch_as_admin():
            return
        raise PermissionError("Administratorrechte sind erforderlich. Bitte App als Administrator starten.")
    app = UpdaterApp()
    app.mainloop()


if __name__ == "__main__":
    try:
        main()
    except Exception as exc:
        print(f"Fataler Fehler: {exc}", file=sys.stderr)
        sys.exit(1)
