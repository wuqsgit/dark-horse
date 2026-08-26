import React, { useCallback, useEffect, useRef, useState } from 'react';
import {
  fetchTradingAccounts,
  fetchTradingAccountsStatus,
  fetchTradingDecisions,
  fetchTradingHistory,
  fetchTradingRuntimeStatus,
} from '../api/tradingData';
import { adminFetch } from '../api/adminFetch';
import TradingAccountManager from './TradingAccountManager';
import {
  advanceHistoryNavigation,
  createHistoryNavigation,
  emptyAccountScopedTradingState,
  findSelectedAccount,
  finishLatestRequest,
  invalidateLatestRequest,
  isLatestRequest,
  normalizeSelectedAccount,
  retreatHistoryNavigation,
  startLatestRequest,
  strategySourcesLabel,
} from './liveTradingAccountSelection';

const TRADES_PER_PAGE = 20;
const EMPTY_HISTORY_PAGE = { items: [], next_cursor: null, stats: {} };

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
  if (v === 'alpha') return 'Alpha 策略';
  if (v === 'normal') return '普通策略';
  return '-';
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

function DecisionPanel({ panel, loading, error }) {
  if (loading) return <div className="trading-section">加载交易决策...</div>;
  if (error) {
    return <div className="trading-section" style={{ color: '#fbbf24' }}>{error}</div>;
  }

  const reasons = panel?.top_reasons || [];
  const recent = panel?.recent || [];
  const latestDecision = recent[0];
  const lastExecutionTime = panel?.last_execution_time || panel?.latest_time;
  const lastExecutionText = lastExecutionTime ? timeText(lastExecutionTime) : '暂无记录';

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
            <div className="reason-row" key={`${r.reason}-${i}`}><span>{r.plain || r.reason}</span><b>{r.count} 次</b></div>
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

function RuntimeDiagnostics({ diagnostics }) {
  if (!diagnostics) return null;
  const issues = diagnostics.issues || [];
  const state = diagnostics.status || 'degraded';
  const stateText = {
    healthy: '运行正常，可以开仓',
    degraded: '部分能力降级，请留意',
    blocked: '存在异常，当前可能无法开仓',
  }[state] || state;
  return (
    <div className={`trading-section runtime-diagnostics runtime-${state}`} role="status">
      <div className="runtime-diagnostics-header">
        <div>
          <h3>实盘运行诊断</h3>
          <div className="plain-meta">
            最后检查：{timeText(diagnostics.checked_at)} · 最后策略执行：{timeText(diagnostics.last_execution_at)}
          </div>
        </div>
        <span className={`runtime-state runtime-state-${state}`}>{stateText}</span>
      </div>
      <div className="runtime-scan-times">
        <span>普通评分：{timeText(diagnostics.normal_scan_at)}</span>
        <span>Alpha 评分：{timeText(diagnostics.alpha_scan_at)}</span>
      </div>
      {issues.length === 0 ? (
        <div className="runtime-healthy-text">账户连接、行情采集、策略引擎和交易循环均未发现阻断异常。</div>
      ) : (
        <div className="runtime-issue-list">
          {issues.map((issue, index) => (
            <div className={`runtime-issue runtime-issue-${issue.severity}`} key={`${issue.code}-${index}`}>
              <div className="runtime-issue-title">
                <strong>{issue.title}</strong>
                <span>{issue.severity === 'error' ? '阻断' : '提醒'}</span>
              </div>
              <div>{issue.message}</div>
              {issue.observed_at && <div className="plain-meta">发生时间：{timeText(issue.observed_at)}</div>}
            </div>
          ))}
        </div>
      )}
    </div>
  );
}

export default function LiveTrading() {
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState(null);
  const [accountWarning, setAccountWarning] = useState(null);
  const [snapshotWarning, setSnapshotWarning] = useState(null);
  const [runtimeWarning, setRuntimeWarning] = useState(null);
  const [tradeFilter, setTradeFilter] = useState('all');
  const [tradeSymbol, setTradeSymbol] = useState('');
  const [tradeDirection, setTradeDirection] = useState('all');
  const [switching, setSwitching] = useState(null);
  const [toast, setToast] = useState(null);
  const [accountsData, setAccountsData] = useState({ accounts: [], summary: {} });
  const [accountConfigs, setAccountConfigs] = useState([]);
  const [runtimeData, setRuntimeData] = useState({ accounts: [] });
  const [selectedAccount, setSelectedAccount] = useState(null);
  const [historyPage, setHistoryPage] = useState(EMPTY_HISTORY_PAGE);
  const [historyLoading, setHistoryLoading] = useState(false);
  const [historyError, setHistoryError] = useState(null);
  const [historyNavigation, setHistoryNavigation] = useState(createHistoryNavigation());
  const [historyRetryToken, setHistoryRetryToken] = useState(0);
  const [decisionsData, setDecisionsData] = useState(null);
  const [decisionsLoading, setDecisionsLoading] = useState(false);
  const [decisionsError, setDecisionsError] = useState(null);
  const mountedRef = useRef(false);
  const runtimeRequestRef = useRef({
    generation: 0,
    inFlight: false,
    controller: null,
    promise: null,
  });

  const applyAccountSnapshot = useCallback((data) => {
    setAccountsData(data || { accounts: [], summary: {} });
    setError(null);
    if (data?.last_error) {
      setSnapshotWarning(`账户快照刷新失败：${data.last_error}`);
    } else if (data?.fresh === false) {
      setSnapshotWarning('账户快照已过期');
    } else {
      setSnapshotWarning(null);
    }
  }, []);

  const loadRuntimeStatus = useCallback(() => {
    if (runtimeRequestRef.current.inFlight) return runtimeRequestRef.current.promise;
    const currentRequest = runtimeRequestRef.current;

    const startedRequest = startLatestRequest(currentRequest);
    const controller = new AbortController();
    const generation = startedRequest.generation;
    const promise = fetchTradingRuntimeStatus({ signal: controller.signal })
      .then((data) => {
        if (!mountedRef.current || !isLatestRequest(runtimeRequestRef.current, generation)) return;
        setRuntimeData(data || { accounts: [] });
        setRuntimeWarning(null);
      })
      .catch((requestError) => {
        if (
          mountedRef.current
          && requestError.name !== 'AbortError'
          && isLatestRequest(runtimeRequestRef.current, generation)
        ) {
          setRuntimeWarning(`运行诊断刷新失败：${requestError.message}`);
        }
      })
      .finally(() => {
        if (!isLatestRequest(runtimeRequestRef.current, generation)) return;
        runtimeRequestRef.current = {
          ...finishLatestRequest(runtimeRequestRef.current, generation),
          controller: null,
          promise: null,
        };
      });
    runtimeRequestRef.current = {
      ...startedRequest,
      controller,
      promise,
    };
    return promise;
  }, []);

  const refreshAccountData = useCallback(async () => {
    loadRuntimeStatus();
    const [configsResult, snapshotResult] = await Promise.allSettled([
      fetchTradingAccounts(),
      fetchTradingAccountsStatus({ force: true }),
    ]);
    if (configsResult.status === 'fulfilled') {
      setAccountConfigs(configsResult.value.accounts || []);
      setAccountWarning(null);
    } else {
      setAccountWarning(`账户配置刷新失败：${configsResult.reason.message}`);
    }
    if (snapshotResult.status === 'fulfilled') {
      applyAccountSnapshot(snapshotResult.value);
    } else {
      setSnapshotWarning(`账户快照刷新失败：${snapshotResult.reason.message}`);
    }
  }, [applyAccountSnapshot, loadRuntimeStatus]);

  useEffect(() => {
    let active = true;
    mountedRef.current = true;
    fetchTradingAccounts()
      .then((data) => {
        if (!active) return;
        setAccountConfigs(data.accounts || []);
        setAccountWarning(null);
      })
      .catch((requestError) => {
        if (active) setAccountWarning(`账户配置加载失败：${requestError.message}`);
      });
    fetchTradingAccountsStatus()
      .then((data) => { if (active) applyAccountSnapshot(data); })
      .catch((requestError) => {
        if (active) setError(`加载实盘数据失败: ${requestError.message}`);
      })
      .finally(() => { if (active) setLoading(false); });
    loadRuntimeStatus();

    const pollLiveData = () => {
      fetchTradingAccountsStatus({ force: true })
        .then((data) => { if (active) applyAccountSnapshot(data); })
        .catch((requestError) => {
          if (active) setSnapshotWarning(`账户快照刷新失败：${requestError.message}`);
        });
      loadRuntimeStatus();
    };
    const id = setInterval(pollLiveData, 30000);
    return () => {
      active = false;
      mountedRef.current = false;
      clearInterval(id);
      runtimeRequestRef.current.controller?.abort();
      runtimeRequestRef.current = {
        ...invalidateLatestRequest(runtimeRequestRef.current),
        controller: null,
        promise: null,
      };
    };
  }, [applyAccountSnapshot, loadRuntimeStatus]);

  useEffect(() => {
    const normalized = normalizeSelectedAccount(selectedAccount, accountsData.accounts || []);
    if (String(normalized) !== String(selectedAccount)) setSelectedAccount(normalized);
  }, [accountsData.accounts, selectedAccount]);

  const selectedRow = findSelectedAccount(selectedAccount, accountsData.accounts || []);
  const positions = selectedRow?.positions || [];
  const stats = historyPage.stats || {};
  const accountSummary = selectedRow || {};
  const runtimeAccount = (runtimeData.accounts || []).find(
    (account) => String(account.account_id) === String(selectedAccount),
  );
  const runtimeDiagnostics = runtimeAccount?.runtime_diagnostics;
  const historyQueryKey = [selectedAccount, tradeFilter, tradeSymbol, tradeDirection].join('|');
  const historyNavigationMatches = historyNavigation.queryKey === historyQueryKey;
  const historyCursor = historyNavigationMatches ? historyNavigation.cursor : null;
  const historyCursorStack = historyNavigationMatches
    ? historyNavigation.historyCursorStack
    : [];
  const warning = [accountWarning, snapshotWarning, runtimeWarning].filter(Boolean).join('；');

  useEffect(() => {
    if (selectedAccount !== null && selectedAccount !== undefined) return;
    const emptyState = emptyAccountScopedTradingState(historyQueryKey);
    setHistoryPage(emptyState.historyPage);
    setHistoryNavigation(emptyState.historyNavigation);
    setHistoryError(emptyState.historyError);
    setHistoryLoading(emptyState.historyLoading);
    setDecisionsData(emptyState.decisionsData);
    setDecisionsError(emptyState.decisionsError);
    setDecisionsLoading(emptyState.decisionsLoading);
  }, [historyQueryKey, selectedAccount]);

  useEffect(() => {
    if (selectedAccount === null || selectedAccount === undefined) return;
    setHistoryNavigation(createHistoryNavigation(historyQueryKey));
    setHistoryPage(EMPTY_HISTORY_PAGE);
    setHistoryError(null);
  }, [historyQueryKey, selectedAccount]);

  useEffect(() => {
    if (selectedAccount === null || selectedAccount === undefined) return undefined;
    const controller = new AbortController();
    let active = true;
    setHistoryLoading(true);
    setHistoryError(null);
    fetchTradingHistory(selectedAccount, {
      cursor: historyCursor || undefined,
      limit: TRADES_PER_PAGE,
      source: tradeFilter === 'all' ? undefined : tradeFilter,
      symbol: tradeSymbol || undefined,
      direction: tradeDirection === 'all' ? undefined : tradeDirection,
    }, { signal: controller.signal })
      .then((data) => {
        if (!active) return;
        setHistoryPage({
          items: Array.isArray(data?.items) ? data.items : [],
          next_cursor: data?.next_cursor || null,
          stats: data?.stats || {},
        });
      })
      .catch((requestError) => {
        if (active && requestError.name !== 'AbortError') {
          setHistoryError(`历史交易加载失败：${requestError.message}`);
        }
      })
      .finally(() => { if (active) setHistoryLoading(false); });
    return () => {
      active = false;
      controller.abort();
    };
  }, [
    selectedAccount,
    tradeFilter,
    tradeSymbol,
    tradeDirection,
    historyCursor,
    historyRetryToken,
  ]);

  useEffect(() => {
    if (selectedAccount === null || selectedAccount === undefined) return undefined;
    const controller = new AbortController();
    let active = true;
    setDecisionsData(null);
    setDecisionsLoading(true);
    setDecisionsError(null);
    fetchTradingDecisions(selectedAccount, { signal: controller.signal })
      .then((data) => { if (active) setDecisionsData(data); })
      .catch((requestError) => {
        if (active && requestError.name !== 'AbortError') {
          setDecisionsError(`交易决策加载失败：${requestError.message}`);
        }
      })
      .finally(() => { if (active) setDecisionsLoading(false); });
    return () => {
      active = false;
      controller.abort();
    };
  }, [selectedAccount]);

  useEffect(() => {
    if (!toast) return undefined;
    const id = setTimeout(() => setToast(null), 3600);
    return () => clearTimeout(id);
  }, [toast]);

  const showToast = (type, message) => {
    setToast({ type, message, id: Date.now() });
  };

  const goToNextHistoryPage = () => {
    if (!historyPage.next_cursor) return;
    setHistoryNavigation((navigation) => advanceHistoryNavigation(
      navigation.queryKey === historyQueryKey
        ? navigation
        : createHistoryNavigation(historyQueryKey),
      historyPage.next_cursor,
    ));
  };

  const goToPreviousHistoryPage = () => {
    if (historyCursorStack.length === 0) return;
    setHistoryNavigation((navigation) => retreatHistoryNavigation(navigation));
  };

  const retryHistoryPage = () => setHistoryRetryToken((token) => token + 1);

  const toggleTrading = async (mode, enabled) => {
    setSwitching(mode);
    setError(null);
    const modeText = mode === 'alpha' ? 'Alpha 交易' : '普通交易';
    try {
      const targets = accountConfigs.filter((item) => String(item.id) === String(selectedAccount));
      if (!targets.length) throw new Error('请先选择具体账户');
      const key = mode === 'alpha' ? 'alpha_trading_enabled' : 'normal_trading_enabled';
      const results = await Promise.all(targets.map(async (account) => {
        const res = await adminFetch(`/api/trading/accounts/${account.id}`, {
          method: 'PATCH', headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ ...account, [key]: enabled }),
        });
        return res.json();
      }));
      const failed = results.find((item) => item.error);
      if (failed) throw new Error(failed.error);
      await refreshAccountData();
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
        <TradingAccountManager accounts={accountConfigs} onChanged={refreshAccountData} />
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
          {warning}{accountsData.age_seconds != null ? `（快照延迟 ${Math.round(accountsData.age_seconds)} 秒）` : ''}
        </div>
      )}
      {toast && (
        <div className={`trade-toast trade-toast-${toast.type}`} role="status">
          <span>{toast.message}</span>
          <button type="button" onClick={() => setToast(null)}>×</button>
        </div>
      )}
      <RuntimeDiagnostics diagnostics={runtimeDiagnostics} />
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
          <div className="stat-card"><div className="stat-label">开仓次数</div><div className="stat-value">{stats.position_count || 0}</div></div>
          <div className="stat-card"><div className="stat-label">已平仓</div><div className="stat-value">{stats.total_cycles || 0}</div></div>
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
                    <div>滚仓：{p.roll_layer || 0}/{p.roll_max_layers || 3} · 状态 {p.roll_status || 'state_incomplete'} · 成交价 {p.roll_price ? `$${fmt(p.roll_price, 4)}` : '-'}</div>
                    <div>最高浮盈：${fmt(p.max_floating_pnl || 0)} · {p.roll_enabled ? '允许滚仓观察' : '暂不滚仓'}</div>
                    {p.strategy_source === 'alpha' && (
                      <>
                        <div>Alpha利润保护：第 {p.alpha_profit_lock_stage || 0} 档 · 历史最高 {fmt(p.max_floating_roi || 0, 2)}% · 当前锁定 {fmt(p.alpha_locked_roi || 0, 2)}%</div>
                        <div>已落袋保护金：${fmt(p.protected_profit || 0)} · 趋势转弱减仓：{p.alpha_stall_protect_price ? `已触发（触发价 $${fmt(p.alpha_stall_protect_price, 4)}）` : '未触发'}</div>
                      </>
                    )}
                    {p.roll_block_reason && <div>滚仓阻断：{p.roll_block_reason}</div>}
                  </div>
                </div>
              </div>
            ))}
          </div>
        )}
      </div>

      <DecisionPanel panel={decisionsData} loading={decisionsLoading} error={decisionsError} />

      <div className="trading-section">
        <div style={{ display: 'flex', alignItems: 'center', justifyContent: 'space-between', gap: 12, flexWrap: 'wrap' }}>
          <h3>历史交易</h3>
          <div className="filters">
            <input
              aria-label="按币种筛选历史交易"
              placeholder="币种，例如 BTCUSDT"
              value={tradeSymbol}
              onChange={(event) => setTradeSymbol(event.target.value.toUpperCase())}
            />
            <select
              aria-label="按方向筛选历史交易"
              value={tradeDirection}
              onChange={(event) => setTradeDirection(event.target.value)}
            >
              <option value="all">全部方向</option>
              <option value="LONG">做多</option>
              <option value="SHORT">做空</option>
            </select>
            <div className="scan-toolbar" style={{ margin: 0 }}>
              <button className={tradeFilter === 'all' ? 'active' : ''} onClick={() => setTradeFilter('all')}>全部</button>
              <button className={tradeFilter === 'normal' ? 'active' : ''} onClick={() => setTradeFilter('normal')}>普通策略</button>
              <button className={tradeFilter === 'alpha' ? 'active' : ''} onClick={() => setTradeFilter('alpha')}>Alpha 策略</button>
            </div>
          </div>
        </div>
        {historyError && (
          <div style={{ color: '#fbbf24', padding: '12px 0' }} role="alert">
            {historyError}
            <button type="button" onClick={retryHistoryPage} disabled={historyLoading} style={{ marginLeft: 10 }}>
              重试
            </button>
          </div>
        )}
        {historyLoading && historyPage.items.length === 0 ? (
          <div style={{ color: '#6b7280', padding: 20, textAlign: 'center' }}>加载历史交易...</div>
        ) : historyPage.items.length === 0 ? (
          <div style={{ color: '#6b7280', padding: 20, textAlign: 'center' }}>暂无历史交易</div>
        ) : (
          <>
            <table className="trade-table">
              <thead>
                <tr><th>币种</th><th>来源</th><th>方向</th><th>数量</th><th>开仓价</th><th>平仓价</th><th>盈亏</th><th>盈亏%</th><th>评分</th><th>时间</th></tr>
              </thead>
              <tbody>
                {historyPage.items.map((t) => (
                  <tr key={`${t.account_id}-${t.symbol}-${t.side}`}>
                    <td style={{ fontWeight: 600, color: '#c9d1d9' }}>
                      {t.symbol}
                      {t.position_count > 1 ? <span className="mini-pill" style={{ marginLeft: 6 }}>合并 {t.position_count}</span> : null}
                      {t.alpha_symbol ? <span className="mini-pill" style={{ marginLeft: 6 }}>{t.alpha_symbol}</span> : null}
                    </td>
                    <td>{strategySourcesLabel(t.strategy_sources)}{t.alpha_profile ? ` · ${alphaProfileText(t.alpha_profile)}` : ''}</td>
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
              <button onClick={goToPreviousHistoryPage} disabled={historyCursorStack.length === 0 || historyLoading}>上一页</button>
              <span style={{ color: '#9ca3af', padding: '4px 8px' }}>{historyCursorStack.length + 1}</span>
              <button onClick={goToNextHistoryPage} disabled={!historyPage.next_cursor || historyLoading || Boolean(historyError)}>下一页</button>
            </div>
          </>
        )}
      </div>
    </div>
  );
}
