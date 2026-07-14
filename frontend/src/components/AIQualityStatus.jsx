import React, { useEffect, useRef, useState } from 'react';
import { chinaTime, sampleProgress } from './aiQualityData.mjs';

const LABELS = {
  live: 'AI 实盘',
  collecting: 'AI 采集中',
  error: 'AI 异常',
};

function DecisionStats({ stats = {} }) {
  return (
    <div className="ai-popover-decisions">
      <span>今日放行 <strong>{stats.allow || 0}</strong></span>
      <span>试探 <strong>{stats.probe || 0}</strong></span>
      <span>拒绝 <strong>{stats.reject || 0}</strong></span>
      <span>AI判断 <strong>{stats.collecting || 0}</strong></span>
    </div>
  );
}

function ModelProgress({ label, model = {} }) {
  const progress = sampleProgress(model);
  return (
    <section className="ai-model-progress">
      <div className="ai-model-progress-head">
        <strong>{label}</strong>
        <span>{progress.labeled}/{progress.required} 已标注</span>
      </div>
      <div className="ai-progress-track" aria-label={`${label}训练进度 ${progress.percent}%`}>
        <span style={{ width: `${progress.percent}%` }} />
      </div>
      <div className="ai-progress-metrics">
        <span>候选进池 {progress.total}</span>
        <span>今日候选 {progress.collectedToday}</span>
        <span>等待24h {progress.pending}</span>
        <span>已标注 {progress.labeled}</span>
        <span>训练还差 {progress.remaining}</span>
      </div>
      <DecisionStats stats={model.decisions_today} />
    </section>
  );
}

export default function AIQualityStatus() {
  const [status, setStatus] = useState({ status: 'collecting', models: {} });
  const [pinned, setPinned] = useState(false);
  const shellRef = useRef(null);

  useEffect(() => {
    let active = true;
    const load = () => fetch('/api/ai/status', { cache: 'no-store' })
      .then((response) => response.json())
      .then((data) => active && setStatus(data))
      .catch((error) => active && setStatus({ status: 'error', error: String(error), models: {} }));
    load();
    const timer = setInterval(load, 30000);
    return () => { active = false; clearInterval(timer); };
  }, []);

  useEffect(() => {
    const close = (event) => {
      if (pinned && shellRef.current && !shellRef.current.contains(event.target)) setPinned(false);
    };
    const escape = (event) => event.key === 'Escape' && setPinned(false);
    document.addEventListener('pointerdown', close);
    document.addEventListener('keydown', escape);
    return () => {
      document.removeEventListener('pointerdown', close);
      document.removeEventListener('keydown', escape);
    };
  }, [pinned]);

  const state = status.status || 'error';
  const alpha = status.models?.alpha || {};
  const normal = status.models?.normal || {};
  const maintenanceError = status.error || status.maintenance?.last_error;
  const explanation = state === 'live'
    ? '模型已生效：质量分 62+ 放行，55-62 使用 5% 试探仓，低于 55 拒绝。'
    : '原策略生成计划开仓后，AI 记录候选；满 24 小时生成结果标签，累计 300 条后训练并验证。采集期间原策略照常开仓。';

  return (
    <div ref={shellRef} className={`ai-quality-shell ${pinned ? 'is-open' : ''}`}>
      <button
        type="button"
        className={`ai-quality-status ai-${state}`}
        aria-expanded={pinned}
        aria-label={`${LABELS[state] || 'AI 异常'}，查看运行详情`}
        onClick={() => setPinned((value) => !value)}
      >
        <span className="ai-quality-dot" />
        <span>{LABELS[state] || 'AI 异常'}</span>
      </button>

      <div className="ai-quality-popover" role="status">
        <div className="ai-popover-head">
          <div>
            <strong>AI 入场质量模型</strong>
            <span>{explanation}</span>
          </div>
          <span className={`ai-popover-state ai-${state}`}>{LABELS[state] || 'AI 异常'}</span>
        </div>

        <div className="ai-workflow" aria-label="AI工作流程">
          <span>1 收集候选</span><span>2 等待24h</span><span>3 生成标签</span><span>4 训练生效</span>
        </div>

        <ModelProgress label="普通币模型" model={normal} />
        <ModelProgress label="Alpha 模型" model={alpha} />

        <div className="ai-maintenance">
          <span>最近标注：{chinaTime(status.maintenance?.last_label)}</span>
          <span>最近训练：{chinaTime(status.maintenance?.last_train)}</span>
        </div>
        {maintenanceError && <div className="ai-popover-error">异常：{maintenanceError}</div>}
      </div>
    </div>
  );
}
