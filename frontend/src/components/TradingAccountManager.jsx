import React, { useState } from 'react';

import {
  EMPTY_ACCOUNT_FORM,
  accountToForm,
  buildAccountSaveRequest,
  createAccountForm,
  editAccountForm,
  idleAccountForm,
} from './tradingAccountFormMode';

export default function TradingAccountManager({ accounts, onChanged }) {
  const [open, setOpen] = useState(false);
  const [mode, setMode] = useState(idleAccountForm());
  const [form, setForm] = useState({ ...EMPTY_ACCOUNT_FORM });
  const [message, setMessage] = useState('');
  const [saving, setSaving] = useState(false);

  const change = (key, value) => setForm((old) => ({ ...old, [key]: value }));
  const startCreate = () => {
    setOpen(true);
    setMode(createAccountForm());
    setForm({ ...EMPTY_ACCOUNT_FORM });
    setMessage('');
  };
  const startEdit = (account) => {
    setOpen(true);
    setMode(editAccountForm(account));
    setForm(accountToForm(account));
    setMessage('');
  };
  const resetForm = () => {
    setMode(idleAccountForm());
    setForm({ ...EMPTY_ACCOUNT_FORM });
  };
  const save = async () => {
    setSaving(true);
    setMessage('');
    try {
      const request = buildAccountSaveRequest(mode, form);
      const res = await fetch(request.url, { method: request.method, headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(request.body) });
      const data = await res.json();
      if (!res.ok || data.error) throw new Error(data.error || `HTTP ${res.status}`);
      setMessage('账户配置已保存，交易进程会自动重新加载。');
      resetForm();
      await onChanged();
    } catch (error) { setMessage(`保存失败：${error.message}`); } finally { setSaving(false); }
  };

  const remove = async (account) => {
    const ok = window.confirm(`确认删除账户「${account.name}」？有持仓的账户会被系统拒绝删除。`);
    if (!ok) return;
    setSaving(true);
    setMessage('');
    try {
      const res = await fetch(`/api/trading/accounts/${account.id}`, { method: 'DELETE' });
      const data = await res.json();
      if (!res.ok || data.error) throw new Error(data.error || `HTTP ${res.status}`);
      setMessage('账户已删除，交易进程会自动重新加载。');
      if (mode.type === 'edit' && Number(mode.accountId) === Number(account.id)) resetForm();
      await onChanged();
    } catch (error) { setMessage(`删除失败：${error.message}`); } finally { setSaving(false); }
  };

  const test = async (id) => {
    setMessage('正在测试 Binance 连接...');
    const res = await fetch(`/api/trading/accounts/${id}/test`, { method: 'POST' });
    const data = await res.json();
    setMessage(data.error ? `连接失败：${data.error}` : `连接成功，账户权益 ${Number(data.equity || 0).toFixed(2)} USDT`);
  };

  const isEditing = mode.type === 'edit';
  const isCreating = mode.type === 'create';
  const canCreate = accounts.length < 5;

  return <div className="account-manager">
    <div className="account-manager-head"><div><h3>交易账户</h3><div className="plain-meta">最多 5 个账户；Secret 只在服务端加密保存。</div></div><div className="account-head-actions"><button type="button" onClick={startCreate} disabled={!canCreate}>{canCreate ? '新增账户' : '已达上限'}</button><button type="button" onClick={() => setOpen((v) => !v)}>{open ? '收起配置' : '管理账户'}</button></div></div>
    {open && <>
      <div className="account-list">{accounts.map((account) => <div className="account-list-row" key={account.id}>
        <strong>{account.name}</strong><span className={`env-chip env-${account.environment}`}>{account.environment === 'prod' ? '正式' : '测试网'}</span><span>{account.api_key_masked || '未配置密钥'}</span><span>最大持仓 {account.max_positions}</span>
        <button type="button" onClick={() => startEdit(account)}>编辑</button><button type="button" onClick={() => test(account.id)}>测试</button><button type="button" className="danger" disabled={saving} onClick={() => remove(account)}>删除</button>
      </div>)}</div>
      {(isCreating || isEditing) && <div className="account-form">
        <div className="account-form-title">{isEditing ? `编辑账户：${form.name}` : '新增账户'}</div>
        <label>账户名称<input value={form.name} onChange={(e) => change('name', e.target.value)} /></label>
        <label>环境<select value={form.environment} onChange={(e) => change('environment', e.target.value)}><option value="testnet">Testnet</option><option value="prod">正式环境</option></select></label>
        <label>期初资金<input type="number" min="0" value={form.initial_capital} onChange={(e) => change('initial_capital', Number(e.target.value))} /></label>
        <label>期初日期<input type="datetime-local" value={(form.initial_capital_time || '').replace(' ', 'T').slice(0, 16)} onChange={(e) => change('initial_capital_time', e.target.value)} /></label>
        <label>最大同时持仓<input type="number" min="1" max="5" value={form.max_positions} onChange={(e) => change('max_positions', Number(e.target.value))} /></label>
        <label>API Key<input value={form.api_key} placeholder={isEditing ? '留空表示不修改' : ''} onChange={(e) => change('api_key', e.target.value)} /></label>
        <label>Secret Key<input type="password" value={form.api_secret} placeholder={isEditing ? '留空表示不修改' : ''} onChange={(e) => change('api_secret', e.target.value)} /></label>
        <label className="check-label"><input type="checkbox" checked={form.auto_trading_enabled} onChange={(e) => change('auto_trading_enabled', e.target.checked)} />自动交易</label>
        <label className="check-label"><input type="checkbox" checked={form.normal_trading_enabled} onChange={(e) => change('normal_trading_enabled', e.target.checked)} />普通策略</label>
        <label className="check-label"><input type="checkbox" checked={form.alpha_trading_enabled} onChange={(e) => change('alpha_trading_enabled', e.target.checked)} />Alpha 策略</label>
        <div className="account-form-actions"><button type="button" onClick={resetForm}>{isEditing ? '取消编辑' : '取消新增'}</button><button type="button" className="primary-action" disabled={saving || !form.name} onClick={save}>{saving ? '保存中...' : (isEditing ? '保存修改' : '创建账户')}</button></div>
      </div>}
      {message && <div className="account-message">{message}</div>}
    </>}
  </div>;
}
