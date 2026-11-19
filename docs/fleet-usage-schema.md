# fleet-usage.json schema (version 2)

`bin/collect_fleet_usage.py` writes `data/fleet-usage.json`. It is optional: the one-command
page (`bin/usage_tracker.py`) never reads or needs it. This document is the contract for
anything that consumes the file, such as OpsDeck.

**Version 2 replaces the version 1 shape.** Version 1 held a `machines[].hours[]` array of
UTC-hour aggregates with an `unknownTokens` field. Version 2 uses the same per-provider and
per-model buckets as `data/usage.json` (the T3 Code Usage model), tagged with the machine.
A consumer should check `schemaVersion === 2` and refuse anything else.

## Top level

| Field | Type | Meaning |
| --- | --- | --- |
| `schemaVersion` | number | `2`. |
| `usageSchemaVersion` | number | Version of the bucket format (`1`), shared with `data/usage.json`. |
| `generatedAt` | string | UTC ISO time the collector finished. |
| `machines` | array | One entry per configured machine, sorted by `machineId`. Metadata and limits, no buckets. |
| `daily` | array | Daily buckets for every machine (the machine dimension is `machineId`). |
| `hourly` | array | Hourly buckets for every machine, covering the most recent 72 hours. |

## `machines[]`

| Field | Type | Meaning |
| --- | --- | --- |
| `machineId` | string | The id from `config/fleet.json`. |
| `status` | `"ok"` \| `"stale"` \| `"error"` | `ok`: collected this run. `stale`: this run failed, the previous successful snapshot is kept and its buckets are still in `daily` and `hourly`. `error`: failed with nothing cached; no buckets. |
| `error` | string or null | Last line of the failure, at most 240 printable characters. Never contains credentials. |
| `generatedAt` | string or null | When that machine's snapshot was taken (for `stale`, when the kept one was). |
| `timeZone` | string or null | Zone used for that machine's `day` values (IANA name, or an offset such as `UTC-05:00`). |
| `windowDays`, `sinceDay`, `untilDay` | number, string, string | The days covered by `daily` (`YYYY-MM-DD` in `timeZone`). |
| `pricing` | object | `{ status: "fresh" \| "cached" \| "unavailable", source, fetchedAt, knownModels }`. With `unavailable`, costs are unknown (see `costSource`). |
| `sources` | array | One entry per transcript source: `{ provider, path, status, scannedFiles, skippedFiles, malformedRecords, distinctSessions, message }`. `status` is `ok`, `missing`, `partial` or `failed`. |
| `limits` | object or null | The machine's limits snapshot, below. `null` when limits were not collected (`"limits": false` in the config) or the machine is in `error`. |

### `limits`

```json
{ "checkedAt": "2026-11-18T08:00:03Z",
  "providers": [
    { "provider": "claude", "checkedAt": "2026-11-18T08:00:03Z", "plan": "Claude Subscription",
      "windows": [
        { "id": "seven_day", "kind": "weekly", "label": "Weekly", "usedPercent": 39.0,
          "windowDurationMins": 10080, "resetsAt": "2026-11-24T17:59:59Z" } ] },
    { "provider": "codex", "checkedAt": "2026-11-18T08:00:03Z",
      "unavailable": { "reason": "probeFailed", "message": "The provider rejected the saved login (HTTP 401). Sign in again." },
      "windows": [] } ] }
```

Only `claude` and `codex` appear, and only when that machine has a saved login for them.
`kind` is `session`, `weekly` or `monthly`; `usedPercent` is 0 to 100; remaining quota is
`100 - usedPercent`. `resetsAt` and `windowDurationMins` are optional. A provider with
`unavailable` has no windows. Credentials are never present.

## Buckets (`daily[]` and `hourly[]`)

Every bucket has `machineId` plus the fields below. Daily buckets are keyed by
`machineId, day, provider, model, speed`; hourly buckets add `hourStart`.

| Field | Type | Meaning |
| --- | --- | --- |
| `machineId` | string | Which machine. |
| `day` | string | `YYYY-MM-DD` in that machine's `timeZone`. Present on hourly buckets too. |
| `hourStart` | string | Hourly only. UTC ISO time of the start of the local wall-clock hour (zones with 30 minute offsets start at `:30`). Comparable across machines. |
| `provider` | string | `claude`, `codex`, `opencode`, `grok`, `antigravity` or `pi`. |
| `model` | string | Model id as the provider recorded it. |
| `speed` | `"standard"` \| `"fast"` \| `"ultrafast"` | Service speed the requests were billed at. |
| `uncachedInputTokens`, `cachedInputTokens`, `cacheCreationTokens`, `outputTokens` | number | The token split. Processed tokens is the sum of these four. |
| `reasoningTokens` | number | A subset of `outputTokens`. Do not add it again. |
| `costUsd` | number | Estimated API-rate cost. `0` with `costSource: "unpriced"` means unknown, not free. |
| `cacheSavingsUsd` | number | What the cached reads would have cost at the input rate, minus what they cost. |
| `categoryCostUsd` | object or null | `{ input, cacheRead, cacheWrite, output }` in USD, or `null` when unpriced. |
| `speedPremiumUsd` | number | The extra cost of `fast` or `ultrafast` over standard rates. `0` for standard. |
| `costSource` | `"modelPriced"` \| `"providerReported"` \| `"unpriced"` | How `costUsd` was determined. |
| `records` | number | Billed requests in the bucket. |
| `unpricedRecords` | number | Requests with no known price. |
| `sessions` | number | Distinct sessions that touched the bucket on that machine. Do not sum across buckets to count sessions: a session spans buckets. |

Costs come from LiteLLM's public price table with a 24 hour cache on each machine, so two
machines can price a model differently for a day after a price change.

## Example

```json
{
  "schemaVersion": 2,
  "usageSchemaVersion": 1,
  "generatedAt": "2026-11-18T08:05:12Z",
  "machines": [
    {
      "machineId": "rocinante",
      "status": "ok",
      "error": null,
      "generatedAt": "2026-11-18T08:05:09Z",
      "timeZone": "America/New_York",
      "windowDays": 31,
      "sinceDay": "2026-10-19",
      "untilDay": "2026-11-18",
      "pricing": { "status": "fresh", "source": "https://raw.githubusercontent.com/BerriAI/litellm/main/model_prices_and_context_window.json", "fetchedAt": "2026-11-18T08:05:01Z", "knownModels": 6229 },
      "sources": [
        { "provider": "claude", "path": "~/.claude/projects", "status": "ok", "scannedFiles": 212, "skippedFiles": 3, "malformedRecords": 0, "distinctSessions": 140, "message": null }
      ],
      "limits": {
        "checkedAt": "2026-11-18T08:05:10Z",
        "providers": [
          { "provider": "claude", "checkedAt": "2026-11-18T08:05:10Z", "plan": "Claude Subscription",
            "windows": [ { "id": "seven_day", "kind": "weekly", "label": "Weekly", "usedPercent": 39.0, "windowDurationMins": 10080, "resetsAt": "2026-11-24T17:59:59Z" } ] }
        ]
      }
    },
    {
      "machineId": "shadowfax",
      "status": "stale",
      "error": "ssh unavailable",
      "generatedAt": "2026-11-18T07:55:04Z",
      "timeZone": "America/New_York",
      "windowDays": 31,
      "sinceDay": "2026-10-19",
      "untilDay": "2026-11-18",
      "pricing": { "status": "cached", "source": "https://raw.githubusercontent.com/BerriAI/litellm/main/model_prices_and_context_window.json", "fetchedAt": "2026-11-17T09:00:00Z", "knownModels": 6229 },
      "sources": [],
      "limits": null
    }
  ],
  "daily": [
    {
      "machineId": "rocinante",
      "day": "2026-11-17",
      "provider": "claude",
      "model": "claude-opus-5-5",
      "speed": "standard",
      "uncachedInputTokens": 120000,
      "cachedInputTokens": 9800000,
      "cacheCreationTokens": 410000,
      "outputTokens": 88000,
      "reasoningTokens": 0,
      "costUsd": 14.82,
      "cacheSavingsUsd": 44.1,
      "categoryCostUsd": { "input": 0.6, "cacheRead": 4.9, "cacheWrite": 2.56, "output": 2.2 },
      "speedPremiumUsd": 0,
      "costSource": "modelPriced",
      "records": 311,
      "unpricedRecords": 0,
      "sessions": 9
    }
  ],
  "hourly": [
    {
      "machineId": "rocinante",
      "day": "2026-11-18",
      "hourStart": "2026-11-18T07:00:00Z",
      "provider": "claude",
      "model": "claude-opus-5-5",
      "speed": "fast",
      "uncachedInputTokens": 1800,
      "cachedInputTokens": 120000,
      "cacheCreationTokens": 6000,
      "outputTokens": 2400,
      "reasoningTokens": 0,
      "costUsd": 0.41,
      "cacheSavingsUsd": 0.5,
      "categoryCostUsd": { "input": 0.02, "cacheRead": 0.12, "cacheWrite": 0.07, "output": 0.2 },
      "speedPremiumUsd": 0.2,
      "costSource": "modelPriced",
      "records": 6,
      "unpricedRecords": 0,
      "sessions": 1
    }
  ]
}
```

## Producing it

`config/fleet.json` lists the machines. Per machine: `machineId`, `mode` (`local` or `ssh`),
`alias` (the SSH host alias), `python` (the remote interpreter, default `python3`), and the
optional `timeZone` (an IANA name, so every machine buckets days the same way), `limits`
(`false` skips the limits request) and `offline` (cached prices only). Top level: `days`
(default 31), `connectTimeoutSeconds` and `processTimeoutSeconds`.

For an SSH machine the collector pipes a small bundle of the exporter to `python3 -` over
`ssh -o BatchMode=yes`. The remote machine needs Python 3.10 or newer and nothing installed.
Transcripts and credentials never leave it: only the exporter's JSON comes back.
