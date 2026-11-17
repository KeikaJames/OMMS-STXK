'use strict';

// 登录页：身份切换、开放时间、排队重试。人多时服务器让请求排队，浏览器按约定的秒数重试，最多等两分钟。
(function () {
  var QUEUE_WINDOW_MS = 120000;
  var role = 'student';
  var form = $('#login-form');
  var submit = $('#login-submit');
  var error = $('#login-error');
  var wait = $('#login-wait');

  App.request('/api/check_registration_time').then(function (result) {
    var data = result.data;
    if (!result.ok || !data) return;
    var until = data.end_time ? '，截止 ' + App.schoolTime(data.end_time) : '';
    var text = '报名开放时间待定';
    if (data.registration_locked) text = '报名已截止';
    else if (data.can_register) text = '报名中' + until;
    else if (data.start_time) text = '报名开放 ' + App.schoolTime(data.start_time) + until;
    var line = $('#login-schedule');
    line.textContent = text;
    line.hidden = false;
  });

  $$('.segmented button').forEach(function (item) {
    item.addEventListener('click', function () {
      role = item.getAttribute('data-role');
      $$('.segmented button').forEach(function (other) {
        other.setAttribute('aria-pressed', String(other === item));
      });
      $('#username').focus();
    });
  });

  function shouldWait(result) {
    if (result.status === 0 || result.status === 503) return true;
    return result.status === 429 && !(result.data && result.data.code === 'login_throttled');
  }

  function attempt(path, body, started) {
    return App.request(path, { method: 'POST', body: body }).then(function (result) {
      if (!shouldWait(result) || Date.now() - started > QUEUE_WINDOW_MS) return result;
      App.busy(submit, null);
      App.busy(submit, '排队中');
      wait.hidden = false;
      wait.textContent = '排队中，已等待 ' + Math.round((Date.now() - started) / 1000) + ' 秒';
      return new Promise(function (resolve) {
        setTimeout(resolve, result.retryAfter * 1000 + Math.random() * 600);
      }).then(function () {
        return attempt(path, body, started);
      });
    });
  }

  form.addEventListener('submit', function (event) {
    event.preventDefault();
    if (submit.disabled) return;
    var username = $('#username').value.replace(/^\s+|\s+$/g, '');
    var password = $('#password').value;
    if (!username || !password) {
      error.textContent = '请输入账号和密码';
      return;
    }
    error.textContent = '';
    App.busy(submit, '登录中');
    var path = role === 'student' ? '/api/login' : '/api/admin_login';
    attempt(path, { username: username, password: password }, Date.now()).then(function (result) {
      wait.hidden = true;
      App.busy(submit, null);
      if (result.ok && result.data && result.data.success) {
        window.location.href = role === 'student' ? '/student/dashboard' : '/admin/dashboard';
        return;
      }
      error.textContent = result.status === 0 ? '网络连接中断' : (result.data && result.data.message) || '登录未完成';
      $('#password').select();
    });
  });
})();
