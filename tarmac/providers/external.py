"""Plugin adapters: any executable speaking the JSON stdin/stdout contract.

Two ways in, one code path:
- the ``command`` adapter: the source's config carries ``command`` as a JSON
  argv array;
- the plugins directory: any executable in ``~/.tarmac/plugins/`` becomes an
  adapter named by its filename stem, no registration needed.

Contract: tarmac writes one JSON object to stdin —
``{"source_id", "provider", "account", "config"}`` — and the plugin prints
one reading JSON object to stdout: ``{"remaining", "unit", "resets_at",
"as_of", "reason", "windows", "raw"}``, every field optional. Exit non-zero
or print garbage and the reading degrades to unknown-with-reason. No shell
exists; 20s default timeout (``config.timeout_seconds``, cap 120); output
capped at 1 MiB and scrubbed before persistence.
"""
import json
import os
import subprocess
from pathlib import Path

from tarmac.model import Reading, iso, now, scrub, unknown


def plugins_dir(home: Path) -> Path:
    return home / "plugins"


def discover(home: Path) -> dict[str, Path]:
    """Executable files in the plugins dir, adapter name = filename stem."""
    found = {}
    try:
        entries = sorted(plugins_dir(home).iterdir())
    except OSError:
        return found
    for entry in entries:
        if entry.is_file() and os.access(entry, os.X_OK):
            found[entry.stem] = entry
    return found


def run_plugin(argv: list[str], source: dict, config: dict,
               runner=subprocess.run) -> Reading:
    """Run one argv adapter. JSON goes in and comes out; no shell exists."""
    provider = source["provider"]
    timeout = max(1, min(int(config.get("timeout_seconds", 20)), 120))
    payload = {
        "source_id": source["source_id"],
        "provider": provider,
        "account": source["account"],
        "config": config,
    }
    try:
        result = runner(argv, input=json.dumps(payload), capture_output=True,
                        text=True, timeout=timeout, check=False)
    except (OSError, subprocess.TimeoutExpired) as exc:
        return unknown(provider, f"plugin failed to run: {exc}")
    if result.returncode != 0:
        detail = (result.stderr or "").strip()[:300]
        return unknown(provider,
                       f"plugin exited {result.returncode}" +
                       (f": {detail}" if detail else ""))
    if len(result.stdout.encode("utf-8")) > 1024 * 1024:
        return unknown(provider, "plugin output exceeded 1 MiB")
    try:
        value = json.loads(result.stdout)
    except ValueError:
        return unknown(provider, "plugin returned invalid JSON")
    if not isinstance(value, dict):
        return unknown(provider, "plugin must return a JSON object")
    remaining = value.get("remaining")
    if remaining is not None and (isinstance(remaining, bool) or not isinstance(
            remaining, (int, float))):
        return unknown(provider, "plugin remaining must be numeric")
    windows = value.get("windows", [])
    if not isinstance(windows, list):
        return unknown(provider, "plugin windows must be an array")
    return Reading(
        provider=provider, remaining=remaining,
        unit=str(value.get("unit") or ""), resets_at=iso(value.get("resets_at")),
        as_of=iso(value.get("as_of")) or now(),
        reason=str(value.get("reason") or "") or None,
        windows=scrub(windows), raw=scrub(value.get("raw") or {}),
    )


def command_argv(config: dict) -> list[str]:
    raw = config.get("command")
    if isinstance(raw, str):
        try:
            raw = json.loads(raw)
        except ValueError as exc:
            raise ValueError("command must be a JSON argv array") from exc
    if not isinstance(raw, list) or not raw or any(
            not isinstance(value, str) or not value for value in raw):
        raise ValueError("command must be a non-empty JSON argv array")
    return raw
