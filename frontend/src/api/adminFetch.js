const TOKEN_KEY = 'darkHorseAdminToken';

function readToken() {
  return window.sessionStorage.getItem(TOKEN_KEY) || '';
}

function requestToken() {
  const token = window.prompt(
    '请输入 DarkHorse 管理令牌。服务器可执行：cat /tmp/dark_horse_api_token 查看。',
  );
  const normalized = String(token || '').trim();
  if (!normalized) throw new Error('操作已取消：缺少管理令牌');
  window.sessionStorage.setItem(TOKEN_KEY, normalized);
  return normalized;
}

export async function adminFetch(url, options = {}) {
  const execute = (token) => fetch(url, {
    ...options,
    headers: {
      ...(options.headers || {}),
      'X-Dark-Horse-Token': token,
    },
  });

  let token = readToken() || requestToken();
  let response = await execute(token);
  if (response.status === 401) {
    window.sessionStorage.removeItem(TOKEN_KEY);
    token = requestToken();
    response = await execute(token);
  }
  return response;
}
