export const EMPTY_ACCOUNT_FORM = {
  name: '',
  environment: 'testnet',
  api_key: '',
  api_secret: '',
  initial_capital: 5000,
  initial_capital_time: '',
  max_positions: 5,
  enabled: true,
  auto_trading_enabled: false,
  normal_trading_enabled: true,
  alpha_trading_enabled: true,
};

export function createAccountForm() {
  return { type: 'create', accountId: null };
}

export function editAccountForm(account) {
  if (!account?.id) throw new Error('编辑账户缺少账户ID');
  return { type: 'edit', accountId: Number(account.id) };
}

export function idleAccountForm() {
  return { type: 'idle', accountId: null };
}

export function accountToForm(account) {
  return { ...EMPTY_ACCOUNT_FORM, ...account, api_key: '', api_secret: '' };
}

export function buildAccountSaveRequest(mode, form) {
  if (mode?.type === 'edit') {
    if (!mode.accountId) throw new Error('编辑账户缺少账户ID');
    return {
      url: `/api/trading/accounts/${mode.accountId}`,
      method: 'PATCH',
      body: form,
    };
  }
  if (mode?.type === 'create') {
    return {
      url: '/api/trading/accounts',
      method: 'POST',
      body: form,
    };
  }
  throw new Error('请先选择新增或编辑账户');
}
