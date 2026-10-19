"""Credential lookup and the one HTTP helper that may hold a key.

A key comes from OpenCode's own credential store — ``auth.json`` (OpenCode
1.x) or the ``credential`` table of ``opencode.db`` beside it (OpenCode 2.x),
both under ``~/.local/share/opencode`` — falling back to a named environment
variable. A source's config may override either lookup with ``auth_entry``
(a different auth.json entry) or ``key_env`` (a different variable) — that
is how a second account of the same provider resolves its own key. The key
is read inside the adapter, used for one request, and never returned,
stored, printed, or logged. Only the source LABEL is ever safe to show.
"""
import json
import os
import shutil
import sqlite3
import time
import urllib.error
import urllib.request
from pathlib import Path

HTTP_TIMEOUT = 10

OPENCODE_AUTH = Path("~/.local/share/opencode/auth.json")

# provider -> (auth.json entry, environment variable fallback)
KEY_SOURCES = {
    "deepseek": ("deepseek", "DEEPSEEK_API_KEY"),
    "kimi": ("kimi-for-coding", "KIMI_CODING_API_KEY"),
    "meta": ("meta", "META_API_KEY"),
    "minimax": ("minimax-coding-plan", "MINIMAX_API_KEY"),
    "openrouter": ("openrouter", "OPENROUTER_API_KEY"),
    "xai": ("xai", "XAI_API_KEY"),
    "zai": ("zai", "ZAI_API_KEY"),
}


def _db_path(auth_path: Path | str) -> Path:
    """OpenCode 2.x keeps the same entries in a sqlite table next to where
    auth.json used to live, so a test that points auth_path somewhere empty
    also points the database somewhere empty."""
    return Path(auth_path).expanduser().with_name("opencode.db")


def _db_connect(path: Path, *, write: bool = False):
    mode = "rw" if write else "ro"
    return sqlite3.connect(f"file:{path}?mode={mode}", uri=True, timeout=5)


def opencode_entries(auth_path: Path | str = OPENCODE_AUTH) -> tuple[dict, str]:
    """``(entries, label)``: provider name -> entry dict (``{"type": "key",
    "key": ...}`` or ``{"type": "oauth", "access": ..., "refresh": ...}``).
    auth.json wins when it exists; otherwise the active rows of OpenCode
    2.x's credential table. Missing or unreadable stores read as empty."""
    path = Path(auth_path).expanduser()
    if path.exists():
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
            return (data if isinstance(data, dict) else {}), "opencode auth.json"
        except (OSError, ValueError):
            return {}, "opencode auth.json"
    db = _db_path(path)
    entries: dict = {}
    if db.exists():
        try:
            con = _db_connect(db)
            try:
                rows = con.execute(
                    "SELECT integration_id, value FROM credential "
                    "WHERE active = 1 ORDER BY time_updated").fetchall()
            finally:
                con.close()
            for name, raw in rows:
                try:
                    entry = json.loads(raw)
                except (TypeError, ValueError):
                    continue
                if isinstance(name, str) and isinstance(entry, dict):
                    entries[name] = entry
        except sqlite3.Error:
            pass  # schema drift or a locked database reads as no store
    return entries, "opencode db"


def replace_entry(name: str, expected_refresh: str, entry: dict,
                  auth_path: Path | str = OPENCODE_AUTH,
                  write_file=None) -> tuple[dict | None, str | None]:
    """Compare-and-swap one OAuth entry back into OpenCode's store.

    Returns ``(current, error)``. When another process already rotated the
    refresh token, nothing is written and ``current`` is the live entry.
    ``write_file(path, data) -> error`` handles the auth.json case."""
    path = Path(auth_path).expanduser()
    if path.exists():
        try:
            latest = json.loads(path.read_text(encoding="utf-8"))
            current = latest[name]
            if not isinstance(current, dict):
                raise ValueError(f"the {name} entry is not an object")
        except (OSError, ValueError, KeyError, TypeError) as exc:
            return None, f"{path} could not be re-read: {exc}"
        if current.get("refresh") != expected_refresh:
            return current, None
        latest[name] = entry
        err = write_file(path, latest) if write_file else "no writer for auth.json"
        return (None, err) if err else (entry, None)
    db = _db_path(path)
    try:
        con = _db_connect(db, write=True)
        try:
            con.execute("BEGIN IMMEDIATE")
            row = con.execute(
                "SELECT id, value FROM credential WHERE integration_id = ? "
                "AND active = 1 ORDER BY time_updated DESC LIMIT 1",
                (name,)).fetchone()
            if row is None:
                con.rollback()
                return None, f"no active {name} credential in {db}"
            current = json.loads(row[1])
            if not isinstance(current, dict) \
                    or current.get("refresh") != expected_refresh:
                con.rollback()
                return (current if isinstance(current, dict) else None), None
            con.execute(
                "UPDATE credential SET value = ?, time_updated = ? WHERE id = ?",
                (json.dumps(entry), int(time.time() * 1000), row[0]))
            con.commit()
        finally:
            con.close()
    except (sqlite3.Error, ValueError, TypeError) as exc:
        return None, f"the renewed grant could not be saved to {db}: {exc}"
    return entry, None


def which(name: str) -> str | None:
    return shutil.which(name)


def fchmod(fd: int, mode: int) -> None:
    fn = getattr(os, "fchmod", None)
    if fn is None:
        return
    try:
        fn(fd, mode)
    except (OSError, NotImplementedError):
        pass


def api_key(provider: str, config: dict | None = None,
            auth_path: Path | str = OPENCODE_AUTH):
    """``(key, source)`` — the per-source override, then OpenCode's store,
    then the named environment variable. On failure ``(None, reason)``."""
    entry_name, env = KEY_SOURCES.get(
        provider, (provider, f"{provider.upper()}_API_KEY"))
    config = config or {}
    entry_name = str(config.get("auth_entry") or entry_name)
    env = str(config.get("key_env") or env)
    entries, store = opencode_entries(auth_path)
    entry = entries.get(entry_name)
    value = (entry.get("key") or entry.get("access")) if isinstance(entry, dict) else None
    if isinstance(value, str) and value.strip():
        return value.strip(), f"{store} ({entry_name})"
    value = os.environ.get(env, "").strip()
    if value:
        return value, f"${env}"
    return None, (f"no key for {entry_name} in OpenCode's store, and "
                  f"${env} is not set")


def fetch(url: str, key: str | None = None, headers: dict | None = None,
          data: bytes | None = None):
    """(response headers, body bytes, error). The key lives in this frame only;
    error strings never include the response body, which can echo a key."""
    req = urllib.request.Request(url, data=data, headers={
        "Accept": "application/json",
        # grok.com answers 403 to the default Python-urllib agent.
        "User-Agent": "tarmac/1",
        **({"Authorization": f"Bearer {key}"} if key else {}),
        **(headers or {}),
    })
    try:
        with urllib.request.urlopen(req, timeout=HTTP_TIMEOUT) as resp:
            return ({k.lower(): v for k, v in resp.headers.items()},
                    resp.read(), None)
    except urllib.error.HTTPError as exc:
        return {}, b"", f"http {exc.code} {exc.reason}"
    except (urllib.error.URLError, TimeoutError, OSError) as exc:
        return {}, b"", f"unreachable: {getattr(exc, 'reason', exc)}"


def get_json(url: str, provider: str, config: dict | None = None,
             headers: dict | None = None,
             auth_path: Path | str = OPENCODE_AUTH):
    """(data, error), with the provider's key looked up for this one request."""
    key, source = api_key(provider, config, auth_path)
    if not key:
        return None, source
    _, body, err = fetch(url, key, headers)
    if err:
        return None, err
    try:
        return json.loads(body or b"{}"), None
    except ValueError:
        return None, "response was not JSON"
