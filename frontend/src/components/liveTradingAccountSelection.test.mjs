import assert from 'node:assert/strict';
import test from 'node:test';

import { normalizeSelectedAccount, findSelectedAccount } from './liveTradingAccountSelection.js';

const accounts = [
  { account_id: 3, account_name: '账户A' },
  { account_id: 8, account_name: '账户B' },
];

test('normalizes all-account selection to the first concrete account', () => {
  assert.equal(normalizeSelectedAccount('all', accounts), 3);
});

test('keeps existing concrete account selection', () => {
  assert.equal(normalizeSelectedAccount(8, accounts), 8);
});

test('finds only concrete account rows', () => {
  assert.equal(findSelectedAccount('all', accounts), null);
  assert.equal(findSelectedAccount(3, accounts).account_name, '账户A');
});
