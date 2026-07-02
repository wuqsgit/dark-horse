import React, { useState } from 'react';
import ScanTable from './components/ScanTable';
import AlphaScan from './components/AlphaScan';
import BacktestPanel from './components/BacktestPanel';
import LiveTrading from './components/LiveTrading';
import './styles.css';

export default function App() {
  const [currentPage, setCurrentPage] = useState('scan');

  return (
    <div className="app">
      <header className="header">
        <div className="brand-lockup">
          <div className="brand-mark">DH</div>
          <div>
            <h1>DarkHorse</h1>
            <span className="subtitle">Quant Trading Terminal</span>
          </div>
        </div>
        <nav className="nav">
          <button className={currentPage === 'scan' ? 'active' : ''} onClick={() => setCurrentPage('scan')}>扫描</button>
          <button className={currentPage === 'alpha' ? 'active' : ''} onClick={() => setCurrentPage('alpha')}>Alpha 扫描</button>
          <button className={currentPage === 'backtest' ? 'active' : ''} onClick={() => setCurrentPage('backtest')}>回测</button>
          <button className={currentPage === 'trading' ? 'active' : ''} onClick={() => setCurrentPage('trading')}>实盘</button>
        </nav>
        <div className="terminal-status">
          <span className="live-dot" />
          <span>TESTNET LIVE</span>
        </div>
      </header>

      <main className="main">
        {currentPage === 'scan' && <ScanTable />}
        {currentPage === 'alpha' && <AlphaScan />}
        {currentPage === 'backtest' && <BacktestPanel API={{ get: (url) => fetch(`/api${url}`).then((r) => r.json()) }} />}
        {currentPage === 'trading' && <LiveTrading API="/api" />}
      </main>
    </div>
  );
}
