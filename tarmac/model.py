"""The reading model: windows, freshness, staleness, burn, and the hold rule.

A provider reports WINDOWS, not one number: a 5-hour limit and a weekly limit
are two facts about one plan. A window has no ``limit`` field — every window
is a percentage of itself, so a limit is always 100 and says nothing.

Failure model: every adapter fails soft. Unreachable, unauthorized, garbage
JSON, or a local file whose shape drifted all yield a ``Reading`` with
``remaining=None`` and a ``reason``. Adapters never raise.

Staleness: a window whose reset has already passed says NOTHING about the
present — it refilled at that moment and has been drawn down since by an
amount nobody measured. ``as_of_now`` blanks it and says why.

Hold rule: only a fresh, definitive zero blocks work. Stale or unknown never
blocks.

Two provider kinds: a ``plan`` provider is a subscription and shows
consumption against its windows, never a price. An ``api`` provider is billed
per token and its money is its runway.
"""
import functools
import math
import re
import time
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone

# Subscriptions. Everything else is billed per token and may show money.
PLAN_PROVIDERS = frozenset({"antigravity", "claude", "codex", "kimi",
                            "meta", "minimax", "xai"})

# Windows are named by their length, so one labeller serves every provider.
WINDOW_LABELS = {60: "hourly", 300: "5h", 1440: "daily", 10080: "weekly"}

_SECRET_NAME = re.compile(
    r"key|token|secret|auth|password|credential|cookie|session", re.I)
_SECRET_VALUE = re.compile(r"^(sk-|Bearer\s)|^[A-Za-z0-9_\-]{32,}$")


@dataclass
class Reading:
    """One provider observation: the scalar fields are the tightest live
    window (or an api provider's balance and currency); ``windows`` carries
    every window the provider reports. ``remaining is None`` means unknown.
    ``needs_login`` is an affirmative logged-out signal, distinct from a
    merely stale cache."""
    provider: str
    remaining: float | None = None
    unit: str = ""
    resets_at: str | None = None
    raw: dict = field(default_factory=dict)
    as_of: str | None = None
    reason: str | None = None
    kind: str = "api"
    stale: bool = False
    windows: list = field(default_factory=list)
    needs_login: bool = False

    def __post_init__(self) -> None:
        self.kind = kind_of(self.provider)

    @property
    def known(self) -> bool:
        return self.remaining is not None

    def as_dict(self) -> dict:
        return asdict(self)


def kind_of(provider: str) -> str:
    """``plan`` never shows money; ``api`` is billed per token and may."""
    return "plan" if provider in PLAN_PROVIDERS else "api"


def unknown(provider: str, reason: str) -> Reading:
    return Reading(provider, reason=reason)


def soft(provider: str):
    """Every exception becomes an unknown-with-reason. No adapter raises."""
    def wrap(fn):
        @functools.wraps(fn)
        def inner(*a, **kw):
            try:
                return fn(*a, **kw)
            except Exception as exc:  # noqa: BLE001 — that is the contract
                return unknown(provider, f"{type(exc).__name__}: {exc}")
        return inner
    return wrap


def now() -> str:
    return datetime.now(timezone.utc).isoformat()


def scrub(obj):
    """Redact anything credential-shaped before it reaches raw."""
    if isinstance(obj, dict):
        return {k: "[redacted]" if _SECRET_NAME.search(str(k)) else scrub(v)
                for k, v in obj.items()}
    if isinstance(obj, list):
        return [scrub(v) for v in obj]
    if isinstance(obj, str) and _SECRET_VALUE.search(obj):
        return "[redacted]"
    return obj


def iso(value) -> str | None:
    """ISO-8601 UTC from an ISO string, epoch seconds, or epoch millis."""
    if value is None or value == "":
        return None
    if isinstance(value, bool):
        raise ValueError("timestamp is a bool")
    if isinstance(value, (int, float)):
        secs = value / 1000 if value > 1e11 else value
        dt = datetime.fromtimestamp(secs, timezone.utc)
    else:
        dt = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def seconds_until(iso_text: str | None) -> float | None:
    if not iso_text:
        return None
    dt = datetime.fromisoformat(iso_text.replace("Z", "+00:00"))
    return (dt - datetime.now(timezone.utc)).total_seconds()


def expired(iso_text: str | None) -> bool:
    left = seconds_until(iso_text)
    return left is not None and left <= 0


def age_hours(iso_text: str | None, now_s: float | None = None) -> float | None:
    if not iso_text:
        return None
    stamped = datetime.fromisoformat(iso_text.replace("Z", "+00:00")).timestamp()
    return max(0.0, ((time.time() if now_s is None else now_s) - stamped) / 3600)


def age_text(hours: float | None) -> str:
    if hours is None:
        return "unknown age"
    return f"{hours:.0f}h" if hours >= 1 else f"{hours * 60:.0f}m"


def window_label(minutes) -> str:
    """Named lengths win; anything else is rendered in the largest whole unit
    it divides into, so 240 minutes reads '4h' rather than '240m'."""
    if not isinstance(minutes, (int, float)) or isinstance(minutes, bool):
        return "window"
    m = int(minutes)
    if m in WINDOW_LABELS:
        return WINDOW_LABELS[m]
    if m > 0 and m % 1440 == 0:
        return f"{m // 1440}d"
    if m > 0 and m % 60 == 0:
        return f"{m // 60}h"
    return f"{m}m"


def make_window(label: str, remaining: float, resets_at: str | None) -> dict:
    return {"label": label, "remaining": remaining, "unit": "percent",
            "resets_at": resets_at, "stale": expired(resets_at),
            "stale_reason": None}


def as_of_now(window: dict) -> dict:
    """A window as it stands NOW. An expired window reports unknown, and says
    why: the number from before the reset is not stale, it is wrong."""
    if not expired(window.get("resets_at")):
        return window
    return {**window, "remaining": None, "stale": True,
            "stale_reason": "reset since this was read"}


def from_windows(provider: str, windows: list[dict], as_of: str | None = None,
                 raw: dict | None = None) -> Reading:
    """Use the tightest live window as the compact scalar; retain all windows."""
    live = [w for w in windows if not expired(w["resets_at"])] or windows
    known = [w for w in live if isinstance(w.get("remaining"), (int, float))
             and not isinstance(w["remaining"], bool) and math.isfinite(w["remaining"])]
    if not known:
        return Reading(provider, windows=windows, as_of=as_of, raw=raw or {},
                       stale=True, reason="no usable window")
    tight = min(known, key=lambda w: w["remaining"])
    return Reading(provider, remaining=tight["remaining"], unit=tight["unit"],
                   resets_at=tight["resets_at"], as_of=as_of,
                   stale=any(w["stale"] for w in windows), windows=windows,
                   raw=raw or {})


def until_text(iso_text: str | None) -> str:
    left = seconds_until(iso_text)
    if left is None:
        return "-"
    if left <= 0:
        return "now"
    minutes = int(left // 60)
    days, minutes = divmod(minutes, 1440)
    hours, minutes = divmod(minutes, 60)
    parts = ([f"{days}d"] if days else []) + \
            ([f"{hours}h"] if days or hours else []) + [f"{minutes}m"]
    return "in " + " ".join(parts)


def number(value) -> float:
    """Accepts decimal-as-string numbers too."""
    return float(value)


def percent(remaining, total) -> float | None:
    """A window as a percentage of itself, or None when either side is junk."""
    try:
        remaining, total = number(remaining), number(total)
    except (TypeError, ValueError):
        return None
    if total <= 0:
        return None
    return max(0.0, min(100.0, remaining / total * 100))


def credit_summary(text: str, count=None, expires_at=None) -> dict:
    """The only banked-credit metadata safe and useful outside an adapter."""
    count = count if isinstance(count, int) and not isinstance(count, bool) \
        and count >= 0 else None
    return {"text": text, "count": count,
            "expires_at": expires_at if isinstance(expires_at, str) else None}


def credits_text(raw) -> str | None:
    """Return a provider's already-scrubbed reset-credit summary, if any."""
    value = raw.get("credits") if isinstance(raw, dict) else None
    text = value.get("text") if isinstance(value, dict) else value
    return text if isinstance(text, str) and text else None


def source_hold(reading: dict | None, *, now: datetime | None = None) -> str | None:
    """Only a fresh, definitive zero is allowed to hold queued work."""
    if not reading or not reading.get("definitive"):
        return None
    remaining = reading.get("remaining")
    if remaining is None or remaining > 0:
        return None
    try:
        fresh_until = datetime.fromisoformat(reading["fresh_until"])
    except (KeyError, TypeError, ValueError):
        return None
    current = now or datetime.now(timezone.utc)
    if fresh_until.tzinfo is None:
        fresh_until = fresh_until.replace(tzinfo=timezone.utc)
    reset = reading.get("resets_at")
    try:
        reset_at = datetime.fromisoformat(str(reset).replace("Z", "+00:00")) \
            if reset else None
        if reset_at is not None and reset_at.tzinfo is None:
            reset_at = reset_at.replace(tzinfo=timezone.utc)
    except ValueError:
        reset_at = None
    if fresh_until <= current or (reset_at is not None and reset_at <= current):
        return None
    return "runway exhausted until " + (reading.get("resets_at") or "the next poll")
