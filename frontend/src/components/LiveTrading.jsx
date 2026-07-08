import React, { useCallback, useEffect, useMemo, useState } from 'react';

const TRADES_PER_PAGE = 20;

function pnlColor(v) {
  const n = Number(v || 0);
  return { color: n > 0 ? '#22c55e' : n < 0 ? '#ef4444' : '#9ca3af' };
}

function fmt(v, digits = 2) {
  const n = Number(v || 0);
  return Number.isFinite(n) ? n.toFixed(digits) : '-';
}

function timeText(value) {
  if (!value) return '-';
  const text = String(value).trim();
  const hasZone = /(?:Z|[+-]\d{2}:?\d{2})$/i.test(text);
  const normalized = hasZone ? text : `${text.replace(' ', 'T')}Z`;
  return new Date(normalized).toLocaleString('zh-CN', {
    timeZone: 'Asia/Shanghai',
    hour12: false,
  });
}

function sourceText(v) {
  return v === 'alpha' ? 'Alpha 策略' : '普通策略';
}

function alphaProfileText(v) {
  if (v === 'early_discovery') return '早期发现型';
  if (v === 'momentum_continuation') return '动量延续型';
  if (v === 'futures_mapped') return '合约映射型';
  if (v === 'high_risk_watch') return '高风险观察型';
  if (v === 'neutral_watch') return '中性观察型';
  return v || '-';
}

function entryLevelText(v) {
  if (v === 'probe') return '小仓试探';
  if (v === 'candidate') return 'Alpha 候选';
  if (v === 'observe') return '观察';
  if (v === 'block') return '禁止开仓';
  return v || '-';
}

function volumePriceText(v) {
  const map = {
    accumulation_volume: 'Accumulation',
    breakout_pullback: 'Breakout pullback',
    momentum_continuation: 'Momentum',
    wide_spread: 'Wide spread',
    neutral: 'Neutral',
    failed_breakout: 'Failed breakout',
    distribution: 'Distribution',
    dumping: 'Dumping',
    breakdown: 'Breakdown',
  };
  return map[v] || v || '-';
}

function volumePriceActionText(v) {
  const map = {
    normal_review: 'normal',
    normal_review_probe: 'probe',
    short_review_only: 'short',
    observe: 'observe',
    cooldown: 'cooldown',
  };
  return map[v] || v || '-';
}

function DecisionPanel({ panel }) {
  const reasons = panel?.top_reasons || [];
  const recent = panel?.recent || [];
  const latestDecision = recent[0];
  const lastExecutionTime = panel?.last_execution_time || panel?.latest_time;
  const lastExecutionText = lastExecutionTime
    ? timeText(lastExecutionTime)
    : '暂无记录';

  return (
    <div className="trading-section">
      <h3>系统刚才为什么没动手</h3>
      <div className="plain-grid">
        <div className="plain-card">
          <div className="plain-title">开仓前检查</div>
          <div className="plain-meta">最后执行：{lastExecutionText}</div>
          <div className="plain-meta">策略学习规则：{panel?.active_entry_policy_count || 0} 条已生效 | {panel?.active_entry_policy_version || 'empty'}</div>
          <div className="plain-text">
            普通信号和 Alpha 信号都会先过分数、模板、方向、账户风控和 Binance 实时盘口；Alpha 还会检查分类模板、entry_level、futures 映射和信号新鲜度。
          </div>
        </div>
        <div className="plain-card">
          <div className="plain-title">持仓后检查</div>
          <div className="plain-text">
            持仓会继续看 Hold Alpha、评分衰减、盘口变弱、时间止损、移动止盈和 TP1/TP2，触发后自动减仓或平仓。
          </div>
        </div>
      </div>
      <div className="muted-box">
        当前开仓线：{panel?.entry_gate_plain || '按币种模板判断'}　行情状态：{panel?.regime_effect_plain || '只调整名额和仓位'}
      </div>
      {reasons.length > 0 ? (
        <div className="reason-list">
          {reasons.map((r, i) => (
            <div className="reason-row" key={i}><span>{r.plain || r.reason}</span><b>{r.count} 次</b></div>
          ))}
        </div>
      ) : <div className="muted-box" style={{ marginTop: 10 }}>最近一轮没有记录到过滤原因。</div>}
      {latestDecision && (
        <div className="decision-strip">
          <div className="decision-pill">
            <strong>{latestDecision.symbol}</strong>
            <span>{latestDecision.plain || latestDecision.result}</span>
          </div>
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
  const [tradeFilter, setTradeFilter] = useState('all');
  const [switching, setSwitching] = useState(null);
  const [toast, setToast] = useState(null);

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

  const positions = status?.positions || [];
  const recentTrades = status?.recent_trades || [];
  const stats = status?.stats || status || {};
  const filteredTrades = useMemo(() => {
    if (tradeFilter === 'all') return recentTrades;
    return recentTrades.filter((t) => (t.strategy_source || 'normal') === tradeFilter);
  }, [recentTrades, tradeFilter]);
  const visibleTrades = filteredTrades.slice((tradePage - 1) * TRADES_PER_PAGE, tradePage * TRADES_PER_PAGE);

  useEffect(() => {
    setTradePage(1);
  }, [tradeFilter]);

  useEffect(() => {
    if (!toast) return undefined;
    const id = setTimeout(() => setToast(null), 3600);
    return () => clearTimeout(id);
  }, [toast]);

  const showToast = (type, message) => {
    setToast({ type, message, id: Date.now() });
  };

  const toggleTrading = async (mode, enabled) => {
    setSwitching(mode);
    setError(null);
    const modeText = mode === 'alpha' ? 'Alpha 交易' : '普通交易';
    try {
      const res = await fetch('/api/trading/controls', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ mode, enabled }),
      });
      const data = await res.json();
      if (data.error) throw new Error(data.error);
      await fetchAll();
      const closed = Number(data?.close_result?.closed || 0);
      if (enabled) {
        showToast('success', `${modeText}已开启，下一轮实盘扫描会按新开关执行。`);
      } else if (closed > 0) {
        showToast('warning', `${modeText}已关闭，并已触发平仓 ${closed} 个持仓。`);
      } else {
        showToast('warning', `${modeText}已关闭，当前没有需要平掉的对应持仓。`);
      }
    } catch (e) {
      showToast('error', `${modeText}切换失败：${e.message}`);
    } finally {
      setSwitching(null);
    }
  };
  if (loading) return <div className="trading-section">加载中...</div>;
  if (error) return <div className="trading-section" style={{ color: '#ef4444' }}>{error}</div>;

  return (
    <div className="trading-panel">
      {toast && (
        <div className={`trade-toast trade-toast-${toast.type}`} role="status">
          <span>{toast.message}</span>
          <button type="button" onClick={() => setToast(null)}>×</button>
        </div>
      )}
      <div className="trading-section">
        <h3>交易开关</h3>
        <div className="plain-grid">
          {[
            {
              mode: 'normal',
              key: 'normal_trading_enabled',
              title: '普通交易',
              desc: '关闭后普通策略不再开新仓；如果已有普通策略持仓，会立即市价平仓。',
              activeText: '普通开仓已开启',
              inactiveText: '普通开仓已关闭',
            },
            {
              mode: 'alpha',
              key: 'alpha_trading_enabled',
              title: 'Alpha 交易',
              desc: '关闭后 Alpha 策略不再开新仓；如果已有 Alpha 持仓，会立即市价平仓。',
              activeText: 'Alpha 开仓已开启',
              inactiveText: 'Alpha 开仓已关闭',
            },
          ].map((item) => {
            const enabled = Boolean(status?.trading_controls?.[item.key]);
            const relatedCount = positions.filter((p) => (p.strategy_source || 'normal') === item.mode).length;
            return (
              <div className="plain-card" key={item.mode}>
                <div style={{ display: 'flex', alignItems: 'center', gap: 12 }}>
                  <div style={{ flex: 1 }}>
                    <div className="plain-title">{item.title}</div>
                    <div className="plain-meta">{enabled ? item.activeText : item.inactiveText} · 当前持仓 {relatedCount}</div>
                  </div>
                  <button
                    onClick={() => toggleTrading(item.mode, !enabled)}
                    disabled={switching === item.mode}
                    style={{
                      minWidth: 86,
                      border: `1px solid ${enabled ? '#16a34a' : '#475569'}`,
                      background: enabled ? 'rgba(22, 163, 74, 0.18)' : '#111827',
                      color: enabled ? '#4ade80' : '#cbd5e1',
                      borderRadius: 999,
                      padding: '8px 14px',
                      cursor: switching === item.mode ? 'wait' : 'pointer',
                      fontWeight: 700,
                    }}
                  >
                    {switching === item.mode ? '处理中' : enabled ? '开启' : '关闭'}
                  </button>
                </div>
                <div className="plain-text" style={{ marginTop: 10 }}>{item.desc}</div>
              </div>
            );
          })}
        </div>
      </div>

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

      <div className="trading-section">
        <h3>当前持仓</h3>
        {positions.length === 0 ? (
          <div style={{ color: '#6b7280', padding: 20, textAlign: 'center' }}>暂无持仓，系统会在评分扫描后自动判断是否开仓。</div>
        ) : (
          <div style={{ display: 'grid', gap: 12, gridTemplateColumns: 'repeat(auto-fill, minmax(340px, 1fr))' }}>
            {positions.map((p) => (
              <div key={p.symbol} className="pos-card">
                <div className="pos-header">
                  <span className="pos-symbol">{p.symbol}</span>
                  <span className="pos-side" style={{ color: p.side === 'LONG' ? '#22c55e' : '#ef4444' }}>{p.side === 'LONG' ? '做多' : '做空'}</span>
                  <span className="mini-pill" style={{ marginLeft: 8 }}>{sourceText(p.strategy_source)}</span>
                  <span style={{ marginLeft: 'auto', fontSize: 12, color: '#6b7280' }}>名义价值 {fmt(p.invested || p.margin)}U</span>
                </div>
                {p.strategy_source === 'alpha' && (
                  <div className="scan-score" style={{ marginBottom: 10 }}>
                    {p.alpha_symbol || 'Alpha'} · {alphaProfileText(p.alpha_profile)} · {entryLevelText(p.alpha_entry_level)} · Alpha {fmt(p.alpha_score, 1)}
                  </div>
                )}
                {p.strategy_source === 'alpha' && p.alpha_volume_price_state && (
                  <div className="scan-score" style={{ marginBottom: 10 }}>
                    Alpha hold: {volumePriceText(p.alpha_volume_price_state)} · {volumePriceActionText(p.alpha_volume_price_action)}
                    {p.alpha_current_score != null ? ` · score ${fmt(p.alpha_current_score, 1)}` : ''}
                    {p.alpha_volume_price_reason ? ` · ${p.alpha_volume_price_reason}` : ''}
                  </div>
                )}
                <div className="pos-body">
                  <div className="pos-row"><span className="label">数量</span><span className="value">{p.quantity}</span></div>
                  <div className="pos-row"><span className="label">杠杆</span><span className="value">{p.leverage ? `${p.leverage}x` : '-'}</span></div>
                  <div className="pos-row"><span className="label">保证金</span><span className="value">{fmt(p.margin)}U</span></div>
                  <div className="pos-row"><span className="label">维持保证金</span><span className="value">{fmt(p.maint_margin)}U</span></div>
                  <div className="pos-row"><span className="label">保证金率</span><span className="value">{p.margin_ratio != null ? `${fmt(p.margin_ratio, 4)}%` : '-'}</span></div>
                  <div className="pos-row"><span className="label">保证金类型</span><span className="value">{p.margin_type || '-'}</span></div>
                  <div className="pos-row"><span className="label">入场价</span><span className="value">${fmt(p.entry_price, 4)}</span></div>
                  <div className="pos-row"><span className="label">持仓时间</span><span className="value">{p.holding_time || '-'}</span></div>
                  <div className="pos-row"><span className="label">当前价</span><span className="value">${fmt(p.mark_price, 4)}</span></div>
                  <div className="pos-row"><span className="label">浮动盈亏/保证金</span><span className="value" style={pnlColor(p.unrealized_pnl)}>${fmt(p.unrealized_pnl)} ({fmt(p.pnl_pct)}%)</span></div>
                  <div className="position-rules">
                    <div><b>系统管理</b> 入场评分 {p.entry_score ? fmt(p.entry_score, 1) : '-'}</div>
                    <div>TP1：{p.tp1_hit ? '已减过仓' : '未触发'} · TP2：{p.tp2_hit ? '已减过仓' : '未触发'}</div>
                    <div>最高跟踪价：{p.highest_price ? `$${fmt(p.highest_price, 4)}` : '-'}</div>
                    <div>止损模型：{p.stop_model || '-'} · 初始止损 {p.stop_pct ? `${fmt(Number(p.stop_pct) * 100, 2)}%` : '-'}</div>
                    <div>当前R：{p.r_multiple != null ? `${fmt(p.r_multiple, 2)}R` : '-'} · 保护止损：{p.current_stop_loss ? `$${fmt(p.current_stop_loss, 4)}` : '-'}</div>
                    <div>移动止损：{p.trailing_enabled ? '已启用' : '未启用'} · {p.trailing_stop_price ? `$${fmt(p.trailing_stop_price, 4)}` : '-'}</div>
                    <div>上次系统动作：{p.last_exit_plain || p.last_exit_reason || '暂无'}</div>
                    <div>滚仓层数：{p.roll_layer || 0}/2 · 保护利润：${fmt(p.protected_profit || 0)}</div>
                    <div>最高浮盈：${fmt(p.max_floating_pnl || 0)} · {p.roll_enabled ? '允许滚仓观察' : '暂不滚仓'}</div>
                    {p.roll_block_reason && <div>滚仓阻断：{p.roll_block_reason}</div>}
                  </div>
                </div>
              </div>
            ))}
          </div>
        )}
      </div>

      <DecisionPanel panel={status?.decision_panel} />

      <div className="trading-section">
        <div style={{ display: 'flex', alignItems: 'center', justifyContent: 'space-between', gap: 12, flexWrap: 'wrap' }}>
          <h3>历史交易</h3>
          <div className="scan-toolbar" style={{ margin: 0 }}>
            <button className={tradeFilter === 'all' ? 'active' : ''} onClick={() => setTradeFilter('all')}>全部</button>
            <button className={tradeFilter === 'normal' ? 'active' : ''} onClick={() => setTradeFilter('normal')}>普通策略</button>
            <button className={tradeFilter === 'alpha' ? 'active' : ''} onClick={() => setTradeFilter('alpha')}>Alpha 策略</button>
          </div>
        </div>
        {filteredTrades.length === 0 ? (
          <div style={{ color: '#6b7280', padding: 20, textAlign: 'center' }}>暂无历史交易</div>
        ) : (
          <>
            <table className="trade-table">
              <thead>
                <tr><th>币种</th><th>来源</th><th>方向</th><th>数量</th><th>开仓价</th><th>平仓价</th><th>盈亏</th><th>盈亏%</th><th>评分</th><th>时间</th></tr>
              </thead>
              <tbody>
                {visibleTrades.map((t, i) => (
                  <tr key={i}>
                    <td style={{ fontWeight: 600, color: '#c9d1d9' }}>
                      {t.symbol}
                      {t.close_count > 1 ? <span className="mini-pill" style={{ marginLeft: 6 }}>合并 {t.close_count}</span> : null}
                      {t.alpha_symbol ? <span className="mini-pill" style={{ marginLeft: 6 }}>{t.alpha_symbol}</span> : null}
                    </td>
                    <td>{sourceText(t.strategy_source)}{t.alpha_profile ? ` · ${alphaProfileText(t.alpha_profile)}` : ''}</td>
                    <td style={{ color: t.side === 'LONG' ? '#22c55e' : '#ef4444' }}>{t.side === 'LONG' ? '多' : '空'}</td>
                    <td>{t.qty || t.quantity}</td>
                    <td>${fmt(t.entry_price, 4)}</td>
                    <td>{t.exit_price ? '$' + fmt(t.exit_price, 4) : '-'}</td>
                    <td style={pnlColor(t.pnl)}>{Number(t.pnl || 0) >= 0 ? '+' : ''}${fmt(t.pnl)}</td>
                    <td style={pnlColor(t.pnl_pct)}>{t.pnl_pct != null ? `${fmt(t.pnl_pct)}%` : '-'}</td>
                    <td>{t.grade_at_entry || '-'} {t.score_at_entry != null ? fmt(t.score_at_entry, 1) : ''}</td>
                    <td>{timeText(t.exit_time || t.entry_time)}</td>
                  </tr>
                ))}
              </tbody>
            </table>
            <div style={{ display: 'flex', justifyContent: 'center', gap: 8, marginTop: 12 }}>
              <button onClick={() => setTradePage((p) => Math.max(1, p - 1))} disabled={tradePage === 1}>上一页</button>
              <span style={{ color: '#9ca3af', padding: '4px 8px' }}>{tradePage}</span>
              <button onClick={() => setTradePage((p) => p + 1)} disabled={tradePage * TRADES_PER_PAGE >= filteredTrades.length}>下一页</button>
            </div>
          </>
        )}
      </div>
    </div>
  );
}
