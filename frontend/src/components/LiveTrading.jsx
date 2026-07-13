import React, { useCallback, useEffect, useMemo, useState } from 'react';
import TradingAccountManager from './TradingAccountManager';
import { findSelectedAccount, normalizeSelectedAccount } from './liveTradingAccountSelection';

const TRADES_PER_PAGE = 20;

function pnlColor(v) {
  const n = Number(v || 0);
  return { color: n > 0 ? '#22c55e' : n < 0 ? '#ef4444' : '#9ca3af' };
}

function fmt(v, digits = 2) {
  const n = Number(v || 0);
  return Number.isFinite(n) ? n.toFixed(digits) : '-';
}

function fmtValue(v, digits = 2) {
  if (v === null || v === undefined || v === '') return '-';
  const n = Number(v);
  return Number.isFinite(n) ? n.toFixed(digits) : '-';
}

function marketPhaseText(v) {
  const map = {
    trend_up: '上涨趋势',
    trend_down: '下跌趋势',
    range: '震荡',
    breakout_pending: '突破待确认',
    breakdown_risk: '破位风险',
    uncertain: '不确定',
  };
  return map[v] || v || '-';
}

function sideText(side) {
  if (side === 'LONG') return '多';
  if (side === 'SHORT') return '空';
  return '-';
}

function sideColor(side) {
  if (side === 'LONG') return '#22c55e';
  if (side === 'SHORT') return '#ef4444';
  return '#9ca3af';
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
  const [warning, setWarning] = useState(null);
  const [tradePage, setTradePage] = useState(1);
  const [tradeFilter, setTradeFilter] = useState('all');
  const [switching, setSwitching] = useState(null);
  const [toast, setToast] = useState(null);
  const [accountsData, setAccountsData] = useState({ accounts: [], summary: {} });
  const [accountConfigs, setAccountConfigs] = useState([]);
  const [selectedAccount, setSelectedAccount] = useState(null);

  const fetchAll = useCallback(async () => {
    try {
      const [res, accountsRes, configsRes] = await Promise.all([
        fetch('/api/trading/status'), fetch('/api/trading/accounts/status'), fetch('/api/trading/accounts'),
      ]);
      const [data, multi, configs] = await Promise.all([res.json(), accountsRes.json(), configsRes.json()]);
      setAccountsData(multi);
      setAccountConfigs(configs.accounts || []);
      if (data.error) {
        setWarning('默认账户诊断暂时不可用：' + data.error);
        setStatus(null);
        setError(null);
      } else {
        setStatus(data);
        setError(null);
        setWarning(data.binance_warning || null);
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

  useEffect(() => {
    const normalized = normalizeSelectedAccount(selectedAccount, accountsData.accounts || []);
    if (String(normalized) !== String(selectedAccount)) setSelectedAccount(normalized);
  }, [accountsData.accounts, selectedAccount]);

  const selectedRow = findSelectedAccount(selectedAccount, accountsData.accounts || []);
  const positions = selectedRow?.positions || [];
  const recentTrades = selectedRow?.recent_trades || [];
  const stats = selectedRow?.stats || {};
  const accountSummary = selectedRow || {};
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
      const targets = accountConfigs.filter((item) => String(item.id) === String(selectedAccount));
      if (!targets.length) throw new Error('请先选择具体账户');
      const key = mode === 'alpha' ? 'alpha_trading_enabled' : 'normal_trading_enabled';
      const results = await Promise.all(targets.map(async (account) => {
        const res = await fetch(`/api/trading/accounts/${account.id}`, {
          method: 'PATCH', headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ ...account, [key]: enabled }),
        });
        return res.json();
      }));
      const failed = results.find((item) => item.error);
      if (failed) throw new Error(failed.error);
      await fetchAll();
      if (enabled) {
        showToast('success', `${modeText}已开启，交易进程会自动加载账户配置。`);
      } else {
        showToast('warning', `${modeText}已关闭，仅停止新开仓，不会强制平掉已有仓位。`);
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
      <div className="trading-section">
        <TradingAccountManager accounts={accountConfigs} onChanged={fetchAll} />
        <div className="account-tabs" role="tablist" aria-label="交易账户">
          {(accountsData.accounts || []).map((account) => (
            <button key={account.account_id} className={String(selectedAccount) === String(account.account_id) ? 'active' : ''} onClick={() => setSelectedAccount(account.account_id)}>
              {account.account_name}<span className={`account-health ${account.status}`} />
            </button>
          ))}
        </div>
      </div>
      {warning && (
        <div className="trading-section" style={{ color: '#fbbf24', borderColor: '#92400e' }} role="status">
          {warning}{status?.stale_age_seconds != null ? `（快照延迟 ${status.stale_age_seconds} 秒）` : ''}
        </div>
      )}
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
            const enabled = selectedRow
              ? Boolean(selectedRow[item.key])
              : Boolean((accountsData.accounts || []).length && (accountsData.accounts || []).every((account) => account[item.key]));
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
          <div className="stat-card"><div className="stat-label">账户权益</div><div className="stat-value">${fmt(accountSummary.equity)}</div></div>
          <div className="stat-card"><div className="stat-label">当前持仓</div><div className="stat-value">{positions.length}</div></div>
          <div className="stat-card"><div className="stat-label">开仓次数</div><div className="stat-value">{stats.total_opens || 0}</div></div>
          <div className="stat-card"><div className="stat-label">已平仓</div><div className="stat-value">{stats.total_closed || 0}</div></div>
          <div className="stat-card"><div className="stat-label">胜利/失败</div><div className="stat-value">{stats.win_count || 0} / {stats.loss_count || 0}</div></div>
          <div className="stat-card"><div className="stat-label">总盈亏</div><div className="stat-value" style={pnlColor(accountSummary.total_pnl)}>${fmt(accountSummary.total_pnl)}</div></div>
        </div>
      </div>

      <div className="trading-section">
        <h3>当前持仓</h3>
        {positions.length === 0 ? (
          <div style={{ color: '#6b7280', padding: 20, textAlign: 'center' }}>暂无持仓，系统会在评分扫描后自动判断是否开仓。</div>
        ) : (
          <div style={{ display: 'grid', gap: 12, gridTemplateColumns: 'repeat(auto-fill, minmax(340px, 1fr))' }}>
            {positions.map((p) => (
              <div key={`${p.account_id || 'all'}-${p.symbol}`} className="pos-card">
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
                    <div>市场状态：{marketPhaseText(p.market_phase?.phase)} · {p.market_phase?.confidence != null ? `${fmt(p.market_phase.confidence, 0)}分` : '-'} · {p.market_phase?.allow_roll ? '允许滚仓' : '不滚仓'}</div>
                    <div>TP1：{p.tp1_hit ? '已减过仓' : '未触发'} · TP2：{p.tp2_hit ? '已减过仓' : '未触发'}</div>
                    <div>最高跟踪价：{p.highest_price ? `$${fmt(p.highest_price, 4)}` : '-'}</div>
                    <div>止损模型：{p.stop_model || '-'} · 初始止损 {p.stop_pct ? `${fmt(Number(p.stop_pct) * 100, 2)}%` : '-'}</div>
                    <div>当前R：{p.r_multiple != null ? `${fmt(p.r_multiple, 2)}R` : '-'} · 保护止损：{p.protected_stop || p.current_stop_loss ? `$${fmt(p.protected_stop || p.current_stop_loss, 4)}` : '-'}</div>
                    <div>移动止损：{p.trailing_enabled ? '已启用' : '未启用'} · {p.trailing_stop_price ? `$${fmt(p.trailing_stop_price, 4)}` : '-'}</div>
                    <div>上次系统动作：{p.last_system_action || p.last_exit_plain || p.last_exit_reason || '暂无'}</div>
                    <div>滚仓：{p.roll_layer || 0}/1 · 状态 {p.roll_status || 'state_incomplete'} · 成交价 {p.roll_price ? `$${fmt(p.roll_price, 4)}` : '-'}</div>
                    <div>最高浮盈：${fmt(p.max_floating_pnl || 0)} · {p.roll_enabled ? '允许滚仓观察' : '暂不滚仓'}</div>
                    {p.roll_block_reason && <div>滚仓阻断：{p.roll_block_reason}</div>}
                  </div>
                </div>
              </div>
            ))}
          </div>
        )}
      </div>

      <DecisionPanel panel={selectedRow?.decision_panel} />

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
                    <td style={{ color: sideColor(t.side) }}>{sideText(t.side)}</td>
                    <td>{fmtValue(t.qty ?? t.quantity, 6)}</td>
                    <td>{t.entry_price ? `$${fmtValue(t.entry_price, 4)}` : '-'}</td>
                    <td>{t.exit_price ? `$${fmtValue(t.exit_price, 4)}` : '-'}</td>
                    <td style={pnlColor(t.pnl)}>{Number(t.pnl || 0) >= 0 ? '+' : ''}${fmt(t.pnl)}</td>
                    <td style={pnlColor(t.pnl_pct)}>{t.pnl_pct != null ? `${fmtValue(t.pnl_pct)}%` : '-'}</td>
                    <td>{t.score_at_entry != null ? `${t.grade_at_entry ? `${t.grade_at_entry} ` : ''}${fmt(t.score_at_entry, 1)}` : '-'}</td>
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
