"""Provider adapters against synthetic fixtures, never live accounts.

Fixtures preserve protocol shapes, including partial and expired readings.
Failures must return unknown-with-reason without exposing credentials.
"""
import importlib
import json
import os
import tempfile
import time
import pathlib
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest import mock

from tarmac import model
from tarmac.providers import BUILTINS

claude_mod = importlib.import_module("tarmac.providers.claude")
agy_mod = importlib.import_module("tarmac.providers.antigravity")
codex_mod = importlib.import_module("tarmac.providers.codex")
xai_mod = importlib.import_module("tarmac.providers.xai")
simple = importlib.import_module("tarmac.providers.simple")
creds = importlib.import_module("tarmac.providers.creds")

# Every adapter that needs a credential is pointed at this: an auth.json that
# is not there, so the lookup falls through to an environment variable the
# test also clears. No fixture in this file holds key-shaped material.
NO_AUTH = "/nonexistent/opencode/auth.json"
NO_KEYS = {env: "" for _, env in creds.KEY_SOURCES.values()}

# claude() probes the login first; its default spawns the real CLI. Every
# test injects this instead.
AUTH_OK = lambda: {"logged_in": True, "method": "test",  # noqa: E731
                   "checked_at": "", "reason": None}

_HOME = None


def setUpModule() -> None:
    global _HOME
    _HOME = tempfile.TemporaryDirectory()
    os.environ["TARMAC_HOME"] = _HOME.name


def tearDownModule() -> None:
    os.environ.pop("TARMAC_HOME", None)
    _HOME.cleanup()


def _iso_in(hours: float) -> str:
    return (datetime.now(timezone.utc) + timedelta(hours=hours)).isoformat()


def _epoch_in(hours: float) -> int:
    return int(time.time() + hours * 3600)


DEEPSEEK_OK = {
    "is_available": True,
    "balance_infos": [
        {"currency": "CNY", "total_balance": "72.00", "granted_balance": "0.00",
         "topped_up_balance": "72.00"},
        {"currency": "USD", "total_balance": "10.50", "granted_balance": "0.50",
         "topped_up_balance": "10.00"},
    ],
}

# Decimal-string quota fixture: limits carries the burst window and usage
# carries the exhausted plan-wide window.
KIMI_OK = {
    "usage": {"limit": "2000", "used": "2000", "resetTime": _iso_in(48)},
    "limits": [{"window": {"duration": 300, "timeUnit": "TIME_UNIT_MINUTE"},
                "detail": {"limit": "2000", "remaining": "1280",
                           "resetTime": _iso_in(3)}}],
    "user": {"membership": {"level": "pro"}},
    "subType": "TYPE_PURCHASE",
}


def minimax_fixture(interval=64, weekly=88, status=0):
    now_ms = time.time() * 1000
    row = {"model_name": "general",
           "start_time": now_ms - 3600_000, "end_time": now_ms + 14400_000,
           "current_interval_remaining_percent": interval,
           "weekly_start_time": now_ms - 86400_000,
           "weekly_end_time": now_ms + 518400_000,
           "current_weekly_remaining_percent": weekly}
    return {"model_remains": [dict(row, model_name="video"), row],
            "base_resp": {"status_code": status, "status_msg": "ok"}}


def claude_fixture(five_hour_used=12, seven_day_used=40, age_min=30):
    return {
        "someOtherKey": "ignored",
        "cachedUsageUtilization": {
            "fetchedAtMs": (time.time() - age_min * 60) * 1000,
            "accountUuid": "00000000-0000-0000-0000-000000000000",
            "utilization": {
                "five_hour": {"utilization": five_hour_used,
                              "resets_at": _iso_in(2),
                              "limit_dollars": None},
                "seven_day": {"utilization": seven_day_used,
                              "resets_at": _iso_in(80)},
                "seven_day_opus": None,
                "extra_usage": {"is_enabled": False},
            },
        },
    }


def codex_lines(used=25.0, resets_in_h=6, secondary=None):
    other = json.dumps({"timestamp": "2026-08-13T10:00:00.000Z",
                        "type": "event_msg", "payload": {"type": "agent_message"}})
    event = json.dumps({
        "timestamp": "2026-08-13T10:05:00.000Z",
        "type": "event_msg",
        "payload": {
            "type": "token_count",
            "info": {"total_token_usage": {"total_tokens": 1234},
                     "model_context_window": 258400},
            "rate_limits": {
                "limit_id": "codex", "plan_type": "prolite",
                "primary": {"used_percent": used, "window_minutes": 10080,
                            "resets_at": _epoch_in(resets_in_h)},
                "secondary": secondary,
                "credits": {"has_credits": False, "balance": "0"},
                "rate_limit_reached_type": None,
            },
        },
    })
    stale = event.replace('"used_percent": %s' % used, '"used_percent": 99.0')
    return [other, stale, event]  # the LAST token_count event wins


class ResultShapeTests(unittest.TestCase):
    def test_every_adapter_returns_the_same_record(self) -> None:
        names = []
        with mock.patch.dict(os.environ, NO_KEYS):
            for adapter in BUILTINS.values():
                with self.subTest(adapter.__name__):
                    got = adapter(**({"sessions_dir": "/nonexistent",
                                      "reader": lambda: {}}
                                     if adapter is codex_mod.codex else
                                     {"screen": lambda: []}
                                     if adapter is agy_mod.antigravity else
                                     {"path": "/nonexistent", "auth": AUTH_OK}
                                     if adapter is claude_mod.claude else
                                     {"auth_path": NO_AUTH}))
                    self.assertIsInstance(got, model.Reading)
                    self.assertFalse(got.known)
                    self.assertTrue(got.reason)
                    # ``limit`` is absent: a window's limit is always
                    # 100% of the window, so the field said nothing.
                    self.assertEqual(set(got.as_dict()), {
                        "provider", "remaining", "unit", "resets_at", "raw",
                        "as_of", "reason", "kind", "stale", "windows",
                        "needs_login"})
                    names.append(got.provider)
        self.assertEqual(
            sorted(names),
            ["antigravity", "claude", "codex", "deepseek", "kimi", "meta",
             "minimax", "openrouter", "xai", "zai"])
        self.assertEqual(len(names), len(set(names)))


class ProviderKindTests(unittest.TestCase):
    def test_only_an_api_provider_has_money_at_all(self) -> None:
        for provider in ("antigravity", "claude", "codex", "kimi", "meta",
                         "minimax", "xai"):
            self.assertEqual(model.kind_of(provider), "plan", provider)
        for provider in ("deepseek", "zai", "openrouter", "opencode"):
            self.assertEqual(model.kind_of(provider), "api", provider)


class KeyLookupTests(unittest.TestCase):
    """A key comes from OpenCode's store, then the named environment
    variable. Nothing here uses or asserts a real credential."""

    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.auth = Path(self.tmp.name) / "auth.json"
        self.addCleanup(self.tmp.cleanup)

    def _write(self, data) -> Path:
        self.auth.write_text(json.dumps(data))
        return self.auth

    def test_opencode_entry_wins_over_the_environment(self) -> None:
        self._write({"deepseek": {"type": "api", "key": "from-opencode"}})
        with mock.patch.dict(os.environ, {"DEEPSEEK_API_KEY": "from-env"}):
            value, source = creds.api_key("deepseek", None, self.auth)
        self.assertEqual(value, "from-opencode")
        self.assertIn("deepseek", source)
        self.assertNotIn(value, source)

    def test_an_oauth_entry_uses_its_access_token(self) -> None:
        """OpenCode stores xai as a grant, not an api key."""
        self._write({"xai": {"type": "oauth", "access": "grant",
                             "refresh": "r", "expires": 1}})
        self.assertEqual(creds.api_key("xai", None, self.auth)[0], "grant")

    def test_the_named_variable_is_the_fallback(self) -> None:
        cases = {
            "blank store entry": self._write({"deepseek": {"key": "  "}}),
            "missing store": Path("/nonexistent/auth.json"),
        }
        for label, auth in cases.items():
            with self.subTest(label), \
                    mock.patch.dict(os.environ,
                                    {"DEEPSEEK_API_KEY": "from-env"}):
                self.assertEqual(creds.api_key("deepseek", auth_path=auth),
                                 ("from-env", "$DEEPSEEK_API_KEY"))

    def test_no_key_anywhere_names_both_places_it_looked(self) -> None:
        self.auth.write_text("{not json")
        with mock.patch.dict(os.environ, NO_KEYS):
            value, reason = creds.api_key("minimax", None, self.auth)
        self.assertIsNone(value)
        self.assertIn("minimax-coding-plan", reason)
        self.assertIn("MINIMAX_API_KEY", reason)


class DeepSeekTests(unittest.TestCase):
    def test_prefers_usd_balance(self) -> None:
        got = simple.parse_deepseek(DEEPSEEK_OK)
        self.assertTrue(got.known)
        self.assertEqual((got.provider, got.remaining, got.unit),
                         ("deepseek", 10.5, "USD"))
        self.assertIsNone(got.resets_at)  # a prepaid balance has no window
        self.assertEqual(got.kind, "api")
        self.assertEqual(got.windows, [])

    def test_missing_and_garbage_shapes_are_unknown(self) -> None:
        cases = {
            "missing balances": {},
            "missing total": {"balance_infos": [{"currency": "USD"}]},
            "nonnumeric total": {"balance_infos": [{"total_balance": "abc"}]},
        }
        for label, data in cases.items():
            with self.subTest(label):
                got = simple.parse_deepseek(data)
                self.assertFalse(got.known)
                self.assertTrue(got.reason)


META_EVENT = {
    "subscription": {
        "tier": "synthetic-tier",
        "weekly": {"resets_at": _epoch_in(80), "used_percent": 0},
        "window": {"resets_at": _epoch_in(2), "used_percent": 12.5,
                   "window_duration_mins": 300},
    },
    "type": "response.subscription_usage",
}


class MetaTests(unittest.TestCase):
    """Subscription windows carried by a synthetic SSE event."""

    def test_both_windows_are_reported_under_one_provider(self) -> None:
        got = simple.parse_meta(META_EVENT)
        self.assertEqual([(w["label"], w["remaining"]) for w in got.windows],
                         [("5h", 87.5), ("weekly", 100.0)])
        self.assertEqual((got.provider, got.remaining, got.unit),
                         ("meta", 87.5, "percent"))  # the tightest window
        self.assertEqual(got.kind, "plan")  # a subscription, never money
        self.assertTrue(got.windows[0]["resets_at"].endswith("Z"))
        self.assertEqual(got.raw["tier"], "synthetic-tier")

    def test_broken_shapes_are_unknown(self) -> None:
        cases = {
            "no subscription": {},
            "not an object": {"subscription": "nope"},
            "no windows": {"subscription": {"tier": "t"}},
            "nonnumeric usage": {"subscription": {
                "window": {"used_percent": "lots"}}},
        }
        for label, data in cases.items():
            with self.subTest(label):
                got = simple.parse_meta(data)
                self.assertFalse(got.known)
                self.assertTrue(got.reason)

    def test_the_stream_event_becomes_the_reading(self) -> None:
        lines = ["event: response.subscription_usage",
                 "data: " + json.dumps(META_EVENT), ""]
        with mock.patch.dict(os.environ, {"META_API_KEY": "test-key"}):
            got = simple.meta(auth_path=NO_AUTH,
                              stream=lambda *a: (META_EVENT, None))
        self.assertTrue(got.known)
        self.assertEqual(got.remaining, 87.5)
        # The wire parser finds the event among the SSE noise and stops.
        with mock.patch.dict(os.environ, {"META_API_KEY": "test-key"}), \
                mock.patch.object(simple.urllib.request, "urlopen",
                                  return_value=_FakeSse(lines)):
            got = simple.meta(auth_path=NO_AUTH)
        self.assertEqual(got.remaining, 87.5)

    def test_stream_failures_are_unknown_with_a_reason(self) -> None:
        cases = {
            "http error": (None, "http 429 Too Many Requests"),
            "no event": (None, "no subscription event in stream"),
            "garbage event": (None, "subscription event was not JSON"),
        }
        for label, (_, err) in cases.items():
            with self.subTest(label), \
                    mock.patch.dict(os.environ, {"META_API_KEY": "test-key"}):
                got = simple.meta(auth_path=NO_AUTH,
                                  stream=lambda *a: (None, err))
                self.assertFalse(got.known)
                self.assertEqual(got.reason, err)

    def test_no_key_anywhere_names_both_places_it_looked(self) -> None:
        with mock.patch.dict(os.environ, NO_KEYS):
            got = simple.meta(auth_path=NO_AUTH)
        self.assertFalse(got.known)
        self.assertIn("meta", got.reason)
        self.assertIn("META_API_KEY", got.reason)


class _FakeSse:
    """A urlopen stand-in yielding raw SSE byte lines."""

    def __init__(self, lines: list[str]):
        self._lines = [line.encode() + b"\r\n" for line in lines]

    def __enter__(self):
        return iter(self._lines)

    def __exit__(self, *args):
        return False


class ZaiTests(unittest.TestCase):
    """Synthetic wallet response with no quota window."""

    OK = {"code": 200, "msg": "Operation successful", "success": True,
          "data": {"accountList": [{"customerId": 1000000000000001,
                                    "balance": 12.5, "type": 10,
                                    "enableStatus": "ENABLE",
                                    "frozenBalance": 0.0}],
                   "totalBalance": 12.5, "isKa": False}}

    def test_wallet_balance_is_the_runway(self) -> None:
        got = simple.parse_zai(self.OK)
        self.assertTrue(got.known)
        self.assertEqual((got.provider, got.remaining, got.unit),
                         ("zai", 12.5, "USD"))
        self.assertIsNone(got.resets_at)  # a prepaid wallet has no window
        self.assertEqual((got.kind, got.windows), ("api", []))

    def test_missing_and_garbage_shapes_are_unknown(self) -> None:
        cases = {
            "empty": {},
            "no accounts": {"data": {"accountList": []}},
            "nonnumeric total": {"data": {"totalBalance": "abc",
                                          "accountList": [{}]}},
        }
        for label, data in cases.items():
            with self.subTest(label):
                got = simple.parse_zai(data)
                self.assertFalse(got.known)
                self.assertTrue(got.reason)

    def test_an_account_without_its_customer_id_says_so(self) -> None:
        """The key authenticates but does not identify the account, so the
        source config has to carry the customer id."""
        got = simple.zai({})
        self.assertFalse(got.known)
        self.assertIn("customer_id", got.reason)


class KimiTests(unittest.TestCase):
    def test_both_the_burst_window_and_the_plan_quota_are_reported(self) -> None:
        """``limits[]`` is only the 5-hour window. The plan quota in
        ``usage`` is a separate plan-wide window."""
        got = simple.parse_kimi(KIMI_OK)
        # 1280 of 2000 left over a 300-minute window; the plan quota is spent.
        self.assertEqual([(w["label"], w["remaining"]) for w in got.windows],
                         [("5h", 64.0), ("weekly", 0.0)])
        self.assertEqual((got.provider, got.remaining, got.unit),
                         ("kimi", 0.0, "percent"))
        self.assertNotIn("limit", got.windows[0])
        self.assertTrue(got.resets_at.endswith("Z"))
        self.assertEqual(got.raw["membership"], "pro")

    def test_a_weekly_in_limits_is_not_duplicated_by_usage(self) -> None:
        got = simple.parse_kimi({
            "usage": {"limit": "10", "used": "1", "resetTime": _iso_in(40)},
            "limits": [{"window": {"duration": 7, "timeUnit": "TIME_UNIT_DAY"},
                        "detail": {"limit": "10", "remaining": "5",
                                   "resetTime": _iso_in(40)}}]})
        self.assertEqual([(w["label"], w["remaining"]) for w in got.windows],
                         [("weekly", 50.0)])

    def test_the_plan_quota_alone_is_still_a_reading(self) -> None:
        got = simple.parse_kimi({"usage": {"limit": "4", "used": "3",
                                           "resetTime": _iso_in(40)}})
        self.assertEqual([(w["label"], w["remaining"]) for w in got.windows],
                         [("weekly", 25.0)])

    def test_broken_shapes_are_unknown(self) -> None:
        cases = {
            "missing windows": {},
            "nonnumeric quota": {"limits": [{"detail": {
                "remaining": "lots", "limit": "1000"}}]},
            "invalid reset": {"limits": [{"detail": {
                "remaining": "1", "limit": "2", "resetTime": "never"}}]},
        }
        for label, data in cases.items():
            with self.subTest(label):
                got = simple.parse_kimi(data)
                self.assertFalse(got.known)
                self.assertTrue(got.reason)


class MiniMaxTests(unittest.TestCase):
    def test_both_token_plan_windows_are_reported(self) -> None:
        got = simple.parse_minimax(minimax_fixture())
        self.assertEqual([(w["label"], w["remaining"]) for w in got.windows],
                         [("5h", 64.0), ("weekly", 88.0)])
        self.assertEqual((got.kind, got.unit, got.remaining),
                         ("plan", "percent", 64.0))
        self.assertEqual(got.raw["model_name"], "general")  # not the video row

    def test_a_refusal_carried_in_base_resp_is_unknown(self) -> None:
        """MiniMax answers 200 and puts the rejection in the body."""
        got = simple.parse_minimax(minimax_fixture(status=1004))
        self.assertFalse(got.known)
        self.assertIn("1004", got.reason)

    def test_broken_shapes_are_unknown(self) -> None:
        cases = {
            "missing rows": {},
            "missing percentages": {
                "model_remains": [{"model_name": "general"}]},
            "nonnumeric percentage": {"model_remains": [{
                "current_interval_remaining_percent": "lots"}]},
        }
        for label, data in cases.items():
            with self.subTest(label):
                got = simple.parse_minimax(data)
                self.assertFalse(got.known)
                self.assertTrue(got.reason)


# --- Grok / xAI --------------------------------------------------------------
# Every byte below is BUILT HERE. Nothing in this file touches the owner's
# OpenCode credentials, and no test makes a live call.

def _varint(value: int) -> bytes:
    out = bytearray()
    while True:
        byte, value = value & 0x7F, value >> 7
        out.append(byte | (0x80 if value else 0))
        if not value:
            return bytes(out)


def _bytes_field(number: int, payload: bytes) -> bytes:
    return _varint(number << 3 | 2) + _varint(len(payload)) + payload


def _proto_time(seconds: int) -> bytes:
    return _varint(1 << 3) + _varint(seconds)  # Timestamp.seconds


def _credit(token_id: str, expires: int) -> bytes:
    """One banked reset: field 10 is the REDEEMABLE ID, field 30 the expiry.
    The id is in the fixture precisely so a test can prove it never comes back
    out."""
    return _bytes_field(10, token_id.encode()) + \
        _bytes_field(30, _proto_time(expires))


def _frame(payload: bytes, flags: int = 0) -> bytes:
    return bytes([flags]) + len(payload).to_bytes(4, "big") + payload


def _resets_body(*credits: bytes, status: bytes = b"grpc-status:0\r\n") -> bytes:
    return _frame(b"".join(_bytes_field(10, c) for c in credits)) + \
        _frame(status, 0x80)


def _billing(used=None, start=_iso_in(-24), end=_iso_in(144)) -> dict:
    config = {"currentPeriod": {"type": "USAGE_PERIOD_TYPE_WEEKLY",
                                "start": start, "end": end}}
    if used is not None:  # proto3 JSON omits a zero, so absent means zero
        config["creditUsagePercent"] = used
    return {"config": config}


class XaiBillingTests(unittest.TestCase):
    """Synthetic subscription windows and optional reset-credit metadata."""

    def test_the_subscription_window_reads_as_a_weekly_window(self) -> None:
        got = xai_mod.parse_xai(_billing(used=30.0))
        self.assertEqual([(w["label"], w["remaining"]) for w in got.windows],
                         [("weekly", 70.0)])
        self.assertEqual((got.provider, got.kind, got.unit),
                         ("xai", "plan", "percent"))
        self.assertTrue(got.resets_at.endswith("Z"))
        self.assertEqual(got.raw["period"], "USAGE_PERIOD_TYPE_WEEKLY")

    def test_an_omitted_percentage_is_proto3_for_nothing_spent(self) -> None:
        self.assertEqual(xai_mod.parse_xai(_billing()).remaining, 100.0)

    def test_broken_shapes_are_unknown(self) -> None:
        cases = {
            "missing period": {},
            "period is not an object": {"config": {"currentPeriod": "nope"}},
            "invalid end": {"config": {"currentPeriod": {"end": "never"}}},
        }
        for label, data in cases.items():
            with self.subTest(label):
                got = xai_mod.parse_xai(data)
                self.assertFalse(got.known)
                self.assertTrue(got.reason)

    def test_reset_credits_are_optional_window_metadata(self) -> None:
        cases = {
            "credits present": (
                {"available": 2, "soonest_expiry": "2099-01-01T00:00:00Z"},
                "2 banked reset credits · soonest expires 2099-01-01"),
            "reset rpc unavailable": (None, None),
        }
        for label, (resets, expected) in cases.items():
            with self.subTest(label):
                got = xai_mod.parse_xai(_billing(used=10.0), resets)
                self.assertEqual(got.remaining, 90.0)
                self.assertEqual(model.credits_text(got.raw), expected)
                if resets is not None:
                    self.assertEqual(got.raw["credits"]["count"], 2)
                    self.assertEqual(got.raw["credits"]["expires_at"],
                                     resets["soonest_expiry"])


class XaiResetTests(unittest.TestCase):
    """The gRPC-Web reset rpc: framing and protobuf, both hand-rolled."""

    def test_unexpired_credits_are_counted_and_their_ids_never_returned(self) -> None:
        body = _resets_body(_credit("restok_live", int(time.time()) + 86400),
                            _credit("restok_also", int(time.time()) + 172800),
                            _credit("restok_gone", int(time.time()) - 60))
        got = xai_mod.parse_resets(body)
        self.assertEqual(got["available"], 2)  # the lapsed one is unspendable
        self.assertNotIn("restok", json.dumps(got))
        self.assertTrue(got["soonest_expiry"].endswith("Z"))

    def test_zero_credits_is_an_empty_message_not_an_error(self) -> None:
        self.assertEqual(xai_mod.parse_resets(_resets_body()),
                         {"available": 0, "soonest_expiry": None})

    def test_grpc_errors_are_raised_not_read_as_zero(self) -> None:
        cases = {
            "trailer": (
                _frame(b"grpc-status:16\r\ngrpc-message:no-credentials\r\n", 0x80),
                {}, "16"),
            "header": (_resets_body(status=b""), {"grpc-status": "7"}, "7"),
        }
        for label, (body, headers, status) in cases.items():
            with self.subTest(label), self.assertRaises(ValueError) as caught:
                xai_mod.parse_resets(body, headers)
            self.assertIn(f"grpc status {status}", str(caught.exception))
            self.assertNotIn("no-credentials", str(caught.exception))

    def test_malformed_frames_raise_rather_than_inventing_a_count(self) -> None:
        cases = {
            "no frame": b"",
            "truncated frame": b"\x00\x00\x00\x00\x40short",
            "truncated protobuf": _frame(b"\x0a\x7f"),
            "compressed frame": _frame(b"\x0a\x00", 0x01),
        }
        for label, body in cases.items():
            with self.subTest(label), self.assertRaises(ValueError):
                xai_mod.parse_resets(body)


class OpenRouterAdapterTests(unittest.TestCase):
    """/credits (account balance) and /key (this key's cap) on one reading.
    ``fetch`` is mocked per URL; the key is a placeholder, never key-shaped."""

    CREDITS = {"data": {"total_credits": 50.0, "total_usage": 12.5}}

    def _key(self, **fields):
        return {"data": {"limit": 10.0, "limit_remaining": 4.25,
                         "limit_reset": "monthly", "usage": 5.75,
                         "is_free_tier": False, **fields}}

    def _run(self, credits, key, config=None):
        """Each reply is a dict (JSON 200), a str (fetch error), or bytes."""
        def reply(value):
            if isinstance(value, str):
                return {}, b"", value
            if isinstance(value, bytes):
                return {}, value, None
            return {}, json.dumps(value).encode(), None

        def fake_fetch(url, key_value=None, *a, **kw):
            self.assertEqual(key_value, "placeholder")
            return reply(credits if url == simple.OPENROUTER_CREDITS_URL
                         else key)
        env = {k: v for k, v in os.environ.items()
               if k != "OPENROUTER_MANAGEMENT_KEY"}
        with mock.patch.object(simple, "api_key",
                               return_value=("placeholder", "test")), \
                mock.patch.object(simple, "fetch", side_effect=fake_fetch), \
                mock.patch.dict(os.environ, env, clear=True):
            got = simple.openrouter(config, auth_path=NO_AUTH)
        self.assertNotIn("placeholder", json.dumps(got.as_dict()))
        return got

    def _window(self, got, label):
        return next(w for w in got.windows if w["label"] == label)

    def test_both_endpoints_ok_scalar_is_the_tighter(self) -> None:
        got = self._run(self.CREDITS, self._key())
        self.assertEqual(self._window(got, "account credits")["remaining"], 37.5)
        cap = self._window(got, "key cap")
        self.assertEqual(cap["remaining"], 4.25)
        self.assertEqual(cap["unit"], "USD")
        self.assertTrue(cap["resets_at"].endswith("-01T00:00:00Z"))
        self.assertEqual((got.remaining, got.unit), (4.25, "USD"))
        self.assertEqual(got.resets_at, cap["resets_at"])
        self.assertIsNone(got.reason)
        self.assertEqual(got.raw["total_credits"], 50.0)
        self.assertEqual(got.raw["limit"], 10.0)

    def test_credits_tighter_than_cap_wins(self) -> None:
        got = self._run({"data": {"total_credits": 5, "total_usage": 4}},
                        self._key())
        self.assertEqual(got.remaining, 1.0)
        self.assertIsNone(got.resets_at)

    def test_null_limit_reports_no_cap_not_a_number(self) -> None:
        got = self._run(self.CREDITS, self._key(limit=None,
                                                limit_remaining=None,
                                                limit_reset=None))
        cap = self._window(got, "key cap")
        self.assertIsNone(cap["remaining"])
        self.assertEqual(cap["stale_reason"], "no cap")
        self.assertFalse(cap["stale"])
        self.assertEqual(got.remaining, 37.5)
        self.assertIsNone(got.reason)

    def test_credits_forbidden_degrades_only_that_window(self) -> None:
        for status in ("http 401 Unauthorized", "http 403 Forbidden"):
            with self.subTest(status):
                got = self._run(status, self._key())
                credits = self._window(got, "account credits")
                self.assertIsNone(credits["remaining"])
                self.assertIn("management key", credits["stale_reason"])
                self.assertEqual(got.remaining, 4.25)
                self.assertIsNone(got.reason)

    def test_credits_forbidden_and_no_cap_is_unknown_with_reason(self) -> None:
        got = self._run("http 403 Forbidden",
                        self._key(limit=None, limit_remaining=None))
        self.assertIsNone(got.remaining)
        self.assertIn("management key", got.reason)
        self.assertIn("no cap", got.reason)
        self.assertEqual(len(got.windows), 2)

    def test_malformed_json_never_raises_or_echoes(self) -> None:
        got = self._run(b"<html>sk-or-v1-oops", self._key())
        self.assertIn("not JSON",
                      self._window(got, "account credits")["stale_reason"])
        self.assertEqual(got.remaining, 4.25)
        self.assertNotIn("sk-or", json.dumps(got.as_dict()))
        got = self._run(self.CREDITS, b"{not json")
        self.assertIn("not JSON", self._window(got, "key cap")["stale_reason"])
        self.assertEqual(got.remaining, 37.5)
        got = self._run(b"garbage", b"garbage")
        self.assertIsNone(got.remaining)
        self.assertTrue(got.reason)

    def test_shape_drift_degrades_per_window(self) -> None:
        got = self._run({"data": {"total_credits": "x"}}, {"data": "nope"})
        self.assertIsNone(got.remaining)
        self.assertIn("total_credits", got.reason)
        self.assertIn("key response", got.reason)

    def test_management_key_is_used_for_credits_only(self) -> None:
        seen = {}

        def fake_fetch(url, key_value=None, *a, **kw):
            seen[url] = key_value
            return {}, json.dumps(self.CREDITS if "credits" in url
                                  else self._key()).encode(), None
        with mock.patch.object(simple, "api_key",
                               return_value=("placeholder", "test")), \
                mock.patch.object(simple, "fetch", side_effect=fake_fetch), \
                mock.patch.dict(os.environ, {"OR_MGMT": "mgmt-placeholder"}):
            got = simple.openrouter({"management_key_env": "OR_MGMT"},
                                    auth_path=NO_AUTH)
        self.assertEqual(seen[simple.OPENROUTER_CREDITS_URL], "mgmt-placeholder")
        self.assertEqual(seen[simple.OPENROUTER_KEY_URL], "placeholder")
        self.assertEqual(got.remaining, 4.25)

    def test_cli_line_shows_money_windows(self) -> None:
        from tarmac.cli import _format_source
        line = _format_source({
            "provider": "openrouter", "slug": "openrouter", "status": "ok",
            "latest": {"remaining": 37.5, "unit": "USD", "windows": [
                {"label": "account credits", "remaining_percent": 37.5,
                 "unit": "USD"},
                {"label": "key cap", "remaining_percent": None,
                 "unit": "USD", "stale_reason": "no cap"}]}})
        self.assertIn("account credits 37.5 USD", line)
        self.assertIn("key cap no cap", line)


class XaiAdapterTests(unittest.TestCase):
    """The three calls wired together, every failure landing on
    unknown-with-reason rather than an exception."""

    def _replies(self, *calls):
        return mock.patch.object(xai_mod, "fetch", side_effect=list(calls))

    OK_USER = ({}, b'{"userId":"u-1"}', None)

    def test_the_happy_path_is_a_window_plus_a_credits_phrase(self) -> None:
        with mock.patch.object(xai_mod, "bearer",
                               return_value=("token", None)), self._replies(
                self.OK_USER,
                ({}, json.dumps(_billing(used=25.0)).encode(), None),
                ({}, _resets_body(_credit("restok", int(time.time()) + 86400)),
                 None)):
            got = xai_mod.xai(auth_path=NO_AUTH)
        self.assertEqual(got.remaining, 75.0)
        self.assertEqual(model.credits_text(got.raw).split(" ·")[0],
                         "1 banked reset credit")
        self.assertNotIn("restok", json.dumps(got.as_dict()))
        self.assertNotIn("token", json.dumps(got.as_dict()))

    def test_a_dead_reset_rpc_still_leaves_the_window(self) -> None:
        with mock.patch.object(xai_mod, "bearer",
                               return_value=("token", None)), self._replies(
                self.OK_USER,
                ({}, json.dumps(_billing(used=25.0)).encode(), None),
                ({}, b"", "http 503 Service Unavailable")):
            got = xai_mod.xai(auth_path=NO_AUTH)
        self.assertEqual(got.remaining, 75.0)
        self.assertIsNone(model.credits_text(got.raw))

    def test_every_call_failing_is_unknown_with_a_reason(self) -> None:
        cases = {
            "user request": ("http 401 Unauthorized",
                             [({}, b"", "http 401 Unauthorized")]),
            "unrecognized user": ("did not recognise",
                                  [({}, b'{"error":"nope"}', None)]),
            "billing response": ("not JSON",
                                 [self.OK_USER, ({}, b"<html>", None)]),
        }
        for label, (expected, calls) in cases.items():
            with self.subTest(label), \
                    mock.patch.object(xai_mod, "bearer",
                                      return_value=("token", None)), \
                    self._replies(*calls):
                got = xai_mod.xai(auth_path=NO_AUTH)
                self.assertFalse(got.known)
                self.assertIn(expected, got.reason)

    def test_an_unusable_credential_never_reaches_the_network(self) -> None:
        with mock.patch.object(xai_mod, "bearer",
                               return_value=(None, "no xai login")), \
                mock.patch.object(xai_mod, "fetch", side_effect=AssertionError):
            got = xai_mod.xai(auth_path=NO_AUTH)
        self.assertEqual(got.reason, "no xai login")


class ClaudeTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        # Removing the cache-freshness shortcut means claude() always considers
        # a screen read. These cases are about the cache FILE, so refuse the
        # screen rather than spawn Claude Code on the developer's machine.
        self._no_screen = mock.patch.object(claude_mod, "read_screen",
                                            return_value={})
        self._no_screen.start()
        self.addCleanup(self._no_screen.stop)
        claude_mod._SCREEN_LAST = {}
        self.addCleanup(setattr, claude_mod, "_SCREEN_LAST", {})

        self.path = Path(self.tmp.name) / ".claude.json"

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def _write(self, data) -> Path:
        self.path.write_text(json.dumps(data))
        return self.path

    def _claude(self, path, **kw):
        return claude_mod.claude(path=path, auth=AUTH_OK, **kw)

    def test_both_windows_are_reported_under_one_provider(self) -> None:
        """The 5-hour and weekly limits are two windows of one
        Claude plan, and neither carries a limit field."""
        got = self._claude(self._write(claude_fixture(five_hour_used=12,
                                                      seven_day_used=40)))
        self.assertEqual(got.provider, "claude")
        self.assertEqual([(w["label"], w["remaining"]) for w in got.windows],
                         [("5h", 88.0), ("weekly", 60.0)])
        for w in got.windows:
            self.assertNotIn("limit", w)
            self.assertEqual(w["unit"], "percent")
        # the scalar fields stay the tightest live window, for dispatch
        self.assertEqual((got.remaining, got.unit), (60.0, "percent"))
        self.assertEqual(got.kind, "plan")
        self.assertFalse(got.stale)
        self.assertTrue(got.as_of.endswith("Z"))

    def test_an_expired_window_is_flagged_not_dropped(self) -> None:
        data = claude_fixture()
        data["cachedUsageUtilization"]["utilization"]["five_hour"]["resets_at"] = \
            _iso_in(-1)
        got = self._claude(self._write(data))
        five, week = got.windows
        self.assertTrue(five["stale"])
        # The machine-readable flag stays; the caption is gone — the daemon
        # keeps readings current, so an old number is a fault, not a caption.
        self.assertIsNone(five["stale_reason"])
        self.assertFalse(week["stale"])
        self.assertEqual(got.remaining, 60.0)  # the tightest LIVE window

    def test_an_old_cache_is_reported_without_narrating_its_age(self) -> None:
        """The age of a reading stops being a story on the card. Only
        a window that HAS ALREADY RESET changes what its number means, and
        this fixture's windows both still stand."""
        got = self._claude(self._write(claude_fixture(age_min=60 * 27)))
        self.assertTrue(got.known)
        self.assertFalse(got.stale)
        self.assertEqual(len(got.windows), 2)
        self.assertIsNone(got.windows[1]["stale_reason"])
        # the age itself survives on the record for machine policy
        self.assertEqual(got.raw["as_of_age_h"], 27.0)
        self.assertEqual(round(model.age_hours(got.as_of)), 27)

    def test_shape_drift_and_bad_files_are_unknown(self) -> None:
        cases = {
            "missing cache": {"other": 1},
            "missing timestamp": {"cachedUsageUtilization": {"utilization": {}}},
            "no usable windows": {"cachedUsageUtilization": {
                "fetchedAtMs": time.time() * 1000,
                "utilization": {"five_hour": None, "seven_day": None}}},
        }
        for label, data in cases.items():
            with self.subTest(label):
                got = self._claude(self._write(data))
                self.assertFalse(got.known)
                self.assertTrue(got.reason)
        self.path.write_text("{not json")
        self.assertFalse(self._claude(self.path).known)
        self.assertFalse(self._claude(self.path.parent / "absent.json").known)


def _no_app_server():
    """Stand in for a machine with no codex-cli, so a test that is about the
    session files does not reach the real app server."""
    raise RuntimeError("codex is not installed")


class CodexTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self.tmp.name) / "2026" / "08" / "13"
        self.dir.mkdir(parents=True)

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def _rollout(self, name: str, lines) -> Path:
        p = self.dir / name
        p.write_text("\n".join(lines) + "\n")
        return p

    def test_last_token_count_event_wins(self) -> None:
        got = codex_mod.parse_codex(codex_lines(used=25.0))
        self.assertEqual((got.provider, got.remaining, got.unit),
                         ("codex", 75.0, "percent"))
        # window_minutes 10080 reads "weekly" rather than going unnamed
        self.assertEqual([w["label"] for w in got.windows], ["weekly"])
        self.assertEqual(got.kind, "plan")
        self.assertEqual(got.raw["plan_type"], "prolite")
        self.assertEqual(got.as_of, "2026-08-13T10:05:00Z")

    def test_workspace_credits_are_not_banked_resets(self) -> None:
        """The session balance cannot establish an earned reset count."""
        got = codex_mod.parse_codex(codex_lines())
        self.assertIsNone(model.credits_text(got.raw))
        self.assertEqual(got.kind, "plan")  # still never shows a price

    def test_a_second_window_renders_when_the_account_has_one(self) -> None:
        got = codex_mod.parse_codex(codex_lines(
            secondary={"used_percent": 10.0, "window_minutes": 300,
                       "resets_at": _epoch_in(1)}))
        self.assertEqual([(w["label"], w["remaining"]) for w in got.windows],
                         [("weekly", 75.0), ("5h", 90.0)])

    def test_a_reset_window_is_flagged_not_withheld(self) -> None:
        got = codex_mod.parse_codex(codex_lines(resets_in_h=-5))
        self.assertTrue(got.known)
        self.assertEqual(got.remaining, 75.0)
        self.assertTrue(got.stale)
        self.assertIsNone(got.windows[0]["stale_reason"])

    def test_newest_file_wins_and_empty_sessions_are_unknown(self) -> None:
        old = self._rollout("rollout-2026-08-13T09-00-00-aaa.jsonl",
                            codex_lines(used=90.0))
        new = self._rollout("rollout-2026-08-13T10-00-00-bbb.jsonl",
                            codex_lines(used=25.0))
        os.utime(old, (1_780_000_000, 1_780_000_000))
        os.utime(new, (1_790_000_000, 1_790_000_000))
        # A reader that refuses forces the session-file path, which is what
        # these cases are about. Without it the test reaches the real codex
        # binary on the developer's machine and waits out its timeout.
        self.assertEqual(
            codex_mod.codex(auth=AUTH_OK, sessions_dir=self.dir.parents[2],
                            reader=_no_app_server).remaining, 75.0)
        got = codex_mod.codex(auth=AUTH_OK, sessions_dir=Path(self.tmp.name) / "nothing-here",
                              reader=_no_app_server)
        self.assertFalse(got.known)
        self.assertIn("no usable session snapshot", got.reason)

    def test_falls_back_past_a_session_with_no_snapshot_yet(self) -> None:
        started = self._rollout("rollout-2026-08-13T11-00-00-ccc.jsonl",
                                ['{"timestamp":"x","payload":{"type":"user_message"}}'])
        older = self._rollout("rollout-2026-08-13T10-00-00-ddd.jsonl",
                              codex_lines(used=25.0))
        os.utime(older, (1_780_000_000, 1_780_000_000))
        os.utime(started, (1_790_000_000, 1_790_000_000))
        self.assertEqual(
            codex_mod.codex(auth=AUTH_OK, sessions_dir=self.dir.parents[2],
                            reader=_no_app_server).remaining, 75.0)

    def test_corrupt_lines_and_drifted_shapes_are_unknown(self) -> None:
        cases = {
            "empty": [],
            "truncated json": ['{"payload":{"type":"token_count", "rate_l'],
            "no usable window": ['{"payload":{"type":"token_count",'
                                 '"rate_limits":{"primary":null}}}'],
            "invalid reset": ['{"payload":{"type":"token_count","rate_limits":'
                              '{"primary":{"used_percent":10,'
                              '"resets_at":"soon"}}}}'],
        }
        for label, lines in cases.items():
            with self.subTest(label):
                got = codex_mod.parse_codex(lines)
                self.assertFalse(got.known)
                self.assertTrue(got.reason)


class FormattingTests(unittest.TestCase):
    def test_window_minutes_are_named(self) -> None:
        cases = {
            "weekly": (10080, "weekly"),
            "five hours": (300, "5h"),
            "arbitrary minutes": (42, "42m"),
            "missing duration": (None, "window"),
            "whole hours": (240, "4h"),
            "whole days": (4320, "3d"),
            "zero": (0, "0m"),
        }
        for label, (minutes, expected) in cases.items():
            with self.subTest(label):
                self.assertEqual(model.window_label(minutes), expected)

    def test_time_to_reset_reads_in_days_hours_and_minutes(self) -> None:
        """"in 55h" is arithmetic the reader should not have to do."""
        cases = {
            "hours": (_iso_in(2), "in 1h 5"),
            "days": (_iso_in(80), "in 3d 7h"),
            "minutes": (_iso_in(0.5), "in 29m"),
            "missing": (None, "-"),
            "past": (_iso_in(-1), "now"),
        }
        for label, (resets_at, expected) in cases.items():
            with self.subTest(label):
                self.assertTrue(model.until_text(resets_at).startswith(expected))


class ExpiredWindowTests(unittest.TestCase):
    """Expired windows must lose their public quota value."""

    def _window(self, resets_at):
        return model.make_window("5h", 88.0, resets_at)

    def test_an_expired_window_reports_unknown_not_the_old_number(self) -> None:
        past = "2020-01-01T00:00:00Z"
        current = model.as_of_now(self._window(past))
        self.assertIsNone(current["remaining"])
        self.assertTrue(current["stale"])
        self.assertIn("reset", current["stale_reason"])
        self.assertEqual("5h", current["label"])

    def test_current_windows_keep_their_reading(self) -> None:
        for label, resets_at in {
                "future reset": "2999-01-01T00:00:00Z",
                "unknown reset": None}.items():
            with self.subTest(label):
                window = self._window(resets_at)
                self.assertEqual(window, model.as_of_now(window))
                self.assertEqual(88.0, model.as_of_now(window)["remaining"])


class CodexLiveTests(unittest.TestCase):
    """App-server parser and session-file fallback using synthetic data."""

    LIVE = {"rateLimits": {"limitId": "codex", "planType": "prolite",
                           "primary": {"usedPercent": 75,
                                       "windowDurationMins": 10080,
                                       "resetsAt": 4070908800},
                           "secondary": None,
                           "credits": {"hasCredits": False, "balance": "0"}}}

    def test_used_percent_becomes_remaining(self) -> None:
        r = codex_mod.parse_codex_live(self.LIVE)
        self.assertTrue(r.known)
        self.assertEqual(25.0, r.remaining)
        self.assertEqual(["weekly"], [w["label"] for w in r.windows])
        self.assertIsNone(model.credits_text(r.raw))

    # The shape the app server actually returns, trimmed: the granted
    # resets live at the TOP level, beside rateLimits, not inside it.
    WITH_RESET = {
        **LIVE,
        "rateLimitResetCredits": {"credits": [
            {"id": "synthetic-reset-available", "resetType": "codexRateLimits",
             "status": "available", "grantedAt": 4068230400,
             "expiresAt": 4070908800, "title": "Full reset"},
            {"id": "RateLimitResetCredit_old", "resetType": "codexRateLimits",
             "status": "redeemed", "grantedAt": 946684800,
             "expiresAt": 946771200, "title": "Full reset"}]}}

    def test_a_banked_reset_is_read_from_the_reset_list(self) -> None:
        """Reset credits come from the reset list, separately from balance."""
        r = codex_mod.parse_codex_live(self.WITH_RESET)
        self.assertEqual("1 banked reset · soonest expires 2099-01-01",
                         model.credits_text(r.raw))
        self.assertEqual(r.raw["credits"], {
            "text": "1 banked reset · soonest expires 2099-01-01",
            "count": 1, "expires_at": "2099-01-01T00:00:00Z",
        })

    def test_no_reset_list_means_unknown_reset_count(self) -> None:
        """A session-file recording carries the old block and nothing else."""
        self.assertIsNone(model.credits_text(codex_mod.parse_codex_live(self.LIVE).raw))

    def test_an_empty_reset_list_says_none_banked(self) -> None:
        payload = {**self.LIVE, "rateLimitResetCredits": {"credits": []}}
        self.assertEqual(
            "0 banked resets",
            model.credits_text(codex_mod.parse_codex_live(payload).raw))

    def test_a_dead_app_server_falls_back_to_the_session_record(self) -> None:
        def boom():
            raise RuntimeError("codex is not installed")
        with tempfile.TemporaryDirectory() as tmp:
            d = pathlib.Path(tmp) / "2026" / "08"
            d.mkdir(parents=True)
            (d / "rollout-x.jsonl").write_text(json.dumps({
                "timestamp": "2026-08-15T00:00:00Z",
                "payload": {"type": "token_count", "rate_limits": {
                    "primary": {"used_percent": 20, "window_minutes": 10080,
                                "resets_in_seconds": 3600}}}}) + "\n")
            r = codex_mod.codex(auth=AUTH_OK, sessions_dir=tmp, reader=boom)
        self.assertTrue(r.known, r.reason)
        self.assertEqual(80.0, r.remaining)

    def test_no_app_server_and_no_record_says_both(self) -> None:
        def boom():
            raise RuntimeError("codex is not installed")
        with tempfile.TemporaryDirectory() as tmp:
            r = codex_mod.codex(auth=AUTH_OK, sessions_dir=tmp, reader=boom)
        self.assertFalse(r.known)
        self.assertIn("codex is not installed", r.reason)
        self.assertIn("no usable session snapshot", r.reason)


SCREEN = """\
 Current session
   4% used · resets in 3h
 Current week (all models)
   52% used · resets Fri
 Account
   99% something irrelevant
"""


class ClaudeLiveTests(unittest.TestCase):
    """Synthetic stale-cache and screen measurements with injected readers."""

    STATE = {"cachedUsageUtilization": {
        "fetchedAtMs": 4_000_000_000_000,
        "utilization": {
            "five_hour": {"utilization": 14,
                          "resets_at": "2020-01-01T00:00:00Z"},
            "seven_day": {"utilization": 18,
                          "resets_at": "2999-01-01T00:00:00Z"}}}}

    def setUp(self) -> None:
        claude_mod._SCREEN_LAST = {}
        self.addCleanup(setattr, claude_mod, "_SCREEN_LAST", {})

    def _file(self, tmp, state=None):
        path = pathlib.Path(tmp) / "claude.json"
        path.write_text(json.dumps(state if state is not None else self.STATE))
        return path

    def _claude(self, path, **kw):
        return claude_mod.claude(path=path, auth=AUTH_OK, **kw)

    def test_the_screen_beats_a_stale_cache(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            r = self._claude(self._file(tmp),
                             now_ms=4_000_000_000_000 + 72 * 3600000,
                             screen=lambda state:
                             claude_mod.parse_screen(SCREEN))
        by = {w["label"]: w for w in r.windows}
        self.assertEqual(48.0, by["weekly"]["remaining"])
        self.assertEqual(96.0, by["5h"]["remaining"])
        # The screen gives percentages, not reset times. Pairing a live number
        # with a reset that has passed would make as_of_now blank a reading
        # just taken.
        self.assertIsNone(by["5h"]["resets_at"])
        self.assertEqual(96.0, model.as_of_now(by["5h"])["remaining"])
        self.assertEqual("2999-01-01T00:00:00Z", by["weekly"]["resets_at"])

    def test_a_fresh_cache_does_not_skip_the_screen(self) -> None:
        # It used to. The per-model rows live only on the screen and are never
        # written to the cache, and reading /usage refreshes the cache as a
        # side effect -- so a fresh cache hid the very thing that refreshed it,
        # and Fable never appeared again.
        called = []
        with tempfile.TemporaryDirectory() as tmp:
            r = self._claude(self._file(tmp),
                             now_ms=4_000_000_000_000 + 60_000,
                             screen=lambda state: called.append(1) or
                             {"five_hour": 5.0, "seven_day": 61.0,
                              "week:fable": 53.0})
        self.assertEqual([1], called)
        by = {w["label"]: w for w in r.windows}
        self.assertEqual(39.0, by["weekly"]["remaining"])
        self.assertIn("weekly · fable", by)

    def test_a_screen_that_says_nothing_keeps_the_cache(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            r = self._claude(self._file(tmp),
                             now_ms=4_000_000_000_000 + 72 * 3600000,
                             screen=lambda state: {})
        self.assertTrue(r.known)
        self.assertEqual(
            82.0, {w["label"]: w for w in r.windows}["weekly"]["remaining"])

    def test_the_screen_read_is_throttled_between_polls(self) -> None:
        # Repeated polls share a screen read to limit CLI traffic.
        calls = []

        def once(state):
            calls.append(1)
            return {"five_hour": 5.0, "seven_day": 61.0}

        with tempfile.TemporaryDirectory() as tmp:
            path = self._file(tmp)
            stale = 4_000_000_000_000 + 72 * 3600000
            with mock.patch.object(claude_mod, "read_screen", once):
                first = self._claude(path, now_ms=stale)
                second = self._claude(path, now_ms=stale)
        self.assertEqual(1, len(calls), "the second poll reused the first answer")
        self.assertEqual(first.remaining, second.remaining)
        self.assertEqual(first.as_of, second.as_of)

    def test_a_partial_read_is_retried_sooner_than_a_complete_one(self) -> None:
        # A read missing the per-model rows was rate limited, not finished.
        # Caching it for the full interval locked Fable out for twenty minutes.
        answers = [{"five_hour": 5.0, "seven_day": 61.0},
                   {"five_hour": 5.0, "seven_day": 61.0, "week:fable": 53.0}]
        calls = []

        def reader(state):
            calls.append(1)
            return answers[min(len(calls) - 1, len(answers) - 1)]

        with tempfile.TemporaryDirectory() as tmp:
            path = self._file(tmp)
            stale = 4_000_000_000_000 + 72 * 3600000
            with mock.patch.object(claude_mod, "read_screen", reader):
                self._claude(path, now_ms=stale)              # partial
                self.assertEqual(1, len(calls))
                # A partial answer is held only briefly, so the next poll asks.
                claude_mod._SCREEN_LAST[""] = (
                    time.monotonic() - claude_mod.CLAUDE_PARTIAL_EVERY_S - 1,
                    *claude_mod._SCREEN_LAST[""][1:])
                second = self._claude(path, now_ms=stale)
                self.assertEqual(2, len(calls))
                # Now it is complete, so the long interval applies.
                claude_mod._SCREEN_LAST[""] = (
                    time.monotonic() - claude_mod.CLAUDE_PARTIAL_EVERY_S - 1,
                    *claude_mod._SCREEN_LAST[""][1:])
                self._claude(path, now_ms=stale)
                self.assertEqual(2, len(calls),
                                 "a complete read is not re-asked")
        self.assertIn("weekly · fable", [w["label"] for w in second.windows])

    def test_a_per_model_week_is_reported_but_never_the_scalar(self) -> None:
        # Anthropic meters some models separately. Fable's weekly running out
        # says nothing about Opus, so it must not become "how much Claude is
        # left" -- which is the number dispatch reads.
        screen = ("Current session\n 5% used\n"
                  "Current week (all models)\n 61% used\n"
                  "Current week (Fable only)\n 23% used\n")
        with tempfile.TemporaryDirectory() as tmp:
            r = self._claude(self._file(tmp),
                             now_ms=4_000_000_000_000 + 72 * 3600000,
                             screen=lambda state:
                             claude_mod.parse_screen(screen))
        by = {w["label"]: w for w in r.windows}
        self.assertEqual(77.0, by["weekly · fable"]["remaining"])
        self.assertTrue(by["weekly · fable"]["per_model"])
        self.assertEqual(39.0, r.remaining)  # the account week, not Fable's

    def test_screen_parser_recognizes_quota_rows(self) -> None:
        cases = {
            "account rows only": (
                SCREEN, {"five_hour": 4.0, "seven_day": 52.0}),
            "new per-model row": (
                "Current week (Something New only)\n 10% used\n",
                {"week:something new": 10.0}),
            "all models is account-wide": (
                "Current week (all models)\n 61% used\n", {"seven_day": 61.0}),
            "terminal escapes": (
                "\x1b[1m Current session\x1b[0m\r\n"
                "\x1b[32m   2% used\x1b[0m\r\n", {"five_hour": 2.0}),
        }
        for label, (screen, expected) in cases.items():
            with self.subTest(label):
                self.assertEqual(expected, claude_mod.parse_screen(screen))


AGY_SCREEN = """
└ Models & Quota

  Account: someone@example.com

GEMINI MODELS
  Models within this group: Gemini Flash, Gemini Pro

  Weekly Limit Remaining

    [████░] 75.25%

    75% remaining · Refreshes in 144h 15m

  Five Hour Limit Remaining
    [████░] 65.75%
    66% remaining · Refreshes in 3h 15m


CLAUDE AND GPT MODELS
  Models within this group: Claude Opus, Claude Sonnet, GPT-OSS

  Weekly Limit Remaining
    [████░] 30.5%
    31% remaining · Refreshes in 5d 12h 15m

  Five Hour Limit Remaining
    [████░] 55.25%
    55% remaining · Refreshes in 2h 15m
"""


# Synthetic spent group; the disabled notice is deliberately truncated.
AGY_SPENT_SCREEN = """
GEMINI MODELS
Models within this group: Gemini Flash, Gemini Pro
Weekly Limit Remaining
[████░] 70.25%
Refreshes in 72h 15m
Five Hour Limit Remaining
[████░] 60.75%
Refreshes in 1h 15m
CLAUDE AND GPT MODELS
Models within this group: Claude Opus, Claude Sonnet, GPT-OSS
Weekly Limit Remaining
[░░░░░] 0.00%
Refreshes in 120h 15m
Five Hour Limit Remaining
Disabled: You have hit your weekly limit, the 5-hour limit does not currently apply. Your weekly limit will fully re
│Within each group, models share a weekly limit and a 5-hour limit. Quota is
"""


class AntigravityTests(unittest.TestCase):
    """The plan meters model GROUPS: Gemini one pair of windows, Claude and
    GPT-OSS another. Both pairs are reported; the tightest is the scalar."""

    def test_screen_parse_gives_one_window_per_group_and_length(self) -> None:
        base = time.time()
        windows = agy_mod.parse_screen(AGY_SCREEN, now_s=base)
        by = {w["label"]: w for w in windows}
        self.assertEqual(
            {"weekly · gemini", "5h · gemini",
             "weekly · claude+gpt", "5h · claude+gpt"}, set(by))
        self.assertEqual(75.25, by["weekly · gemini"]["remaining"])
        self.assertEqual(30.5, by["weekly · claude+gpt"]["remaining"])
        # Synthetic countdown relative to the injected clock.
        self.assertEqual(
            model.iso(base + 5 * 86400 + 12 * 3600 + 15 * 60),
            by["weekly · claude+gpt"]["resets_at"])
        self.assertFalse(any(w["stale"] for w in windows))

    def test_the_tightest_group_window_is_the_scalar(self) -> None:
        reading = agy_mod.antigravity(
            screen=lambda: agy_mod.parse_screen(AGY_SCREEN))
        self.assertEqual(30.5, reading.remaining)
        self.assertEqual("percent", reading.unit)
        self.assertEqual("plan", reading.kind)  # a subscription, never money
        self.assertEqual(4, len(reading.windows))
        self.assertIsNone(reading.reason)

    def test_a_half_painted_frame_is_skipped_not_guessed(self) -> None:
        # The TUI repaints, so a frame can carry the bar without its
        # countdown. A window with no reset would look permanent.
        partial = "GEMINI MODELS\n  Weekly Limit Remaining\n    [█] 75.25%\n"
        self.assertEqual([], agy_mod.parse_screen(partial))

    def test_a_group_missing_one_bar_is_dropped_not_reported(self) -> None:
        # A frame caught half-painted loses a bar, and a card with three
        # bars reads as a plan with three windows.
        windows = agy_mod.parse_screen(AGY_SCREEN)
        half = [w for w in windows if w["label"] != "5h · claude+gpt"]
        self.assertEqual(["weekly · gemini", "5h · gemini"],
                         [w["label"] for w in agy_mod.paired(half)])
        self.assertEqual(4, len(agy_mod.paired(windows)))

    def test_an_exhausted_window_states_no_percent_prose(self) -> None:
        # At zero the screen drops the "0% remaining ·" half and prints the
        # countdown alone. Zero is a reading, and the one that matters.
        screen = ("GEMINI MODELS\n\n  Five Hour Limit Remaining\n\n"
                  "    [░░░] 0.00%\n\n    Refreshes in 1h 34m\n")
        window, = agy_mod.parse_screen(screen)
        self.assertEqual(0.0, window["remaining"])

    def test_a_full_window_states_no_reset_and_is_still_a_window(self) -> None:
        # An untouched window prints "Quota available" and no countdown --
        # there is nothing to refresh. Requiring a countdown dropped it, and
        # the card lost its bar.
        screen = ("GEMINI MODELS\n\n  Weekly Limit Remaining\n\n"
                  "    [███] 100.00%\n\n    Quota available\n\n"
                  "  Five Hour Limit Remaining\n\n    [███] 100.00%\n\n"
                  "    Quota available\n")
        windows = agy_mod.parse_screen(screen)
        self.assertEqual(2, len(windows))
        self.assertTrue(all(w["remaining"] == 100.0 for w in windows))
        self.assertTrue(all(w["resets_at"] is None for w in windows))
        self.assertFalse(any(w["stale"] for w in windows))

    def test_a_spent_weekly_group_is_reported_without_its_five_hour_bar(self) -> None:
        # With the weekly limit spent, the five-hour row prints why it no
        # longer applies instead of a bar. Pairing dropped the whole group,
        # and Claude and GPT vanished from the card at exactly zero.
        base = time.time()
        windows = agy_mod.paired(agy_mod.parse_screen(AGY_SPENT_SCREEN, now_s=base))
        by = {w["label"]: w for w in windows}
        self.assertEqual({"weekly · gemini", "5h · gemini", "weekly · claude+gpt"}, set(by))
        self.assertEqual(0.0, by["weekly · claude+gpt"]["remaining"])
        self.assertEqual(model.iso(base + 120 * 3600 + 15 * 60),
                         by["weekly · claude+gpt"]["resets_at"])
        reading = agy_mod.antigravity(screen=lambda: windows)
        self.assertEqual(0.0, reading.remaining)
        self.assertIsNone(reading.reason)

    def test_a_disabled_five_hour_row_without_its_weekly_bar_is_still_dropped(self) -> None:
        # Half-painted: the disabled notice landed but the weekly bar did not.
        partial = AGY_SPENT_SCREEN.replace("[░░░░░] 0.00%", "")
        labels = [w["label"] for w in agy_mod.paired(agy_mod.parse_screen(partial))]
        self.assertEqual(["weekly · gemini", "5h · gemini"], labels)

    def test_no_screen_is_unknown_with_a_reason_not_a_raise(self) -> None:
        reading = agy_mod.antigravity(screen=lambda: [])
        self.assertIsNone(reading.remaining)
        self.assertIn("agy", reading.reason)

    def test_a_broken_screen_read_never_raises(self) -> None:
        def boom():
            raise RuntimeError("pty is gone")
        self.assertIn("RuntimeError",
                      agy_mod.antigravity(screen=boom).reason)



if __name__ == "__main__":
    unittest.main()
