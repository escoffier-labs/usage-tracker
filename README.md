<p align="center">
  <img src="docs/assets/usage-tracker-social-preview.jpg" alt="Usage Tracker banner" width="900">
</p>

<h1 align="center">Usage Tracker</h1>

<p align="center">
  <strong>Machine-wide session cost analytics for OpenClaw, Claude Code, and Codex CLI, with API spend separated from OAuth subscription value.</strong>
</p>

<p align="center">
  <a href="#install"><b>Install</b></a>
  &middot;
  <a href="#try-it-in-60-seconds"><b>Try it in 60 seconds</b></a>
  &middot;
  <a href="#billing-classification"><b>Billing classification</b></a>
</p>

<p align="center">
  <img src="https://shieldcn.dev/github/ci/escoffier-labs/usage-tracker.svg?workflow=tests.yml&branch=main&label=ci&size=xs" alt="CI status">
  <img src="https://shieldcn.dev/badge/HTML5-E34F26.svg?logo=html5&logoColor=white&size=xs" alt="HTML5">
  <img src="https://shieldcn.dev/badge/Python-3.x-3776AB.svg?logo=python&logoColor=white&size=xs" alt="Python 3">
  <img src="https://shieldcn.dev/badge/OpenClaw-usage_analytics-ef4444.svg?size=xs" alt="OpenClaw usage analytics">
  <img src="https://shieldcn.dev/badge/static_page-no_backend-0f766e.svg?size=xs" alt="Static page, no backend">
  <img src="https://shieldcn.dev/badge/license-MIT-green.svg?size=xs" alt="MIT license">
</p>

See which sessions cost real API money, which sessions spent subscription value, and which models are missing price data.

<!-- proof: app screenshot with sample data lands here -->

## What it does

usage-tracker is a static browser dashboard plus stdlib-only Python exporter that turns local OpenClaw session transcripts, Claude Code project transcripts, and Codex CLI rollouts into a flat `data/usage.json`. It exists to split real per-token API spend from OAuth subscription burn, so session cost, model usage, and subscription ROI are visible in the same page. It differs from dragging raw logs into a spreadsheet because the exporter reads the complete plain transcript locations, skips undercounting `*.trajectory.jsonl` files, estimates API-equivalent cost for CLI transcripts that do not carry cost, and keeps the page offline with `localStorage` persistence instead of a backend. The dashboard shows per-agent, per-session, per-model, daily, and subscription views across five design variants.

## Install

```bash
git clone https://github.com/escoffier-labs/usage-tracker.git
cd usage-tracker
```

No package install is required for the app. The page is single-file HTML with vanilla JavaScript and inline SVG charts, and the exporter uses only the Python standard library.

## Try it in 60 seconds

Build `data/usage.json` from your local session transcripts and serve the page:

```bash
python3 bin/export_usage.py --since 30d
python3 -m http.server 5200
```

Open http://localhost:5200.

For an always-fresh dataset, install the opt-in user-systemd timer with a 5 minute refresh:

```bash
cp bin/usage-tracker-export.service ~/.config/systemd/user/
cp bin/usage-tracker-export.timer   ~/.config/systemd/user/
systemctl --user daemon-reload
systemctl --user enable --now usage-tracker-export.timer
```

Edit the service file's `ExecStart` path to point at wherever you cloned this repo.

## What the dashboard shows

- **API spend** versus **OAuth value extracted** for the period
- Per-agent breakdown, including main, coder, codex-builder, claude-code, and codex-cli
- Sessions table grouped by session id, with per-call drill-down
- Per-model bar chart, stacked by billing type
- Daily cost time series, stacked by billing type
- Subscription ROI: monthly subscription costs versus OAuth value extracted
- Five design variants to choose from

## Billing classification

Each call is classified as `oauth` (subscription burn, billed flat) or `api` (real per-token spend):

- Calls served by a subscription backend are detected from the API id (`openai-chatgpt-responses`, `cli`, `google-gemini-cli`) regardless of provider.
- Providers in the OAuth set (`openai-codex`, `claude-cli`, `acpx`, `google-gemini-cli` by default) are `oauth`; everything else is `api`.
- Override per export with `--oauth-providers`. Example: if grok runs on a SuperGrok subscription via device-code OAuth, use `--oauth-providers openai-codex,claude-cli,acpx,google-gemini-cli,xai`.
- Claude Code records are always `oauth` (Claude Code transcripts carry token counts but no cost; the exporter estimates the API-equivalent cost from a built-in pricing table). Disable the source with `--no-claude-code` or point elsewhere with `--claude-projects PATH`.
- Codex CLI rollouts (`~/.codex/sessions`) are always `oauth` (ChatGPT subscription); per-call tokens come from `token_count` events and cost is estimated from the same pricing table. Disable with `--no-codex` or point elsewhere with `--codex-sessions PATH`.
- Models missing from the pricing table export with `costUsd: null`; the page counts them as "calls missing cost data" instead of silently pricing them at zero.

## Drag-and-drop fallback

If `data/usage.json` is missing, for example because you opened the page on a different machine, drop one or more OpenClaw `*.trajectory.jsonl` files or a previously-exported `usage.json` onto the page. Records are parsed client-side and cached in `localStorage`.

## Architecture

- `bin/export_usage.py` walks `~/.openclaw/agents/*/sessions/*.jsonl` (plain session transcripts; one per session), `~/.claude/projects/*/*.jsonl` (Claude Code), and `~/.codex/sessions/**/*.jsonl` (Codex CLI rollouts), extracts per-call usage, and writes a flat array to `data/usage.json`. OpenClaw's `*.trajectory.jsonl` files are not used: they only exist for a fraction of runs and undercount usage by an order of magnitude.
- `index.html` fetches `data/usage.json` on load, falls back to drag-and-drop, normalizes records into renderer-friendly aggregates, and displays the dashboard.
- No backend. `localStorage` caches the last load and your subscription settings.

## Development

```bash
python3 -m pytest tests/     # exporter tests
python3 -m http.server 5200  # page
```

## Why not raw trajectory files, spreadsheets, or a backend dashboard?

Raw OpenClaw `*.trajectory.jsonl` files are useful as a fallback input when `data/usage.json` is absent, but this repo does not use them for the exporter because they exist for only a fraction of runs and undercount usage by an order of magnitude. A spreadsheet can analyze a flat export after you build the mapping yourself, but usage-tracker already walks the transcript locations, classifies API versus OAuth billing, and renders the per-agent, session, model, daily, and subscription views. A backend dashboard would add a service to operate; this project deliberately keeps preprocessing offline and the viewer static.

## What usage-tracker is not

usage-tracker is not a hosted billing service, a provider invoice, or a data warehouse. Claude Code and Codex CLI transcripts carry no cost, so their costs are API-equivalent estimates from the built-in pricing table, and unknown models export `costUsd: null` instead of being silently priced at zero. The page does not persist data to a server: settings, UI preferences, and the last-loaded payload stay in `localStorage`.

## License

MIT. usage-tracker is maintained by `escoffier-labs/usage-tracker`.
