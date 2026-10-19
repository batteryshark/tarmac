"""Small HTTPS providers: DeepSeek, Z.ai, Kimi, MiniMax, OpenRouter (two
calls), Meta.

Each adapter takes the source's config dict (which may carry ``auth_entry``
or ``key_env`` credential overrides) and returns a ``Reading``.
"""
import json
import os
import urllib.error
import urllib.request
from datetime import datetime, timedelta, timezone

from tarmac.model import (Reading, from_windows, iso, make_window, number,
                          percent, scrub, soft, unknown, window_label)
from tarmac.providers.creds import (OPENCODE_AUTH, HTTP_TIMEOUT, api_key,
                                    fetch, get_json)

DEEPSEEK_BALANCE_URL = "https://api.deepseek.com/user/balance"

# The wallet behind per-token Z.ai billing. Undocumented — it is what the
# billing page itself calls — but it takes the ordinary API key, and it needs
# the account's own customer id, which the source carries in its config.
ZAI_BALANCE_URL = \
    "https://api.z.ai/api/platform-charge-zai/business/accountBalance"

# Kimi's coding plan is a separate surface from the Moonshot open platform,
# with its own key (sk-kimi-…). Undocumented endpoint, verified 2026-08-13.
KIMI_USAGE_URL = "https://api.kimi.com/coding/v1/usages"

# MiniMax Token Plan. Note the www host — the api host does not serve it.
MINIMAX_USAGE_URL = "https://www.minimax.io/v1/token_plan/remains"
MINIMAX_MODEL = "general"  # the coding models; the other row is video

# /credits is the account balance and documents a MANAGEMENT key (an
# ordinary key gets 403); /key is the calling key's own cap and usage.
OPENROUTER_CREDITS_URL = "https://openrouter.ai/api/v1/credits"
OPENROUTER_KEY_URL = "https://openrouter.ai/api/v1/key"

# Muse Code (Meta Model API) subscription. There is no usage endpoint: the
# 5-hour and weekly windows ride along as an SSE event on every streamed
# response, so the probe is a minimal generation (the server floors
# max_output_tokens at 16) that costs a rounding error of the window it reads.
META_RESPONSES_URL = "https://api.meta.ai/v1/responses"
META_PROBE_MODEL = "muse-spark-1.3"

# The window is described rather than named: {"duration": 300,
# "timeUnit": "TIME_UNIT_MINUTE"} is the same 5-hour window Claude reports.
_TIME_UNIT_MINUTES = {"TIME_UNIT_MINUTE": 1, "TIME_UNIT_HOUR": 60,
                      "TIME_UNIT_DAY": 1440, "TIME_UNIT_WEEK": 10080}


# --- DeepSeek ---------------------------------------------------------------

@soft("deepseek")
def parse_deepseek(data: dict) -> Reading:
    infos = (data or {}).get("balance_infos") or []
    if not infos:
        return unknown("deepseek", "no balance_infos in response")
    info = next((i for i in infos if i.get("currency") == "USD"), infos[0])
    return Reading(
        "deepseek",
        remaining=number(info["total_balance"]),
        unit=info.get("currency") or "?",
        raw=scrub({
            "is_available": (data or {}).get("is_available"),
            "granted_balance": info.get("granted_balance"),
            "topped_up_balance": info.get("topped_up_balance"),
        }),
    )


@soft("deepseek")
def deepseek(config: dict | None = None, *, auth_path=OPENCODE_AUTH,
             url: str = DEEPSEEK_BALANCE_URL) -> Reading:
    """Prepaid balance — no window, so no resets_at."""
    data, err = get_json(url, "deepseek", config, auth_path=auth_path)
    return unknown("deepseek", err) if err else parse_deepseek(data)


# --- Z.ai wallet ------------------------------------------------------------

@soft("zai")
def parse_zai(data: dict) -> Reading:
    payload = (data or {}).get("data") or {}
    accounts = payload.get("accountList") or []
    total = payload.get("totalBalance")
    if total is None and not accounts:
        return unknown("zai", "no balance in response")
    if total is None:
        total = accounts[0].get("balance")
    frozen = sum(number(a.get("frozenBalance") or 0) for a in accounts)
    return Reading(
        "zai",
        remaining=number(total),
        unit="USD",
        raw=scrub({"frozen_balance": frozen,
                   "accounts": len(accounts),
                   "is_ka": payload.get("isKa")}),
    )


@soft("zai")
def zai(config: dict | None = None, *, auth_path=OPENCODE_AUTH,
        url: str = ZAI_BALANCE_URL) -> Reading:
    """Prepaid wallet — the per-token balance, not the coding-plan quota."""
    customer = str((config or {}).get("customer_id") or "").strip()
    if not customer:
        return unknown("zai", "set config customer_id for this Z.ai account")
    key, source = api_key("zai", config, auth_path)
    if not key:
        return unknown("zai", source)
    _, body, err = fetch(
        url, key, {"Content-Type": "application/json;charset=UTF-8"},
        json.dumps({"customerId": customer}).encode("utf-8"))
    if err:
        return unknown("zai", err)
    try:
        data = json.loads(body or b"{}")
    except ValueError:
        # Never echo the body: it can carry the key back.
        return unknown("zai", "response was not JSON")
    return parse_zai(data)


# --- Kimi coding plan -------------------------------------------------------

@soft("kimi")
def parse_kimi(data: dict) -> Reading:
    """``limits[]``: one entry per rolling window. ``usage`` is a second
    window, not a summary of the first — on a Kimi For Coding plan it carries
    the plan-wide weekly quota with its own reset. ``usage`` states REMAINING
    beside its limit; ``used`` is still honoured if it ever appears.

    ponytail: ``usage`` states no duration, so its length is assumed weekly —
    what the plan sells. If Kimi ever puts a ``window`` beside ``usage``,
    label it from that instead.
    """
    windows = []
    for entry in (data or {}).get("limits") or []:
        if not isinstance(entry, dict):
            continue
        window, detail = entry.get("window") or {}, entry.get("detail") or {}
        pct = percent(detail.get("remaining"), detail.get("limit"))
        if pct is None:
            continue
        minutes = _TIME_UNIT_MINUTES.get(window.get("timeUnit"), 0) * \
            (window.get("duration") or 0)
        windows.append(make_window(window_label(minutes or None), pct,
                                   iso(detail.get("resetTime"))))
    usage = (data or {}).get("usage") or {}
    if not any(w["label"] == "weekly" for w in windows):
        left = usage.get("remaining")
        if left is None and usage.get("used") is not None:
            try:
                left = number(usage["limit"]) - number(usage["used"])
            except (KeyError, TypeError, ValueError):
                left = None
        pct = None if left is None else percent(left, usage.get("limit"))
        if pct is not None:  # 0% is a reading, and the one that matters
            windows.append(make_window("weekly", pct,
                                       iso(usage.get("resetTime"))))
    if not windows:
        return unknown("kimi", "no usable window in limits[] or usage")
    return from_windows("kimi", windows, raw=scrub({
        "membership": ((data or {}).get("user") or {})
        .get("membership", {}).get("level"),
        "plan": (data or {}).get("subType"),
    }))


@soft("kimi")
def kimi(config: dict | None = None, *, auth_path=OPENCODE_AUTH,
         url: str = KIMI_USAGE_URL) -> Reading:
    """Coding-plan quota (Kimi For Coding), not the open-platform balance."""
    data, err = get_json(url, "kimi", config, auth_path=auth_path)
    return unknown("kimi", err) if err else parse_kimi(data)


# --- MiniMax Token Plan -----------------------------------------------------

@soft("minimax")
def parse_minimax(data: dict) -> Reading:
    """``model_remains[]``: one row per model family, each carrying a rolling
    window and a weekly window as REMAINING PERCENTAGES with epoch-millis
    ends. ``base_resp.status_code`` is MiniMax's real error channel — a
    rejected request still answers 200."""
    status = ((data or {}).get("base_resp") or {}).get("status_code")
    if status not in (None, 0):
        return unknown("minimax", f"minimax refused the request (status {status})")
    rows = [r for r in ((data or {}).get("model_remains") or [])
            if isinstance(r, dict)]
    row = next((r for r in rows if r.get("model_name") == MINIMAX_MODEL), None) \
        or (rows[0] if rows else None)
    if row is None:
        return unknown("minimax", "no model_remains in response")
    windows = []
    for percent_key, start_key, end_key in (
            ("current_interval_remaining_percent", "start_time", "end_time"),
            ("current_weekly_remaining_percent", "weekly_start_time",
             "weekly_end_time")):
        pct = row.get(percent_key)
        if not isinstance(pct, (int, float)) or isinstance(pct, bool):
            continue
        minutes = None
        if isinstance(row.get(start_key), (int, float)) and \
                isinstance(row.get(end_key), (int, float)):
            minutes = round((row[end_key] - row[start_key]) / 60000)
        windows.append(make_window(window_label(minutes), float(pct),
                                   iso(row.get(end_key))))
    if not windows:
        return unknown("minimax", "no remaining-percent field in model_remains")
    return from_windows("minimax", windows,
                        raw=scrub({"model_name": row.get("model_name")}))


@soft("minimax")
def minimax(config: dict | None = None, *, auth_path=OPENCODE_AUTH,
            url: str = MINIMAX_USAGE_URL) -> Reading:
    data, err = get_json(url, "minimax", config, auth_path=auth_path)
    return unknown("minimax", err) if err else parse_minimax(data)


# --- OpenRouter -------------------------------------------------------------

def _usd_window(label: str, remaining: float | None,
                resets_at: str | None = None, note: str | None = None) -> dict:
    """A money window. ``remaining=None`` with a ``note`` is a fact we could
    not state as a number ("no cap", "needs a management key"); the note
    rides in ``stale_reason``, the one free-text field a window carries."""
    return {"label": label, "remaining": remaining, "unit": "USD",
            "resets_at": resets_at, "stale": False, "stale_reason": note}


def _next_reset(kind) -> str | None:
    """The next UTC boundary of a key's ``limit_reset`` (daily, weekly from
    Monday, monthly). Anything else — including null, "never" — is None."""
    now_ = datetime.now(timezone.utc).replace(hour=0, minute=0, second=0,
                                               microsecond=0)
    if kind == "daily":
        nxt = now_ + timedelta(days=1)
    elif kind == "weekly":
        nxt = now_ + timedelta(days=7 - now_.weekday())
    elif kind == "monthly":
        nxt = (now_.replace(day=1) + timedelta(days=32)).replace(day=1)
    else:
        return None
    return nxt.strftime("%Y-%m-%dT%H:%M:%SZ")


def _credits_window(data, err) -> tuple[dict, dict]:
    """Account-level balance: total purchased minus total used."""
    label = "account credits"
    if err:
        if err.startswith(("http 401", "http 403")):
            err = f"needs an OpenRouter management key ({err})"
        return _usd_window(label, None, note=err), {"credits_error": err}
    try:
        d = data["data"]
        total, used = number(d["total_credits"]), number(d["total_usage"])
    except (KeyError, TypeError, ValueError):
        note = "credits response lacks total_credits/total_usage"
        return _usd_window(label, None, note=note), {"credits_error": note}
    return (_usd_window(label, round(total - used, 6)),
            {"total_credits": total, "total_usage": used})


def _cap_window(data, err) -> tuple[dict, dict]:
    """The calling key's own spend cap. ``limit: null`` is no cap at all."""
    label = "key cap"
    if err:
        return _usd_window(label, None, note=err), {"cap_error": err}
    try:
        d = data["data"]
        limit, left = d.get("limit"), d.get("limit_remaining")
        raw = {"limit": None if limit is None else number(limit),
               "limit_remaining": None if left is None else number(left),
               "limit_reset": d.get("limit_reset"),
               "usage": None if d.get("usage") is None else number(d["usage"]),
               "is_free_tier": d.get("is_free_tier")}
    except (AttributeError, KeyError, TypeError, ValueError):
        note = "key response lacks a data object"
        return _usd_window(label, None, note=note), {"cap_error": note}
    if limit is None:
        return _usd_window(label, None, note="no cap"), raw
    if left is None:
        note = "cap set but limit_remaining missing"
        return _usd_window(label, None, note=note), raw
    budget = next((b for b in d.get("budgets") or []
                   if isinstance(b, dict) and b.get("scope") == "key_limit"), {})
    resets = iso(budget.get("resets_at")) or _next_reset(d.get("limit_reset"))
    return _usd_window(label, round(raw["limit_remaining"], 6), resets), raw


@soft("openrouter")
def parse_openrouter(credits: dict | None, key: dict | None = None, *,
                     credits_err: str | None = None,
                     key_err: str | None = None) -> Reading:
    """Two money facts on one reading: the account balance (``/credits``)
    and the calling key's cap (``/key``). Either may be missing; the scalar
    is the tighter of whichever are numbers. ``key`` and ``key_err`` both
    None means ``/key`` was not asked, and no cap window is reported."""
    windows, raw = [], {}
    for data, err, build in ((credits, credits_err, _credits_window),
                             (key, key_err, _cap_window)):
        if data is None and err is None and build is _cap_window:
            continue
        window, facts = build(data, err)
        windows.append(window)
        raw.update(facts)
    known = [w for w in windows if w["remaining"] is not None]
    if not known:
        return Reading("openrouter", windows=windows, raw=scrub(raw),
                       reason="; ".join(f"{w['label']}: {w['stale_reason']}"
                                        for w in windows))
    tight = min(known, key=lambda w: w["remaining"])
    return Reading("openrouter", remaining=tight["remaining"], unit="USD",
                   resets_at=tight["resets_at"], windows=windows,
                   raw=scrub(raw))


def _openrouter_get(url: str, key: str | None) -> tuple[dict | None, str | None]:
    """(data, error) for one request. The key lives in this frame only."""
    _, body, err = fetch(url, key)
    if err:
        return None, err
    try:
        return json.loads(body or b"{}"), None
    except ValueError:
        # Never echo the body: it can carry the key back.
        return None, "response was not JSON"


def _openrouter_management_key(config: dict, auth_path) -> str | None:
    """An optional management key for ``/credits``: ``config.
    management_auth_entry`` (an OpenCode store entry) or the variable named
    by ``config.management_key_env`` (default OPENROUTER_MANAGEMENT_KEY)."""
    env = str(config.get("management_key_env") or "OPENROUTER_MANAGEMENT_KEY")
    entry = config.get("management_auth_entry")
    if entry:
        return api_key("openrouter", {"auth_entry": entry, "key_env": env},
                       auth_path)[0]
    return os.environ.get(env, "").strip() or None


@soft("openrouter")
def openrouter(config: dict | None = None, *, auth_path=OPENCODE_AUTH,
               url: str = OPENROUTER_CREDITS_URL,
               key_url: str = OPENROUTER_KEY_URL) -> Reading:
    """Account credits and this key's cap. ``/credits`` documents a
    management key; without one configured the ordinary key is tried and a
    401/403 degrades only that window."""
    config = config or {}
    found, source = api_key("openrouter", config, auth_path)
    if not found:
        return unknown("openrouter", source)
    found = None  # each request below resolves its own key
    key_data, key_err = _openrouter_get(
        key_url, api_key("openrouter", config, auth_path)[0])
    credits, credits_err = _openrouter_get(
        url, _openrouter_management_key(config, auth_path)
        or api_key("openrouter", config, auth_path)[0])
    return parse_openrouter(credits, key_data, credits_err=credits_err,
                            key_err=key_err)


# --- Meta Model API (Muse Code subscription) ---------------------------------

@soft("meta")
def parse_meta(data: dict) -> Reading:
    """The ``response.subscription_usage`` event: one tight window (5h on this
    plan, named by ``window_duration_mins``) plus the plan-wide weekly, both
    stated as USED percentages with epoch-second resets."""
    sub = (data or {}).get("subscription")
    if not isinstance(sub, dict) or not sub:
        return unknown("meta", "no subscription block in response")
    windows = []
    for name, entry in (("window", sub.get("window")),
                        ("weekly", sub.get("weekly"))):
        if not isinstance(entry, dict):
            continue
        used = entry.get("used_percent")
        if not isinstance(used, (int, float)) or isinstance(used, bool):
            continue
        label = "weekly" if name == "weekly" else \
            window_label(entry.get("window_duration_mins"))
        windows.append(make_window(label, max(0.0, 100.0 - float(used)),
                                   iso(entry.get("resets_at"))))
    if not windows:
        return unknown("meta", "no usable window in subscription block")
    return from_windows("meta", windows, raw=scrub({"tier": sub.get("tier")}))


def _meta_stream(url: str, key: str, body: bytes):
    """POST the probe and return the subscription event's JSON, closing the
    stream early. (data, error) — the key lives in this frame only."""
    req = urllib.request.Request(url, data=body, headers={
        "Accept": "text/event-stream", "Content-Type": "application/json",
        "User-Agent": "tarmac/1", "Authorization": f"Bearer {key}"})
    try:
        with urllib.request.urlopen(req, timeout=HTTP_TIMEOUT) as resp:
            for raw in resp:
                line = raw.decode("utf-8", "replace").strip()
                if line.startswith("data:") and "subscription_usage" in line:
                    try:
                        return json.loads(line[5:]), None
                    except ValueError:
                        return None, "subscription event was not JSON"
            return None, "no subscription event in stream"
    except urllib.error.HTTPError as exc:
        return None, f"http {exc.code} {exc.reason}"
    except (urllib.error.URLError, TimeoutError, OSError) as exc:
        return None, f"unreachable: {getattr(exc, 'reason', exc)}"


@soft("meta")
def meta(config: dict | None = None, *, auth_path=OPENCODE_AUTH,
         url: str = META_RESPONSES_URL, stream=_meta_stream) -> Reading:
    """Muse Code subscription windows, read from the event the Model API
    attaches to a streamed response."""
    key, source = api_key("meta", config, auth_path)
    if not key:
        return unknown("meta", source)
    body = json.dumps({
        "model": str((config or {}).get("model") or META_PROBE_MODEL),
        "input": "hi", "stream": True, "max_output_tokens": 16,
    }).encode("utf-8")
    data, err = stream(url, key, body)
    if err:
        return unknown("meta", err)
    return parse_meta(data)
