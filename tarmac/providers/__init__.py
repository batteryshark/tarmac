"""The adapter registry and the one call that polls a source.

Builtins are in-process functions; plugins are executables discovered in
``~/.tarmac/plugins/`` (see ``external``). Both take the source's config and
return a ``Reading``.
"""
import json
from datetime import datetime, timezone
from pathlib import Path

from tarmac.model import scrub, unknown
from tarmac.providers import (antigravity as agy, claude, codex, external,
                              simple, xai)

BUILTINS = {
    "antigravity": agy.antigravity,
    "claude": claude.claude,
    "codex": codex.codex,
    "deepseek": simple.deepseek,
    "kimi": simple.kimi,
    "meta": simple.meta,
    "minimax": simple.minimax,
    "openrouter": simple.openrouter,
    "xai": xai.xai,
    "zai": simple.zai,
}


def adapter_names(home: Path) -> dict[str, dict]:
    """Every adapter this instance can run: builtins plus discovered plugins,
    plus ``command``. The API serves this so no client hardcodes the list."""
    names = {name: {"builtin": True} for name in BUILTINS}
    names["command"] = {"builtin": True}
    for name in external.discover(home):
        names.setdefault(name, {"builtin": False})
    return names


def source_config(source) -> dict:
    raw = source["config_json"] if "config_json" in source.keys() else None
    if raw in (None, ""):
        return {}
    if isinstance(raw, dict):
        return dict(raw)
    try:
        value = json.loads(raw)
    except (TypeError, ValueError) as exc:
        raise ValueError("source config must be a JSON object") from exc
    if not isinstance(value, dict):
        raise ValueError("source config must be a JSON object")
    return value


def poll_source(source, home: Path) -> dict:
    """Poll one source and return a DB reading row (as a dict)."""
    adapter = source["adapter"]
    config = source_config(source)
    if adapter in BUILTINS:
        reading = BUILTINS[adapter](config)
    elif adapter == "command":
        try:
            reading = external.run_plugin(
                external.command_argv(config), source, config)
        except ValueError as exc:
            reading = unknown(source["provider"], str(exc))
    else:
        plugin = external.discover(home).get(adapter)
        if plugin is None:
            reading = unknown(source["provider"], f"unknown adapter {adapter!r}")
        else:
            reading = external.run_plugin([str(plugin)], source, config)
    fresh_seconds = max(60, min(int(config.get("fresh_seconds", 3600)), 86400))
    polled = datetime.now(timezone.utc)
    as_of = reading.as_of or polled.isoformat()
    try:
        observed = datetime.fromisoformat(as_of.replace("Z", "+00:00"))
        if observed.tzinfo is None:
            observed = observed.replace(tzinfo=timezone.utc)
    except (TypeError, ValueError):
        observed = polled
        as_of = polled.isoformat()
    definitive = bool(reading.known and reading.reason is None and not reading.stale)
    return {
        "source_id": source["source_id"],
        "remaining": reading.remaining,
        "limit_value": 100.0 if reading.unit == "percent" else None,
        "unit": reading.unit,
        "resets_at": reading.resets_at,
        "as_of": as_of,
        "fresh_until": datetime.fromtimestamp(
            min(polled.timestamp(), observed.timestamp()) + fresh_seconds,
            timezone.utc).isoformat(),
        "definitive": definitive,
        "needs_login": bool(reading.needs_login),
        "reason": reading.reason,
        "windows": reading.windows,
        "raw": scrub(reading.raw),
        "polled_at": polled.isoformat(),
    }
