import assert from 'node:assert/strict';
import test from 'node:test';

import { buildAccountSaveRequest, createAccountForm, editAccountForm } from './tradingAccountFormMode.js';

test('edit account mode saves with PATCH instead of creating a new account', () => {
  const account = { id: 7, name: '主账户', environment: 'testnet', max_positions: 3 };
  const mode = editAccountForm(account);
  const request = buildAccountSaveRequest(mode, { name: '主账户-改', environment: 'testnet' });

  assert.equal(request.url, '/api/trading/accounts/7');
  assert.equal(request.method, 'PATCH');
});

test('create account mode is the only mode that posts to account collection', () => {
  const mode = createAccountForm();
  const request = buildAccountSaveRequest(mode, { name: '新账户', environment: 'testnet' });

  assert.equal(request.url, '/api/trading/accounts');
  assert.equal(request.method, 'POST');
});
