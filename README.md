# 📺 TVDebloater

A safe, reversible debloater for Android TV / Google TV (built and tested for an
**Acer Google TV**, works with any ADB-capable TV: TCL, Sony, Hisense, Philips, Sharp,
Fire TV…). It lists every package on the TV, classifies it (ads, promo, telemetry,
cruft, vendor services, streaming apps, system core…) using public debloat databases,
and lets you disable the junk **without ever risking a brick**.

```
┌──────────────────────────── Flask backend (app.py) ───────────────────────────┐
│  discovery.py   → find TVs on the LAN (mDNS + port scan)                      │
│  adb.py         → talk to the TV (adb binary, bundled)                        │
│  device.py      → list packages, parse dumpsys, metrics                       │
│  rules.py       → classify every package: tier + risk + protected             │
│  actions.py     → safety gates, batches of 10, journal, undo                  │
│  mcp_*.py       → POST /mcp: the same engine for AI agents (TurboMCP)         │
└───────────────────────────────── HTML frontend ───────────────────────────────┘
```

## Safety model (why this will not brick your TV)

| Principle | How it's enforced |
|---|---|
| **Disable, never uninstall** | Default action is `pm disable-user --user 0` — 100% reversible, the app stays on disk. Uninstall exists only behind Expert mode. |
| **Hard keep-list** | 60+ protected packages (launcher, GMS/Play, remote services, HDMI/input services, `com.android.location.fused`…) cannot be touched — not even from the API. |
| **Guard rails** | The stock launcher can only be disabled after a replacement launcher (FLauncher/Projectivy) is installed *and* enabled. The only keyboard cannot be disabled. |
| **Small batches** | Max 10 packages per apply — test the TV, then continue. Never a "disable all 300" button. |
| **Journal + undo** | Every command is logged with its inverse. One click undoes a whole batch. `reports/undo.sh` re-enables everything, even from a clean machine. |
| **No root, no flashing** | Nothing here needs root/rooting — and you shouldn't. Unlocking the bootloader drops Widevine L1→L3 (Netflix SD). |

## Quickstart

```bash
./run.sh                # downloads adb into tools/ if missing, installs Flask, serves the UI
```

Open **http://127.0.0.1:8477** → *Connect device* → *Scan network* → pick your TV.

Try the whole thing without a TV first: `TVD_MOCK=1 ./run.sh`

### Enabling debugging on an Acer / Google TV

1. **Settings → System → About** → highlight **Build** and press OK 7× (turns on Developer options).
2. **Settings → System → Developer options**:
   * **USB debugging** = ON (some models: **Network debugging** / **ADB debugging**)
   * Android 11+ shows **Wireless debugging**: open it (keep that screen visible!), then in the tool use
     *Pair* with the IP + **pairing port** + 6-digit code, then *Connect* to the **main port** shown on that screen.
3. Classic Android TV (port 5555): just enable debugging and hit *Connect* — the tool finds it via scan.

> Phone hotspot / guest Wi-Fi: laptop and TV must be on the **same** network segment, otherwise nothing will be found.

## What it does

* **Packages tab** – search/filter/sort all ~300 packages by tier, risk, state, system/user.
  Details drawer shows dependencies, permissions, code path, and the exact adb command.
* **Presets** – `Ads & Telemetry purge`, `Declutter`, `Trim extra Google apps`, `Vendor diet`,
  `Expert`. Pre-selected, never auto-applied, always previewed command-by-command.
* **Tune & Guide** – animation scales off, cache trim, before/after RAM+process snapshots,
  and the guidance cards (launcher swap, SmartTube, DNS-level ad blocking…).
* **Journal** – every action with its undo; per-batch one-click revert.
* **Report** – markdown report + `reports/undo.sh` ready to run from anywhere.

## Agent access (MCP)

The server also speaks the [Model Context Protocol](https://modelcontextprotocol.io) at
**`http://127.0.0.1:8477/mcp`** (Streamable HTTP, built on [TurboMCP](https://pypi.org/project/turbomcp/)),
so an AI agent can do the whole job: find and connect to the TV, pick a preset, preview, apply in
batches, check the result, undo.

```bash
# Claude Code
claude mcp add --transport http tvdebloater http://127.0.0.1:8477/mcp
# ...with a token (see below)
claude mcp add --transport http tvdebloater http://127.0.0.1:8477/mcp --header "Authorization: Bearer $TVD_MCP_TOKEN"
```

Any other MCP client that supports HTTP servers works the same way. Try it without a TV:
`TVD_MOCK=1 ./run.sh`.

**The agent gets the same safety model as the UI, not a way around it.** Every change goes through
the same engine: hard keep-list, launcher and keyboard guards, max 10 packages per batch, journal and
undo. On top of that:

* Packages are only ever **disabled or re-enabled**. Uninstall and *Expert mode* are not exposed —
  that consent belongs to a human at the UI. Expert-risk and unclassified packages are refused.
* Package names must be valid Android names (they end up in an `adb shell` command).
* The apply tools are separate from the read-only ones, so a client can auto-approve browsing and
  still prompt before anything touches the TV (`apply_changes`, `undo_batch`, `apply_tuning`).
* Device-changing calls run one at a time, even if the agent fires them in parallel.

| Tools | |
|---|---|
| `get_status` `discover_devices` `connect_device` `pair_device` `select_device` | find and connect |
| `list_packages` `get_package` `list_presets` `get_preset` | look around (compact, paged output) |
| `preview_changes` | dry run: exact commands + what the rules refuse |
| `apply_changes` `undo_batch` `apply_tuning` | **change the TV** (journaled) |
| `get_journal` `save_snapshot` `compare_snapshots` `generate_report` | verify and document |

**Who can reach `/mcp`.** Browsers are kept out: a request with a foreign `Origin` is refused, no CORS
headers are sent, and without a token the `Host` must be `localhost` / `127.0.0.1` / `[::1]` (blocks
DNS rebinding). Binding to anything other than loopback (`TVD_HOST=0.0.0.0`) **disables `/mcp`
unless a token is set**.

| Variable | Effect |
|---|---|
| `TVD_MCP=0` | don't mount `/mcp` |
| `TVD_MCP_TOKEN=<secret>` | require `Authorization: Bearer <secret>` |
| `TVD_MCP_ALLOWED_HOSTS=a.lan,b.lan` | extra `Host` names accepted when no token is set |

## Classification sources

| Source | What comes from it |
|---|---|
| [UAD-ng](https://github.com/Universal-Debloater-Alliance/universal-android-debloater-next-generation) | 5 300+ packages, descriptions, dependencies, removal risk |
| [26zl/tv-tweak](https://github.com/26zl/tv-tweak) | per-device package tiers + keep lists (Fire TV, Sharp Google TV) |
| [skinny-bravia](https://github.com/therealhoodboy/skinny-bravia) | Sony Bravia package catalogue |
| [tv.cobanov.dev](https://tv.cobanov.dev/) | never-touch list (TCL/Google TV community guide) |
| [android-tv-debloat-toolkit](https://github.com/seun-novodev/android-tv-debloat-toolkit) | TCL safe list |
| hand-curated (`data/curated_extra.json`) | TV-specific rules, keep-list with reasons, presets |

Priority: **keep-list → curated → tv-tweak → UAD-ng → vendor prefix → heuristics → unclassified**.
Everything unknown is flagged *unclassified / expert* and excluded from presets.

Merge order / regeneration:

```bash
python3 scripts/update_sources.py    # re-download upstream data + rebuild data/tv_rules.json
python3 scripts/selftest.py          # 160+ checks: classification, guards, apply/undo, REST API, MCP
```

Never edit `data/tv_rules.json` by hand — edit `data/curated_extra.json` and rebuild.

## Layout

```
app.py                  Flask app / REST API (State = what the UI and MCP both call)
run.sh                  launcher (bootstraps adb + Flask + TurboMCP)
debloater/
  adb.py  mockadb.py    adb wrapper + full fake TV for testing
  discovery.py          LAN scan + mDNS
  device.py             inventory, dumpsys detail, metrics
  rules.py              classification engine
  actions.py            safety gates, batching, journal+undo
  journal.py            append-only log, undo script, markdown report
  mcp_tools.py          the MCP tools + agent instructions
  mcp_endpoint.py       POST /mcp: origin / host / token checks around TurboMCP
data/                   curated rules + upstream databases (raw_sources/)
scripts/                import_sources.py, update_sources.py, selftest.py
journal/journal.jsonl   every change ever made, with undo info
reports/                generated reports + undo.sh + snapshots.json
tools/platform-tools/   bundled adb (auto-downloaded)
```

## Troubleshooting

| Symptom | Fix |
|---|---|
| `unauthorized` | Look at the TV — an "Allow USB debugging?" dialog is waiting. Tick "always allow". |
| Connect works but no packages | TV busy booting; hit ↻ (refresh) after a few seconds. |
| Scan finds nothing | Debugging off, wrong network segment, or Apple-style client isolation on the router. Use manual IP entry. |
| Wireless debugging pairing fails | You must retype the code each time; pairing port ≠ connect port. |
| An app broke (rare) | Journal → *Undo this batch*. Worst case: `reports/undo.sh` or factory reset. |
| Tool says "protected" | That package is on the hard keep-list — the tool is protecting you from yourself. 🙂 |

## License / credits

Personal tool. Classification data belongs to the upstream projects credited above.
adb is bundled by downloading Google's official platform-tools.
