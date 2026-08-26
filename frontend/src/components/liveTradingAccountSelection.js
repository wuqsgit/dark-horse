function accountIdentifier(account) {
  return account?.account_id ?? account?.id;
}

export function normalizeSelectedAccount(selectedAccount, accounts = []) {
  if (!accounts.length) return null;
  const exists = accounts.some((account) => String(accountIdentifier(account)) === String(selectedAccount));
  return exists ? selectedAccount : accountIdentifier(accounts[0]);
}

export function findSelectedAccount(selectedAccount, accounts = []) {
  if (selectedAccount == null || selectedAccount === 'all') return null;
  return accounts.find((account) => String(accountIdentifier(account)) === String(selectedAccount)) || null;
}

export function accountSnapshotAvailability(account, snapshot = {}, snapshotWarning = null) {
  const snapshotAccount = findSelectedAccount(accountIdentifier(account), snapshot?.accounts || []);
  if (!snapshotAccount) return 'unavailable';
  if (snapshotWarning || snapshot?.last_error || snapshot?.fresh === false) return 'stale';
  return 'available';
}

export function normalizeHistoryPage(data) {
  return {
    ...(data || {}),
    items: Array.isArray(data?.items) ? data.items : [],
    next_cursor: data?.next_cursor || null,
    stats: data?.stats || {},
  };
}

function hasKnownHistoryValue(value) {
  return value !== null
    && value !== undefined
    && value !== ''
    && Number.isFinite(Number(value));
}

export function formatHistoryValue(value, digits = 2) {
  return hasKnownHistoryValue(value) ? Number(value).toFixed(digits) : '-';
}

export function formatHistoryMoney(value, digits = 2, { signed = false } = {}) {
  if (!hasKnownHistoryValue(value)) return '-';
  const amount = Number(value);
  return `${signed && amount >= 0 ? '+' : ''}$${amount.toFixed(digits)}`;
}

export function reconciliationStatusLabel(status) {
  const labels = {
    ok: '完整',
    incomplete: '不完整',
    mismatch: '对账不匹配',
  };
  return labels[status] || '完整性未知';
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
  };
}

export function resetAccountScopedTradingState(_currentState, queryKey = null) {
  return emptyAccountScopedTradingState(queryKey);
}

export function historyFailureTransition(state, historyError) {
  return {
    ...state,
    historyError,
    historyLoading: false,
    retryable: true,
  };
}

export function settleIndependentLoads(loaders, handlers = {}) {
  return Object.fromEntries(Object.entries(loaders).map(([key, load]) => {
    let request;
    try {
      request = load();
    } catch (error) {
      request = Promise.reject(error);
    }
    const handler = handlers[key] || {};
    const promise = Promise.resolve(request)
      .then(handler.onFulfilled, handler.onRejected)
      .finally(() => handler.onSettled?.());
    return [key, promise];
  }));
}

export function createSingleFlightRequest(request) {
  let generation = 0;
  let activeRequest = null;

  return {
    run(handlers = {}) {
      if (activeRequest) return activeRequest.promise;

      const requestGeneration = ++generation;
      const controller = new AbortController();
      let pendingRequest;
      try {
        pendingRequest = request({ signal: controller.signal });
      } catch (error) {
        pendingRequest = Promise.reject(error);
      }

      const promise = Promise.resolve(pendingRequest)
        .then(
          (value) => {
            if (generation === requestGeneration) handlers.onSuccess?.(value);
            return value;
          },
          (error) => {
            if (generation === requestGeneration && error?.name !== 'AbortError') {
              handlers.onError?.(error);
            }
            return undefined;
          },
        )
        .finally(() => {
          if (activeRequest?.generation === requestGeneration) activeRequest = null;
        });

      activeRequest = { generation: requestGeneration, controller, promise };
      return promise;
    },
    invalidate() {
      generation += 1;
      const requestToAbort = activeRequest;
      activeRequest = null;
      requestToAbort?.controller.abort();
    },
    get inFlight() {
      return Boolean(activeRequest);
    },
  };
}
