<p align="center">
  <img src="docs/assets/usage-tracker-social-preview.jpg" alt="Usage Tracker banner" width="900">
</p>

<h1 align="center">Usage Tracker</h1>

<p align="center">
  <img src="docs/assets/marks/usage-tracker-circle.svg" alt="" width="40" height="40">
</p>

<p align="center">
  <strong>See what your coding agents cost, how many tokens they burn, and how much of your plan is left.</strong>
</p>

<p align="center">
  A standalone clone of the Usage page in T3 Code (Cost, Tokens and Limits). It needs only Python 3 and a browser. You do not need T3 Code.
</p>

<p align="center">
  <a href="#quick-start">Quick start</a> &middot; <a href="#what-it-shows">What it shows</a> &middot; <a href="#privacy">Privacy</a> &middot; <a href="#credits">Credits</a>
</p>

<p align="center">
  <img src="https://img.shields.io/badge/Python-3.10+-3776AB?style=for-the-badge&logo=python&logoColor=white" alt="Python 3.10+">
  <img src="https://img.shields.io/badge/dependencies-none-0f766e?style=for-the-badge" alt="No dependencies">
  <img src="https://img.shields.io/badge/license-MIT-green?style=for-the-badge" alt="MIT license">
</p>

## Quick start

```bash
git clone https://github.com/escoffier-labs/usage-tracker.git
cd usage-tracker
python3 bin/usage_tracker.py
```

That one command reads your local agent history, prices it, starts a server on `http://127.0.0.1:5200/` (this machine only), and opens your browser. The first run over a large history can take a minute. The Refresh button in the page re-runs the scan.

Options: `--port 0` picks a free port, `--no-browser` skips opening a window, `--no-export` serves the last export, `--no-limits` never contacts Claude or Codex, `--offline` uses cached prices only, `--days N` sets the history length (default 90).

To write the data file without serving anything: `python3 bin/export_usage.py` (writes `data/usage.json`; see `--help`).

## What it shows

The page follows T3 Code's Usage page.

- **Cost, Tokens and Limits** tabs. Cost is an API-rate estimate of what the usage would cost at list prices, not what a subscription charged you.
- **Past 24h** (hourly), **7**, **30** and **90 days**.
- A provider filter, per-provider totals and session counts, and a layered per-provider chart with a hover tooltip.
- Totals: processed, cached and uncached input, output, and cache savings. Share bars for tokens by type, cost by type, and cost by speed (fast or priority premium where the provider bills one).
- A model table sorted like T3's (cost for Cost, tokens for Tokens) and a per-model dialog with its own trend, cost and token mix, cache hit rate and cost per million tokens.
- **Model prices**: set your own per-million-token prices for any model, for example one the price table does not know. They are stored in this browser (`localStorage`) and applied in the page. Models with no price show as Unpriced instead of zero.
- **Limits**: remaining quota bars with the even-spending pace marker and reset times for Claude Code and Codex.
- Data freshness, price status, and a warning for every source that could not be read in full.
- Keyboard: `C`, `T`, `L` switch tab; `Ctrl` or `Cmd` + `Shift` + `1` to `4` (or just `1` to `4`) switch period. Light and dark themes follow your system, with a toggle.

### Where the data comes from

Missing folders are skipped silently. Locations and parsing rules match T3 Code's readers, including de-duplication of repeated records.

| Provider | Location | Notes |
| --- | --- | --- |
| Claude Code | `~/.claude/projects/**/*.jsonl` (or `$CLAUDE_CONFIG_DIR`) | One request per message even though Claude writes a line per content block. Advisor-model calls are billed to their own model. Fast mode is priced as fast. |
| Codex | `~/.codex/sessions/**/*.jsonl[.zst]` (or `$CODEX_HOME`) | Cumulative counters validate each request. Forked and copied rollouts are not billed twice. `.zst` files need Python 3.14 or the `zstd` command. |
| OpenCode | `~/.local/share/opencode` (`opencode*.db`, legacy `storage/message`) | Honors `OPENCODE_DATA_DIR` and `XDG_DATA_HOME`. |
| Grok Build | `~/.grok/sessions/**/updates.jsonl` (or `$GROK_HOME`) | Uses the cost Grok reports per model. |
| Antigravity | `~/.gemini/antigravity*` and `~/.config/antigravity` conversation databases | Decodes the protobuf usage rows. `$ANTIGRAVITY_DATA_DIR` overrides. |
| Pi | `~/.pi/agent/sessions` (or `$PI_CODING_AGENT_SESSION_DIR`) | |

Prices come from [LiteLLM's model price table](https://github.com/BerriAI/litellm), fetched once a day and cached in your user cache directory (`~/.cache/usage-tracker` on Linux). Offline, the cached copy is used; with no cache, costs show as unknown rather than zero.

Not ported from T3 Code: Cursor and Muse, and T3's own database lookup that resolves some older Codex service tiers (those requests are billed as standard).

### Limits

Limits read the login your tools already saved: Claude Code's OAuth token (`~/.claude/.credentials.json`, or the macOS Keychain item `Claude Code-credentials`) and the ChatGPT token in `~/.codex/auth.json`. If you are not signed in to one of them, it is simply not shown.

## Several machines (optional)

Single-machine use never needs this. To merge usage from more than one machine, `bin/collect_fleet_usage.py` runs the exporter on each machine in `config/fleet.json` (this one, or any SSH alias that has Python 3.10+; nothing is installed remotely) and writes `data/fleet-usage.json` with a machine dimension and each machine's limits. The format is in [docs/fleet-usage-schema.md](docs/fleet-usage-schema.md). `systemd/usage-tracker-fleet.service` and `.timer` run it every five minutes:

```bash
python3 bin/collect_fleet_usage.py            # one collection, uses config/fleet.json
systemctl --user link "$PWD/systemd/usage-tracker-fleet.service" "$PWD/systemd/usage-tracker-fleet.timer"
systemctl --user enable --now usage-tracker-fleet.timer
```

## Privacy

- The server binds to `127.0.0.1` only, serves just the page and the data file, rejects requests whose `Host` is not loopback, and requires a custom header on the refresh endpoint.
- Tokens are read into memory, sent only over HTTPS to Anthropic (`api.anthropic.com`) and OpenAI (`chatgpt.com`) respectively, never follow a redirect, and are never written to `data/usage.json`, logs or error messages.
- The only other request is the public LiteLLM price table. Your transcripts and prompts never leave your machine, and the page loads no CDN or font.
- `data/usage.json` and `data/fleet-usage.json` are git-ignored and written with mode `0600`. It contains token counts, costs and model names, not conversation text or session ids.

## Development

```bash
python -m pytest tests/ -q
python scripts/check_frontend.py index.html
node tests/frontend.test.js
python scripts/scan_repo.py .
```

No runtime or development dependency besides Python 3 (`pytest` for tests) and Node for the page-logic check. See [AGENTS.md](AGENTS.md) for the architecture.

## Credits

The design, layout and logic are ported from the Usage page of [T3 Code](https://github.com/pingdotgg/t3code) (MIT, Copyright (c) 2026 T3 Tools Inc.). See [NOTICE](NOTICE) for what was ported and the license text.

## License

MIT. See [LICENSE](LICENSE) and [NOTICE](NOTICE).
