import React, { useEffect, useState } from 'react';

export default function MarketDataHealth() {
  const [health, setHealth] = useState(null);

  useEffect(() => {
    let active = true;
    const load = () => fetch('/api/market-data/health')
      .then((response) => response.json())
      .then((data) => active && setHealth(data))
      .catch(() => active && setHealth(null));
    load();
    const timer = setInterval(load, 30000);
    return () => { active = false; clearInterval(timer); };
  }, []);

  if (!health) return <div className="market-health market-health-error">行情状态不可用</div>;
  const pools = [['normal', '普通'], ['alpha', 'Alpha']];
  return (
    <div className="market-health" title="现货与合约 K 线均完整且新鲜时才计为就绪">
      {pools.map(([key, label]) => {
        const pool = health[key] || {};
        const healthy = pool.selected > 0 && pool.unready === 0;
        return (
          <span className={healthy ? 'market-health-ok' : 'market-health-warn'} key={key}>
            {label} {pool.ready || 0}/{pool.selected || 0}
          </span>
        );
      })}
    </div>
  );
}
