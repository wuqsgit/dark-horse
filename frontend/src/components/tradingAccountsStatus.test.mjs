import assert from 'node:assert/strict';
import test from 'node:test';

import { createTradingAccountsStatusClient } from '../api/tradingAccountsStatus.js';

test('concurrent consumers and near-simultaneous polls share one request', async () => {
  let calls = 0;
  let release;
  const pending = new Promise((resolve) => { release = resolve; });
  const client = createTradingAccountsStatusClient(async () => {
    calls += 1;
    await pending;
    return { ok: true, json: async () => ({ environment_status: 'PROD LIVE' }) };
  }, 5000);

  const first = client.load();
  const second = client.load();
  release();

  assert.deepEqual(await first, { environment_status: 'PROD LIVE' });
  assert.deepEqual(await second, { environment_status: 'PROD LIVE' });
  assert.equal(calls, 1);

  await client.load();
  assert.equal(calls, 1);
});
