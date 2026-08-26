import assert from 'node:assert/strict';
import test from 'node:test';

import { createTradingDataClient } from '../api/tradingData.js';

function response(data = {}) {
  return { ok: true, json: async () => data };
}

test('history encodes account and filters', async () => {
  const calls = [];
  const client = createTradingDataClient(async (url, options) => {
    calls.push([url, options]);
    return response({ items: [] });
  });

  await client.history('account/2', { symbol: 'AKEUSDT', direction: 'LONG', limit: 20 });

  assert.equal(
    calls[0][0],
    '/api/trading/accounts/account%2F2/history?symbol=AKEUSDT&direction=LONG&limit=20',
  );
});

test('history includes supported filters and omits empty query parameters', async () => {
  let requestedUrl;
  const client = createTradingDataClient(async (url) => {
    requestedUrl = url;
    return response();
  });

  await client.history(2, {
    cursor: 'next/page',
    limit: 20,
    symbol: '',
    direction: null,
    source: 'alpha',
    from: '2026-08-01T00:00:00Z',
    to: undefined,
  });

  assert.equal(
    requestedUrl,
    '/api/trading/accounts/2/history?cursor=next%2Fpage&limit=20&source=alpha&from=2026-08-01T00%3A00%3A00Z',
  );
});

test('account and runtime clients request their exact endpoints', async () => {
  const calls = [];
  const client = createTradingDataClient(async (url, options) => {
    calls.push([url, options]);
    return response({ url });
  });

  await client.accounts();
  await client.status();
  await client.decisions(7);
  await client.runtime();

  assert.deepEqual(calls.map(([url]) => url), [
    '/api/trading/accounts',
    '/api/trading/accounts/status',
    '/api/trading/accounts/7/decisions',
    '/api/trading/runtime/status',
  ]);
});

test('status deduplicates concurrent and cached requests for 30 seconds', async () => {
  let calls = 0;
  let release;
  const pending = new Promise((resolve) => { release = resolve; });
  const client = createTradingDataClient(async () => {
    calls += 1;
    await pending;
    return response({ calls });
  });

  const first = client.status();
  const second = client.status();
  release();

  assert.deepEqual(await first, { calls: 1 });
  assert.deepEqual(await second, { calls: 1 });
  assert.deepEqual(await client.status(), { calls: 1 });
  assert.equal(calls, 1);

  await client.status({ force: true });
  assert.equal(calls, 2);
});

test('status callers with owned signals cancel independently', async () => {
  const requests = [];
  const client = createTradingDataClient((url, { signal }) => new Promise((resolve, reject) => {
    const request = { url, signal, resolve, reject };
    signal.addEventListener('abort', () => reject(new Error('aborted')), { once: true });
    requests.push(request);
  }));
  const firstController = new AbortController();
  const secondController = new AbortController();

  const first = client.status({ signal: firstController.signal });
  const second = client.status({ signal: secondController.signal });
  await new Promise((resolve) => setImmediate(resolve));

  firstController.abort();
  requests[1]?.resolve(response({ caller: 'second' }));
  const [firstOutcome, secondOutcome] = await Promise.all([
    first.then((value) => ({ status: 'fulfilled', value }), (error) => ({ status: 'rejected', error })),
    second.then((value) => ({ status: 'fulfilled', value }), (error) => ({ status: 'rejected', error })),
  ]);

  assert.deepEqual(requests.map(({ signal }) => signal), [
    firstController.signal,
    secondController.signal,
  ]);
  assert.equal(firstOutcome.status, 'rejected');
  assert.equal(firstOutcome.error.message, 'aborted');
  assert.deepEqual(secondOutcome, { status: 'fulfilled', value: { caller: 'second' } });
});

test('history requests are independently cancellable and never deduplicated', async () => {
  const calls = [];
  const client = createTradingDataClient(async (url, options) => {
    calls.push([url, options]);
    return response({ items: [] });
  });
  const firstSignal = new AbortController().signal;
  const secondSignal = new AbortController().signal;

  await Promise.all([
    client.history(2, { limit: 20 }, { signal: firstSignal }),
    client.history(2, { limit: 20 }, { signal: secondSignal }),
  ]);

  assert.equal(calls.length, 2);
  assert.equal(calls[0][1].signal, firstSignal);
  assert.equal(calls[1][1].signal, secondSignal);
});

test('all clients forward AbortSignal and report HTTP errors with URL and status', async () => {
  const signal = new AbortController().signal;
  const calls = [];
  const client = createTradingDataClient(async (url, options) => {
    calls.push([url, options]);
    return response({ ok: true });
  });

  await client.accounts({ signal });
  await client.status({ force: true, signal });
  await client.history(2, {}, { signal });
  await client.decisions(2, { signal });
  await client.runtime({ signal });

  assert.ok(calls.every(([, options]) => options.signal === signal));

  const failingClient = createTradingDataClient(async () => ({ ok: false, status: 503 }));
  await assert.rejects(
    failingClient.runtime(),
    new Error('/api/trading/runtime/status: 503'),
  );
});
