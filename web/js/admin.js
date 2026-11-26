// 管理控制台：侧栏导航、名单导入与核对、账号表、社团、报名、设置。只用 ES5。
(function () {
  'use strict';
  var IMPORT_BATCH = 200;
  var ACCOUNT_FILE_ROWS = 5000;
  var REVIEW_PAGE = 300;
  var LIST_PAGE = 200;
  var PREVIEW_ROWS = 100;
  var MAX_UPLOAD = 10 * 1024 * 1024;
  var MAX_RESTORE = 64 * 1024 * 1024;

  var views = {
    home: { title: '工作台', panes: [] },
    students: { title: '学生', panes: [['list', '名单'], ['import', '导入'], ['accounts', '账号']] },
    clubs: { title: '社团', panes: [['cards', '卡片'], ['add', '添加']] },
    registration: { title: '报名', panes: [['overview', '概览'], ['adjust', '调剂'], ['export', '导出'], ['audit', '记录']] },
    settings: { title: '设置', panes: [['time', '时间'], ['backup', '备份'], ['data', '数据'], ['password', '密码']] }
  };
  var route = { view: '', pane: '' };
  var lastPane = {};
  var store = {
    students: [],
    studentIndex: {},
    studentsReady: false,
    clubs: [],
    clubIndex: {},
    time: null,
    summary: null,
    credentials: [],
    credentialsNote: ''
  };
  var listPage = 0;
  var audit = { role: 'admin', cursor: null };
  var versions = {};
  var signatures = {};
  var refreshTimer = null;
  var searchTimer = null;
  var importing = false;
  var classResetId = App.requestId();

  var el = App.el;
  var add = App.add;
  var clear = App.clear;

  // ---- 工具 ----
  function has(object, key) { return Object.prototype.hasOwnProperty.call(object, key); }
  function ok(result) { return Boolean(result.ok && result.data && result.data.success !== false); }
  function message(result, fallback) {
    if (result.data && result.data.message) return result.data.message;
    return result.status === 0 ? '请检查网络连接' : fallback;
  }
  function begin(name) {
    versions[name] = (versions[name] || 0) + 1;
    return versions[name];
  }
  function latest(name, version) { return versions[name] === version; }
  function changed(name, value) {
    var text = JSON.stringify(value);
    if (signatures[name] === text) return false;
    signatures[name] = text;
    return true;
  }
  function trim(value) { return String(value === null || value === undefined ? '' : value).replace(/^\s+|\s+$/g, ''); }
  function isInteger(value) { return typeof value === 'number' && isFinite(value) && Math.floor(value) === value; }
  function wait(ms) { return new Promise(function (resolve) { setTimeout(resolve, ms); }); }
  function post(path, body, options) {
    return App.request(path, Object.assign({ method: 'POST', body: body }, options || {}));
  }
  // 服务器忙（503）或连接重置时按 Retry-After 重试；同一 request_id 的请求只执行一次。
  function retry(path, body) {
    var attempts = 0;
    function run() {
      return post(path, body).then(function (result) {
        attempts += 1;
        if ((result.status !== 503 && result.status !== 0) || attempts >= 4) return result;
        return wait(result.retryAfter * 1000).then(run);
      });
    }
    return run();
  }
  function sequence(steps) {
    var index = 0;
    function run() {
      if (index >= steps.length) return Promise.resolve(null);
      var step = steps[index];
      index += 1;
      return step().then(function (failure) { return failure || run(); });
    }
    return run();
  }
  function targetOf(event, selector) {
    var node = event.target;
    if (node && node.nodeType !== 1) node = node.parentNode;
    return node && node.closest ? node.closest(selector) : null;
  }
  function teacher(name) {
    if (!name) return '';
    return /老师$/.test(name) ? name : name + ' 老师';
  }
  function splitRoom(name) {
    var match = /^(.*?)（\s*([0-9A-Za-z]+)\s*）\s*$/.exec(name || '');
    return match ? { name: trim(match[1]), room: match[2] + ' 室' } : { name: name || '', room: null };
  }
  function splitList(value) {
    return String(value || '').split(/[,，、\s]+/).map(trim).filter(Boolean);
  }
  var NUMERALS = '一二三四五六七八九十';
  function sortKey(text) {
    return String(text).replace(/[一二三四五六七八九十]/g, function (char) { return String(NUMERALS.indexOf(char) + 1); });
  }
  function naturalCompare(a, b) {
    var left = sortKey(a).match(/\d+|\D+/g) || [];
    var right = sortKey(b).match(/\d+|\D+/g) || [];
    for (var i = 0; i < Math.min(left.length, right.length); i += 1) {
      if (left[i] === right[i]) continue;
      if (/^\d/.test(left[i]) && /^\d/.test(right[i])) return Number(left[i]) - Number(right[i]);
      return left[i] < right[i] ? -1 : 1;
    }
    return left.length - right.length;
  }
  function stampText(value) {
    if (typeof value !== 'number') return value ? App.schoolTime(String(value)) : '';
    var date = new Date(value);
    return (date.getMonth() + 1) + ' 月 ' + date.getDate() + ' 日 ' + App.pad(date.getHours()) + ':' + App.pad(date.getMinutes());
  }
  function fileSize(bytes) {
    return bytes >= 1048576 ? (bytes / 1048576).toFixed(1) + ' MB' : Math.max(1, Math.round(bytes / 1024)) + ' KB';
  }

  // ---- 报名方式 ----
  // 同时报名：全体学生一个时段、共用名额。分批报名：按班级分批，各批一个时段，社团名额按批计算。
  // 报名方式、各批时段和人数都来自 /api/admin/summary。
  var BATCH_DONE = 1000000;
  function batchMode() { return Boolean(store.summary && store.summary.batch_mode); }
  function batchList() { return (store.summary && store.summary.batches) || []; }
  function currentBatch() {
    var id = store.summary ? store.summary.current_batch : null;
    var found = null;
    batchList().forEach(function (batch) { if (batch.id === id) found = batch; });
    return found;
  }
  function capacityLabel() { return batchMode() ? '每批名额' : '人数上限'; }
  function batchWindow(batch) {
    var start = App.schoolTime(batch.start_time);
    var end = App.schoolTime(batch.end_time);
    var sameDay = String(batch.start_time).slice(0, 10) === String(batch.end_time).slice(0, 10);
    return start + '–' + (sameDay ? end.split(' ').pop() : end);
  }
  function batchStateText(batch) {
    return { open: '报名中', closed: '已截止', waiting: '待开放' }[batch.state] || '';
  }
  // 各批次里这个社团的报名人数的最大值：每批名额不能低于它。
  function peakUsed(club) {
    if (!batchMode()) return club.current_students;
    var peak = Number((store.summary.unassigned_clubs || {})[club.id]) || 0;
    batchList().forEach(function (batch) { peak = Math.max(peak, Number((batch.clubs || {})[club.id]) || 0); });
    return peak;
  }
  // 社团的全部报名人数：分批报名时为各批合计。
  function totalUsed(club) {
    if (!batchMode()) return club.current_students;
    var total = Number((store.summary.unassigned_clubs || {})[club.id]) || 0;
    batchList().forEach(function (batch) { total += Number((batch.clubs || {})[club.id]) || 0; });
    return total;
  }
  // 当前批次的即时操作：待开放时“现在开放”，报名中时“现在截止”。
  function batchNow() {
    var batch = batchMode() ? currentBatch() : null;
    if (!batch || store.time && store.time.registration_locked) return null;
    if (batch.state === 'waiting') return { batch: batch, action: 'open', label: '现在开放' + batch.name };
    if (batch.state === 'open') return { batch: batch, action: 'close', label: '现在截止' + batch.name };
    return null;
  }
  function runBatchNow(now, button) {
    var batches = batchList();
    var next = batches[batches.indexOf(now.batch) + 1];
    var options = now.action === 'open'
      ? { title: now.label + '？', message: now.batch.classes.join('、') + '的学生现在可以报名', confirm: '开放' }
      : { title: now.label + '？', message: '本批报名与退选在此刻结束，' + (next ? '随后切换到' + next.name : '名单随即锁定'), confirm: '截止' };
    App.confirm(options).then(function (yes) {
      if (!yes) return;
      App.busy(button, '处理中');
      retry('/api/admin/batch_now', { batch_id: now.batch.id, action: now.action, request_id: App.requestId() }).then(function (result) {
        App.busy(button, null);
        if (!ok(result)) {
          App.toast(message(result, '请重试'), 'err');
          return;
        }
        App.toast(now.action === 'open' ? now.batch.name + '已开放' : now.batch.name + '已截止', 'ok');
        if (route.view === 'settings') loadSettings();
        else refreshOverview();
      });
    });
  }
  function applyModeLabels() {
    $('#club-add-max-label').textContent = capacityLabel();
  }
  function loadSummary() {
    return App.request('/api/admin/summary').then(function (result) {
      if (result.ok && result.data) {
        store.summary = result.data;
        applyModeLabels();
      }
      return store.summary;
    });
  }

  // ---- 界面元件 ----
  function meter(fraction, low) {
    var bar = el('div', 'meter' + (low ? ' low' : ''));
    var fill = el('i');
    fill.style.width = Math.max(0, Math.min(100, Math.round(fraction * 100))) + '%';
    bar.setAttribute('aria-hidden', 'true');
    bar.appendChild(fill);
    return bar;
  }
  function head(table, labels) {
    var row = el('tr');
    labels.forEach(function (label) { row.appendChild(el('th', '', label)); });
    table.appendChild(add(el('thead'), row));
  }
  function emptyRow(text, span) {
    var cell = el('td', 'empty', text);
    cell.colSpan = span || 1;
    return add(el('tr'), cell);
  }
  function tableMessage(table, text) {
    clear(table);
    table.appendChild(add(el('tbody'), emptyRow(text)));
  }
  function field(label, control) {
    return add(el('label', 'field'), el('span', '', label), control);
  }
  function textInput(value, max) {
    var input = el('input', 'input');
    input.value = value || '';
    input.maxLength = max;
    input.setAttribute('autocomplete', 'off');
    return input;
  }
  function checkbox(checked) {
    var input = el('input');
    input.type = 'checkbox';
    input.checked = Boolean(checked);
    return input;
  }
  function fileInput(accept) {
    var input = el('input', 'sr-only');
    input.type = 'file';
    input.accept = accept;
    input.tabIndex = -1;
    input.setAttribute('aria-hidden', 'true');
    return input;
  }
  // 读出所选文件后清空输入框，同一文件再次选择时仍会触发 change。
  function takeFile(input) {
    var file = (input.files && input.files[0]) || null;
    try { input.value = ''; } catch (error) { return file; }
    return file;
  }
  function renderPager(box, page, pages, onChange) {
    clear(box);
    box.hidden = pages <= 1;
    if (pages <= 1) return;
    var previous = App.button('上一页', 'btn btn-plain btn-small', function () { onChange(page - 1); });
    var following = App.button('下一页', 'btn btn-plain btn-small', function () { onChange(page + 1); });
    previous.disabled = page <= 0;
    following.disabled = page >= pages - 1;
    add(box, previous, el('span', 'num', (page + 1) + ' / ' + pages), following);
  }
  function rememberClubs(list) {
    if (!Array.isArray(list)) return;
    store.clubs = list;
    store.clubIndex = {};
    list.forEach(function (club) { store.clubIndex[club.id] = club; });
  }
  function clubName(id) {
    var club = store.clubIndex[id];
    return club ? splitRoom(club.name).name : (id ? '社团 ' + id : '');
  }
  function setStudents(list) {
    store.students = list;
    store.studentIndex = {};
    list.forEach(function (student) { store.studentIndex[student.id] = student; });
    store.studentsReady = true;
  }
  function studentLabel(id) {
    var student = store.studentIndex[id];
    return student ? student.name + '（' + student['class'] + '）' : '学生 ' + id;
  }

  // ---- 导航 ----
  function readRoute() {
    var parts = location.hash.replace(/^#/, '').split('/');
    var view = has(views, parts[0]) ? parts[0] : 'home';
    var panes = views[view].panes;
    var pane = panes.length ? panes[0][0] : '';
    panes.forEach(function (item) { if (item[0] === parts[1]) pane = item[0]; });
    return { view: view, pane: pane };
  }

  function go(view, pane) {
    var hash = '#' + view + (pane ? '/' + pane : '');
    if (location.hash === hash) show(readRoute(), true);
    else location.hash = hash;
  }

  function renderTabs() {
    var box = $('#tabs');
    var panes = views[route.view].panes;
    clear(box);
    box.hidden = !panes.length;
    panes.forEach(function (item) {
      var tab = App.button(item[1], '', function () { go(route.view, item[0]); });
      tab.setAttribute('role', 'tab');
      tab.setAttribute('aria-selected', String(item[0] === route.pane));
      box.appendChild(tab);
    });
  }

  function show(next, focus) {
    route = next;
    if (next.pane) lastPane[next.view] = next.pane;
    $$('#nav button').forEach(function (button) {
      if (button.getAttribute('data-view') === next.view) button.setAttribute('aria-current', 'page');
      else button.removeAttribute('aria-current');
    });
    var title = views[next.view].title;
    $('#view-title').textContent = title;
    document.title = title + ' · 管理控制台';
    renderTabs();
    $$('section[data-view]').forEach(function (section) {
      var active = section.getAttribute('data-view') === next.view;
      section.hidden = !active;
      if (!active) return;
      $$('[data-pane]', section).forEach(function (node) { node.hidden = node.getAttribute('data-pane') !== next.pane; });
    });
    load();
    if (focus) {
      try { $('#view-title').focus(); } catch (error) { focus = false; }
    }
  }

  function watching() {
    return route.view === 'home' || (route.view === 'registration' && route.pane === 'overview');
  }

  function load() {
    var key = route.view + '/' + route.pane;
    clearTimeout(refreshTimer);
    if (watching()) refreshOverview();
    if (route.view === 'home') loadAccess();
    if (route.view === 'students') loadStudents();
    if (key === 'students/accounts') renderCredentials();
    if (key === 'clubs/cards') loadClubs();
    if (key === 'clubs/add') loadSummary();
    if (key === 'registration/adjust') loadAdjust();
    if (key === 'registration/audit') loadAudit(true);
    if (key === 'settings/time' || key === 'settings/data') loadSettings();
    if (key === 'settings/backup') loadBackups();
  }

  window.addEventListener('hashchange', function () { show(readRoute(), true); });
  $('#nav').addEventListener('click', function (event) {
    var button = targetOf(event, 'button[data-view]');
    if (!button) return;
    var view = button.getAttribute('data-view');
    go(view, lastPane[view] || '');
  });
  document.addEventListener('click', function (event) {
    var target = targetOf(event, '[data-go]');
    if (!target) return;
    var parts = target.getAttribute('data-go').split('/');
    go(parts[0], parts[1]);
  });

  // ---- 工作台与概览 ----
  function refreshOverview() {
    clearTimeout(refreshTimer);
    return loadOverview().then(function () {
      clearTimeout(refreshTimer);
      if (watching() && !App.hidden()) refreshTimer = setTimeout(refreshOverview, 6000);
    });
  }

  function loadOverview() {
    var version = begin('overview');
    var sent = Date.now();
    return Promise.all([
      App.request('/api/get_clubs'), App.request('/api/admin/summary'), App.request('/api/check_registration_time')
    ]).then(function (results) {
      var clubs = results[0].data;
      var summary = results[1].data;
      var time = results[2].data;
      if (!latest('overview', version) || !Array.isArray(clubs) || !summary || !time) return;
      rememberClubs(clubs);
      store.summary = summary;
      store.time = time;
      applyModeLabels();
      renderClockSkew(time.server_now, sent, Date.now());
      renderPhase(time);
      renderFlow(summary, time);
      // 分批报名时进度与人数按当前批次显示，下方另列各批次。
      var batch = currentBatch();
      var rows = fillRows(clubs);
      renderFill('home-fill', rows, fillCaption());
      renderFill('ov-fill', rows, fillCaption());
      renderBatchTable(summary);
      var total = batch ? batch.students : summary.students;
      var registered = batch ? batch.registered : summary.registered;
      $('#m-students-label').textContent = batch ? batch.name + '学生' : '学生';
      $('#m-students').textContent = String(total);
      $('#m-reg').textContent = String(registered);
      $('#m-unreg').textContent = String(total - registered);
      $('#m-rate').textContent = total ? Math.round(registered / total * 100) + '%' : '—';
      $('#m-clubs').textContent = String(clubs.length);
    });
  }

  // 概览里的各批次：时段、班级、报名进度和状态；同时报名时不显示。
  function renderBatchTable(summary) {
    var box = $('#ov-batches');
    var rows = summary.batch_mode ? summary.batches.map(function (batch) {
      return [batch.name, batchWindow(batch), batch.classes.join('、'), batch.registered + ' / ' + batch.students,
        batchStateText(batch)];
    }) : [];
    if (summary.batch_mode && summary.unassigned_students) {
      rows.push(['待分批', '', summary.unassigned.map(function (item) { return item['class']; }).join('、'),
        summary.unassigned_students + ' 人', '']);
    }
    if (!changed('ov-batches', rows)) return;
    clear(box);
    if (!rows.length) return;
    var table = el('table', 'tbl batch-table');
    head(table, ['批次', '时段', '班级', '已报名', '状态']);
    var body = el('tbody');
    rows.forEach(function (row) {
      add(body, add(el('tr'), el('td', 'nowrap', row[0]), el('td', 'nowrap', row[1]), el('td', 'wrap', row[2]),
        el('td', 'num', row[3]), el('td', '', row[4])));
    });
    table.appendChild(body);
    add(box, el('h3', 'gap', '各批次'), add(el('div', 'table-wrap'), table));
  }

  function scheduleText(time) {
    var parts = [];
    if (time.start_time) parts.push(App.schoolTime(time.start_time) + ' 开放');
    if (time.end_time) parts.push(App.schoolTime(time.end_time) + ' 截止');
    return parts.join(' · ');
  }

  function renderPhase(time) {
    var title = '开放时间待定';
    var detail = scheduleText(time);
    var batch = currentBatch();
    if (time.registration_locked) title = '名单已锁定';
    else if (batchMode()) {
      if (store.summary.current_batch >= BATCH_DONE) title = '全部批次已截止';
      else if (batch) title = batch.name + (batch.state === 'open' ? '报名中' : '待开放');
    } else if (time.can_register) title = '报名中';
    else if (time.start_time) title = '待开放';
    if (batchMode()) detail = batch && !time.registration_locked ? batchWindow(batch) : '';
    var now = batchNow();
    var quick = $('#home-batch-now');
    quick.hidden = !now;
    if (now && quick.textContent !== now.label) quick.textContent = now.label;
    $('#home-phase').textContent = title;
    $('#home-detail').textContent = detail;
    $('#home-detail').hidden = !detail;
    $('#ov-time').textContent = title + (detail ? ' · ' + detail : '');
  }

  // 各社团的报名进度。分批报名时显示当前批次；没有当前批次时（全部截止）显示各批合计。
  function fillRows(clubs) {
    var batches = batchList();
    var total = batchMode() && !currentBatch() && batches.length;
    return clubs.map(function (club) {
      if (!total) return [club.name, club.current_students, club.max_students];
      var used = 0;
      batches.forEach(function (batch) { used += Number((batch.clubs || {})[club.id]) || 0; });
      return [club.name, used, club.max_students * batches.length];
    });
  }

  function fillCaption() {
    var batch = currentBatch();
    if (batch) return batch.name + ' · ' + batchStateText(batch);
    return batchMode() && batchList().length ? '各批合计' : '';
  }

  function renderFill(id, rows, caption) {
    if (!changed(id, [caption || '', rows])) return;
    var box = $('#' + id);
    clear(box);
    if (!rows.length) {
      box.appendChild(el('p', 'hint', '添加社团后显示在这里'));
      return;
    }
    if (caption) box.appendChild(el('p', 'hint', caption));
    rows.forEach(function (row) {
      var info = splitRoom(row[0]);
      add(box, add(el('div', 'fill-row'),
        el('span', '', info.name + (info.room ? ' · ' + info.room : '')),
        el('span', 'num', row[1] + ' / ' + row[2]),
        meter(row[2] ? row[1] / row[2] : 0, row[2] - row[1] <= 3)));
    });
  }

  // 流程：按使用顺序列出各步骤的状态和入口，第一个待办步骤用主按钮。
  function renderFlow(summary, time) {
    var students = summary.students;
    var registered = summary.registered;
    var unregistered = students - registered;
    var short = students - summary.capacity;
    var locked = Boolean(time.registration_locked);
    function fresh(at) { return Boolean(at) && (!summary.last_change || at >= summary.last_change); }
    var exported = !summary.last_export ? '报名表待导出'
      : fresh(summary.last_export) ? '报名表已导出 ' + stampText(summary.last_export) : '报名表导出后有变动';
    var backedUp = !summary.last_backup ? '备份待下载'
      : fresh(summary.last_backup) ? '备份已下载 ' + stampText(summary.last_backup) : '备份后有变动';
    var tally = '已报名 ' + registered + ' / ' + students;
    var steps = [
      { title: '导入学生', done: students > 0, text: students ? students + ' 名学生' : '上传含姓名、班级的表格',
        routes: students ? [['名单', 'students/list'], ['导入', 'students/import']] : [['导入', 'students/import']] },
      { title: '添加社团', done: summary.clubs > 0, warn: summary.clubs > 0 && short > 0,
        text: summary.clubs ? summary.clubs + ' 个社团 · 名额 ' + summary.capacity + (short > 0 ? ' / 学生 ' + students : '') : '名称与人数上限',
        routes: [['社团', 'clubs/cards']] },
      { title: '开放时间', done: Boolean(time.start_time), text: time.start_time ? scheduleText(time) : '设置开放与截止时间',
        routes: [['时间', 'settings/time']] },
      { title: '报名', done: locked,
        text: locked ? '已截止 · ' + tally : time.can_register ? '报名中 · ' + tally : '到开放时间后开始',
        routes: [['概览', 'registration/overview']] },
      { title: '调剂', done: locked && unregistered === 0,
        text: !locked ? '锁定名单后安排未报名学生' : unregistered ? '未报名 ' + unregistered + ' 名' : '全部学生已有社团',
        routes: [['调剂', 'registration/adjust']] },
      { title: '导出与备份', done: locked && fresh(summary.last_export) && fresh(summary.last_backup),
        text: exported + ' · ' + backedUp,
        routes: [['导出', 'registration/export'], ['备份', 'settings/backup']] }
    ];
    if (summary.batch_mode) {
      // 分批报名：每批名额合计与人数最多的一批比较；时间按批次设置；报名进度看当前批次。
      var batches = summary.batches || [];
      var batch = currentBatch();
      var shortBatch = summary.largest_batch - summary.capacity;
      steps[1] = { title: '添加社团', done: summary.clubs > 0, warn: summary.clubs > 0 && shortBatch > 0,
        text: summary.clubs ? summary.clubs + ' 个社团 · 每批名额 ' + summary.capacity +
          (shortBatch > 0 ? ' / 人数最多的一批 ' + summary.largest_batch : '') : '名称与每批名额',
        routes: [['社团', 'clubs/cards']] };
      steps[2] = { title: '分批与时间', done: batches.length > 0, warn: batches.length > 0 && summary.unassigned_students > 0,
        text: !batches.length ? '按班级分批，设置各批时段'
          : summary.unassigned_students ? batches.length + ' 批 · ' + summary.unassigned.length + ' 个班级待分批'
            : batches.length + ' 批 · ' + App.schoolTime(batches[0].start_time) + ' 起',
        routes: [['时间', 'settings/time']] };
      steps[3] = { title: '报名', done: locked,
        text: locked ? '已截止 · ' + tally
          : batch ? batch.name + (batch.state === 'open' ? '报名中 · 本批已报名 ' + batch.registered + ' / ' + batch.students
            : ' ' + App.schoolTime(batch.start_time) + ' 开放')
            : summary.current_batch >= BATCH_DONE ? '全部批次已截止 · ' + tally : '设置批次后开始',
        routes: [['概览', 'registration/overview']] };
    }
    if (!changed('flow', steps)) return;
    var current = -1;
    for (var i = 0; i < steps.length; i += 1) {
      if (!steps[i].done) {
        current = i;
        break;
      }
    }
    var list = $('#home-flow');
    clear(list);
    steps.forEach(function (step, index) {
      var state = step.done ? (step.warn ? 'warn' : 'done') : index === current ? 'current' : 'todo';
      var item = el('li', state);
      if (state === 'current') item.setAttribute('aria-current', 'step');
      var mark = el('span', 'mark', state === 'done' ? '✓' : state === 'warn' ? '!' : String(index + 1));
      mark.setAttribute('aria-hidden', 'true');
      var actions = el('div', 'flow-actions');
      step.routes.forEach(function (target, position) {
        var kind = index === current && position === 0 ? 'btn btn-primary btn-small' : 'btn btn-plain btn-small';
        var button = App.button(target[0], kind);
        button.setAttribute('data-go', target[1]);
        actions.appendChild(button);
      });
      add(list, add(item, mark, add(el('div', 'flow-text'), el('b', '', step.title), el('small', '', step.text)), actions));
    });
  }

  // 报名按服务器时钟开放；本机与服务器相差超过一分钟时显示差值。
  function renderClockSkew(serverNow, sent, received) {
    var note = $('#home-clock');
    if (typeof serverNow !== 'number') return;
    var skew = serverNow - (sent + received) / 2;
    if (Math.abs(skew) < 60000) {
      note.hidden = true;
      return;
    }
    var seconds = Math.round(Math.abs(skew) / 1000);
    var hours = Math.floor(seconds / 3600);
    note.textContent = '服务器时间比本机' + (skew < 0 ? '慢 ' : '快 ') + (hours ? hours + ' 小时 ' : '') +
      Math.floor(seconds % 3600 / 60) + ' 分 ' + seconds % 60 + ' 秒，开放以服务器时间为准';
    note.hidden = false;
  }

  $('#home-batch-now').addEventListener('click', function () {
    var now = batchNow();
    if (now) runBatchNow(now, $('#home-batch-now'));
  });

  App.onVisible(function () {
    if (App.hidden()) clearTimeout(refreshTimer);
    else if (watching()) refreshOverview();
  });

  // ---- 学生访问地址：服务器列出各网卡地址，协议和端口沿用当前页面 ----
  var accessList = [];
  var FULLSCREEN_EVENTS = ['fullscreenchange', 'webkitfullscreenchange', 'MSFullscreenChange'];

  function hostIsAddress(host) {
    return host === 'localhost' || /^\d+\.\d+\.\d+\.\d+$/.test(host) || host.charAt(0) === '[';
  }

  function siteBase(host) {
    return location.protocol + '//' + host + (location.port ? ':' + location.port : '');
  }

  function loadAccess() {
    var version = begin('access');
    return App.request('/api/admin/addresses').then(function (result) {
      if (!latest('access', version)) return;
      var list = [];
      // 经域名（内网穿透、反向代理）打开控制台时，学生使用同一个域名。
      if (!hostIsAddress(location.hostname)) list.push({ url: location.protocol + '//' + location.host, label: '' });
      ((result.ok && result.data && result.data.addresses) || []).forEach(function (item) {
        list.push({ url: siteBase(item.address), label: item.label || '' });
      });
      accessList = list;
      $('#access-card').hidden = !list.length;
      if (!list.length) return;
      $('#access-url').textContent = list[0].url;
      var tag = $('#access-label');
      tag.textContent = list[0].label;
      tag.hidden = !(list.length > 1 && list[0].label);
      var more = $('#access-more');
      more.textContent = list.length > 1 ? '其他网卡：' + list.slice(1).map(function (item) {
        return item.url + (item.label ? '（' + item.label + '）' : '');
      }).join('，') : '';
      more.hidden = list.length < 2;
    });
  }

  function copyText(text) {
    if (window.clipboardData && window.clipboardData.setData) return Boolean(window.clipboardData.setData('Text', text));
    var area = el('textarea');
    area.value = text;
    area.setAttribute('readonly', '');
    area.style.position = 'fixed';
    area.style.top = '-1000px';
    document.body.appendChild(area);
    area.select();
    var copied = false;
    try { copied = document.execCommand('copy'); } catch (error) { copied = false; }
    document.body.removeChild(area);
    return copied;
  }

  function fullscreenElement() {
    return document.fullscreenElement || document.webkitFullscreenElement || document.msFullscreenElement || null;
  }

  // 放大显示：整屏大字，便于电子教室的屏幕广播；支持时进入浏览器全屏。
  function presentAccess() {
    if (!accessList.length) return;
    var overlay = el('div', 'present');
    var closed = false;
    overlay.setAttribute('role', 'dialog');
    overlay.setAttribute('aria-modal', 'true');
    overlay.setAttribute('aria-label', '学生访问地址');
    overlay.tabIndex = -1;
    add(overlay, el('p', 'present-label', '学生访问'), el('p', 'present-url mono', accessList[0].url));
    if (accessList.length > 1) {
      overlay.appendChild(el('p', 'present-more', accessList.slice(1).map(function (item) { return item.url; }).join('　')));
    }
    var batch = batchMode() ? currentBatch() : null;
    if (batch) overlay.appendChild(el('p', 'present-batch', batch.name + ' · ' + batchWindow(batch)));
    overlay.appendChild(el('p', 'present-hint', '点击或按 Esc 返回'));
    function onKey(event) {
      var key = event.key || event.keyCode;
      if (key === 'Escape' || key === 'Esc' || key === 27) close();
    }
    function onChange() {
      if (!fullscreenElement()) close();
    }
    function close() {
      if (closed) return;
      closed = true;
      document.removeEventListener('keydown', onKey);
      FULLSCREEN_EVENTS.forEach(function (name) { document.removeEventListener(name, onChange); });
      var exit = document.exitFullscreen || document.webkitExitFullscreen || document.msExitFullscreen;
      if (fullscreenElement() && exit) {
        try { exit.call(document); } catch (error) { exit = null; }
      }
      if (overlay.parentNode) overlay.parentNode.removeChild(overlay);
      $('#access-show').focus();
    }
    overlay.addEventListener('click', close);
    document.addEventListener('keydown', onKey);
    document.body.appendChild(overlay);
    overlay.focus();
    var request = overlay.requestFullscreen || overlay.webkitRequestFullscreen || overlay.msRequestFullscreen;
    if (!request) return;
    FULLSCREEN_EVENTS.forEach(function (name) { document.addEventListener(name, onChange); });
    try {
      var pending = request.call(overlay);
      if (pending && pending.then) pending.then(null, function () { return null; });
    } catch (error) {
      request = null;
    }
  }

  $('#access-copy').addEventListener('click', function () {
    if (!accessList.length) return;
    if (copyText(accessList[0].url)) App.toast('已拷贝', 'ok');
    else App.toast('可手动选中地址后拷贝', 'err');
  });
  $('#access-show').addEventListener('click', presentAccess);

  // ---- 学生名单 ----
  function loadStudents() {
    var version = begin('students');
    return Promise.all([App.request('/api/get_all_students'), App.request('/api/get_clubs'), loadSummary()]).then(function (results) {
      if (!latest('students', version)) return;
      rememberClubs(results[1].data);
      if (!Array.isArray(results[0].data)) {
        tableMessage($('#stu-tbl'), message(results[0], '请刷新页面'));
        return;
      }
      setStudents(results[0].data);
      renderStudents();
      fillClassSelect();
    });
  }

  // 分批报名时学生所在的批次，按班级查。
  function batchOf(klass) {
    var found = '';
    batchList().forEach(function (batch) { if (batch.classes.indexOf(klass) >= 0) found = batch.name; });
    return found || '待分批';
  }

  function matchingStudents() {
    var query = trim($('#stu-search').value).toLowerCase().replace(/\s+/g, '');
    if (!query) return store.students;
    return store.students.filter(function (student) {
      var values = [student.name, student['class'], student.username, student.club_id ? clubName(student.club_id) : '未报名'];
      if (batchMode()) values.push(batchOf(student['class']).replace(/\s+/g, ''));
      for (var i = 0; i < values.length; i += 1) {
        if (String(values[i] || '').toLowerCase().indexOf(query) >= 0) return true;
      }
      return false;
    });
  }

  function renderStudents() {
    var rows = matchingStudents();
    var pages = Math.max(1, Math.ceil(rows.length / LIST_PAGE));
    if (listPage >= pages) listPage = pages - 1;
    var registered = 0;
    store.students.forEach(function (student) { if (student.club_id) registered += 1; });
    $('#stu-count').textContent = (rows.length !== store.students.length ? '匹配 ' + rows.length + ' 名 · ' : '') +
      '共 ' + store.students.length + ' 名 · 已报名 ' + registered + ' 名';
    var table = $('#stu-tbl');
    clear(table);
    var batched = batchMode();
    $('#stu-search').placeholder = batched ? '搜索姓名、班级、批次、账号或社团' : '搜索姓名、班级、账号或社团';
    head(table, batched ? ['姓名', '班级', '批次', '账号', '社团', ''] : ['姓名', '班级', '账号', '社团', '']);
    var body = el('tbody');
    if (!rows.length) body.appendChild(emptyRow(store.students.length ? '试试其他关键词' : '导入名单后显示在这里', batched ? 6 : 5));
    rows.slice(listPage * LIST_PAGE, (listPage + 1) * LIST_PAGE).forEach(function (student) {
      var edit = App.button('编辑', 'btn btn-plain btn-small');
      edit.setAttribute('data-id', student.id);
      var row = add(el('tr'), el('td', '', student.name), el('td', '', student['class']));
      if (batched) row.appendChild(el('td', 'nowrap', batchOf(student['class'])));
      add(body, add(row,
        el('td', 'mono', student.username),
        el('td', student.club_id ? '' : 'muted', student.club_id ? clubName(student.club_id) : '未报名'),
        add(el('td', 'end'), edit)));
    });
    table.appendChild(body);
    renderPager($('#stu-pager'), listPage, pages, function (page) {
      listPage = page;
      renderStudents();
    });
  }

  function onSearch() {
    clearTimeout(searchTimer);
    searchTimer = setTimeout(function () {
      listPage = 0;
      renderStudents();
    }, 150);
  }
  ['input', 'change', 'keyup'].forEach(function (name) { $('#stu-search').addEventListener(name, onSearch); });
  $('#stu-tbl').addEventListener('click', function (event) {
    var button = targetOf(event, 'button[data-id]');
    var student = button ? store.studentIndex[button.getAttribute('data-id')] : null;
    if (student) openStudent(student);
  });

  function openStudent(student) {
    var ids = { info: App.requestId(), move: App.requestId(), reset: App.requestId() };
    var nameInput = textInput(student.name, 50);
    var classInput = textInput(student['class'], 50);
    var select = el('select', 'input');
    select.options.add(new Option('未报名', ''));
    store.clubs.forEach(function (club) {
      var mine = Number(club.id) === Number(student.club_id);
      var full = club.max_students - club.current_students <= 0;
      var option = new Option(splitRoom(club.name).name + ' · ' + club.current_students + ' / ' + club.max_students +
        (!mine && full ? ' · 已满' : ''), String(club.id));
      option.disabled = !mine && full;
      select.options.add(option);
    });
    select.value = student.club_id ? String(student.club_id) : '';
    var override = checkbox(false);
    var note = textInput('', 200);
    note.placeholder = '记入操作记录';
    var secret = el('div', 'secret-box');
    secret.hidden = true;
    var error = el('p', 'error');
    error.setAttribute('role', 'alert');
    var body = add(el('div'),
      add(el('div', 'form-row'), field('姓名', nameInput), field('班级', classInput)),
      field('社团', select),
      add(el('label', 'check'), override, '忽略报名范围和暂停状态'),
      field('备注（可选）', note),
      secret,
      error);
    var remove = App.button('删除', 'btn btn-destructive btn-small', confirmRemove);
    var reset = App.button('重置密码', 'btn btn-small', confirmReset);
    var sheet = App.sheet({
      narrow: true,
      title: student.name,
      message: '账号 ' + student.username,
      body: body,
      footNote: add(el('div', 'actions'), remove, reset),
      buttons: [
        { label: '取消', onClick: function (controller) { controller.close(); } },
        { label: '存储', kind: 'primary', onClick: save }
      ]
    });
    setTimeout(function () { nameInput.focus(); }, 0);

    [nameInput, classInput].forEach(function (input) {
      input.addEventListener('input', function () { ids.info = App.requestId(); });
    });
    [select, override, note].forEach(function (input) {
      input.addEventListener('change', function () { ids.move = App.requestId(); });
    });

    function save(controller) {
      var name = trim(nameInput.value);
      var klass = trim(classInput.value);
      var target = select.value ? Number(select.value) : null;
      var current = student.club_id ? Number(student.club_id) : null;
      var reason = trim(note.value);
      if (!name || !klass) {
        error.textContent = '请填写姓名和班级';
        return;
      }
      var steps = [];
      if (name !== student.name || klass !== student['class']) {
        steps.push(function () {
          return retry('/api/admin/update_student', { student_id: student.id, name: name, 'class': klass, request_id: ids.info })
            .then(function (result) {
              if (!ok(result)) return message(result, '请重试');
              ids.info = App.requestId();
              return null;
            });
        });
      }
      if (target !== current) {
        steps.push(function () {
          var path = !current ? '/api/admin/assign_registration' : !target ? '/api/admin/remove_registration' : '/api/admin/transfer_registration';
          var payload = { student_id: student.id, request_id: ids.move, reason: reason || '管理员调整' };
          if (target) payload.club_id = target;
          if (target && override.checked) payload.override_eligibility = true;
          return retry(path, payload).then(function (result) {
            if (!ok(result)) return message(result, '请重试');
            ids.move = App.requestId();
            return null;
          });
        });
      }
      if (!steps.length) {
        controller.close();
        return;
      }
      error.textContent = '';
      var submit = controller.buttons[1];
      App.busy(submit, '存储中');
      sequence(steps).then(function (failure) {
        App.busy(submit, null);
        if (failure) {
          error.textContent = failure;
          return;
        }
        controller.close();
        App.toast('已存储', 'ok');
        loadStudents();
      });
    }

    function confirmReset() {
      App.confirm({ title: '重置“' + student.name + '”的密码？', message: '新密码随即生效', confirm: '重置', destructive: true })
        .then(function (yes) {
          if (!yes) return;
          App.busy(reset, '重置中');
          retry('/api/admin/reset_student_password', {
            student_id: student.id, request_id: ids.reset, reason: trim(note.value) || '管理员重置'
          }).then(function (result) {
            App.busy(reset, null);
            if (!ok(result)) {
              error.textContent = message(result, '请重试');
              return;
            }
            ids.reset = App.requestId();
            clear(secret);
            if (result.data.temporary_password) {
              var account = {
                name: student.name, 'class': student['class'],
                username: result.data.username || student.username, password: result.data.temporary_password
              };
              var download = App.button('下载 Excel', 'btn btn-plain btn-small', function () { downloadAccounts([account], download); });
              add(secret, el('small', '', '新密码'), el('b', 'mono secret', account.password), download);
            } else {
              secret.appendChild(el('small', '', result.data.message || '已重置'));
            }
            secret.hidden = false;
          });
        });
    }

    function confirmRemove() {
      App.confirm({ title: '删除“' + student.name + '”？', message: '账号与报名记录一并删除', confirm: '删除', destructive: true })
        .then(function (yes) {
          if (!yes) return;
          App.busy(remove, '删除中');
          post('/api/delete_student', { student_id: student.id }).then(function (result) {
            App.busy(remove, null);
            if (!ok(result)) {
              error.textContent = message(result, '请重试');
              return;
            }
            sheet.close();
            App.toast('已删除 ' + student.name, 'ok');
            loadStudents();
          });
        });
    }
  }

  // ---- 导入名单：上传表格 → 服务器解析 → 核对 → 分批创建 → 下载账号表 ----
  var dropzone = $('#dropzone');
  $('#roster-pick').addEventListener('click', function () { $('#roster-file').click(); });
  $('#roster-file').addEventListener('change', function () {
    var file = takeFile($('#roster-file'));
    if (file) readRoster(file);
  });
  ['dragenter', 'dragover'].forEach(function (name) {
    dropzone.addEventListener(name, function (event) {
      event.preventDefault();
      App.toggleClass(dropzone, 'is-over', true);
    });
  });
  dropzone.addEventListener('dragleave', function () { App.toggleClass(dropzone, 'is-over', false); });
  dropzone.addEventListener('drop', function (event) {
    event.preventDefault();
    App.toggleClass(dropzone, 'is-over', false);
    var files = event.dataTransfer && event.dataTransfer.files;
    if (files && files[0]) readRoster(files[0]);
  });
  // 拖到投放区以外时，浏览器保持在本页。
  document.addEventListener('dragover', function (event) { event.preventDefault(); });
  document.addEventListener('drop', function (event) {
    if (event.dataTransfer && event.dataTransfer.files && event.dataTransfer.files.length) event.preventDefault();
  });

  function readRoster(file) {
    if (importing) return;
    if (file.size > MAX_UPLOAD) {
      App.toast('请选择 10 MB 以内的文件', 'err');
      return;
    }
    var button = $('#roster-pick');
    App.busy(button, '读取中');
    App.upload('/api/admin/import/parse', file, { type: 'application/octet-stream' }).then(function (result) {
      App.busy(button, null);
      if (!ok(result) || !Array.isArray(result.data.rows)) {
        App.toast(message(result, '请刷新页面'), 'err');
        return;
      }
      if (!result.data.rows.length) {
        App.toast('请检查表头是否含“姓名”“班级”', 'err');
        return;
      }
      openReview(file.name || '名单', result.data);
    });
  }

  function openReview(fileName, parsed) {
    var multiSheet = (parsed.sheets || []).filter(function (sheet) { return sheet.rows; }).length > 1;
    var rows = parsed.rows.map(function (row) {
      var notes = row.notes || [];
      return {
        sheet: row.sheet, line: row.line, name: row.name || '', klass: row['class'] || '',
        original: row.name || '', notes: notes, include: notes.indexOf('已导入') < 0
      };
    });
    var state = { filter: 'all', page: 0, toggle: null, pageRows: [] };
    var allTab = App.button('全部', '', function () { setFilter('all'); });
    var checkTab = App.button('待核对', '', function () { setFilter('check'); });
    var tabs = add(el('div', 'segmented'), allTab, checkTab);
    tabs.setAttribute('role', 'group');
    tabs.setAttribute('aria-label', '筛选');
    var table = el('table', 'tbl review');
    var pager = el('div', 'pager');
    var foot = el('span', 'hint');
    var sheet = App.sheet({
      title: '核对名单',
      message: fileName + ' · ' + rows.length + ' 行' + (parsed.skipped ? ' · 略过 ' + parsed.skipped + ' 行' : ''),
      body: add(el('div'), add(el('div', 'review-tools'), tabs), add(el('div', 'table-wrap'), table), pager),
      footNote: foot,
      onDismiss: function () { if (!importing) sheet.close(); },
      buttons: [
        { label: '取消', onClick: function (controller) { if (!importing) controller.close(); } },
        { label: '导入', kind: 'primary', onClick: runImport }
      ]
    });

    function problem(row) {
      if (!row.include) return '';
      var name = trim(row.name);
      var klass = trim(row.klass);
      if (!name) return '待补姓名';
      if (!klass) return '待补班级';
      if (name.length > 50 || klass.length > 50 || /[<>\u0000-\u001F]/.test(name + klass)) return '待核对内容';
      return '';
    }
    function notesFor(row) {
      return row.notes.filter(function (note) {
        if (note === '待补班级') return false;
        return !(note === '待核对姓名' && row.name !== row.original);
      });
    }
    function flagged(row) { return Boolean(problem(row)) || notesFor(row).length > 0; }
    function paintTags(cell, row) {
      clear(cell);
      var issue = problem(row);
      if (issue) cell.appendChild(el('span', 'tag tag-error', issue));
      notesFor(row).forEach(function (note) {
        cell.appendChild(el('span', note === '已导入' ? 'tag' : 'tag tag-warn', note));
      });
    }
    function summary() {
      var chosen = 0;
      var bad = 0;
      var review = 0;
      rows.forEach(function (row) {
        if (row.include) chosen += 1;
        if (problem(row)) bad += 1;
        if (flagged(row)) review += 1;
      });
      foot.textContent = '已选 ' + chosen + ' 名' + (bad ? ' · ' + bad + ' 行待补全' : '');
      allTab.textContent = '全部 ' + rows.length;
      checkTab.textContent = '待核对 ' + review;
      if (state.toggle) {
        state.toggle.checked = state.pageRows.length > 0 && state.pageRows.every(function (row) { return row.include; });
      }
      if (importing) return;
      var submit = sheet.buttons[1];
      submit.textContent = chosen ? '导入 ' + chosen + ' 名' : '导入';
      submit.disabled = !chosen || bad > 0;
    }
    function reviewRow(row) {
      var line = el('tr', row.include ? '' : 'is-out');
      var pick = checkbox(row.include);
      pick.setAttribute('aria-label', '导入第 ' + row.line + ' 行');
      var nameInput = textInput(row.name, 50);
      nameInput.setAttribute('aria-label', '姓名');
      var classInput = textInput(row.klass, 50);
      classInput.setAttribute('aria-label', '班级');
      var tags = el('td');
      pick.addEventListener('click', function () {
        row.include = pick.checked;
        line.className = row.include ? '' : 'is-out';
        paintTags(tags, row);
        summary();
      });
      nameInput.addEventListener('input', function () {
        row.name = nameInput.value;
        paintTags(tags, row);
        summary();
      });
      classInput.addEventListener('input', function () {
        row.klass = classInput.value;
        paintTags(tags, row);
        summary();
      });
      paintTags(tags, row);
      return add(line,
        add(el('td', 'col-check'), pick),
        el('td', 'col-line num muted', multiSheet ? row.sheet + ' · ' + row.line : String(row.line)),
        add(el('td'), nameInput),
        add(el('td'), classInput),
        tags);
    }
    function render() {
      var list = state.filter === 'all' ? rows : rows.filter(flagged);
      var pages = Math.max(1, Math.ceil(list.length / REVIEW_PAGE));
      if (state.page >= pages) state.page = pages - 1;
      var pageRows = list.slice(state.page * REVIEW_PAGE, (state.page + 1) * REVIEW_PAGE);
      allTab.setAttribute('aria-pressed', String(state.filter === 'all'));
      checkTab.setAttribute('aria-pressed', String(state.filter === 'check'));
      var toggle = checkbox(false);
      toggle.setAttribute('aria-label', '选择本页');
      state.toggle = toggle;
      state.pageRows = pageRows;
      toggle.addEventListener('click', function () {
        pageRows.forEach(function (row) { row.include = toggle.checked; });
        render();
      });
      clear(table);
      table.appendChild(add(el('thead'), add(el('tr'),
        add(el('th', 'col-check'), toggle), el('th', 'col-line', '行'), el('th', '', '姓名'), el('th', '', '班级'), el('th', '', '备注'))));
      var body = el('tbody');
      if (!pageRows.length) body.appendChild(emptyRow(state.filter === 'check' ? '全部核对完毕' : '表格里的行显示在这里', 5));
      pageRows.forEach(function (row) { body.appendChild(reviewRow(row)); });
      table.appendChild(body);
      renderPager(pager, state.page, pages, function (page) {
        state.page = page;
        render();
      });
      summary();
    }
    function setFilter(filter) {
      state.filter = filter;
      state.page = 0;
      render();
    }

    function runImport(controller) {
      if (importing) return;
      var picked = rows.filter(function (row) { return row.include && !problem(row); });
      if (!picked.length) return;
      importing = true;
      var submit = controller.buttons[1];
      var cancel = controller.buttons[0];
      App.busy(submit, '导入中');
      cancel.disabled = true;
      var credentials = [];
      var created = 0;
      var skipped = 0;
      var index = 0;
      function nextBatch() {
        if (index >= picked.length) return Promise.resolve(null);
        var part = picked.slice(index, index + IMPORT_BATCH);
        controller.setMessage('导入中 ' + index + ' / ' + picked.length);
        return post('/api/import_students', {
          students: part.map(function (row) { return { name: trim(row.name), 'class': trim(row.klass) }; })
        }, { timeout: 180000 }).then(function (result) {
          if (!result.ok || !result.data || !Array.isArray(result.data.credentials)) return message(result, '请重试');
          created += Number(result.data.success) || 0;
          skipped += Number(result.data.failed) || 0;
          credentials = credentials.concat(result.data.credentials);
          part.forEach(function (row) {
            row.include = false;
            row.notes = row.notes.concat(['已导入']);
          });
          index += part.length;
          return nextBatch();
        });
      }
      nextBatch().then(function (failure) {
        importing = false;
        App.busy(submit, null);
        cancel.disabled = false;
        if (credentials.length) {
          setCredentials(credentials, '导入');
          downloadAccounts(credentials);
        }
        loadStudents();
        if (failure) {
          controller.setMessage((created ? '已导入 ' + created + ' 名 · ' : '') + failure);
          render();
          return;
        }
        controller.close();
        App.toast('已导入 ' + created + ' 名' + (skipped ? ' · 略过 ' + skipped + ' 行' : ''), 'ok');
        go('students', 'accounts');
      });
    }

    render();
  }

  // ---- 账号表：导入或重置后生成 Excel，表头为姓名、班级、账号、密码 ----
  function downloadAccounts(accounts, button) {
    var rows = accounts.map(function (item) {
      return { name: item.name, 'class': item['class'] || '', username: item.username, password: item.password };
    });
    var chain = Promise.resolve(null);
    if (button) App.busy(button, '生成中');
    for (var start = 0; start < rows.length; start += ACCOUNT_FILE_ROWS) {
      (function (part) {
        chain = chain.then(function () {
          return post('/api/admin/accounts_workbook', { accounts: part }, { blob: true, timeout: 120000 }).then(function (result) {
            if (!result.ok || !result.blob) {
              App.toast(message(result, '请重试'), 'err');
              return;
            }
            App.saveBlob(result.blob, result.filename || '学生账号.xlsx');
          });
        });
      })(rows.slice(start, start + ACCOUNT_FILE_ROWS));
    }
    return chain.then(function () { if (button) App.busy(button, null); });
  }

  function setCredentials(list, label) {
    var now = new Date();
    store.credentials = list;
    store.credentialsNote = label + ' · ' + App.pad(now.getHours()) + ':' + App.pad(now.getMinutes());
    renderCredentials();
  }

  function renderCredentials() {
    var box = $('#cred-box');
    var actions = $('#cred-actions');
    var list = store.credentials;
    clear(box);
    clear(actions);
    if (!list.length) {
      box.appendChild(el('p', 'hint', '导入名单后自动生成 Excel'));
      return;
    }
    var download = App.button('下载 Excel', 'btn btn-primary btn-small', function () { downloadAccounts(store.credentials, download); });
    var discard = App.button('清除', 'btn btn-plain btn-small', function () {
      App.confirm({ title: '清除本页的 ' + list.length + ' 个密码？', confirm: '清除', destructive: true }).then(function (yes) {
        if (!yes) return;
        store.credentials = [];
        renderCredentials();
      });
    });
    add(actions, discard, download);
    box.appendChild(el('p', 'hint', store.credentialsNote + ' · ' + list.length + ' 个账号'));
    var table = el('table', 'tbl');
    head(table, ['姓名', '班级', '账号', '密码']);
    var body = el('tbody');
    list.slice(0, PREVIEW_ROWS).forEach(function (item) {
      add(body, add(el('tr'), el('td', '', item.name), el('td', '', item['class'] || ''),
        el('td', 'mono', item.username), el('td', 'mono', item.password)));
    });
    if (list.length > PREVIEW_ROWS) body.appendChild(emptyRow('其余 ' + (list.length - PREVIEW_ROWS) + ' 个见 Excel', 4));
    table.appendChild(body);
    box.appendChild(add(el('div', 'table-wrap'), table));
  }

  function fillClassSelect() {
    var select = $('#reset-class');
    var chosen = select.value;
    var counts = {};
    store.students.forEach(function (student) { counts[student['class']] = (counts[student['class']] || 0) + 1; });
    var classes = Object.keys(counts).sort(naturalCompare);
    clear(select);
    classes.forEach(function (klass) { select.options.add(new Option(klass + '（' + counts[klass] + ' 人）', klass)); });
    if (classes.indexOf(chosen) >= 0) select.value = chosen;
    $('#reset-class-go').disabled = !classes.length;
  }

  $('#reset-class').addEventListener('change', function () { classResetId = App.requestId(); });
  $('#reset-class-go').addEventListener('click', function () {
    var button = $('#reset-class-go');
    var klass = $('#reset-class').value;
    if (!klass) return;
    var count = store.students.filter(function (student) { return student['class'] === klass; }).length;
    App.confirm({ title: '重置“' + klass + '”的密码？', message: count + ' 名学生的新密码随即生效', confirm: '重置', destructive: true })
      .then(function (yes) {
        if (!yes) return;
        App.busy(button, '重置中');
        retry('/api/admin/reset_class_passwords', {
          'class': klass, reason: trim($('#reset-class-note').value) || '管理员重置', request_id: classResetId
        }).then(function (result) {
          App.busy(button, null);
          if (!ok(result)) {
            App.toast(message(result, '请重试'), 'err');
            return;
          }
          classResetId = App.requestId();
          $('#reset-class-note').value = '';
          var credentials = result.data.credentials || [];
          if (credentials.length) {
            setCredentials(credentials, klass + ' 重置');
            downloadAccounts(credentials);
          }
          App.toast('已重置 ' + result.data.count + ' 名', 'ok');
        });
      });
  });

  // ---- 社团 ----
  function loadClubs() {
    var version = begin('clubs');
    return Promise.all([App.request('/api/get_clubs'), loadSummary()]).then(function (results) {
      var result = results[0];
      if (!latest('clubs', version)) return;
      var box = $('#club-list');
      clear(box);
      if (!Array.isArray(result.data)) {
        box.appendChild(el('p', 'empty', message(result, '请刷新页面')));
        return;
      }
      rememberClubs(result.data);
      if (!result.data.length) {
        var create = App.button('添加社团', 'btn btn-primary btn-small');
        create.setAttribute('data-go', 'clubs/add');
        box.appendChild(add(el('div', 'empty'), el('p', '', '添加社团后显示在这里'), add(el('div', 'actions center'), create)));
        return;
      }
      result.data.forEach(function (club) { box.appendChild(clubCard(club)); });
    });
  }

  function clubCard(club) {
    var info = splitRoom(club.name);
    var art = el('div', 'club-art');
    art.setAttribute('data-tone', String((Number(club.id) % 5) + 1));
    art.appendChild(ClubArt.node(club));
    var body = el('div', 'club-body');
    body.appendChild(el('h3', 'club-name', info.name));
    var meta = [info.room, club.location, club.meeting_time, teacher(club.advisor_name)].filter(Boolean).join(' · ');
    if (meta) body.appendChild(el('p', 'club-meta', meta));
    if (club.description) body.appendChild(el('p', 'club-desc', club.description));
    var tags = el('div', 'club-tags');
    if (!club.enabled) tags.appendChild(el('span', 'tag tag-warn', '暂停报名'));
    if ((club.allowed_grades || []).length) tags.appendChild(el('span', 'tag', '限' + club.allowed_grades.join('、')));
    if ((club.allowed_classes || []).length) tags.appendChild(el('span', 'tag', '限 ' + club.allowed_classes.length + ' 个班级'));
    if (club.seat_sync_pending) tags.appendChild(el('span', 'tag tag-warn', '名额更新中'));
    if (tags.childNodes.length) body.appendChild(tags);
    if (batchMode()) {
      // 分批报名：名额按批计算，列出各批已报名人数。
      add(body, add(el('div', 'seats'), el('span', '', '每批名额'), el('b', 'num', String(club.max_students))));
      var counts = el('p', 'club-batches');
      batchList().forEach(function (batch, index) {
        if (index) counts.appendChild(document.createTextNode(' · '));
        add(counts, document.createTextNode(batch.name + ' '), el('b', 'num', String(Number((batch.clubs || {})[club.id]) || 0)));
      });
      if (counts.childNodes.length) body.appendChild(counts);
    } else {
      add(body,
        add(el('div', 'seats'), el('span', '', '已报名'), el('b', 'num', String(club.current_students)), el('span', '', '/ ' + club.max_students)),
        meter(club.max_students ? club.current_students / club.max_students : 0, club.max_students - club.current_students <= 3));
    }
    var roster = el('a', 'btn btn-small', '名单');
    roster.href = '/api/export_club_data?club_id=' + encodeURIComponent(club.id);
    body.appendChild(add(el('div', 'actions'),
      App.button('编辑', 'btn btn-primary btn-small', function () { openClubEditor(club); }),
      roster,
      App.button('删除', 'btn btn-destructive btn-small', function () { confirmDeleteClub(club); })));
    return add(el('article', 'club'), art, body);
  }

  function uploadClubImage(clubId, file, revision) {
    var query = '?club_id=' + encodeURIComponent(clubId) + '&expected_revision=' + encodeURIComponent(revision) +
      '&request_id=' + encodeURIComponent(App.requestId()) + '&reason=' + encodeURIComponent('管理员更换社团图片');
    return App.upload('/api/upload_club_image' + query, file, { type: file.type || 'application/octet-stream' }).then(function (result) {
      return ok(result) ? null : '信息已存储；' + message(result, '图片请重新上传');
    });
  }

  function removeClubImage(clubId, revision) {
    return post('/api/remove_club_image', {
      club_id: clubId, expected_revision: revision, request_id: App.requestId(), reason: '管理员恢复默认图片'
    }).then(function (result) {
      return ok(result) ? null : '信息已存储；' + message(result, '请重试');
    });
  }

  function openClubEditor(original) {
    var club = original;
    var hasPhoto = typeof club.image_path === 'string' && club.image_path.indexOf('/club-images/') === 0;
    var image = { file: null, url: '', remove: false };
    var name = textInput(club.name, 80);
    var capacity = el('input', 'input');
    capacity.type = 'number';
    capacity.min = String(Math.max(1, peakUsed(club)));
    capacity.max = '10000';
    capacity.step = '1';
    capacity.value = String(club.max_students);
    var description = el('textarea', 'input');
    description.maxLength = 1000;
    description.value = club.description || '';
    var advisor = textInput(club.advisor_name, 80);
    var meeting = textInput(club.meeting_time, 120);
    var place = textInput(club.location, 120);
    var grades = textInput((club.allowed_grades || []).join('，'), 200);
    grades.placeholder = '全部年级';
    var classes = textInput((club.allowed_classes || []).join('，'), 400);
    classes.placeholder = '全部班级';
    var enabled = checkbox(club.enabled);
    var tone = String((Number(club.id) % 5) + 1);
    var preview = el('div', 'club-art');
    preview.setAttribute('data-tone', tone);
    var picker = fileInput('image/jpeg,image/png,image/webp,image/gif');
    var change = App.button('上传照片', 'btn btn-small', function () { picker.click(); });
    var restore = App.button('改用插图', 'btn btn-plain btn-small', function () { useArt(chosen); });
    var error = el('p', 'error');
    error.setAttribute('role', 'alert');
    // 卡片插图：点选即用这幅插图；上传的照片优先显示，点插图时改用插图。
    var chosen = ClubArt.kind(club);
    var tiles = {};
    var gallery = el('div', 'art-grid');
    gallery.setAttribute('role', 'group');
    gallery.setAttribute('aria-label', '卡片插图');
    ClubArt.options.forEach(function (option) {
      var thumb = el('span', 'club-art');
      thumb.setAttribute('data-tone', tone);
      thumb.appendChild(ClubArt.illustration(option[0]));
      var tile = add(App.button('', 'art-option', function () { useArt(option[0]); }), thumb, el('span', '', option[1]));
      tile.setAttribute('aria-pressed', 'false');
      tiles[option[0]] = tile;
      gallery.appendChild(tile);
    });

    function releasePreview() {
      if (image.url && window.URL && window.URL.revokeObjectURL) window.URL.revokeObjectURL(image.url);
      image.url = '';
    }
    function photoShown() { return Boolean(image.file) || (hasPhoto && !image.remove); }
    function useArt(key) {
      chosen = key;
      releasePreview();
      image.file = null;
      image.remove = hasPhoto;
      paint();
    }
    // 预览即学生看到的卡片图：上传的照片，或选定的插图。
    function paint() {
      clear(preview);
      if (image.file) preview.appendChild(ClubArt.picture(image.url, club.name));
      else if (photoShown()) preview.appendChild(ClubArt.node(club));
      else preview.appendChild(ClubArt.illustration(chosen));
      restore.hidden = !photoShown();
      ClubArt.options.forEach(function (option) {
        tiles[option[0]].setAttribute('aria-pressed', String(!photoShown() && option[0] === chosen));
      });
    }
    picker.addEventListener('change', function () {
      var file = takeFile(picker);
      if (!file) return;
      if (file.size > MAX_UPLOAD) {
        error.textContent = '请选择 10 MB 以内的图片';
        return;
      }
      releasePreview();
      image.file = file;
      image.url = window.URL.createObjectURL(file);
      image.remove = false;
      error.textContent = '';
      paint();
    });
    paint();

    var form = add(el('div', 'editor'),
      add(el('div', 'image-row'), preview, add(el('div', 'actions'), change, restore), picker),
      add(el('div', 'field'), el('span', '', '卡片插图'), gallery),
      add(el('div', 'form-row'), field('名称', name), field(capacityLabel(), capacity)),
      field('简介', description),
      add(el('div', 'form-row'), field('指导老师', advisor), field('活动时间', meeting)),
      field('地点', place),
      add(el('div', 'form-row'), field('年级范围', grades), field('班级范围', classes)),
      add(el('label', 'check'), enabled, '开放报名'),
      error);
    var sheet = App.sheet({
      narrow: true,
      title: splitRoom(club.name).name,
      body: form,
      onDismiss: function () {
        releasePreview();
        sheet.close();
      },
      buttons: [
        { label: '取消', onClick: function (controller) { releasePreview(); controller.close(); } },
        { label: '存储', kind: 'primary', onClick: save }
      ]
    });
    setTimeout(function () { name.focus(); }, 0);

    function refreshRevision() {
      return App.request('/api/get_clubs').then(function (result) {
        rememberClubs(result.data);
        if (store.clubIndex[club.id]) club = store.clubIndex[club.id];
      });
    }

    function save(controller) {
      var max = Number(capacity.value);
      var minimum = Math.max(1, peakUsed(club));
      if (!trim(name.value)) {
        error.textContent = '请填写名称';
        name.focus();
        return;
      }
      if (!isInteger(max) || max < minimum || max > 10000) {
        error.textContent = capacityLabel() + '为 ' + minimum + '–10000';
        capacity.focus();
        return;
      }
      var submit = controller.buttons[1];
      error.textContent = '';
      App.busy(submit, '存储中');
      post('/api/update_club', {
        club_id: club.id, request_id: App.requestId(), reason: '管理员编辑社团', expected_revision: club.revision,
        name: trim(name.value), max_students: max, art: chosen, description: trim(description.value),
        advisor_name: trim(advisor.value), meeting_time: trim(meeting.value), location: trim(place.value),
        allowed_grades: splitList(grades.value), allowed_classes: splitList(classes.value), enabled: enabled.checked
      }).then(function (result) {
        if (!ok(result)) return message(result, '请重试');
        if (result.data.club) club = result.data.club;
        if (image.file) return uploadClubImage(club.id, image.file, club.revision);
        if (image.remove) return removeClubImage(club.id, club.revision);
        return null;
      }).then(function (failure) {
        App.busy(submit, null);
        if (failure) {
          error.textContent = failure;
          refreshRevision();
          return;
        }
        releasePreview();
        controller.close();
        App.toast('已存储', 'ok');
        loadClubs();
      });
    }
  }

  function confirmDeleteClub(club) {
    var name = splitRoom(club.name).name;
    App.confirm({
      title: '删除“' + name + '”？',
      message: totalUsed(club) ? totalUsed(club) + ' 条报名一并删除，删除前自动保存快照' : '',
      confirm: '删除',
      destructive: true
    }).then(function (yes) {
      if (!yes) return;
      post('/api/delete_club', { club_id: club.id }).then(function (result) {
        if (!ok(result)) {
          App.toast(message(result, '请重试'), 'err');
          return;
        }
        App.toast('已删除 ' + name, 'ok');
        loadClubs();
      });
    });
  }

  $('#club-add').addEventListener('submit', function (event) {
    event.preventDefault();
    var nameInput = $('#club-add-name');
    var maxInput = $('#club-add-max');
    var error = $('#club-add-error');
    var button = $('#club-add-go');
    var name = trim(nameInput.value);
    var max = Number(maxInput.value);
    if (!name) {
      error.textContent = '请填写名称';
      nameInput.focus();
      return;
    }
    if (!isInteger(max) || max < 1 || max > 10000) {
      error.textContent = capacityLabel() + '为 1–10000';
      maxInput.focus();
      return;
    }
    error.textContent = '';
    App.busy(button, '添加中');
    post('/api/import_clubs', { clubs: [{ name: name, max_students: max }] }).then(function (result) {
      App.busy(button, null);
      var added = Number(result.data && result.data.success) || 0;
      if (!added) {
        error.textContent = result.data && result.data.failed ? '名称已存在' : message(result, '请重试');
        return;
      }
      nameInput.value = '';
      maxInput.value = '';
      App.toast('已添加 ' + name, 'ok');
      go('clubs', 'cards');
    });
  });

  // ---- 调剂 ----
  function loadAdjust() {
    var version = begin('adjust');
    return App.request('/api/admin/adjustment').then(function (result) {
      if (!latest('adjust', version)) return;
      var box = $('#adjust-body');
      var plan = result.data;
      clear(box);
      if (!result.ok || !plan) {
        box.appendChild(el('p', 'empty', message(result, '请刷新页面')));
        return;
      }
      var metrics = el('div', 'metrics');
      [['未报名', plan.unregistered], ['空余名额', plan.seats], ['可安排', plan.assignable]].forEach(function (item) {
        metrics.appendChild(add(el('div', 'metric'), el('small', '', item[0]), el('b', 'num', String(item[1]))));
      });
      box.appendChild(metrics);
      // 分批报名时每批只用本批的空余名额，表格按批列出。
      if (plan.clubs.length) {
        var table = el('table', 'tbl');
        head(table, plan.batch_mode ? ['批次', '社团', '空余', '将安排'] : ['社团', '空余', '将安排']);
        var body = el('tbody');
        plan.clubs.forEach(function (club) {
          var row = el('tr');
          if (plan.batch_mode) row.appendChild(el('td', '', '第 ' + club.batch + ' 批'));
          add(body, add(row, el('td', '', splitRoom(club.name).name), el('td', 'num', String(club.remaining)), el('td', 'num', String(club.assign))));
        });
        table.appendChild(body);
        box.appendChild(add(el('div', 'table-wrap'), table));
      }
      if (plan.unbatched) {
        box.appendChild(el('p', 'note gap', '班级待分批的 ' + plan.unbatched + ' 名学生，加入批次后即可调剂'));
      }
      var actions = el('div', 'actions');
      var short = plan.unregistered - plan.assignable - (plan.unbatched || 0);
      if (!plan.unregistered) {
        box.appendChild(el('p', 'hint gap', '全部学生已有社团'));
      } else if (!plan.locked) {
        box.appendChild(el('p', 'hint gap', plan.batch_mode ? '最后一批截止后名单自动锁定，随后可调剂' : '锁定名单后可调剂'));
        var lock = App.button('锁定名单', 'btn btn-small');
        lock.setAttribute('data-go', 'settings/time');
        actions.appendChild(lock);
      } else {
        if (short > 0) {
          box.appendChild(el('p', 'note gap', '再增加 ' + short + ' 个名额可安排全部学生：提高' +
            (plan.batch_mode ? '每批名额' : '人数上限') + '或放宽报名范围'));
        }
        if (plan.assignable) {
          var requestId = App.requestId();
          var run = App.button('调剂 ' + plan.assignable + ' 名', 'btn btn-primary btn-small', function () {
            App.confirm({
              title: '调剂 ' + plan.assignable + ' 名学生？',
              message: plan.batch_mode ? '每批按本批的空余名额与报名范围随机安排' : '按空余名额与报名范围随机安排',
              confirm: '调剂'
            })
              .then(function (yes) {
                if (!yes) return;
                App.busy(run, '调剂中');
                retry('/api/admin/adjust_registrations', { request_id: requestId }).then(function (outcome) {
                  App.busy(run, null);
                  if (!ok(outcome)) {
                    App.toast(message(outcome, '请重试'), 'err');
                    return;
                  }
                  App.toast('已调剂 ' + outcome.data.assigned + ' 名', 'ok');
                  loadAdjust();
                });
              });
          });
          actions.appendChild(run);
        }
      }
      var download = el('a', 'btn btn-small', '下载报名表');
      download.href = '/api/export_workbook';
      actions.appendChild(download);
      box.appendChild(actions);
    });
  }

  // ---- 操作记录 ----
  var actionLabels = {
    'registration.created': '报名', 'registration.cancelled': '退选',
    'registration.admin_assigned': '代为报名', 'registration.admin_removed': '移出社团', 'registration.admin_transferred': '调整社团',
    'registration.start_time_updated': '设置开放时间', 'registration.end_time_updated': '设置截止时间', 'registration.lock_updated': '名单锁定',
    'student.password_reset': '重置学生密码', 'student.password_changed': '学生修改密码', 'student.deleted': '删除学生',
    'student.updated': '修改学生信息', 'students.passwords_reset': '按班重置密码',
    'students.imported': '导入学生', 'students.deleted_all': '删除全部学生',
    'club.updated': '编辑社团', 'club.deleted': '删除社团', 'club.image_uploaded': '更换社团图片', 'club.image_removed': '恢复默认图片',
    'clubs.imported': '添加社团', 'clubs.deleted_all': '删除全部社团', 'admin.password_changed': '修改管理员密码',
    'registrations.adjusted': '调剂', 'registrations.cleared': '清空报名', 'admin.password_reset': '重置管理员密码',
    'registrations.exported': '导出报名数据', 'students.exported': '导出学生账号', 'backup.downloaded': '下载备份',
    'backup.restored': '恢复备份', 'batches.updated': '设置报名方式'
  };
  var downloadActions = ['registrations.exported', 'students.exported', 'backup.downloaded'];
  var downloadLabels = {
    workbook: '报名表（Excel）', all_csv: '报名总表（CSV）', unregistered_csv: '未报名名单（CSV）', club_csv: '社团名单（CSV）',
    students_csv: '学生账号（CSV）', accounts_xlsx: '学生账号（Excel）', full: '完整备份', snapshot: '自动快照'
  };

  function auditTime(ms) {
    var date = new Date(Number(ms));
    if (isNaN(date.getTime())) return '';
    return App.pad(date.getMonth() + 1) + '-' + App.pad(date.getDate()) + ' ' + App.pad(date.getHours()) + ':' +
      App.pad(date.getMinutes()) + ':' + App.pad(date.getSeconds());
  }

  function auditTarget(event) {
    var before = event.before || {};
    var after = event.after || {};
    switch (event.target_type) {
      case 'student':
        return !store.studentIndex[event.target_id] && before.name
          ? before.name + (before['class'] ? '（' + before['class'] + '）' : '') : studentLabel(event.target_id);
      case 'club':
        return !store.clubIndex[event.target_id] && before.name ? splitRoom(before.name).name : clubName(event.target_id);
      case 'registration':
        return clubName(after.club_id || before.club_id);
      case 'students':
        if (before['class']) return before['class'] + ' · ' + after.count + ' 名';
        if (after.success !== undefined) return after.success + ' 名';
        return before.students !== undefined ? before.students + ' 名' : '全部学生';
      case 'clubs':
        return after.success !== undefined ? after.success + ' 个' : '全部社团';
      case 'registrations':
        return '未报名学生';
      case 'data':
        return '全部数据';
      default:
        return '';
    }
  }

  function auditDetail(event) {
    var before = event.before || {};
    var after = event.after || {};
    var meta = event.metadata || {};
    var parts = [];
    switch (event.action) {
      case 'registration.admin_assigned': parts.push('报名 ' + clubName(after.club_id)); break;
      case 'registration.admin_removed': parts.push('从 ' + clubName(before.club_id) + ' 移出'); break;
      case 'registration.admin_transferred': parts.push(clubName(before.club_id) + ' → ' + clubName(after.club_id)); break;
      case 'registration.start_time_updated': parts.push(after.start_time || '清除'); break;
      case 'registration.end_time_updated': parts.push(after.end_time || '清除'); break;
      case 'registration.lock_updated': parts.push(after.locked ? '锁定' : '解除锁定'); break;
      case 'registrations.cleared': parts.push('删除 ' + after.cleared + ' 条报名'); break;
      case 'registrations.adjusted':
        parts.push('安排 ' + after.assigned + ' 名' + (after.unassigned ? '，' + after.unassigned + ' 名待安排' : ''));
        break;
      case 'student.updated':
        [['name', '姓名'], ['class', '班级']].forEach(function (pair) {
          if (before[pair[0]] !== after[pair[0]]) parts.push(pair[1] + ' ' + before[pair[0]] + ' → ' + after[pair[0]]);
        });
        break;
      case 'backup.restored':
        parts.push((after.source || '') + ' · 学生 ' + after.students + ' · 社团 ' + after.clubs + ' · 报名 ' + after.registrations);
        break;
      case 'batches.updated':
        parts.push(after.batch_mode ? '分批报名 · ' + (after.batches || []).length + ' 批' : '同时报名');
        break;
      default:
        break;
    }
    if (downloadActions.indexOf(event.action) >= 0) {
      parts.push((downloadLabels[meta.kind] || '') + (meta.club ? ' · ' + meta.club : '') + (meta.name ? ' · ' + meta.name : '') +
        (meta.count ? ' · ' + meta.count + ' 个' : ''));
    }
    if (event.reason) parts.push(event.reason);
    return parts.filter(Boolean).join(' · ');
  }

  function loadAudit(reset) {
    var version = begin('audit');
    if (reset) audit.cursor = null;
    var ready = store.studentsReady ? Promise.resolve(null) : loadStudents();
    return ready.then(function () {
      var query = '?limit=50' + (audit.role ? '&role=' + encodeURIComponent(audit.role) : '') +
        (audit.cursor ? '&cursor=' + encodeURIComponent(audit.cursor) : '');
      return App.request('/api/admin/audit_events' + query);
    }).then(function (result) {
      if (!latest('audit', version)) return;
      var table = $('#audit-tbl');
      var more = $('#audit-more');
      if (!result.data || !Array.isArray(result.data.events)) {
        tableMessage(table, message(result, '请刷新页面'));
        more.hidden = true;
        return;
      }
      var events = result.data.events;
      if (reset) {
        clear(table);
        head(table, ['时间', '操作人', '操作', '对象', '说明']);
        table.appendChild(el('tbody'));
      }
      var body = $('tbody', table);
      if (reset && !events.length) body.appendChild(emptyRow('操作记录会显示在这里', 5));
      events.forEach(function (event) {
        var actor = event.actor_role === 'student' ? studentLabel(event.actor_id) : event.actor_id;
        add(body, add(el('tr'),
          el('td', 'num', auditTime(event.occurred_at)),
          el('td', '', actor),
          el('td', '', actionLabels[event.action] || event.action),
          el('td', '', auditTarget(event)),
          el('td', 'wrap', auditDetail(event))));
      });
      audit.cursor = result.data.next_cursor;
      more.hidden = events.length < 50 || !audit.cursor;
    });
  }

  $('#audit-scope').addEventListener('click', function (event) {
    var button = targetOf(event, 'button[data-role]');
    if (!button) return;
    audit.role = button.getAttribute('data-role');
    $$('#audit-scope button').forEach(function (item) { item.setAttribute('aria-pressed', String(item === button)); });
    loadAudit(true);
  });
  $('#audit-more').addEventListener('click', function () { loadAudit(false); });

  // ---- 设置：时间与锁定 ----
  var dateTimeSupported = (function () {
    var probe = document.createElement('input');
    try { probe.setAttribute('type', 'datetime-local'); } catch (error) { return false; }
    return probe.type === 'datetime-local';
  })();
  if (!dateTimeSupported) {
    $('#open-at').placeholder = '2026-09-01 08:00';
    $('#close-at').placeholder = '2026-09-01 18:00';
  }
  function toField(text) {
    if (!text) return '';
    var value = String(text).slice(0, 16);
    return dateTimeSupported ? value.replace(' ', 'T') : value;
  }
  function fromField(value) {
    var match = /^(\d{4})[-/.](\d{1,2})[-/.](\d{1,2})[T\s]+(\d{1,2}):(\d{2})/.exec(trim(value));
    if (!match) return null;
    return match[1] + '-' + App.pad(Number(match[2])) + '-' + App.pad(Number(match[3])) + ' ' +
      App.pad(Number(match[4])) + ':' + match[5] + ':00';
  }

  function loadSettings() {
    var version = begin('settings');
    return Promise.all([App.request('/api/check_registration_time'), App.request('/api/admin/summary')]).then(function (results) {
      if (!latest('settings', version)) return;
      var time = results[0].data;
      var summary = results[1].data;
      if (!time) return;
      store.time = time;
      if (summary) {
        store.summary = summary;
        applyModeLabels();
      }
      $('#open-at').value = toField(time.start_time);
      $('#close-at').value = toField(time.end_time);
      $('#clear-time').hidden = !time.start_time;
      renderMode();
      renderLock(time);
      if (summary) renderData(time, summary);
    });
  }

  function renderLock(time) {
    var locked = Boolean(time.registration_locked);
    var state = $('#lock-state');
    state.textContent = locked ? '已锁定' : '可修改';
    state.className = locked ? 'tag tag-warn' : 'tag';
    var detail = $('#lock-detail');
    detail.textContent = locked ? '报名与退选已停止'
      : batchMode() ? (batchList().length ? '最后一批截止后自动锁定' : '')
        : time.end_time ? App.schoolTime(time.end_time) + ' 自动锁定' : '';
    detail.hidden = !detail.textContent;
    var button = $('#toggle-lock');
    button.textContent = locked ? '解除锁定' : '锁定名单';
    button.disabled = false;
  }

  $('#time-form').addEventListener('submit', function (event) {
    event.preventDefault();
    var error = $('#time-error');
    var startText = trim($('#open-at').value);
    var closeText = trim($('#close-at').value);
    var start = fromField(startText);
    var close = closeText ? fromField(closeText) : '';
    if (!startText) {
      error.textContent = '请填写开放时间';
      return;
    }
    if (!start || close === null) {
      error.textContent = '时间格式：2026-09-01 08:00';
      return;
    }
    if (close && close <= start) {
      error.textContent = '截止时间需晚于开放时间';
      return;
    }
    error.textContent = '';
    var button = $('#save-time');
    App.busy(button, '存储中');
    post('/api/update_registration_time', { start_time: start, end_time: close }).then(function (result) {
      App.busy(button, null);
      if (!ok(result)) {
        error.textContent = message(result, '请重试');
        return;
      }
      App.toast('已存储', 'ok');
      loadSettings();
    });
  });

  $('#clear-time').addEventListener('click', function () {
    App.confirm({ title: '清除开放时间？', message: '学生页显示“开放时间待定”', confirm: '清除', destructive: true }).then(function (yes) {
      if (!yes) return;
      retry('/api/update_registration_time', { start_time: '' }).then(function (result) {
        if (!ok(result)) {
          App.toast(message(result, '请重试'), 'err');
          return;
        }
        App.toast('已清除', 'ok');
        loadSettings();
      });
    });
  });

  $('#toggle-lock').addEventListener('click', function () {
    var button = $('#toggle-lock');
    var locked = Boolean(store.time && store.time.registration_locked);
    var options = locked
      ? { title: '解除锁定？', message: '学生可继续报名与退选', confirm: '解除' }
      : { title: '锁定名单？', message: '报名与退选停止，并自动保存快照', confirm: '锁定' };
    App.confirm(options).then(function (yes) {
      if (!yes) return;
      App.busy(button, '处理中');
      post('/api/update_registration_lock', {
        locked: !locked, reason: locked ? '解除锁定' : '手动锁定', request_id: App.requestId()
      }).then(function (result) {
        App.busy(button, null);
        if (!ok(result)) {
          App.toast(message(result, '请重试'), 'err');
          return;
        }
        App.toast(locked ? '已解除锁定' : '已锁定', 'ok');
        loadSettings();
      });
    });
  });

  // ---- 设置：报名方式与批次 ----
  var MODE_HINTS = {
    single: '全体学生同一时段报名，共用名额。学生分几节课报名时，选“分批报名”。',
    batch: '各班按批次轮流报名，每批名额单独计算。'
  };
  var CLASS_PERIOD_MINUTES = 40;

  function renderMode() {
    var batched = batchMode();
    $$('#mode-switch button').forEach(function (button) {
      button.setAttribute('aria-pressed', String((button.getAttribute('data-mode') === 'batch') === batched));
    });
    $('#mode-hint').textContent = MODE_HINTS[batched ? 'batch' : 'single'];
    $('#time-form').hidden = batched;
    $('#batch-pane').hidden = !batched;
    if (batched) renderBatchList();
  }

  function renderBatchList() {
    var list = $('#batch-list');
    var summary = store.summary;
    clear(list);
    if (!summary.batches.length) {
      list.appendChild(add(el('li'), el('span', 'hint', '添加批次，为每批选择班级和时段')));
    }
    summary.batches.forEach(function (batch) {
      var tag = el('span', batch.state === 'open' ? 'tag tag-ok' : 'tag', batchStateText(batch));
      var buttons = el('div', 'actions');
      var now = batchNow();
      if (now && now.batch === batch) {
        var quick = App.button(now.action === 'open' ? '现在开放' : '现在截止', 'btn btn-primary btn-small', function () {
          runBatchNow(now, quick);
        });
        buttons.appendChild(quick);
      }
      buttons.appendChild(App.button('编辑', 'btn btn-small', function () { openBatchEditor(batch); }));
      add(list, add(el('li'),
        add(el('span'), add(el('b', '', batch.name), tag),
          el('small', '', batchWindow(batch)),
          el('small', '', batch.classes.join('、') + ' · ' + batch.students + ' 人')),
        buttons));
    });
    var note = $('#batch-unassigned');
    note.textContent = summary.unassigned_students ? '待分批：' + summary.unassigned.map(function (item) {
      return item['class'];
    }).join('、') + '（' + summary.unassigned_students + ' 人），请加入批次' : '';
    note.hidden = !summary.unassigned_students;
  }

  // 保存报名方式与批次；batches 省略时只改报名方式。
  function saveBatches(batched, batches) {
    var body = { batch_mode: batched, request_id: App.requestId() };
    if (batches) {
      body.batches = batches.map(function (batch) {
        return { start_time: batch.start_time, end_time: batch.end_time, classes: batch.classes };
      });
    }
    return retry('/api/admin/batches', body);
  }

  $('#mode-switch').addEventListener('click', function (event) {
    var button = targetOf(event, 'button[data-mode]');
    if (!button || !store.summary) return;
    var batched = button.getAttribute('data-mode') === 'batch';
    if (batched === batchMode()) return;
    App.confirm(batched
      ? { title: '改为分批报名？', message: MODE_HINTS.batch + '开放时间按批次设置。', confirm: '切换' }
      : { title: '改为同时报名？', message: MODE_HINTS.single.split('。')[0] + '。请随后设置开放时间。', confirm: '切换' }
    ).then(function (yes) {
      if (!yes) return;
      saveBatches(batched).then(function (result) {
        if (!ok(result)) {
          App.toast(message(result, '请重试'), 'err');
          return;
        }
        App.toast(batched ? '已改为分批报名' : '已改为同时报名', 'ok');
        loadSettings();
      });
    });
  });

  // 时间文本加上若干分钟（按学校时间的文本计算，与设备时区无关）。
  function addMinutes(text, minutes) {
    var match = /^(\d{4})-(\d{2})-(\d{2}) (\d{2}):(\d{2})/.exec(text || '');
    if (!match) return '';
    var date = new Date(Date.UTC(Number(match[1]), Number(match[2]) - 1, Number(match[3]), Number(match[4]), Number(match[5]) + minutes));
    return date.getUTCFullYear() + '-' + App.pad(date.getUTCMonth() + 1) + '-' + App.pad(date.getUTCDate()) + ' ' +
      App.pad(date.getUTCHours()) + ':' + App.pad(date.getUTCMinutes()) + ':00';
  }

  function datetimeInput(text) {
    var input = el('input', 'input');
    try { input.type = 'datetime-local'; } catch (error) { input.placeholder = '2026-09-01 08:00'; }
    if (!dateTimeSupported) input.placeholder = '2026-09-01 08:00';
    input.value = toField(text);
    return input;
  }

  // 批次编辑：时段与班级。勾选其他批次的班级会把它移到这一批。
  function openBatchEditor(batch) {
    var batches = batchList();
    var ready = store.studentsReady ? Promise.resolve(null) : loadStudents();
    ready.then(function () {
      var counts = {};
      store.students.forEach(function (student) { counts[student['class']] = (counts[student['class']] || 0) + 1; });
      var owner = {};
      batches.forEach(function (item) { item.classes.forEach(function (klass) { owner[klass] = item; }); });
      var classes = Object.keys(counts);
      Object.keys(owner).forEach(function (klass) { if (!has(counts, klass)) classes.push(klass); });
      classes.sort(naturalCompare);
      var last = batches[batches.length - 1];
      var startDefault = batch ? batch.start_time : last ? last.end_time : '';
      var start = datetimeInput(startDefault);
      var end = datetimeInput(batch ? batch.end_time : startDefault ? addMinutes(startDefault, CLASS_PERIOD_MINUTES) : '');
      start.addEventListener('change', function () {
        var text = fromField(start.value);
        if (text && !trim(end.value)) end.value = toField(addMinutes(text, CLASS_PERIOD_MINUTES));
      });
      var picks = {};
      var grid = el('div', 'class-grid');
      var tally = el('p', 'hint batch-tally');
      function paintTally() {
        var chosen = classes.filter(function (klass) { return picks[klass].checked; });
        var people = 0;
        chosen.forEach(function (klass) { people += counts[klass] || 0; });
        tally.textContent = chosen.length ? '已选 ' + chosen.length + ' 个班级 · ' + people + ' 人' : '请选择这一批的班级';
        classes.forEach(function (klass) { App.toggleClass(picks[klass].parentNode, 'is-on', picks[klass].checked); });
      }
      classes.forEach(function (klass) {
        var box = checkbox(batch ? owner[klass] === batch : false);
        box.addEventListener('change', paintTally);
        picks[klass] = box;
        var label = add(el('label', 'check'), box, document.createTextNode(klass), el('small', 'num', (counts[klass] || 0) + ' 人'));
        var other = owner[klass];
        if (other && other !== batch) label.appendChild(el('span', 'tag', other.name));
        grid.appendChild(label);
      });
      paintTally();
      var error = el('p', 'error');
      error.setAttribute('role', 'alert');
      var form = add(el('div', 'editor'),
        add(el('div', 'form-row'), field('开放', start), field('截止', end)),
        el('p', 'hint gap', '学校时间。批次按开放时间自动编号'),
        add(el('div', 'field'), el('span', '', '班级'), grid),
        tally,
        error);
      var remove = null;
      if (batch) {
        remove = App.button('删除', 'btn btn-destructive', function () {
          App.confirm({ title: '删除' + batch.name + '？', message: '这些班级改为待分批', confirm: '删除', destructive: true })
            .then(function (yes) {
              if (!yes) return;
              submit(batches.filter(function (item) { return item !== batch; }), sheet, '已删除' + batch.name);
            });
        });
      }
      var sheet = App.sheet({
        narrow: true,
        title: batch ? batch.name : '添加批次',
        body: form,
        footNote: remove || el('span'),
        buttons: [
          { label: '取消', onClick: function (controller) { controller.close(); } },
          { label: '存储', kind: 'primary', onClick: function (controller) { save(controller); } }
        ]
      });

      function submit(list, controller, done) {
        var button = controller.buttons[1];
        App.busy(button, '存储中');
        saveBatches(true, list).then(function (result) {
          App.busy(button, null);
          if (!ok(result)) {
            error.textContent = message(result, '请重试');
            return;
          }
          controller.close();
          App.toast(done, 'ok');
          loadSettings();
        });
      }

      function save(controller) {
        var startText = fromField(start.value);
        var endText = fromField(end.value);
        var chosen = classes.filter(function (klass) { return picks[klass].checked; });
        if (!startText || !endText) {
          error.textContent = '请填写开放和截止时间，格式 2026-09-01 08:00';
          return;
        }
        if (endText <= startText) {
          error.textContent = '截止时间请晚于开放时间';
          return;
        }
        if (!chosen.length) {
          error.textContent = '请选择这一批的班级';
          return;
        }
        error.textContent = '';
        var list = [];
        batches.forEach(function (item) {
          if (item === batch) return;
          var rest = item.classes.filter(function (klass) { return chosen.indexOf(klass) < 0; });
          list.push({ start_time: item.start_time, end_time: item.end_time, classes: rest });
        });
        list.push({ start_time: startText, end_time: endText, classes: chosen });
        submit(list, controller, '已存储');
      }
    });
  }

  $('#batch-add').addEventListener('click', function () { openBatchEditor(null); });

  // 快速分批：按班级顺序每几个班一批，从第 1 批开放时间起依次排时段。
  function numberInput(value, min, max) {
    var input = el('input', 'input');
    input.type = 'number';
    input.min = String(min);
    input.max = String(max);
    input.step = '1';
    input.value = String(value);
    return input;
  }

  function openQuickSplit() {
    var ready = store.studentsReady ? Promise.resolve(null) : loadStudents();
    ready.then(function () {
      var counts = {};
      store.students.forEach(function (student) { counts[student['class']] = (counts[student['class']] || 0) + 1; });
      var classes = Object.keys(counts).sort(naturalCompare);
      var existing = batchList().length;
      var size = numberInput(2, 1, 50);
      var start = datetimeInput('');
      var minutes = numberInput(CLASS_PERIOD_MINUTES, 5, 600);
      var gap = numberInput(10, 0, 600);
      var preview = el('ul', 'list batch-list');
      var error = el('p', 'error');
      error.setAttribute('role', 'alert');
      var plan = [];

      function paint() {
        clear(preview);
        plan = [];
        var each = Math.floor(Number(size.value));
        var length = Math.floor(Number(minutes.value));
        var pause = Math.floor(Number(gap.value));
        var first = fromField(start.value);
        if (!classes.length || !(each >= 1) || !(length >= 5) || !(pause >= 0)) {
          preview.appendChild(add(el('li'), el('span', 'hint', classes.length ? '请填写每批班级数和时长' : '导入名单后即可分批')));
          return;
        }
        for (var i = 0; i * each < classes.length; i += 1) {
          var group = classes.slice(i * each, (i + 1) * each);
          var people = 0;
          group.forEach(function (klass) { people += counts[klass]; });
          var opens = first ? addMinutes(first, i * (length + pause)) : '';
          var closes = first ? addMinutes(opens, length) : '';
          plan.push({ start_time: opens, end_time: closes, classes: group });
          add(preview, add(el('li'), add(el('span'),
            el('b', '', '第 ' + (i + 1) + ' 批'),
            el('small', '', first ? batchWindow({ start_time: opens, end_time: closes }) : '填写开放时间后显示时段'),
            el('small', '', group.join('、') + ' · ' + people + ' 人'))));
        }
      }
      [size, start, minutes, gap].forEach(function (input) {
        input.addEventListener('input', paint);
        input.addEventListener('change', paint);
      });
      paint();

      var form = add(el('div', 'editor'),
        add(el('div', 'form-row'), field('每批班级数', size), field('第 1 批开放', start)),
        add(el('div', 'form-row'), field('每批时长（分钟）', minutes), field('两批间隔（分钟）', gap)),
        el('p', 'hint gap', '按班级顺序分批，学校时间' + (existing ? '。存储后替换现有的 ' + existing + ' 批' : '')),
        preview,
        error);
      App.sheet({
        narrow: true,
        title: '快速分批',
        body: form,
        buttons: [
          { label: '取消', onClick: function (controller) { controller.close(); } },
          { label: '存储', kind: 'primary', onClick: function (controller) {
            if (!fromField(start.value)) {
              error.textContent = '请填写第 1 批的开放时间，格式 2026-09-01 08:00';
              return;
            }
            if (!plan.length) {
              error.textContent = '请填写每批班级数和时长';
              return;
            }
            error.textContent = '';
            var button = controller.buttons[1];
            App.busy(button, '存储中');
            saveBatches(true, plan).then(function (result) {
              App.busy(button, null);
              if (!ok(result)) {
                error.textContent = message(result, '请重试');
                return;
              }
              controller.close();
              App.toast('已分成 ' + plan.length + ' 批', 'ok');
              loadSettings();
            });
          } }
        ]
      });
    });
  }

  $('#batch-quick').addEventListener('click', openQuickSplit);

  // ---- 设置：数据 ----
  function renderData(time, summary) {
    var locked = Boolean(time.registration_locked);
    store.summary = summary;
    $('#data-reg').textContent = summary.registered + ' 条报名记录' + (locked ? '' : ' · 锁定名单后可用');
    $('#clear-reg').disabled = !locked || !summary.registered;
    $('#data-stu').textContent = summary.students + ' 名学生及其报名记录';
    $('#delete-students').disabled = !summary.students;
    $('#data-clubs').textContent = summary.clubs + ' 个社团及其报名记录';
    $('#delete-clubs').disabled = !summary.clubs;
  }

  function destructive(button, title, confirmLabel, path, body, done) {
    App.confirm({ title: title, message: '操作前自动保存快照', confirm: confirmLabel, destructive: true }).then(function (yes) {
      if (!yes) return;
      App.busy(button, '处理中');
      retry(path, body).then(function (result) {
        App.busy(button, null);
        App.toast(ok(result) ? done : message(result, '请重试'), ok(result) ? 'ok' : 'err');
        loadSettings();
      });
    });
  }

  $('#clear-reg').addEventListener('click', function () {
    var count = store.summary ? store.summary.registered : 0;
    destructive($('#clear-reg'), '清空 ' + count + ' 条报名？', '清空', '/api/admin/clear_registrations',
      { request_id: App.requestId() }, '已清空报名');
  });
  $('#delete-students').addEventListener('click', function () {
    var count = store.summary ? store.summary.students : 0;
    destructive($('#delete-students'), '删除全部 ' + count + ' 名学生？', '删除', '/api/delete_all_students',
      { confirm: true }, '已删除全部学生');
  });
  $('#delete-clubs').addEventListener('click', function () {
    var count = store.summary ? store.summary.clubs : 0;
    destructive($('#delete-clubs'), '删除全部 ' + count + ' 个社团？', '删除', '/api/delete_all_clubs',
      { confirm: true }, '已删除全部社团');
  });

  // ---- 设置：备份与恢复 ----
  function loadBackups() {
    var version = begin('backups');
    return App.request('/api/admin/backups').then(function (result) {
      if (!latest('backups', version)) return;
      var table = $('#backup-tbl');
      if (!result.data || !Array.isArray(result.data.backups)) {
        tableMessage(table, message(result, '请刷新页面'));
        return;
      }
      $('#backup-keep').textContent = '保留最近 ' + result.data.keep + ' 份';
      clear(table);
      head(table, ['内容', '时间', '大小', '']);
      var body = el('tbody');
      if (!result.data.backups.length) body.appendChild(emptyRow('锁定、调剂、清空或恢复前会自动保存快照', 4));
      result.data.backups.forEach(function (item) {
        var download = el('a', 'btn btn-plain btn-small', '下载');
        download.href = '/api/admin/backup?name=' + encodeURIComponent(item.name);
        var restore = App.button('恢复', 'btn btn-plain btn-small', function () { confirmRestore(item, restore); });
        add(body, add(el('tr', 'backup-row'),
          el('td', '', item.label),
          el('td', 'num', item.time),
          el('td', 'num', fileSize(item.size)),
          add(el('td', 'end'), download, restore)));
      });
      table.appendChild(body);
    });
  }

  function finishRestore(result, button) {
    App.busy(button, null);
    if (!ok(result)) {
      App.toast(message(result, '请重试'), 'err');
      return;
    }
    App.alert({
      title: '已恢复',
      message: '学生 ' + result.data.students + ' 名 · 社团 ' + result.data.clubs + ' 个 · 报名 ' + result.data.registrations + ' 条'
    }).then(function () { window.location.reload(); });
  }

  function confirmRestore(item, button) {
    App.confirm({
      title: '恢复到此快照？',
      message: item.label + ' · ' + item.time + '。恢复前自动保存当前数据，学生需重新登录。',
      confirm: '恢复',
      destructive: true
    }).then(function (yes) {
      if (!yes) return;
      App.busy(button, '恢复中');
      retry('/api/admin/restore', { name: item.name }).then(function (result) { finishRestore(result, button); });
    });
  }

  $('#restore-pick').addEventListener('click', function () { $('#restore-file').click(); });
  $('#restore-file').addEventListener('change', function () {
    var file = takeFile($('#restore-file'));
    if (!file) return;
    if (file.size > MAX_RESTORE) {
      App.toast('请选择 64 MB 以内的文件', 'err');
      return;
    }
    App.confirm({
      title: '从文件恢复？',
      message: file.name + '。恢复前自动保存当前数据，学生需重新登录。',
      confirm: '恢复',
      destructive: true
    }).then(function (yes) {
      if (!yes) return;
      var button = $('#restore-pick');
      App.busy(button, '恢复中');
      App.upload('/api/admin/restore_upload', file, { type: 'application/octet-stream' }).then(function (result) {
        finishRestore(result, button);
      });
    });
  });

  // ---- 设置：管理员密码 ----
  $('#password-form').addEventListener('submit', function (event) {
    event.preventDefault();
    var current = $('#ap-cur').value;
    var next = $('#ap-new').value;
    var error = $('#password-error');
    if (!current) {
      error.textContent = '请填写当前密码';
      return;
    }
    if (next.length < 8) {
      error.textContent = '新密码至少 8 位';
      return;
    }
    error.textContent = '';
    var button = $('#save-password');
    App.busy(button, '存储中');
    post('/api/admin_change_password', { current: current, 'new': next }).then(function (result) {
      App.busy(button, null);
      if (!ok(result)) {
        error.textContent = message(result, '请重试');
        return;
      }
      App.toast('已修改，请重新登录', 'ok');
      setTimeout(function () { window.location.href = '/'; }, 900);
    });
  });

  renderCredentials();
  show(readRoute(), false);
})();
