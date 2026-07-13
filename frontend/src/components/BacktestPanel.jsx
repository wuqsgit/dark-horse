import React, { useEffect, useState } from 'react';

async function apiGet(path) {
  const res = await fetch(`/api${path}`);
  return res.json();
}

async function apiPost(path, body = {}) {
  const res = await fetch(`/api${path}`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(body),
  });
  return res.json();
}

function pct(value, digits = 2) {
  if (value == null || Number.isNaN(Number(value))) return '-';
  const n = Number(value) * 100;
  return `${n >= 0 ? '+' : ''}${n.toFixed(digits)}%`;
}

function num(value, digits = 2) {
  if (value == null || Number.isNaN(Number(value))) return '-';
  return Number(value).toFixed(digits);
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

function tone(value) {
  const n = Number(value || 0);
  if (n > 0) return { color: '#22c55e' };
  if (n < 0) return { color: '#ef4444' };
  return { color: '#9ca3af' };
}

function issueText(value) {
  if (value === 'entry_confirmation') return '开仓确认不足';
  if (value === 'early_exit') return '平仓过早';
  return value || '-';
}

function Metric({ label, value, detail, color }) {
  return (
    <div className="bt-card">
      <h3>{label}</h3>
      <div className="value" style={color || {}}>{value}</div>
      {detail && <div className="detail">{detail}</div>}
    </div>
  );
}

export default function BacktestPanel() {
  const [tab, setTab] = useState('overview');
  const [data, setData] = useState(null);
  const [loading, setLoading] = useState(true);
  const [running, setRunning] = useState(false);
  const [error, setError] = useState(null);
  const [actionDetails, setActionDetails] = useState({});

  const load = async () => {
    try {
      setError(null);
      const summary = await apiGet('/backtest/summary');
      setData(summary);
    } catch (e) {
      setError(e.message);
    } finally {
      setLoading(false);
    }
  };

  useEffect(() => {
    load();
    const id = setInterval(load, 30000);
    return () => clearInterval(id);
  }, []);

  const runReview = async () => {
    setRunning(true);
    try {
      await apiPost('/policy/review/run');
      await load();
    } finally {
      setRunning(false);
    }
  };

  const runExitReview = async () => {
    setRunning(true);
    try {
      await apiPost('/policy/exit-review/run');
      await load();
    } finally {
      setRunning(false);
    }
  };

  const overview = data?.overview || {};
  const exitReviews = data?.exit_reviews || [];
  const exitSummaries = data?.exit_summaries || [];
  const entryReviews = data?.entry_reviews || [];
  const entrySummaries = data?.entry_summaries || [];
  const entryStatus = data?.entry_review_status || {};
  const tradeReviews = data?.trade_reviews || [];
  const tradeReviewSummaries = data?.trade_review_summaries || [];

  const toggleActions = async (positionTradeId) => {
    if (actionDetails[positionTradeId]) {
      setActionDetails((current) => ({ ...current, [positionTradeId]: null }));
      return;
    }
    const result = await apiGet(`/policy-loop/positions/${encodeURIComponent(positionTradeId)}/actions`);
    setActionDetails((current) => ({ ...current, [positionTradeId]: result.actions || [] }));
  };

  if (loading) return <div className="trading-section">加载策略闭环数据...</div>;
  if (error) return <div className="trading-section" style={{ color: '#ef4444' }}>加载失败: {error}</div>;

  return (
    <div className="backtest-panel">
      <div style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'center', gap: 12, marginBottom: 16 }}>
        <div>
          <h2 style={{ margin: 0, fontSize: 18 }}>策略闭环</h2>
          <div style={{ color: '#94a3b8', fontSize: 12, marginTop: 4 }}>
            最近复盘: {timeText(data?.latest_run)} · 生成: {timeText(data?.generated_at)}
          </div>
        </div>
        <button
          onClick={runReview}
          disabled={running}
          style={{ background: '#f59e0b', color: '#111827', border: 0, borderRadius: 6, padding: '8px 14px', fontWeight: 700, cursor: running ? 'wait' : 'pointer' }}
        >
          {running ? '复盘中...' : '立即复盘并自动生效'}
        </button>
        <button
          onClick={runExitReview}
          disabled={running}
          style={{ background: '#22c55e', color: '#04130a', border: 0, borderRadius: 6, padding: '8px 14px', fontWeight: 700, cursor: running ? 'wait' : 'pointer' }}
        >
          {running ? '复盘中...' : '复盘平仓'}
        </button>
      </div>

      <div className="nav" style={{ marginBottom: 16 }}>
        {[
          ['overview', '闭环总览'],
          ['tradeReview', '完整交易复盘'],
          ['categoryReview', '分类优化建议'],
        ].map(([key, label]) => (
          <button key={key} className={tab === key ? 'active' : ''} onClick={() => setTab(key)}>{label}</button>
        ))}
      </div>

      {tab === 'overview' && (
        <div style={{ display: 'flex', flexDirection: 'column', gap: 12 }}>
          <div className="backtest-grid">
            <Metric label="样本动作" value={overview.samples || 0} detail="scan / block / open / close" />
            <Metric label="24h 平均收益" value={pct(overview.avg_return_24h)} color={tone(overview.avg_return_24h)} />
            <Metric label="72h 平均收益" value={pct(overview.avg_return_72h)} color={tone(overview.avg_return_72h)} />
            <Metric label="平均最大浮盈" value={pct(overview.avg_mfe)} color={tone(overview.avg_mfe)} />
            <Metric label="平均最大回撤" value={pct(overview.avg_mae)} color={tone(overview.avg_mae)} />
            <Metric label="波段捕获率" value={pct(overview.trend_capture_ratio)} />
            <Metric label="错过大波段" value={overview.missed_big_move_count || 0} detail="被拦截后仍走出趋势" />
            <Metric label="过早平仓" value={overview.early_exit_count || 0} detail="平仓后继续向原方向运行" />
            <Metric label="小盈利过早平仓" value={overview.small_profit_exit_count || 0} />
            <Metric label="误拦截" value={overview.bad_block_count || 0} />
            <Metric label="有效拦截" value={overview.good_block_count || 0} />
          </div>
          <div className="trading-section">
            <h3>闭环状态</h3>
            <div style={{ color: '#cbd5e1', fontSize: 13, lineHeight: 1.8 }}>
              旧回测已下线。当前页面基于真实仓位展示开仓事实、开仓质量、后续走势、平仓事实和分类建议。
            </div>
          </div>
        </div>
      )}

      {tab === 'tradeReview' && (
        <div className="trading-section">
          <h3>完整交易复盘</h3>
          <div className="review-table-wrap">
          <table className="trade-table review-table trade-review-table">
            <colgroup>
              <col style={{ width: '9%' }} />
              <col style={{ width: '7%' }} />
              <col style={{ width: '6%' }} />
              <col style={{ width: '23%' }} />
              <col style={{ width: '18%' }} />
              <col style={{ width: '12%' }} />
              <col style={{ width: '10%' }} />
              <col style={{ width: '12%' }} />
              <col style={{ width: '3%' }} />
            </colgroup>
            <thead>
              <tr>
                <th>平仓时间</th><th>币种</th><th>盈亏</th><th>开仓条件</th><th>平仓条件</th>
                <th>平仓后走势</th><th>联合结论</th><th>优化建议</th><th>证据</th>
              </tr>
            </thead>
            <tbody>
              {tradeReviews.map((r) => (
                <React.Fragment key={r.position_trade_id}>
                  <tr>
                    <td style={{ whiteSpace: 'nowrap' }}>{timeText(r.exit_time)}</td>
                    <td style={{ fontWeight: 700 }}>{r.symbol}<div style={{ color: '#64748b', fontSize: 11 }}>{r.category || '-'}</div></td>
                    <td style={tone(r.net_pnl)}>{num(r.net_pnl)}U<div>{pct(r.pnl_pct)}</div></td>
                    <td style={{ color: '#cbd5e1', lineHeight: 1.6 }}>{r.entry_condition}</td>
                    <td style={{ color: '#cbd5e1', lineHeight: 1.6 }}>{r.exit_condition}</td>
                    <td style={{ lineHeight: 1.6 }}>
                      <div>1h {pct(r.return_1h)} · 4h {pct(r.return_4h)}</div>
                      <div>12h {pct(r.return_12h)} · 24h {pct(r.return_24h)}</div>
                      <div style={tone(r.post_mfe)}>最大有利 {pct(r.post_mfe)}</div>
                      <div style={tone(r.post_mae)}>最大不利 {pct(r.post_mae)}</div>
                    </td>
                    <td style={{ fontWeight: 700 }}>{r.conclusion}</td>
                    <td style={{ color: '#94a3b8', lineHeight: 1.6 }}>{r.recommendation}</td>
                    <td><button onClick={() => toggleActions(r.position_trade_id)}>{actionDetails[r.position_trade_id] ? '收起' : '查看'}</button></td>
                  </tr>
                  {actionDetails[r.position_trade_id] && (
                    <tr><td colSpan={9} style={{ background: '#0b1220', color: '#94a3b8' }}>
                      {actionDetails[r.position_trade_id].length === 0 ? '暂无可用动作证据' : actionDetails[r.position_trade_id].map((a) => (
                        <div key={a.action_id || a.id}>{timeText(a.time)} · {a.action_type} · {a.action_result} · {a.reason_text || a.reason_code || '-'}</div>
                      ))}
                    </td></tr>
                  )}
                </React.Fragment>
              ))}
              {tradeReviews.length === 0 && <tr><td colSpan={9} style={{ textAlign: 'center', color: '#6b7280' }}>暂无完整交易复盘</td></tr>}
            </tbody>
          </table>
          </div>
        </div>
      )}

      {tab === 'categoryReview' && (
        <div className="trading-section">
          <h3>分类优化建议</h3>
          <div className="review-table-wrap">
          <table className="trade-table review-table category-review-table">
            <colgroup>
              <col style={{ width: '9%' }} />
              <col style={{ width: '12%' }} />
              <col style={{ width: '22%' }} />
              <col style={{ width: '13%' }} />
              <col style={{ width: '20%' }} />
              <col style={{ width: '24%' }} />
            </colgroup>
            <thead><tr><th>优先级</th><th>策略 / 分类</th><th>统计证据</th><th>代表币种</th><th>结论</th><th>建议</th></tr></thead>
            <tbody>
              {tradeReviewSummaries.map((r) => (
                <tr key={`${r.strategy_source}-${r.category}-${r.issue_type}`}>
                  <td style={{ color: r.priority === '急需修复' ? '#ef4444' : '#fbbf24', fontWeight: 800 }}>{r.priority || '-'}</td>
                  <td>
                    <div style={{ fontWeight: 800 }}>{r.strategy_source || '-'}</div>
                    <div style={{ color: '#94a3b8' }}>{r.category || '-'}</div>
                    <div style={{ color: '#cbd5e1', marginTop: 4 }}>{issueText(r.issue_type)}</div>
                  </td>
                  <td style={{ lineHeight: 1.65 }}>
                    <div>{r.issue_count || 0}/{r.sample_size || 0} 笔 · {pct(r.issue_rate)}</div>
                    <div style={tone(r.total_pnl)}>问题样本盈亏 {num(r.total_pnl)}U</div>
                    <div>持仓 MFE {pct(r.avg_mfe)} · MAE {pct(r.avg_mae)}</div>
                    <div>平仓后 MFE {pct(r.avg_post_mfe)}</div>
                  </td>
                  <td style={{ color: '#cbd5e1' }}>{(r.representative_symbols || []).join('、') || '-'}</td>
                  <td style={{ color: '#cbd5e1', lineHeight: 1.65 }}>{r.conclusion}</td>
                  <td style={{ color: '#94a3b8', lineHeight: 1.65 }}>{r.recommendation}</td>
                </tr>
              ))}
              {tradeReviewSummaries.length === 0 && <tr><td colSpan={6} style={{ textAlign: 'center', color: '#6b7280' }}>当前没有达到样本与集中度门槛的问题</td></tr>}
            </tbody>
          </table>
          </div>
        </div>
      )}

      {tab === 'entrySummary' && (
        <div style={{ display: 'flex', flexDirection: 'column', gap: 12 }}>
          <div className="backtest-grid">
            <Metric label="开仓样本" value={entryStatus.total || 0} />
            <Metric label="已有结论" value={entryStatus.reviewed || 0} />
            <Metric label="待观察" value={entryStatus.pending || 0} />
            <Metric label="历史指标缺失" value={entryStatus.missing_snapshot || 0} />
          </div>
          <div className="trading-section">
            <h3>开仓事实</h3>
            <table className="trade-table">
              <thead><tr><th>时间</th><th>币种</th><th>方向</th><th>分类 / 模板</th><th>开仓原因</th><th>状态</th><th>结果</th><th>最大浮盈</th><th>最大回撤</th><th>结论</th><th>证据</th></tr></thead>
              <tbody>
                {entryReviews.map((r) => (
                  <React.Fragment key={r.position_trade_id}>
                    <tr>
                      <td style={{ whiteSpace: 'nowrap' }}>{timeText(r.entry_time)}</td>
                      <td style={{ fontWeight: 700 }}>{r.symbol}</td>
                      <td>{r.side || '-'}</td>
                      <td>{r.category || '-'} / {r.entry_template || '-'}</td>
                      <td style={{ minWidth: 360, color: '#cbd5e1' }}>{r.entry_reason_text || '历史开仓指标未记录'}</td>
                      <td>{r.position_status === 'open' ? '持有中' : '已平仓'}</td>
                      <td style={tone(r.position_status === 'open' ? r.return_now : r.pnl_pct)}>{pct(r.position_status === 'open' ? r.return_now : r.pnl_pct)}</td>
                      <td style={tone(r.max_favorable_return)}>{pct(r.max_favorable_return)}</td>
                      <td style={tone(r.max_adverse_return)}>{pct(r.max_adverse_return)}</td>
                      <td title={r.review_reason || ''}>{r.review_label || 'pending'}</td>
                      <td><button onClick={() => toggleActions(r.position_trade_id)} title="查看该仓位动作证据">{actionDetails[r.position_trade_id] ? '收起' : '查看'}</button></td>
                    </tr>
                    {actionDetails[r.position_trade_id] && (
                      <tr><td colSpan={11} style={{ background: '#0b1220', color: '#94a3b8' }}>
                        {actionDetails[r.position_trade_id].length === 0 ? '最近5天没有可用动作证据' : actionDetails[r.position_trade_id].map((a) => (
                          <div key={a.action_id || a.id}>{timeText(a.time)} · {a.action_type} · {a.action_result} · {a.reason_text || a.reason_code || '-'}</div>
                        ))}
                      </td></tr>
                    )}
                  </React.Fragment>
                ))}
                {entryReviews.length === 0 && <tr><td colSpan={11} style={{ textAlign: 'center', color: '#6b7280' }}>暂无开仓总结</td></tr>}
              </tbody>
            </table>
          </div>
          <div className="trading-section">
            <h3>分类建议</h3>
            <table className="trade-table">
              <thead><tr><th>策略</th><th>分类</th><th>模板</th><th>样本</th><th>合理</th><th>偏早</th><th>追高</th><th>条件错误</th><th>平均浮盈</th><th>平均回撤</th><th>建议</th></tr></thead>
              <tbody>
                {entrySummaries.map((r) => (
                  <tr key={r.summary_id}>
                    <td>{r.strategy_source || '-'}</td><td>{r.category || '-'}</td><td>{r.entry_template || '-'}</td><td>{r.sample_size || 0}</td>
                    <td>{r.reasonable_count || 0}</td><td>{r.early_count || 0}</td><td>{r.chased_count || 0}</td><td>{r.bad_condition_count || 0}</td>
                    <td style={tone(r.avg_mfe)}>{pct(r.avg_mfe)}</td><td style={tone(r.avg_mae)}>{pct(r.avg_mae)}</td>
                    <td style={{ minWidth: 360, color: r.action_type === 'improve' ? '#fbbf24' : '#9ca3af' }}>{r.recommendation}</td>
                  </tr>
                ))}
                {entrySummaries.length === 0 && <tr><td colSpan={11} style={{ textAlign: 'center', color: '#6b7280' }}>样本不足，暂不生成分类建议</td></tr>}
              </tbody>
            </table>
          </div>
        </div>
      )}

      {tab === 'exitFacts' && (
        <div className="trading-section">
          <h3>平仓事实</h3>
          <table className="trade-table">
            <thead>
              <tr>
                <th>时间</th><th>币种</th><th>策略</th><th>分类</th><th>平仓原因</th><th>盈亏</th><th>持仓</th><th>后续最大浮盈</th><th>后续最大回撤</th><th>标签</th><th>事实结论</th>
              </tr>
            </thead>
            <tbody>
              {exitReviews.map((r) => (
                <tr key={r.position_trade_id || r.id}>
                  <td style={{ whiteSpace: 'nowrap' }}>{timeText(r.exit_time)}</td>
                  <td style={{ fontWeight: 700 }}>{r.symbol}</td>
                  <td>{r.strategy_source || '-'}</td>
                  <td>{r.category || '-'}</td>
                  <td style={{ maxWidth: 260, color: '#cbd5e1' }}>{r.exit_reason || '-'}</td>
                  <td style={tone(r.net_pnl)}>{num(r.net_pnl)}U</td>
                  <td>{r.holding_minutes != null ? `${num(r.holding_minutes, 0)}m` : '-'}</td>
                  <td style={tone(r.max_favorable_return)}>{pct(r.max_favorable_return)}</td>
                  <td style={tone(r.max_adverse_return)}>{pct(r.max_adverse_return)}</td>
                  <td>{r.review_label || '-'}</td>
                  <td style={{ maxWidth: 420, color: '#9ca3af' }}>{r.review_summary || '-'}</td>
                </tr>
              ))}
              {exitReviews.length === 0 && <tr><td colSpan={11} style={{ textAlign: 'center', color: '#6b7280' }}>暂无平仓事实复盘</td></tr>}
            </tbody>
          </table>
        </div>
      )}

      {tab === 'exitSummary' && (
        <div className="trading-section">
          <h3>平仓总结</h3>
          <table className="trade-table">
            <thead>
              <tr>
                <th>策略</th><th>分类</th><th>平仓原因</th><th>样本</th><th>总盈亏</th><th>平均后续浮盈</th><th>有效</th><th>过早</th><th>噪音小亏</th><th>小盈过早</th><th>结论</th><th>总结</th>
              </tr>
            </thead>
            <tbody>
              {exitSummaries.map((r) => (
                <tr key={r.summary_id || `${r.strategy_source}-${r.exit_reason}`}>
                  <td>{r.strategy_source || '-'}</td>
                  <td>{r.category || '-'}</td>
                  <td style={{ maxWidth: 280, color: '#cbd5e1' }}>{r.exit_reason || '-'}</td>
                  <td>{r.sample_size || 0}</td>
                  <td style={tone(r.total_pnl)}>{num(r.total_pnl)}U</td>
                  <td style={tone(r.avg_mfe_after_exit)}>{pct(r.avg_mfe_after_exit)}</td>
                  <td>{r.good_exit_count || 0}</td>
                  <td>{r.early_exit_count || 0}</td>
                  <td>{r.noise_loss_exit_count || 0}</td>
                  <td>{r.small_profit_exit_count || 0}</td>
                  <td style={{ color: r.action_type === 'improve' ? '#ef4444' : r.action_type === 'keep' ? '#22c55e' : '#fbbf24', fontWeight: 700 }}>{r.conclusion || r.action_type}</td>
                  <td style={{ minWidth: 360, color: '#9ca3af' }}>{r.summary_text || '-'}</td>
                </tr>
              ))}
              {exitSummaries.length === 0 && <tr><td colSpan={12} style={{ textAlign: 'center', color: '#6b7280' }}>暂无平仓总结</td></tr>}
            </tbody>
          </table>
        </div>
      )}
    </div>
  );
}
