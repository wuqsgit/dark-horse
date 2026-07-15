export function createTradingAccountsStatusClient(fetchImpl = fetch, dedupeMs = 30000) {
  let inFlight = null;
  let cached = null;
  let cachedAt = 0;

  const load = async ({ force = false } = {}) => {
    if (!force && cached && Date.now() - cachedAt < dedupeMs) return cached;
    if (inFlight) return inFlight;

    inFlight = (async () => {
      const response = await fetchImpl('/api/trading/accounts/status');
      if (!response.ok) throw new Error(`持仓状态请求失败: ${response.status}`);
      const data = await response.json();
      cached = data;
      cachedAt = Date.now();
      return data;
    })();

    try {
      return await inFlight;
    } finally {
      inFlight = null;
    }
  };

  return { load };
}

const tradingAccountsStatusClient = createTradingAccountsStatusClient();

export const fetchTradingAccountsStatus = (options) => tradingAccountsStatusClient.load(options);
