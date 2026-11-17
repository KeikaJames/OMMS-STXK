'use strict';

(() => {
  async function load() {
    const result = await App.request('/api/get_student_info');
    if (!result.ok || !result.data) {
      App.toast('个人信息读取失败，5 秒后重试', 'err');
      setTimeout(load, 5000);
      return;
    }
    const me = result.data;
    $('#f-name').textContent = me.name;
    $('#f-class').textContent = me.class;
    $('#f-sid').textContent = me.student_id;
    $('#f-user').textContent = me.username;
    const registered = Boolean(me.registered_club);
    $('#reg-empty').hidden = registered;
    $('#reg-detail').hidden = !registered;
    if (registered) {
      $('#reg-name').textContent = me.registered_club;
      $('#reg-time').textContent = '报名时间 ' + (me.registration_time || '').slice(0, 16);
    }
  }

  $('#password-form').addEventListener('submit', async event => {
    event.preventDefault();
    const current = $('#cp').value;
    const next = $('#np').value;
    const error = $('#password-error');
    error.textContent = '';
    if (!current || !next) { error.textContent = '请填写当前密码和新密码'; return; }
    if (next.length < 6) { error.textContent = '新密码至少 6 位'; return; }
    if (next !== $('#np2').value) { error.textContent = '两次输入的新密码不一致'; return; }
    const button = $('#save');
    App.busy(button, '保存中');
    const result = await App.request('/api/change_password', { method: 'POST', body: { current, new: next } });
    App.busy(button, null);
    if (result.ok && result.data && result.data.success) {
      App.toast('密码已修改，请重新登录', 'ok');
      setTimeout(() => { location.href = '/'; }, 900);
      return;
    }
    error.textContent = (result.data && result.data.message) || '保存失败，请重试';
  });

  load();
})();
