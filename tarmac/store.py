"""SQLite at ``~/.tarmac/tarmac.db``: sources, append-only readings, meta.

Readings are append-only because burn rate and the hold rule both need
adjacent historical readings to be trustworthy. ``raw_json`` is pruned after
30 days; the numeric history is kept forever.
"""
import json
import os
import secrets
import sqlite3
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path

SCHEMA_VERSION = 1

DDL = """
CREATE TABLE IF NOT EXISTS sources (
  source_id TEXT PRIMARY KEY,
  slug TEXT NOT NULL COLLATE NOCASE UNIQUE CHECK(length(trim(slug)) > 0),
  name TEXT NOT NULL CHECK(length(trim(name)) > 0),
  provider TEXT NOT NULL CHECK(length(trim(provider)) > 0),
  account TEXT NOT NULL DEFAULT '',
  adapter TEXT NOT NULL CHECK(length(trim(adapter)) > 0),
  config_json TEXT NOT NULL DEFAULT '{}',
  enabled INTEGER NOT NULL DEFAULT 1 CHECK(enabled IN (0, 1)),
  archived INTEGER NOT NULL DEFAULT 0 CHECK(archived IN (0, 1)),
  revision INTEGER NOT NULL DEFAULT 1 CHECK(revision > 0),
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL,
  UNIQUE(provider, account)
);

CREATE TABLE IF NOT EXISTS readings (
  id INTEGER PRIMARY KEY,
  source_id TEXT NOT NULL REFERENCES sources(source_id) ON DELETE CASCADE,
  remaining REAL,
  limit_value REAL,
  unit TEXT,
  resets_at TEXT,
  as_of TEXT,
  fresh_until TEXT,
  definitive INTEGER NOT NULL DEFAULT 0 CHECK(definitive IN (0, 1)),
  needs_login INTEGER NOT NULL DEFAULT 0 CHECK(needs_login IN (0, 1)),
  reason TEXT,
  windows_json TEXT,
  raw_json TEXT,
  polled_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_readings_source ON readings(source_id, id);

CREATE TABLE IF NOT EXISTS meta (
  key TEXT PRIMARY KEY,
  value TEXT NOT NULL
);
"""


def home() -> Path:
    root = Path(os.environ.get("TARMAC_HOME", "~/.tarmac")).expanduser()
    root.mkdir(mode=0o700, exist_ok=True)
    return root


def db_path() -> Path:
    return home() / "tarmac.db"


def now() -> str:
    return datetime.now(timezone.utc).isoformat()


def connect(path: Path | str | None = None) -> sqlite3.Connection:
    con = sqlite3.connect(str(path or db_path()))
    con.row_factory = sqlite3.Row
    con.execute("PRAGMA journal_mode=WAL")
    con.execute("PRAGMA foreign_keys=ON")
    con.executescript(DDL)
    con.execute("INSERT OR IGNORE INTO meta(key, value) VALUES(?, ?)",
                ("schema_version", str(SCHEMA_VERSION)))
    return con


def token_path() -> Path:
    return home() / "token"


def token() -> str:
    """The bearer token, generated on first use, mode 0600, read fresh on
    every call so rotation needs no restart."""
    path = token_path()
    if not path.exists():
        path.touch(mode=0o600)
        path.write_text(secrets.token_urlsafe(32))
        os.chmod(path, 0o600)
    value = path.read_text().strip()
    if not value:
        raise RuntimeError(f"empty token file: {path}")
    return value


# --- sources ----------------------------------------------------------------

def _slugify(name: str) -> str:
    slug = "".join(c if c.isalnum() else "-" for c in name.strip().lower())
    slug = "-".join(part for part in slug.split("-") if part)
    return slug or "source"


def _mint_slug(con, name: str, wanted: str | None = None) -> str:
    base = _slugify(wanted or name)
    slug, n = base, 1
    while con.execute("SELECT 1 FROM sources WHERE slug=?", (slug,)).fetchone():
        n += 1
        slug = f"{base}-{n}"
    return slug


def find_source(con, selector: str):
    return con.execute(
        "SELECT * FROM sources WHERE source_id=? OR slug=?",
        (selector, selector)).fetchone()


def all_sources(con, *, include_archived: bool = False):
    where = "" if include_archived else "WHERE archived=0"
    return con.execute(
        f"SELECT * FROM sources {where} ORDER BY provider, account").fetchall()


def create_source(con, *, name: str, provider: str, adapter: str,
                  account: str = "", config: dict | None = None,
                  slug: str | None = None, enabled: bool = True):
    source_id = uuid.uuid4().hex[:12]
    stamp = now()
    con.execute(
        "INSERT INTO sources(source_id,slug,name,provider,account,adapter,"
        "config_json,enabled,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?,?,?)",
        (source_id, _mint_slug(con, name, slug), name.strip(),
         provider.strip().lower(), account.strip(), adapter.strip(),
         json.dumps(config or {}, separators=(",", ":")), int(enabled),
         stamp, stamp))
    con.commit()
    return find_source(con, source_id)


UPDATABLE = {"name", "account", "adapter", "config", "enabled", "archived"}


def update_source(con, selector: str, changes: dict,
                  *, revision: int | None = None):
    row = find_source(con, selector)
    if row is None:
        raise KeyError(f"no source {selector!r}")
    if revision is not None and revision != row["revision"]:
        raise ValueError(
            f"revision mismatch: have {row['revision']}, got {revision}")
    unknown_keys = set(changes) - UPDATABLE
    if unknown_keys:
        raise ValueError(f"unknown fields: {', '.join(sorted(unknown_keys))}")
    sets, values = [], []
    for key, value in changes.items():
        if key == "config":
            if not isinstance(value, dict):
                raise ValueError("config must be a JSON object")
            sets.append("config_json=?")
            values.append(json.dumps(value, separators=(",", ":")))
        elif key in ("enabled", "archived"):
            sets.append(f"{key}=?")
            values.append(int(bool(value)))
        else:
            if not isinstance(value, str) or (key == "name" and not value.strip()):
                raise ValueError(f"{key} must be a non-empty string")
            sets.append(f"{key}=?")
            values.append(value.strip())
    if not sets:
        return row
    sets += ["revision=revision+1", "updated_at=?"]
    values += [now(), row["source_id"]]
    con.execute(f"UPDATE sources SET {', '.join(sets)} WHERE source_id=?",
                values)
    con.commit()
    return find_source(con, row["source_id"])


def delete_source(con, selector: str) -> bool:
    row = find_source(con, selector)
    if row is None:
        return False
    con.execute("DELETE FROM readings WHERE source_id=?", (row["source_id"],))
    con.execute("DELETE FROM sources WHERE source_id=?", (row["source_id"],))
    con.commit()
    return True


# --- readings ---------------------------------------------------------------

def record_reading(con, reading: dict) -> int:
    cursor = con.execute(
        "INSERT INTO readings(source_id,remaining,limit_value,unit,resets_at,"
        "as_of,fresh_until,definitive,needs_login,reason,windows_json,raw_json,"
        "polled_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (reading["source_id"], reading["remaining"], reading["limit_value"],
         reading["unit"], reading["resets_at"], reading["as_of"],
         reading["fresh_until"], int(reading["definitive"]),
         int(reading.get("needs_login", False)), reading["reason"],
         json.dumps(reading["windows"], separators=(",", ":")),
         json.dumps(reading["raw"], separators=(",", ":")),
         reading["polled_at"]))
    con.commit()
    return int(cursor.lastrowid)


def readings_for(con, source_id: str, limit: int = 100, since: str | None = None):
    if since:
        return con.execute(
            "SELECT * FROM readings WHERE source_id=? AND polled_at>=? "
            "ORDER BY id DESC LIMIT ?", (source_id, since, limit)).fetchall()
    return con.execute(
        "SELECT * FROM readings WHERE source_id=? ORDER BY id DESC LIMIT ?",
        (source_id, limit)).fetchall()


def latest_reading(con, source_id: str):
    return con.execute(
        "SELECT * FROM readings WHERE source_id=? ORDER BY id DESC LIMIT 1",
        (source_id,)).fetchone()


def prune_raw(con, older_than_days: int = 30) -> int:
    """Null out raw_json on old readings; the numeric history stays."""
    cutoff = (datetime.now(timezone.utc)
              - timedelta(days=older_than_days)).isoformat()
    cursor = con.execute(
        "UPDATE readings SET raw_json=NULL "
        "WHERE polled_at < ? AND raw_json IS NOT NULL", (cutoff,))
    con.commit()
    return cursor.rowcount
