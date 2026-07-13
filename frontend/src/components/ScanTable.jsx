import React, { useEffect, useMemo, useState } from 'react';

const API_BASE = '/api';
const gradeColor = { S1: '#f59e0b', S2: '#22c55e', A1: '#3b82f6', A2: '#8b5cf6', B: '#9ca3af', C: '#f97316', D: '#ef4444' };
const statusText = { pass: '正常仓候选', probe: '可小仓试探', observe: '先观察', block: '暂不开仓', error: '判断异常' };
const statusColor = { pass: '#22c55e', probe: '#38bdf8', observe: '#f59e0b', block: '#ef4444', error: '#ef4444' };

function num(v, fallback = 0) {
  const n = Number(v);
  return Number.isFinite(n) ? n : fallback;
}

function fmt(v, d = 1) {
  return num(v).toFixed(d);
}

function fmtPrice(v) {
  const n = num(v);
  if (!n) return '-';
  if (n < 0.01) return `$${n.toFixed(6)}`;
  if (n < 100) return `$${n.toFixed(4)}`;
  return `$${n.toFixed(2)}`;
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

function DetailRow({ label, value, valueColor }) {
  return (
    <div style={{ display: 'flex', justifyContent: 'space-between', gap: 16, padding: '7px 0', borderBottom: '1px solid #1f2937' }}>
      <span style={{ color: '#6b7280' }}>{label}</span>
      <span style={{ color: valueColor || '#d0d5e0', fontWeight: 600, textAlign: 'right' }}>{value ?? '-'}</span>
    </div>
  );
}

function V3Badge({ row }) {
  const status = row?.entry_profile?.status;
  const color = statusColor[status] || '#9ca3af';
  const mark = status === 'pass' ? '✓' : status === 'probe' ? '小' : status === 'observe' ? '观' : '×';
  return <span style={{ color, fontWeight: 800 }}>{mark}</span>;
}

function SignalCard({ detail }) {
  const signal = detail.plain_signal || {};
  const depth = detail.depth || {};
  return (
    <div style={{ background: '#111827', borderRadius: 10, padding: 20, marginTop: 16 }}>
      <h3 style={{ fontSize: 14, color: '#d0d5e0', marginBottom: 12, fontWeight: 600 }}>系统大白话</h3>
      <div style={{ color: signal.headline?.includes('暂不') || signal.headline?.includes('观察') ? '#f97316' : '#22c55e', fontSize: 18, fontWeight: 700, marginBottom: 8 }}>
        {signal.headline || '-'}
      </div>
      <div style={{ color: '#9ca3af', fontSize: 13, lineHeight: 1.6 }}>{signal.detail || '-'}</div>
      <div style={{ display: 'flex', gap: 10, flexWrap: 'wrap', marginTop: 12 }}>
        <span className="mini-pill">评分 {fmt(detail.composite_score)}</span>
        <span className="mini-pill">开仓信号 {fmt(signal.entry_alpha ?? detail.entry_alpha)}</span>
        <span className="mini-pill">持仓质量 {fmt(signal.hold_alpha ?? detail.hold_alpha)}</span>
        <span className="mini-pill">方向 {signal.side || detail.trend_direction || '-'}</span>
      </div>
      <div style={{ marginTop: 12, color: '#9ca3af', fontSize: 13 }}>
        盘口快照：承接分 {fmt(depth.bid_support_score ?? 50)}，大单分 {fmt(depth.large_order_score ?? 50)}，
        {depth.machine_like ? '疑似机器盘口。' : '未发现明显机器盘口。'}
      </div>
    </div>
  );
}

function ConfirmationList({ entry }) {
  const items = entry.confirmations || [];
  if (!items.length) {
    return <div style={{ color: '#6b7280', marginTop: 10 }}>暂无模板确认明细。</div>;
  }
  return (
    <div style={{ display: 'grid', gap: 8, marginTop: 14 }}>
      {items.map((item, idx) => {
        const color = item.ok ? '#22c55e' : item.required ? '#ff5570' : '#fbbf24';
        const icon = item.ok ? '✓' : item.required ? '×' : '·';
        return (
          <div key={`${item.label}-${idx}`} style={{ display: 'grid', gridTemplateColumns: '120px 70px 1fr', gap: 10, alignItems: 'center', padding: '9px 10px', border: '1px solid #1f2937', borderRadius: 8, background: '#0d1117' }}>
            <span style={{ color: '#8b949e', fontWeight: 600 }}>{item.label}</span>
            <span style={{ color, fontWeight: 800 }}>{icon} {item.required ? '硬条件' : '参考项'}</span>
            <span style={{ color: '#d0d5e0' }}>{item.text}</span>
          </div>
        );
      })}
    </div>
  );
}

function V3SignalDetail({ detail }) {
  const v3 = detail.v3_signals || {};
  const signal = detail.plain_signal || {};
  const entry = detail.entry_profile || {};
  const thresholds = entry.thresholds || {};
  const tech = detail.technical || {};
  const price = num(detail.market_price ?? detail.price);
  const atr = num(tech.atr);
  const entryAlpha = signal.entry_alpha ?? detail.entry_alpha ?? entry.metrics?.entry_alpha;
  const holdAlpha = signal.hold_alpha ?? detail.hold_alpha ?? entry.metrics?.hold_alpha;
  const status = entry.status || 'block';
  const color = statusColor[status] || '#9ca3af';
  const focus = entry.focus || [];
  const classification = entry.classification || {};

  return (
    <div style={{ background: '#111827', borderRadius: 10, padding: 20, marginTop: 16 }}>
      <h3 style={{ fontSize: 14, color: '#d0d5e0', marginBottom: 12, fontWeight: 600 }}>V3.0 模板开仓条件</h3>
      <div style={{ display: 'flex', gap: 16, marginBottom: 16 }}>
        <div style={{ flex: 1, background: '#0d1117', borderRadius: 10, padding: 18, textAlign: 'center' }}>
          <div style={{ color: '#6b7280', marginBottom: 8 }}>Entry Alpha</div>
          <div style={{ color: '#fbbf24', fontSize: 32, fontWeight: 800 }}>{fmt(entryAlpha)}</div>
        </div>
        <div style={{ flex: 1, background: '#0d1117', borderRadius: 10, padding: 18, textAlign: 'center' }}>
          <div style={{ color: '#6b7280', marginBottom: 8 }}>Hold Alpha</div>
          <div style={{ color: '#fbbf24', fontSize: 32, fontWeight: 800 }}>{fmt(holdAlpha)}</div>
        </div>
      </div>

      <div style={{ border: '1px solid #263244', borderRadius: 10, padding: 14, marginBottom: 14, background: '#0d1117' }}>
        <div style={{ display: 'flex', justifyContent: 'space-between', gap: 12, flexWrap: 'wrap' }}>
          <span style={{ color: '#8b949e' }}>当前币种开仓模板</span>
          <span style={{ color: '#8b949e' }}>阈值：分数 {thresholds.min_score ?? '-'} / Entry {thresholds.min_entry_alpha ?? '-'} / R:R {thresholds.min_rr ?? '-'}</span>
        </div>
        <div style={{ marginTop: 8, color: '#d0d5e0', fontSize: 22, fontWeight: 800 }}>
          {entry.template_name || entry.template || '-'}
          <span style={{ color, fontSize: 16, marginLeft: 10 }}>{statusText[status] || status}</span>
        </div>
        <div style={{ marginTop: 10, color: '#9ca3af', lineHeight: 1.7 }}>{entry.template_message || entry.description || '-'}</div>
        {classification.reason && (
          <div style={{ marginTop: 8, color: '#8b949e', lineHeight: 1.7 }}>
            分类依据：{classification.reason}，置信度 {fmt((classification.confidence || 0) * 100, 0)}%
            {entry.template_locked ? '（手工锁定模板）' : ''}
          </div>
        )}
        <div style={{ marginTop: 8, color: '#d0d5e0', lineHeight: 1.7 }}>{entry.reason || '-'}</div>
        {!!focus.length && (
          <div style={{ display: 'flex', flexWrap: 'wrap', gap: 8, marginTop: 12 }}>
            {focus.map((x) => <span className="mini-pill" key={x}>重点看：{x}</span>)}
          </div>
        )}
      </div>

      <ConfirmationList entry={entry} />

      <div style={{ display: 'grid', gridTemplateColumns: '1fr 1fr', gap: 10, marginTop: 14 }}>
        <div style={{ border: '1px solid #1f2937', borderRadius: 8, padding: 12, background: '#0d1117' }}>
          <div style={{ color: '#8b949e', fontSize: 12 }}>试探仓判断</div>
          <div style={{ color: status === 'probe' || status === 'pass' ? '#38bdf8' : '#f59e0b', fontWeight: 800, marginTop: 6 }}>
            {status === 'pass' ? '已满足，可不止试探仓' : status === 'probe' ? '允许小仓试探' : status === 'observe' ? '只观察，不下单' : '不允许'}
          </div>
        </div>
        <div style={{ border: '1px solid #1f2937', borderRadius: 8, padding: 12, background: '#0d1117' }}>
          <div style={{ color: '#8b949e', fontSize: 12 }}>正常仓判断</div>
          <div style={{ color: status === 'pass' ? '#22c55e' : '#ff5570', fontWeight: 800, marginTop: 6 }}>
            {status === 'pass' ? '模板确认通过' : '确认不足'}
          </div>
        </div>
      </div>

      <DetailRow label="成交量放大" value={`${fmt(v3.breakout?.volume_ratio ?? entry.metrics?.volume_ratio, 2)}x`} valueColor="#d0d5e0" />
      <DetailRow label="R:R 口径" value={`${fmt(v3.rr_ratio ?? entry.metrics?.rr_used, 2)} · ${v3.rr?.rr_method || 'structure'}`} valueColor="#d0d5e0" />
      <DetailRow label="冷却状态" value={v3.cooldown?.in_cooldown ? v3.cooldown.reason : '正常'} valueColor={v3.cooldown?.in_cooldown ? '#fbbf24' : '#22c55e'} />

      <div style={{ marginTop: 16, color: '#9ca3af', fontSize: 13 }}>ATR 动态止盈位 (ATR={fmt(atr, 4)})</div>
      <div style={{ display: 'grid', gridTemplateColumns: 'repeat(4, 1fr)', gap: 10, marginTop: 10 }}>
        {[
          ['TP1', price + atr * 2],
          ['TP2', price + atr * 4],
          ['TP3', price + atr * 6],
          ['止损', price - atr * 2],
        ].map(([label, value]) => (
          <div key={label} style={{ background: '#0d1117', borderRadius: 8, padding: 12 }}>
            <div style={{ color: '#6b7280', fontSize: 12 }}>{label}</div>
            <div style={{ color: '#d0d5e0', fontWeight: 800, marginTop: 4 }}>{fmtPrice(value)}</div>
          </div>
        ))}
      </div>
    </div>
  );
}

function SymbolDetail({ symbol, onBack }) {
  const [detail, setDetail] = useState(null);
  const [loading, setLoading] = useState(true);

  useEffect(() => {
    setLoading(true);
    fetch(`${API_BASE}/scan/by_symbol/${symbol}`)
      .then((r) => r.json())
      .then(setDetail)
      .catch((e) => setDetail({ error: e.message }))
      .finally(() => setLoading(false));
  }, [symbol]);

  if (loading) return <div className="loading">加载详情...</div>;
  if (!detail || detail.error) return <div className="error">{detail?.error || '未找到数据'}</div>;

  const tech = detail.technical || {};
  const fut = detail.futures || {};
  const interp = detail.interpretation || {};

  return (
    <div className="detail-panel">
      <div className="detail-header">
        <button className="back-btn" onClick={onBack}>← 返回</button>
        <h2>{detail.symbol}</h2>
        <span className={`grade grade-${detail.grade || 'unknown'}`}>{detail.grade}</span>
        <span style={{ color: '#6b7280', fontSize: 13 }}>评分: {fmt(detail.composite_score)}</span>
        <span style={{ color: '#9ca3af', fontSize: 13, marginLeft: 'auto' }}>{fmtPrice(detail.market_price ?? detail.price)}</span>
      </div>

      <div style={{ display: 'grid', gridTemplateColumns: '150px 1fr', gap: 16, marginBottom: 16 }}>
        <div style={{ background: '#0d1117', border: `1px solid ${gradeColor[detail.grade] || '#374151'}`, borderRadius: 10, padding: 18, textAlign: 'center' }}>
          <div style={{ color: gradeColor[detail.grade] || '#9ca3af', fontSize: 34, fontWeight: 800 }}>{detail.grade}</div>
          <div style={{ color: '#d0d5e0', fontSize: 20, fontWeight: 700 }}>{fmt(detail.composite_score)}</div>
        </div>
        <div style={{ background: '#0d1117', border: '1px solid #1f2937', borderRadius: 10, padding: 18 }}>
          <div style={{ color: '#9ca3af', marginBottom: 8 }}>{detail.chip_phase || '-'} · {detail.trend_direction || '-'} · {detail.volatility_level || '-'}</div>
          <div style={{ color: '#9ca3af', marginBottom: 8 }}>
            市场状态：{marketPhaseText(detail.market_phase?.phase)}
            {detail.market_phase?.confidence != null ? ` · ${fmt(detail.market_phase.confidence, 0)}分` : ''}
            {detail.market_phase?.allow_roll ? ' · 允许滚仓' : ' · 不滚仓'}
          </div>
          <div style={{ color: '#d0d5e0', fontSize: 20, fontWeight: 800 }}>{detail.risk_label || '-'}</div>
        </div>
      </div>

      <div style={{ display: 'grid', gridTemplateColumns: 'repeat(5, 1fr)', gap: 12, marginBottom: 16 }}>
        {[
          ['技术面', interp.technical?.score, interp.technical?.detail, '#6366f1'],
          ['合约', interp.futures?.score, interp.futures?.detail, '#8b5cf6'],
          ['位置', interp.position?.score, interp.position?.detail, '#22c55e'],
          ['筹码', interp.chip?.score, interp.chip?.detail, '#eab308'],
          ['热度', interp.heat?.score, interp.heat?.detail, '#f97316'],
        ].map(([label, score, text, color]) => (
          <div key={label} style={{ background: '#111827', borderRadius: 10, padding: 14 }}>
            <div style={{ color: '#8b949e', fontSize: 12 }}>{label}</div>
            <div style={{ color, fontSize: 24, fontWeight: 800 }}>{fmt(score)}</div>
            <div style={{ color: '#8b949e', fontSize: 12 }}>{text || '-'}</div>
          </div>
        ))}
      </div>

      <div style={{ display: 'grid', gridTemplateColumns: '1fr 1fr', gap: 16 }}>
        <div style={{ background: '#111827', borderRadius: 10, padding: 20 }}>
          <h3 style={{ fontSize: 14, color: '#d0d5e0', marginBottom: 12, fontWeight: 600 }}>详细指标</h3>
          <DetailRow label="ATR (14)" value={fmtPrice(tech.atr)} />
          <DetailRow label="ATR 占比" value={`${fmt(num(tech.atr_ratio) * 100, 2)}%`} />
          <DetailRow label="24h 涨跌幅" value={`${fmt(num(tech.price_change_24h) * 100, 2)}%`} valueColor={num(tech.price_change_24h) >= 0 ? '#22c55e' : '#ef4444'} />
          <DetailRow label="成交量变化" value={`${fmt(num(tech.volume_change_pct) * 100, 1)}%`} />
          <DetailRow label="趋势状态" value={tech.trend_state || detail.trend_state} />
          <DetailRow label="筹码阶段" value={tech.chip_phase || detail.chip_phase} />
          <DetailRow label="价格位置" value={tech.price_position || detail.price_position} />
          <DetailRow label="支撑质量" value={`${tech.support_quality || '-'} ${tech.support_score != null ? fmt(tech.support_score) : ''}`} />
          <DetailRow label="吸筹质量" value={`${tech.absorption_quality || '-'} ${tech.absorption_score != null ? fmt(tech.absorption_score) : ''}`} />
          <DetailRow label="相对强度" value={fmt(detail.relative_strength)} />
          <DetailRow label="资金费率" value={`${fmt(num(fut.funding_rate) * 100, 4)}%`} />
          <DetailRow label="未平仓量" value={fut.open_interest != null ? num(fut.open_interest).toLocaleString() : '-'} />
          <DetailRow label="持仓量变化" value={`${fmt(num(fut.oi_change_pct) * 100, 2)}%`} />
        </div>
        <div>
          <SignalCard detail={detail} />
          <V3SignalDetail detail={detail} />
        </div>
      </div>
    </div>
  );
}

export default function ScanTable() {
  const [data, setData] = useState({ symbols: [], count: 0 });
  const [loading, setLoading] = useState(true);
  const [selected, setSelected] = useState(null);
  const [keyword, setKeyword] = useState('');
  const [sortField, setSortField] = useState('composite_score');
  const [sortDir, setSortDir] = useState('desc');

  useEffect(() => {
    const load = () => fetch(`${API_BASE}/scan/latest`).then((r) => r.json()).then((d) => {
      setData(d);
      setLoading(false);
    }).catch(() => setLoading(false));
    load();
    const id = setInterval(load, 30000);
    return () => clearInterval(id);
  }, []);

  const rows = useMemo(() => {
    const filtered = [...(data.symbols || [])].filter((r) => !keyword || String(r.symbol || '').toUpperCase().includes(keyword.toUpperCase()));
    filtered.sort((a, b) => {
      const av = sortField === 'symbol' ? String(a.symbol) : num(a[sortField]);
      const bv = sortField === 'symbol' ? String(b.symbol) : num(b[sortField]);
      const res = typeof av === 'string' ? av.localeCompare(bv) : av - bv;
      return sortDir === 'asc' ? res : -res;
    });
    return filtered;
  }, [data.symbols, keyword, sortField, sortDir]);

  if (selected) return <SymbolDetail symbol={selected} onBack={() => setSelected(null)} />;

  return (
    <div>
      <div className="toolbar">
        <div className="filters">
          <input value={keyword} onChange={(e) => setKeyword(e.target.value)} placeholder="搜索币种" />
        </div>
        <div className="toolbar-info">
          <button onClick={() => { setSortField('composite_score'); setSortDir(sortDir === 'desc' ? 'asc' : 'desc'); }}>按评分排序</button>
          <button onClick={() => { setSortField('symbol'); setSortDir(sortDir === 'desc' ? 'asc' : 'desc'); }}>按币种排序</button>
          <span>最后更新：{data.scan_time ? new Date(data.scan_time).toLocaleString('zh-CN') : '-'}</span>
          <span>共 {data.count || rows.length} 个</span>
        </div>
      </div>

      {loading ? <div className="loading">加载中...</div> : (
        <div className="scan-card-grid" style={{ display: 'grid', gridTemplateColumns: 'repeat(auto-fill, minmax(320px, 1fr))', gap: 12 }}>
          {rows.map((r) => (
            <div
              key={r.symbol}
              className="scan-card"
              onClick={() => setSelected(r.symbol)}
              style={{
                background: '#0d1117',
                border: '1px solid #1f2937',
                borderRadius: 10,
                padding: 14,
                cursor: 'pointer',
                minHeight: 150,
              }}
            >
              <div style={{ display: 'flex', alignItems: 'center', gap: 10, marginBottom: 10 }}>
                <div style={{ color: '#d0d5e0', fontSize: 18, fontWeight: 800 }}>{r.symbol}</div>
                <span className={`grade grade-${r.grade || 'unknown'}`} style={{ marginLeft: 'auto' }}>{r.grade}</span>
                <V3Badge row={r} />
              </div>
              <div style={{ display: 'flex', alignItems: 'baseline', gap: 12, marginBottom: 10 }}>
                <div style={{ color: gradeColor[r.grade] || '#d0d5e0', fontSize: 28, fontWeight: 800 }}>{fmt(r.composite_score)}</div>
                <div style={{ color: '#9ca3af', fontSize: 15 }}>{fmtPrice(r.price)}</div>
              </div>
              <div style={{ color: '#c9d1d9', fontSize: 13, marginBottom: 10 }}>
                {r.chip_phase || '-'} · {r.trend_direction || '-'} · {r.volatility_level || '-'}
              </div>
              <div style={{ color: '#9ca3af', fontSize: 12, marginBottom: 10 }}>
                市场状态：{marketPhaseText(r.market_phase?.phase)}
                {r.market_phase?.confidence != null ? ` · ${fmt(r.market_phase.confidence, 0)}分` : ''}
              </div>
              <div style={{ color: r.plain_signal?.headline?.includes('暂不') || r.plain_signal?.headline?.includes('观察') ? '#f97316' : '#22c55e', fontSize: 14, fontWeight: 700, marginBottom: 8 }}>
                {r.plain_signal?.headline || r.risk_label || '-'}
              </div>
              <div style={{ display: 'flex', gap: 8, flexWrap: 'wrap', color: '#8b949e', fontSize: 12 }}>
                <span className="mini-pill">{r.entry_profile?.template_name || '默认模板'}</span>
                <span className="mini-pill">RS {fmt(r.relative_strength)}</span>
                <span className="mini-pill">{r.price_position || '-'}</span>
                <span className="mini-pill">{statusText[r.entry_profile?.status] || '-'}</span>
              </div>
            </div>
          ))}
        </div>
      )}
    </div>
  );
}
