import React, { useCallback, useEffect, useState } from 'react';

const TRADES_PER_PAGE = 20;

function pnlColor(v) {
  const n = Number(v || 0);
  return { color: n > 0 ? '#22c55e' : n < 0 ? '#ef4444' : '#9ca3af' };
}

function fmt(v, digits = 2) {
  const n = Number(v || 0);
  return Number.isFinite(n) ? n.toFixed(digits) : '-';
}

function DecisionPanel({ panel }) {
  const reasons = panel?.top_reasons || [];
  const recent = panel?.recent || [];
  const lastExecutionTime = panel?.last_execution_time || panel?.latest_time;
  const lastExecutionText = lastExecutionTime
    ? new Date(lastExecutionTime).toLocaleString('zh-CN', { timeZone: 'Asia/Shanghai' })
    : '暂无记录';

  return (
    <div className="trading-section">
      <h3>系统刚才为什么没动手</h3>
      <div className="plain-grid">
        <div className="plain-card">
          <div className="plain-title">开仓前新增检查</div>
          <div className="plain-meta">最后执行：{lastExecutionText}</div>
          <div className="plain-meta">策略学习规则：{panel?.active_entry_policy_count || 0} 条已生效 | {panel?.active_entry_policy_version || 'empty'}</div>
          <div className="plain-text">现在不是只看排名。系统会先看分数、开仓信号、方向一致性、历史期望和盘口承接；最后下单前还会实时查 Binance 买卖盘。</div>
        </div>
        <div className="plain-card">
          <div className="plain-title">持仓后新增检查</div>
          <div className="plain-text">持仓会持续看 Hold Alpha、评分衰减、盘口变弱、时间止损、移动止盈和 TP1/TP2，触发后自动减仓或平仓。</div>
        </div>
      </div>
      <div className="muted-box">当前开仓线：{panel?.entry_gate_plain || '开仓线按币种模板判断'}　行情状态：{panel?.regime_effect_plain || '行情状态只调整开仓名额和仓位'}</div>
      {reasons.length > 0 ? (
        <div className="reason-list">
          {reasons.map((r, i) => (
            <div className="reason-row" key={i}><span>{r.plain || r.reason}</span><b>{r.count} 次</b></div>
          ))}
        </div>
      ) : <div className="muted-box" style={{ marginTop: 10 }}>最近一轮没有记录到过滤原因。</div>}
      {recent.length > 0 && (
        <div className="decision-strip">
          {recent.slice(0, 8).map((d, i) => <div className="decision-pill" key={i}><strong>{d.symbol}</strong><span>{d.plain || d.result}</span></div>)}
        </div>
      )}
    </div>
  );
}

export default function LiveTrading() {
  const [status, setStatus] = useState(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState(null);
  const [tradePage, setTradePage] = useState(1);

  const fetchAll = useCallback(async () => {
    try {
      const res = await fetch('/api/trading/status');
      const data = await res.json();
      if (data.error) {
        setError('Binance API 数据不可用: ' + data.error);
        setStatus(null);
      } else {
        setStatus(data);
        setError(null);
      }
    } catch (e) {
      setError('加载实盘数据失败: ' + e.message);
    } finally {
      setLoading(false);
    }
  }, []);

  useEffect(() => {
    fetchAll();
    const id = setInterval(fetchAll, 30000);
    return () => clearInterval(id);
  }, [fetchAll]);

  if (loading) return <div className="trading-section">加载中...</div>;
  if (error) return <div className="trading-section" style={{ color: '#ef4444' }}>{error}</div>;

  const positions = status?.positions || [];
  const recentTrades = status?.recent_trades || [];
  const stats = status?.stats || status || {};
  const visibleTrades = recentTrades.slice((tradePage - 1) * TRADES_PER_PAGE, tradePage * TRADES_PER_PAGE);

  return (
    <div className="trading-panel">
      <div className="trading-section">
        <h3>账户统计</h3>
        <div className="stats-grid">
          <div className="stat-card"><div className="stat-label">账户余额</div><div className="stat-value">${fmt(status?.balance)}</div></div>
          <div className="stat-card"><div className="stat-label">当前持仓</div><div className="stat-value">{positions.length}</div></div>
          <div className="stat-card"><div className="stat-label">开仓次数</div><div className="stat-value">{stats.total_opens || status?.total_trades || 0}</div></div>
          <div className="stat-card"><div className="stat-label">已平仓</div><div className="stat-value">{stats.total_closed || 0}</div></div>
          <div className="stat-card"><div className="stat-label">胜利/失败</div><div className="stat-value">{stats.win_count || 0} / {stats.loss_count || 0}</div></div>
          <div className="stat-card"><div className="stat-label">总盈亏</div><div className="stat-value" style={pnlColor(status?.total_pnl)}>${fmt(status?.total_pnl)}</div></div>
        </div>
      </div>

      <DecisionPanel panel={status?.decision_panel} />

      <div className="trading-section">
        <h3>当前持仓</h3>
        {positions.length === 0 ? (
          <div style={{ color: '#6b7280', padding: 20, textAlign: 'center' }}>暂无持仓，系统将在评分扫描后自动判断是否开仓</div>
        ) : (
          <div style={{ display: 'grid', gap: 12, gridTemplateColumns: 'repeat(auto-fill, minmax(340px, 1fr))' }}>
            {positions.map((p) => (
              <div key={p.symbol} className="pos-card">
                <div className="pos-header">
                  <span className="pos-symbol">{p.symbol}</span>
                  <span className="pos-side" style={{ color: p.side === 'LONG' ? '#22c55e' : '#ef4444' }}>{p.side === 'LONG' ? '做多' : '做空'}</span>
                  <span style={{ marginLeft: 'auto', fontSize: 12, color: '#6b7280' }}>投入 {fmt(p.invested || p.margin)}U</span>
                </div>
                <div className="pos-body">
                  <div className="pos-row"><span className="label">数量</span><span className="value">{p.quantity}</span></div>
                  <div className="pos-row"><span className="label">入场价</span><span className="value">${fmt(p.entry_price, 4)}</span></div>
                  <div className="pos-row"><span className="label">当前价</span><span className="value">${fmt(p.mark_price, 4)}</span></div>
                  <div className="pos-row"><span className="label">浮动盈亏</span><span className="value" style={pnlColor(p.unrealized_pnl)}>${fmt(p.unrealized_pnl)} ({fmt(p.pnl_pct)}%)</span></div>
                  <div className="position-rules">
                    <div><b>系统管理</b> 入场评分 {p.entry_score ? fmt(p.entry_score, 1) : '-'}</div>
                    <div>TP1：{p.tp1_hit ? '已减过仓' : '未触发'} · TP2：{p.tp2_hit ? '已减过仓' : '未触发'}</div>
                    <div>最高跟踪价：{p.highest_price ? `$${fmt(p.highest_price, 4)}` : '-'}</div>
                    <div>上次系统动作：{p.last_exit_plain || p.last_exit_reason || '暂无'}</div>
                  </div>
                </div>
              </div>
            ))}
          </div>
        )}
      </div>

      <div className="trading-section">
        <h3>历史交易</h3>
        {recentTrades.length === 0 ? (
          <div style={{ color: '#6b7280', padding: 20, textAlign: 'center' }}>暂无历史交易</div>
        ) : (
          <>
            <table className="trade-table">
              <thead>
                <tr><th>币种</th><th>方向</th><th>数量</th><th>开仓价</th><th>平仓价</th><th>盈亏</th><th>盈亏%</th><th>评分</th><th>时间</th></tr>
              </thead>
              <tbody>
                {visibleTrades.map((t, i) => (
                  <tr key={i}>
                    <td style={{ fontWeight: 600, color: '#c9d1d9' }}>{t.symbol}</td>
                    <td style={{ color: t.side === 'LONG' ? '#22c55e' : '#ef4444' }}>{t.side === 'LONG' ? '多' : '空'}</td>
                    <td>{t.qty || t.quantity}</td>
                    <td>${fmt(t.entry_price, 4)}</td>
                    <td>{t.exit_price ? '$' + fmt(t.exit_price, 4) : '-'}</td>
                    <td style={pnlColor(t.pnl)}>{Number(t.pnl || 0) >= 0 ? '+' : ''}${fmt(t.pnl)}</td>
                    <td style={pnlColor(t.pnl_pct)}>{t.pnl_pct != null ? `${fmt(t.pnl_pct)}%` : '-'}</td>
                    <td>{t.grade_at_entry || '-'} {t.score_at_entry != null ? fmt(t.score_at_entry, 1) : ''}</td>
                    <td>{t.exit_time || t.entry_time || '-'}</td>
                  </tr>
                ))}
              </tbody>
            </table>
            <div style={{ display: 'flex', justifyContent: 'center', gap: 8, marginTop: 12 }}>
              <button onClick={() => setTradePage((p) => Math.max(1, p - 1))} disabled={tradePage === 1}>上一页</button>
              <span style={{ color: '#9ca3af', padding: '4px 8px' }}>{tradePage}</span>
              <button onClick={() => setTradePage((p) => p + 1)} disabled={tradePage * TRADES_PER_PAGE >= recentTrades.length}>下一页</button>
            </div>
          </>
        )}
      </div>
    </div>
  );
}
