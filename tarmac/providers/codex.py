"""Codex: the app server's live rate limits, with the session files as a
fallback recording.

The session files are a RECORD, not a reading: the newest one is as old as
the last time Codex happened to write a token_count event. That was 23 hours
stale and showed 100% headroom while the account was actually at 11%. An
explicit reading over the stdio app server is the only way to know.
"""
import json
import math
import os
import selectors
import subprocess
import time
from pathlib import Path

from tarmac.model import (Reading, credit_summary, from_windows, iso,
                          make_window, now, scrub, soft, unknown,
                          window_label)
from tarmac.providers.creds import which

CODEX_SESSIONS = Path("~/.codex/sessions")
CODEX_SCAN_FILES = 5  # a just-started session has no token_count event yet

# Login-state probe, keyed by CODEX_HOME ("" = the default account).
CODEX_AUTH_EVERY_S = 300.0
_AUTH_LAST: dict[str, tuple[float, dict]] = {}


def _codex_env(codex_home: str | None) -> dict:
    env = dict(os.environ)
    if codex_home:
        env["CODEX_HOME"] = str(Path(codex_home).expanduser())
    else:
        env.pop("CODEX_HOME", None)
    return env


def auth_status(codex_home: str | None = None, runner=subprocess.run) -> dict:
    """``{"logged_in": bool|None, "checked_at": iso, "reason": str|None}``
    from ``codex login status``. Logged-out is affirmed ONLY on a non-zero
    exit whose output says "not logged in"; anything else (CLI missing,
    unexpected words) is None — a probe failure is never a logout."""
    exe = which("codex")
    checked_at = now()
    if not exe:
        return {"logged_in": None, "checked_at": checked_at,
                "reason": "codex is not installed"}
    try:
        result = runner([exe, "login", "status"], env=_codex_env(codex_home),
                        capture_output=True, text=True, encoding="utf-8",
                        errors="replace", timeout=15)
    except (OSError, subprocess.TimeoutExpired) as exc:
        return {"logged_in": None, "checked_at": checked_at,
                "reason": f"auth probe unavailable: {exc}"}
    text = f"{result.stdout}\n{result.stderr}".lower()
    if result.returncode == 0:
        return {"logged_in": True, "checked_at": checked_at, "reason": None}
    if "not logged in" in text:
        return {"logged_in": False, "checked_at": checked_at, "reason": None}
    return {"logged_in": None, "checked_at": checked_at,
            "reason": f"auth probe exited {result.returncode}"}


def cached_auth_status(codex_home: str | None = None) -> dict:
    key = str(codex_home or "")
    last = _AUTH_LAST.get(key)
    if last and time.monotonic() - last[0] < CODEX_AUTH_EVERY_S:
        return last[1]
    status = auth_status(codex_home)
    _AUTH_LAST[key] = (time.monotonic(), status)
    return status


@soft("codex")
def parse_codex(lines) -> Reading:
    """Last ``token_count`` event of a rollout JSONL: rate_limits from the
    live response headers. ``primary`` and ``secondary`` are both reported
    when present; each is named by its ``window_minutes``.

    ponytail: codex-cli owns ~/.codex/sessions and may change this format
    without notice — shape drift degrades to unknown."""
    last = None
    for line in lines:
        if '"token_count"' not in line:
            continue
        try:
            event = json.loads(line)
        except ValueError:
            continue
        payload = event.get("payload") or {}
        if payload.get("type") == "token_count" and payload.get("rate_limits"):
            last = event
    if last is None:
        return unknown("codex", "no token_count event with rate_limits")

    limits = last["payload"]["rate_limits"]
    as_of = iso(last.get("timestamp"))
    windows = []
    for slot in ("primary", "secondary"):
        w = limits.get(slot)
        if not isinstance(w, dict) or not isinstance(
                w.get("used_percent"), (int, float)):
            continue
        windows.append(make_window(window_label(w.get("window_minutes")),
                                   100 - float(w["used_percent"]),
                                   iso(w.get("resets_at"))))
    if not windows:
        return unknown("codex", "rate_limits has no window with used_percent")
    return from_windows("codex", windows, as_of=as_of, raw=scrub({
        "plan_type": limits.get("plan_type"),
        "rate_limit_reached_type": limits.get("rate_limit_reached_type"),
        "workspace_credits": _workspace_credits(limits.get("credits")),
        "acquisition": "session-record",
    }))


def _reset_credits(result: dict) -> dict | None:
    """Earned resets only. The authoritative count may exceed detail rows."""
    block = result.get("rateLimitResetCredits")
    if not isinstance(block, dict):
        return None
    entries = block.get("credits")
    live = [c for c in entries if isinstance(c, dict)
            and str(c.get("status", "")).lower() == "available"] \
        if isinstance(entries, list) else []
    count = block.get("availableCount")
    if not isinstance(count, int) or isinstance(count, bool) or count < 0:
        if not isinstance(entries, list):
            return None
        count = len(live)  # legacy servers with no authoritative count
    text = f"{count} banked reset" + ("" if count == 1 else "s")
    # Only a complete list can establish the earliest expiry.
    expiries = sorted(c["expiresAt"] for c in live
                      if isinstance(c.get("expiresAt"), (int, float))
                      and not isinstance(c["expiresAt"], bool)
                      and math.isfinite(c["expiresAt"]))
    soonest = iso(expiries[0]) if count == len(live) == len(expiries) and count else None
    return credit_summary(text + (f" · soonest expires {soonest[:10]}" if soonest else ""),
                          count, soonest)


def _workspace_credits(credits) -> dict | None:
    """Workspace balances never imply earned resets or purchase permission."""
    if not isinstance(credits, dict):
        return None
    try:
        balance = float(credits.get("balance"))
        if not math.isfinite(balance) or balance < 0:
            balance = None
    except (TypeError, ValueError):
        balance = None
    return {"balance": balance, "unlimited": credits.get("unlimited") is True,
            "has_credits": credits.get("hasCredits", credits.get("has_credits")) is True}


def parse_codex_live(result: dict) -> Reading:
    """Preserve each metered bucket. Names alone do not establish model coverage."""
    legacy = result.get("rateLimits")
    buckets = result.get("rateLimitsByLimitId")
    buckets = dict(buckets) if isinstance(buckets, dict) else {}
    if isinstance(legacy, dict):
        legacy_id = legacy.get("limitId")
        if legacy_id:
            buckets.setdefault(legacy_id, legacy)
        elif not buckets:
            buckets["legacy"] = legacy
    windows = []
    for bucket_id, limits in buckets.items():
        if not isinstance(limits, dict):
            continue
        for slot in ("primary", "secondary"):
            w = limits.get(slot)
            if w is None:  # absent is not an unlimited window
                continue
            w = w if isinstance(w, dict) else {}
            used = w.get("usedPercent")
            valid = isinstance(used, (int, float)) and not isinstance(used, bool) \
                and math.isfinite(used) and 0 <= used <= 100
            try:
                reset = iso(w.get("resetsAt"))
            except (ValueError, TypeError, OverflowError, OSError):
                reset, valid = None, False
            label = window_label(w.get("windowDurationMins"))
            window = make_window(label, 100 - used if valid else None, reset)
            window.update(id=f"{bucket_id}:{slot}", bucket_id=str(bucket_id),
                          duration_minutes=w.get("windowDurationMins"),
                          reset_precision_seconds=1)
            if not valid:
                window.update(stale=True, stale_reason="invalid quota window")
            windows.append(window)
    if not windows or all(w["remaining"] is None for w in windows):
        return unknown("codex", "no usable window in rate limits")
    reading = from_windows("codex", windows, as_of=now(), raw={
        "acquisition": "app-server", "credits": _reset_credits(result),
        "workspace_credits": _workspace_credits((legacy or {}).get("credits")),
    })
    return reading


def read_app_server(timeout: float = 20.0,
                    codex_home: str | None = None) -> dict:
    """Ask codex-cli for the account's limits over its stdio app server."""
    exe = which("codex")
    if not exe:
        raise RuntimeError("codex is not installed")
    proc = subprocess.Popen([exe, "app-server", "--stdio"],
                            stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                            stderr=subprocess.DEVNULL, text=True, bufsize=1,
                            env=_codex_env(codex_home))
    sel = None
    try:
        for message in ({"id": 1, "method": "initialize", "params": {
                            "clientInfo": {"name": "tarmac", "version": "1"},
                            "capabilities": {"experimentalApi": True}}},
                        {"method": "initialized"},
                        {"id": 2, "method": "account/rateLimits/read"}):
            proc.stdin.write(json.dumps(message, separators=(",", ":")) + "\n")
        proc.stdin.flush()
        sel = selectors.DefaultSelector()
        sel.register(proc.stdout, selectors.EVENT_READ)
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if not sel.select(max(0.0, deadline - time.monotonic())):
                break
            line = proc.stdout.readline()
            if not line:
                break
            try:
                message = json.loads(line)
            except ValueError:
                continue
            if message.get("id") != 2:
                continue
            if isinstance(message.get("error"), dict):
                raise RuntimeError("codex refused the quota request")
            if isinstance(message.get("result"), dict):
                return message["result"]
            raise RuntimeError("codex returned an unexpected response")
        raise RuntimeError("codex quota request timed out")
    finally:
        if sel is not None:
            sel.close()
        for stream in (proc.stdin, proc.stdout):
            if stream is not None:
                stream.close()
        if proc.poll() is None:
            proc.terminate()
            try:
                proc.wait(timeout=1)
            except subprocess.TimeoutExpired:
                proc.kill()


@soft("codex")
def codex(config: dict | None = None, *, sessions_dir: Path | str = CODEX_SESSIONS,
          reader=None, auth=None) -> Reading:
    """Ask the app server; fall back to the session files it used to scrape.
    The fallback is not equivalent and says so — it is a recording, and its
    age rides on the entry — but a machine without codex-cli on PATH should
    still see whatever was last written rather than nothing.

    ``config.codex_home`` selects the account: probe, app server, and the
    session fallback all run against that CODEX_HOME."""
    codex_home = str((config or {}).get("codex_home") or "") or None
    status = (auth or cached_auth_status)(codex_home) if auth is None \
        else auth()
    if status.get("logged_in") is False:
        where = f" (CODEX_HOME={codex_home})" if codex_home else ""
        return Reading("codex", needs_login=True,
                       reason=f"not logged in{where} — run tarmac login")
    if codex_home and sessions_dir == CODEX_SESSIONS:
        sessions_dir = Path(codex_home).expanduser() / "sessions"
    try:
        result = reader() if reader is not None \
            else read_app_server(codex_home=codex_home)
        live = parse_codex_live(result)
        if live.known:
            return live
        why = live.reason or "app server gave no reading"
    except Exception as exc:
        why = str(exc)
    fallback = _from_sessions(sessions_dir)
    if fallback.known:
        fallback.reason = "historical session record; live quota read failed"
        return fallback
    return unknown("codex", f"{why}; and no usable session snapshot")


def _from_sessions(sessions_dir: Path | str = CODEX_SESSIONS) -> Reading:
    d = Path(sessions_dir).expanduser()
    files = sorted(d.glob("**/rollout-*.jsonl"),
                   key=lambda p: p.stat().st_mtime, reverse=True)
    if not files:
        return unknown("codex", f"no rollout-*.jsonl under {d}")
    result = unknown("codex", "no token_count event with rate_limits")
    for path in files[:CODEX_SCAN_FILES]:
        with path.open(encoding="utf-8", errors="replace") as fh:
            result = parse_codex(fh)
        if result.known:
            return result
    return result  # newest files carried no usable snapshot; last reason wins
