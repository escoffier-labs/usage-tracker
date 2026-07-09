<p align="center">
  <img src="docs/assets/usage-tracker-social-preview.jpg" alt="Usage Tracker banner" width="900">
</p>

<h1 align="center">Usage Tracker</h1>

<p align="center">
  <img src="docs/assets/marks/usage-tracker-circle.svg" alt="" width="40" height="40">
</p>

<p align="center">
  <strong>See what your agent sessions actually cost.</strong>
</p>

<p align="center">
  Static page plus a tiny Python exporter: OpenClaw, Claude Code, and Codex session spend. API cost vs OAuth subscription value, per model and per agent. No backend.
</p>

<p align="center">
  <a href="#quick-start">Quick start</a> &middot; <a href="#what-it-shows">What it shows</a>
</p>

<p align="center">
  <img src="https://img.shields.io/badge/Python-3.x-3776AB?style=for-the-badge&logo=python&logoColor=white" alt="Python 3">
  <img src="https://img.shields.io/badge/static_page-no_backend-0f766e?style=for-the-badge" alt="Static page, no backend">
  <img src="https://img.shields.io/badge/license-MIT-green?style=for-the-badge" alt="MIT license">
</p>

## Install

```bash
git clone https://github.com/escoffier-labs/usage-tracker.git
cd usage-tracker
# export local session transcripts to data/usage.json, then open the static page
python export.py    # see repo for exact entrypoint
```

## What it does

| | Job | What you get |
|---|---|---|
| **Collect** | Local transcripts | OpenClaw, Claude Code, Codex rollouts |
| **Split** | API vs OAuth value | What you paid vs what subscriptions absorbed |
| **Chart** | Models and agents | Daily series, per-model bars, session tables |
| **Stay local** | Static HTML | No backend, no phone-home |


## Quick start

```bash
git clone https://github.com/escoffier-labs/usage-tracker.git
cd usage-tracker

# Build data/usage.json from your OpenClaw + Claude Code sessions
python3 bin/export_usage.py --since 30d

# Print a compact machine summary for station health or daily briefs
python3 bin/export_usage.py --since 30d --summary-json

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
- Claude Code records are always `oauth` (Claude Code transcripts carry token counts but no cost; the exporter estimates the API-equivalent cost from a built-in pricing table). Disable the source with `--no-claude-code`, point elsewhere with `--claude-projects PATH`, or include more machines/backups with repeatable `--extra-claude-projects PATH`.
- Codex CLI rollouts (`~/.codex/sessions` and `~/.codex/archived_sessions`) are always `oauth` (ChatGPT subscription); per-call tokens come from `token_count` events and cost is estimated from the same pricing table. The exporter also reads `~/.codex/state_5.sqlite` for total-only backfill when a Codex thread exists but its rollout records are missing. Disable with `--no-codex`, point elsewhere with `--codex-sessions PATH`, include more machines/backups with repeatable `--extra-codex-sessions PATH`, or add more SQLite backfills with repeatable `--extra-codex-state-db PATH`.
- Models missing from the pricing table export with `costUsd: null`; the page counts them as "calls missing cost data" instead of silently pricing them at zero.

## Drag-and-drop fallback

If `data/usage.json` is missing (e.g., you opened the page on a different machine), drop one or more OpenClaw `*.trajectory.jsonl` files or a previously-exported `usage.json` onto the page. Records are parsed client-side and cached in localStorage.

## Architecture

- `bin/export_usage.py` walks `~/.openclaw/agents/*/sessions/*.jsonl` (plain session transcripts; one per session), `~/.claude/projects/*/*.jsonl` (Claude Code), and `~/.codex/**/*.jsonl` (Codex CLI rollouts, including archived sessions), extracts per-call usage, writes a flat array to `data/usage.json`, and uses Codex `state_5.sqlite` only as a total-only missing-thread backfill. OpenClaw's `*.trajectory.jsonl` files are NOT used: they only exist for a fraction of runs and undercount usage by an order of magnitude.
- Codex profile/account counters may include server-side usage that is not retained in local rollout files or SQLite state. This tracker reports local evidence plus local backfill, not an authoritative account lifetime total.
- `index.html` fetches `data/usage.json` on load (drag-and-drop fallback), normalizes records into renderer-friendly aggregates, displays.
- No backend. localStorage caches the last load and your subscription settings.

## Development

```bash
python3 -m pytest tests/   # exporter tests
python3 -m http.server 5200  # page
```

Example multi-machine export:

```bash
python3 bin/export_usage.py \
  --extra-claude-projects /tmp/gandalf-usage/claude/projects \
  --extra-codex-sessions /tmp/gandalf-usage/codex \
  --extra-codex-state-db /tmp/gandalf-usage/codex/state_5.sqlite
```

## License

MIT
