<p align="center">
  <img src="https://img.shields.io/badge/HTML5-E34F26?style=flat-square&logo=html5&logoColor=white" alt="HTML5" />
  <img src="https://img.shields.io/badge/Python-3776AB?style=flat-square&logo=python&logoColor=white" alt="Python" />
  <img src="https://img.shields.io/badge/License-MIT-green?style=flat-square" alt="MIT License" />
</p>

# Usage Tracker

OpenClaw session cost analytics. Single static page plus a tiny Python exporter that reads OpenClaw trajectory jsonls and writes a flat `data/usage.json` the page renders.

Splits real **API spend** from **OAuth subscription burn** (what your Codex Pro / Claude Max calls would have cost at API rates) so you can see what each session actually cost and whether your subscriptions are paying off.

## Quick start

```bash
git clone https://github.com/solomonneas/usage-tracker.git
cd usage-tracker

# Build data/usage.json from your OpenClaw sessions
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

## Drag-and-drop fallback

If `data/usage.json` is missing (e.g., you opened the page on a different machine), drop one or more `*.trajectory.jsonl` files or a previously-exported `usage.json` onto the page. Records are parsed client-side and cached in localStorage.

## What it shows

- **API spend** versus **OAuth value extracted** for the period
- Per-agent breakdown (main, coder, codex-builder, claude-builder, ...)
- Sessions table grouped by session id, with per-call drill-down
- Per-model bar chart, stacked by billing type
- Daily cost time series, stacked by billing type
- Subscription ROI: monthly subscription costs versus OAuth value extracted
- Five design variants to choose from

## Architecture

- `bin/export_usage.py` walks `~/.openclaw/agents/*/sessions/*.trajectory.jsonl`, extracts `model.completed` events, writes a flat array to `data/usage.json`.
- `index.html` fetches `data/usage.json` on load (drag-and-drop fallback), normalizes records into renderer-friendly aggregates, displays.
- No backend. localStorage caches the last load and your subscription settings.

## Development

```bash
python3 -m pytest tests/   # exporter tests
python3 -m http.server 5200  # page
```

## License

MIT
