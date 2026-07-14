import test from 'node:test';
import assert from 'node:assert/strict';

import { chinaTime, latestAIBySymbol, sampleProgress } from './aiQualityData.mjs';


test('latestAIBySymbol keeps the newest decision for each symbol', () => {
  const result = latestAIBySymbol([
    { symbol: 'B2USDT', decision: 'allow', observed_at: '2026-07-14T10:00:00Z' },
    { symbol: 'ETHUSDT', decision: 'probe', observed_at: '2026-07-14T10:30:00Z' },
    { symbol: 'B2USDT', decision: 'reject', observed_at: '2026-07-14T11:00:00Z' },
  ]);

  assert.equal(result.B2USDT.decision, 'reject');
  assert.equal(result.ETHUSDT.decision, 'probe');
});

test('sampleProgress uses labeled samples for model readiness', () => {
  assert.deepEqual(
    sampleProgress({
      total_samples: 120,
      pending_samples: 70,
      sample_count: 50,
      required_samples: 300,
      collected_today: 18,
    }),
    { total: 120, pending: 70, labeled: 50, required: 300, remaining: 250, percent: 16.7, collectedToday: 18 },
  );
});

test('chinaTime renders UTC timestamps in Asia Shanghai time', () => {
  assert.equal(chinaTime('2026-07-14T05:44:06Z'), '2026/7/14 13:44:06');
  assert.equal(chinaTime(null), '-');
});
