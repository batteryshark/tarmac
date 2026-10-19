"""Loopback HTTP API and dashboard.

Reads are open on loopback (the boundary check rejects any non-loopback Host
or Origin, which is also what blocks a web page's cross-origin CSRF).
Mutations — creating, updating, deleting sources — require the bearer token
from ``~/.tarmac/token``, read fresh per request. Refresh only triggers a
poll the loop would run anyway, so it stays open and the dashboard's button
works without a token.

No adapter ever runs on a request thread: refresh queues the source on the
poller and waits on a completion event, answering 200 with the fresh reading
or 202 with the previous one still pending.
"""
import json
import secrets
import shlex
import subprocess
import sys
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from tarmac import model, payloads, providers, store
from tarmac.providers import claude as claude_provider

DEFAULT_PORT = 8767
VERSION = "0.1.0"

DASHBOARD = Path(__file__).parent / "dashboard.html"


def login_command(row, config: dict) -> str | None:
    """The interactive OAuth sign-in for a claude or codex source, as one
    shell command. The CLI owns the browser flow — tarmac only launches it
    in a Terminal with the right per-account environment."""
    if row["adapter"] == "claude":
        base = "claude auth login"
        env_var, dir_key = "CLAUDE_CONFIG_DIR", "config_dir"
    elif row["adapter"] == "codex":
        base = "codex login"
        env_var, dir_key = "CODEX_HOME", "codex_home"
    else:
        return None
    account_dir = str(config.get(dir_key) or "").strip()
    if not account_dir:
        return base
    quoted = shlex.quote(str(Path(account_dir).expanduser()))
    return f"mkdir -p {quoted} && {env_var}={quoted} {base}"


def open_in_terminal(command: str, runner=subprocess.run) -> bool:
    escaped = command.replace("\\", "\\\\").replace('"', '\\"')
    script = (f'tell application "Terminal" to do script "{escaped}"\n'
              'tell application "Terminal" to activate')
    try:
        done = runner(["osascript", "-e", script], capture_output=True,
                      timeout=15)
        return done.returncode == 0
    except (OSError, subprocess.TimeoutExpired):
        return False


class TarmacServer(ThreadingHTTPServer):
    daemon_threads = True
    # SO_REUSEADDR: a restart must not lose to the old daemon's TIME_WAIT
    # sockets. This is not SO_REUSEPORT — two live daemons still cannot bind.
    allow_reuse_address = True

    def handle_error(self, request, client_address):
        """A client that hangs up mid-keep-alive is routine, not a fault."""
        if sys.exc_info()[0] not in (ConnectionResetError, BrokenPipeError,
                                     TimeoutError):
            super().handle_error(request, client_address)

    def __init__(self, poller, host: str = "127.0.0.1",
                 port: int = DEFAULT_PORT, db_path=None):
        self.poller = poller
        self.db_path = db_path
        try:
            super().__init__((host, port), Handler)
        except OSError as exc:
            raise SystemExit(
                f"tarmac: cannot bind {host}:{port} ({exc.strerror}); "
                "the port is occupied — stop the other process or pass "
                "--port") from None


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    server: TarmacServer

    def log_message(self, *_):
        pass

    # --- plumbing -----------------------------------------------------------

    def _send(self, status: int, payload, content_type="application/json"):
        raw = payload if isinstance(payload, bytes) else \
            json.dumps(payload).encode()
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)

    def _error(self, status: int, detail: str):
        self._send(status, {"error": detail})

    def _body(self) -> dict:
        length = int(self.headers.get("Content-Length") or 0)
        if not length:
            return {}
        try:
            value = json.loads(self.rfile.read(length))
        except (json.JSONDecodeError, UnicodeDecodeError):
            return {}
        return value if isinstance(value, dict) else {}

    def _boundary_ok(self) -> bool:
        host = (self.headers.get("Host") or "").split(":")[0]
        if host not in ("127.0.0.1", "localhost"):
            return False
        origin = self.headers.get("Origin")
        if origin:
            parsed = urllib.parse.urlsplit(origin)
            if parsed.hostname not in ("127.0.0.1", "localhost"):
                return False
        return True

    def _authed(self) -> bool:
        auth = self.headers.get("Authorization") or ""
        if not auth.startswith("Bearer "):
            return False
        try:
            return secrets.compare_digest(auth[7:], store.token())
        except RuntimeError:
            return False

    def _con(self):
        return store.connect(self.server.db_path)

    def _query(self) -> dict:
        return {k: v[-1] for k, v in urllib.parse.parse_qs(
            urllib.parse.urlsplit(self.path).query).items()}

    def _payload(self, con, row, **kw) -> dict:
        limit = 100 if kw.get("with_history") else 2
        readings = store.readings_for(con, row["source_id"], limit=limit)
        return payloads.source_payload(row, readings, **kw)

    # --- routing ------------------------------------------------------------

    def do_GET(self):
        if not self._boundary_ok():
            return self._error(403, "loopback only")
        path = urllib.parse.urlsplit(self.path).path
        if path not in ("/health", "/api/v1/snapshot"):  # liveness pings are not consumption
            self.server.poller.note_reader()
        if path == "/health":
            return self._send(200, {"status": "ok", "version": VERSION})
        if path in ("", "/"):
            try:
                return self._send(200, DASHBOARD.read_bytes(),
                                  "text/html; charset=utf-8")
            except OSError:
                return self._error(404, "dashboard not built")
        if path in ("/api/v1/status", "/api/v1/snapshot"):
            return self._status(passive=path.endswith("/snapshot"))
        if path == "/api/v1/meta":
            return self._send(200, {
                "adapters": providers.adapter_names(store.home()),
                "plan_providers": sorted(model.PLAN_PROVIDERS),
                "version": VERSION})
        if path == "/api/v1/claude/auth":
            return self._send(200, claude_provider.cached_auth_status())
        if path == "/api/v1/holds":
            return self._send(200, {"holds": self._holds()})
        if path == "/api/v1/sources":
            con = self._con()
            try:
                include = self._query().get("archived") == "1"
                rows = store.all_sources(con, include_archived=include)
                return self._send(200, {"sources": [
                    self._payload(con, row) for row in rows]})
            finally:
                con.close()
        if path.startswith("/api/v1/sources/"):
            rest = path[len("/api/v1/sources/"):]
            selector, _, tail = rest.partition("/")
            con = self._con()
            try:
                row = store.find_source(con, selector)
                if row is None:
                    return self._error(404, f"no source {selector!r}")
                if tail == "history":
                    q = self._query()
                    limit = max(1, min(int(q.get("limit", 100)), 1000))
                    rows = store.readings_for(con, row["source_id"],
                                              limit=limit, since=q.get("since"))
                    return self._send(200, {"history": payloads.history(rows)})
                if tail:
                    return self._error(404, "unknown route")
                return self._send(200, self._payload(con, row,
                                                     with_history=True))
            except ValueError:
                return self._error(400, "bad query parameter")
            finally:
                con.close()
        return self._error(404, "unknown route")

    def do_POST(self):
        if not self._boundary_ok():
            return self._error(403, "loopback only")
        self.server.poller.note_reader()
        path = urllib.parse.urlsplit(self.path).path
        if path.startswith("/api/v1/sources/") and path.endswith("/refresh"):
            return self._refresh(path[len("/api/v1/sources/"):-len("/refresh")])
        if path.startswith("/api/v1/sources/") and path.endswith("/login"):
            return self._login(path[len("/api/v1/sources/"):-len("/login")])
        if path == "/api/v1/sources":
            if not self._authed():
                return self._error(401, "bearer token required")
            body = self._body()
            for field in ("name", "provider", "adapter"):
                if not str(body.get(field) or "").strip():
                    return self._error(400, f"{field} is required")
            known = providers.adapter_names(store.home())
            if body["adapter"] not in known:
                return self._error(400, f"unknown adapter {body['adapter']!r}; "
                                        f"have {', '.join(sorted(known))}")
            config = body.get("config") or {}
            if not isinstance(config, dict):
                return self._error(400, "config must be a JSON object")
            con = self._con()
            try:
                row = store.create_source(
                    con, name=body["name"], provider=body["provider"],
                    adapter=body["adapter"],
                    account=str(body.get("account") or ""),
                    config=config, slug=body.get("slug"),
                    enabled=bool(body.get("enabled", True)))
                self.server.poller.request_refresh(row["source_id"])
                return self._send(201, self._payload(con, row))
            except Exception as exc:
                return self._error(400, str(exc))
            finally:
                con.close()
        return self._error(404, "unknown route")

    def do_PATCH(self):
        if not self._boundary_ok():
            return self._error(403, "loopback only")
        self.server.poller.note_reader()
        path = urllib.parse.urlsplit(self.path).path
        if not path.startswith("/api/v1/sources/"):
            return self._error(404, "unknown route")
        if not self._authed():
            return self._error(401, "bearer token required")
        selector = path[len("/api/v1/sources/"):]
        body = self._body()
        revision = body.pop("revision", None)
        con = self._con()
        try:
            row = store.update_source(con, selector, body, revision=revision)
            return self._send(200, self._payload(con, row))
        except KeyError as exc:
            return self._error(404, str(exc))
        except ValueError as exc:
            status = 409 if "revision" in str(exc) else 400
            return self._error(status, str(exc))
        finally:
            con.close()

    def do_DELETE(self):
        if not self._boundary_ok():
            return self._error(403, "loopback only")
        self.server.poller.note_reader()
        path = urllib.parse.urlsplit(self.path).path
        if not path.startswith("/api/v1/sources/"):
            return self._error(404, "unknown route")
        if not self._authed():
            return self._error(401, "bearer token required")
        selector = path[len("/api/v1/sources/"):]
        con = self._con()
        try:
            if not store.delete_source(con, selector):
                return self._error(404, f"no source {selector!r}")
            return self._send(200, {"deleted": selector})
        finally:
            con.close()

    # --- handlers -----------------------------------------------------------

    def _holds(self) -> dict:
        """{source_id: reason} for every enabled source a fresh, definitive
        zero is holding. Cheap on purpose: a scheduler may ask every tick."""
        con = self._con()
        try:
            holds = {}
            for row in store.all_sources(con):
                if not row["enabled"] or row["archived"]:
                    continue
                latest = store.latest_reading(con, row["source_id"])
                hold = model.source_hold(dict(latest) if latest else None)
                if hold:
                    holds[row["source_id"]] = hold
            return holds
        finally:
            con.close()

    def _status(self, *, passive=False):
        con = self._con()
        try:
            rows = [row for row in store.all_sources(con)
                    if row["enabled"] and not row["archived"]]
            sources = [self._payload(con, row) for row in rows]
            claude_present = any(row["adapter"] == "claude" for row in rows)
        finally:
            con.close()
        # A demand source served stale or unknown refreshes in the background,
        # so the next read (the dashboard reloads in 45s) carries fresh data.
        poller = self.server.poller
        for row, payload in zip(rows, sources):
            if not passive and payload["status"] in ("stale", "unknown") and \
                    poller.is_demand(row):
                poller.request_refresh(row["source_id"])
        return self._send(200, {
            "generated_at": store.now(),
            "schema_version": 2, "passive": passive,
            "sources": sources,
            "holds": self._holds(),
            "claude_auth": claude_provider.cached_auth_status()
            if claude_present and not passive else None,
        })

    def _login(self, selector: str):
        con = self._con()
        try:
            row = store.find_source(con, selector)
        finally:
            con.close()
        if row is None:
            return self._error(404, f"no source {selector!r}")
        try:
            config = providers.source_config(row)
        except ValueError:
            config = {}
        command = login_command(row, config)
        if command is None:
            return self._error(400, f"{row['adapter']} has no interactive "
                                    "login; set an API key instead")
        if not open_in_terminal(command):
            return self._error(502, f"could not open Terminal; run this "
                                    f"yourself: {command}")
        return self._send(200, {"launched": True, "command": command,
                                "note": "finish the sign-in in Terminal, "
                                        "then refresh the source"})

    def _refresh(self, selector: str):
        con = self._con()
        try:
            row = store.find_source(con, selector)
        finally:
            con.close()
        if row is None:
            return self._error(404, f"no source {selector!r}")
        try:
            wait = max(0.0, min(float(self._query().get("wait", 30)), 40.0))
        except ValueError:
            return self._error(400, "bad wait parameter")
        done = self.server.poller.request_refresh(row["source_id"])
        finished = done.wait(wait)
        con = self._con()
        try:
            payload = self._payload(con, store.find_source(con, selector))
        finally:
            con.close()
        if finished:
            return self._send(200, payload)
        payload["refresh"] = "pending"
        return self._send(202, payload)
