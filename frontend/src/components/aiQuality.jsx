import React, { useEffect, useMemo, useState } from 'react';
import { latestAIBySymbol } from './aiQualityData.mjs';

export function useAIQualityDecisions() {
  const [decisions, setDecisions] = useState([]);
  useEffect(() => {
    let active = true;
    const load = () => fetch('/api/ai/decisions?limit=500', { cache: 'no-store' })
      .then((response) => response.json())
      .then((data) => active && setDecisions(Array.isArray(data.decisions) ? data.decisions : []))
      .catch(() => active && setDecisions([]));
    load();
    const timer = setInterval(load, 30000);
    return () => { active = false; clearInterval(timer); };
  }, []);
  return useMemo(() => latestAIBySymbol(decisions), [decisions]);
}

const DECISION_TEXT = {
  allow: 'AI 放行',
  probe: 'AI 试探',
  reject: 'AI 拒绝',
  collecting: 'AI 采集',
};

export function AIQualityBadge({ decision }) {
  if (!decision) return null;
  const score = decision.quality_score == null ? '' : ` ${Number(decision.quality_score).toFixed(0)}`;
  return (
    <span className={`ai-decision-badge ai-decision-${decision.decision}`} title={(decision.reasons || []).join('；')}>
      {DECISION_TEXT[decision.decision] || 'AI'}{score}
    </span>
  );
}
