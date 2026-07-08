import React, { useEffect, useMemo, useState } from 'react';

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
  const categories = data?.categories || [];
  const reviews = data?.reviews || [];
  const candidates = data?.candidates || [];
  const versions = data?.versions || [];
  const actions = data?.actions || [];
  const exitReviews = data?.exit_reviews || [];
  const exitSummaries = data?.exit_summaries || [];

  const issueRows = useMemo(() => reviews
    .filter((r) => (r.bad_block_count || 0) > 0 || (r.early_exit_count || 0) > 0 || (r.small_profit_exit_count || 0) > 0)
    .slice(0, 80), [reviews]);
  const diagnosticRows = issueRows.length > 0 ? issueRows : reviews.slice(0, 80);

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
          ['categories', '分类表现'],
          ['issues', '问题诊断'],
          ['policies', '自动策略'],
          ['actions', '动作流水'],
          ['exitFacts', '平仓事实'],
          ['exitSummary', '平仓总结'],
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
            <Metric label="自动生效版本" value={versions.filter((v) => v.status === 'active').length} />
          </div>
          <div className="trading-section">
            <h3>闭环状态</h3>
            <div style={{ color: '#cbd5e1', fontSize: 13, lineHeight: 1.8 }}>
              旧回测已下线。当前页面直接展示真实动作的后续结果：收益、最大浮盈、最大回撤、错过大波段、过早平仓、自动生效策略和回滚状态。
            </div>
          </div>
        </div>
      )}

      {tab === 'categories' && (
        <div className="trading-section">
          <h3>分类表现</h3>
          <table className="trade-table">
            <thead>
              <tr>
                <th>类别</th><th>样本</th><th>24h收益</th><th>72h收益</th><th>最大浮盈</th><th>最大回撤</th><th>波段捕获</th><th>错过</th><th>过早平仓</th>
              </tr>
            </thead>
            <tbody>
              {categories.map((r) => (
                <tr key={r.category || 'none'}>
                  <td style={{ fontWeight: 700 }}>{r.category || '-'}</td>
                  <td>{r.samples || 0}</td>
                  <td style={tone(r.avg_return_24h)}>{pct(r.avg_return_24h)}</td>
                  <td style={tone(r.avg_return_72h)}>{pct(r.avg_return_72h)}</td>
                  <td style={tone(r.avg_mfe)}>{pct(r.avg_mfe)}</td>
                  <td style={tone(r.avg_mae)}>{pct(r.avg_mae)}</td>
                  <td>{pct(r.trend_capture_ratio)}</td>
                  <td>{r.missed_big_move_count || 0}</td>
                  <td>{r.early_exit_count || 0}</td>
                </tr>
              ))}
              {categories.length === 0 && <tr><td colSpan={9} style={{ textAlign: 'center', color: '#6b7280' }}>暂无分类样本</td></tr>}
            </tbody>
          </table>
        </div>
      )}

      {tab === 'issues' && (
        <div className="trading-section">
          <h3>问题诊断</h3>
          <table className="trade-table">
            <thead>
              <tr>
                <th>类别</th><th>目标</th><th>规则/原因</th><th>样本</th><th>24h收益</th><th>最大浮盈</th><th>误拦截</th><th>过早平仓</th><th>诊断</th>
              </tr>
            </thead>
            <tbody>
              {diagnosticRows.map((r) => (
                <tr key={r.review_id}>
                  <td>{r.category || '-'}</td>
                  <td>{r.target_type}</td>
                  <td style={{ maxWidth: 320, color: '#cbd5e1' }}>{r.target_name}</td>
                  <td>{r.sample_size || 0}</td>
                  <td style={tone(r.avg_return)}>{pct(r.avg_return)}</td>
                  <td style={tone(r.avg_mfe)}>{pct(r.avg_mfe)}</td>
                  <td>{r.bad_block_count || 0}</td>
                  <td>{r.early_exit_count || 0}</td>
                  <td style={{ color: '#fbbf24' }}>{r.diagnosis}</td>
                </tr>
              ))}
              {diagnosticRows.length === 0 && <tr><td colSpan={9} style={{ textAlign: 'center', color: '#6b7280' }}>暂无诊断样本</td></tr>}
            </tbody>
          </table>
          {issueRows.length === 0 && diagnosticRows.length > 0 && (
            <div style={{ marginTop: 10, color: '#94a3b8', fontSize: 12 }}>
              当前样本没有触发误拦截、过早平仓或小盈利早平阈值，所以上面展示的是常规诊断行。
            </div>
          )}
        </div>
      )}

      {tab === 'policies' && (
        <div style={{ display: 'flex', flexDirection: 'column', gap: 12 }}>
          <div className="trading-section">
            <h3>自动策略</h3>
            {candidates.length === 0 && (
              <div style={{ border: '1px solid #243246', background: '#0b1220', borderRadius: 8, padding: 12, color: '#cbd5e1', fontSize: 13, lineHeight: 1.7, marginBottom: 12 }}>
                暂无自动生效策略。当前复盘样本没有达到自动改规则阈值：误拦截为 0、过早平仓为 0、小盈利过早平仓为 0。系统会继续记录后续动作，一旦某个原因连续误杀大波段或导致早平，会自动生成并生效策略。
              </div>
            )}
            <table className="trade-table">
              <thead>
                <tr><th>状态</th><th>目标</th><th>建议</th><th>样本</th><th>预期改善</th><th>原因</th></tr>
              </thead>
              <tbody>
                {candidates.map((c) => (
                  <tr key={c.id}>
                    <td><span className={`policy-status ${c.status}`}>{c.status}</span></td>
                    <td>{c.target}</td>
                    <td style={{ fontWeight: 700, color: '#cbd5e1' }}>{c.title}</td>
                    <td>{c.sample_size || 0}</td>
                    <td style={tone(c.expected_delta)}>{pct(c.expected_delta)}</td>
                    <td style={{ minWidth: 280, color: '#9ca3af' }}>{c.summary}</td>
                  </tr>
                ))}
                {candidates.length === 0 && <tr><td colSpan={6} style={{ textAlign: 'center', color: '#6b7280' }}>暂无自动策略</td></tr>}
              </tbody>
            </table>
          </div>
          <div className="trading-section">
            <h3>策略版本</h3>
            <table className="trade-table">
              <thead>
                <tr><th>状态</th><th>类别</th><th>目标</th><th>版本</th><th>激活时间</th></tr>
              </thead>
              <tbody>
                {versions.map((v) => (
                  <tr key={v.version_id}>
                    <td>{v.status}</td>
                    <td>{v.category || '-'}</td>
                    <td>{v.target_type}</td>
                    <td style={{ fontSize: 11 }}>{v.version_id}</td>
                    <td>{timeText(v.activated_at || v.created_at)}</td>
                  </tr>
                ))}
                {versions.length === 0 && <tr><td colSpan={5} style={{ textAlign: 'center', color: '#6b7280' }}>暂无策略版本</td></tr>}
              </tbody>
            </table>
          </div>
        </div>
      )}

      {tab === 'actions' && (
        <div className="trading-section">
          <h3>动作流水</h3>
          <table className="trade-table">
            <thead>
              <tr>
                <th>时间</th><th>币种</th><th>类别</th><th>动作</th><th>结果</th><th>原因</th><th>24h</th><th>最大浮盈</th><th>最大回撤</th><th>标签</th>
              </tr>
            </thead>
            <tbody>
              {actions.map((a) => (
                <tr key={a.action_id}>
                  <td style={{ whiteSpace: 'nowrap' }}>{timeText(a.time)}</td>
                  <td style={{ fontWeight: 700 }}>{a.symbol}</td>
                  <td>{a.category || '-'}</td>
                  <td>{a.action_type}</td>
                  <td>{a.action_result}</td>
                  <td style={{ maxWidth: 320, color: '#9ca3af' }}>{a.reason_text || '-'}</td>
                  <td style={tone(a.return_24h)}>{pct(a.return_24h)}</td>
                  <td style={tone(a.max_favorable_return)}>{pct(a.max_favorable_return)}</td>
                  <td style={tone(a.max_adverse_return)}>{pct(a.max_adverse_return)}</td>
                  <td>
                    {a.missed_big_move ? '错过 ' : ''}
                    {a.early_exit ? '过早 ' : ''}
                    {a.bad_block ? '误拦截 ' : ''}
                    {a.good_block ? '有效拦截' : ''}
                  </td>
                </tr>
              ))}
              {actions.length === 0 && <tr><td colSpan={10} style={{ textAlign: 'center', color: '#6b7280' }}>暂无动作流水</td></tr>}
            </tbody>
          </table>
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
