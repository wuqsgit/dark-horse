const HISTORY_QUERY_PARAMETERS = new Set([
  'cursor',
  'limit',
  'symbol',
  'direction',
  'source',
  'from',
  'to',
]);

async function requestJson(fetchImpl, url, signal) {
  const response = await fetchImpl(url, { signal });
  if (!response.ok) throw new Error(`${url}: ${response.status}`);
  return response.json();
}

function historyUrl(accountId, params = {}) {
  const query = new URLSearchParams();
  for (const [key, value] of Object.entries(params || {})) {
    if (!HISTORY_QUERY_PARAMETERS.has(key) || value === undefined || value === null || value === '') {
      continue;
    }
    query.set(key, value);
  }

  const base = `/api/trading/accounts/${encodeURIComponent(String(accountId))}/history`;
  const queryString = query.toString();
  return queryString ? `${base}?${queryString}` : base;
}

export function createTradingDataClient(fetchImpl = fetch, dedupeMs = 30000) {
  let statusInFlight = null;
  let statusCached;
  let statusCachedAt = 0;
  let hasCachedStatus = false;

  const accounts = ({ signal } = {}) => requestJson(
    fetchImpl,
    '/api/trading/accounts',
    signal,
  );

  const status = async ({ force = false, signal } = {}) => {
    if (!force && hasCachedStatus && Date.now() - statusCachedAt < dedupeMs) {
      return statusCached;
    }
    if (statusInFlight) return statusInFlight;

    statusInFlight = requestJson(
      fetchImpl,
      '/api/trading/accounts/status',
      signal,
    ).then((data) => {
      statusCached = data;
      statusCachedAt = Date.now();
      hasCachedStatus = true;
      return data;
    });

    try {
      return await statusInFlight;
    } finally {
      statusInFlight = null;
    }
  };

  const history = (accountId, params = {}, { signal } = {}) => requestJson(
    fetchImpl,
    historyUrl(accountId, params),
    signal,
  );

  const decisions = (accountId, { signal } = {}) => requestJson(
    fetchImpl,
    `/api/trading/accounts/${encodeURIComponent(String(accountId))}/decisions`,
    signal,
  );

  const runtime = ({ signal } = {}) => requestJson(
    fetchImpl,
    '/api/trading/runtime/status',
    signal,
  );

  return { accounts, status, history, decisions, runtime };
}

const tradingDataClient = createTradingDataClient();

export const fetchTradingAccounts = (options) => tradingDataClient.accounts(options);
export const fetchTradingAccountsStatus = (options) => tradingDataClient.status(options);
export const fetchTradingHistory = (accountId, params, options) => (
  tradingDataClient.history(accountId, params, options)
);
export const fetchTradingDecisions = (accountId, options) => (
  tradingDataClient.decisions(accountId, options)
);
export const fetchTradingRuntimeStatus = (options) => tradingDataClient.runtime(options);
