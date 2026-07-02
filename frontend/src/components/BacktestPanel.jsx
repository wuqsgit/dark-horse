import React, { useState, useEffect } from 'react';
import { AgGridReact } from 'ag-grid-react';
import 'ag-grid-community/styles/ag-grid.css';
import 'ag-grid-community/styles/ag-theme-alpine.css';

function pctFmt(p) { return p.value == null ? '-' : (p.value * 100).toFixed(2) + '%'; }
function pctColor(p) {
  if (p.value == null) return {};
  return { color: p.value > 0 ? '#22c55e' : p.value < 0 ? '#ef4444' : '#9ca3af' };
}
function winColor(p) {
  if (p.value == null) return {};
  return p.value ? { color: '#22c55e' } : { color: '#ef4444' };
}
function gradeClass(p) { return `grade-${p.value || ''}`; }

async function apiGet(path) {
  const r = await fetch(`/api${path}`);
  return { data: await r.json() };
}

async function apiPost(path, body) {
  const r = await fetch(`/api${path}`, {
    method: 'POST', headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(body),
  });
  return { data: await r.json() };
}

function pnlColor(v) {
  if (v == null) return {};
  return { color: v > 0 ? '#22c55e' : v < 0 ? '#ef4444' : '#9ca3af' };
}

export default function BacktestPanel() {
  const [tab, setTab] = useState('summary');
  const [review, setReview] = useState(null);
  const [reviewLoading, setReviewLoading] = useState(false);
  const [reviewError, setReviewError] = useState(null);
  const [reviewRequested, setReviewRequested] = useState(false);
  const [summary, setSummary] = useState(null);
  const [recentSignals, setRecentSignals] = useState([]);
  const [selectedGrade, setSelectedGrade] = useState('ALL');
  const [factorData, setFactorData] = useState(null);
  const [learningData, setLearningData] = useState(null);
  const [learningLoading, setLearningLoading] = useState(false);

  // 自定义因子本地状态: {name: {weight, source, default, thresholds}}
  const [customFactors, setCustomFactors] = useState({});
  const [weightsDirty, setWeightsDirty] = useState(false);

  useEffect(() => {
    apiGet('/backtest/summary').then(r => setSummary(r.data)).catch(e => console.error(e));
    fetchRecent('ALL');
  }, []);

  const loadWeights = async () => {
    try {
      const r = await apiGet('/backtest/factor_weights');
      if (r.data?.custom_factors) {
        setCustomFactors(r.data.custom_factors);
      }
    } catch (e) { console.error(e); }
  };

  const fetchRecent = (grade) => {
    setSelectedGrade(grade);
    const normalizedGrade = grade === 'ALL' ? 'all' : grade;
    const limit = grade === 'ALL' ? 200 : 50;
    apiGet(`/backtest/signals?grade=${normalizedGrade}&limit=${limit}`)
      .then(r => setRecentSignals(r.data))
      .catch(e => console.error(e));
  };

  useEffect(() => {
    if (tab !== 'review' || review || reviewLoading || reviewRequested) return;
    setReviewRequested(true);
    setReviewLoading(true);
    setReviewError(null);
    apiGet('/backtest/review').then(r => {
      if (r.data?.error) {
        setReview(null);
        setReviewError(r.data.error);
      } else {
        setReview(r.data);
      }
    }).catch(e => {
      setReview(null);
      setReviewError(e.message);
    }).finally(() => setReviewLoading(false));
  }, [tab, review, reviewLoading, reviewRequested]);

  useEffect(() => {
    if (tab !== 'factors' || factorData) return;
    apiGet('/backtest/factor_analysis').then(r => setFactorData(r.data)).catch(e => console.error(e));
    loadWeights();
  }, [tab, factorData]);

  const loadLearning = async () => {
    setLearningLoading(true);
    try {
      const r = await apiGet('/strategy/learning');
      setLearningData(r.data);
    } catch (e) {
      console.error(e);
      setLearningData({ error: e.message, candidates: [], status_counts: {} });
    } finally {
      setLearningLoading(false);
    }
  };

  useEffect(() => {
    if (tab !== 'learning' || learningData || learningLoading) return;
    loadLearning();
  }, [tab, learningData, learningLoading]);

  const changeCandidateStatus = async (id, status) => {
    const r = await apiPost(`/strategy/learning/${id}/status`, {
      status,
      detail: { source: 'backtest_panel' },
    });
    if (r.data?.error) {
      alert('状态更新失败: ' + r.data.error);
      return;
    }
    await loadLearning();
  };

  const addFactor = (factorName, description, disc) => {
    const existing = customFactors[factorName];
    const weight = 5; // 默认5%
    const defaultValue = 50;
    const thresholds = [
      { max: 30, score: 80 },
      { max: 50, score: 65 },
      { max: 70, score: 50 },
      { max: 85, score: 35 },
      { max: 999, score: 20 },
    ];
    setCustomFactors(prev => ({
      ...prev,
      [factorName]: {
        weight: existing?.weight || weight,
        source: factorName,
        mapping: existing?.mapping || 'thresholds',
        default: existing?.default || defaultValue,
        thresholds: existing?.thresholds || thresholds,
        description,
      },
    }));
    setWeightsDirty(true);
  };

  const updateFactorWeight = (name, val) => {
    const w = parseInt(val) || 0;
    setCustomFactors(prev => prev[name] ? { ...prev, [name]: { ...prev[name], weight: w } } : prev);
    setWeightsDirty(true);
  };

  const removeFactor = (name) => {
    setCustomFactors(prev => {
      const { [name]: _, ...rest } = prev;
      return rest;
    });
    setWeightsDirty(true);
  };

  const saveWeights = async () => {
    try {
      const r = await apiPost('/backtest/factor_weights', {
        custom_factors: customFactors,
      });
      if (r.data.status === 'ok') {
        setWeightsDirty(false);
        alert('✅ 因子配置已保存，下次评分轮询生效');
      } else {
        alert('保存失败: ' + (r.data.error || ''));
      }
    } catch (e) {
      alert('保存失败: ' + e.message);
    }
  };

  const grades = summary?.grades || [];
  const totalSignals = grades.reduce((s, g) => s + g.count, 0);

  const columnDefs = [
    { field: 'symbol', headerName: '币种', width: 120, pinned: 'left', cellStyle: { fontWeight: 600 } },
    { field: 'time', headerName: '信号时间', width: 160,
      valueFormatter: p => p.value ? new Date(p.value).toLocaleString('zh-CN') : '' },
    { field: 'grade', headerName: '等级', width: 80, cellClass: gradeClass },
    { field: 'score', headerName: '评分', width: 80, valueFormatter: p => p.value?.toFixed(1) },
    { field: 'price', headerName: '信号价', width: 100,
      valueFormatter: p => p.value ? '$' + p.value.toFixed(p.value < 1 ? 6 : p.value < 100 ? 4 : 2) : '' },
    { field: 'return_12h', headerName: '12h 收益', width: 110, valueFormatter: pctFmt, cellStyle: pctColor },
    { field: 'return_24h', headerName: '24h 收益', width: 110, valueFormatter: pctFmt, cellStyle: pctColor },
    { field: 'win_12h', headerName: '12h 胜', width: 80, cellStyle: winColor, valueFormatter: p => p.value === true ? '✅' : p.value === false ? '❌' : '-' },
    { field: 'win_24h', headerName: '24h 胜', width: 80, cellStyle: winColor, valueFormatter: p => p.value === true ? '✅' : p.value === false ? '❌' : '-' },
  ];

  const renderSummary = () => {
    // 回测概览：显示等级卡片（summary 数据）
    const grades = summary?.grades || [];
    const latestRun = summary?.latest_run;
    const decisionSummary = summary?.decision_summary || {};
    const backtestStatus = summary?.backtest_status || {};
    const stageCounts = Object.fromEntries((decisionSummary.stage_counts || []).map(x => [x.stage, x.count]));
    const resultCounts = Object.fromEntries((decisionSummary.result_counts || []).map(x => [x.result, x.count]));
    if (grades.length > 0) {
      return (
        <>
          {latestRun && <div style={{marginBottom:12,color:'#888',fontSize:12}}>📅 上次运行: {latestRun.replace('T',' ').slice(0,16)}</div>}
          {decisionSummary.latest_run_id && (
            <div className="trading-section" style={{ background: '#111827', marginBottom: 16 }}>
              <div style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'baseline', gap: 12, marginBottom: 10 }}>
                <h3 style={{ fontSize: 14, margin: 0, color: '#38bdf8' }}>策略学习 V1 决策日志</h3>
                <div style={{ fontSize: 11, color: '#6b7280' }}>
                  {decisionSummary.latest_time ? new Date(decisionSummary.latest_time).toLocaleString('zh-CN') : '--'}
                </div>
              </div>
              <div className="backtest-grid" style={{ gridTemplateColumns: 'repeat(auto-fit, minmax(150px, 1fr))', marginBottom: 12 }}>
                <div className="bt-card">
                  <h3>本轮记录</h3>
                  <div className="value">{decisionSummary.total || 0}</div>
                  <div className="detail">run: {(decisionSummary.latest_run_id || '').slice(0, 26)}</div>
                </div>
                <div className="bt-card">
                  <h3>扫描信号</h3>
                  <div className="value">{stageCounts.scan || 0}</div>
                  <div className="detail">进入学习样本池</div>
                </div>
                <div className="bt-card">
                  <h3>候选过滤</h3>
                  <div className="value negative">{stageCounts.candidate_filter || 0}</div>
                  <div className="detail">记录未开仓原因</div>
                </div>
                <div className="bt-card">
                  <h3>计划开仓</h3>
                  <div className="value positive">{resultCounts.planned_open || 0}</div>
                  <div className="detail">进入执行前候选</div>
                </div>
                <div className="bt-card">
                  <h3>执行成功</h3>
                  <div className="value positive">{(resultCounts.opened || 0) + (resultCounts.closed || 0) + (resultCounts.partial_closed || 0)}</div>
                  <div className="detail">实盘动作落库</div>
                </div>
              </div>
              <div style={{ display: 'grid', gridTemplateColumns: 'minmax(0, 1fr) minmax(0, 1.4fr)', gap: 12 }}>
                <div>
                  <div style={{ fontSize: 12, color: '#9ca3af', marginBottom: 6 }}>Top 跳过原因</div>
                  <table className="trade-table">
                    <thead><tr><th>原因</th><th>次数</th></tr></thead>
                    <tbody>
                      {(decisionSummary.top_filter_reasons || []).slice(0, 5).map((r, i) => (
                        <tr key={i}>
                          <td style={{ fontSize: 11, color: '#c9d1d9' }}>{r.reason}</td>
                          <td>{r.count}</td>
                        </tr>
                      ))}
                      {(!decisionSummary.top_filter_reasons || decisionSummary.top_filter_reasons.length === 0) && (
                        <tr><td colSpan={2} style={{ color: '#6b7280', textAlign: 'center' }}>暂无过滤记录</td></tr>
                      )}
                    </tbody>
                  </table>
                </div>
                <div>
                  <div style={{ fontSize: 12, color: '#9ca3af', marginBottom: 6 }}>最近决策</div>
                  <table className="trade-table">
                    <thead><tr><th>币种</th><th>阶段</th><th>结果</th><th>原因</th><th>分数</th></tr></thead>
                    <tbody>
                      {(decisionSummary.recent || []).slice(0, 6).map((r, i) => (
                        <tr key={i}>
                          <td style={{ fontWeight: 600 }}>{r.symbol}</td>
                          <td>{r.decision_stage}</td>
                          <td>{r.decision_result}</td>
                          <td style={{ fontSize: 11, color: '#9ca3af' }}>{r.filter_reason || '-'}</td>
                          <td>{r.composite_score != null ? Number(r.composite_score).toFixed(1) : '-'}</td>
                        </tr>
                      ))}
                      {(!decisionSummary.recent || decisionSummary.recent.length === 0) && (
                        <tr><td colSpan={5} style={{ color: '#6b7280', textAlign: 'center' }}>暂无决策记录</td></tr>
                      )}
                    </tbody>
                  </table>
                </div>
              </div>
            </div>
          )}
          <div className="backtest-grid">
            {grades.filter(g => g.grade).map(g => (
              <div key={g.grade} className="bt-card"
                onClick={() => fetchRecent(g.grade)}
                style={{ cursor: 'pointer', border: selectedGrade === g.grade ? '1px solid #f59e0b' : '1px solid #1f2937' }}>
                <h3>
                  <span className={`grade-${g.grade}`}>{g.grade}</span> 信号 (n={g.count})
                </h3>
                <div className={`value ${g.avg_return_24h >= 0 ? 'positive' : 'negative'}`}>
                  {(g.avg_return_24h * 100).toFixed(2)}%
                </div>
                <div className="detail" style={{ fontSize: 11, lineHeight: 1.6 }}>
                  12h胜率: {(g.win_rate_12h * 100).toFixed(1)}% | 24h胜率: {(g.win_rate_24h * 100).toFixed(1)}%
                </div>
                <div className="detail" style={{ fontSize: 11 }}>
                  24h收效: {(g.avg_return_24h * 100).toFixed(2)}% | 回撤: {(g.avg_drawdown * 100).toFixed(2)}% | 均分: {g.avg_score?.toFixed(1)}
                </div>
              </div>
            ))}
          </div>

          {grades.length > 0 && (
            <div className="bt-table-container">
              <div style={{ marginBottom: 8, color: '#9ca3af', fontSize: 14, display: 'flex', justifyContent: 'space-between', alignItems: 'center', gap: 12 }}>
                <span>
                  最近 <span className={selectedGrade === 'ALL' ? '' : `grade-${selectedGrade}`}>{selectedGrade === 'ALL' ? '全部' : selectedGrade}</span> 信号明细
                </span>
                {selectedGrade !== 'ALL' && (
                  <button
                    onClick={() => fetchRecent('ALL')}
                    style={{ background: '#1f2937', color: '#c9d1d9', border: '1px solid #374151', borderRadius: 6, padding: '4px 10px', fontSize: 12, cursor: 'pointer' }}
                  >
                    查看全部
                  </button>
                )}
              </div>
              <div className="ag-theme-alpine-dark" style={{ height: 400, width: '100%' }}>
                <AgGridReact
                  rowData={recentSignals}
                  columnDefs={columnDefs}
                  animateRows={true}
                  rowHeight={36} headerHeight={36}
                  defaultColDef={{ resizable: true, sortable: true, filter: true }}
                />
              </div>
            </div>
          )}
        </>
      );
    }

    if (!summary) {
      return <div style={{ color: '#6b7280', padding: 40, textAlign: 'center' }}>加载中...</div>;
    }

    const fmtTime = (value) => value ? new Date(value).toLocaleString('zh-CN') : '--';
    return (
      <div className="trading-section" style={{ background: '#111827' }}>
        <div style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'baseline', gap: 12, marginBottom: 14 }}>
          <h3 style={{ margin: 0, color: '#f8fafc', fontSize: 18 }}>暂无成熟回测概览</h3>
          <span style={{ color: '#94a3b8', fontSize: 12 }}>
            {backtestStatus.waiting_for_mature_returns ? '等待收益窗口成熟' : '等待回测产出'}
          </span>
        </div>
        <div style={{
          border: '1px solid #243246',
          borderRadius: 8,
          padding: 14,
          color: '#cbd5e1',
          background: '#0b1220',
          marginBottom: 14,
          lineHeight: 1.7
        }}>
          {backtestStatus.plain || '回测接口已返回，但当前没有可展示的等级收益数据。'}
        </div>
        <div className="backtest-grid" style={{ gridTemplateColumns: 'repeat(auto-fit, minmax(170px, 1fr))', marginBottom: 16 }}>
          <div className="bt-card">
            <h3>成熟回测样本</h3>
            <div className="value">{backtestStatus.backtest_rows || 0}</div>
            <div className="detail">生成等级收益概览后会显示</div>
          </div>
          <div className="bt-card">
            <h3>扫描评分样本</h3>
            <div className="value positive">{backtestStatus.score_count || 0}</div>
            <div className="detail">最新: {fmtTime(backtestStatus.score_max_time)}</div>
          </div>
          <div className="bt-card">
            <h3>最新 1h 行情</h3>
            <div className="value" style={{ fontSize: 16 }}>{fmtTime(backtestStatus.latest_price_time)}</div>
            <div className="detail">用于计算未来收益</div>
          </div>
          <div className="bt-card">
            <h3>复盘记录</h3>
            <div className="value">{backtestStatus.review_rows || 0}</div>
            <div className="detail">最近: {fmtTime(backtestStatus.latest_review_time)}</div>
          </div>
        </div>
        {decisionSummary.latest_run_id && (
          <div style={{ borderTop: '1px solid #243246', paddingTop: 14 }}>
            <div style={{ color: '#38bdf8', fontSize: 14, fontWeight: 700, marginBottom: 10 }}>策略决策日志仍然可用</div>
            <div className="backtest-grid" style={{ gridTemplateColumns: 'repeat(auto-fit, minmax(150px, 1fr))' }}>
              <div className="bt-card">
                <h3>本轮记录</h3>
                <div className="value">{decisionSummary.total || 0}</div>
                <div className="detail">run: {(decisionSummary.latest_run_id || '').slice(0, 26)}</div>
              </div>
              <div className="bt-card">
                <h3>扫描信号</h3>
                <div className="value">{stageCounts.scan || 0}</div>
                <div className="detail">进入学习样本池</div>
              </div>
              <div className="bt-card">
                <h3>候选过滤</h3>
                <div className="value negative">{stageCounts.candidate_filter || 0}</div>
                <div className="detail">记录未开仓原因</div>
              </div>
              <div className="bt-card">
                <h3>计划开仓</h3>
                <div className="value positive">{resultCounts.planned_open || 0}</div>
                <div className="detail">进入执行前候选</div>
              </div>
            </div>
          </div>
        )}
      </div>
    );
  };

  // 复盘分析：显示详细问题分析（review 数据）
  const renderReviewTab = () => {
    if (review) return renderReview(review);
    if (reviewLoading) return <div style={{ color: '#6b7280', padding: 40, textAlign: 'center' }}>加载复盘数据...</div>;
    return (
      <div style={{ color: '#6b7280', padding: 40, textAlign: 'center' }}>
        {reviewError || '暂无复盘数据，请运行回测'}
      </div>
    );
  };

  const renderReview = (r) => {
    if (!r || r.error) return (
      <div style={{ color: '#6b7280', padding: 40, textAlign: 'center' }}>
        暂无复盘数据，请运行回测<br /><code>python3 run_backtest_now.py</code>
      </div>
    );

    const rules = r.rules || [];
    const entryIssues = r.entry_issues || [];
    const exitIssues = r.exit_issues || [];
    const goodExits = r.good_exits || [];
    const overview = r.summary?.overview || {};

    const pct = v => (v != null) ? (v >= 0 ? '+' : '') + v.toFixed(2) + '%' : '--';
    const pctColor2 = v => v >= 0 ? { color: '#22c55e' } : { color: '#ef4444' };

    return (
      <div style={{ display: 'flex', flexDirection: 'column', gap: 12 }}>
        {/* 运行时间 */}
        <div style={{ color: '#6b7280', fontSize: 11 }}>
          分析运行: {r._run_time ? new Date(r._run_time).toLocaleString('zh-CN', {timeZone: 'Asia/Shanghai'}) : '--'}
          {r.total_signals != null ? ` | 总信号: ${r.total_signals}` : ''}
          {r.total_trades != null ? ` | 实盘: ${r.total_trades} 笔` : ''}
        </div>

        {/* ── 总体判断 ── */}
        <div className="trading-section" style={{ background: '#111827' }}>
          <h3 style={{ fontSize: 14, marginBottom: 8 }}>📈 总体判断</h3>
          <div style={{ fontSize: 12, color: '#9ca3af', lineHeight: 1.7 }}>
            本轮复盘 {overview.total_samples || 0} 个{overview.review_window || '最近 1 天'}已评分样本；
            其中 {overview.gave_space_5pct || 0} 个在持仓内曾给出超过 5% 的顺势空间，
            {overview.had_drawdown_8pct || 0} 个出现超过 8% 的持仓内回撤。
            <br /><br />
            最近重点问题共 <strong style={{ color: '#fbbf24' }}>{entryIssues.length + exitIssues.length}</strong> 个；
            开仓需改进 <strong style={{ color: '#fbbf24' }}>{entryIssues.length}</strong> 个，
            主要问题是入场后先承受较大回撤或最大浮盈不足；
            平仓偏早 <strong style={{ color: '#f87171' }}>{exitIssues.length}</strong> 个，
            主要问题是退出后仍继续上涨；
            平仓保护有效 <strong style={{ color: '#34d399' }}>{goodExits.length}</strong> 个，
            说明转弱/保护退出仍有价值。
          </div>
        </div>

        {/* ── 自动调参 ── */}
        {r.auto_tune && r.auto_tune.records && Object.keys(r.auto_tune.records).length > 0 && (
          <div className="trading-section" style={{ background: '#111827' }}>
            <h3 style={{ fontSize: 14, marginBottom: 8 }}>🔧 自动调参</h3>
            <div style={{ fontSize: 12, color: '#9ca3af', marginBottom: 8 }}>
              回测 {r.auto_tune.run_time ? new Date(r.auto_tune.run_time).toLocaleString('zh-CN', {timeZone: 'Asia/Shanghai'}) : '--'}
              基于 24h 胜率自动优化各类别阈值
            </div>
            <table className="trade-table">
              <thead>
                <tr><th>类别</th><th>阈值</th><th>24h胜率</th><th>样本</th><th>状态</th></tr>
              </thead>
              <tbody>
                {Object.entries(r.auto_tune.records).map(([cat, rec]) => (
                  <tr key={cat}>
                    <td style={{ fontWeight: 600 }}>{cat}</td>
                    <td>{rec.old_threshold}→{rec.new_threshold}</td>
                    <td style={{ color: rec.win_rate >= 30 ? '#22c55e' : '#ef4444' }}>{rec.win_rate}%</td>
                    <td>{rec.samples}</td>
                    <td>{rec.adjusted ? <span style={{ color: '#f59e0b' }}>🔄 已调整</span> : <span style={{ color: '#6b7280' }}>⏸️ 保持</span>}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        )}

        {/* ── 开仓问题 ── */}

        {/* ── 开仓问题 ── */}
        <div className="trading-section" style={{ background: '#111827' }}>
          <h3 style={{ fontSize: 14, marginBottom: 8 }}>⚠️ 开仓问题</h3>
          <div style={{ fontSize: 12, color: '#9ca3af', marginBottom: 10 }}>
            需要重点区分"真启动"和"高位追入"：如果入场后很快先打出 -8% 左右回撤，
            而不是先给 5% 以上顺势空间，说明确认条件还不够。
          </div>
          <div style={{ overflowX: 'auto' }}>
            <table className="trade-table">
              <thead>
                <tr><th>币种</th><th>等级</th><th>最大浮盈</th><th>最大回撤</th><th>6h收益</th><th>24h收益</th><th>开仓</th><th>平仓</th></tr>
              </thead>
              <tbody>
                {entryIssues.slice(0, 10).map((e, i) => (
                  <tr key={i}>
                    <td style={{ fontWeight: 600 }}>{e.symbol}</td>
                    <td><span className={`grade-${e.grade || 'B'}`}>{e.grade || '-'}</span></td>
                    <td style={pctColor2(e.max_gain_pct)}>{pct(e.max_gain_pct)}</td>
                    <td style={{ color: '#ef4444' }}>{pct(e.max_dd_pct < 0 ? e.max_dd_pct : -e.max_dd_pct)}</td>
                    <td style={pctColor2(e.ret_6h_pct || 0)}>{pct(e.ret_6h_pct)}</td>
                    <td style={pctColor2(e.ret_24h_pct || 0)}>{pct(e.ret_24h_pct)}</td>
                    <td style={{ color: '#fbbf24', fontSize: 11 }}>{e.entry_quality}</td>
                    <td style={pctColor2(e.ret_24h_pct <= 0 ? 1 : -1)}>{e.exit_quality}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
          {entryIssues.length > 10 && (
            <div style={{ color: '#6b7280', fontSize: 11, marginTop: 6 }}>
              还有 {entryIssues.length - 10} 个...
            </div>
          )}
        </div>

        {/* ── 平仓问题 ── */}
        <div className="trading-section" style={{ background: '#111827' }}>
          <h3 style={{ fontSize: 14, marginBottom: 8 }}>⏰ 平仓问题（偏早）</h3>
          <div style={{ fontSize: 12, color: '#9ca3af', marginBottom: 10 }}>
            偏早退出集中在平仓后继续上涨的样本，说明 TP 后剩余仓位不宜只因单次弱化就全平，
            需要叠加价格回撤、分数/OI/筹码共同转弱。
          </div>
          <div style={{ overflowX: 'auto' }}>
            <table className="trade-table">
              <thead>
                <tr><th>币种</th><th>等级</th><th>最大浮盈</th><th>最大回撤</th><th>6h收益</th><th>24h收益</th><th>开仓</th><th>平仓</th></tr>
              </thead>
              <tbody>
                {exitIssues.slice(0, 10).map((e, i) => (
                  <tr key={i}>
                    <td style={{ fontWeight: 600 }}>{e.symbol}</td>
                    <td><span className={`grade-${e.grade || 'B'}`}>{e.grade || '-'}</span></td>
                    <td style={pctColor2(e.max_gain_pct)}>{pct(e.max_gain_pct)}</td>
                    <td style={{ color: '#ef4444' }}>{pct(e.max_dd_pct < 0 ? e.max_dd_pct : -e.max_dd_pct)}</td>
                    <td style={pctColor2(e.ret_6h_pct || 0)}>{pct(e.ret_6h_pct)}</td>
                    <td style={pctColor2(e.ret_24h_pct || 0)}>{pct(e.ret_24h_pct)}</td>
                    <td style={pctColor2('entry_quality' in e && e.entry_quality === '基本正确' ? 1 : -1)}>{e.entry_quality}</td>
                    <td style={{ color: '#f87171', fontSize: 11 }}>{e.exit_quality}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
          {exitIssues.length > 10 && (
            <div style={{ color: '#6b7280', fontSize: 11, marginTop: 6 }}>
              还有 {exitIssues.length - 10} 个...
            </div>
          )}
        </div>

        {/* ── 有效做法 ── */}
        <div className="trading-section" style={{ background: '#111827' }}>
          <h3 style={{ fontSize: 14, marginBottom: 8 }}>✅ 有效做法</h3>
          <div style={{ fontSize: 12, color: '#9ca3af', marginBottom: 10 }}>
            保护利润和转弱退出不能直接取消；多笔样本显示平仓后继续走弱，说明风控对小额实盘验证有保护作用。
          </div>
          <div style={{ overflowX: 'auto' }}>
            <table className="trade-table">
              <thead>
                <tr><th>币种</th><th>等级</th><th>最大浮盈</th><th>最大回撤</th><th>6h收益</th><th>24h收益</th><th>开仓</th><th>平仓</th></tr>
              </thead>
              <tbody>
                {goodExits.slice(0, 8).map((e, i) => (
                  <tr key={i}>
                    <td style={{ fontWeight: 600 }}>{e.symbol}</td>
                    <td><span className={`grade-${e.grade || 'B'}`}>{e.grade || '-'}</span></td>
                    <td style={pctColor2(e.max_gain_pct)}>{pct(e.max_gain_pct)}</td>
                    <td style={{ color: '#ef4444' }}>{pct(e.max_dd_pct < 0 ? e.max_dd_pct : -e.max_dd_pct)}</td>
                    <td style={pctColor2(e.ret_6h_pct || 0)}>{pct(e.ret_6h_pct)}</td>
                    <td style={pctColor2(e.ret_24h_pct || 0)}>{pct(e.ret_24h_pct)}</td>
                    <td style={pctColor2('entry_quality' in e && e.entry_quality === '基本正确' ? 1 : -1)}>{e.entry_quality}</td>
                    <td style={{ color: '#34d399', fontSize: 11 }}>{e.exit_quality}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
          {goodExits.length > 8 && (
            <div style={{ color: '#6b7280', fontSize: 11, marginTop: 6 }}>
              还有 {goodExits.length - 8} 个...
            </div>
          )}
        </div>

        {/* ── 规则启示 ── */}
        {rules.length > 0 && (
          <div className="trading-section" style={{ background: '#111827' }}>
            <h3 style={{ fontSize: 14, marginBottom: 8 }}>📋 规则启示</h3>
            {rules.filter(s => s.section !== '总体判断').map((s, i) => (
              <div key={i} style={{ marginBottom: 12 }}>
                <div style={{ fontSize: 13, fontWeight: 600, color: '#f59e0b', marginBottom: 4 }}>{s.section}</div>
                <div style={{ fontSize: 12, color: '#9ca3af', lineHeight: 1.7, whiteSpace: 'pre-line' }}>{s.text}</div>
              </div>
            ))}
          </div>
        )}
      </div>
    );
  };

  const renderFactorAnalysis = () => {
    if (!factorData) return <div style={{ color: '#6b7280', padding: 40, textAlign: 'center' }}>加载因子分析中...</div>;
    if (factorData.error) return <div style={{ color: '#6b7280', padding: 40, textAlign: 'center' }}>暂无因子分析数据，请在终端运行 <code>python3 run_backtest_now.py</code></div>;

    const runTime = factorData.run_time ? new Date(factorData.run_time).toLocaleString('zh-CN') : '--';
    const disc = factorData.overall_discrimination;

    return (
      <div style={{ display: 'flex', flexDirection: 'column', gap: 12 }}>
        <div style={{ color: '#6b7280', fontSize: 12 }}>分析时间: {runTime} | 总信号: {factorData.total_signals || 0}</div>

        {/* 整体效能 */}
        <div className="trading-section" style={{ background: '#111827' }}>
          <h3 style={{ fontSize: 14 }}>📊 评分体系整体效能</h3>
          <div style={{ fontSize: 24, fontWeight: 700, ...pnlColor(disc) }}>
            {disc >= 0 ? '+' : ''}{disc?.toFixed(1)}%
          </div>
          <div style={{ fontSize: 11, color: '#6b7280' }}>
            评分区分度: 高分组胜率 - 低分组胜率 = {disc?.toFixed(1)}%
            {disc < 0 && ' (当前评分区分度不佳, 高分信号反而不如低分)'}
          </div>
        </div>

        {/* 已有因子效能 */}
        <div className="trading-section" style={{ background: '#111827' }}>
          <h3 style={{ fontSize: 14 }}>✅ 已有评分因子效能</h3>
          <div style={{ overflowX: 'auto' }}>
            <table className="trade-table">
              <thead>
                <tr>
                  <th>因子</th><th>区分度</th><th>高分组胜率</th><th>低分组胜率</th><th>建议</th>
                </tr>
              </thead>
              <tbody>
                {(factorData.current_factors || []).filter(f => f.name).map((f, i) => {
                  const d = f.discrimination || 0;
                  return (
                    <tr key={i}>
                      <td style={{ fontWeight: 600, color: '#c9d1d9' }}>{f.name}</td>
                      <td style={{ ...pnlColor(d) }}>{d >= 0 ? '+' : ''}{d.toFixed(1)}%</td>
                      <td style={{ color: '#22c55e' }}>{f.high_win_rate?.toFixed(1)}%</td>
                      <td style={{ color: '#ef4444' }}>{f.low_win_rate?.toFixed(1)}%</td>
                      <td>{Math.abs(d) >= 5
                        ? (d > 0 ? '✅ 有效, 可增权' : '🔁 反向有效, 反向计入')
                        : '❌ 无区分度, 建议降权'}</td>
                    </tr>
                  );
                })}
              </tbody>
            </table>
          </div>
        </div>

        {/* 推荐新增因子 + 添加按钮 */}
        <div className="trading-section" style={{ background: '#111827' }}>
          <div style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'center', marginBottom: 12 }}>
            <h3 style={{ fontSize: 14, margin: 0 }}>💡 推荐新增因子</h3>
            {weightsDirty && (
              <button onClick={saveWeights}
                style={{ background: '#f59e0b', color: '#000', border: 'none', borderRadius: 6, padding: '6px 14px', fontSize: 12, fontWeight: 600, cursor: 'pointer' }}>
                💾 保存到评分系统
              </button>
            )}
          </div>
          <div style={{ overflowX: 'auto' }}>
            <table className="trade-table">
              <thead>
                <tr>
                  <th>因子</th><th>说明</th><th>相关性</th><th>区分度</th><th>推荐权重</th><th>操作</th>
                </tr>
              </thead>
              <tbody>
                {(factorData.candidate_recommendations || []).filter(f => f.discrimination >= 5).map((f, i) => {
                  const alreadyAdded = customFactors[f.factor];
                  const w = alreadyAdded?.weight || 5;
                  const recommendedWeights = f.discrimination >= 15 ? 12 : f.discrimination >= 10 ? 8 : 5;
                  return (
                    <tr key={i}>
                      <td style={{ fontWeight: 600, color: '#c9d1d9' }}>{f.factor}</td>
                      <td style={{ fontSize: 11 }}>{f.description}</td>
                      <td>{f.correlation?.toFixed(3) || '-'}</td>
                      <td style={{ fontWeight: 600, color: '#22c55e' }}>+{f.discrimination?.toFixed(1)}%</td>
                      <td>
                        <div style={{ display: 'flex', alignItems: 'center', gap: 4 }}>
                          <input type="number" min="0" max="30" step="1"
                            value={w}
                            onChange={e => updateFactorWeight(f.factor, e.target.value)}
                            disabled={!alreadyAdded}
                            style={{
                              width: 48, padding: '3px 6px', background: '#1e293b',
                              border: '1px solid #374151', borderRadius: 4, color: '#c9d1d9',
                              fontSize: 12, textAlign: 'center',
                            }} />
                          <span style={{ fontSize: 10, color: '#6b7280' }}>%</span>
                        </div>
                      </td>
                      <td>
                        {alreadyAdded ? (
                          <button onClick={() => removeFactor(f.factor)}
                            style={{ background: '#7f1d1d', color: '#fca5a5', border: 'none', borderRadius: 4, padding: '3px 10px', fontSize: 11, cursor: 'pointer' }}>
                            🗑 移除
                          </button>
                        ) : (
                          <button onClick={() => addFactor(f.factor, f.description, f.discrimination)}
                            style={{ background: '#166534', color: '#86efac', border: 'none', borderRadius: 4, padding: '3px 10px', fontSize: 11, cursor: 'pointer' }}>
                            + 添加 (建议{w}%)
                          </button>
                        )}
                      </td>
                    </tr>
                  );
                })}
                {(!factorData.candidate_recommendations || factorData.candidate_recommendations.length === 0) && (
                  <tr><td colSpan={6} style={{ textAlign: 'center', color: '#6b7280' }}>暂无足够数据分析推荐，请先运行回测</td></tr>
                )}
              </tbody>
            </table>
          </div>
        </div>

        {/* 已添加的自定义因子 */}
        {Object.keys(customFactors).length > 0 && (
          <div className="trading-section" style={{ background: '#111827' }}>
            <div style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'center', marginBottom: 12 }}>
              <h3 style={{ fontSize: 14, margin: 0 }}>🔧 已添加的自定义因子</h3>
              <button onClick={saveWeights}
                style={{ background: '#f59e0b', color: '#000', border: 'none', borderRadius: 6, padding: '6px 14px', fontSize: 12, fontWeight: 600, cursor: weightsDirty ? 'pointer' : 'default', opacity: weightsDirty ? 1 : 0.5 }}>
                💾 {weightsDirty ? '保存到评分系统' : '已同步'}
              </button>
            </div>
            <div style={{ overflowX: 'auto' }}>
              <table className="trade-table">
                <thead>
                  <tr>
                    <th>因子</th><th>说明</th><th>权重</th><th>数据源</th><th>操作</th>
                  </tr>
                </thead>
                <tbody>
                  {Object.entries(customFactors).map(([name, cfg]) => (
                    <tr key={name}>
                      <td style={{ fontWeight: 600, color: '#c9d1d9' }}>{name}</td>
                      <td style={{ fontSize: 11, color: '#9ca3af' }}>{cfg.description || '-'}</td>
                      <td>
                        <div style={{ display: 'flex', alignItems: 'center', gap: 4 }}>
                          <input type="number" min="0" max="30" step="1"
                            value={cfg.weight}
                            onChange={e => updateFactorWeight(name, e.target.value)}
                            style={{
                              width: 48, padding: '3px 6px', background: '#1e293b',
                              border: '1px solid #374151', borderRadius: 4, color: '#c9d1d9',
                              fontSize: 12, textAlign: 'center',
                            }} />
                          <span style={{ fontSize: 10, color: '#6b7280' }}>%</span>
                        </div>
                      </td>
                      <td style={{ fontSize: 11, color: '#6b7280' }}>{cfg.source}</td>
                      <td>
                        <button onClick={() => removeFactor(name)}
                          style={{ background: '#7f1d1d', color: '#fca5a5', border: 'none', borderRadius: 4, padding: '3px 10px', fontSize: 11, cursor: 'pointer' }}>
                          🗑 移除
                        </button>
                      </td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          </div>
        )}
      </div>
    );
  };

  const renderStrategyLearning = () => {
    if (learningLoading && !learningData) {
      return <div style={{ color: '#6b7280', padding: 40, textAlign: 'center' }}>加载策略学习结果...</div>;
    }
    const data = learningData || {};
    const candidates = data.candidates || [];
    const counts = data.status_counts || {};
    const activeRules = data.active_entry_policy?.rules || [];
    const statusText = {
      proposed: '待处理',
      shadow: '影子验证',
      approved: '已批准',
      active: '已生效',
      rejected: '已拒绝',
      rolled_back: '已回滚',
    };
    const targetText = {
      entry_filter: '开仓前检查',
      exit_policy: '平仓规则',
      score_weight: '评分权重',
      position_sizing: '仓位大小',
    };

    return (
      <div style={{ display: 'flex', flexDirection: 'column', gap: 12 }}>
        <div className="trading-section" style={{ background: '#111827' }}>
          <div style={{ display: 'flex', justifyContent: 'space-between', gap: 12, alignItems: 'flex-start', flexWrap: 'wrap' }}>
            <div>
              <h3 style={{ fontSize: 14, marginBottom: 8 }}>策略学习闭环</h3>
              <div style={{ color: '#9ca3af', fontSize: 12, lineHeight: 1.7 }}>
                回测和复盘先生成候选策略；候选策略只有切到“已生效”后，才会写入开仓前检查。
                影子验证和待处理状态不会影响真实开仓。
              </div>
            </div>
            <button onClick={loadLearning}
              style={{ background: '#1f2937', color: '#c9d1d9', border: '1px solid #374151', borderRadius: 6, padding: '6px 12px', cursor: 'pointer' }}>
              刷新
            </button>
          </div>
          <div className="learning-stats">
            {['proposed', 'shadow', 'approved', 'active', 'rejected', 'rolled_back'].map(k => (
              <div className="learning-stat" key={k}>
                <div className="label">{statusText[k]}</div>
                <div className="value">{counts[k] || 0}</div>
              </div>
            ))}
          </div>
        </div>

        <div className="trading-section" style={{ background: '#111827' }}>
          <h3 style={{ fontSize: 14, marginBottom: 8 }}>当前已生效的开仓前检查</h3>
          {activeRules.length === 0 ? (
            <div style={{ color: '#6b7280', fontSize: 12 }}>暂无 active 规则，实盘仍按原有开仓检查运行。</div>
          ) : (
            <div className="policy-rule-list">
              {activeRules.map(rule => (
                <div className="policy-rule" key={rule.id}>
                  <div style={{ fontWeight: 700, color: '#c9d1d9' }}>{rule.title || rule.id}</div>
                  <div style={{ color: '#9ca3af', fontSize: 12 }}>命中后动作：{rule.effect?.block_entry ? '禁止开仓' : '提示/调整'}</div>
                  <div style={{ color: '#6b7280', fontSize: 11 }}>来源：{rule.source} | {rule.source_run_time}</div>
                </div>
              ))}
            </div>
          )}
        </div>

        <div className="trading-section" style={{ background: '#111827' }}>
          <h3 style={{ fontSize: 14, marginBottom: 8 }}>候选策略</h3>
          <div style={{ overflowX: 'auto' }}>
            <table className="trade-table">
              <thead>
                <tr>
                  <th>状态</th><th>影响位置</th><th>建议</th><th>大白话原因</th><th>样本</th><th>置信度</th><th>操作</th>
                </tr>
              </thead>
              <tbody>
                {candidates.map(c => (
                  <tr key={c.id}>
                    <td><span className={`policy-status ${c.status}`}>{statusText[c.status] || c.status}</span></td>
                    <td>{targetText[c.target] || c.target}</td>
                    <td style={{ fontWeight: 700, color: '#c9d1d9' }}>{c.title}</td>
                    <td style={{ minWidth: 260, color: '#9ca3af', lineHeight: 1.5 }}>{c.summary}</td>
                    <td>{c.sample_size || 0}</td>
                    <td>{((c.confidence || 0) * 100).toFixed(0)}%</td>
                    <td>
                      <div className="policy-actions">
                        {c.status !== 'shadow' && c.status !== 'active' && (
                          <button onClick={() => changeCandidateStatus(c.id, 'shadow')}>影子验证</button>
                        )}
                        {c.target === 'entry_filter' && c.status !== 'active' && (
                          <button className="primary" onClick={() => changeCandidateStatus(c.id, 'active')}>生效</button>
                        )}
                        {c.status !== 'rejected' && c.status !== 'active' && (
                          <button className="danger" onClick={() => changeCandidateStatus(c.id, 'rejected')}>拒绝</button>
                        )}
                        {c.status === 'active' && (
                          <button className="danger" onClick={() => changeCandidateStatus(c.id, 'rolled_back')}>回滚</button>
                        )}
                      </div>
                    </td>
                  </tr>
                ))}
                {candidates.length === 0 && (
                  <tr><td colSpan={7} style={{ textAlign: 'center', color: '#6b7280' }}>暂无策略建议。下一次复盘/因子分析后会自动生成。</td></tr>
                )}
              </tbody>
            </table>
          </div>
        </div>
      </div>
    );
  };

  return (
    <div className="backtest-panel">
      <h2 style={{ marginBottom: 16, fontSize: 18 }}>📊 评分回测</h2>

      <div className="nav" style={{ marginBottom: 16 }}>
        <button className={tab === 'summary' ? 'active' : ''} onClick={() => setTab('summary')}>
          📈 回测概览
        </button>
        <button className={tab === 'review' ? 'active' : ''} onClick={() => setTab('review')}>
          📊 复盘分析
        </button>
        <button className={tab === 'factors' ? 'active' : ''} onClick={() => setTab('factors')}>
          🔬 因子效能
        </button>
        <button className={tab === 'learning' ? 'active' : ''} onClick={() => setTab('learning')}>
          策略学习
        </button>
      </div>

      {tab === 'summary' ? renderSummary() : tab === 'review' ? renderReviewTab() : tab === 'factors' ? renderFactorAnalysis() : renderStrategyLearning()}
    </div>
  );
}
