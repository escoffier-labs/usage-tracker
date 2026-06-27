<p align="center">
  <img src="docs/assets/usage-tracker-banner.jpg" alt="Usage Tracker banner">
</p>

<h1 align="center">Usage Tracker</h1>

<p align="center">
  <strong>OpenClaw + Claude Code session cost analytics for API spend, OAuth subscription value, and model usage.</strong>
</p>

<p align="center">
  <img src="https://img.shields.io/badge/HTML5-E34F26?style=for-the-badge&logo=html5&logoColor=white" alt="HTML5">
  <img src="https://img.shields.io/badge/Python-3.x-3776AB?style=for-the-badge&logo=python&logoColor=white" alt="Python 3">
  <img src="https://img.shields.io/badge/OpenClaw-usage_analytics-ef4444?style=for-the-badge" alt="OpenClaw usage analytics">
  <img src="https://img.shields.io/badge/static_page-no_backend-0f766e?style=for-the-badge" alt="Static page, no backend">
  <img src="https://img.shields.io/badge/license-MIT-green?style=for-the-badge" alt="MIT license">
</p>

Machine-wide AI session cost analytics. Single static page plus a tiny Python exporter that reads OpenClaw session transcripts, Claude Code project transcripts, and Codex CLI rollouts, and writes a flat `data/usage.json` the page renders.

Splits real **API spend** from **OAuth subscription burn** (what your Codex Pro / Claude Max calls would have cost at API rates) so you can see what each session actually cost and whether your subscriptions are paying off.

## What it shows

- **API spend** versus **OAuth value extracted** for the period
- Per-agent breakdown (main, coder, codex-builder, claude-code, codex-cli, ...)
- Sessions table grouped by session id, with per-call drill-down
- Per-model bar chart, stacked by billing type
- Daily cost time series, stacked by billing type
- Subscription ROI: monthly subscription costs versus OAuth value extracted
- Five design variants to choose from

## Quick start

```bash
git clone https://github.com/escoffier-labs/usage-tracker.git
cd usage-tracker

# Build data/usage.json from your OpenClaw + Claude Code sessions
python3 bin/export_usage.py --since 30d

# Serve the page
python3 -m http.server 5200
```

Open http://localhost:5200.

For an always-fresh dataset, install the opt-in user-systemd timer (5 minute refresh):

```bash
cp bin/usage-tracker-export.service ~/.config/systemd/user/
cp bin/usage-tracker-export.timer   ~/.config/systemd/user/
systemctl --user daemon-reload
systemctl --user enable --now usage-tracker-export.timer
```

(Edit the service file's `ExecStart` path to point at wherever you cloned this repo.)

## Billing classification

Each call is classified as `oauth` (subscription burn, billed flat) or `api` (real per-token spend):

- Calls served by a subscription backend are detected from the API id (`openai-chatgpt-responses`, `cli`, `google-gemini-cli`) regardless of provider.
- Providers in the OAuth set (`openai-codex`, `claude-cli`, `acpx`, `google-gemini-cli` by default) are `oauth`; everything else is `api`.
- Override per export with `--oauth-providers`. Example: if grok runs on a SuperGrok subscription via device-code OAuth, use `--oauth-providers openai-codex,claude-cli,acpx,google-gemini-cli,xai`.
- Claude Code records are always `oauth` (Claude Code transcripts carry token counts but no cost; the exporter estimates the API-equivalent cost from a built-in pricing table). Disable the source with `--no-claude-code` or point elsewhere with `--claude-projects PATH`.
- Codex CLI rollouts (`~/.codex/sessions`) are always `oauth` (ChatGPT subscription); per-call tokens come from `token_count` events and cost is estimated from the same pricing table. Disable with `--no-codex` or point elsewhere with `--codex-sessions PATH`.
- Models missing from the pricing table export with `costUsd: null`; the page counts them as "calls missing cost data" instead of silently pricing them at zero.

## Drag-and-drop fallback

If `data/usage.json` is missing (e.g., you opened the page on a different machine), drop one or more OpenClaw `*.trajectory.jsonl` files or a previously-exported `usage.json` onto the page. Records are parsed client-side and cached in localStorage.

## Architecture

- `bin/export_usage.py` walks `~/.openclaw/agents/*/sessions/*.jsonl` (plain session transcripts; one per session), `~/.claude/projects/*/*.jsonl` (Claude Code), and `~/.codex/sessions/**/*.jsonl` (Codex CLI rollouts), extracts per-call usage, writes a flat array to `data/usage.json`. OpenClaw's `*.trajectory.jsonl` files are NOT used: they only exist for a fraction of runs and undercount usage by an order of magnitude.
- `index.html` fetches `data/usage.json` on load (drag-and-drop fallback), normalizes records into renderer-friendly aggregates, displays.
- No backend. localStorage caches the last load and your subscription settings.

## Development

```bash
python3 -m pytest tests/   # exporter tests
python3 -m http.server 5200  # page
```

## License

MIT
