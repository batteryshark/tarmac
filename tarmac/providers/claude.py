"""Claude: auth probe, cache-file parse, and the /usage screen read.

Claude stores no key tarmac can use — the Claude Code CLI owns the login.
Three sources combine into one reading:

1. ``claude auth status --json`` — the authoritative logged-in probe. An
   affirmative ``loggedIn: false`` yields an instant ``needs_login`` reading
   and skips everything below. A probe FAILURE (CLI missing, malformed JSON)
   is not a logout: the cache path below still works on a machine that only
   has ``~/.claude.json``, and it must never be falsely flagged.
2. ``~/.claude.json`` ``cachedUsageUtilization`` — reset times, and a
   fallback for the percentages. Cache measurements can lag behind /usage.
3. A PTY running ``claude --safe-mode --ax-screen-reader`` with ``/usage``
   typed at it — the live percentages, including per-model weekly rows that
   are never written to the cache file.
"""
import json
import os
import re
import select
import subprocess
import time
from pathlib import Path

from tarmac.model import (Reading, age_text, expired, from_windows, iso,
                          make_window, now, scrub, soft, unknown,
                          window_label)
from tarmac.providers.creds import which

try:
    import pty
except ImportError:
    pty = None

CLAUDE_CACHE = Path("~/.claude.json")

# How often the auth probe may spawn the CLI. ~1s each; 300s keeps expiry
# detection within minutes without a subprocess on every poll.
CLAUDE_AUTH_EVERY_S = 300.0
# Keyed by config dir ("" = the default account): a second account is a
# second CLAUDE_CONFIG_DIR with its own login and its own throttle state.
_AUTH_LAST: dict[str, tuple[float, dict, str]] = {}

# How often the screen read may run at all. The poller ticks every five
# minutes; throttle screen reads to reduce CLI traffic and rate limiting.
CLAUDE_SCREEN_EVERY_S = 1200.0
# A read that came back WITHOUT the per-model rows is a rate-limited one, not
# a finished one. Keep it only long enough to avoid hammering.
CLAUDE_PARTIAL_EVERY_S = 120.0
_SCREEN_LAST: dict[str, tuple[float, dict, str, Reading]] = {}

CLAUDE_SCREEN_TIMEOUT = 35.0
# The per-model rows are a SEPARATE async fetch inside /usage and land after
# the account rows. Keep reading this long after them.
CLAUDE_SCREEN_GRACE = 10.0

_ANSI = re.compile(r"\x1b(?:\[[0-?]*[ -/]*[@-~]|\][^\x07]*(?:\x07|\x1b\\))")
_ROWS = {"Current session": "five_hour",
         "Current week (all models)": "seven_day"}
# Anthropic meters some models separately, and the row appears only once that
# model has its own limit — "Current week (Opus only)", and Fable the same.
# Matching the SHAPE rather than a list of names means a new one shows up
# without a code change.
_PER_MODEL = re.compile(r"Current week \(([^)]+)\)")


def auth_status(executable: str | None = None, runner=subprocess.run,
                config_dir: str | None = None) -> dict:
    """``{"logged_in": bool|None, "method": str|None, "checked_at": iso,
    "reason": str|None}``. ``logged_in`` is None when the probe could not
    answer — which is NOT a logout.

    The API-key variables are stripped so the probe answers for the
    subscription login, not for a key that happens to be exported.
    ``config_dir`` selects the account: the probe runs with that
    CLAUDE_CONFIG_DIR."""
    exe = executable or os.environ.get("TARMAC_CLAUDE", "claude")
    env = dict(os.environ)
    for name in ("ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN",
                 "CLAUDE_CODE_OAUTH_TOKEN"):
        env.pop(name, None)
    if config_dir:
        env["CLAUDE_CONFIG_DIR"] = str(Path(config_dir).expanduser())
    else:
        env.pop("CLAUDE_CONFIG_DIR", None)
    checked_at = now()
    try:
        result = runner([exe, "auth", "status", "--json"], env=env,
                        capture_output=True, text=True, encoding="utf-8",
                        errors="replace", timeout=15)
        value = json.loads(result.stdout)
        logged_in = value.get("loggedIn")
        if not isinstance(logged_in, bool):
            raise ValueError("no loggedIn bool in auth status")
    except (OSError, subprocess.TimeoutExpired, ValueError) as exc:
        return {"logged_in": None, "method": None, "checked_at": checked_at,
                "reason": f"auth probe unavailable: {exc}"}
    return {"logged_in": logged_in,
            "method": value.get("authMethod") or "unknown",
            "checked_at": checked_at, "reason": None}


def cached_auth_status(config_dir: str | None = None) -> dict:
    """The probe, at most once per CLAUDE_AUTH_EVERY_S per account."""
    key = str(config_dir or "")
    last = _AUTH_LAST.get(key)
    if last and time.monotonic() - last[0] < CLAUDE_AUTH_EVERY_S:
        return last[1]
    status = auth_status(config_dir=config_dir)
    _AUTH_LAST[key] = (time.monotonic(), status)
    return status


@soft("claude")
def parse_claude(data: dict, now_ms: float | None = None) -> Reading:
    """``cachedUsageUtilization``: percent USED per window, plus resets_at.
    Both windows are reported — a 5-hour limit and a weekly limit are two
    separate facts about the account.

    ponytail: Claude Code owns ~/.claude.json and may change this key without
    notice — shape drift degrades to unknown, it never guesses."""
    cache = (data or {}).get("cachedUsageUtilization")
    if not isinstance(cache, dict):
        return unknown("claude", "no cachedUsageUtilization in ~/.claude.json")
    fetched = cache.get("fetchedAtMs")
    if not isinstance(fetched, (int, float)):
        return unknown("claude", "cachedUsageUtilization has no fetchedAtMs")
    now_ms = time.time() * 1000 if now_ms is None else now_ms
    age_h = max(0.0, (now_ms - fetched) / 3600000)
    as_of = iso(fetched)

    windows = []
    for name, minutes in (("five_hour", 300), ("seven_day", 10080)):
        w = (cache.get("utilization") or {}).get(name)
        if not isinstance(w, dict) or not isinstance(w.get("utilization"), (int, float)):
            continue
        windows.append(make_window(window_label(minutes),
                                   100 - float(w["utilization"]),
                                   iso(w.get("resets_at"))))
    if not windows:
        return unknown("claude", f"no utilization window in the cache "
                                 f"(as of {as_of}, {age_text(age_h)} ago)")
    return from_windows("claude", windows, as_of=as_of,
                        raw=scrub({"as_of_age_h": round(age_h, 1)}))


def parse_screen(output) -> dict:
    """Percent USED per window from Claude Code's screen-reader /usage view.
    Only the named quota rows are read; the surrounding terminal UI and the
    account diagnostics below it are not."""
    text = (bytes(output).decode("utf-8", errors="replace")
            if isinstance(output, (bytes, bytearray)) else output)
    lines = []
    for line in text.replace("\r", "\n").splitlines():
        line = _ANSI.sub("", line)
        line = re.sub(r"\x1b(?:[()][0-9A-Z]|.)", "", line)
        lines.append("".join(c for c in line if c >= " " or c == "\t").strip())
    used = {}
    for index, line in enumerate(lines):
        key = next((v for label, v in _ROWS.items() if label in line), None)
        if not key:
            found = _PER_MODEL.search(line)
            # "all models" is the account-wide row and already has a key.
            if found and found.group(1).strip().lower() != "all models":
                key = "week:" + found.group(1).strip().lower().replace(" only", "")
        if not key:
            continue
        for candidate in lines[index + 1:index + 4]:
            if "used" not in candidate.lower():
                continue
            found = re.findall(r"(\d+(?:\.\d+)?)%", candidate)
            if found:
                used[key] = float(found[-1])
                break
    return used


def screen_cwd(state: dict) -> Path | None:
    """A project Claude Code already trusts; it refuses to start anywhere
    else. Override: $CLAUDE_USAGE_CWD."""
    override = os.environ.get("CLAUDE_USAGE_CWD")
    if override:
        path = Path(override).expanduser()
        return path if path.is_dir() else None
    projects = state.get("projects")
    if not isinstance(projects, dict):
        return None
    for raw, settings in reversed(list(projects.items())):
        if not isinstance(settings, dict):
            continue
        if settings.get("hasTrustDialogAccepted") is True or \
                settings.get("hasCompletedProjectOnboarding") is True:
            path = Path(raw).expanduser()
            if path.is_dir():
                return path
    return None


def read_screen(state: dict, timeout: float = CLAUDE_SCREEN_TIMEOUT,
                config_dir: str | None = None) -> dict:
    """Drive Claude Code's own /usage view and read the numbers off it.

    A pseudo-terminal in safe mode with the screen reader on, one built-in
    command, no prompt and no model call — so it costs nothing but the wait.
    Returns {} for anything that does not work, and the caller falls back to
    the cache file."""
    exe = which("claude")
    cwd = screen_cwd(state)
    if not exe or cwd is None or pty is None:
        return {}
    try:
        master, slave = pty.openpty()
    except OSError:
        return {}
    env = {**os.environ, "NO_COLOR": "1"}
    if config_dir:
        env["CLAUDE_CONFIG_DIR"] = str(Path(config_dir).expanduser())
    else:
        env.pop("CLAUDE_CONFIG_DIR", None)
    try:
        proc = subprocess.Popen([exe, "--safe-mode", "--ax-screen-reader"],
                                stdin=slave, stdout=slave, stderr=slave,
                                cwd=str(cwd), env=env, close_fds=True)
    except OSError:
        os.close(master)
        os.close(slave)
        return {}
    os.close(slave)
    sent, buffer = False, bytearray()
    started = time.monotonic()
    account_at, retried = None, False
    try:
        while time.monotonic() - started < timeout:
            try:
                readable, _, _ = select.select([master], [], [], 0.25)
            except OSError:
                break
            if readable:
                try:
                    chunk = os.read(master, 65536)
                except OSError:
                    break
                if len(buffer) < 262144:
                    buffer.extend(chunk[:262144 - len(buffer)])
            # It answers once the shell is up; the elapsed check covers a
            # build whose banner never says so.
            if not sent and (b"anual mode on" in buffer
                             or time.monotonic() - started >= 8.0):
                try:
                    os.write(master, b"/usage\r")
                    sent = True
                except OSError:
                    break
            if sent:
                found = parse_screen(buffer)
                # That section can come back rate limited, and offers a retry.
                if not retried and b"rate limited" in buffer:
                    try:
                        os.write(master, b"r")
                    except OSError:
                        pass
                    retried = True
                have_account = "five_hour" in found and "seven_day" in found
                if have_account and account_at is None:
                    account_at = time.monotonic()
                # Done once a per-model row lands, or once waiting for one
                # has cost more than it is worth.
                if have_account and (any(k.startswith("week:") for k in found)
                                     or time.monotonic() - account_at >= CLAUDE_SCREEN_GRACE):
                    return found
            if proc.poll() is not None:
                break
        return parse_screen(buffer)
    finally:
        if proc.poll() is None:
            proc.terminate()
            try:
                proc.wait(timeout=1)
            except subprocess.TimeoutExpired:
                proc.kill()
        os.close(master)


@soft("claude")
def claude(config: dict | None = None, *, path: Path | str = CLAUDE_CACHE,
           now_ms: float | None = None, screen=None, auth=None) -> Reading:
    """Probe the login, then the cache file for reset times and Claude Code's
    own /usage for the numbers.

    NO cache-freshness shortcut on the screen read. The per-model rows exist
    only on the /usage screen and are never written to the cache file, and
    reading /usage refreshes the cache as a side effect — a fresh cache would
    hide the very thing that refreshed it. The throttle is the only gate.

    ``config.config_dir`` selects the account: probe, cache file, and screen
    read all run against that CLAUDE_CONFIG_DIR. Log the second account in
    once with ``tarmac login <slug>`` (or CLAUDE_CONFIG_DIR=<dir> claude)."""
    config_dir = str((config or {}).get("config_dir") or "") or None
    status = (auth or cached_auth_status)(config_dir) if auth is None \
        else auth()
    if status.get("logged_in") is False:
        where = f" (CLAUDE_CONFIG_DIR={config_dir})" if config_dir else ""
        return Reading("claude", needs_login=True,
                       reason=f"not logged in{where} — run tarmac login")

    if config_dir and path == CLAUDE_CACHE:
        path = Path(config_dir).expanduser() / ".claude.json"
    p = Path(path).expanduser()
    if not p.is_file():
        return unknown("claude", f"{p} not found")
    state = json.loads(p.read_text(encoding="utf-8"))
    cached = parse_claude(state, now_ms)

    key = config_dir or ""
    measured_at = now()
    if screen is not None:
        used = screen(state)
    else:
        last = _SCREEN_LAST.get(key)
        fresh_for = (CLAUDE_SCREEN_EVERY_S
                     if last and any(k.startswith("week:") for k in last[1])
                     else CLAUDE_PARTIAL_EVERY_S)
        if last and time.monotonic() - last[0] < fresh_for:
            used = last[1]  # too soon to ask again
            measured_at = last[2]
            cached = last[3]
        else:
            used = read_screen(state, config_dir=config_dir) if config_dir \
                else read_screen(state)
            if used:
                _SCREEN_LAST[key] = (time.monotonic(), used, measured_at, cached)
    if not used:
        return cached  # keep the cache, and its age still rides on the entry
    windows, cache_windows = [], {w["label"]: w for w in cached.windows}
    per_model = [(k, k.split(":", 1)[1]) for k in used if k.startswith("week:")]
    for name, minutes in (("five_hour", 300), ("seven_day", 10080)):
        if name not in used:
            continue
        label = window_label(minutes)
        # The screen gives percentages, not reset times, so they come from
        # the cache — but only if still in the future. Better to say when it
        # resets is unknown than to pretend the reading is.
        resets = (cache_windows.get(label) or {}).get("resets_at")
        windows.append({**make_window(label, 100 - used[name],
                                   None if expired(resets) and iso(measured_at) >= iso(resets) else resets),
                        "duration_minutes": minutes, "bucket_id": "account",
                        "reset_precision_seconds": 1})
    if not windows:
        return cached
    # A per-model week is REPORTED but never drives the provider's scalar.
    # Fable's weekly running out says nothing about Opus, and the scalar is
    # what a consumer reads as "how much Claude is left".
    account = from_windows("claude", windows, as_of=measured_at,
                           raw=scrub({"source": "claude /usage"}))
    for key, model in sorted(per_model):
        account.windows.append({**make_window(f"weekly · {model}",
                                              100 - used[key], None),
                                "per_model": True, "bucket_id": model,
                                "duration_minutes": 10080})
    return account
