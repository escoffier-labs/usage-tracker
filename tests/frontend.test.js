#!/usr/bin/env node
'use strict';

const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');

const html = fs.readFileSync(new URL('../index.html', `file://${__filename}`), 'utf8');
const match = html.match(/\/\/ BEGIN SESSION VIEW MODEL([\s\S]*?)\/\/ END SESSION VIEW MODEL/);
assert.ok(match, 'index.html exposes the session view-model helpers');

const context = {};
vm.runInNewContext(`${match[1]}; this.buildSessionViewModels = buildSessionViewModels;`, context);

const groups = [
  {
    sessionId: 'abcdef123456',
    agent: 'writer',
    firstTs: '2026-07-09T10:00:00Z',
    cost: 1,
    tokens: 20,
    apiCost: 1,
    oauthCost: 0,
    models: ['alpha', 'beta'],
    calls: [{ provider: 'openai', model: 'alpha', usage: { totalTokens: 20, cost: { total: 1 } } }],
  },
  {
    sessionId: 'second',
    sessionKey: 'Named session',
    agent: 'coder',
    firstTs: '2026-07-10T10:00:00Z',
    cost: 3,
    tokens: 30,
    apiCost: 0,
    oauthCost: 3,
    models: ['gamma'],
  },
];

const sessions = context.buildSessionViewModels(groups, 'cost', 'desc');
assert.equal(sessions[0].displayName, 'Named session');
assert.equal(sessions[0].callCount, 0);
assert.equal(sessions[0].firstProvider, 'openclaw');
assert.equal(sessions[1].displayName, 'abcdef12');
assert.equal(sessions[1].modelLabel, 'alpha +1');
assert.equal(sessions[1].calls[0].tokens, 20);
assert.equal(sessions[1].calls[0].cost, 1);
assert.equal(sessions[1].calls[0].billingLabel, 'openai');

assert.ok(!/fonts\.(?:googleapis|gstatic)\.com/.test(html), 'index.html has no remote Google Fonts requests');
console.log('frontend helpers: 10 assertions passed');
