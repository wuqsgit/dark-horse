export function latestAIBySymbol(decisions) {
  const latest = {};
  (decisions || []).forEach((item) => {
    const symbol = String(item.symbol || '').toUpperCase();
    if (!symbol) return;
    const current = latest[symbol];
    if (!current || String(item.observed_at || '') > String(current.observed_at || '')) {
      latest[symbol] = item;
    }
  });
  return latest;
}

export function sampleProgress(model = {}) {
  const total = Number(model.total_samples || 0);
  const pending = Number(model.pending_samples || 0);
  const labeled = Number(model.sample_count || 0);
  const required = Number(model.required_samples || 300);
  const collectedToday = Number(model.collected_today || 0);
  return {
    total,
    pending,
    labeled,
    required,
    remaining: Math.max(0, required - labeled),
    percent: required > 0 ? Number(Math.min(100, labeled / required * 100).toFixed(1)) : 0,
    collectedToday,
  };
}

export function chinaTime(value) {
  if (!value) return '-';
  const date = new Date(value);
  if (Number.isNaN(date.getTime())) return '-';
  return new Intl.DateTimeFormat('zh-CN', {
    timeZone: 'Asia/Shanghai',
    year: 'numeric', month: 'numeric', day: 'numeric',
    hour: '2-digit', minute: '2-digit', second: '2-digit', hour12: false,
  }).format(date);
}
