"""Measure whether a launcher actually shows a console window.

Reproduces the finding documented in docs/windows-no-console-window.md:
`powershell.exe -WindowStyle Hidden` does NOT prevent the flash. The console
(and its window) is allocated by the OS when the process is created, before
PowerShell gets a chance to parse its own arguments.

Run it on the machine you care about:

    python tools\\check_console_flash.py

It launches three throwaway launchers and samples every 5 ms for visible
`ConsoleWindowClass` windows, then prints how long each was on screen.
Case 3 is the one this repository ships; case 2 is the naive fix.

Windows only. Python 3.9+, stdlib only.
"""

import ctypes
import ctypes.wintypes as wt
import os
import subprocess
import sys
import tempfile
import time

if sys.platform != "win32":
    raise SystemExit("This tool measures Windows console windows; it only runs on Windows.")

sys.stdout.reconfigure(encoding="utf-8", errors="replace")

_user32 = ctypes.windll.user32
_ENUM_PROC = ctypes.WINFUNCTYPE(wt.BOOL, wt.HWND, wt.LPARAM)

SAMPLE_MS = 5
RUN_MS = 1600


def _console_windows():
    """Every top-level console window: (pid, visible, title)."""
    found = []

    def _cb(hwnd, _):
        name = ctypes.create_unicode_buffer(256)
        _user32.GetClassNameW(hwnd, name, 256)
        if name.value == "ConsoleWindowClass":
            title = ctypes.create_unicode_buffer(512)
            _user32.GetWindowTextW(hwnd, title, 512)
            pid = wt.DWORD(0)
            _user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
            found.append((pid.value, bool(_user32.IsWindowVisible(hwnd)), title.value))
        return True

    _user32.EnumWindows(_ENUM_PROC(_cb), 0)
    return found


def _run_case(vbs_line):
    """Launch a VBS snippet and watch for newly visible console windows.

    Returns {pid: (first_visible_ms, last_visible_ms)}.
    """
    fd, path = tempfile.mkstemp(suffix=".vbs", text=True)
    try:
        with os.fdopen(fd, "w", encoding="ascii") as f:
            f.write('Set sh = CreateObject("WScript.Shell")\n')
            f.write(vbs_line + "\n")

        before = {pid for pid, _, _ in _console_windows()}
        visible = {}
        t0 = time.time()
        proc = subprocess.Popen(["wscript.exe", "//B", path])
        try:
            while True:
                elapsed = (time.time() - t0) * 1000
                if elapsed > RUN_MS:
                    break
                for pid, is_visible, _ in _console_windows():
                    if pid in before or not is_visible:
                        continue
                    span = visible.setdefault(pid, [elapsed, elapsed])
                    span[1] = elapsed
                time.sleep(SAMPLE_MS / 1000.0)
        finally:
            try:
                proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                proc.kill()
        return visible
    finally:
        try:
            os.unlink(path)
        except OSError:
            pass


def _escape(s):
    """VBScript escapes a double quote by doubling it."""
    return s.replace('"', '""')


def main():
    # Short-lived payloads, so the window is gone before the next case runs.
    plain = 'powershell.exe -NoProfile -Command "Start-Sleep -Milliseconds 700"'
    hidden = 'powershell.exe -WindowStyle Hidden -NoProfile -Command "Start-Sleep -Milliseconds 700"'

    cases = [
        (
            "control: no -WindowStyle",
            "the window is on screen for the whole run",
            'sh.Run "%s", 1, False' % _escape(plain),
        ),
        (
            "-WindowStyle Hidden  (does NOT work)",
            "shorter, but it still appears",
            'sh.Run "%s", 1, False' % _escape(hidden),
        ),
        (
            "wscript SW_HIDE  (what this repo ships)",
            "never visible, not even for one frame",
            'sh.Run "%s", 0, False' % _escape(plain),
        ),
    ]

    print()
    print("Codex Overload Fix - console window check")
    print("-" * 62)
    print("Sampling every %d ms for visible ConsoleWindowClass windows." % SAMPLE_MS)
    print()

    results = []
    for i, (label, expect, vbs) in enumerate(cases):
        if i:
            time.sleep(0.8)          # let the previous window finish closing
        spans = _run_case(vbs)
        if spans:
            first = min(f for f, _ in spans.values())
            last = max(l for _, l in spans.values())
            on_screen = last - first
        else:
            first = last = on_screen = None
        results.append((label, expect, first, on_screen))

        if spans:
            print("  %-40s FLASH" % label)
            print("      first visible +%.0f ms, on screen %.0f ms" % (first, on_screen))
        else:
            print("  %-40s clean (no window)" % label)
        print("      %s" % expect)
        print()

    ok = results[0][2] is not None and results[1][2] is not None and results[2][2] is None
    print("-" * 62)
    if ok:
        print("Result: reproduced on this machine.")
        print("  -WindowStyle Hidden cut the flash from %.0f ms to %.0f ms, but did not remove it."
              % (results[0][3], results[1][3]))
        print("  Both appeared at roughly the same moment (+%.0f ms / +%.0f ms) -- the window"
              % (results[0][2], results[1][2]))
        print("  is created by conhost before PowerShell parses the flag.")
        print("  Only SW_HIDE at process creation keeps it hidden from the first frame.")
    else:
        print("Result: numbers differ from the reference run in the docs.")
        print("  Report your OS build -- this behaviour is version-dependent.")
    print()


if __name__ == "__main__":
    main()
