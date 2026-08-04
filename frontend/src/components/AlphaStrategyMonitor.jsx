import React, { useEffect, useMemo, useState } from 'react';

const number = (value, digits = 2) => {
  if (value === null || value === undefined || value === '') return '—';
  const parsed = Number(value);
  return Number.isFinite(parsed) ? parsed.toFixed(digits) : '—';
};

const probability = (value) => {
  if (value === null || value === undefined || value === '') return '样本收集中';
  const parsed = Number(value);
  return Number.isFinite(parsed) ? `${(parsed * 100).toFixed(0)}%` : '样本收集中';
};

const stateLabel = (state) => ({
  IDLE: '观察中',
  WATCH_ACCUMULATION: '发现吸筹迹象',
  WATCH_CONTINUATION: '发现趋势中继',
  WATCH_RECLAIM: '发现洗盘收复',
  ARMED: '等待启动',
  PROBE_READY: '可以试仓',
  WAIT_RETEST: '急拉后等待回踩',
  ACCEPTANCE_PENDING: '等待突破确认',
  CONFIRMED: '突破已经确认',
  RETEST_READY: '回踩后可以加仓',
  FAILED: '结构已经失效',
  COOLDOWN: '冷却观察中',
  EXPIRED: '机会已经过期',
}[state] || state || '未知');

const environmentLabel = (environment) => ({
  testnet: '测试网',
  mainnet: '主网',
}[environment] || environment || '未知环境');

const time = (value) => {
  if (!value) return '—';
  const parsed = new Date(value);
  return Number.isNaN(parsed.getTime()) ? value : parsed.toLocaleString();
};

const stateTone = (state) => {
  if (['ACCEPTED', 'RETESTED', 'BREAKOUT'].includes(state)) return 'good';
  if (['INVALIDATED', 'EXPIRED'].includes(state)) return 'bad';
  return 'neutral';
};

export default function AlphaStrategyMonitor() {
  const [environment, setEnvironment] = useState('');
  const [data, setData] = useState(null);
  const [error, setError] = useState('');
  const [loading, setLoading] = useState(true);

  useEffect(() => {
    let active = true;
    const load = async () => {
      try {
        const query = environment ? `?market_env=${environment}` : '';
        const response = await fetch(`/api/alpha-strategy/status${query}`, {
          cache: 'no-store',
        });
        if (!response.ok) throw new Error(`HTTP ${response.status}`);
        const result = await response.json();
        if (result.error) throw new Error(result.error);
        if (active) {
          setData(result);
          setError('');
        }
      } catch (reason) {
        if (active) setError(String(reason.message || reason));
      } finally {
        if (active) setLoading(false);
      }
    };
    load();
    const timer = setInterval(load, 5000);
    return () => {
      active = false;
      clearInterval(timer);
    };
  }, [environment]);

  const stateCounts = useMemo(
    () => Object.fromEntries((data?.states || []).map((row) => [
      `${row.market_env}:${row.state}`,
      row.count,
    ])),
    [data],
  );

  const ai = data?.ai || {};
  const models = ai.models || [];
  const trainingRuns = ai.recent_training_runs || [];
  const latestTrainingRuns = trainingRuns.reduce((latest, run) => {
    const key = `${run.model_key}:${run.target}`;
    if (!latest.some((item) => `${item.model_key}:${item.target}` === key)) {
      latest.push(run);
    }
    return latest;
  }, []);
  const runtime = data?.runtime || [];
  const states = data?.recent_states || [];
  const events = data?.recent_events || [];
  const consumptions = data?.recent_consumptions || [];

  return (
    <section className="alpha-monitor">
      <div className="alpha-monitor-head">
        <div>
          <p className="eyebrow">ALPHA STRATEGY V2</p>
          <h2>趋势启动监控</h2>
          <p className="muted">状态机、AI 概率、交易事件与账户消费状态实时汇总。</p>
        </div>
        <div className="alpha-monitor-controls">
          <select value={environment} onChange={(event) => setEnvironment(event.target.value)}>
            <option value="">全部环境</option>
            <option value="testnet">Testnet</option>
            <option value="mainnet">Mainnet</option>
          </select>
          <span className={`strategy-mode ${ai.execution_mode === 'live' ? 'live' : ''}`}>
            AI {ai.execution_mode || 'unknown'}
          </span>
        </div>
      </div>

      {error && <div className="alpha-alert bad">加载失败：{error}</div>}
      {(data?.alerts || []).map((alert, index) => (
        <div
          className={`alpha-alert ${alert.severity === 'error' ? 'bad' : 'warn'}`}
          key={`${alert.code}-${alert.market_env || alert.version || index}`}
        >
          {alert.code}
          {alert.market_env ? ` · ${alert.market_env}` : ''}
          {alert.age_minutes ? ` · 延迟 ${alert.age_minutes} 分钟` : ''}
          {alert.rate !== undefined ? ` · ${(Number(alert.rate) * 100).toFixed(1)}%` : ''}
        </div>
      ))}
      {loading && !data && <div className="alpha-empty">正在读取策略状态…</div>}

      <div className="alpha-kpis">
        <article>
          <span>特征快照</span>
          <strong>{data?.snapshot_summary?.snapshot_count ?? 0}</strong>
          <small>Ready {data?.snapshot_summary?.ready_count ?? 0}</small>
        </article>
        <article>
          <span>AI 样本</span>
          <strong>{ai.samples?.total ?? 0}</strong>
          <small>已标注 {ai.samples?.labeled ?? 0}</small>
        </article>
        <article>
          <span>模型</span>
          <strong>{models.length}</strong>
          <small>
            Champion {models.filter((model) => model.status === 'champion').length}
            {' · '}实盘闭合 {ai.execution_outcomes?.closed ?? 0}
          </small>
        </article>
        <article>
          <span>待执行</span>
          <strong>{consumptions.filter((item) => ['PENDING', 'PLANNED', 'SUBMITTED'].includes(item.status)).length}</strong>
          <small>近 {consumptions.length} 条消费记录</small>
        </article>
      </div>

      <div className="alpha-monitor-grid">
        <section className="alpha-card">
          <h3>Worker 心跳</h3>
          {runtime.length === 0 && <div className="alpha-empty">尚无运行心跳</div>}
          {runtime.map((row) => (
            <div className="runtime-row" key={row.market_env}>
              <div>
                <strong>{row.market_env}</strong>
                <span>{row.strategy_mode}</span>
              </div>
              <div><span>最后 K 线</span><strong>{time(row.last_candle_close_time)}</strong></div>
              <div><span>处理 / 转移</span><strong>{row.processed_count} / {row.transition_count}</strong></div>
              <div><span>心跳</span><strong>{time(row.heartbeat_at)}</strong></div>
              {row.last_error && <p className="alpha-error">{row.last_error}</p>}
            </div>
          ))}
        </section>

        <section className="alpha-card">
          <h3>状态分布</h3>
          <div className="state-cloud">
            {Object.entries(stateCounts).map(([key, count]) => (
              <span key={key}>
                {environmentLabel(key.split(':')[0])} · {stateLabel(key.split(':')[1])}
                {' '}<b>{count}</b>
              </span>
            ))}
            {Object.keys(stateCounts).length === 0 && <div className="alpha-empty">暂无状态</div>}
          </div>
        </section>
      </div>

      <section className="alpha-card">
        <h3>最近状态</h3>
        <div className="alpha-table-wrap">
          <table className="alpha-table">
            <thead>
              <tr>
                <th>环境 / 合约</th><th>当前进度</th><th>更新时间</th>
                <th>启动可能</th><th>持续上涨可能</th><th>假突破风险</th><th>结构价位</th>
              </tr>
            </thead>
            <tbody>
              {states.map((row) => (
                <tr key={`${row.market_env}-${row.futures_symbol}`}>
                  <td><b>{row.futures_symbol}</b><small>{environmentLabel(row.market_env)}</small></td>
                  <td>
                    <span className={`state-pill ${stateTone(row.state)}`}>
                      {stateLabel(row.state)}
                    </span>
                  </td>
                  <td>{time(row.updated_at)}</td>
                  <td>{probability(row.p_setup_success)}</td>
                  <td>{probability(row.p_followthrough)}</td>
                  <td>{probability(row.p_fakeout)}</td>
                  <td>
                    <small>突破 {number(row.breakout_level, 6)}</small>
                    <small>失效 {number(row.invalidation_price, 6)}</small>
                    {row.reason_codes?.includes('ai_prediction_not_ready')
                      && <small>AI 样本收集中</small>}
                  </td>
                </tr>
              ))}
              {states.length === 0 && <tr><td colSpan="7" className="alpha-empty">暂无策略状态</td></tr>}
            </tbody>
          </table>
        </div>
      </section>

      <div className="alpha-monitor-grid">
        <section className="alpha-card">
          <h3>信号事件</h3>
          <div className="alpha-feed">
            {events.map((event) => (
              <article key={event.event_id}>
                <div>
                  <b>{event.futures_symbol}</b>
                  <span>{event.from_state} → {event.to_state}</span>
                </div>
                <strong>{event.action_type}</strong>
                <small>{event.strategy_mode} · {time(event.event_time)}</small>
              </article>
            ))}
            {events.length === 0 && (
              <div className="alpha-empty">暂无状态迁移信号；AI 收集期不会生成交易事件</div>
            )}
          </div>
        </section>

        <section className="alpha-card">
          <h3>账户消费</h3>
          <div className="alpha-feed">
            {consumptions.map((item) => (
              <article key={`${item.account_id}-${item.event_id}-${item.action_type}`}>
                <div>
                  <b>A{item.account_id} · {item.futures_symbol}</b>
                  <span>{item.action_type}</span>
                </div>
                <strong>{item.status}</strong>
                <small>{item.rejection_reason || time(item.updated_at)}</small>
              </article>
            ))}
            {consumptions.length === 0 && (
              <div className="alpha-empty">尚无可执行信号，因此没有账户消费记录</div>
            )}
          </div>
        </section>
      </div>

      <section className="alpha-card">
        <h3>模型注册表</h3>
        <div className="model-grid">
          {models.map((model) => (
            <article key={model.version}>
              <span className={`state-pill ${model.status === 'champion' ? 'good' : 'neutral'}`}>{model.status}</span>
              <b>{model.target}</b>
              <small>{model.market_env} · {model.version}</small>
              <small>样本 {model.sample_count} / 验证 {model.validation_count}</small>
            </article>
          ))}
          {models.length === 0 && latestTrainingRuns.map((run) => (
            <article key={`${run.model_key}-${run.target}`}>
              <span className="state-pill neutral">{run.status}</span>
              <b>{run.target}</b>
              <small>{run.market_env} · {run.model_key}</small>
              <small>
                合格样本 {run.sample_count ?? run.labeled_samples ?? 0}
                {' / '}{ai.training_requirements?.minimum_training_samples ?? 1000}
              </small>
            </article>
          ))}
          {models.length === 0 && latestTrainingRuns.length === 0 && (
            <div className="alpha-empty">仍在收集标注样本，尚未发布模型</div>
          )}
        </div>
      </section>
    </section>
  );
}
