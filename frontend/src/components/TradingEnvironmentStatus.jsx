import React, { useEffect, useState } from 'react';
import { fetchTradingAccountsStatus } from '../api/tradingData';
import { tradingEnvironmentDisplay } from './liveTradingAccountSelection';

export default function TradingEnvironmentStatus() {
  const [display, setDisplay] = useState({ label: 'LIVE CHECKING', degraded: false });
  useEffect(() => {
    let active = true;
    const load = async () => {
      try {
        const data = await fetchTradingAccountsStatus();
        if (active) setDisplay(tradingEnvironmentDisplay(data));
      } catch {
        if (active) setDisplay({ label: 'LIVE DEGRADED', degraded: true });
      }
    };
    load();
    const timer = setInterval(load, 30000);
    return () => { active = false; clearInterval(timer); };
  }, []);
  return <div className={`terminal-status ${display.degraded ? 'degraded' : ''}`}><span className="live-dot" /><span>{display.label}</span></div>;
}
