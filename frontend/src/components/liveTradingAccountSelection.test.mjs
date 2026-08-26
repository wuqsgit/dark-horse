import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import test from 'node:test';

import * as tradingSelection from './liveTradingAccountSelection.js';

const {
  findSelectedAccount,
  normalizeSelectedAccount,
  tradingEnvironmentDisplay,
} = tradingSelection;

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

test('environment display degrades stale and failed snapshots', () => {
  assert.deepEqual(
    tradingEnvironmentDisplay({ environment_status: 'PROD LIVE', fresh: true, last_error: null }),
    { label: 'PROD LIVE', degraded: false },
  );
  assert.deepEqual(
    tradingEnvironmentDisplay({ environment_status: 'PROD LIVE', fresh: false, last_error: null }),
    { label: 'LIVE STALE', degraded: true },
  );
  assert.deepEqual(
    tradingEnvironmentDisplay({ environment_status: 'TESTNET LIVE', fresh: true, last_error: 'timeout' }),
    { label: 'LIVE DEGRADED', degraded: true },
  );
});

test('strategy source label uses distinct row sources and supports mixed history', () => {
  assert.equal(typeof tradingSelection.strategySourcesLabel, 'function');
  assert.equal(tradingSelection.strategySourcesLabel(['normal', 'normal']), '普通策略');
  assert.equal(tradingSelection.strategySourcesLabel(['normal', 'alpha']), '普通策略 / Alpha 策略');
  assert.equal(tradingSelection.strategySourcesLabel(['alpha']), 'Alpha 策略');
  assert.equal(tradingSelection.strategySourcesLabel([]), '-');
});

test('history cursor transitions can recover from a failed next cursor', () => {
  assert.equal(typeof tradingSelection.createHistoryNavigation, 'function');
  assert.equal(typeof tradingSelection.advanceHistoryNavigation, 'function');
  assert.equal(typeof tradingSelection.retreatHistoryNavigation, 'function');
  const firstPage = tradingSelection.createHistoryNavigation('account-3');
  const failedNextPage = tradingSelection.advanceHistoryNavigation(firstPage, 'bad-cursor');

  assert.deepEqual(failedNextPage, {
    queryKey: 'account-3',
    cursor: 'bad-cursor',
    historyCursorStack: [null],
  });
  assert.deepEqual(
    tradingSelection.advanceHistoryNavigation(failedNextPage, 'bad-cursor'),
    failedNextPage,
  );
  assert.deepEqual(tradingSelection.retreatHistoryNavigation(failedNextPage), firstPage);
});

test('empty account-scoped state clears history decisions errors and loading', () => {
  assert.equal(typeof tradingSelection.emptyAccountScopedTradingState, 'function');
  assert.deepEqual(tradingSelection.emptyAccountScopedTradingState('no-account'), {
    historyPage: { items: [], next_cursor: null, stats: {} },
    historyNavigation: {
      queryKey: 'no-account',
      cursor: null,
      historyCursorStack: [],
    },
    historyError: null,
    historyLoading: false,
    decisionsData: null,
    decisionsError: null,
    decisionsLoading: false,
  });
});

test('latest request generation rejects stale completion and survives overlap attempts', () => {
  assert.equal(typeof tradingSelection.startLatestRequest, 'function');
  assert.equal(typeof tradingSelection.isLatestRequest, 'function');
  assert.equal(typeof tradingSelection.finishLatestRequest, 'function');
  assert.equal(typeof tradingSelection.invalidateLatestRequest, 'function');

  const first = tradingSelection.startLatestRequest({ generation: 0, inFlight: false });
  const second = tradingSelection.startLatestRequest(first);
  assert.equal(tradingSelection.isLatestRequest(second, first.generation), false);
  assert.equal(tradingSelection.isLatestRequest(second, second.generation), true);
  assert.deepEqual(tradingSelection.finishLatestRequest(second, first.generation), second);
  assert.equal(tradingSelection.finishLatestRequest(second, second.generation).inFlight, false);

  const invalidated = tradingSelection.invalidateLatestRequest(second);
  assert.equal(invalidated.inFlight, false);
  assert.equal(tradingSelection.isLatestRequest(invalidated, second.generation), false);
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

test('components wire the tested environment and strategy source helpers', () => {
  assert.match(environmentStatusSource, /tradingEnvironmentDisplay\(data\)/);
  assert.match(environmentStatusSource, /display\.degraded/);
  assert.match(liveTradingSource, /strategySourcesLabel\(t\.strategy_sources\)/);
  assert.doesNotMatch(
    liveTradingSource,
    /sourceText\(t\.strategy_source \|\| \(tradeFilter/,
  );
});

test('live trading polls only account snapshots and runtime status', () => {
  const pollBody = liveTradingSource.match(/const pollLiveData = [^{]*\{([\s\S]*?)\n\s*\};/);
  assert.ok(pollBody, 'expected a dedicated pollLiveData callback');
  assert.match(pollBody[1], /fetchTradingAccountsStatus\(/);
  assert.match(pollBody[1], /loadRuntimeStatus\(\)/);
  assert.doesNotMatch(
    pollBody[1],
    /fetchTradingAccounts\(|fetchTradingHistory\(|fetchTradingDecisions\(|fetchTradingRuntimeStatus\(/,
  );
  assert.match(liveTradingSource, /setInterval\(pollLiveData, 30000\)/);
});

test('live trading settles initial resources independently and snapshot controls loading', () => {
  assert.doesNotMatch(liveTradingSource, /Promise\.allSettled\(\[initialAccounts/);
  assert.match(
    liveTradingSource,
    /fetchTradingAccountsStatus\(\)[\s\S]*?\.finally\(\(\) => \{ if \(active\) setLoading\(false\); \}\)/,
  );
  assert.match(liveTradingSource, /const loadRuntimeStatus = useCallback/);
  assert.match(liveTradingSource, /runtimeRequestRef\.current\.inFlight/);
  assert.match(liveTradingSource, /isLatestRequest\(/);
  assert.match(liveTradingSource, /invalidateLatestRequest\(/);
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

test('live trading keeps history recovery controls visible and clears null account state', () => {
  assert.match(liveTradingSource, /historyError &&/);
  assert.match(liveTradingSource, /retryHistoryPage/);
  assert.doesNotMatch(liveTradingSource, /: historyError \? \(/);
  assert.match(liveTradingSource, /emptyAccountScopedTradingState\(historyQueryKey\)/);
  assert.match(liveTradingSource, /setHistoryPage\(emptyState\.historyPage\)/);
  assert.match(liveTradingSource, /setDecisionsData\(emptyState\.decisionsData\)/);
});
