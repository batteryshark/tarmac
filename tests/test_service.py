"""Service-layer tests: store, payloads, poller, HTTP API, plugins."""
import json
import os
import stat
import tempfile
import threading
import time
import unittest
from unittest.mock import patch
import urllib.error
import urllib.request
from pathlib import Path

_TMP = tempfile.mkdtemp()
os.environ["TARMAC_HOME"] = _TMP

from tarmac import model, payloads, poller as poller_mod, providers, store  # noqa: E402
from tarmac.api import TarmacServer  # noqa: E402
from tarmac.poller import Poller  # noqa: E402


def make_reading(source_id, *, remaining=50.0, definitive=True,
                 needs_login=False, observed="2026-08-31T10:00:00+00:00",
                 windows=None, fresh_until="2099-01-01T00:00:00+00:00"):
    return {
        "source_id": source_id, "remaining": remaining,
        "limit_value": 100.0, "unit": "percent", "resets_at": None,
        "as_of": observed, "fresh_until": fresh_until,
        "definitive": definitive, "needs_login": needs_login,
        "reason": None, "windows": windows if windows is not None else
        [{"label": "weekly", "remaining": remaining, "unit": "percent",
          "resets_at": None, "stale": False, "stale_reason": None}],
        "raw": {}, "polled_at": observed,
    }


class StoreTests(unittest.TestCase):
    def setUp(self):
        self.db = Path(_TMP) / f"store-{self.id().split('.')[-1]}.db"
        self.con = store.connect(self.db)

    def tearDown(self):
        self.con.close()

    def test_create_and_slug_collision(self):
        a = store.create_source(self.con, name="Claude", provider="claude",
                                adapter="claude")
        b = store.create_source(self.con, name="Claude", provider="claude",
                                adapter="claude", account="work")
        self.assertEqual(a["slug"], "claude")
        self.assertEqual(b["slug"], "claude-2")

    def test_unique_provider_account(self):
        store.create_source(self.con, name="A", provider="kimi", adapter="kimi")
        with self.assertRaises(Exception):
            store.create_source(self.con, name="B", provider="kimi",
                                adapter="kimi")

    def test_update_revision_conflict(self):
        row = store.create_source(self.con, name="X", provider="zai",
                                  adapter="zai")
        with self.assertRaisesRegex(ValueError, "revision"):
            store.update_source(self.con, row["slug"], {"name": "Y"},
                                revision=99)
        updated = store.update_source(self.con, row["slug"], {"name": "Y"},
                                      revision=row["revision"])
        self.assertEqual(updated["name"], "Y")
        self.assertEqual(updated["revision"], row["revision"] + 1)

    def test_disable_keeps_history(self):
        row = store.create_source(self.con, name="G", provider="xai",
                                  adapter="xai")
        store.record_reading(self.con, make_reading(row["source_id"]))
        off = store.update_source(self.con, row["slug"], {"enabled": False})
        self.assertFalse(off["enabled"])
        self.assertEqual(
            len(store.readings_for(self.con, row["source_id"])), 1)
        on = store.update_source(self.con, row["slug"], {"enabled": True})
        self.assertTrue(on["enabled"])

    def test_delete_cascades_readings(self):
        row = store.create_source(self.con, name="D", provider="deepseek",
                                  adapter="deepseek")
        store.record_reading(self.con, make_reading(row["source_id"]))
        self.assertTrue(store.delete_source(self.con, row["slug"]))
        self.assertEqual(len(store.readings_for(self.con, row["source_id"])), 0)

    def test_token_created_0600(self):
        token = store.token()
        self.assertTrue(token)
        mode = stat.S_IMODE(os.stat(store.token_path()).st_mode)
        self.assertEqual(mode, 0o600)

    def test_prune_raw_keeps_numbers(self):
        row = store.create_source(self.con, name="P", provider="minimax",
                                  adapter="minimax")
        reading = make_reading(row["source_id"],
                               observed="2020-01-01T00:00:00+00:00")
        reading["raw"] = {"keep": "not-after-prune"}
        store.record_reading(self.con, reading)
        self.assertEqual(store.prune_raw(self.con, older_than_days=30), 1)
        latest = store.latest_reading(self.con, row["source_id"])
        self.assertIsNone(latest["raw_json"])
        self.assertEqual(latest["remaining"], 50.0)


class PayloadTests(unittest.TestCase):
    def setUp(self):
        self.db = Path(_TMP) / f"payload-{self.id().split('.')[-1]}.db"
        self.con = store.connect(self.db)
        self.row = store.create_source(self.con, name="C", provider="claude",
                                       adapter="claude")

    def tearDown(self):
        self.con.close()

    def _payload(self, **kw):
        readings = store.readings_for(self.con, self.row["source_id"])
        return payloads.source_payload(self.row, readings, **kw)

    def test_burn_between_adjacent_definitive_readings(self):
        windows = lambda remaining: [{"label": "weekly", "remaining": remaining,
            "unit": "percent", "resets_at": "2099-01-01T00:00:00Z", "stale": False}]
        store.record_reading(self.con, make_reading(
            self.row["source_id"], remaining=60, windows=windows(60),
            observed="2026-08-31T10:00:00+00:00"))
        store.record_reading(self.con, make_reading(
            self.row["source_id"], remaining=50, windows=windows(50),
            observed="2026-08-31T12:00:00+00:00"))
        payload = self._payload()
        self.assertEqual(payload["burn_rate"], 5.0)  # 10% over 2h

    def test_no_burn_across_reset_boundary(self):
        w1 = [{"label": "weekly", "remaining": 10, "unit": "percent",
               "resets_at": "2099-01-01T00:00:00Z", "stale": False,
               "stale_reason": None}]
        w2 = [{"label": "weekly", "remaining": 90, "unit": "percent",
               "resets_at": "2099-02-01T00:00:00Z", "stale": False,
               "stale_reason": None}]
        store.record_reading(self.con, make_reading(
            self.row["source_id"], remaining=10, windows=w1,
            observed="2026-08-31T10:00:00+00:00"))
        store.record_reading(self.con, make_reading(
            self.row["source_id"], remaining=90, windows=w2,
            observed="2026-08-31T12:00:00+00:00"))
        self.assertIsNone(self._payload()["burn_rate"])

    def test_needs_login_surfaces(self):
        reading = make_reading(self.row["source_id"], remaining=None,
                               definitive=False, needs_login=True)
        reading["reason"] = "not logged in — run claude /login"
        store.record_reading(self.con, reading)
        payload = self._payload()
        self.assertTrue(payload["needs_login"])
        self.assertIn("not logged in", payload["latest"]["reason"])

    def test_disabled_status(self):
        store.update_source(self.con, self.row["slug"], {"enabled": False})
        self.row = store.find_source(self.con, self.row["slug"])
        self.assertEqual(self._payload()["status"], "disabled")


class PollerTests(unittest.TestCase):
    def setUp(self):
        self.db = Path(_TMP) / f"poller-{self.id().split('.')[-1]}.db"
        con = store.connect(self.db)
        self.row = store.create_source(con, name="C", provider="claude",
                                       adapter="claude")
        con.close()
        self.calls = []
        self.notes = []
        self.orig = providers.poll_source

    def tearDown(self):
        providers.poll_source = self.orig

    def _stub(self, result=None):
        def fake(source, home):
            self.calls.append(source["source_id"])
            time.sleep(0.05)
            return result or make_reading(source["source_id"])
        providers.poll_source = fake

    def test_concurrent_refreshes_share_one_poll(self):
        self._stub()
        p = Poller(self.db, notifier=lambda *a: self.notes.append(a))
        events = [p.request_refresh(self.row["source_id"]) for _ in range(3)]
        p.tick({self.row["source_id"]})
        for event in events:
            self.assertTrue(event.wait(1))
        self.assertEqual(len(self.calls), 1)

    def test_needs_login_transition_notifies_once(self):
        p = Poller(self.db, notifier=lambda *a: self.notes.append(a))
        self._stub(make_reading(self.row["source_id"], remaining=None,
                                definitive=False, needs_login=True))
        p.tick()
        p.tick()
        self.assertEqual(len(self.notes), 1)
        self.assertIn("login", self.notes[0][1])
        self._stub(make_reading(self.row["source_id"]))
        p.tick()
        self.assertEqual(len(self.notes), 2)
        self.assertIn("restored", self.notes[1][1])

    def test_disabled_source_not_polled(self):
        con = store.connect(self.db)
        store.update_source(con, self.row["slug"], {"enabled": False})
        con.close()
        self._stub()
        Poller(self.db, notifier=lambda *a: None).tick()
        self.assertEqual(self.calls, [])

    def test_refresh_of_disabled_source_completes(self):
        con = store.connect(self.db)
        store.update_source(con, self.row["slug"], {"enabled": False})
        con.close()
        self._stub()
        p = Poller(self.db, notifier=lambda *a: None)
        event = p.request_refresh(self.row["source_id"])
        p.tick({self.row["source_id"]})
        self.assertTrue(event.wait(1))
        self.assertEqual(self.calls, [])

    def test_demand_source_polls_only_with_a_reader(self):
        con = store.connect(self.db)
        row = store.create_source(con, name="M", provider="meta",
                                  adapter="meta", config={"demand": True})
        con.close()
        self._stub()
        p = Poller(self.db, notifier=lambda *a: None)
        p.tick()  # nobody has read anything: the source costs nothing
        self.assertNotIn(row["source_id"], self.calls)
        p.note_reader()
        p.tick()
        self.assertIn(row["source_id"], self.calls)

    def test_demand_garbage_config_polls_normally(self):
        con = store.connect(self.db)
        row = store.create_source(con, name="G", provider="meta",
                                  adapter="meta")
        con.execute("UPDATE sources SET config_json='{not json' WHERE slug=?",
                    (row["slug"],))
        con.commit()
        con.close()
        self._stub()
        Poller(self.db, notifier=lambda *a: None).tick()
        self.assertIn(row["source_id"], self.calls)


class PluginTests(unittest.TestCase):
    def test_plugin_discovery_and_poll(self):
        plugin_dir = Path(_TMP) / "plugins"
        plugin_dir.mkdir(exist_ok=True)
        script = plugin_dir / "fakeplug"
        script.write_text("#!/bin/sh\n"
                          "echo '{\"remaining\": 42.5, \"unit\": \"USD\"}'\n")
        script.chmod(0o755)
        self.assertIn("fakeplug", providers.adapter_names(Path(_TMP)))
        db = Path(_TMP) / "plugin.db"
        con = store.connect(db)
        row = store.create_source(con, name="FP", provider="fakeprov",
                                  adapter="fakeplug")
        con.close()
        reading = providers.poll_source(row, Path(_TMP))
        self.assertEqual(reading["remaining"], 42.5)
        self.assertEqual(reading["unit"], "USD")
        self.assertTrue(reading["definitive"])


class ApiTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.db = Path(_TMP) / "api.db"
        store.connect(cls.db).close()
        cls.calls = []

        def fake(source, home):
            cls.calls.append(source["source_id"])
            return make_reading(source["source_id"])
        cls.orig = providers.poll_source
        providers.poll_source = fake
        cls.poller = Poller(cls.db, notifier=lambda *a: None)
        cls.poller.start()
        cls.server = TarmacServer(cls.poller, port=0, db_path=cls.db)
        cls.port = cls.server.server_address[1]
        threading.Thread(target=cls.server.serve_forever, daemon=True).start()

    @classmethod
    def tearDownClass(cls):
        cls.poller.stop()
        cls.server.shutdown()
        cls.server.server_close()
        providers.poll_source = cls.orig

    def _call(self, method, path, body=None, *, authed=False, ok=True):
        headers = {"Content-Type": "application/json"}
        if authed:
            headers["Authorization"] = f"Bearer {store.token()}"
        req = urllib.request.Request(
            f"http://127.0.0.1:{self.port}{path}", method=method,
            headers=headers,
            data=json.dumps(body).encode() if body is not None else None)
        try:
            with urllib.request.urlopen(req, timeout=10) as resp:
                return resp.status, json.loads(resp.read() or b"{}")
        except urllib.error.HTTPError as exc:
            if ok:
                raise
            return exc.code, json.loads(exc.read() or b"{}")

    def test_lifecycle(self):
        status, _ = self._call("GET", "/health")
        self.assertEqual(status, 200)

        # Mutations need the token.
        status, _ = self._call("POST", "/api/v1/sources",
                               {"name": "K", "provider": "kimi",
                                "adapter": "kimi"}, ok=False)
        self.assertEqual(status, 401)

        status, source = self._call("POST", "/api/v1/sources",
                                    {"name": "K", "provider": "kimi",
                                     "adapter": "kimi"}, authed=True)
        self.assertEqual(status, 201)
        slug = source["slug"]

        status, _ = self._call("POST", "/api/v1/sources",
                               {"name": "B", "provider": "b",
                                "adapter": "no-such"}, authed=True, ok=False)
        self.assertEqual(status, 400)

        status, refreshed = self._call(
            "POST", f"/api/v1/sources/{slug}/refresh?wait=5")
        self.assertEqual(status, 200)
        self.assertEqual(refreshed["latest"]["remaining"], 50.0)

        status, doc = self._call("GET", "/api/v1/status")
        self.assertEqual(status, 200)
        self.assertEqual([s["slug"] for s in doc["sources"]], [slug])
        self.assertIsNone(doc["claude_auth"])  # no claude source here

        status, _ = self._call("PATCH", f"/api/v1/sources/{slug}",
                               {"enabled": False}, authed=True)
        self.assertEqual(status, 200)
        _, doc = self._call("GET", "/api/v1/status")
        self.assertEqual(doc["sources"], [])  # disabled: does not show up
        _, listing = self._call("GET", "/api/v1/sources")
        self.assertEqual([s["slug"] for s in listing["sources"]], [slug])

        status, meta = self._call("GET", "/api/v1/meta")
        self.assertIn("openrouter", meta["adapters"])

        status, _ = self._call("DELETE", f"/api/v1/sources/{slug}",
                               authed=True)
        self.assertEqual(status, 200)

    def test_boundary_rejects_foreign_host(self):
        import http.client
        conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=5)
        conn.request("GET", "/api/v1/status", headers={"Host": "evil.example"})
        self.assertEqual(conn.getresponse().status, 403)
        conn.close()

    def test_demand_source_refreshes_on_read(self):
        con = store.connect(self.db)
        try:
            row = store.create_source(con, name="MD", provider="meta",
                                      adapter="meta",
                                      config={"demand": True})
        finally:
            con.close()
        try:
            self.poller.last_reader_at = 0.0  # idle: the tick would skip it
            con = store.connect(self.db)
            try:
                store.record_reading(con, make_reading(
                    row["source_id"], fresh_until="2020-01-01T00:00:00+00:00"))
            finally:
                con.close()
            before = len(self.calls)
            _, doc = self._call("GET", "/api/v1/status")
            mine = next(s for s in doc["sources"]
                        if s["id"] == row["source_id"])
            self.assertEqual(mine["status"], "stale")
            for _ in range(50):  # the read queued a background refresh
                if len(self.calls) > before:
                    break
                time.sleep(0.02)
            self.assertGreater(len(self.calls), before)
        finally:
            con = store.connect(self.db)
            try:
                store.delete_source(con, row["source_id"])
            finally:
                con.close()

    def test_snapshot_does_not_activate_or_refresh_demand(self):
        con = store.connect(self.db)
        row = store.create_source(con, name='passive', provider='meta', adapter='meta',
                                  account='passive', config={'demand': True})
        store.record_reading(con, make_reading(row['source_id'],
                                              fresh_until='2020-01-01T00:00:00Z'))
        con.close()
        self.poller.last_reader_at = 0
        with patch.object(self.poller, 'note_reader') as reader, \
             patch.object(self.poller, 'request_refresh') as refresh, \
             patch('tarmac.api.claude_provider.cached_auth_status') as auth:
            status, body = self._call('GET', '/api/v1/snapshot')
            self.assertEqual(status, 200)
            self.assertEqual(body['schema_version'], 2)
            self.assertTrue(body['passive'])
            reader.assert_not_called(); refresh.assert_not_called(); auth.assert_not_called()

        con = store.connect(self.db)
        store.delete_source(con, row["source_id"])
        con.close()



class OpenRouterTests(unittest.TestCase):
    def test_parse_balance(self):
        from tarmac.providers.simple import parse_openrouter
        reading = parse_openrouter(
            {"data": {"total_credits": 120.0, "total_usage": 45.25}})
        self.assertEqual(reading.remaining, 74.75)
        self.assertEqual(reading.unit, "USD")
        self.assertEqual(reading.kind, "api")
        self.assertIsNone(reading.resets_at)

    def test_shape_drift_degrades_to_unknown(self):
        from tarmac.providers.simple import parse_openrouter
        reading = parse_openrouter({"data": {}})
        self.assertIsNone(reading.remaining)
        self.assertTrue(reading.reason)

    def test_missing_key_reason(self):
        from tarmac.providers.simple import openrouter
        env = os.environ.pop("OPENROUTER_API_KEY", None)
        try:
            reading = openrouter(auth_path=Path(_TMP) / "no-auth.json")
        finally:
            if env is not None:
                os.environ["OPENROUTER_API_KEY"] = env
        self.assertIsNone(reading.remaining)
        self.assertIn("OPENROUTER_API_KEY", reading.reason)


class CredentialOverrideTests(unittest.TestCase):
    def test_per_source_key_env_and_auth_entry(self):
        from tarmac.providers.creds import api_key
        auth = Path(_TMP) / "auth.json"
        auth.write_text(json.dumps({
            "openrouter": {"key": "sk-default00000000000000000000000"},
            "openrouter-work": {"key": "sk-work000000000000000000000000"}}))
        key, label = api_key("openrouter", None, auth)
        self.assertEqual(key, "sk-default00000000000000000000000")
        key, label = api_key("openrouter",
                             {"auth_entry": "openrouter-work"}, auth)
        self.assertEqual(key, "sk-work000000000000000000000000")
        self.assertIn("openrouter-work", label)
        os.environ["OR_SECOND"] = "sk-env0000000000000000000000000"
        try:
            key, label = api_key("openrouter", {"key_env": "OR_SECOND"},
                                 Path(_TMP) / "no-auth.json")
        finally:
            del os.environ["OR_SECOND"]
        self.assertEqual(key, "sk-env0000000000000000000000000")
        self.assertEqual(label, "$OR_SECOND")


class MultiAccountTests(unittest.TestCase):
    AUTH_OK = staticmethod(lambda: {"logged_in": True, "method": "test",
                                    "checked_at": "", "reason": None})

    def test_claude_config_dir_selects_cache(self):
        from tarmac.providers import claude as claude_mod
        account = Path(_TMP) / "claude-work"
        account.mkdir(exist_ok=True)
        (account / ".claude.json").write_text(json.dumps({
            "cachedUsageUtilization": {
                "fetchedAtMs": time.time() * 1000,
                "utilization": {"five_hour": {
                    "utilization": 25.0,
                    "resets_at": "2099-01-01T00:00:00Z"}}}}))
        reading = claude_mod.claude({"config_dir": str(account)},
                                    auth=self.AUTH_OK, screen=lambda s: {})
        self.assertEqual(reading.remaining, 75.0)

    def test_claude_needs_login_names_the_account(self):
        from tarmac.providers import claude as claude_mod
        logged_out = lambda: {"logged_in": False, "method": None,  # noqa: E731
                              "checked_at": "", "reason": None}
        reading = claude_mod.claude({"config_dir": "~/claude-work"},
                                    auth=logged_out)
        self.assertTrue(reading.needs_login)
        self.assertIn("claude-work", reading.reason)

    def test_codex_auth_status_semantics(self):
        from tarmac.providers import codex as codex_mod

        def runner_for(rc, out):
            def runner(argv, **kw):
                class R:
                    returncode, stdout, stderr = rc, out, ""
                return R()
            return runner
        ok = codex_mod.auth_status(runner=runner_for(0, "Logged in using ChatGPT"))
        self.assertIs(ok["logged_in"], True)
        out = codex_mod.auth_status(runner=runner_for(1, "Not logged in"))
        self.assertIs(out["logged_in"], False)
        odd = codex_mod.auth_status(runner=runner_for(2, "something exploded"))
        self.assertIsNone(odd["logged_in"])  # probe failure is never a logout

    def test_codex_home_selects_sessions(self):
        from tarmac.providers import codex as codex_mod
        home_dir = Path(_TMP) / "codex-work"
        sessions = home_dir / "sessions"
        sessions.mkdir(parents=True, exist_ok=True)
        (sessions / "rollout-1.jsonl").write_text(json.dumps({
            "timestamp": "2099-01-01T00:00:00Z",
            "payload": {"type": "token_count", "rate_limits": {
                "primary": {"used_percent": 40.0, "window_minutes": 10080,
                            "resets_at": "2099-01-02T00:00:00Z"}}}}) + "\n")

        def boom():
            raise RuntimeError("no app server in tests")
        reading = codex_mod.codex({"codex_home": str(home_dir)},
                                  auth=self.AUTH_OK, reader=boom)
        self.assertEqual(reading.remaining, 60.0)


class LoginTests(unittest.TestCase):
    def _row(self, adapter):
        db = Path(_TMP) / f"login-{adapter}.db"
        con = store.connect(db)
        try:
            return store.create_source(con, name=adapter, provider=adapter,
                                       adapter=adapter)
        finally:
            con.close()

    def test_login_command_shapes(self):
        from tarmac.api import login_command
        self.assertEqual(login_command(self._row("claude"), {}),
                         "claude auth login")
        cmd = login_command(self._row("codex"),
                            {"codex_home": "~/tarmac accounts/codex2"})
        self.assertIn("codex login", cmd)
        self.assertIn("mkdir -p", cmd)
        self.assertIn("CODEX_HOME='", cmd)  # space in the path is quoted
        self.assertIsNone(login_command(self._row("openrouter"), {}))

    def test_login_endpoint(self):
        import tarmac.api as api_mod
        db = Path(_TMP) / "login-api.db"
        con = store.connect(db)
        claude_row = store.create_source(con, name="C", provider="claude",
                                         adapter="claude")
        kimi_row = store.create_source(con, name="K", provider="kimi",
                                       adapter="kimi")
        con.close()
        launched = []
        orig = api_mod.open_in_terminal
        api_mod.open_in_terminal = lambda cmd: launched.append(cmd) or True
        poller = Poller(db, notifier=lambda *a: None)
        server = TarmacServer(poller, port=0, db_path=db)
        port = server.server_address[1]
        threading.Thread(target=server.serve_forever, daemon=True).start()
        try:
            req = urllib.request.Request(
                f"http://127.0.0.1:{port}/api/v1/sources/"
                f"{claude_row['slug']}/login", method="POST")
            with urllib.request.urlopen(req, timeout=10) as resp:
                body = json.loads(resp.read())
            self.assertTrue(body["launched"])
            self.assertEqual(launched, ["claude auth login"])
            req = urllib.request.Request(
                f"http://127.0.0.1:{port}/api/v1/sources/"
                f"{kimi_row['slug']}/login", method="POST")
            with self.assertRaises(urllib.error.HTTPError) as caught:
                urllib.request.urlopen(req, timeout=10)
            self.assertEqual(caught.exception.code, 400)
        finally:
            api_mod.open_in_terminal = orig
            server.shutdown()
            server.server_close()


class HoldTests(unittest.TestCase):
    def test_only_fresh_definitive_zero_holds(self):
        base = make_reading("s", remaining=0.0)
        self.assertIn("exhausted", model.source_hold(base))
        self.assertIsNone(model.source_hold(make_reading("s", remaining=1.0)))
        self.assertIsNone(model.source_hold(
            make_reading("s", remaining=0.0, definitive=False)))
        expired = make_reading("s", remaining=0.0,
                               fresh_until="2020-01-01T00:00:00+00:00")
        self.assertIsNone(model.source_hold(expired))
        self.assertIsNone(model.source_hold(None))


if __name__ == "__main__":
    unittest.main()
