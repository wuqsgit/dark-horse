import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import test from 'node:test';

import { normalizeSelectedAccount, findSelectedAccount } from './liveTradingAccountSelection.js';

const liveTradingSource = readFileSync(new URL('./LiveTrading.jsx', import.meta.url), 'utf8');
const environmentStatusSource = readFileSync(
  new URL('./TradingEnvironmentStatus.jsx', import.meta.url),
  'utf8',
);

const accounts = [
  { account_id: 3, account_name: '账户A' },
  { account_id: 8, account_name: '账户B' },
];

test('normalizes all-account selection to the first concrete account', () => {
  assert.equal(normalizeSelectedAccount('all', accounts), 3);
});

test('keeps existing concrete account selection', () => {
  assert.equal(normalizeSelectedAccount(8, accounts), 8);
});

test('finds only concrete account rows', () => {
  assert.equal(findSelectedAccount('all', accounts), null);
  assert.equal(findSelectedAccount(3, accounts).account_name, '账户A');
});

test('live trading consumers use only the split trading data client', () => {
  for (const clientFunction of [
    'fetchTradingAccounts',
    'fetchTradingAccountsStatus',
    'fetchTradingHistory',
    'fetchTradingDecisions',
    'fetchTradingRuntimeStatus',
  ]) {
    assert.match(liveTradingSource, new RegExp(`\\b${clientFunction}\\(`));
  }
  assert.match(liveTradingSource, /from ['"]\.\.\/api\/tradingData['"]/);
  assert.match(environmentStatusSource, /from ['"]\.\.\/api\/tradingData['"]/);
  assert.doesNotMatch(liveTradingSource, /tradingAccountsStatus|\/api\/trading\/status/);
  assert.doesNotMatch(environmentStatusSource, /tradingAccountsStatus|\/api\/trading\/status/);
});

test('live trading polls only account snapshots and runtime status', () => {
  const pollBody = liveTradingSource.match(/const pollLiveData = [^{]*\{([\s\S]*?)\n\s*\};/);
  assert.ok(pollBody, 'expected a dedicated pollLiveData callback');
  assert.match(pollBody[1], /fetchTradingAccountsStatus\(/);
  assert.match(pollBody[1], /fetchTradingRuntimeStatus\(/);
  assert.doesNotMatch(
    pollBody[1],
    /fetchTradingAccounts\(|fetchTradingHistory\(|fetchTradingDecisions\(/,
  );
  assert.match(liveTradingSource, /setInterval\(pollLiveData, 30000\)/);
});

test('live trading clears a fatal snapshot error after a successful refresh', () => {
  const applySnapshotBody = liveTradingSource.match(
    /const applyAccountSnapshot = [^{]*\{([\s\S]*?)\n\s*\}, \[\]\);/,
  );
  assert.ok(applySnapshotBody, 'expected an applyAccountSnapshot callback');
  assert.match(applySnapshotBody[1], /setError\(null\)/);
});

test('live trading cancels account-scoped history and decision loads', () => {
  assert.ok(
    (liveTradingSource.match(/new AbortController\(\)/g) || []).length >= 2,
    'expected independently cancellable history and decision requests',
  );
  assert.match(
    liveTradingSource,
    /fetchTradingHistory\(selectedAccount,[\s\S]*?\{ signal: controller\.signal \}/,
  );
  assert.match(
    liveTradingSource,
    /fetchTradingDecisions\(selectedAccount, \{ signal: controller\.signal \}\)/,
  );
  assert.ok(
    (liveTradingSource.match(/controller\.abort\(\)/g) || []).length >= 2,
    'expected both account-scoped effects to abort on cleanup',
  );
});

test('live trading sends history filters before rendering backend rows unchanged', () => {
  assert.match(liveTradingSource, /source: tradeFilter === 'all' \? undefined : tradeFilter/);
  assert.match(liveTradingSource, /symbol: tradeSymbol \|\| undefined/);
  assert.match(liveTradingSource, /direction: tradeDirection === 'all' \? undefined : tradeDirection/);
  assert.match(liveTradingSource, /historyPage\.items\.map\(/);
  assert.match(liveTradingSource, /historyPage\.stats/);
  assert.match(liveTradingSource, /historyPage\.next_cursor/);
  assert.match(liveTradingSource, /historyCursorStack/);
  assert.match(liveTradingSource, /t\.position_count > 1/);
  assert.doesNotMatch(liveTradingSource, /t\.close_count > 1/);
  assert.doesNotMatch(liveTradingSource, /\.reduce\(|new Map\(|\.slice\(/);
});
