// 学生页：开放状态、名额轮询、报名与退选。只用 ES5。
(function () {
  'use strict';
  var RETRY_WINDOW_MS = 25000;
  var state = {
    me: null,
    catalog: [],
    catalogTag: null,
    seats: {},
    openAt: null,
    openText: null,
    closeAt: null,
    closeText: null,
    locked: false,
    offset: 0,
    expanded: {},
    pending: null,
    renderedPhase: null
  };
  var cards = {};
  var pollTimer = null;
  var openTimer = null;

  function now() { return Date.now() + state.offset; }
  function wait(ms) { return new Promise(function (resolve) { setTimeout(resolve, ms); }); }
  function has(object, key) { return Object.prototype.hasOwnProperty.call(object, key); }

  // ---- 状态 ----
  function registeredId() {
    return state.me && state.me.registered_club_id ? Number(state.me.registered_club_id) : null;
  }
  function phase() {
    if (state.locked || (state.closeAt !== null && now() >= state.closeAt)) return 'locked';
    if (registeredId()) return 'chosen';
    if (state.openAt === null) return 'unset';
    return now() >= state.openAt ? 'open' : 'countdown';
  }
  function remaining(club) {
    return has(state.seats, club.id) ? state.seats[club.id] : club.max_students;
  }
  function eligibility(club) {
    if (!state.me) return null;
    var grades = club.allowed_grades || [];
    var classes = club.allowed_classes || [];
    if (grades.length && grades.indexOf(state.me.grade) < 0) return '限' + grades.join('、');
    if (classes.length && classes.indexOf(state.me['class']) < 0) return '限指定班级';
    return null;
  }
  function teacher(name) {
    if (!name) return '';
    return /老师$/.test(name) ? name : name + ' 老师';
  }
  function splitRoom(name) {
    var match = /^(.*?)（\s*([0-9A-Za-z]+)\s*）\s*$/.exec(name || '');
    return match ? { name: match[1].replace(/^\s+|\s+$/g, ''), room: match[2] + ' 室' } : { name: name, room: null };
  }
  function clubById(id) {
    for (var i = 0; i < state.catalog.length; i += 1) if (state.catalog[i].id === id) return state.catalog[i];
    return null;
  }
  function metaLine(club, info) {
    return [info.room, club.location, club.meeting_time, teacher(club.advisor_name)].filter(Boolean).join(' · ');
  }

  // ---- 数据 ----
  function loadMe() {
    return App.request('/api/get_student_info').then(function (result) {
      if (!result.ok || !result.data) return;
      state.me = result.data;
      $('#greeting').textContent = result.data.name + ' · ' + result.data['class'];
    });
  }

  function syncTime() {
    var sent = Date.now();
    return App.request('/api/check_registration_time').then(function (result) {
      if (!result.ok || !result.data) return;
      var data = result.data;
      var rtt = Date.now() - sent;
      if (typeof data.server_now === 'number') state.offset = data.server_now - (sent + rtt / 2);
      state.openAt = typeof data.open_at === 'number' ? data.open_at : null;
      state.openText = data.start_time || null;
      state.closeAt = typeof data.close_at === 'number' ? data.close_at : null;
      state.closeText = data.end_time || null;
      state.locked = Boolean(data.registration_locked);
    });
  }

  function loadCatalog() {
    return App.request('/api/catalog').then(function (result) {
      if (result.ok && Array.isArray(result.data)) state.catalog = result.data;
    });
  }

  function loadSeats() {
    return App.request('/api/seats').then(function (result) {
      if (!result.ok || !result.data) return null;
      var data = result.data;
      var seats = {};
      (data.seats || []).forEach(function (pair) { seats[pair[0]] = pair[1]; });
      state.seats = seats;
      state.openAt = typeof data.open_at === 'number' ? data.open_at : null;
      state.closeAt = typeof data.close_at === 'number' ? data.close_at : null;
      state.closeText = data.end_time || null;
      state.locked = Boolean(data.locked);
      var stamp = new Date();
      $('#updated-at').textContent = '更新于 ' + [stamp.getHours(), stamp.getMinutes(), stamp.getSeconds()].map(App.pad).join(':');
      if (data.catalog !== state.catalogTag || !state.catalog.length) {
        state.catalogTag = data.catalog;
        return loadCatalog();
      }
      return null;
    });
  }

  // ---- 轮询：开放时 2 秒，临近开放 4 秒，其余 10–15 秒；页面在后台时暂停 ----
  function pollDelay() {
    var current = phase();
    if (current === 'open') return 2000 + Math.random() * 800;
    if (current === 'countdown') return state.openAt - now() < 90000 ? 4000 : 15000;
    return current === 'chosen' ? 10000 : 15000;
  }
  function schedulePoll() {
    clearTimeout(pollTimer);
    if (App.hidden()) return;
    pollTimer = setTimeout(function () {
      loadSeats().then(function () {
        render();
        schedulePoll();
      });
    }, pollDelay());
  }
  function scheduleOpen() {
    clearTimeout(openTimer);
    if (state.openAt === null || state.locked) return;
    var left = state.openAt - now();
    if (left <= 0 || left > 86400000) return;
    openTimer = setTimeout(function () {
      render();
      loadSeats().then(render);
    }, left + 30);
  }

  // ---- 状态条 ----
  function formatDuration(ms) {
    var seconds = Math.max(0, Math.ceil(ms / 1000));
    var days = Math.floor(seconds / 86400);
    var clock = [Math.floor(seconds % 86400 / 3600), Math.floor(seconds % 3600 / 60), seconds % 60].map(App.pad).join(':');
    return days ? days + ' 天 ' + clock : clock;
  }
  function formatClock(ms) {
    var date = new Date(ms);
    return [date.getHours(), date.getMinutes(), date.getSeconds()].map(App.pad).join(':');
  }

  function renderStatus() {
    var current = phase();
    var openText = App.schoolTime(state.openText);
    var closeText = App.schoolTime(state.closeText);
    var until = closeText ? closeText + ' 截止' : '';
    var texts = {
      unset: ['开放时间待定', ''],
      countdown: ['距开放', [openText ? openText + ' 开放' : '', until].filter(Boolean).join(' · ')],
      open: ['报名中', until],
      chosen: ['已报名', until],
      locked: ['报名已截止', '']
    };
    var text = texts[current];
    var detail = $('#status-detail');
    $('#status').setAttribute('data-phase', current);
    $('#status-title').textContent = text[0];
    detail.textContent = text[1];
    detail.hidden = !text[1];
    tick();
  }

  function tick() {
    var current = phase();
    if (current !== state.renderedPhase) {
      render();
      return;
    }
    var label = current === 'countdown' ? '倒计时' : '当前时间';
    var labelNode = $('#clock-label');
    if (labelNode.textContent !== label) labelNode.textContent = label;
    $('#clock').textContent = current === 'countdown' ? formatDuration(state.openAt - now()) : formatClock(now());
  }

  // ---- 已报名 ----
  function renderMine() {
    var id = registeredId();
    var box = $('#mine');
    var actions = $('#mine-actions');
    if (!id) {
      box.hidden = true;
      actions.removeAttribute('data-key');
      return;
    }
    var club = clubById(id);
    var info = splitRoom(club ? club.name : state.me.registered_club);
    var meta = club ? metaLine(club, info) : '';
    box.hidden = false;
    $('#mine-name').textContent = info.name;
    $('#mine-meta').textContent = meta;
    $('#mine-meta').hidden = !meta;
    var cancelling = state.pending && state.pending.kind === 'cancel';
    var key = phase() === 'locked' ? 'locked' : cancelling ? 'busy:' + state.pending.label : 'idle:' + info.name;
    if (actions.getAttribute('data-key') === key) return;
    actions.setAttribute('data-key', key);
    App.clear(actions);
    if (key === 'locked') return;
    var node = App.button('退选', 'btn btn-destructive btn-small', function () { confirmCancel(info.name); });
    actions.appendChild(node);
    if (cancelling) App.busy(node, state.pending.label);
  }

  function confirmCancel(name) {
    if (state.pending) return;
    App.confirm({ title: '退选“' + name + '”？', confirm: '退选', destructive: true }).then(function (yes) {
      if (yes) cancelRegistration();
    });
  }

  // ---- 社团卡片：按 id 复用节点，内容没变时不改 DOM ----
  function createCard(club) {
    var card = App.el('article', 'club');
    card.setAttribute('data-id', club.id);
    card.innerHTML =
      '<div class="club-art"></div>' +
      '<div class="club-body">' +
      '<h3 class="club-name"></h3><p class="club-meta"></p><p class="club-desc"></p>' +
      '<button class="more" type="button" aria-expanded="false" hidden>展开</button>' +
      '<div class="club-tags" hidden></div>' +
      '<div class="seats"><span>剩余名额</span><b class="num"></b><span class="seats-total"></span></div>' +
      '<div class="meter" aria-hidden="true"><i></i></div>' +
      '<div class="club-action"></div></div>';
    $('.more', card).addEventListener('click', function () {
      state.expanded[club.id] = !state.expanded[club.id];
      renderClubs();
    });
    return card;
  }

  function renderArt(card, club) {
    var art = $('.club-art', card);
    var key = ClubArt.key(club);
    if (art.getAttribute('data-key') === key) return;
    art.setAttribute('data-key', key);
    art.setAttribute('data-tone', String((Number(club.id) % 5) + 1));
    App.clear(art);
    art.appendChild(ClubArt.node(club));
  }

  function actionFor(club, left, restriction, mine) {
    var current = phase();
    if (state.pending && state.pending.kind === 'register' && state.pending.clubId === club.id) {
      return { key: 'busy:' + state.pending.label, busy: state.pending.label };
    }
    if (mine) return { key: 'mine', text: '已报名' };
    if (current === 'locked') return { key: 'locked', text: '已截止', off: true };
    if (registeredId()) return { key: 'other', text: '已报名其他社团', off: true };
    if (!club.enabled) return { key: 'disabled', text: '暂停报名', off: true };
    if (restriction) return { key: 'restricted', text: restriction, off: true };
    if (club.seat_sync_pending) return { key: 'syncing', text: '名额更新中' };
    if (left <= 0) return { key: 'full', text: '已满', off: true };
    if (current !== 'open') return { key: 'waiting', text: '待开放' };
    if (state.pending) return { key: 'waiting-other', text: '处理中' };
    return { key: 'register', register: true };
  }

  function updateCard(card, club) {
    var left = remaining(club);
    var mine = registeredId() === club.id;
    var restriction = eligibility(club);
    var next = actionFor(club, left, restriction, mine);
    var expanded = Boolean(state.expanded[club.id]);
    var signature = [club.name, club.location, club.meeting_time, club.advisor_name, club.description,
      club.image_path, club.max_students, (club.allowed_grades || []).join(','), (club.allowed_classes || []).length,
      left, mine, restriction, next.key, expanded].join('|');
    if (card.getAttribute('data-signature') === signature) return;
    card.setAttribute('data-signature', signature);

    var info = splitRoom(club.name);
    renderArt(card, club);
    App.toggleClass(card, 'is-mine', mine);
    App.toggleClass(card, 'is-off', Boolean(next.off));
    $('.club-name', card).textContent = info.name;
    var meta = $('.club-meta', card);
    meta.textContent = metaLine(club, info);
    meta.hidden = !meta.textContent;
    var desc = $('.club-desc', card);
    desc.textContent = club.description || '';
    desc.hidden = !club.description;
    App.toggleClass(desc, 'is-open', expanded);
    var more = $('.more', card);
    more.hidden = !club.description || club.description.length < 60;
    more.textContent = expanded ? '收起' : '展开';
    more.setAttribute('aria-expanded', String(expanded));

    var tags = $('.club-tags', card);
    App.clear(tags);
    if (restriction) tags.appendChild(App.el('span', 'tag tag-warn', restriction));
    tags.hidden = !restriction;

    $('.seats b', card).textContent = String(left);
    $('.seats-total', card).textContent = '/ ' + club.max_students;
    var meter = $('.meter', card);
    App.toggleClass(meter, 'low', left > 0 && left <= Math.max(3, club.max_students * 0.1));
    $('i', meter).style.width = (club.max_students ? Math.round(left / club.max_students * 100) : 0) + '%';

    var action = $('.club-action', card);
    if (action.getAttribute('data-key') === next.key) return;
    action.setAttribute('data-key', next.key);
    App.clear(action);
    if (next.busy) {
      var busy = App.button('', 'btn btn-primary');
      action.appendChild(busy);
      App.busy(busy, next.busy);
    } else if (next.register) {
      action.appendChild(App.button('报名', 'btn btn-primary', function () { register(club); }));
    } else {
      action.appendChild(App.el('span', 'club-state', next.text));
    }
  }

  function renderClubs() {
    var grid = $('#clubs');
    if (grid.getAttribute('aria-busy') !== 'false') grid.setAttribute('aria-busy', 'false');
    $$('.sk, .empty', grid).forEach(function (node) { grid.removeChild(node); });
    var ordered = state.catalog.slice().sort(function (a, b) { return a.id - b.id; });
    var live = {};
    ordered.forEach(function (club) { live[club.id] = true; });
    Object.keys(cards).forEach(function (id) {
      if (live[id]) return;
      if (cards[id].parentNode) cards[id].parentNode.removeChild(cards[id]);
      delete cards[id];
    });
    ordered.forEach(function (club, position) {
      var card = cards[club.id];
      if (!card) {
        card = createCard(club);
        cards[club.id] = card;
      }
      updateCard(card, club);
      var current = grid.children[position];
      if (current !== card) grid.insertBefore(card, current || null);
    });
    if (!ordered.length) grid.appendChild(App.el('p', 'empty', '社团名单待发布'));
  }

  function render() {
    state.renderedPhase = phase();
    renderStatus();
    renderMine();
    renderClubs();
  }

  // ---- 报名与退选：同一个 request_id 在排队或确认中自动重试 ----
  function register(club) {
    if (state.pending) return null;
    var pending = { kind: 'register', clubId: club.id, requestId: App.requestId(), started: Date.now(), label: '提交中' };
    state.pending = pending;
    render();
    function attempt() {
      return App.request('/api/register_club', {
        method: 'POST',
        body: { club_id: club.id, request_id: pending.requestId }
      }).then(function (result) {
        if (state.pending !== pending) return null;
        if (result.status === 200 && result.data) {
          state.pending = null;
          return settle(result.data, club);
        }
        if (result.status === 0 || result.status === 202 || result.status === 429 || result.status >= 500) {
          if (Date.now() - pending.started > RETRY_WINDOW_MS) {
            state.pending = null;
            App.toast('提交未完成', 'err');
            return refreshAll();
          }
          pending.label = result.status === 202 ? '确认中' : '排队中';
          render();
          return wait(result.retryAfter * 1000 + Math.random() * 500).then(attempt);
        }
        state.pending = null;
        App.toast((result.data && result.data.message) || '提交未完成', 'err');
        return refreshAll();
      });
    }
    return attempt();
  }

  function settle(data, club) {
    App.toast(data.message, data.success ? 'ok' : 'err');
    return refreshAll().then(function () {
      var card = cards[club.id];
      if (!data.success || !card) return;
      card.classList.add('stamp');
      setTimeout(function () { card.classList.remove('stamp'); }, 600);
    });
  }

  function cancelRegistration() {
    if (state.pending) return null;
    var pending = { kind: 'cancel', started: Date.now(), label: '退选中' };
    state.pending = pending;
    render();
    function attempt() {
      return App.request('/api/cancel_registration', { method: 'POST', body: {} }).then(function (result) {
        if (state.pending !== pending) return null;
        if (result.status === 200 && result.data) {
          state.pending = null;
          var done = result.data.success || result.data.code === 'not_registered';
          App.toast(done ? '已退选' : result.data.message, done ? 'ok' : 'err');
          return refreshAll();
        }
        if ((result.status === 0 || result.status === 202 || result.status >= 500) && Date.now() - pending.started < RETRY_WINDOW_MS) {
          pending.label = '确认中';
          render();
          return wait(result.retryAfter * 1000 + Math.random() * 500).then(attempt);
        }
        state.pending = null;
        App.toast((result.data && result.data.message) || '退选未完成', 'err');
        return refreshAll();
      });
    }
    return attempt();
  }

  // 名额快照有不到 1 秒的缓存，稍后再取一次以显示本次操作后的余量。
  function refreshAll() {
    return Promise.all([loadMe(), loadSeats()]).then(function () {
      render();
      schedulePoll();
      setTimeout(function () { loadSeats().then(render); }, 1500);
    });
  }

  // ---- 启动 ----
  App.onVisible(function () {
    if (App.hidden()) {
      clearTimeout(pollTimer);
      return;
    }
    Promise.all([syncTime(), loadMe()]).then(loadSeats).then(function () {
      render();
      scheduleOpen();
      schedulePoll();
    });
  });

  Promise.all([loadMe(), syncTime()]).then(loadSeats).then(function () {
    return state.catalog.length ? null : loadCatalog();
  }).then(function () {
    render();
    scheduleOpen();
    schedulePoll();
    setInterval(function () { if (!App.hidden()) tick(); }, 1000);
    setInterval(function () { if (!App.hidden()) syncTime().then(scheduleOpen); }, 60000);
  });
})();
