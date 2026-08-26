export function normalizeSelectedAccount(selectedAccount, accounts = []) {
  if (!accounts.length) return null;
  const exists = accounts.some((account) => String(account.account_id) === String(selectedAccount));
  return exists ? selectedAccount : accounts[0].account_id;
}

export function findSelectedAccount(selectedAccount, accounts = []) {
  if (selectedAccount == null || selectedAccount === 'all') return null;
  return accounts.find((account) => String(account.account_id) === String(selectedAccount)) || null;
}

export function tradingEnvironmentDisplay(snapshot = {}) {
  if (snapshot.last_error) return { label: 'LIVE DEGRADED', degraded: true };
  if (snapshot.fresh === false) return { label: 'LIVE STALE', degraded: true };
  const label = snapshot.environment_status || 'LIVE DEGRADED';
  return { label, degraded: label === 'LIVE DEGRADED' };
}

export function strategySourcesLabel(strategySources = []) {
  const distinctSources = [];
  for (const value of strategySources || []) {
    const source = String(value || '').trim().toLowerCase();
    if (source && !distinctSources.includes(source)) distinctSources.push(source);
  }
  if (distinctSources.length === 0) return '-';
  return distinctSources.map((source) => {
    if (source === 'normal') return '普通策略';
    if (source === 'alpha') return 'Alpha 策略';
    return source;
  }).join(' / ');
}

export function createHistoryNavigation(queryKey = null) {
  return { queryKey, cursor: null, historyCursorStack: [] };
}

export function advanceHistoryNavigation(navigation, nextCursor) {
  if (!nextCursor || String(navigation.cursor) === String(nextCursor)) return navigation;
  return {
    queryKey: navigation.queryKey,
    cursor: nextCursor,
    historyCursorStack: [
      ...(navigation.historyCursorStack || []),
      navigation.cursor ?? null,
    ],
  };
}

export function retreatHistoryNavigation(navigation) {
  if (!navigation.historyCursorStack?.length) return navigation;
  const previousCursors = [...navigation.historyCursorStack];
  const cursor = previousCursors.pop() ?? null;
  return {
    queryKey: navigation.queryKey,
    cursor,
    historyCursorStack: previousCursors,
  };
}

export function emptyAccountScopedTradingState(queryKey = null) {
  return {
    historyPage: { items: [], next_cursor: null, stats: {} },
    historyNavigation: createHistoryNavigation(queryKey),
    historyError: null,
    historyLoading: false,
    decisionsData: null,
    decisionsError: null,
    decisionsLoading: false,
  };
}

export function startLatestRequest(state = { generation: 0, inFlight: false }) {
  return {
    ...state,
    generation: Number(state.generation || 0) + 1,
    inFlight: true,
  };
}

export function isLatestRequest(state, generation) {
  return Number(state?.generation) === Number(generation);
}

export function finishLatestRequest(state, generation) {
  if (!isLatestRequest(state, generation)) return state;
  return { ...state, inFlight: false };
}

export function invalidateLatestRequest(state = { generation: 0, inFlight: false }) {
  return {
    ...state,
    generation: Number(state.generation || 0) + 1,
    inFlight: false,
  };
}
