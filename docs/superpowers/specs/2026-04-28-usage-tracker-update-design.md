# Usage Tracker Update - Design

**Date:** 2026-04-28
**Status:** approved, ready for implementation plan

## Context

`solomonneas/usage-tracker` was built 2026-02-09 to compare local-model savings vs. paid API calls. That premise is dead (local 7B/14B models on a 16GB RTX Ada 2000 weren't viable for real coding work), and the app hasn't been used since. The current state:

- Single-file static HTML, deployed to Vercel as a "demo," with seeded fake data
- Hardcoded 3-model catalog (`claude-opus-4-6`, `claude-haiku-4-5`, `gpt-5.2`) - all stale
- Manual-entry form, localStorage persistence, 5 visual themes
- Description claims "OpenClaw session analytics" but the app has no actual OpenClaw hookup

## Goal

Repurpose the tracker into a clean per-session/per-task cost dashboard for OpenClaw work, with a clear split between **real API spend** and **OAuth subscription burn** (Codex Pro, Claude Max, etc.) so the user can see at a glance what each session cost and how much subscription value is being extracted.

## Non-goals

- Real-time streaming / live updating during a session
- Multi-machine sync (this is a single-host tool for automation-host)
- Server-side anything - the AGENTS.md "no backend" rule stays
- Hitting provider billing APIs to reconcile exact charges

## Key insight: OpenClaw already computes per-call cost

OpenClaw writes `*.trajectory.jsonl` files alongside session jsonls at `~/.openclaw/agents/*/sessions/`. Each `model.completed` event carries a fully populated `usage` block including a `cost` object with input/output/cacheRead/cacheWrite/total in USD, **even for OAuth-routed calls** (where the framing is "what this would have cost at API rates"). This means we don't need to maintain our own pricing catalog. The exporter just reads what's already there.

## Architecture

```
~/.openclaw/agents/*/sessions/*.trajectory.jsonl
                  │
                  ▼  (cron or manual, ~5 min cadence)
       bin/export-usage.py  ──►  data/usage.json   (gitignored)
                  │
                  ▼  fetch('./usage.json') on page load
              index.html
                  ▲
                  │  drag-and-drop fallback
       *.trajectory.jsonl  or  usage.json
```

Single static page, no Vercel, no backend. The companion exporter is offline preprocessing, not a service. Page tries `fetch('./usage.json')` first; if it fails (page served from a different host, or fresh clone with no export yet), drops to drag-and-drop. localStorage caches the last successful load so a reopen without the script running shows the previous snapshot.

## Components

### 1. Exporter - `bin/export-usage.py`

Walks every `*.trajectory.jsonl` under `~/.openclaw/agents/*/sessions/`, pulls each `model.completed` event, emits a flat array of normalized records to `data/usage.json` (next to `index.html`).

**Record shape:**

```json
{
  "ts": "2026-04-23T18:48:23Z",
  "agent": "codex-builder",
  "sessionId": "93dec437-2459-4fff-8b65-f126c2bca3db",
  "sessionKey": "agent:codex-builder:main",
  "runId": "93dec437-...",
  "provider": "openai-codex",
  "modelId": "gpt-5.5",
  "modelApi": "openai-codex-responses",
  "billing": "oauth",
  "input": 9043,
  "output": 43,
  "cacheRead": 0,
  "cacheWrite": 0,
  "totalTokens": 9086,
  "costUsd": 0.0465,
  "workspaceDir": "/home/user/.openclaw/workspace/codex-builder"
}
```

Field sourcing: `ts` from event `ts`; `agent` from the parent dir name (`agents/<agent>/sessions/...`); `sessionId`, `sessionKey`, `runId`, `provider`, `modelId`, `modelApi`, `workspaceDir` straight off the event; token + cost fields preferentially from `data.messagesSnapshot[-1].usage` (which has the cost object), falling back to `data.usage` for older events that lack the snapshot.

**Provider → billing classification** (hardcoded in exporter):

| Provider | Billing |
|---|---|
| `openai-codex` | oauth |
| `claude-cli` | oauth |
| `acpx` | oauth |
| `anthropic` | api |
| `openai` | api |
| `google` | api |
| `kimi` | api |
| `deepseek` | api |
| `minimax` | api |
| `zhipu` | api |
| anything else | api (with a warning logged to stderr) |

Note: agents that route through ACPX (e.g., `claude-builder`'s wrapper that exports `ANTHROPIC_MODEL` then execs the Claude Code CLI) will appear with whatever provider OpenClaw records on the trajectory event. The classification is keyed off `provider`, not agent name.

**Run modes:**

- `python3 bin/export-usage.py` - full export, overwrites `data/usage.json`
- `--since 7d` - only events from the last N days (faster incremental refresh)
- `--out PATH` - override output path
- `--agents-dir PATH` - override `~/.openclaw/agents` (for testing)

Skips truncated events (`data.truncated == true`), counts and reports them in stderr summary.

**Auto-refresh (opt-in):** ship a commented-out `bin/usage-tracker-export.timer` and `.service` user-systemd unit in the repo. User runs `systemctl --user enable --now usage-tracker-export.timer` if they want the 5-minute cadence. Off by default.

### 2. UI - `index.html`

Top-down layout:

1. **Filters bar** (top, sticky) - date range picker, agent multi-select, provider multi-select, billing-type toggle (api / oauth / both), theme switcher.

2. **MTD summary card** - two big numbers side by side:
   - **API spend** - real $ for billing=api this period
   - **OAuth value extracted** - sum of notional $ for billing=oauth this period, captioned "equivalent API spend"
   - Sub-stats: total tokens, total calls, distinct sessions, distinct agents.

3. **Per-agent strip** - one row per agent observed, showing calls / tokens / API $ / OAuth $.

4. **Sessions table** - sortable list of distinct `sessionId`s with: started_at, agent, session_key (the `agent:main:discord:...` identifier - usually the most human-readable handle), call count, total tokens, total cost (split). Row click expands a per-call timeline panel for that session.

5. **Per-model breakdown** - bar chart, cost by `modelId`, stacked API + OAuth.

6. **Time-series chart** - daily cost over selected range, stacked API + OAuth, Canvas API.

### 3. Storage

`localStorage` holds:
- The last successfully loaded `usage.json` payload (so reopens work offline)
- UI preferences: theme, last-used filters
- That's it. No demo data. No manual entries (dropped - see below).

## What gets removed

- Demo toggle button and all demo seed data arrays
- The hardcoded 3-model pricing catalog (`claude-opus-4-6` / `claude-haiku-4-5` / `gpt-5.2`)
- Manual-entry form and its handlers (dropped - everything comes from `usage.json`)
- Vercel deployment: `homepageUrl` cleared on the repo, `vercel.json` / `.vercel/` deleted if present, README quick-start replaced
- README framing as a generic "AI provider tracker" - replaced with OpenClaw session analytics framing, demo link removed

## What stays

- Single-file `index.html` with vanilla JS + Canvas API + CSS3 themes
- All 5 visual themes (dark, light, cyberpunk, ocean, forest)
- localStorage persistence (now for last-loaded snapshot + UI prefs only)
- "No backend" rule in `AGENTS.md` - exporter is offline preprocessing, doesn't violate it
- MIT license

## File layout (post-update)

```
usage-tracker/
├── AGENTS.md                       # updated: mention exporter, keep no-backend rule
├── LICENSE
├── README.md                       # rewritten for new flow
├── index.html                      # demo data/toggle/manual-entry stripped, fetch + DnD added
├── bin/
│   ├── export-usage.py             # new
│   ├── usage-tracker-export.service  # new, commented out by default
│   └── usage-tracker-export.timer    # new, commented out by default
├── data/
│   └── .gitkeep                    # data/usage.json itself is gitignored
├── docs/
│   └── superpowers/specs/
│       └── 2026-04-28-usage-tracker-update-design.md   # this file
└── .gitignore                      # add data/usage.json
```

## Testing

- **Exporter:** point at a fixture dir of representative trajectory jsonls (mix of `openai-codex` / `acpx` / `anthropic`, mix of cached / uncached, at least one truncated event to confirm skip behavior). Verify output schema matches spec, billing classification correct, totals match a hand summed reference.
- **UI:** smoke-test in browser with a small `usage.json` (10-20 records covering 2-3 agents, 3-4 models, both billing types). Verify each panel renders, filters narrow correctly, drill-down opens, themes switch cleanly.
- **No backend regression:** confirm `python3 -m http.server` and direct `file://` open both work.

## Amendments (2026-04-28, post initial approval)

### A1. Variants stay (all 5)

Initial design assumed "5 themes" were CSS palette swaps. The existing `index.html` actually implements 5 entirely distinct design **variants** (`#variant-1` through `#variant-5`), each with its own CSS and its own per-component render functions (`renderDailyChart(containerId, data, variant)` etc.). User decided to keep all 5 plumbed with real data and pick a favorite later. Implication: every render function gets updated for the new data shape, but the variant-switcher UI and 5-design structure stay intact.

### A2. Subscription settings stay (per-session manual entry still dropped)

Per-session manual entry form is still dropped (everything per-call comes from `usage.json`). However, the existing **subscription settings panel** (where the user configures plan name + monthly $ cost for things like Codex Pro, Claude Max) stays. Subscription costs cannot be derived from session jsonls and are needed to compute ROI ("you extracted $X of equivalent API value out of $Y of subscription cost"). Storage stays in localStorage. UI is a small inline-edit settings panel, present in each variant.

The MTD summary in each variant now includes a third tile (or equivalent panel position):

1. **API spend** - real $
2. **OAuth value extracted** - notional $
3. **Subscription ROI** - sum(OAuth value) / sum(active subscription monthly costs), shown as a multiplier (e.g. "4.25x") with the underlying numbers

### A3. Removed list updated

The "What gets removed" list now reads:

- Demo toggle button and all demo seed data arrays
- Hardcoded 3-model pricing catalog (replaced by costs computed by OpenClaw and read from trajectory events)
- **Per-session** manual entry form (subscription settings panel stays - see A2)
- Vercel deployment metadata, README quick-start references to the demo URL

## Open questions

None.
