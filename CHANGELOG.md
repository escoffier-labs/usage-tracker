# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

### Added
- README screenshots of the Cost, Tokens, model detail and Limits views, and `scripts/demo_data.py`, which writes a seeded synthetic `data/usage.json` for regenerating them.
- OpenCode Go limits (session, weekly and monthly quota), ported from T3 Code's OpenCode reader: the Go key OpenCode saved is sent only to `opencode.ai/zen/go/v1/usage`.
- `GET /api/limits`: a small read-only snapshot of the cached limits with a per-provider `ok`, `stale` or `unavailable` status, for other local tools. It never contacts a provider.
- Concurrent exporters share one limits fetch (a file lock on the cache), and a failed provider keeps its last good reading for up to 24 hours, marked stale.

### Changed
- README rewritten around the page itself: the shared banner, logo mark and badge header are gone, and each tab has its own section and screenshot.
- Rewrote the project as a standalone clone of T3 Code's Usage page (Cost, Tokens and Limits) that needs only Python 3 and a browser. Credit and license text: `NOTICE`.
- `python3 bin/usage_tracker.py` exports, serves the page on 127.0.0.1 and opens the browser; the Refresh button re-runs the scan through a local endpoint.
- Providers: Claude Code, Codex, OpenCode, Grok, Antigravity and Pi, with T3's file locations and de-duplication rules. Missing directories are skipped silently.
- Costs use LiteLLM prices with a 24 hour cache, fall back to the cached copy offline, then to unknown. Models with no price show as Unpriced.
- Claude and Codex limits are read with the logins those tools already saved, never written to disk, and sent only to the provider's own host.
- `index.html` is a single file with hourly and daily windows, per-provider chart, share bars, model dialog, price overrides (stored in the browser), limits cards, keyboard shortcuts and light and dark themes.
- **Breaking:** `data/fleet-usage.json` is now schema version 2: the same buckets as `data/usage.json` with a machine dimension and each machine's limits. See `docs/fleet-usage-schema.md`. Consumers (opsdeck-api) must be updated before this version is pulled onto a machine running the fleet timer. The collector, `config/fleet.json` and the `usage-tracker-fleet` systemd units keep their names; the collector now runs the new exporter over SSH and is optional.

### Added
- A per-file scan cache for Claude, Codex, Grok and Pi transcripts, ported from T3 Code's usage scan cache. Unchanged files are not parsed again, so a repeat export of a large history takes seconds instead of most of a minute. Cost is computed after the cache, output is identical warm or cold, and `--no-cache` (also `"cache": false` per machine in `config/fleet.json`) skips it.

### Removed
- The OpenClaw session reader, the API-versus-OAuth billing split, the subscription ROI panel, the five design variants and the drag-and-drop trajectory loader. The old version stays in git history.

## [0.1.0] - 2026-06-27

### Added
- Machine-wide OpenClaw session transcript exporter.
- Codex CLI rollout ingestion.
- OAuth provider cost flag and atomic exporter output writes.
- UI charts that include API and OAuth costs across variants, plus session grouping by `sessionId`.

### Fixed
- Corrected provider labels, UI render hardening, timestamp formatting, timezone math, subscription ROI calendar spans, and ROI proration over the actual query window.

### Changed
- Refreshed the README for the OpenClaw session analytics flow and standardized repo metadata for the Escoffier Labs org.
