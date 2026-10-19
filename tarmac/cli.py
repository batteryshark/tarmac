"""``tarmac`` command line: run the daemon, manage sources, read status."""
import argparse
import json
import sys
import urllib.error
import urllib.request

from tarmac import service, store
from tarmac.api import DEFAULT_PORT, TarmacServer
from tarmac.model import until_text
from tarmac.poller import Poller


def _base_url(args) -> str:
    return f"http://127.0.0.1:{args.port}"


def _request(args, method: str, path: str, body: dict | None = None,
             *, authed: bool = False) -> dict:
    headers = {"Content-Type": "application/json"}
    if authed:
        headers["Authorization"] = f"Bearer {store.token()}"
    req = urllib.request.Request(
        _base_url(args) + path, method=method, headers=headers,
        data=json.dumps(body).encode() if body is not None else None)
    try:
        with urllib.request.urlopen(req, timeout=45) as resp:
            return json.loads(resp.read() or b"{}")
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", "replace")[:300]
        raise SystemExit(f"tarmac: http {exc.code}: {detail}") from None
    except (urllib.error.URLError, TimeoutError, OSError) as exc:
        raise SystemExit(
            f"tarmac: the service is not answering on {_base_url(args)} "
            f"({getattr(exc, 'reason', exc)}); start it with `tarmac serve` "
            "or `tarmac service install`") from None


def cmd_daemon(args) -> int:
    store.token()  # mint the bearer token on first start
    poller = Poller()
    poller.start()
    server = TarmacServer(poller, host=args.host, port=args.port)
    print(f"tarmac: serving on http://{args.host}:{args.port}")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        poller.stop()
        server.server_close()
    return 0


def _format_source(s: dict) -> str:
    left = f"{s['provider']:<11} {s['slug']:<24} {s['status']:<9}"
    latest = s.get("latest") or {}
    if s.get("needs_login"):
        return f"{left} NEEDS LOGIN — {latest.get('reason') or ''}"
    if latest.get("remaining") is not None:
        parts = [f"{latest['remaining']:g}{'%' if latest.get('unit') == 'percent' else ' ' + (latest.get('unit') or '')}"]
        if s.get("burn_rate") is not None:
            parts.append(f"burn {s['burn_rate']:g}%/h")
        if latest.get("resets_at"):
            parts.append(f"resets {until_text(latest['resets_at'])}")
        for w in latest.get("windows") or []:
            value, unit = w.get("remaining_percent"), w.get("unit") or "percent"
            if unit != "percent":  # money windows: a number or a stated fact
                text = f"{value:g} {unit}" if value is not None \
                    else w.get("stale_reason")
                if text:
                    parts.append(f"{w['label']} {text}")
            elif value is not None:
                parts.append(f"{w['label']} {value:g}%")
        return f"{left} {' · '.join(parts)}"
    return f"{left} {latest.get('reason') or 'no reading yet'}"


def cmd_status(args) -> int:
    data = _request(args, "GET", "/api/v1/snapshot" if args.passive else "/api/v1/status")
    if args.json:
        print(json.dumps(data, indent=2))
        return 0
    for s in data.get("sources") or []:
        print(_format_source(s))
    auth = data.get("claude_auth")
    if auth and auth.get("logged_in") is False:
        print("claude: NOT LOGGED IN — run `claude /login`")
    holds = data.get("holds") or {}
    for source_id, reason in holds.items():
        print(f"hold: {source_id}: {reason}")
    if not data.get("sources"):
        print("no sources; add one with `tarmac sources add`")
    return 0


def cmd_sources_list(args) -> int:
    data = _request(args, "GET", "/api/v1/sources?archived="
                    + ("1" if args.archived else "0"))
    for s in data.get("sources") or []:
        flags = "" if s["enabled"] else " [disabled]"
        flags += " [archived]" if s.get("archived") else ""
        print(f"{s['slug']:<24} {s['provider']:<11} {s['adapter']:<11} "
              f"{s['account'] or '-':<16}{flags}")
    return 0


def cmd_sources_add(args) -> int:
    config = json.loads(args.config) if args.config else {}
    body = {"name": args.name or args.provider, "provider": args.provider,
            "adapter": args.adapter or args.provider,
            "account": args.account, "config": config}
    data = _request(args, "POST", "/api/v1/sources", body, authed=True)
    print(f"created {data['slug']} ({data['id']})")
    return 0


def cmd_sources_rm(args) -> int:
    _request(args, "DELETE", f"/api/v1/sources/{args.slug}", authed=True)
    print(f"deleted {args.slug}")
    return 0


def _toggle(args, enabled: bool) -> int:
    data = _request(args, "PATCH", f"/api/v1/sources/{args.slug}",
                    {"enabled": enabled}, authed=True)
    print(f"{data['slug']}: {'enabled' if enabled else 'disabled'}")
    return 0


def cmd_refresh(args) -> int:
    data = _request(args, "POST", f"/api/v1/sources/{args.slug}/refresh?wait=40")
    print(_format_source(data))
    return 0


def cmd_login(args) -> int:
    data = _request(args, "POST", f"/api/v1/sources/{args.slug}/login")
    print(f"launched in Terminal: {data['command']}")
    print("finish the sign-in there, then: tarmac refresh " + args.slug)
    return 0


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(prog="tarmac")
    parser.add_argument("--port", type=int, default=DEFAULT_PORT)
    sub = parser.add_subparsers(dest="command", required=True)

    for name in ("serve", "daemon"):
        p = sub.add_parser(name, help="run the service in the foreground")
        p.add_argument("--host", default="127.0.0.1",
                       help="bind address (0.0.0.0 inside a container; the "
                            "Host-header boundary still admits loopback only)")
        p.set_defaults(fn=cmd_daemon)

    p = sub.add_parser("status", help="show every source's runway")
    p.add_argument("--json", action="store_true")
    p.add_argument("--passive", action="store_true", help="cached observations only; never activate polling")
    p.set_defaults(fn=cmd_status)

    p = sub.add_parser("refresh", help="poll one source now")
    p.add_argument("slug")
    p.set_defaults(fn=cmd_refresh)

    p = sub.add_parser("login", help="open the OAuth sign-in for a source")
    p.add_argument("slug")
    p.set_defaults(fn=cmd_login)

    sources = sub.add_parser("sources", help="manage sources")
    ssub = sources.add_subparsers(dest="action", required=True)
    p = ssub.add_parser("list")
    p.add_argument("--archived", action="store_true")
    p.set_defaults(fn=cmd_sources_list)
    p = ssub.add_parser("add")
    p.add_argument("provider")
    p.add_argument("--adapter", help="defaults to the provider name")
    p.add_argument("--name")
    p.add_argument("--account", default="")
    p.add_argument("--config", help="JSON object")
    p.set_defaults(fn=cmd_sources_add)
    p = ssub.add_parser("rm")
    p.add_argument("slug")
    p.set_defaults(fn=cmd_sources_rm)
    p = ssub.add_parser("enable")
    p.add_argument("slug")
    p.set_defaults(fn=lambda a: _toggle(a, True))
    p = ssub.add_parser("disable")
    p.add_argument("slug")
    p.set_defaults(fn=lambda a: _toggle(a, False))

    p = sub.add_parser("service", help="manage the LaunchAgent")
    p.add_argument("action", choices=("install", "uninstall", "restart",
                                      "status"))
    p.set_defaults(fn=lambda a: service.run(a.action))

    args = parser.parse_args(argv)
    return args.fn(args)


if __name__ == "__main__":
    sys.exit(main())
