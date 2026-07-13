export function normalizeSelectedAccount(selectedAccount, accounts = []) {
  if (!accounts.length) return null;
  const exists = accounts.some((account) => String(account.account_id) === String(selectedAccount));
  return exists ? selectedAccount : accounts[0].account_id;
}

export function findSelectedAccount(selectedAccount, accounts = []) {
  if (selectedAccount == null || selectedAccount === 'all') return null;
  return accounts.find((account) => String(account.account_id) === String(selectedAccount)) || null;
}
