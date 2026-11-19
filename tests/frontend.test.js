#!/usr/bin/env node
'use strict';

// Runs the page's pure logic block (formatting, windows, merging, price overrides, limits
// presentation, chart math) in a vm context and checks it against known values.

const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');

const html = fs.readFileSync(path.join(__dirname, '..', 'index.html'), 'utf8');
const match = html.match(/\/\/ BEGIN USAGE MODEL([\s\S]*?)\/\/ END USAGE MODEL/);
assert.ok(match, 'index.html exposes the USAGE MODEL block');

const names = [
  'formatUsd', 'formatTokens', 'formatPercent', 'formatDayShort', 'enumerateDays', 'makeWindow', 'mergeUsage',
  'applyOverride', 'parsePriceForm', 'remainingPercent', 'paceOf', 'paceRemainingPercent', 'formatResetsIn',
  'formatDuration', 'niceScale', 'curvePath', 'sourceMessages', 'isModelCostUnknown', 'cacheHitRate',
  'costPerMillionTokens', 'sortModelsByTokens', 'modelShare', 'costTypeSegments', 'formatHourShort',
  'formatRelativeHourShort', 'buildChart', 'PROVIDER_ORDER', 'providersWithUsage',
];
const context = {};
vm.runInNewContext(`${match[1]}; ${names.map((n) => `this.${n} = ${n};`).join(' ')}`, context);
const m = context;
let checks = 0;
// Values from the vm context have a different Array prototype, so compare as JSON.
const is = (actual, expected, label) => { assert.equal(JSON.stringify(actual), JSON.stringify(expected), label); checks += 1; };
const near = (actual, expected, label) => { assert.ok(Math.abs(actual - expected) < 1e-9, `${label}: ${actual} vs ${expected}`); checks += 1; };

// ---- formatting (T3's usageFormat) ----
is(m.formatUsd(1234.5), '$1,234.50', 'usd');
is(m.formatTokens(999), '999', 'tokens small');
is(m.formatTokens(1500), '1.50K', 'tokens K keeps two digits, as T3 does');
is(m.formatTokens(56_200_000_000), '56.2B', 'tokens B');
is(m.formatTokens(2_000_000), '2M', 'tokens trims .0');
is(m.formatPercent(0), '0.0%', 'percent zero');
is(m.formatPercent(0.00004), '<0.1%', 'percent tiny');
is(m.formatPercent(0.537), '53.7%', 'percent');
is(m.formatDayShort('2026-11-05'), 'Nov 5', 'day short');
is(m.enumerateDays('2026-02-27', '2026-03-02'), ['2026-02-27', '2026-02-28', '2026-03-01', '2026-03-02'], 'days across month');
is(m.formatDuration(90 * 60_000), '1h 30m', 'duration');
is(m.formatDuration(3 * 86_400_000 + 5 * 3_600_000), '3d 5h', 'duration days');

// ---- windows ----
const NOW = Date.parse('2026-11-18T12:34:56Z');
const week = m.makeWindow(7, NOW, 'UTC');
is(week.dayList.length, 7, 'seven days');
is([week.dayList[0], week.dayList[6]], ['2026-11-12', '2026-11-18'], 'week bounds');
const ny = m.makeWindow(1, Date.parse('2026-11-18T03:30:00Z'), 'America/New_York');
is(ny.hours.length, 24, '24 hourly buckets');
is(ny.hours[23], '2026-11-18T03:00:00Z', 'hour floor');
is(ny.untilDay, '2026-11-17', 'today follows the data zone');
const kolkata = m.makeWindow(1, Date.parse('2026-11-18T03:45:00Z'), 'Asia/Kolkata');
is(kolkata.hours[23], '2026-11-18T03:30:00Z', 'half-hour zone floors to the local hour');
is(m.formatHourShort('2026-11-18T15:00:00Z', 'UTC', true), '3 PM', 'hour 12h');
is(m.formatHourShort('2026-11-18T15:00:00Z', 'UTC', false), '15:00', 'hour 24h');
is(m.formatRelativeHourShort('2026-11-17T15:00:00Z', '2026-11-18T10:00:00Z', 'UTC', true), '3 PM yesterday', 'relative');

// ---- merge ----
const base = { speed: 'standard', uncachedInputTokens: 0, cachedInputTokens: 0, cacheCreationTokens: 0, outputTokens: 0, reasoningTokens: 0, costUsd: 0, cacheSavingsUsd: 0, categoryCostUsd: null, speedPremiumUsd: 0, costSource: 'modelPriced', records: 1, unpricedRecords: 0, sessionRefs: [] };
const data = {
  daily: [
    Object.assign({}, base, { day: '2026-11-17', provider: 'claude', model: 'opus', uncachedInputTokens: 1_000_000, cachedInputTokens: 2_000_000, outputTokens: 100_000, costUsd: 10, categoryCostUsd: { input: 5, cacheRead: 1, cacheWrite: 0, output: 4 }, cacheSavingsUsd: 9, sessionRefs: [1, 2] }),
    Object.assign({}, base, { day: '2026-11-17', provider: 'claude', model: 'opus', speed: 'fast', uncachedInputTokens: 1000, costUsd: 4, speedPremiumUsd: 2, sessionRefs: [2, 3] }),
    Object.assign({}, base, { day: '2026-11-18', provider: 'codex', model: 'sol', uncachedInputTokens: 500_000, costUsd: 3, sessionRefs: [10] }),
    Object.assign({}, base, { day: '2026-11-18', provider: 'opencode', model: 'mystery', uncachedInputTokens: 700, costUsd: 0, costSource: 'unpriced', unpricedRecords: 1, sessionRefs: [20] }),
    Object.assign({}, base, { day: '2026-10-01', provider: 'claude', model: 'opus', uncachedInputTokens: 5, costUsd: 99 }),
  ],
  hourly: [],
};
const merged = m.mergeUsage(data, { window: week });
near(merged.costUsd, 17, 'cost excludes buckets outside the window');
is(merged.sessions, 3 + 1 + 1, 'sessions are unions of refs per provider, then summed');
is(merged.models.map((x) => x.model), ['opus', 'sol', 'mystery'], 'models sort by cost');
is(m.isModelCostUnknown(merged.models[2]), true, 'unpriced model is cost unknown');
is(m.modelShare(merged.models[2], 'cost'), null, 'no cost share for an unpriced model');
near(merged.speedCost.fast, 4, 'fast cost');
near(merged.speedCost.premium, 2, 'premium');
near(merged.speedCost.standard, 13, 'standard cost');
near(merged.categoryCost.unsplit, 17 - 10, 'unsplit cost is what the categories do not explain');
near(merged.costQuality.unpricedShare, 0.25, 'unpriced share of records');
is(m.sortModelsByTokens(merged.models)[0].model, 'opus', 'sort by tokens');
is(m.providersWithUsage(merged.providers), ['codex', 'claude', 'opencode'], 'provider order follows T3');
near(m.cacheHitRate(merged.models[0]), 2_000_000 / 3_001_000, 'cache hit rate');
const hidden = m.mergeUsage(data, { window: week, hidden: ['claude'] });
near(hidden.costUsd, 3, 'hidden providers are excluded');

// ---- price overrides ----
const override = { inputCostPerMillionTokens: 1, outputCostPerMillionTokens: 2 };
const priced = m.mergeUsage(data, { window: week, overrides: { opus: override, mystery: override } });
near(priced.costUsd, (1 + 2 + 0.2) + (0.001) + 3 + 0.0007, 'override re-prices from tokens; blank cache price uses input');
is(priced.costQuality.unpricedShare, 0, 'an override makes an unpriced model priced');
near(priced.speedCost.premium, 0, 'an override has no fast premium');
is(m.parsePriceForm({ model: ' m ', inputCostPerMillionTokens: '1,5', outputCostPerMillionTokens: '2' }).price.inputCostPerMillionTokens, 1.5, 'comma decimal');
is(m.parsePriceForm({ model: 'm', inputCostPerMillionTokens: '1' }), null, 'output is required');
is(m.parsePriceForm({ model: 'm', inputCostPerMillionTokens: '-1', outputCostPerMillionTokens: '1' }), null, 'no negatives');
is(m.parsePriceForm({ model: '', inputCostPerMillionTokens: '1', outputCostPerMillionTokens: '1' }), null, 'model required');
is(m.parsePriceForm({ model: 'm', inputCostPerMillionTokens: '1', outputCostPerMillionTokens: '1', cacheReadCostPerMillionTokens: '0' }).price.cacheReadCostPerMillionTokens, 0, 'explicit zero cache price is free');

// ---- limits presentation ----
const win = { id: 'seven_day', usedPercent: 39, windowDurationMins: 10080, resetsAt: new Date(NOW + 3.5 * 86_400_000).toISOString() };
is(m.remainingPercent(win), 61, 'remaining');
is(m.paceRemainingPercent(win, NOW), 50, 'half the window left');
is(m.paceOf(win, NOW), 'under', 'under pace');
is(m.paceOf(Object.assign({}, win, { usedPercent: 90 }), NOW), 'ahead', 'ahead of pace');
is(m.paceOf(Object.assign({}, win, { usedPercent: 52 }), NOW), 'on', 'on pace');
is(m.formatResetsIn(win, NOW), 'resets in 3d 12h', 'resets in');
is(m.formatResetsIn({ usedPercent: 1, resetsAt: new Date(NOW - 1000).toISOString() }, NOW), 'resets now', 'past reset');
is(m.paceOf({ usedPercent: 5 }, NOW), null, 'no pace without a reset time');
is(m.remainingPercent({ usedPercent: 140 }), 0, 'clamped');

// ---- chart math ----
const scale = m.niceScale(3200, 4);
is(scale.max, 4000, 'nice max');
is(scale.ticks, [0, 1000, 2000, 3000, 4000], 'nice ticks');
is(m.niceScale(0, 4), { max: 0, ticks: [0] }, 'empty scale');
const path1 = m.curvePath([{ x: 0, y: 10 }, { x: 10, y: 5 }, { x: 20, y: 5 }, { x: 30, y: 20 }]);
assert.ok(/^M0\.00,10\.00 C/.test(path1) && !/NaN/.test(path1), 'curve path is well formed'); checks += 1;
const chart = m.buildChart(week.dayList, merged.periodMap, 'cost', ['claude', 'codex']);
is(chart.columns.length, 7, 'one column per day');
is(chart.paths.map((p) => p.provider), ['claude', 'codex'], 'series sort by total');
assert.ok(chart.paths.every((p) => !/NaN/.test(p.line)), 'no NaN in series'); checks += 1;

// ---- source warnings ----
is(m.sourceMessages([
  { provider: 'codex', status: 'partial', message: 'x' },
  { provider: 'claude', status: 'partial', message: 'x' },
  { provider: 'pi', status: 'missing', message: 'No transcript directory', malformedRecords: 0 },
  { provider: 'grok', status: 'failed', message: 'y' },
], new Set(['grok'])), ['x'], 'deduplicated, hidden providers skipped, missing is silent');

// ---- page hygiene ----
assert.ok(!/<script[^>]+src=/i.test(html), 'no external scripts'); checks += 1;
assert.ok(!/<link[^>]+rel=["']stylesheet/i.test(html), 'no external stylesheets'); checks += 1;
assert.ok(!/https?:\/\/(?!www\.w3\.org)/.test(html.replace(/\/\/ BEGIN[\s\S]*?\/\/ END USAGE MODEL/, '')), 'no remote URLs besides the SVG namespace'); checks += 1;
assert.ok(!html.includes(String.fromCharCode(0x2014)), 'no em dashes'); checks += 1;
for (const id of ['model-dialog', 'price-dialog', 'refresh-btn', 'period-seg', 'metric-seg', 'provider-menu']) {
  assert.ok(html.includes(`id="${id}"`), `page has #${id}`); checks += 1;
}

console.log(`frontend logic: ${checks} assertions passed`);
