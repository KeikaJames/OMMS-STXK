'use strict';

(() => {
  let role = 'student';
  const form = $('#login-form');
  const submit = $('#login-submit');
  const error = $('#login-error');

  $$('.seg button').forEach(button => {
    button.addEventListener('click', () => {
      role = button.dataset.role;
      $$('.seg button').forEach(other => other.setAttribute('aria-pressed', String(other === button)));
      $('#username').focus();
    });
  });

  async function signIn(attempt = 0) {
    const username = $('#username').value.trim();
    const password = $('#password').value;
    if (!username || !password) {
      error.textContent = '请输入用户名和密码';
      return;
    }
    error.textContent = '';
    App.busy(submit, attempt ? '排队中' : '登录中');
    const path = role === 'student' ? '/api/login' : '/api/admin_login';
    const result = await App.request(path, { method: 'POST', body: { username, password } });
    if (result.status === 503 && attempt < 5) {
      // 登录人数较多时按服务器给出的间隔自动重试。
      setTimeout(() => signIn(attempt + 1), result.retryAfter * 1000 + Math.random() * 400);
      return;
    }
    App.busy(submit, null);
    if (result.ok && result.data && result.data.success) {
      location.href = role === 'student' ? '/student/dashboard' : '/admin/dashboard';
      return;
    }
    if (result.status === 0) {
      error.textContent = '网络连接中断，请检查网络后重试';
    } else {
      error.textContent = (result.data && result.data.message) || '登录未完成，请稍后再试';
    }
    $('#password').select();
  }

  form.addEventListener('submit', event => {
    event.preventDefault();
    signIn();
  });
})();
