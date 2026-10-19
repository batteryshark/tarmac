"""JSON projections: readings and sources as the API serves them.

Burn rate is computed HERE and only here — percent consumed per hour between
two adjacent definitive readings whose windows match by id, unit, and an
identical resets_at, taking the fastest real comparable window. Clients
never recompute it.
"""
import json
import math
from datetime import datetime, timezone

from tarmac import model


def _json_field(row, key, default):
    raw = row[key] if key in row.keys() else None
    if raw in (None, ""):
        return default
    try:
        value = json.loads(raw)
    except (TypeError, ValueError):
        return default
    return value if isinstance(value, type(default)) else default


def _public_number(value):
    return value if isinstance(value, (int, float)) \
        and not isinstance(value, bool) and math.isfinite(value) else None


def _public_text(value):
    return value if isinstance(value, str) and value else None


def credit_payload(raw) -> dict | None:
    value = raw.get("credits") if isinstance(raw, dict) else None
    if isinstance(value, str):
        return {"text": value, "count": None, "expires_at": None}
    if not isinstance(value, dict):
        return None
    count = value.get("count")
    count = count if isinstance(count, int) and not isinstance(count, bool) \
        and count >= 0 else None
    text, expires_at = _public_text(value.get("text")), \
        _public_text(value.get("expires_at"))
    return {"text": text, "count": count, "expires_at": expires_at} \
        if text is not None or count is not None or expires_at is not None \
        else None


def observation(row, *, live: bool = False) -> dict:
    """One reading row as the API serves it. ``live=True`` re-evaluates each
    window as of now, blanking any whose reset has already passed."""
    windows = []
    for index, window in enumerate(_json_field(row, "windows_json", [])):
        if not isinstance(window, dict):
            continue
        if live:
            try:
                window = model.as_of_now(dict(window))
            except (TypeError, ValueError):
                window = {**window, "remaining": None, "stale": True,
                          "stale_reason": "invalid reset timestamp"}
        label = str(window.get("label") or f"window {index + 1}")
        value = _public_number(window.get("remaining"))
        if window.get("unit", "percent") == "percent" and (value is None or not 0 <= value <= 100):
            window = {**window, "remaining": None, "stale": True,
                      "stale_reason": "invalid percentage"}
        windows.append({
            "id": str(window.get("id") or label), "label": label,
            "remaining_percent": _public_number(window.get("remaining")),
            "resets_at": _public_text(window.get("resets_at")),
            "unit": _public_text(window.get("unit")) or "percent",
            "stale": bool(window.get("stale", False)),
            "stale_reason": _public_text(window.get("stale_reason")),
            "per_model": bool(window.get("per_model", False)),
            "bucket_id": _public_text(window.get("bucket_id")),
            "duration_minutes": _public_number(window.get("duration_minutes")),
            "reset_precision_seconds": _public_number(window.get("reset_precision_seconds")),
        })
    raw = _json_field(row, "raw_json", {})
    current = {
        "observed_at": row["as_of"] or row["polled_at"],
        "polled_at": row["polled_at"],
        "fresh_until": row["fresh_until"],
        "definitive": bool(row["definitive"]),
        "needs_login": bool(row["needs_login"]) if "needs_login" in row.keys() else False,
        "remaining": _public_number(row["remaining"]),
        "unit": _public_text(row["unit"]),
        "resets_at": _public_text(row["resets_at"]),
        "credits": credit_payload(raw),
        "acquisition": _public_text(raw.get("acquisition") or raw.get("source")),
        "workspace_credits": _workspace_credits(raw.get("workspace_credits")),
        "reason": row["reason"], "burn_rate": None,
        "windows": windows,
    }
    if live:
        # The compact value must never outlive the windows it summarizes.
        relevant = [w for w in windows if not w["per_model"]]
        if any(w["stale"] or w["remaining_percent"] is None for w in relevant):
            current.update(remaining=None, resets_at=None, definitive=False)
        elif _instant(current["resets_at"]) and _instant(current["resets_at"]) <= datetime.now(timezone.utc):
            current.update(remaining=None, resets_at=None, definitive=False)
    return current


def _workspace_credits(value):
    if not isinstance(value, dict):
        return None
    return {"balance": _public_number(value.get("balance")),
            "unlimited": value.get("unlimited") is True,
            "has_credits": value.get("has_credits") is True}


def _instant(value):
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed


def window_burns(current: dict, previous: dict) -> list[dict]:
    """Observed rates retain window identity; missing epochs are incomparable."""
    if not current.get("definitive") or not previous.get("definitive"):
        return []
    current_at = _instant(current.get("observed_at"))
    previous_at = _instant(previous.get("observed_at"))
    if current_at is None or previous_at is None or current_at <= previous_at:
        return []
    hours = (current_at - previous_at).total_seconds() / 3600
    older = {w["id"]: w for w in previous.get("windows", [])}
    rates = []
    for w in current.get("windows", []):
        prior = older.get(w["id"])
        if prior is None or w.get("unit") != "percent" or prior.get("unit") != "percent":
            continue
        reset, before = _instant(w.get("resets_at")), _instant(prior.get("resets_at"))
        precision = max(w.get("reset_precision_seconds") or 1,
                        prior.get("reset_precision_seconds") or 1)
        if reset is None or before is None or abs((reset - before).total_seconds()) > precision \
                or current_at >= min(reset, before):
            continue
        current_left, prior_left = _public_number(w.get("remaining_percent")), \
            _public_number(prior.get("remaining_percent"))
        if current_left is None or prior_left is None or current_left > prior_left:
            continue
        rates.append({"window_id": w["id"], "resets_at": w["resets_at"],
                      "percent_per_hour": round((prior_left - current_left) / hours, 6),
                      "sample_hours": hours, "observed_at": current["observed_at"]})
    return rates


def adjacent_burn(current: dict, previous: dict) -> float | None:
    rates = window_burns(current, previous)
    return max((r["percent_per_hour"] for r in rates), default=None)


def history(rows) -> list[dict]:
    """Observations newest-first, each with its burn rate vs its neighbour."""
    items = [observation(row) for row in rows]
    for index in range(len(items) - 1):
        rates = window_burns(items[index], items[index + 1])
        items[index]["window_burns"] = rates
        items[index]["burn_rate"] = max((r["percent_per_hour"] for r in rates), default=None)
    return items


def source_payload(row, readings, *, with_history: bool = False) -> dict:
    """One source with its latest observation. ``readings`` is newest-first;
    two are enough for burn unless ``with_history``."""
    items = history(readings)
    current = observation(readings[0], live=True) if readings else None
    if current and items:
        current["burn_rate"] = items[0]["burn_rate"]
        current["window_burns"] = [r for r in items[0].get("window_burns", [])
                                   if _instant(r["resets_at"]) > datetime.now(timezone.utc)]
        if not current["window_burns"]:
            current["burn_rate"] = None
    fresh = False
    if current and current.get("fresh_until"):
        until = _instant(current["fresh_until"])
        fresh = bool(until and until > datetime.now(timezone.utc))
    payload = {
        "id": row["source_id"], "slug": row["slug"], "name": row["name"],
        "provider": row["provider"], "account": row["account"],
        "adapter": row["adapter"],
        "kind": model.kind_of(row["provider"]),
        "enabled": bool(row["enabled"]), "archived": bool(row["archived"]),
        "fresh": fresh,
        "status": ("disabled" if not row["enabled"]
                   else "unknown" if not current or not current.get("definitive")
                   else "current" if fresh else "stale"),
        "needs_login": bool(current.get("needs_login")) if current else False,
        "latest": current,
        "burn_rate": current.get("burn_rate") if current else None,
        "revision": row["revision"],
        "created_at": row["created_at"], "updated_at": row["updated_at"],
    }
    if with_history:
        payload["history"] = items
    return payload
