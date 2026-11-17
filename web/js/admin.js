'use strict';

(() => {
  const IMPORT_BATCH = 200;
  const menu = {
    home: [],
    students: [
      { pane: 'students-import', label: '导入学生' },
      { pane: 'students-list', label: '学生名单' },
      { pane: 'students-accounts', label: '账号发放' },
      { pane: 'students-danger', label: '清空学生' }
    ],
    clubs: [
      { pane: 'clubs-cards', label: '社团卡片' },
      { pane: 'clubs-import', label: '批量添加' }
    ],
    overview: [
      { pane: 'overview-summary', label: '报名概览' },
      { pane: 'overview-adjust', label: '调剂' },
      { pane: 'overview-exports', label: '导出' },
      { pane: 'overview-audit', label: '操作记录' },
      { pane: 'overview-clear', label: '清空报名' }
    ],
    settings: [
      { pane: 'settings-registration', label: '开放与锁定' },
      { pane: 'settings-backup', label: '备份' },
      { pane: 'settings-security', label: '管理员密码' }
    ]
  };
  let view = 'home';
  let primary = null;
  let pendingCredentials = [];
  let importDraft = null;
  let importing = false;
  let editingClub = null;
  let locked = false;
  let overviewTimer = null;
  const knownStudentIds = new Set();
  let directoryReady = false;
  let studentRows = [];
  let openStudentId = null;
  let clubIndex = new Map();
  const audit = { role: 'admin', cursor: null, loading: false };
  const versions = { overview: 0, students: 0, clubs: 0, audit: 0, adjust: 0, clear: 0, backups: 0 };
  const STUDENT_ROWS_SHOWN = 500;

  const ok = result => result.ok && result.data && result.data.success !== false;
  const message = (result, fallback) => (result.data && result.data.message) || (result.status === 0 ? '网络连接中断' : fallback);
  const teacher = name => (!name ? '' : /老师$/.test(name) ? name : name + ' 老师');
  function splitRoom(name) {
    const match = /^(.*?)（\s*([0-9A-Za-z]+)\s*）\s*$/.exec(name || '');
    return match ? { name: match[1].trim(), room: match[2] + ' 室' } : { name, room: null };
  }
  function el(tag, className, text) {
    const node = document.createElement(tag);
    if (className) node.className = className;
    if (text !== undefined) node.textContent = text;
    return node;
  }
  function emptyBody(text, span = 1) {
    const body = el('tbody');
    const row = el('tr');
    const cell = el('td', 'empty', text);
    cell.colSpan = span;
    row.append(cell);
    body.append(row);
    return body;
  }
  function rememberClubs(list) {
    if (Array.isArray(list)) clubIndex = new Map(list.map(club => [Number(club.id), club]));
  }
  function clubLabel(id) {
    const club = clubIndex.get(Number(id));
    return club ? splitRoom(club.name).name : (id ? '社团 #' + id : '');
  }
  function studentLabel(id) {
    const student = studentRows.find(row => String(row.id) === String(id));
    return student ? student.name + '（' + student.class + '）' : '学生 #' + id;
  }
  function meter(fraction, low) {
    const bar = el('div', 'meter' + (low ? ' low' : ''));
    const fill = el('i');
    fill.style.width = Math.max(0, Math.min(100, Math.round(fraction * 100))) + '%';
    bar.append(fill);
    return bar;
  }

  // ---- 路由 ----
  function renderSubnav(selected) {
    const nav = $('#subnav');
    nav.replaceChildren();
    const items = menu[view] || [];
    nav.hidden = !items.length;
    items.forEach(item => {
      const button = el('button', '', item.label);
      button.type = 'button';
      button.dataset.pane = item.pane;
      if (item.pane === selected) button.setAttribute('aria-current', 'page');
      nav.append(button);
    });
  }

  function activate(nextView, pane, options = {}) {
    const items = menu[nextView];
    if (!items) return;
    const selected = items.length ? (items.some(item => item.pane === pane) ? pane : items[0].pane) : null;
    view = nextView;
    const entries = $$('#seg button[data-v]');
    if (options.primaryPane) primary = entries.find(b => b.dataset.v === nextView && b.dataset.pane === options.primaryPane) || null;
    else if (nextView === 'home' || nextView === 'settings') primary = null;
    else if (!primary || primary.dataset.v !== nextView) primary = entries.find(b => b.dataset.v === nextView) || null;
    entries.forEach(button => {
      if (button === primary) button.setAttribute('aria-current', 'page');
      else button.removeAttribute('aria-current');
    });
    $$('section[data-view]').forEach(section => { section.hidden = section.dataset.view !== nextView; });
    const section = $('section[data-view="' + nextView + '"]');
    if (section && selected) {
      section.querySelectorAll('[data-pane]').forEach(node => { node.hidden = node.dataset.pane !== selected; });
    }
    renderSubnav(selected);
    if (options.history !== false) {
      const hash = '#' + (nextView === 'home' ? 'home' : nextView + '/' + selected);
      if (location.hash !== hash) history.pushState(null, '', hash);
    }
    if (nextView === 'home' || nextView === 'overview') loadOverview().finally(scheduleOverview);
    else clearTimeout(overviewTimer);
    if (nextView === 'settings') loadSettings();
    if (selected === 'students-list') loadStudents();
    if (selected === 'overview-audit') loadAudit(true);
    if (selected === 'overview-adjust') loadAdjust();
    if (selected === 'overview-clear') loadClear();
    if (selected === 'settings-backup') loadBackups();
    if (options.focus && section && selected) {
      const heading = section.querySelector('[data-pane="' + selected + '"] h2');
      if (heading) { heading.tabIndex = -1; heading.focus({ preventScroll: true }); }
    }
  }

  function readRoute() {
    const raw = location.hash.replace(/^#/, '');
    if (!raw || raw === 'home') return { view: 'home', pane: null };
    const [nextView, pane] = raw.split('/').map(part => decodeURIComponent(part));
    return menu[nextView] ? { view: nextView, pane: pane || null } : { view: 'home', pane: null };
  }

  $('#seg').addEventListener('click', event => {
    const button = event.target.closest('button[data-v]');
    if (button) activate(button.dataset.v, button.dataset.pane, { focus: true, primaryPane: button.dataset.pane });
  });
  $('#subnav').addEventListener('click', event => {
    const button = event.target.closest('button[data-pane]');
    if (button) activate(view, button.dataset.pane, { focus: true });
  });
  $('#open-settings').addEventListener('click', () => activate('settings', 'settings-registration', { focus: true }));
  $('#home-trigger').addEventListener('click', () => activate('home'));
  $$('[data-home-route]').forEach(button => button.addEventListener('click', () => {
    const [nextView, pane] = button.dataset.homeRoute.split(':');
    activate(nextView, pane, { focus: true, primaryPane: pane });
  }));
  window.addEventListener('popstate', () => { const route = readRoute(); activate(route.view, route.pane, { history: false }); });

  // ---- 概览 ----
  async function loadOverview() {
    const version = ++versions.overview;
    const sent = Date.now();
    const [clubs, summary, time] = await Promise.all([
      App.request('/api/get_clubs'), App.request('/api/admin/summary'), App.request('/api/check_registration_time')
    ]);
    if (version !== versions.overview || !Array.isArray(clubs.data) || !summary.data || !time.data) return;
    rememberClubs(clubs.data);
    renderClockSkew(time.data.server_now, sent, Date.now());
    const total = summary.data.students;
    const registered = summary.data.registered;
    $('#m-clubs').textContent = clubs.data.length;
    $('#m-students').textContent = total;
    $('#m-reg').textContent = registered;
    $('#m-unreg').textContent = total - registered;
    $('#m-rate').textContent = total ? Math.round(registered / total * 100) + '%' : '—';
    $('#home-account-total').textContent = total || '—';
    $('#home-account-count').textContent = total ? '已为 ' + total + ' 名学生创建账号' : '暂无学生账号';
    renderPhase(time.data);
    const fill = $('#ov-fill');
    fill.replaceChildren();
    if (!clubs.data.length) fill.append(el('p', 'empty', '还没有社团'));
    clubs.data.forEach(club => {
      const row = el('div', 'fill-row');
      const info = splitRoom(club.name);
      row.append(el('span', '', info.name + (info.room ? ' · ' + info.room : '')),
        el('span', 'num', club.current_students + ' / ' + club.max_students),
        meter(club.max_students ? club.current_students / club.max_students : 0, club.max_students - club.current_students <= 3));
      fill.append(row);
    });
    const home = $('#home-project-list');
    home.replaceChildren();
    const busiest = clubs.data.slice().sort((a, b) => b.current_students / b.max_students - a.current_students / a.max_students).slice(0, 3);
    if (!busiest.length) home.append(el('p', 'hint', '还没有社团'));
    busiest.forEach(club => {
      const row = el('div', 'fill-row');
      row.append(el('span', '', splitRoom(club.name).name), el('span', 'num', club.current_students + ' / ' + club.max_students),
        meter(club.max_students ? club.current_students / club.max_students : 0));
      home.append(row);
    });
  }

  function renderPhase(time) {
    locked = Boolean(time.registration_locked);
    let title = '开放时间未设置';
    let detail = '在“设置 → 开放与锁定”中设置。';
    const schedule = '开放时间 ' + time.start_time + (time.end_time ? '，截止时间 ' + time.end_time : '');
    if (locked) { title = '名单已锁定'; detail = '可在“报名数据”中调剂和导出。'; }
    else if (time.can_register) { title = '报名中'; detail = schedule; }
    else if (time.start_time) { title = '待开放'; detail = schedule; }
    $('#home-phase').textContent = title;
    $('#home-phase-detail').textContent = detail;
    $('#ov-time').textContent = detail;
  }

  // 旧电脑的主板电池没电时时钟常常不准；报名按服务器时钟开放，相差超过一分钟就提醒。
  function renderClockSkew(serverNow, sent, received) {
    const note = $('#home-clock');
    if (typeof serverNow !== 'number') return;
    const skew = serverNow - (sent + received) / 2;
    if (Math.abs(skew) < 60000) {
      note.hidden = true;
      return;
    }
    const seconds = Math.round(Math.abs(skew) / 1000);
    const hours = Math.floor(seconds / 3600);
    const parts = [hours ? hours + ' 小时 ' : '', Math.floor(seconds % 3600 / 60) + ' 分 ', seconds % 60 + ' 秒'];
    note.textContent = '服务器时间比本机' + (skew < 0 ? '慢 ' : '快 ') + parts.join('') +
      '。报名按服务器时间开放，请校准服务器时间。';
    note.hidden = false;
  }

  function scheduleOverview() {
    clearTimeout(overviewTimer);
    if (document.hidden || !['home', 'overview'].includes(view)) return;
    overviewTimer = setTimeout(() => loadOverview().finally(scheduleOverview), 6000);
  }
  document.addEventListener('visibilitychange', () => {
    if (document.hidden) clearTimeout(overviewTimer);
    else if (['home', 'overview'].includes(view)) loadOverview().finally(scheduleOverview);
  });

  // ---- 学生名单：查找、调整报名、重置密码 ----
  async function loadStudents() {
    const version = ++versions.students;
    const [result, clubs] = await Promise.all([App.request('/api/get_all_students'), App.request('/api/get_clubs')]);
    if (version !== versions.students) return;
    rememberClubs(clubs.data);
    if (!Array.isArray(result.data)) {
      directoryReady = false;
      $('#stu-tbl').replaceChildren(emptyBody('学生名单读取失败，请刷新页面'));
      return;
    }
    studentRows = result.data;
    fillClassSelect();
    knownStudentIds.clear();
    studentRows.forEach(student => knownStudentIds.add(String(student.student_id)));
    directoryReady = true;
    if (importDraft) updateImportCheck(true);
    renderStudents();
  }

  function renderStudents() {
    const table = $('#stu-tbl');
    const count = $('#stu-count');
    table.replaceChildren();
    if (!studentRows.length) {
      count.textContent = '';
      table.append(emptyBody('暂无学生'));
      return;
    }
    const query = $('#stu-search').value.trim().toLowerCase();
    const matches = !query ? studentRows : studentRows.filter(student =>
      [student.name, student.class, student.student_id, student.username, student.club_id ? clubLabel(student.club_id) : '未报名']
        .some(value => String(value || '').toLowerCase().includes(query)));
    const registered = studentRows.filter(student => student.club_id).length;
    count.textContent = (query ? '匹配 ' + matches.length + ' 名 · ' : '') + '共 ' + studentRows.length + ' 名学生，已报名 ' + registered + ' 名' +
      (matches.length > STUDENT_ROWS_SHOWN ? '；显示前 ' + STUDENT_ROWS_SHOWN + ' 名，可按姓名或学号查找' : '');
    const head = el('thead');
    const headRow = el('tr');
    ['姓名', '班级', '学号', '账号', '社团', ''].forEach(label => headRow.append(el('th', '', label)));
    head.append(headRow);
    if (!matches.length) {
      table.append(head, emptyBody('无匹配结果', 6));
      return;
    }
    const body = el('tbody');
    matches.slice(0, STUDENT_ROWS_SHOWN).forEach(student => {
      const row = el('tr');
      const club = el('td', student.club_id ? '' : 'muted', student.club_id ? clubLabel(student.club_id) : '未报名');
      row.append(el('td', '', student.name), el('td', '', student.class), el('td', 'mono', student.student_id), el('td', 'mono', student.username), club);
      const cell = el('td', 'end');
      const manage = el('button', 'btn btn-quiet btn-sm', openStudentId === student.id ? '收起' : '管理');
      manage.type = 'button';
      manage.setAttribute('aria-expanded', String(openStudentId === student.id));
      manage.addEventListener('click', () => {
        openStudentId = openStudentId === student.id ? null : student.id;
        renderStudents();
        const panel = $('#student-panel select');
        if (panel) panel.focus();
      });
      cell.append(manage);
      row.append(cell);
      body.append(row);
      if (openStudentId === student.id) body.append(studentPanel(student));
    });
    table.append(head, body);
  }

  function studentPanel(student) {
    const row = el('tr', 'row-detail');
    const cell = el('td');
    cell.colSpan = 6;
    const panel = el('div', 'student-panel');
    panel.id = 'student-panel';
    const field = (label, input) => { const wrap = el('label', 'field'); wrap.append(el('span', '', label), input); return wrap; };
    const textInput = (value, max) => { const input = el('input', 'input'); input.value = value || ''; input.maxLength = max; return input; };
    const nameInput = textInput(student.name, 50);
    const classInput = textInput(student.class, 50);
    const numberInput = textInput(student.student_id, 40);
    const info = el('div', 'row3');
    info.append(field('姓名', nameInput), field('班级', classInput), field('学号', numberInput));
    const saveInfo = el('button', 'btn btn-secondary btn-sm', '保存信息');
    saveInfo.type = 'button';
    const infoActions = el('div', 'actions');
    infoActions.append(saveInfo);
    const select = el('select', 'input');
    const none = el('option', '', '未报名');
    none.value = '';
    select.append(none);
    Array.from(clubIndex.values()).forEach(club => {
      const left = club.max_students - club.current_students;
      const current = Number(club.id) === Number(student.club_id);
      const option = el('option', '', splitRoom(club.name).name + ' · ' + club.current_students + '/' + club.max_students + (current ? '（当前）' : left <= 0 ? '（已满）' : ''));
      option.value = String(club.id);
      option.disabled = !current && left <= 0;
      select.append(option);
    });
    select.value = student.club_id ? String(student.club_id) : '';
    const override = el('input');
    override.type = 'checkbox';
    const overrideLabel = el('label', 'check');
    overrideLabel.append(override, document.createTextNode('允许安排到报名范围以外或暂停报名的社团'));
    const reason = el('input', 'input');
    reason.maxLength = 200;
    reason.placeholder = '例如：班主任代报';
    const error = el('p', 'error');
    error.setAttribute('role', 'alert');
    const save = el('button', 'btn btn-primary btn-sm', '保存社团');
    save.type = 'button';
    const reset = el('button', 'btn btn-secondary btn-sm', '重置密码');
    reset.type = 'button';
    const remove = el('button', 'btn btn-quiet btn-sm', '删除学生');
    remove.type = 'button';
    const output = el('div', 'reset-result');
    output.hidden = true;
    const actions = el('div', 'actions');
    actions.append(save, reset, remove);
    const columns = el('div', 'row2');
    columns.append(field('社团', select), field('原因（记入操作记录）', reason));
    panel.append(info, infoActions, el('hr'), columns, overrideLabel, error, actions, output);
    cell.append(panel);
    row.append(cell);

    const ids = { move: App.requestId(), reset: App.requestId(), info: App.requestId() };
    [select, override, reason].forEach(input => input.addEventListener('input', () => { ids.move = App.requestId(); }));
    [nameInput, classInput, numberInput].forEach(input => input.addEventListener('input', () => { ids.info = App.requestId(); }));

    saveInfo.addEventListener('click', async () => {
      const body = { student_id: student.id, name: nameInput.value.trim(), class: classInput.value.trim(),
        student_no: numberInput.value.trim(), request_id: ids.info };
      if (!body.name || !body.class || !body.student_no) { error.textContent = '请填写姓名、班级和学号'; return; }
      error.textContent = '';
      App.busy(saveInfo, '保存中');
      const result = await adminRetry('/api/admin/update_student', body);
      App.busy(saveInfo, null);
      if (!ok(result)) { error.textContent = message(result, '保存失败'); return; }
      ids.info = App.requestId();
      App.toast(result.data.changed === false ? '信息没有变化' : '已保存 ' + body.name + ' 的信息', 'ok');
      await loadStudents();
    });

    save.addEventListener('click', async () => {
      const target = select.value ? Number(select.value) : null;
      const current = student.club_id ? Number(student.club_id) : null;
      if (target === current) { error.textContent = '请选择社团'; return; }
      if (!reason.value.trim()) { error.textContent = '请填写原因'; reason.focus(); return; }
      const body = { student_id: student.id, request_id: ids.move, reason: reason.value.trim() };
      let path = '/api/admin/transfer_registration';
      if (!current) path = '/api/admin/assign_registration';
      else if (!target) path = '/api/admin/remove_registration';
      if (target) body.club_id = target;
      if (target && override.checked) body.override_eligibility = true;
      error.textContent = '';
      App.busy(save, '保存中');
      const result = await adminRetry(path, body);
      App.busy(save, null);
      if (!ok(result)) {
        const text = message(result, '保存失败');
        error.textContent = target && !override.checked && result.status === 400 && /限制|开放/.test(text)
          ? text + '。如需安排，请勾选“允许安排到报名范围以外或暂停报名的社团”。' : text;
        return;
      }
      const done = !current ? '已为 ' + student.name + ' 报名 ' + clubLabel(target)
        : target ? student.name + ' 已调整到 ' + clubLabel(target) : student.name + ' 已从 ' + clubLabel(current) + ' 移出';
      App.toast(done, 'ok');
      openStudentId = null;
      await loadStudents();
      loadOverview();
    });

    reset.addEventListener('click', () => {
      if (!reason.value.trim()) { error.textContent = '请填写原因'; reason.focus(); return; }
      error.textContent = '';
      confirmInline(reset, async () => {
        const result = await adminRetry('/api/admin/reset_student_password', { student_id: student.id, request_id: ids.reset, reason: reason.value.trim() });
        if (!ok(result)) throw new Error(message(result, '重置失败'));
        ids.reset = App.requestId();
        output.replaceChildren();
        if (result.data.temporary_password) {
          output.append(document.createTextNode('账号 '), el('b', 'mono', result.data.username), document.createTextNode(' 的临时密码：'),
            el('b', 'mono', result.data.temporary_password), el('small', '', '临时密码仅显示一次，请交给学生本人，登录后修改。'));
        } else {
          output.append(el('small', '', result.data.message || '密码已重置'));
        }
        output.hidden = false;
      });
    });

    remove.addEventListener('click', () => confirmInline(remove, async () => {
      const deleted = await App.request('/api/delete_student', { method: 'POST', body: { student_id: student.id } });
      if (!ok(deleted)) throw new Error(message(deleted, '删除失败'));
      App.toast('已删除 ' + student.name, 'ok');
      openStudentId = null;
      await Promise.all([loadStudents(), loadOverview()]);
    }));
    return row;
  }

  async function adminRetry(path, body) {
    let result;
    for (let attempt = 0; attempt < 4; attempt += 1) {
      result = await App.request(path, { method: 'POST', body });
      if (result.status !== 503 && result.status !== 0) return result;
      await new Promise(resolve => setTimeout(resolve, result.retryAfter * 1000));
    }
    return result;
  }

  $('#stu-search').addEventListener('input', () => { openStudentId = null; renderStudents(); });

  // ---- 操作记录 ----
  const actionLabels = {
    'registration.created': '报名', 'registration.cancelled': '退选',
    'registration.admin_assigned': '代为报名', 'registration.admin_removed': '移出社团', 'registration.admin_transferred': '调整社团',
    'registration.start_time_updated': '设置开放时间', 'registration.end_time_updated': '设置截止时间', 'registration.lock_updated': '名单锁定',
    'student.password_reset': '重置学生密码', 'student.password_changed': '学生修改密码', 'student.deleted': '删除学生',
    'student.updated': '修改学生信息', 'students.passwords_reset': '按班重置密码',
    'students.imported': '导入学生', 'students.deleted_all': '清空学生',
    'club.updated': '编辑社团', 'club.deleted': '删除社团', 'club.image_uploaded': '更新社团图片', 'club.image_removed': '移除社团图片',
    'clubs.imported': '添加社团', 'clubs.deleted_all': '清空社团', 'admin.password_changed': '修改管理员密码',
    'registrations.adjusted': '调剂', 'registrations.cleared': '清空报名', 'admin.password_reset': '重置管理员密码'
  };

  function auditTime(ms) {
    const date = new Date(Number(ms));
    if (Number.isNaN(date.getTime())) return '';
    return App.pad(date.getMonth() + 1) + '-' + App.pad(date.getDate()) + ' ' + App.pad(date.getHours()) + ':' + App.pad(date.getMinutes()) + ':' + App.pad(date.getSeconds());
  }

  function auditTarget(event) {
    const before = event.before || {};
    const after = event.after || {};
    const studentKnown = id => studentRows.some(row => String(row.id) === String(id));
    switch (event.target_type) {
      case 'student': return !studentKnown(event.target_id) && before.name ? before.name + (before.class ? '（' + before.class + '）' : '') : studentLabel(event.target_id);
      case 'club': return !clubIndex.has(Number(event.target_id)) && before.name ? splitRoom(before.name).name : clubLabel(event.target_id);
      case 'registration': return clubLabel(after.club_id || before.club_id);
      case 'students':
        if (before.class) return before.class + ' · ' + after.count + ' 名';
        return after.success !== undefined ? after.success + ' 名' : before.students !== undefined ? before.students + ' 名' : '全部学生';
      case 'clubs': return after.success !== undefined ? after.success + ' 个' : '全部社团';
      case 'registrations': return '未报名学生';
      default: return '';
    }
  }

  function auditDetail(event) {
    const before = event.before || {};
    const after = event.after || {};
    const parts = [];
    if (event.action === 'registration.admin_assigned') parts.push('报名 ' + clubLabel(after.club_id));
    if (event.action === 'registration.admin_removed') parts.push('从 ' + clubLabel(before.club_id) + ' 移出');
    if (event.action === 'registration.admin_transferred') parts.push(clubLabel(before.club_id) + ' → ' + clubLabel(after.club_id));
    if (event.action === 'registration.start_time_updated') parts.push(after.start_time || '清除');
    if (event.action === 'registration.end_time_updated') parts.push(after.end_time || '清除');
    if (event.action === 'registration.lock_updated') parts.push(after.locked ? '锁定' : '解除锁定');
    if (event.action === 'registrations.cleared') parts.push('删除 ' + after.cleared + ' 条报名');
    if (event.action === 'student.updated') {
      [['name', '姓名'], ['class', '班级'], ['student_no', '学号']].forEach(([key, label]) => {
        if (before[key] !== after[key]) parts.push(label + ' ' + before[key] + ' → ' + after[key]);
      });
    }
    if (event.action === 'registrations.adjusted') {
      parts.push('安排 ' + after.assigned + ' 人' + (after.unassigned ? '，' + after.unassigned + ' 人无可用名额' : ''));
    }
    if (event.reason) parts.push(event.reason);
    return parts.filter(Boolean).join(' · ');
  }

  async function loadAudit(reset) {
    if (audit.loading && !reset) return;
    const version = ++versions.audit;
    audit.loading = true;
    if (reset) audit.cursor = null;
    if (!studentRows.length) await loadStudents();
    const params = new URLSearchParams({ limit: '50' });
    if (audit.role) params.set('role', audit.role);
    if (audit.cursor) params.set('cursor', String(audit.cursor));
    const result = await App.request('/api/admin/audit_events?' + params);
    if (version !== versions.audit) return;
    audit.loading = false;
    const table = $('#audit-tbl');
    const more = $('#audit-more');
    if (!result.data || !Array.isArray(result.data.events)) {
      table.replaceChildren(emptyBody(message(result, '操作记录读取失败')));
      more.hidden = true;
      return;
    }
    const events = result.data.events;
    if (reset) {
      const head = el('thead');
      const headRow = el('tr');
      ['时间', '操作人', '操作', '对象', '说明'].forEach(label => headRow.append(el('th', '', label)));
      head.append(headRow);
      table.replaceChildren(head, el('tbody'));
      if (!events.length) table.replaceChildren(head, emptyBody('暂无记录', 5));
    }
    const body = table.querySelector('tbody');
    events.forEach(event => {
      const row = el('tr');
      const actor = event.actor_role === 'student' ? studentLabel(event.actor_id) : event.actor_id;
      row.append(el('td', 'num', auditTime(event.occurred_at)), el('td', '', actor), el('td', '', actionLabels[event.action] || event.action),
        el('td', '', auditTarget(event)), el('td', 'wrap', auditDetail(event)));
      body.append(row);
    });
    audit.cursor = result.data.next_cursor;
    more.hidden = events.length < 50 || !audit.cursor;
  }

  $('#audit-scope').addEventListener('click', event => {
    const button = event.target.closest('button[data-role]');
    if (!button) return;
    audit.role = button.dataset.role;
    $$('#audit-scope button').forEach(item => item.setAttribute('aria-pressed', String(item === button)));
    loadAudit(true);
  });
  $('#audit-more').addEventListener('click', () => loadAudit(false));

  function confirmInline(button, action) {
    if (button.dataset.armed) return;
    const label = button.textContent;
    button.dataset.armed = '1';
    button.textContent = '确认' + label;
    const reset = () => { button.textContent = label; delete button.dataset.armed; };
    const timer = setTimeout(reset, 3000);
    button.addEventListener('click', async () => {
      clearTimeout(timer);
      reset();
      button.disabled = true;
      try { await action(); } catch (error) { App.toast(error.message || '操作失败', 'err'); }
      finally { button.disabled = false; }
    }, { once: true });
  }

  // ---- 导入学生：识别列、分批创建、汇总账号 ----
  const fields = ['name', 'class', 'student_id'];
  const fieldLabels = { name: '姓名', class: '班级', student_id: '学号' };
  const aliases = {
    name: ['姓名', '学生姓名', '学生', 'name'],
    class: ['班级', '班号', '行政班', '班级号', 'class'],
    student_id: ['学号', '学生学号', '学籍号', 'studentid', 'id']
  };
  const normalizeHeader = value => String(value || '').trim().toLowerCase().replace(/[\s_\-（）()]/g, '');
  const headerField = value => fields.find(field => aliases[field].includes(normalizeHeader(value))) || null;
  const idLike = value => /^[A-Za-z0-9_-]+$/.test(String(value || '').trim());

  function buildDraft(text) {
    const rows = [];
    let columns = 0;
    text.split(/\n/).forEach((raw, index) => {
      const line = raw.trim();
      if (!line) return;
      const cells = line.split(/[,，\t]+/).map(value => value.trim());
      columns = Math.max(columns, cells.length);
      rows.push({ line: index + 1, cells });
    });
    if (!rows.length) return null;
    const headerMap = {};
    rows[0].cells.forEach((cell, index) => {
      const field = headerField(cell);
      if (field && headerMap[field] === undefined) headerMap[field] = index;
    });
    const hasHeader = fields.every(field => headerMap[field] !== undefined);
    const data = hasHeader ? rows.slice(1) : rows;
    const stats = Array.from({ length: columns }, (_, index) => {
      const values = data.map(row => row.cells[index] || '').filter(Boolean);
      const ratio = test => values.length ? values.filter(test).length / values.length : 0;
      return {
        index, values,
        chinese: ratio(value => /[㐀-鿿]/.test(value)),
        numeric: ratio(idLike),
        classy: ratio(value => /班|年|级/.test(value)),
        unique: values.length ? new Set(values).size / values.length : 0
      };
    });
    const mapping = hasHeader ? { ...headerMap } : {};
    let confident = hasHeader;
    if (!hasHeader && data.length) {
      const name = stats.slice().sort((a, b) => b.chinese - a.chinese)[0];
      if (name && name.chinese >= .5) mapping.name = name.index;
      const rest = stats.filter(stat => stat.index !== mapping.name && stat.values.length);
      const explicit = rest.slice().sort((a, b) => b.classy - a.classy)[0];
      const repeated = rest.filter(stat => stat.numeric >= .8 && stat.unique < .85).sort((a, b) => a.unique - b.unique)[0];
      const klass = explicit && explicit.classy >= .5 ? explicit : repeated;
      if (klass) mapping.class = klass.index;
      const id = rest.filter(stat => stat.index !== mapping.class && stat.numeric >= .7).sort((a, b) => b.unique - a.unique)[0];
      if (id) mapping.student_id = id.index;
      confident = Boolean(klass && id && new Set(fields.map(field => mapping[field])).size === 3 && (klass.classy >= .5 || data.length > 1));
    }
    return { data, columns, stats, mapping, confident, confirmed: hasHeader };
  }

  function validateDraft(draft) {
    if (!draft) return { students: [], issues: [], ready: false };
    const ready = fields.every(field => Number.isInteger(draft.mapping[field])) && new Set(fields.map(field => draft.mapping[field])).size === 3;
    if (!ready) return { students: [], issues: ['请为姓名、班级和学号分别选择不同的列'], ready: false };
    const students = [];
    const issues = [];
    const seen = new Set();
    const bad = value => !value || value.length > 50 || /[<>\u0000-\u001F]/.test(value);
    draft.data.forEach(row => {
      const student = {
        name: (row.cells[draft.mapping.name] || '').trim(),
        class: (row.cells[draft.mapping.class] || '').trim(),
        student_id: (row.cells[draft.mapping.student_id] || '').trim()
      };
      if (bad(student.name) || bad(student.class) || bad(student.student_id)) issues.push('第 ' + row.line + ' 行内容不完整或含有不支持的字符');
      else if (seen.has(student.student_id)) issues.push('第 ' + row.line + ' 行学号在名单里重复');
      else if (knownStudentIds.has(student.student_id)) issues.push('第 ' + row.line + ' 行学号已经导入过');
      else { seen.add(student.student_id); students.push(student); }
    });
    return { students, issues, ready };
  }

  function renderPreview() {
    const box = $('#imp-stu-preview');
    box.replaceChildren();
    if (!importDraft) { box.hidden = true; return; }
    box.hidden = false;
    const check = validateDraft(importDraft);
    box.append(el('p', 'hint', '读取到 ' + importDraft.data.length + ' 行' + (importDraft.confident || importDraft.confirmed ? '，已识别各列' : '，请确认各列对应关系')));
    const map = el('div', 'column-map');
    fields.forEach(field => {
      const label = el('label', '', fieldLabels[field]);
      const select = el('select', 'input');
      select.append(new Option('选择列', ''));
      for (let index = 0; index < importDraft.columns; index += 1) {
        const sample = (importDraft.stats[index] && importDraft.stats[index].values.slice(0, 2).join('、')) || '空列';
        const option = new Option('第 ' + (index + 1) + ' 列 · ' + sample, String(index));
        option.selected = importDraft.mapping[field] === index;
        select.append(option);
      }
      select.addEventListener('change', () => {
        importDraft.mapping[field] = select.value === '' ? undefined : Number(select.value);
        importDraft.confirmed = true;
        updateImportCheck(true);
      });
      label.append(select);
      map.append(label);
    });
    box.append(map);
    const samples = el('div', 'samples');
    check.students.slice(0, 3).forEach(student => samples.append(el('span', '', student.name + ' · ' + student.class + ' · ' + student.student_id)));
    if (samples.childElementCount) box.append(samples);
    if (check.issues.length) {
      box.append(el('p', 'issues', check.issues.slice(0, 3).join('；') + (check.issues.length > 3 ? '；另有 ' + (check.issues.length - 3) + ' 处' : '')));
    }
  }

  function updateImportCheck(keepDraft = false) {
    if (!keepDraft) importDraft = buildDraft($('#imp-stu').value);
    renderPreview();
    const note = $('#imp-stu-check');
    const button = $('#imp-stu-go');
    button.disabled = true;
    if (!importDraft) { note.textContent = '粘贴后显示识别结果。'; button.textContent = '创建账号'; return; }
    if (pendingCredentials.length) { note.textContent = '上一批账号尚未下载，请先在“账号发放”中下载。'; return; }
    if (!directoryReady) { note.textContent = '正在核对已导入的学号'; return; }
    if (!importDraft.confident && !importDraft.confirmed) { note.textContent = '请确认姓名、班级和学号分别在哪一列。'; return; }
    const check = validateDraft(importDraft);
    note.textContent = check.issues.length ? check.issues[0] + '，修改后再导入。' : '共 ' + check.students.length + ' 名学生，确认后创建账号。';
    button.textContent = check.students.length ? '为 ' + check.students.length + ' 名学生创建账号' : '创建账号';
    button.disabled = importing || !check.students.length || Boolean(check.issues.length);
  }

  async function importStudents() {
    if (importing) return;
    const check = validateDraft(importDraft);
    if (!check.students.length || check.issues.length) return;
    importing = true;
    const button = $('#imp-stu-go');
    App.busy(button, '正在创建');
    const progress = $('#imp-stu-progress');
    const progressText = $('#imp-stu-progress-text');
    progress.hidden = false;
    let created = 0;
    let failed = 0;
    const credentials = [];
    let stoppedAt = null;
    for (let start = 0; start < check.students.length; start += IMPORT_BATCH) {
      const batch = check.students.slice(start, start + IMPORT_BATCH);
      progressText.textContent = '正在创建 ' + (start + 1) + '–' + (start + batch.length) + ' / ' + check.students.length;
      progress.querySelector('i').style.width = Math.round(start / check.students.length * 100) + '%';
      const result = await App.request('/api/import_students', { method: 'POST', body: { students: batch } });
      if (!result.ok || !result.data || !Array.isArray(result.data.credentials)) {
        stoppedAt = start;
        App.toast(message(result, '导入中断'), 'err');
        break;
      }
      created += Number(result.data.success) || 0;
      failed += Number(result.data.failed) || 0;
      credentials.push(...result.data.credentials);
    }
    progress.querySelector('i').style.width = '100%';
    progressText.textContent = '已创建 ' + created + ' 个账号' + (failed ? '，' + failed + ' 行学号重复未导入' : '');
    $('#imp-stu-result').textContent = stoppedAt === null ? '' : '第 ' + (stoppedAt + 1) + ' 名起未导入。已创建的账号可先下载，其余名单重新粘贴后继续导入。';
    App.busy(button, null);
    importing = false;
    if (credentials.length) {
      pendingCredentials = credentials;
      renderCredentials();
      $('#imp-stu').value = '';
      updateImportCheck();
      loadStudents();
      loadOverview();
      activate('students', 'students-accounts', { focus: true, primaryPane: 'students-accounts' });
    } else {
      updateImportCheck(true);
    }
  }

  function csvCell(value) {
    let text = String(value == null ? '' : value);
    if (/^[=+\-@\t\r]/.test(text)) text = "'" + text;
    return '"' + text.replace(/"/g, '""') + '"';
  }

  function downloadCredentials() {
    const rows = [['姓名', '班级', '账号', '初始密码']].concat(pendingCredentials.map(item => [item.name, item.class || '', item.username, item.password]));
    const csv = '﻿' + rows.map(row => row.map(csvCell).join(',')).join('\r\n');
    const link = el('a');
    link.href = URL.createObjectURL(new Blob([csv], { type: 'text/csv;charset=utf-8' }));
    link.download = '账号密码表_' + new Date().toISOString().slice(0, 10) + '.csv';
    document.body.append(link);
    link.click();
    link.remove();
    setTimeout(() => URL.revokeObjectURL(link.href), 1000);
  }

  function fillClassSelect() {
    const select = $('#reset-class');
    const current = select.value;
    const counts = new Map();
    studentRows.forEach(student => counts.set(student.class, (counts.get(student.class) || 0) + 1));
    select.replaceChildren();
    Array.from(counts.keys()).sort((a, b) => a.localeCompare(b, 'zh-Hans-CN', { numeric: true })).forEach(klass => {
      const option = new Option(klass + '（' + counts.get(klass) + ' 人）', klass);
      option.selected = klass === current;
      select.append(option);
    });
    $('#reset-class-go').disabled = !counts.size;
  }

  let classResetId = App.requestId();
  $('#reset-class').addEventListener('change', () => { classResetId = App.requestId(); });
  $('#reset-class-go').addEventListener('click', () => {
    const klass = $('#reset-class').value;
    const reason = $('#reset-class-reason').value.trim();
    if (!klass || !reason) { App.toast('请选择班级并填写原因', 'err'); return; }
    if (pendingCredentials.length) { App.toast('本页还有未清除的账号密码，请先下载或清除', 'err'); return; }
    confirmInline($('#reset-class-go'), async () => {
      const result = await adminRetry('/api/admin/reset_class_passwords', { class: klass, reason, request_id: classResetId });
      if (!ok(result)) throw new Error(message(result, '重置失败'));
      classResetId = App.requestId();
      $('#reset-class-reason').value = '';
      if (result.data.credentials.length) {
        pendingCredentials = result.data.credentials;
        renderCredentials();
      }
      App.toast(klass + ' ' + result.data.count + ' 名学生的密码已重置', 'ok');
    });
  });

  function slipOrigin() {
    try { return localStorage.getItem('omms-slip-origin') || location.origin; } catch (error) { return location.origin; }
  }

  function printSlips(origin) {
    try { localStorage.setItem('omms-slip-origin', origin); } catch (error) { /* 仅记住上次填写的地址 */ }
    const groups = new Map();
    pendingCredentials.forEach(item => {
      const klass = item.class || '';
      if (!groups.has(klass)) groups.set(klass, []);
      groups.get(klass).push(item);
    });
    const row = (label, value, wide) => { const line = el('div', wide ? 'slip-row wide' : 'slip-row'); line.append(el('small', '', label), el('span', '', value)); return line; };
    const sheet = el('div', 'print-sheet');
    sheet.id = 'print-sheet';
    Array.from(groups.keys()).sort((a, b) => a.localeCompare(b, 'zh-Hans-CN', { numeric: true })).forEach(klass => {
      const section = el('section', 'print-class');
      section.append(el('h2', '', (klass || '未分班') + ' · ' + groups.get(klass).length + ' 人'));
      const slips = el('div', 'slips');
      groups.get(klass).forEach(item => {
        const slip = el('div', 'slip');
        slip.append(el('b', '', item.name + '　' + (item.class || '')), row('账号', item.username), row('初始密码', item.password), row('登录地址', origin, true));
        slips.append(slip);
      });
      section.append(slips);
      sheet.append(section);
    });
    removePrintSheet();
    document.body.append(sheet);
    window.print();
  }

  function removePrintSheet() {
    const sheet = document.getElementById('print-sheet');
    if (sheet) sheet.remove();
  }
  window.addEventListener('afterprint', removePrintSheet);

  function renderCredentials() {
    const box = $('#imp-stu-out');
    box.replaceChildren();
    if (!pendingCredentials.length) {
      removePrintSheet();
      box.append(el('p', 'empty', '暂无待发放的账号。'));
      return;
    }
    const head = el('div', 'actions');
    const download = el('button', 'btn btn-primary', '下载账号密码表');
    download.type = 'button';
    download.addEventListener('click', downloadCredentials);
    const origin = el('input', 'input');
    origin.id = 'slip-origin';
    origin.value = slipOrigin();
    const print = el('button', 'btn btn-secondary', '打印账号条');
    print.type = 'button';
    print.addEventListener('click', () => printSlips(origin.value.trim() || location.origin));
    const done = el('button', 'btn btn-quiet', '清除本页密码');
    done.type = 'button';
    done.addEventListener('click', () => {
      pendingCredentials = [];
      renderCredentials();
      updateImportCheck();
      App.toast('已从本页清除密码', 'ok');
    });
    head.append(download, print, done);
    const originField = el('label', 'field');
    originField.append(el('span', '', '登录地址（印在账号条上）'), origin);
    box.append(el('p', 'hint', '本页 ' + pendingCredentials.length + ' 个账号'), originField, head);
    const wrap = el('div', 'table-wrap');
    const table = el('table', 'tbl');
    const header = el('tr');
    ['姓名', '班级', '账号', '初始密码'].forEach(label => header.append(el('th', '', label)));
    const thead = el('thead');
    thead.append(header);
    const tbody = el('tbody');
    pendingCredentials.forEach(item => {
      const row = el('tr');
      row.append(el('td', '', item.name), el('td', '', item.class || ''), el('td', 'mono', item.username), el('td', 'mono', item.password));
      tbody.append(row);
    });
    table.append(thead, tbody);
    wrap.append(table);
    box.append(wrap);
  }

  // ---- 社团卡片与编辑 ----
  async function loadClubs() {
    const version = ++versions.clubs;
    const result = await App.request('/api/get_clubs');
    if (version !== versions.clubs) return;
    rememberClubs(result.data);
    const box = $('#club-list');
    box.replaceChildren();
    if (!Array.isArray(result.data)) { box.append(el('p', 'empty', '社团读取失败')); return; }
    if (!result.data.length) { box.append(el('p', 'empty', '暂无社团。在“批量添加”中添加。')); return; }
    result.data.forEach(club => box.append(clubCard(club)));
  }

  function clubArt(club) {
    const art = el('div', 'club-art');
    art.dataset.tone = String((Number(club.id) % 5) + 1);
    art.append(ClubArt.node(club));
    return art;
  }

  function clubCard(club) {
    const card = el('article', 'club');
    const body = el('div', 'club-body');
    const info = splitRoom(club.name);
    body.append(el('h3', 'club-name', info.name));
    const meta = [info.room, club.location, club.meeting_time, teacher(club.advisor_name)].filter(Boolean).join(' · ');
    if (meta) body.append(el('p', 'club-meta', meta));
    if (club.description) body.append(el('p', 'club-desc', club.description));
    const tags = el('div', 'club-tags');
    if (!club.enabled) tags.append(el('span', 'tag tag-warn', '暂停报名'));
    if ((club.allowed_grades || []).length) tags.append(el('span', 'tag', '限' + club.allowed_grades.join('、')));
    if ((club.allowed_classes || []).length) tags.append(el('span', 'tag', '限 ' + club.allowed_classes.length + ' 个班级'));
    if (club.seat_sync_pending) tags.append(el('span', 'tag tag-warn', '名额更新中'));
    if (tags.childElementCount) body.append(tags);
    const seats = el('div', 'seats');
    seats.append(el('span', '', '已报名'), el('b', 'num', String(club.current_students)), el('span', '', '/ ' + club.max_students));
    body.append(seats, meter(club.max_students ? club.current_students / club.max_students : 0, club.max_students - club.current_students <= 3));
    const actions = el('div', 'actions');
    const edit = el('button', 'btn btn-primary btn-sm', '编辑');
    edit.type = 'button';
    const exportLink = el('a', 'btn btn-secondary btn-sm', '导出名单');
    exportLink.href = '/api/export_club_data?club_id=' + club.id;
    const remove = el('button', 'btn btn-quiet btn-sm', '删除');
    remove.type = 'button';
    remove.addEventListener('click', () => confirmInline(remove, async () => {
      const result = await App.request('/api/delete_club', { method: 'POST', body: { club_id: club.id } });
      if (!ok(result)) throw new Error(message(result, '删除失败'));
      App.toast('已删除 ' + info.name, 'ok');
      await Promise.all([loadClubs(), loadOverview()]);
    }));
    actions.append(edit, exportLink, remove);
    body.append(actions);
    card.append(clubArt(club), body);
    edit.addEventListener('click', () => {
      if (editingClub !== null && editingClub !== club.id) { App.toast('请先保存或取消正在编辑的社团', 'err'); return; }
      editingClub = club.id;
      body.replaceWith(editor(club));
    });
    return card;
  }

  function editor(club) {
    const form = el('form', 'club-body editor');
    form.noValidate = true;
    const field = (label, input) => { const wrap = el('label', 'field'); wrap.append(el('span', '', label), input); return wrap; };
    const text = (value, max) => { const input = el('input', 'input'); input.value = value || ''; input.maxLength = max; return input; };
    const name = text(club.name, 80);
    const capacity = el('input', 'input');
    capacity.type = 'number';
    capacity.min = String(Math.max(1, club.current_students));
    capacity.max = '10000';
    capacity.value = String(club.max_students);
    const description = el('textarea', 'input');
    description.maxLength = 1000;
    description.value = club.description || '';
    const advisor = text(club.advisor_name, 80);
    const meeting = text(club.meeting_time, 120);
    const location = text(club.location, 120);
    const grades = text((club.allowed_grades || []).join('，'), 200);
    grades.placeholder = '留空表示所有年级，例如：高一，高二';
    const classes = text((club.allowed_classes || []).join('，'), 400);
    classes.placeholder = '留空表示所有班级';
    const enabled = el('input');
    enabled.type = 'checkbox';
    enabled.checked = Boolean(club.enabled);
    const enabledLabel = el('label', 'check');
    enabledLabel.append(enabled, document.createTextNode('开放报名'));
    const error = el('p', 'error');
    const save = el('button', 'btn btn-primary btn-sm', '保存');
    save.type = 'submit';
    const cancel = el('button', 'btn btn-quiet btn-sm', '取消');
    cancel.type = 'button';
    const row1 = el('div', 'row2');
    row1.append(field('名称', name), field('人数上限', capacity));
    const row2 = el('div', 'row2');
    row2.append(field('指导老师', advisor), field('活动时间', meeting));
    const row3 = el('div', 'row2');
    row3.append(field('年级限制', grades), field('班级限制', classes));
    const buttons = el('div', 'actions');
    buttons.append(save, cancel);
    form.append(row1, field('简介', description), row2, field('地点', location), row3, enabledLabel, error, buttons);
    let requestId = App.requestId();
    form.addEventListener('input', () => { requestId = App.requestId(); });
    cancel.addEventListener('click', () => { editingClub = null; loadClubs(); });
    form.addEventListener('submit', async event => {
      event.preventDefault();
      const list = value => value.split(/[,，、\s]+/).map(item => item.trim()).filter(Boolean);
      const max = Number(capacity.value);
      if (!name.value.trim()) { error.textContent = '请填写名称'; return; }
      if (!Number.isInteger(max) || max < Math.max(1, club.current_students) || max > 10000) {
        error.textContent = '人数上限需要在 ' + Math.max(1, club.current_students) + '–10000 之间';
        return;
      }
      App.busy(save, '保存中');
      error.textContent = '';
      const payload = {
        club_id: club.id, request_id: requestId, reason: '管理员编辑社团信息',
        expected_revision: club.revision, name: name.value.trim(), max_students: max,
        description: description.value.trim(), advisor_name: advisor.value.trim(),
        meeting_time: meeting.value.trim(), location: location.value.trim(),
        allowed_grades: list(grades.value), allowed_classes: list(classes.value), enabled: enabled.checked
      };
      const result = await App.request('/api/update_club', { method: 'POST', body: payload });
      if (!ok(result)) {
        App.busy(save, null);
        error.textContent = message(result, '保存失败');
        return;
      }
      editingClub = null;
      App.toast('已保存', 'ok');
      await loadClubs();
      loadOverview();
    });
    return form;
  }

  $('#imp-club-go').addEventListener('click', async () => {
    const button = $('#imp-club-go');
    const rows = $('#imp-club').value.split(/\n/).map(line => line.trim()).filter(Boolean);
    const clubs = [];
    const bad = [];
    rows.forEach((line, index) => {
      const cells = line.split(/[,，\t]+/).map(value => value.trim());
      const max = Number(cells[1]);
      if (!cells[0] || !Number.isInteger(max) || max < 1) bad.push(index + 1);
      else clubs.push({ name: cells[0], max_students: max });
    });
    if (!clubs.length || bad.length) { App.toast(bad.length ? '请检查第 ' + bad.join('、') + ' 行' : '没有可添加的社团', 'err'); return; }
    App.busy(button, '正在添加');
    const result = await App.request('/api/import_clubs', { method: 'POST', body: { clubs } });
    App.busy(button, null);
    const added = Number(result.data && result.data.success) || 0;
    const failed = Number(result.data && result.data.failed) || 0;
    $('#imp-club-out').textContent = result.data ? '已添加 ' + added + ' 个社团' + (failed ? '，' + failed + ' 行名称重复或人数无效' : '') : message(result, '添加失败');
    if (added) { $('#imp-club').value = ''; loadClubs(); loadOverview(); }
  });

  // ---- 调剂 ----
  async function loadAdjust() {
    const version = ++versions.adjust;
    const result = await App.request('/api/admin/adjustment');
    if (version !== versions.adjust) return;
    const box = $('#adjust-body');
    box.replaceChildren();
    const plan = result.data;
    if (!result.ok || !plan) { box.append(el('p', 'empty', '读取失败，请刷新页面')); return; }
    const metrics = el('div', 'metrics metrics-3');
    [['未报名', plan.unregistered + ' 人'], ['空余名额', plan.seats + ' 个'], ['可安排', plan.assignable + ' 人']].forEach(([label, value]) => {
      const item = el('div', 'metric');
      item.append(el('small', '', label), el('b', 'num', value));
      metrics.append(item);
    });
    box.append(metrics);
    if (plan.clubs.length) {
      const table = el('table', 'tbl');
      const head = el('tr');
      ['社团', '空余名额', '将安排'].forEach(text => head.append(el('th', '', text)));
      const thead = el('thead');
      thead.append(head);
      const body = el('tbody');
      plan.clubs.forEach(club => {
        const row = el('tr');
        row.append(el('td', '', splitRoom(club.name).name), el('td', 'num', String(club.remaining)), el('td', 'num', String(club.assign)));
        body.append(row);
      });
      table.append(thead, body);
      const wrap = el('div', 'table-wrap');
      wrap.append(table);
      box.append(wrap);
    }
    const actions = el('div', 'actions');
    if (!plan.unregistered) {
      box.append(el('p', 'hint', '全部学生均已安排社团。'));
    } else if (!plan.locked) {
      box.append(el('p', 'hint', '名单锁定后可以调剂。'));
      const go = el('button', 'btn btn-secondary btn-sm', '前往锁定');
      go.type = 'button';
      go.addEventListener('click', () => activate('settings', 'settings-registration'));
      actions.append(go);
    } else {
      if (plan.assignable < plan.unregistered) {
        box.append(el('p', 'warn-note', (plan.unregistered - plan.assignable) + ' 人无符合条件的空余名额，需先提高社团人数上限或调整报名范围。'));
      }
      if (plan.assignable) {
        const run = el('button', 'btn btn-primary btn-sm', '调剂 ' + plan.assignable + ' 人');
        run.type = 'button';
        const requestId = App.requestId();
        run.addEventListener('click', () => confirmInline(run, async () => {
          const result = await adminRetry('/api/admin/adjust_registrations', { request_id: requestId });
          if (!ok(result)) throw new Error(message(result, '调剂失败'));
          App.toast('已调剂 ' + result.data.assigned + ' 人', 'ok');
          await Promise.all([loadAdjust(), loadOverview()]);
        }));
        actions.append(run);
      }
    }
    const download = el('a', 'btn btn-secondary btn-sm', '下载报名表');
    download.href = '/api/export_workbook';
    actions.append(download);
    box.append(actions);
  }

  // ---- 清空报名 ----
  async function loadClear() {
    const version = ++versions.clear;
    const [time, summary] = await Promise.all([App.request('/api/check_registration_time'), App.request('/api/admin/summary')]);
    if (version !== versions.clear) return;
    const box = $('#clear-body');
    box.replaceChildren();
    if (!time.data || !summary.data) { box.append(el('p', 'empty', '读取失败，请刷新页面')); return; }
    const count = summary.data.registered;
    box.append(el('p', 'hint', '当前报名 ' + count + ' 条。'));
    const actions = el('div', 'actions');
    if (!time.data.registration_locked) {
      box.append(el('p', 'hint', '名单锁定后可以清空。'));
      const go = el('button', 'btn btn-secondary btn-sm', '前往锁定');
      go.type = 'button';
      go.addEventListener('click', () => activate('settings', 'settings-registration'));
      actions.append(go);
    } else if (count) {
      const run = el('button', 'btn btn-danger btn-sm', '清空 ' + count + ' 条报名');
      run.type = 'button';
      const requestId = App.requestId();
      run.addEventListener('click', () => confirmInline(run, async () => {
        const result = await adminRetry('/api/admin/clear_registrations', { request_id: requestId });
        if (!ok(result)) throw new Error(message(result, '清空失败'));
        App.toast('已清空 ' + result.data.cleared + ' 条报名', 'ok');
        await Promise.all([loadClear(), loadOverview()]);
      }));
      actions.append(run);
    }
    box.append(actions);
  }

  // ---- 备份 ----
  function fileSize(bytes) {
    return bytes >= 1048576 ? (bytes / 1048576).toFixed(1) + ' MB' : Math.max(1, Math.round(bytes / 1024)) + ' KB';
  }

  async function loadBackups() {
    const version = ++versions.backups;
    const result = await App.request('/api/admin/backups');
    if (version !== versions.backups) return;
    const table = $('#backup-tbl');
    if (!result.data || !Array.isArray(result.data.backups)) {
      table.replaceChildren(emptyBody(message(result, '读取失败')));
      return;
    }
    const head = el('thead');
    const headRow = el('tr');
    ['自动快照', '时间', '大小', ''].forEach(label => headRow.append(el('th', '', label)));
    head.append(headRow);
    if (!result.data.backups.length) {
      table.replaceChildren(head, emptyBody('暂无自动快照', 4));
    } else {
      const body = el('tbody');
      result.data.backups.forEach(item => {
        const row = el('tr');
        const cell = el('td', 'end');
        const link = el('a', 'btn btn-quiet btn-sm', '下载');
        link.href = '/api/admin/backup?name=' + encodeURIComponent(item.name);
        cell.append(link);
        row.append(el('td', '', item.label), el('td', 'num', item.time), el('td', 'num', fileSize(item.size)), cell);
        body.append(row);
      });
      table.replaceChildren(head, body);
    }
    $('#backup-note').textContent = '保留最近 ' + result.data.keep + ' 份，目录 ' + result.data.directory +
      '。恢复：在服务器上执行 bash run.sh restore 备份文件，服务先停止，恢复后重新启动。';
  }

  // ---- 设置 ----
  async function loadSettings() {
    const result = await App.request('/api/check_registration_time');
    if (!result.data) return;
    const field = text => (text ? text.slice(0, 16).replace(' ', 'T') : '');
    $('#open-at').value = field(result.data.start_time);
    $('#close-at').value = field(result.data.end_time);
    $('#clear-time').hidden = !result.data.start_time;
    locked = Boolean(result.data.registration_locked);
    $('#lock-state').textContent = locked ? '名单已锁定，报名与退选已停止。未报名的学生在“报名数据 → 调剂”中安排。'
      : result.data.end_time ? '名单未锁定。到截止时间 ' + result.data.end_time + ' 自动锁定。' : '名单未锁定。';
    $('#toggle-lock').textContent = locked ? '解除锁定' : '锁定名单';
  }

  $('#save-time').addEventListener('click', async () => {
    const value = $('#open-at').value;
    const close = $('#close-at').value;
    if (!value) { App.toast('请选择开放时间', 'err'); return; }
    const text = input => (input ? input.replace('T', ' ') + ':00' : '');
    const result = await App.request('/api/update_registration_time', { method: 'POST', body: { start_time: text(value), end_time: text(close) } });
    App.toast(ok(result) ? '时间已保存' : message(result, '保存失败'), ok(result) ? 'ok' : 'err');
    loadSettings();
    loadOverview();
  });

  $('#clear-time').addEventListener('click', () => confirmInline($('#clear-time'), async () => {
    const result = await adminRetry('/api/update_registration_time', { start_time: '' });
    if (!ok(result)) throw new Error(message(result, '清除失败'));
    App.toast('开放时间已清除', 'ok');
    await loadSettings();
    loadOverview();
  }));

  $('#toggle-lock').addEventListener('click', async () => {
    const reason = $('#lock-reason').value.trim();
    if (!reason) { App.toast('请填写原因', 'err'); return; }
    const button = $('#toggle-lock');
    App.busy(button, '保存中');
    const result = await App.request('/api/update_registration_lock', { method: 'POST', body: { locked: !locked, reason, request_id: App.requestId() } });
    App.busy(button, null);
    App.toast(ok(result) ? (locked ? '已解除锁定' : result.data.backup ? '名单已锁定，已自动备份' : '名单已锁定') : message(result, '保存失败'), ok(result) ? 'ok' : 'err');
    if (ok(result)) $('#lock-reason').value = '';
    await loadSettings();
    loadOverview();
  });

  $('#save-apw').addEventListener('click', async () => {
    const current = $('#ap-cur').value;
    const next = $('#ap-new').value;
    if (!current || next.length < 8) { App.toast('请填写当前密码，新密码至少 8 位', 'err'); return; }
    const result = await App.request('/api/admin_change_password', { method: 'POST', body: { current, new: next } });
    App.toast(ok(result) ? '密码已修改，请重新登录' : message(result, '修改失败'), ok(result) ? 'ok' : 'err');
    if (ok(result)) setTimeout(() => { location.href = '/'; }, 900);
  });

  $('#del-all-stu').addEventListener('click', () => confirmInline($('#del-all-stu'), async () => {
    const result = await App.request('/api/delete_all_students', { method: 'POST', body: { confirm: true } });
    if (!ok(result)) throw new Error(message(result, '清空失败'));
    App.toast('已清空全部学生', 'ok');
    await Promise.all([loadStudents(), loadOverview()]);
  }));

  $('#imp-stu').addEventListener('input', () => updateImportCheck());
  $('#imp-stu-go').addEventListener('click', importStudents);

  renderCredentials();
  updateImportCheck();
  const route = readRoute();
  activate(route.view, route.pane, { history: false });
  loadStudents();
  loadClubs();
})();
