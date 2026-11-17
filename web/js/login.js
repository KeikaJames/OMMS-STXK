'use strict';

(() => {
  // 登录人数多时服务器让请求排队，浏览器按服务器给出的秒数一直重试，最多等两分钟。
  const QUEUE_WINDOW_MS = 120000;
  let role = 'student';
  const form = $('#login-form');
  const submit = $('#login-submit');
  const error = $('#login-error');
  const wait = $('#login-wait');

  $$('.seg button').forEach(button => {
    button.addEventListener('click', () => {
      role = button.dataset.role;
      $$('.seg button').forEach(other => other.setAttribute('aria-pressed', String(other === button)));
      $('#username').focus();
    });
  });

  function shouldWait(result) {
    if (result.status === 0 || result.status === 503) return true;
    return result.status === 429 && !(result.data && result.data.code === 'login_throttled');
  }

  async function signIn() {
    const username = $('#username').value.trim();
    const password = $('#password').value;
    if (!username || !password) {
      error.textContent = '请输入用户名和密码';
      return;
    }
    error.textContent = '';
    const started = Date.now();
    const path = role === 'student' ? '/api/login' : '/api/admin_login';
    App.busy(submit, '登录中');
    let result;
    for (;;) {
      result = await App.request(path, { method: 'POST', body: { username, password } });
      if (!shouldWait(result) || Date.now() - started > QUEUE_WINDOW_MS) break;
      App.busy(submit, '排队中');
      wait.hidden = false;
      wait.textContent = '排队中，已等待 ' + Math.round((Date.now() - started) / 1000) + ' 秒';
      await new Promise(resolve => setTimeout(resolve, result.retryAfter * 1000 + Math.random() * 600));
    }
    wait.hidden = true;
    App.busy(submit, null);
    if (result.ok && result.data && result.data.success) {
      location.href = role === 'student' ? '/student/dashboard' : '/admin/dashboard';
      return;
    }
    if (result.status === 0) {
      error.textContent = '网络连接中断';
    } else {
      error.textContent = (result.data && result.data.message) || '登录失败，请重试';
    }
    $('#password').select();
  }

  form.addEventListener('submit', event => {
    event.preventDefault();
    if (!submit.disabled) signIn();
  });
})();
