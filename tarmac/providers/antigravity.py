"""Antigravity (Google AI Pro): the ``/usage`` screen of the official CLI.

Google's terms forbid third-party software from using the Antigravity OAuth
session or its Cloud Code Assist endpoints, so this adapter never touches the
keyring and never makes an HTTP call. It drives the documented ``agy`` binary
in a pseudo-terminal, sends the built-in ``/usage`` command, and reads the
numbers off the screen — no prompt, no model call, no tokens spent.

The plan meters MODEL GROUPS, not models: Gemini Flash and Gemini Pro share
one pair of windows, Claude and GPT-OSS share another. Each group reports a
weekly and a five-hour window, already stated as PERCENT REMAINING with a
countdown rather than a reset time.
"""
import fcntl
import os
import re
import select
import struct
import subprocess
import termios
import time
from pathlib import Path

from tarmac.model import (from_windows, iso, make_window, now, scrub, soft,
                          unknown, window_label)
from tarmac.providers.creds import which

try:
    import pty
except ImportError:
    pty = None

# The TUI costs ~15s of wall clock per read and the poller ticks every five
# minutes. Quota moves in single digits per hour; ten minutes is plenty.
SCREEN_EVERY_S = 600.0
SCREEN_TIMEOUT = 45.0
_SCREEN_LAST: dict[str, tuple[float, list, str]] = {}

_ANSI = re.compile(r"\x1b(?:\[[0-?]*[ -/]*[@-~]|\][^\x07]*(?:\x07|\x1b\\))")
_GROUP = re.compile(r"^([A-Z][A-Z /+-]*(?:AND [A-Z-]+ )?)MODELS\b")
_ROW = re.compile(r"(Weekly|Five Hour) Limit Remaining")
_PERCENT = re.compile(r"(\d+(?:\.\d+)?)%")
_REFRESH = re.compile(r"Refreshes in ((?:\d+[dhm]\s*)+)")
# A window nothing has been drawn from states no reset — there is none.
_UNTOUCHED = "Quota available"
# Once a group's weekly limit is spent, its five-hour row prints why that
# limit "does not currently apply" in place of a bar.
_DISABLED = "Disabled:"
_SPAN = re.compile(r"(\d+)([dhm])")
_ROW_MINUTES = {"weekly": 10080, "five hour": 300}
# The footnote under the last group: the view has finished painting.
_PAINTED = b"Within each group"
_UNIT_SECONDS = {"d": 86400, "h": 3600, "m": 60}


def group_slug(heading: str) -> str:
    """'CLAUDE AND GPT ' -> 'claude+gpt'. The group names are the plan's, so
    a new one appears in the reading without a code change."""
    return heading.strip().lower().replace(" and ", "+").replace(" ", "+")


def refresh_seconds(text: str) -> float:
    return sum(int(n) * _UNIT_SECONDS[u] for n, u in _SPAN.findall(text))


def parse_screen(output, now_s: float | None = None) -> list[dict]:
    """Windows from the ``/usage`` view. The screen repaints, so a later
    frame of the same window wins."""
    text = (bytes(output).decode("utf-8", errors="replace")
            if isinstance(output, (bytes, bytearray)) else output)
    lines = [_ANSI.sub("", line).strip()
             for line in text.replace("\r", "\n").splitlines()]
    base = time.time() if now_s is None else now_s
    found, group, disabled = {}, None, set()
    for index, line in enumerate(lines):
        heading = _GROUP.match(line)
        if heading:
            group = group_slug(heading.group(1))
            continue
        row = _ROW.search(line)
        if not row or group is None:
            continue
        minutes = _ROW_MINUTES[row.group(1).lower()]
        percent = countdown = None
        untouched = off = False
        # The rows are spaced out by blank lines: bar, then countdown. The
        # next row or group starts where this one ends, so a row with no bar
        # of its own never takes the next one's.
        for candidate in lines[index + 1:index + 7]:
            if _ROW.search(candidate) or _GROUP.match(candidate):
                break
            match = _PERCENT.search(candidate)
            if percent is None and match:
                # The bar line carries the decimal; the prose line rounds.
                percent = float(match.group(1))
            match = _REFRESH.search(candidate)
            if countdown is None and match:
                countdown = match.group(1)
            untouched = untouched or _UNTOUCHED in candidate
            off = off or _DISABLED in candidate
        # A disabled five-hour limit is not a window right now; the spent
        # weekly one is the group's runway until it resets.
        if off and minutes == _ROW_MINUTES["five hour"]:
            disabled.add(group)
            continue
        # A frame caught mid-repaint has the bar and not the line under it.
        if percent is None or not (countdown or untouched):
            continue
        label = f"{window_label(minutes)} · {group}"
        found[label] = {**make_window(
            label, min(percent, 100.0),
            iso(base + refresh_seconds(countdown)) if countdown else None),
            "bucket_id": group, "duration_minutes": minutes,
            "reset_precision_seconds": 60}
    for name in disabled:
        weekly = found.get(f"{window_label(_ROW_MINUTES['weekly'])} · {name}")
        if weekly:
            weekly["five_hour_disabled"] = True
    return list(found.values())


def paired(windows: list) -> list:
    """Only groups that reported BOTH of their windows. A frame caught
    half-painted drops a bar, and a missing bar reads as a window that does
    not exist rather than one that was not drawn yet. A spent weekly window
    whose five-hour row was drawn as disabled is a whole group on its own."""
    groups = {}
    for window in windows:
        groups.setdefault(window["label"].split(" · ")[-1], []).append(window)
    return [w for rows in groups.values()
            if len(rows) == 2 or (len(rows) == 1 and rows[0].get("five_hour_disabled"))
            for w in rows]


def read_screen(timeout: float = SCREEN_TIMEOUT, cwd: str | None = None) -> list:
    """Drive ``agy``'s own ``/usage`` view and read it. Returns [] for
    anything that does not work; the caller turns that into a reason."""
    exe = which(os.environ.get("TARMAC_AGY", "agy"))
    if not exe or pty is None:
        return []
    try:
        master, slave = pty.openpty()
        # A terminal of no size draws nothing: the TUI needs a window.
        fcntl.ioctl(slave, termios.TIOCSWINSZ,
                    struct.pack("HHHH", 60, 120, 0, 0))
    except OSError:
        return []
    try:
        proc = subprocess.Popen(
            [exe], stdin=slave, stdout=slave, stderr=slave, close_fds=True,
            cwd=cwd or os.environ.get("TMPDIR") or "/tmp",
            env={**os.environ, "NO_COLOR": "1"}, start_new_session=True)
    except OSError:
        os.close(master)
        os.close(slave)
        return []
    os.close(slave)
    sent, buffer = 0.0, bytearray()
    started = time.monotonic()
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
            # The prompt is drawn once the account line lands; the elapsed
            # check covers a build whose banner never says so.
            if not sent and (b"for shortcuts" in buffer
                             or time.monotonic() - started >= 10.0):
                try:
                    os.write(master, b"/usage")
                    sent = time.monotonic()
                except OSError:
                    break
            # The command palette needs the keystrokes and the Return apart.
            elif sent and sent > 0 and time.monotonic() - sent >= 1.5:
                try:
                    os.write(master, b"\r")
                except OSError:
                    break
                sent = -1.0
            if sent < 0 and _PAINTED in buffer:
                windows = paired(parse_screen(buffer))
                if windows:
                    return windows
            if proc.poll() is not None:
                break
        return paired(parse_screen(buffer))
    finally:
        if proc.poll() is None:
            proc.terminate()
            try:
                proc.wait(timeout=1)
            except subprocess.TimeoutExpired:
                proc.kill()
        os.close(master)


@soft("antigravity")
def antigravity(config: dict | None = None, *, screen=None):
    """Every group window the plan reports; the tightest live one is the
    scalar. ``config.cwd`` picks the directory the CLI starts in."""
    cwd = str((config or {}).get("cwd") or "") or None
    key = cwd or ""
    measured_at = now()
    last = _SCREEN_LAST.get(key)
    if screen is not None:
        windows = screen()
    elif last and time.monotonic() - last[0] < SCREEN_EVERY_S:
        windows, measured_at = last[1], last[2]
    else:
        windows = read_screen(cwd=cwd)
        if windows:
            _SCREEN_LAST[key] = (time.monotonic(), windows, measured_at)
    if not windows:
        return unknown("antigravity",
                       "agy /usage gave no quota rows — is `agy` installed "
                       "and logged in?")
    return from_windows("antigravity", windows, as_of=measured_at,
                        raw=scrub({"source": "agy /usage"}))
