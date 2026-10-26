# Setup and reference

[Tarmac](../README.md) runs independently of the harness querying it. Its API, CLI and dashboard expose the same stored observations. Harness access requires the ability to call the local API or CLI; integrations are configured separately.

## Installation and platforms

The core needs Python 3.11+ with SQLite support and has no third-party Python runtime dependencies. From the source root, `python3 -m tarmac serve` starts the API and poller; Ctrl-C stops the foreground service. The dashboard is at `http://127.0.0.1:8767`. A new state directory has no sources; existing state resumes enabled sources. Review provider access before starting and keep the service bound to loopback.

For an installed `tarmac` command, use a stable checkout and a virtual environment. Run these commands from the source root:

```sh
python3 -m venv .venv
. .venv/bin/activate
python3 -m pip install -e .
```

Installation uses Hatchling and may download build tooling. Provider CLIs and credentials are separate prerequisites. The Python core uses portable standard-library modules, but CLI/PTY adapters need a Unix-like host; Terminal login, notifications and LaunchAgent management use macOS tools. Windows support isn't established.

## Add a source

Each source has its own adapter and account configuration. In another terminal, from the source root, add an OpenRouter source after setting `OPENROUTER_API_KEY` in the **service's environment** or configuring its OpenCode credential entry:

```sh
python3 -m tarmac sources add openrouter
python3 -m tarmac status --passive --json
python3 -m tarmac sources disable openrouter
```

Adding a source enables it and queues a poll. The passive status command reads cached data; it can show an unknown reading while the first poll is pending. Disabling preserves history. `sources rm <slug>` deletes the source and its readings. `sources list` shows the slugs used by `refresh`, `login`, `sources enable` and `sources disable`. Installed users can substitute `tarmac` for `python3 -m tarmac`.

| Adapter | Collection interface and setup |
| --- | --- |
| Claude | Signed-in `claude` CLI, auth-status probe, `/usage` PTY and local usage cache. Requires a trusted project. `config_dir` selects an account directory; `CLAUDE_USAGE_CWD` selects the project. |
| Codex | Signed-in `codex` CLI, auth-status probe and app-server readings, with session-file fallback. `codex_home` selects an account directory. |
| Antigravity | Signed-in `agy` CLI usage PTY; reports model-group windows. `cwd` selects its working directory. |
| DeepSeek, Kimi, MiniMax | Provider balance or usage HTTP endpoints using API credentials. |
| OpenRouter | Account credits and per-key cap endpoints; account credits may need a management key. |
| Z.ai | Wallet endpoint; requires `customer_id` in source config as well as an API credential. |
| xAI | OAuth-backed user, billing and reset-credit endpoints; refreshes the OAuth grant when needed. |
| Meta | A generation request whose response stream carries subscription usage. Polling consumes capacity. |

Use `--config` with a JSON object for adapter options. For a separate OpenRouter credential entry:

```sh
python3 -m tarmac sources add openrouter --account secondary \
  --name 'OpenRouter secondary' --config '{"auth_entry":"openrouter-secondary"}'
```

Key adapters look in OpenCode's `~/.local/share/opencode/auth.json`, or the adjacent `opencode.db` credential table when the JSON file is absent, then fall back to a provider environment variable. `auth_entry` changes the store entry name; `key_env` changes the fallback variable name. A matching store credential takes precedence over that variable. Account labels and directory overrides select scope; they aren't a process sandbox.

## Polling and cached reads

Enabled sources normally poll every 300 seconds, with up to four adapters running concurrently. `config.demand: true` restricts a source to periods with an active reader. Use it for cost-bearing probes such as Meta. A normal status read can queue stale demand sources for refresh; the dashboard reads active status every 45 seconds. Explicit refresh requests also queue work, subject to demand and enabled-source rules.

`GET /api/v1/snapshot` and `status --passive --json` return schema-v2 cached observations without activating demand, probing login or calling providers from that request. An already-running poller continues independently. `GET /api/v1/status` is active demand. Source CRUD, history and adapter metadata are under `/api/v1/sources` and `/api/v1/meta`. Other non-health/non-snapshot GET requests also count as readers; passive consumers should use the explicit snapshot route.

Compare each applicable window's identity, unit, age and reset precision. Unknown or expired readings don't establish available capacity. Burn rate describes adjacent observations and doesn't predict a future task's cost. Some CLI reads are throttled and reuse measurements; provider output and undocumented endpoints can change.

Tarmac supplies observations to other tools; it doesn't dispatch workers, buy credits, redeem resets or select paid fallback automatically. It runs independently of the harness consuming those observations.

## Credentials and local access

Tarmac runs with the permissions of its user. CLI adapters use separately installed, signed-in provider tools. Credentials remain in their provider/OpenCode stores or the service environment. Put lookup names and paths in source config; keep secrets out of it. xAI renewal writes its updated grant back to OpenCode's credential store. Run one poller per machine to avoid competing refresh-token rotation. Its OAuth path uses the fixed `xai` store entry; key-adapter lookup overrides aren't a universal OAuth account-isolation mechanism.

State defaults to `~/.tarmac` (`TARMAC_HOME` overrides it) and includes SQLite data, the API token, plugins and logs. Raw readings are pruned after 30 days; numeric history remains. Output scrubbing is heuristic: treat state and arbitrary plugin output as private. The `command` adapter and executable plugins in the state directory run trusted local programs without sandboxing.

Source creation, updates and deletion require the local bearer token. Reads, refresh and login routes don't require that token in this version. Host/Origin checks restrict requests to loopback names but don't isolate callers on the machine. Keep the bind address, firewall and local-user access restricted. Login is an explicit action that opens macOS Terminal and the provider's sign-in flow.

## macOS background operation

An installed `tarmac service install` writes and starts a user LaunchAgent. Don't run it alongside a foreground instance. It persists only PATH, HOME, USER, LOGNAME and resolved absolute TARMAC_HOME. Provider-key variables and other directory/environment overrides won't survive installation. Use a stable executable path; reinstall after moving it or changing the service environment. `tarmac service uninstall` removes the job and plist, leaving retained state.

## Optional OpenChamber panel

The [OpenChamber panel](../openchamber/README.md) has its own Node/npm build and host-side relay. The core dashboard works without it. The panel requires its generated bundle and an icon before installation; OpenChamber compatibility needs separate validation.

## Development

[`tarmac/`](../tarmac/) contains the service, dashboard and adapters; [`tests/`](../tests/) contains parser, storage, projection, installer and API regressions. Tests use `unittest`; several API tests bind local ports and plugin tests execute fixture programs. Review the relevant subset before running it on a restricted host. Live provider behavior, login flows and service installation need separate integration checks.

## License

Owned source and documentation are [MIT licensed](../LICENSE). The comic artwork is AI-generated and offered under MIT to the extent applicable; see [RIGHTS.md](../RIGHTS.md). Outside dependencies and provider software retain their own licenses and terms.
