import React, { useState } from 'react';
import ScanTable from './components/ScanTable';
import BacktestPanel from './components/BacktestPanel';
import LiveTrading from './components/LiveTrading';
import './styles.css';

export default function App() {
  const [currentPage, setCurrentPage] = useState('scan');

  return (
    <div className="app">
      <header className="header">
        <h1>🐎 DarkHorse</h1>
        <span className="subtitle">加密货币多维评分系统</span>
        <nav className="nav">
          <button className={currentPage === 'scan' ? 'active' : ''} onClick={() => setCurrentPage('scan')}>扫描</button>
          <button className={currentPage === 'backtest' ? 'active' : ''} onClick={() => setCurrentPage('backtest')}>回测</button>
          <button className={currentPage === 'trading' ? 'active' : ''} onClick={() => setCurrentPage('trading')}>实盘</button>
        </nav>
      </header>

      <main className="main">
        {currentPage === 'scan' && <ScanTable />}
        {currentPage === 'backtest' && <BacktestPanel API={{ get: (url) => fetch(`/api${url}`).then(r => r.json()) }} />}
        {currentPage === 'trading' && <LiveTrading API="/api" />}
      </main>
    </div>
  );
}
