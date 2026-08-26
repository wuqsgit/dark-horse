import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import test from 'node:test';

import * as tradingSelection from './liveTradingAccountSelection.js';

const {
  accountSnapshotAvailability,
  formatHistoryMoney,
  formatHistoryValue,
  findSelectedAccount,
  normalizeHistoryPage,
  normalizeSelectedAccount,
  reconciliationStatusLabel,
  tradingEnvironmentDisplay,
} = tradingSelection;

const liveTradingSource = readFileSync(new URL('./LiveTrading.jsx', import.meta.url), 'utf8');
const environmentStatusSource = readFileSync(
  new URL('./TradingEnvironmentStatus.jsx', import.meta.url),
  'utf8',
);
const stylesSource = readFileSync(new URL('../styles.css', import.meta.url), 'utf8');

function deferred() {
  let resolve;
  let reject;
  const promise = new Promise((resolvePromise, rejectPromise) => {
    resolve = resolvePromise;
    reject = rejectPromise;
  });
  return { promise, resolve, reject };
}

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

test('uses configured account identifiers for selection even when snapshots are absent', () => {
  const configuredAccounts = [
    { id: 3, name: '账户A', enabled: true },
    { id: 8, name: '已禁用账户', enabled: false },
  ];

  assert.equal(normalizeSelectedAccount(8, configuredAccounts), 8);
  assert.equal(findSelectedAccount(8, configuredAccounts).enabled, false);
  assert.equal(normalizeSelectedAccount(null, configuredAccounts), 3);
});

test('marks missing and stale account snapshots without removing configured accounts', () => {
  const configuredAccount = { id: 8, name: '账户B', enabled: false };

  assert.equal(
    accountSnapshotAvailability(configuredAccount, { accounts: [], fresh: true }),
    'unavailable',
  );
  assert.equal(
    accountSnapshotAvailability(
      configuredAccount,
      { accounts: [{ account_id: 8, status: 'ok' }], fresh: false },
    ),
    'stale',
  );
  assert.equal(
    accountSnapshotAvailability(
      configuredAccount,
      { accounts: [{ account_id: 8, status: 'ok' }], fresh: true },
      '账户快照刷新失败：timeout',
    ),
    'stale',
  );
  assert.equal(
    accountSnapshotAvailability(
      configuredAccount,
      { accounts: [{ account_id: 8, status: 'ok' }], fresh: true },
    ),
    'available',
  );
});

test('retains history reconciliation metadata and does not invent unknown numbers', () => {
  const response = {
    items: [{
      symbol: 'BTCUSDT',
      side: 'LONG',
      quantity: null,
      entry_price: null,
      pnl: null,
      reconcile_status: 'mismatch',
    }],
    next_cursor: 'cursor-2',
    stats: { total_cycles: 1 },
    reconcile_status: 'incomplete',
  };

  const page = normalizeHistoryPage(response);
  assert.equal(page.reconcile_status, 'incomplete');
  assert.strictEqual(page.items[0], response.items[0]);
  assert.equal(page.items[0].reconcile_status, 'mismatch');
  assert.equal(reconciliationStatusLabel(page.reconcile_status), '不完整');
  assert.equal(reconciliationStatusLabel(page.items[0].reconcile_status), '对账不匹配');
  assert.equal(formatHistoryValue(response.items[0].quantity, 6), '-');
  assert.equal(formatHistoryMoney(response.items[0].entry_price, 4), '-');
  assert.equal(formatHistoryMoney(response.items[0].pnl, 2, { signed: true }), '-');
  assert.equal(formatHistoryValue(0, 6), '0.000000');
  assert.equal(formatHistoryMoney(0, 2, { signed: true }), '+$0.00');
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

test('degraded environment status removes the green container and live-dot treatment', () => {
  assert.match(environmentStatusSource, /terminal-status trading-env-status/);

  const containerRule = stylesSource.match(/\.trading-env-status\.degraded\s*\{([^}]*)\}/);
  assert.ok(containerRule, 'expected an explicit degraded environment container rule');
  assert.match(containerRule[1], /color:\s*#fcd34d;/);
  assert.match(containerRule[1], /border-color:\s*#a16207;/);
  assert.match(containerRule[1], /background:\s*rgba\(69, 45, 12, 0\.85\);/);

  const dotRule = stylesSource.match(/\.trading-env-status\.degraded \.live-dot\s*\{([^}]*)\}/);
  assert.ok(dotRule, 'expected a degraded environment live-dot override');
  assert.match(dotRule[1], /background:\s*#f59e0b;/);
  assert.match(dotRule[1], /box-shadow:\s*none;/);
  assert.match(dotRule[1], /animation:\s*none;/);
  assert.doesNotMatch(dotRule[1], /var\(--green\)|pulseDot/);
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

test('populated account-scoped state resets immediately when the account becomes null', () => {
  assert.equal(typeof tradingSelection.resetAccountScopedTradingState, 'function');
  const populated = {
    historyPage: { items: [{ id: 1 }], next_cursor: 'next', stats: { total: 8 } },
    historyNavigation: {
      queryKey: 'account-3',
      cursor: 'cursor-2',
      historyCursorStack: [null, 'cursor-1'],
    },
    historyError: 'old error',
    historyLoading: true,
    decisionsData: { recent: [{ id: 2 }] },
    decisionsError: 'decision error',
    decisionsLoading: true,
  };

  assert.deepEqual(tradingSelection.resetAccountScopedTradingState(populated, 'no-account'), {
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

test('initial resources invoke snapshot callbacks without waiting for deferred peers', async () => {
  assert.equal(typeof tradingSelection.settleIndependentLoads, 'function');
  const accountsLoad = deferred();
  const snapshotLoad = deferred();
  const runtimeLoad = deferred();
  const calls = [];

  const loads = tradingSelection.settleIndependentLoads({
    accounts: () => accountsLoad.promise,
    snapshot: () => snapshotLoad.promise,
    runtime: () => runtimeLoad.promise,
  }, {
    accounts: { onFulfilled: () => calls.push('accounts') },
    snapshot: {
      onFulfilled: (value) => calls.push(`snapshot:${value.fresh}`),
      onSettled: () => calls.push('snapshot-settled'),
    },
    runtime: { onFulfilled: () => calls.push('runtime') },
  });

  snapshotLoad.resolve({ fresh: true });
  await loads.snapshot;
  assert.deepEqual(calls, ['snapshot:true', 'snapshot-settled']);

  accountsLoad.resolve({ accounts: [] });
  runtimeLoad.resolve({ accounts: [] });
  await Promise.all([loads.accounts, loads.runtime]);
  assert.deepEqual(calls, ['snapshot:true', 'snapshot-settled', 'accounts', 'runtime']);
});

test('runtime controller suppresses overlap and stale completion cannot overwrite', async () => {
  assert.equal(typeof tradingSelection.createSingleFlightRequest, 'function');
  const requests = [];
  const applied = [];
  const controller = tradingSelection.createSingleFlightRequest(({ signal }) => {
    const request = deferred();
    requests.push({ ...request, signal });
    return request.promise;
  });

  const first = controller.run({ onSuccess: (value) => applied.push(value) });
  const suppressed = controller.run({ onSuccess: () => applied.push('overlap') });
  assert.strictEqual(suppressed, first);
  assert.equal(requests.length, 1);
  assert.equal(controller.inFlight, true);

  controller.invalidate();
  assert.equal(requests[0].signal.aborted, true);
  const latest = controller.run({ onSuccess: (value) => applied.push(value) });
  assert.equal(requests.length, 2);

  requests[0].resolve('stale');
  await first;
  assert.deepEqual(applied, []);
  assert.equal(controller.inFlight, true);

  requests[1].resolve('latest');
  await latest;
  assert.deepEqual(applied, ['latest']);
  assert.equal(controller.inFlight, false);
});

test('history failure transition retains rows stats and cursor for retry recovery', () => {
  assert.equal(typeof tradingSelection.historyFailureTransition, 'function');
  const historyPage = {
    items: [{ trade_id: 7 }],
    next_cursor: 'cursor-3',
    stats: { total_trades: 22, total_pnl: 18.5 },
  };
  const historyNavigation = {
    queryKey: 'account-3',
    cursor: 'cursor-2',
    historyCursorStack: [null, 'cursor-1'],
  };
  const failed = tradingSelection.historyFailureTransition({
    historyPage,
    historyNavigation,
    historyError: null,
    historyLoading: true,
    retryable: false,
  }, '历史交易加载失败：timeout');

  assert.strictEqual(failed.historyPage, historyPage);
  assert.strictEqual(failed.historyNavigation, historyNavigation);
  assert.equal(failed.historyError, '历史交易加载失败：timeout');
  assert.equal(failed.historyLoading, false);
  assert.equal(failed.retryable, true);
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
  assert.match(liveTradingSource, /settleIndependentLoads\(/);
  assert.match(liveTradingSource, /createSingleFlightRequest\(/);
  assert.match(liveTradingSource, /historyFailureTransition\(/);
  assert.match(liveTradingSource, /resetAccountScopedTradingState\(/);
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
  assert.match(liveTradingSource, /settleIndependentLoads\(\{/);
  assert.match(liveTradingSource, /snapshot:[\s\S]*?onSettled:[\s\S]*?setLoading\(false\)/);
  assert.match(liveTradingSource, /runtimeControllerRef\.current\.run\(/);
  assert.match(liveTradingSource, /runtimeControllerRef\.current\.invalidate\(\)/);
});

test('live trading clears a fatal snapshot error after a successful refresh', () => {
  const applySnapshotBody = liveTradingSource.match(
    /const applyAccountSnapshot = [^{]*\{([\s\S]*?)\n\s*\}, \[\]\);/,
  );
  assert.ok(applySnapshotBody, 'expected an applyAccountSnapshot callback');
  assert.match(applySnapshotBody[1], /setError\(null\)/);
});

test('live trading keeps configured accounts available when the initial snapshot fails', () => {
  const initialSnapshotFailure = liveTradingSource.match(
    /snapshot:\s*\{[\s\S]*?onRejected:\s*\(requestError\)\s*=>\s*\{([\s\S]*?)\n\s*\},[\s\S]*?onSettled:/,
  );
  assert.ok(initialSnapshotFailure, 'expected an initial snapshot failure handler');
  assert.match(initialSnapshotFailure[1], /setSnapshotWarning\(/);
  assert.doesNotMatch(initialSnapshotFailure[1], /setError\(/);
  assert.match(liveTradingSource, /normalizeSelectedAccount\(selectedAccount, accountConfigs\)/);
  assert.match(liveTradingSource, /accountConfigs\.map\(\(account\)\s*=>/);
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
  assert.match(liveTradingSource, /resetAccountScopedTradingState\(/);
  assert.match(liveTradingSource, /setHistoryPage\(emptyState\.historyPage\)/);
  assert.match(liveTradingSource, /setDecisionsData\(emptyState\.decisionsData\)/);
});
