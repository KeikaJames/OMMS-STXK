'use strict';

(() => {
  // 登录人数多时服务器让请求排队，浏览器按服务器给出的秒数一直重试，最多等两分钟。
  const QUEUE_WINDOW_MS = 120000;
  let role = 'student';
  const form = $('#login-form');
  const submit = $('#login-submit');
  const error = $('#login-error');
  const wait = $('#login-wait');

  // 开放与截止时间按学校时区的原始文本显示，与设备时区无关。
  function schoolTime(text) {
    const match = /^(\d{4})-(\d{2})-(\d{2}) (\d{2}):(\d{2})/.exec(text || '');
    return match ? Number(match[2]) + ' 月 ' + Number(match[3]) + ' 日 ' + match[4] + ':' + match[5] : '';
  }

  async function showSchedule() {
    const result = await App.request('/api/check_registration_time');
    const data = result.data;
    if (!result.ok || !data) return;
    const until = data.end_time ? '，截止 ' + schoolTime(data.end_time) : '';
    let text = '报名开放时间待定';
    if (data.registration_locked) text = '报名已截止';
    else if (data.can_register) text = '报名中' + until;
    else if (data.start_time) text = '报名开放 ' + schoolTime(data.start_time) + until;
    const line = $('#login-schedule');
    line.textContent = text;
    line.hidden = false;
  }
  showSchedule();

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
