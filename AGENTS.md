# AGENTS.md

## Project
- **Name:** Usage Tracker
- **What it is:** a standalone clone of T3 Code's Usage page (Cost, Tokens, Limits) for people who do not use T3 Code. Python 3 standard library plus a browser, nothing else.
- **Run it:** `python3 bin/usage_tracker.py` (exports, serves on 127.0.0.1, opens the browser).
- **Refresh data without serving:** `python3 bin/export_usage.py`

## Architecture
- `usage_tracker/` is the Python package (stdlib only):
  - `readers/` one module per provider (claude, codex, opencode, grok, antigravity, pi), ported from T3's readers. `transcripts.py` is the shared JSONL walker.
  - `scan.py` runs the readers and does cross-file dedupe; `aggregate.py` builds hourly and daily buckets; `pricing.py` handles LiteLLM rates and the cache. `scan_cache.py` caches parsed per-file records (Claude, Codex, Grok, Pi transcripts) in the same cache directory, ported from T3's usageScanCache.ts: size, mtime and inode must match, changed files are re-read whole, cost is applied after the cache, and `--no-cache` bypasses it. Warm and cold output must stay byte-identical (`tests/test_scan_cache.py`).
  - `limits.py` reads Claude and Codex limits with the saved logins; `export.py` writes `data/usage.json`; `server.py` is the launcher.
- `index.html` is the whole page: single file, vanilla JS, inline SVG, no CDN or external asset. The pure logic sits between `// BEGIN USAGE MODEL` and `// END USAGE MODEL` and is tested by `tests/frontend.test.js` through `vm`. Keep DOM code out of that block.
- `data/usage.json` is generated and git-ignored. Its shape is the bucket format in `docs/fleet-usage-schema.md`.
- Optional fleet mode: `bin/collect_fleet_usage.py` runs the exporter on several machines (local or SSH) and writes `data/fleet-usage.json` (schema in `docs/fleet-usage-schema.md`). It is driven by `config/fleet.json` and the `systemd/usage-tracker-fleet.*` units, which keep their names. The single-machine flow must never depend on it. `fleet-usage.json` is consumed by opsdeck-api, so a schema change needs a lockstep consumer change.
- Price overrides and UI preferences live in the browser's `localStorage`, wrapped in try/catch.

## Build & Verify
```bash
python -m compileall -q bin scripts tests usage_tracker
python -m pytest tests/ -q
python scripts/check_frontend.py index.html
node tests/frontend.test.js
python scripts/scan_repo.py .
```
After a UI change, serve the page and check Cost, Tokens and Limits in light and dark.

## Key Rules
- Loopback-only local server, stdlib only. The server binds to 127.0.0.1, serves only the page and `data/usage.json`, checks the `Host` header, and needs the `X-Usage-Tracker` header on `POST /api/refresh`. Do not add a dependency (Python or npm) or a listener on another interface.
- Credentials (Claude and Codex logins) are read in memory, sent only to that provider's own host over HTTPS with redirects refused, and never written to output, logs or error text.
- Keep `index.html` single-file and offline. Never put demo or mock data in it.
- Missing provider directories are skipped silently. Unknown model prices mean cost unknown (`unpriced`), never zero.
- When T3 Code changes a reader or the pricing rules, port the change rather than inventing a different rule. Credit stays in `NOTICE`.

## Style Guide
- No em dashes. Ever.

## Git Rules
- Use conventional commits.
- Never add `Co-Authored-By` lines.
- Never mention AI tools or vendors in commit messages.
