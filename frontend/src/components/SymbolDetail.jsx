import React, { useState, useEffect, useRef } from 'react';
import { Chart, registerables } from 'chart.js';

Chart.register(...registerables);

function gradeClass(grade) {
  return `grade-${grade || 'unknown'}`;
}

function riskClass(risk) {
  if (!risk) return '';
  return risk.includes('/') ? 'risk-multi' : risk.includes('出货') ? 'risk-danger' : risk.includes('风险') ? 'risk-warn' : '';
}

export default function SymbolDetail({ symbol, onBack, API }) {
  const [data, setData] = useState(null);
  const [loading, setLoading] = useState(true);
  const chartRef = useRef(null);
  const canvasRef = useRef(null);

  useEffect(() => {
    API.get(`/scan/by_symbol/${symbol}`)
      .then(res => setData(res.data))
      .catch(e => console.error(e))
      .finally(() => setLoading(false));
  }, [symbol]);

  useEffect(() => {
    if (!data?.score_history?.length || !canvasRef.current) return;

    if (chartRef.current) chartRef.current.destroy();

    const ctx = canvasRef.current.getContext('2d');
    const labels = data.score_history.map(s => new Date(s.time).toLocaleString('zh-CN', { month: '2-digit', day: '2-digit', hour: '2-digit', minute: '2-digit' }));
    const scores = data.score_history.map(s => s.score);
    const prices = data.score_history.map(s => s.price);

    chartRef.current = new Chart(ctx, {
      type: 'line',
      data: {
        labels,
        datasets: [
          {
            label: '评分',
            data: scores,
            borderColor: '#f59e0b',
            backgroundColor: 'rgba(245,158,11,0.1)',
            yAxisID: 'y',
            tension: 0.3,
            pointRadius: 1,
          },
          {
            label: '价格',
            data: prices,
            borderColor: '#3b82f6',
            backgroundColor: 'rgba(59,130,246,0.1)',
            yAxisID: 'y1',
            tension: 0.3,
            pointRadius: 1,
          },
        ],
      },
      options: {
        responsive: true,
        maintainAspectRatio: false,
        interaction: { mode: 'index', intersect: false },
        plugins: { legend: { position: 'top', labels: { color: '#9ca3af' } } },
        scales: {
          x: { ticks: { color: '#6b7280', maxTicksLimit: 10 }, grid: { color: '#1f2937' } },
          y: { position: 'left', ticks: { color: '#f59e0b' }, grid: { color: '#1f2937' }, title: { display: true, text: '评分', color: '#f59e0b' } },
          y1: { position: 'right', ticks: { color: '#3b82f6' }, grid: { display: false }, title: { display: true, text: '价格', color: '#3b82f6' } },
        },
      },
    });

    return () => { if (chartRef.current) chartRef.current.destroy(); };
  }, [data]);

  if (loading) return <div className="loading">加载详情...</div>;
  if (!data) return <div className="error">未找到数据</div>;

  const f = data.features || {};
  const tech = f.technical || {};
  const fut = f.futures || {};

  // 五维雷达得分
  const radarLabels = ['技术面', '合约', '位置', '筹码', '热度'];
  const interp = data.interpretation || {};
  const radarScores = [
    interp.technical?.score || 50,
    interp.futures?.score || 50,
    interp.position?.score || 50,
    interp.chip?.score || 50,
    interp.heat?.score || 50,
  ];

  // SVG 雷达图
  function radarPath(scores, cx, cy, r, n) {
    const pts = scores.map((s, i) => {
      const angle = (2 * Math.PI * i) / n - Math.PI / 2;
      const x = cx + (r * s) / 100 * Math.cos(angle);
      const y = cy + (r * s) / 100 * Math.sin(angle);
      return `${i === 0 ? 'M' : 'L'}${x},${y}`;
    });
    return pts.join(' ') + 'Z';
  }

  function gridPath(cx, cy, r, n, pct) {
    const pts = Array.from({ length: n }, (_, i) => {
      const angle = (2 * Math.PI * i) / n - Math.PI / 2;
      const x = cx + r * pct * Math.cos(angle);
      const y = cy + r * pct * Math.sin(angle);
      return `${i === 0 ? 'M' : 'L'}${x},${y}`;
    });
    return pts.join(' ') + 'Z';
  }

  function labelPos(idx, r, n, cx, cy) {
    const angle = (2 * Math.PI * idx) / n - Math.PI / 2;
    return { x: cx + (r + 28) * Math.cos(angle), y: cy + (r + 28) * Math.sin(angle) };
  }

  const RR = 90;
  const CX = 120;
  const CY = 110;

  return (
    <div className="detail-panel">
      <div className="detail-header">
        <button className="back-btn" onClick={onBack}>← 返回</button>
        <h2>{data.symbol}</h2>
        <span className={`grade ${gradeClass(data.grade)}`}>{data.grade}</span>
        <span style={{ color: '#6b7280', fontSize: 13 }}>评分: {data.composite_score.toFixed(1)}</span>
        <span className={`value ${riskClass(data.risk_label)}`}>{data.risk_label}</span>
        <span style={{ color: '#9ca3af', fontSize: 13, marginLeft: 'auto' }}>
          ${data.price?.toFixed(data.price < 1 ? 6 : data.price < 100 ? 4 : 2)}
        </span>
      </div>

      {/* 雷达图 */}
      <div style={{ display: 'flex', justifyContent: 'center', marginBottom: 16 }}>
        <svg width="260" height="240" viewBox="0 0 260 240">
          {/* 刻度网格 */}
          {[0.2, 0.4, 0.6, 0.8, 1.0].map(p => (
            <path key={p} d={gridPath(CX, CY, RR, 5, p)} fill="none" stroke="#1f2937" strokeWidth={1} />
          ))}
          {/* 轴线 */}
          {radarLabels.map((_, i) => {
            const angle = (2 * Math.PI * i) / 5 - Math.PI / 2;
            const x = CX + RR * Math.cos(angle);
            const y = CY + RR * Math.sin(angle);
            return <line key={i} x1={CX} y1={CY} x2={x} y2={y} stroke="#1f2937" strokeWidth={1} />;
          })}
          {/* 数据多边形 */}
          <path d={radarPath(radarScores, CX, CY, RR, 5)} fill="rgba(99,102,241,0.25)" stroke="#6366f1" strokeWidth={2} />
          {/* 数据点 */}
          {radarScores.map((s, i) => {
            const angle = (2 * Math.PI * i) / 5 - Math.PI / 2;
            const x = CX + (RR * s) / 100 * Math.cos(angle);
            const y = CY + (RR * s) / 100 * Math.sin(angle);
            return <circle key={i} cx={x} cy={y} r={3} fill="#818cf8" />;
          })}
          {/* 标签 */}
          {radarLabels.map((l, i) => {
            const pos = labelPos(i, RR, 5, CX, CY);
            const score = radarScores[i];
            return (
              <g key={i}>
                <text x={pos.x} y={pos.y - 7} textAnchor="middle" fill="#9ca3af" fontSize="12" fontWeight={500}>{l}</text>
                <text x={pos.x} y={pos.y + 8} textAnchor="middle" fill={score >= 70 ? '#22c55e' : score >= 50 ? '#eab308' : '#ef4444'} fontSize="11" fontWeight={700}>{score.toFixed(0)}</text>
              </g>
            );
          })}
        </svg>
      </div>

      <div className="detail-grid">
        <div className="detail-section">
          <h3>技术面分析</h3>
          <div className="detail-item">
            <span className="label">走势状态</span>
            <span className="value">{data.trend_state}</span>
          </div>
          <div className="detail-item">
            <span className="label">趋势方向</span>
            <span className="value" style={{ color: data.trend_direction === '向上' ? '#22c55e' : data.trend_direction === '向下' ? '#ef4444' : '#9ca3af' }}>{data.trend_direction}</span>
          </div>
          <div className="detail-item">
            <span className="label">筹码阶段</span>
            <span className="value">{data.chip_phase} {tech.chip_score != null ? `· ${tech.chip_score.toFixed?.(1) || tech.chip_score}` : ''}</span>
          </div>
          <div className="detail-item">
            <span className="label">波动水平</span>
            <span className="value">{data.volatility_level}</span>
          </div>
          <div className="detail-item">
            <span className="label">价格位置</span>
            <span className="value">{data.price_position}</span>
          </div>
          <div className="detail-item">
            <span className="label">区间宽度</span>
            <span className="value">{tech.range_width_pct != null ? (tech.range_width_pct * 100).toFixed(2) + '%' : '-'}</span>
          </div>
          <div className="detail-item">
            <span className="label">承接质量</span>
            <span className="value">{tech.absorption_quality || '-'} {tech.absorption_score != null ? `· ${tech.absorption_score.toFixed?.(1) || tech.absorption_score}` : ''}</span>
          </div>
          <div className="detail-item">
            <span className="label">涨跌量比</span>
            <span className="value">{tech.up_down_vol_ratio != null ? tech.up_down_vol_ratio.toFixed(2) : '-'}</span>
          </div>
          <div className="detail-item">
            <span className="label">24h 涨跌幅</span>
            <span className="value" style={{ color: (tech.price_change_24h || 0) > 0 ? '#22c55e' : '#ef4444' }}>
              {tech.price_change_24h != null ? (tech.price_change_24h * 100).toFixed(2) + '%' : '-'}
            </span>
          </div>
        </div>

        <div className="detail-section">
          <h3>合约面 & 链上</h3>
          <div className="detail-item">
            <span className="label">资金费率</span>
            <span className="value" style={{ color: (fut.funding_rate || 0) < -0.0005 ? '#f97316' : (fut.funding_rate || 0) > 0.0005 ? '#3b82f6' : '#9ca3af' }}>
              {fut.funding_rate != null ? (fut.funding_rate * 100).toFixed(4) + '%' : '-'}
            </span>
          </div>
          <div className="detail-item">
            <span className="label">费率状态</span>
            <span className="value">{fut.funding_state || '-'}</span>
          </div>
          <div className="detail-item">
            <span className="label">OI 变化</span>
            <span className="value">{fut.oi_state || '-'}</span>
          </div>
          <div className="detail-item">
            <span className="label">OI 变化率</span>
            <span className="value">{fut.oi_change_pct != null ? (fut.oi_change_pct * 100).toFixed(2) + '%' : '-'}</span>
          </div>
          <div className="detail-item">
            <span className="label">相对强度</span>
            <span className="value">{data.relative_strength}</span>
          </div>
          {f.onchain && f.onchain.has_data && (
            <>
              <div className="detail-item">
                <span className="label">交易所净流24h</span>
                <span className="value" style={{ color: (f.onchain.cex_net_flow_usd || 0) > 0 ? '#22c55e' : '#ef4444' }}>
                  {f.onchain.cex_net_flow_usd != null ? `$${(f.onchain.cex_net_flow_usd).toLocaleString()}` : '-'}
                </span>
              </div>
              <div className="detail-item">
                <span className="label">交易所净流14d</span>
                <span className="value" style={{ color: (f.onchain.cex_net_flow_14d_usd || 0) > 0 ? '#22c55e' : '#ef4444' }}>
                  {f.onchain.cex_net_flow_14d_usd != null ? `$${(f.onchain.cex_net_flow_14d_usd).toLocaleString()}` : '-'}
                </span>
              </div>
            </>
          )}
        </div>
      </div>

      <div className="chart-container">
        <canvas ref={canvasRef}></canvas>
      </div>
    </div>
  );
}
