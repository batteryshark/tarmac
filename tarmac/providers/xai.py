"""Grok / xAI: the SuperGrok subscription window plus banked usage resets.

xAI's documented api (api.x.ai) publishes no quota route. The SuperGrok
SUBSCRIPTION does, on the hosts the Grok CLI talks to, and that subscription
is what OpenCode logs into. Three calls: the user id, the billing window
keyed by it, and a gRPC-Web RPC for banked usage resets.

The grant OpenCode stores lives about two hours, so a strictly read-only
adapter is blind by mid-afternoon. Tarmac renews it — but it is writing into
ANOTHER application's credential store, so the write is the careful part:
temp file in the same directory, mode 0600, os.replace, every other
provider's entry carried across untouched, and nothing written at all if any
step fails. Renewal happens only in the last two minutes of the grant's
life, so a five-minute poll touches the file about once every two hours.
"""
import json
import os
import tempfile
import time
import urllib.parse
from datetime import datetime, timezone
from pathlib import Path

from tarmac.model import (Reading, credit_summary, from_windows, iso,
                          make_window, scrub, seconds_until, soft, unknown,
                          window_label)
from tarmac.providers.creds import (OPENCODE_AUTH, api_key, fchmod, fetch,
                                    opencode_entries, replace_entry)

XAI_USER_URL = "https://cli-chat-proxy.grok.com/v1/user"
XAI_BILLING_URL = "https://cli-chat-proxy.grok.com/v1/billing?format=credits"
XAI_RESETS_URL = "https://grok.com/prod_mc_billing.ConsumerUiSvc/GetRemainingResets"

# Renewing OpenCode's OAuth grant. The client id is OpenCode's own, public by
# construction in a PKCE flow and not a secret.
XAI_TOKEN_URL = "https://auth.x.ai/oauth2/token"
XAI_CLIENT_ID = "b1a00492-073a-47ea-816f-4c329264a828"
XAI_REFRESH_SKEW = 120  # seconds before expiry; the grant lives about two hours


def _write_auth(path: Path, data: dict) -> str | None:
    """Replace OpenCode's auth.json atomically, or change nothing. Returns an
    error string, never raises. The file is never truncated in place, so a
    crash mid-write leaves the previous credentials whole."""
    tmp = None
    try:
        fd, tmp = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
        fchmod(fd, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            json.dump(data, fh, indent=2)
            fh.write("\n")
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp, path)  # atomic within the directory; keeps mode 0600
        tmp = None
        return None
    except (OSError, TypeError, ValueError) as exc:
        return f"the renewed grant could not be saved to {path}: {exc}"
    finally:
        if tmp:
            try:
                os.unlink(tmp)
            except OSError:
                pass


def _renew(path: Path, entry: dict) -> tuple[str | None, str | None]:
    """(bearer, error). Trade the refresh token for a new pair and hand it
    back to OpenCode.

    xAI CONSUMES the refresh token it is given, so the pair on disk is
    re-read after the exchange: if another process rotated it while this
    request was in flight, that one is the live grant and this one writes
    nothing rather than overwriting it with a pair nobody else knows about.
    """
    refresh = entry.get("refresh")
    if not isinstance(refresh, str) or not refresh:
        return None, "the OpenCode xai grant expired and has no refresh token"
    _, body, err = fetch(XAI_TOKEN_URL, headers={
        "Content-Type": "application/x-www-form-urlencoded"},
        data=urllib.parse.urlencode({
            "grant_type": "refresh_token", "refresh_token": refresh,
            "client_id": XAI_CLIENT_ID}).encode("ascii"))
    if err:
        return None, f"renewing the xai grant failed ({err}); reconnect xAI in OpenCode"
    try:
        tokens = json.loads(body or b"{}")
        access, seconds = tokens["access_token"], tokens.get("expires_in")
    except (ValueError, KeyError, TypeError):
        return None, "renewing the xai grant returned no access token"
    if not isinstance(access, str) or not access:
        return None, "renewing the xai grant returned no access token"
    if not isinstance(seconds, (int, float)) or isinstance(seconds, bool) \
            or seconds <= 0:
        seconds = 3600

    renewed = {**entry, "access": access,
               "refresh": tokens.get("refresh_token") or refresh,
               "expires": int((time.time() + seconds) * 1000)}
    current, err = replace_entry("xai", refresh, renewed, path, _write_auth)
    if err:
        return None, err
    if current is not renewed:  # somebody else rotated it first
        live = (current or {}).get("access")
        return (live, None) if isinstance(live, str) and live else \
            (None, "another process rotated the xai grant mid-request")
    return access, None


def bearer(config: dict | None = None, auth_path: Path | str = OPENCODE_AUTH,
           now: float | None = None) -> tuple[str | None, str | None]:
    """(bearer, error) for the Grok subscription hosts.

    ponytail: expiry is taken from OpenCode's own ``expires`` rather than
    decoded out of the JWT. A wrong one only means renewing late, which reads
    as an http 401 and an honest unknown."""
    path = Path(auth_path).expanduser()
    entry = opencode_entries(path)[0].get("xai")
    access = entry.get("access") if isinstance(entry, dict) else None
    if not isinstance(entry, dict) or not isinstance(access, str) or not access:
        key, source = api_key("xai", config, auth_path)  # the env fallback
        return (key, None) if key else (None, source)
    expires = entry.get("expires")
    now = time.time() if now is None else now
    if isinstance(expires, (int, float)) and not isinstance(expires, bool) \
            and expires / 1000 > now + XAI_REFRESH_SKEW:
        return access, None
    return _renew(path, entry)


def _varint(data: bytes, i: int) -> tuple[int, int]:
    """Protobuf base-128: seven bits a byte, low group first, high bit set on
    every byte but the last."""
    value = shift = 0
    while i < len(data):
        byte = data[i]
        i, value, shift = i + 1, value | (byte & 0x7F) << shift, shift + 7
        if not byte & 0x80:
            return value, i
        if shift > 63:
            raise ValueError("oversized protobuf varint")
    raise ValueError("truncated protobuf varint")


def _proto_fields(data: bytes):
    """(field number, wire type, value) for one protobuf message. No schema
    and no protobuf library — the wire format is self-describing enough to
    walk. Unknown fields are yielded and ignored by the caller."""
    i = 0
    while i < len(data):
        key, i = _varint(data, i)
        number, wire = key >> 3, key & 7
        if wire == 0:  # varint
            value, i = _varint(data, i)
        elif wire == 2:  # length-delimited: bytes, string, submessage
            length, i = _varint(data, i)
            value, i = data[i:i + length], i + length
            if len(value) != length:
                raise ValueError("truncated length-delimited protobuf field")
        elif wire in (1, 5):  # fixed64, fixed32
            width = 8 if wire == 1 else 4
            value, i = data[i:i + width], i + width
            if len(value) != width:
                raise ValueError("truncated fixed-width protobuf field")
        else:
            raise ValueError(f"unsupported protobuf wire type {wire}")
        yield number, wire, value


def _grpc_web_messages(body: bytes, headers: dict) -> list[bytes]:
    """Unwrap gRPC-Web frames: one flag byte, four big-endian length bytes,
    then the payload. Flag 0x80 marks the trailer frame carrying
    ``grpc-status`` — the real error channel, since a failed RPC still
    answers HTTP 200. Only the status number is read; the message may quote
    the request."""
    messages, status, i = [], headers.get("grpc-status"), 0
    while i + 5 <= len(body):
        flags, length = body[i], int.from_bytes(body[i + 1:i + 5], "big")
        i += 5
        payload, i = body[i:i + length], i + length
        if len(payload) != length:
            raise ValueError("truncated gRPC-Web frame")
        if flags & 0x80:
            for line in payload.decode("utf-8", "replace").splitlines():
                name, sep, value = line.partition(":")
                if sep and name.strip().lower() == "grpc-status":
                    status = value.strip()
        elif flags & 0x01:
            raise ValueError("compressed gRPC-Web frames are not supported")
        else:
            messages.append(payload)
    if i != len(body):
        raise ValueError("trailing bytes after the last gRPC-Web frame")
    if status not in (None, "", "0"):
        raise ValueError(f"the grok reset rpc failed with grpc status {status}")
    if not messages:  # zero credits is an EMPTY message, not a missing one
        raise ValueError("the grok reset rpc returned no protobuf message")
    return messages


def _proto_timestamp(data: bytes):
    """google.protobuf.Timestamp — field 1 is seconds. Nanoseconds are
    ignored: an expiry a month out does not need them."""
    seconds = next((v for n, w, v in _proto_fields(data) if n == 1 and w == 0),
                   None)
    try:
        return None if not seconds else datetime.fromtimestamp(seconds, timezone.utc)
    except (OverflowError, OSError, ValueError):
        return None


def parse_resets(body: bytes, headers: dict | None = None,
                 now: datetime | None = None) -> dict:
    """Banked usage resets: repeated field 10, each one a credit whose field
    30 is when it expires. A credit ALSO carries a redeemable id, which this
    deliberately never reads — a count and an expiry are the whole story a
    dashboard needs, and an id that is never parsed cannot leak.

    Returns ``{"available": n, "soonest_expiry": iso|None}``. Already-expired
    credits are not spendable and are not counted."""
    now = now or datetime.now(timezone.utc)
    expiries = []
    for message in _grpc_web_messages(body, headers or {}):
        for number, wire, value in _proto_fields(message):
            if number != 10 or wire != 2:
                continue
            when = next((_proto_timestamp(v) for n, w, v
                         in _proto_fields(bytes(value)) if n == 30 and w == 2),
                        None)
            if when and when > now:
                expiries.append(when)
    return {"available": len(expiries),
            "soonest_expiry": iso(min(expiries).timestamp()) if expiries else None}


def _reset_credits_text(resets: dict) -> str:
    count = resets.get("available") or 0
    text = f"{count} banked reset credit" + ("" if count == 1 else "s")
    return f"{text} · soonest expires {resets['soonest_expiry'][:10]}" \
        if resets.get("soonest_expiry") else text


@soft("xai")
def parse_xai(data: dict, resets: dict | None = None) -> Reading:
    """``config.currentPeriod`` is the subscription window — weekly on a
    SuperGrok plan — and ``creditUsagePercent`` is the percent USED of it.

    The body is proto3 JSON, which OMITS zero-valued scalars: no
    ``creditUsagePercent`` means nothing has been spent this period."""
    config = (data or {}).get("config")
    period = config.get("currentPeriod") if isinstance(config, dict) else None
    if not isinstance(period, dict):
        return unknown("xai", "no currentPeriod in the grok billing response")
    used = config.get("creditUsagePercent")
    used = float(used) if isinstance(used, (int, float)) \
        and not isinstance(used, bool) else 0.0
    starts, ends = iso(period.get("start")), iso(period.get("end"))
    minutes = None
    if starts and ends:
        minutes = round((seconds_until(ends) - seconds_until(starts)) / 60)
    window = make_window(window_label(minutes), max(0.0, 100.0 - used), ends)
    raw = {"period": period.get("type"), "starts_at": starts}
    if resets is not None:
        raw["credits"] = credit_summary(
            _reset_credits_text(resets), resets.get("available"),
            resets.get("soonest_expiry"))
    return from_windows("xai", [window], raw=scrub(raw))


def _resets(token: str) -> dict | None:
    """An EMPTY protobuf message in a five-byte gRPC-Web frame — no fields,
    so no bytes — asking what usage resets are banked. None when the rpc did
    not answer usably, which the card simply omits."""
    headers, body, err = fetch(XAI_RESETS_URL, token, {
        "Accept": "application/grpc-web+proto",
        "Content-Type": "application/grpc-web+proto",
        "Origin": "https://grok.com",
        "X-Grpc-Web": "1"}, data=b"\x00\x00\x00\x00\x00")
    if err:
        return None
    try:
        return parse_resets(body, headers)
    except (ValueError, TypeError, UnicodeDecodeError):
        return None  # shape drift in an extra never costs the window


@soft("xai")
def xai(config: dict | None = None, *, auth_path=OPENCODE_AUTH) -> Reading:
    """The SuperGrok subscription window, plus whatever usage resets are
    banked. The reset rpc is a nice-to-have — if it fails the window still
    reports, because a missing extra must not cost the number the card
    exists for."""
    token, err = bearer(config, auth_path)
    if err:
        return unknown("xai", err)
    user, err = fetch(XAI_USER_URL, token)[1:]
    if err:
        return unknown("xai", err)
    try:
        user_id = json.loads(user or b"{}")["userId"]
    except (ValueError, KeyError, TypeError):
        return unknown("xai", "grok did not recognise the credential")
    if not isinstance(user_id, str) or not user_id.strip():
        return unknown("xai", "grok did not recognise the credential")
    _, body, err = fetch(XAI_BILLING_URL, token, {"x-userid": user_id})
    if err:
        return unknown("xai", err)
    try:
        billing = json.loads(body or b"{}")
    except ValueError:
        return unknown("xai", "the grok billing response was not JSON")
    return parse_xai(billing, _resets(token))
