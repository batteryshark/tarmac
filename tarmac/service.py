"""The ``local.tarmac`` user LaunchAgent for ``tarmac daemon``.

A USER agent, never a system daemon: the Claude and Codex CLIs need the
login keychain. launchd hands its jobs a bare environment; without USER and
LOGNAME the Claude CLI cannot find its stored credentials and reports "Not
logged in" — which would surface as a FALSE needs_login. The plist therefore
carries PATH, HOME, USER, LOGNAME and the resolved TARMAC_HOME explicitly.
"""
import os
import plistlib
import shutil
import subprocess
import sys
from pathlib import Path

from tarmac import store

LABEL = "local.tarmac"
PLIST = Path("~/Library/LaunchAgents/local.tarmac.plist").expanduser()

def _domain() -> str:
    return f"gui/{os.getuid()}"


def run(action: str) -> int:
    if action == "install":
        exe = shutil.which("tarmac") or sys.argv[0]
        path = os.environ.get("PATH", "")
        for extra in ("/opt/homebrew/bin", "/usr/local/bin", "/usr/bin", "/bin"):
            if extra not in path.split(":"):
                path += f":{extra}"
        try:
            raw = os.environ.get("TARMAC_HOME", "~/.tarmac")
            if not raw or not Path(raw).expanduser().is_absolute():
                raise ValueError("unsupported TARMAC_HOME")
            state = store.home().resolve()
            data = plistlib.dumps({
                "Label": LABEL, "ProgramArguments": [exe, "daemon"],
                "RunAtLoad": True, "KeepAlive": True,
                "EnvironmentVariables": {
                    "PATH": path, "HOME": str(Path.home()),
                    "USER": os.environ.get("USER", ""), "LOGNAME": os.environ.get("USER", ""),
                    "TARMAC_HOME": str(state),
                },
                "StandardOutPath": str(state / "daemon.out.log"),
                "StandardErrorPath": str(state / "daemon.err.log"),
            })
        except (OSError, ValueError, RuntimeError):
            print("tarmac service: unsupported state/environment configuration; no install")
            return 1
        PLIST.parent.mkdir(parents=True, exist_ok=True)
        PLIST.write_bytes(data)
        subprocess.run(["launchctl", "bootout", f"{_domain()}/{LABEL}"],
                       capture_output=True)
        done = subprocess.run(["launchctl", "bootstrap", _domain(), str(PLIST)],
                              capture_output=True, text=True)
        if done.returncode != 0:
            detail = (done.stderr.strip() or done.stdout.strip() or "no diagnostic")[:512]
            print(f"tarmac service: bootstrap failed ({done.returncode}): {detail}")
            return done.returncode
        print(f"tarmac service: installed {PLIST}")
        return 0
    if action == "uninstall":
        subprocess.run(["launchctl", "bootout", f"{_domain()}/{LABEL}"],
                       capture_output=True)
        PLIST.unlink(missing_ok=True)
        print("tarmac service: uninstalled")
        return 0
    if action == "restart":
        done = subprocess.run(["launchctl", "kickstart", "-k",
                               f"{_domain()}/{LABEL}"], capture_output=True,
                              text=True)
        print(done.stdout.strip() or done.stderr.strip()
              or f"tarmac service: restarted {LABEL}")
        return done.returncode
    out = subprocess.run(["launchctl", "print", f"{_domain()}/{LABEL}"],
                         capture_output=True, text=True)
    if out.returncode != 0:
        print(f"tarmac service: not loaded ({PLIST}"
              f"{' present' if PLIST.exists() else ' absent'})")
        return 1
    line = next((ln.strip() for ln in out.stdout.splitlines()
                 if "pid =" in ln or "state =" in ln), "loaded")
    print(f"tarmac service: {line}")
    return 0
