import React, { useEffect, useMemo, useState } from 'react';

const API_BASE = '/api';

function fmt(v, digits = 2) {
  const n = Number(v || 0);
  return Number.isFinite(n) ? n.toFixed(digits) : '-';
}

function money(v) {
  const n = Number(v || 0);
  if (!Number.isFinite(n)) return '-';
  if (n >= 1_000_000) return `$${(n / 1_000_000).toFixed(1)}M`;
  if (n >= 1_000) return `$${(n / 1_000).toFixed(1)}K`;
  return `$${n.toFixed(0)}`;
}

function tradeabilityText(v) {
  if (v === 'alpha_futures_mapped') return '可映射合约';
  if (v === 'alpha_tradeable') return 'Alpha 可交易';
  if (v === 'alpha_only') return '仅 Alpha 观察';
  return '不可用';
}

function profileText(v) {
  if (v === 'early_discovery') return '早期发现型';
  if (v === 'momentum_continuation') return '动量延续型';
  if (v === 'futures_mapped') return '合约映射型';
  if (v === 'high_risk_watch') return '高风险观察型';
  if (v === 'neutral_watch') return '中性观察型';
  return v || '-';
}

function entryText(v) {
  if (v === 'normal_gate') return '交给 Alpha 量价执行';
  if (v === 'block') return '禁止开仓';
  if (v === 'observe') return '观察';
  if (v === 'probe') return '小仓试探';
  if (v === 'candidate') return 'Alpha 候选';
  return v || '-';
}

function volumePriceText(v) {
  const labels = {
    accumulation_volume: '低位温和放量',
    breakout_pullback: '突破后回踩',
    overheated_chase: '暴量暴涨冷静',
    distribution_risk: '高位放量滞涨',
    breakdown_volume: '放量破位',
    neutral: '中性观察',
    insufficient_data: '数据不足',
  };
  return labels[v] || v || '-';
}

function volumePriceActionText(v) {
  const labels = {
    normal_review_probe: '允许 Alpha 小仓',
    normal_review: '允许 Alpha 开仓',
    cooldown: '冷静等待',
    short_review_only: '只允许 Alpha 做空',
    observe: '只观察',
  };
  return labels[v] || v || '-';
}

function scoreColor(score) {
  if (score >= 75) return '#22c55e';
  if (score >= 65) return '#facc15';
  if (score >= 55) return '#38bdf8';
  return '#9ca3af';
}

function thresholdLabel(k) {
  const labels = {
    alpha_score: 'Alpha 总分',
    discovery_score: '发现分',
    momentum_score: '动量分',
    liquidity_score: '流动性',
    risk_score: '风险分',
    spread_pct: '最大价差',
    spread_pct_gt: '高风险价差',
    volume_growth_6h: '6h 成交放大',
    abs_percent_change_24h: '24h 涨跌上限',
    abs_percent_change_24h_gt: '24h 高风险涨跌',
    range_24h_pct_gt: '24h 高风险波动',
    ret_1h_gt: '1h 动量',
    ret_6h_gt: '6h 动量',
    percent_change_24h_max: '24h 涨幅上限',
    risk_score_lt: '高风险风险分',
    liquidity_score_lt: '高风险流动性',
  };
  return labels[k] || k;
}

function AlphaDetail({ symbol, onBack }) {
  const [detail, setDetail] = useState(null);
  const [error, setError] = useState(null);

  useEffect(() => {
    setDetail(null);
    setError(null);
    fetch(`${API_BASE}/alpha/scan/by_symbol/${encodeURIComponent(symbol)}`)
      .then((r) => r.json())
      .then((d) => {
        if (!d || typeof d !== 'object') {
          setError('Alpha 详情为空，请等待下一轮扫描。');
        } else if (d.error) {
          setError(d.error);
        } else {
          setDetail(d);
        }
      })
      .catch((e) => setError(e.message));
  }, [symbol]);

  if (error) return <div className="trading-section" style={{ color: '#ef4444' }}>{error}</div>;
  if (!detail) return <div className="trading-section">加载 Alpha 详情...</div>;

  const scores = detail.scores || {};
  const depth = detail.raw_features?.depth || {};
  const volume = detail.raw_features?.volume || {};
  const returns = detail.raw_features?.returns || {};
  const risk = detail.raw_features?.risk || {};
  const profile = detail.profile || {};
  const thresholds = Object.entries(profile.thresholds || {});
  const blockReasons = profile.block_reasons || [];
  const review = detail.normal_review || {};
  const reviewProfile = review.entry_profile || {};
  const volumePrice = review.volume_price?.state ? review.volume_price : (detail.volume_price || {});
  const vpReasons = volumePrice.reasons || [];
  const vpMetrics = volumePrice.metrics || {};
  const vpDirections = [volumePrice.allow_long ? '多' : null, volumePrice.allow_short ? '空' : null].filter(Boolean).join(' / ') || '-';

  return (
    <div className="symbol-detail">
      <button className="back-btn" onClick={onBack}>返回 Alpha 扫描</button>
      <div className="detail-header">
        <div>
          <h2>{detail.base_asset} <span className="mini-pill">{detail.alpha_symbol}</span></h2>
          <div className="muted">{detail.name || '-'} · {tradeabilityText(detail.tradeability)}</div>
        </div>
        <div className="score-badge" style={{ color: scoreColor(detail.alpha_score) }}>{fmt(detail.alpha_score, 1)}</div>
      </div>

      <div className="trading-section">
        <h3>Alpha 系统判断</h3>
        <div className="muted-box">Alpha 独立按量价评分决定是否进入实盘；普通交易评分不再复核 Alpha，只保留实时盘口、冷却、仓位和风控检查。</div>
        <div className="alpha-grid">
          <div className="alpha-card"><span>发现分</span><b>{fmt(scores.discovery)}</b></div>
          <div className="alpha-card"><span>动量分</span><b>{fmt(scores.momentum)}</b></div>
          <div className="alpha-card"><span>流动性</span><b>{fmt(scores.liquidity)}</b></div>
          <div className="alpha-card"><span>风险分</span><b>{fmt(scores.risk)}</b></div>
          <div className="alpha-card"><span>可交易</span><b>{fmt(scores.tradeability)}</b></div>
        </div>
      </div>

      <div className="trading-section">
        <h3>Alpha 量价第一关</h3>
        <div className="stats-grid">
          <div className="stat-card"><div className="stat-label">量价状态</div><div className="stat-value">{volumePriceText(volumePrice.state)}</div></div>
          <div className="stat-card"><div className="stat-label">系统动作</div><div className="stat-value">{volumePriceActionText(volumePrice.action)}</div></div>
          <div className="stat-card"><div className="stat-label">允许方向</div><div className="stat-value">{vpDirections}</div></div>
          <div className="stat-card"><div className="stat-label">最大仓位系数</div><div className="stat-value">{fmt((volumePrice.max_position_factor || 0) * 100, 0)}%</div></div>
        </div>
        <div className="muted-box" style={{ marginTop: 12 }}>
          {vpReasons.length ? vpReasons.join('；') : '暂无量价原因，等待下一轮 Alpha 扫描。'}
        </div>
        <div className="scan-score" style={{ marginTop: 10 }}>
          15m {fmt(vpMetrics.ret_15m)}% · 1h {fmt(vpMetrics.ret_1h)}% · 6h {fmt(vpMetrics.ret_6h)}%
          · 成交 {fmt(vpMetrics.volume_growth_6h, 2)}x · 距高点 {fmt(vpMetrics.pullback_from_high_pct)}%
        </div>
      </div>

      <div className="trading-section">
        <h3>Alpha 实盘执行</h3>
        <div className="stats-grid">
          <div className="stat-card"><div className="stat-label">执行评分</div><div className="stat-value">{review.normal_score == null ? '-' : fmt(review.normal_score, 1)}</div></div>
          <div className="stat-card"><div className="stat-label">执行方向</div><div className="stat-value">{review.normal_side || '-'}</div></div>
          <div className="stat-card"><div className="stat-label">Alpha 模板</div><div className="stat-value">{reviewProfile.template || '-'}</div></div>
          <div className="stat-card"><div className="stat-label">量价通过</div><div className="stat-value">{volumePrice.action === 'normal_review' || volumePrice.action === 'normal_review_probe' || volumePrice.action === 'short_review_only' ? '是' : '-'}</div></div>
        </div>
        <div className="muted-box" style={{ marginTop: 12 }}>
          {review.block_reason || '还没有进入实盘执行，等待 runner 下一轮扫描。'}
        </div>
        {(review.missing_fields || []).length > 0 && (
          <div className="scan-plain" style={{ marginTop: 12 }}>
            {(review.missing_fields || []).slice(0, 8).map((field) => <span key={field} className="mini-pill" style={{ marginRight: 8 }}>{field}</span>)}
          </div>
        )}
        {thresholds.length > 0 && (
          <div className="scan-score" style={{ marginTop: 12 }}>
            Alpha 发现模板：{profileText(profile.name)} · {thresholds.map(([k, v]) => `${thresholdLabel(k)} ${v}`).join(' · ')}
          </div>
        )}
      </div>

      <div className="trading-section">
        <h3>Alpha 数据</h3>
        <div className="stats-grid">
          <div className="stat-card"><div className="stat-label">Alpha 价格</div><div className="stat-value">${fmt(detail.price, 6)}</div></div>
          <div className="stat-card"><div className="stat-label">24h 涨跌</div><div className="stat-value">{fmt(detail.percent_change_24h)}%</div></div>
          <div className="stat-card"><div className="stat-label">24h 成交额</div><div className="stat-value">{money(detail.volume_24h)}</div></div>
          <div className="stat-card"><div className="stat-label">流动性</div><div className="stat-value">{money(detail.liquidity)}</div></div>
          <div className="stat-card"><div className="stat-label">合约映射</div><div className="stat-value">{detail.futures_symbol || '-'}</div></div>
          <div className="stat-card"><div className="stat-label">盘口价差</div><div className="stat-value">{fmt(depth.spread_pct, 4)}%</div></div>
        </div>
      </div>

      <div className="trading-section">
        <h3>详细指标</h3>
        <table className="trade-table">
          <tbody>
            <tr><td>15m 动量</td><td>{fmt(returns.ret_15m)}%</td><td>1h 动量</td><td>{fmt(returns.ret_1h)}%</td></tr>
            <tr><td>6h 动量</td><td>{fmt(returns.ret_6h)}%</td><td>成交额增长</td><td>{fmt(volume.volume_growth_6h, 2)}x</td></tr>
            <tr><td>盘口承接</td><td>{fmt(depth.imbalance, 2)}</td><td>24h 波动区间</td><td>{fmt(risk.range_24h_pct)}%</td></tr>
            <tr><td>买盘深度</td><td>{fmt(depth.bid_depth)}</td><td>卖盘深度</td><td>{fmt(depth.ask_depth)}</td></tr>
          </tbody>
        </table>
      </div>
    </div>
  );
}

export default function AlphaScan() {
  const [data, setData] = useState({ symbols: [], count: 0 });
  const [selected, setSelected] = useState(null);
  const [keyword, setKeyword] = useState('');
  const [filter, setFilter] = useState('all');

  useEffect(() => {
    const load = () => fetch(`${API_BASE}/alpha/scan/latest`)
      .then((r) => r.json())
      .then((d) => setData(d && typeof d === 'object' ? d : { symbols: [], count: 0 }))
      .catch(() => setData({ symbols: [], count: 0 }));
    load();
    const id = setInterval(load, 30000);
    return () => clearInterval(id);
  }, []);

  const rows = useMemo(() => {
    return [...(data?.symbols || [])]
      .filter((r) => !keyword || `${r.base_asset} ${r.alpha_symbol} ${r.name || ''}`.toUpperCase().includes(keyword.toUpperCase()))
      .filter((r) => filter === 'all' || r.tradeability === filter || r.alpha_profile === filter || r.entry_level === filter)
      .sort((a, b) => Number(b.alpha_score || 0) - Number(a.alpha_score || 0));
  }, [data?.symbols, keyword, filter]);

  if (selected) return <AlphaDetail symbol={selected} onBack={() => setSelected(null)} />;

  return (
    <div>
      <div className="scan-toolbar">
        <input placeholder="搜索 Alpha 币种" value={keyword} onChange={(e) => setKeyword(e.target.value)} />
        <button className={filter === 'all' ? 'active' : ''} onClick={() => setFilter('all')}>全部</button>
        <button className={filter === 'alpha_futures_mapped' ? 'active' : ''} onClick={() => setFilter('alpha_futures_mapped')}>可实盘映射</button>
        <button className={filter === 'early_discovery' ? 'active' : ''} onClick={() => setFilter('early_discovery')}>早期发现</button>
        <button className={filter === 'momentum_continuation' ? 'active' : ''} onClick={() => setFilter('momentum_continuation')}>动量延续</button>
        <button className={filter === 'candidate' ? 'active' : ''} onClick={() => setFilter('candidate')}>候选</button>
        <button className={filter === 'probe' ? 'active' : ''} onClick={() => setFilter('probe')}>试探</button>
        <span>最后更新：{data?.scan_time ? new Date(data.scan_time).toLocaleString('zh-CN') : '-'}</span>
        <span>共 {rows.length} / {data?.count || 0} 个</span>
      </div>

      <div className="scan-card-grid" style={{ display: 'grid', gridTemplateColumns: 'repeat(auto-fill, minmax(320px, 1fr))', gap: 12 }}>
        {rows.map((r) => (
          <div key={r.alpha_symbol} className="scan-card" onClick={() => setSelected(r.alpha_symbol)}>
            <div style={{ display: 'flex', justifyContent: 'space-between', gap: 10 }}>
              <div>
                <div style={{ color: '#d0d5e0', fontSize: 18, fontWeight: 800 }}>{r.base_asset}</div>
                <div className="scan-plain">{r.name || r.alpha_symbol}</div>
              </div>
              <div style={{ color: scoreColor(r.alpha_score), fontSize: 26, fontWeight: 900 }}>{fmt(r.alpha_score, 1)}</div>
            </div>
            <div style={{ marginTop: 10, display: 'flex', gap: 8, flexWrap: 'wrap' }}>
              <span className="mini-pill">{r.grade}</span>
              <span className="mini-pill">{tradeabilityText(r.tradeability)}</span>
              <span className="mini-pill">{profileText(r.alpha_profile)}</span>
              <span className="mini-pill">Alpha 量价执行</span>
              {r.futures_symbol && <span className="mini-pill">{r.futures_symbol}</span>}
            </div>
            <div className="scan-plain" style={{ marginTop: 10 }}>
              {r.normal_review?.block_reason || (r.volume_price?.reasons || [])[0] || 'Alpha 按量价评分独立判断，过线后进入实盘盘口和风控检查。'}
            </div>
            <div className="scan-score" style={{ marginTop: 10 }}>
              量价 {volumePriceText(r.normal_review?.volume_price?.state || r.volume_price?.state)}
              · {volumePriceActionText(r.normal_review?.volume_price?.action || r.volume_price?.action)}
              · 执行分 {r.normal_review?.normal_score == null ? '-' : fmt(r.normal_review.normal_score, 1)}
              · 方向 {r.normal_review?.normal_side || '-'}
              · 发现 {fmt(r.discovery_score, 0)}
            </div>
            {(r.normal_review?.missing_fields || []).length > 0 && (
              <div className="scan-score" style={{ marginTop: 6 }}>
                缺字段：{r.normal_review.missing_fields.slice(0, 2).join('；')}
              </div>
            )}
            <div className="scan-score" style={{ marginTop: 6 }}>
              24h {fmt(r.percent_change_24h)}% · 成交 {money(r.volume_24h)} · spread {r.spread_pct == null ? '-' : `${fmt(r.spread_pct, 4)}%`}
            </div>
          </div>
        ))}
        {rows.length === 0 && (
          <div className="trading-section" style={{ gridColumn: '1 / -1', color: '#8b949e' }}>
            暂无 Alpha 扫描结果。先运行 alpha_pipeline 和 alpha_engine 后会显示。
          </div>
        )}
      </div>
    </div>
  );
}
