import React, { useEffect, useState } from 'react';

export default function TradingEnvironmentStatus() {
  const [label, setLabel] = useState('LIVE CHECKING');
  useEffect(() => {
    let active = true;
    const load = async () => {
      try {
        const response = await fetch('/api/trading/accounts/status');
        const data = await response.json();
        if (active) setLabel(data.environment_status || 'LIVE DEGRADED');
      } catch { if (active) setLabel('LIVE DEGRADED'); }
    };
    load();
    const timer = setInterval(load, 30000);
    return () => { active = false; clearInterval(timer); };
  }, []);
  return <div className={`terminal-status ${label === 'LIVE DEGRADED' ? 'degraded' : ''}`}><span className="live-dot" /><span>{label}</span></div>;
}
