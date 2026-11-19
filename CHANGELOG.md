# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

### Changed
- Rewrote the project as a standalone clone of T3 Code's Usage page (Cost, Tokens and Limits) that needs only Python 3 and a browser. Credit and license text: `NOTICE`.
- `python3 bin/usage_tracker.py` exports, serves the page on 127.0.0.1 and opens the browser; the Refresh button re-runs the scan through a local endpoint.
- Providers: Claude Code, Codex, OpenCode, Grok, Antigravity and Pi, with T3's file locations and de-duplication rules. Missing directories are skipped silently.
- Costs use LiteLLM prices with a 24 hour cache, fall back to the cached copy offline, then to unknown. Models with no price show as Unpriced.
- Claude and Codex limits are read with the logins those tools already saved, never written to disk, and sent only to the provider's own host.
- `index.html` is a single file with hourly and daily windows, per-provider chart, share bars, model dialog, price overrides (stored in the browser), limits cards, keyboard shortcuts and light and dark themes.
- **Breaking:** `data/fleet-usage.json` is now schema version 2: the same buckets as `data/usage.json` with a machine dimension and each machine's limits. See `docs/fleet-usage-schema.md`. Consumers (opsdeck-api) must be updated before this version is pulled onto a machine running the fleet timer. The collector, `config/fleet.json` and the `usage-tracker-fleet` systemd units keep their names; the collector now runs the new exporter over SSH and is optional.

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
