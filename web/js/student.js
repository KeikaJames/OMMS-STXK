'use strict';

(() => {
  const RETRY_WINDOW_MS = 25000;
  const state = {
    me: null,
    catalog: [],
    catalogTag: null,
    seats: new Map(),
    openAt: null,
    openText: null,
    closeAt: null,
    closeText: null,
    locked: false,
    offset: 0,
    expanded: new Set(),
    pending: null,
    confirmCancel: false,
    renderedPhase: null
  };
  const cards = new Map();
  const now = () => Date.now() + state.offset;

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
    return state.seats.has(club.id) ? state.seats.get(club.id) : club.max_students;
  }
  function eligibility(club) {
    if (!state.me) return null;
    const grades = club.allowed_grades || [];
    const classes = club.allowed_classes || [];
    if (grades.length && !grades.includes(state.me.grade)) return '限' + grades.join('、');
    if (classes.length && !classes.includes(state.me.class)) return '限指定班级';
    return null;
  }
  const teacher = name => (!name ? '' : /老师$/.test(name) ? name : name + ' 老师');
  function splitRoom(name) {
    const match = /^(.*?)（\s*([0-9A-Za-z]+)\s*）\s*$/.exec(name || '');
    return match ? { name: match[1].trim(), room: match[2] + ' 室' } : { name, room: null };
  }
  function clubById(id) {
    return state.catalog.find(club => club.id === id) || null;
  }
  function metaLine(club, info) {
    return [info.room, club.location, club.meeting_time, teacher(club.advisor_name)].filter(Boolean).join(' · ');
  }

  // ---- 数据 ----
  async function loadMe() {
    const result = await App.request('/api/get_student_info');
    if (!result.ok || !result.data) return;
    state.me = result.data;
    $('#greeting').textContent = result.data.name + ' · ' + result.data.class;
  }

  async function syncTime() {
    const sent = Date.now();
    const result = await App.request('/api/check_registration_time');
    if (!result.ok || !result.data) return;
    const rtt = Date.now() - sent;
    if (typeof result.data.server_now === 'number') {
      state.offset = result.data.server_now - (sent + rtt / 2);
    }
    state.openAt = typeof result.data.open_at === 'number' ? result.data.open_at : null;
    state.openText = result.data.start_time || null;
    state.closeAt = typeof result.data.close_at === 'number' ? result.data.close_at : null;
    state.closeText = result.data.end_time || null;
    state.locked = Boolean(result.data.registration_locked);
  }

  async function loadCatalog() {
    const result = await App.request('/api/catalog');
    if (result.ok && Array.isArray(result.data)) state.catalog = result.data;
  }

  async function loadSeats() {
    const result = await App.request('/api/seats');
    if (!result.ok || !result.data) return;
    const data = result.data;
    if (data.catalog !== state.catalogTag || !state.catalog.length) {
      state.catalogTag = data.catalog;
      await loadCatalog();
    }
    state.seats = new Map(data.seats.map(([id, left]) => [id, left]));
    state.openAt = typeof data.open_at === 'number' ? data.open_at : null;
    state.closeAt = typeof data.close_at === 'number' ? data.close_at : null;
    state.closeText = data.end_time || null;
    state.locked = Boolean(data.locked);
    const stamp = new Date();
    $('#updated-at').textContent = '名额更新于 ' + [stamp.getHours(), stamp.getMinutes(), stamp.getSeconds()].map(App.pad).join(':');
  }

  // ---- 轮询节奏：开放时 2 秒，临近开放 4 秒，其余 10–15 秒 ----
  let pollTimer = null;
  let openTimer = null;
  function pollDelay() {
    const current = phase();
    if (current === 'open') return 2000 + Math.random() * 800;
    if (current === 'countdown') return state.openAt - now() < 90000 ? 4000 : 15000;
    return current === 'chosen' ? 10000 : 15000;
  }
  function schedulePoll() {
    clearTimeout(pollTimer);
    if (document.hidden) return;
    pollTimer = setTimeout(async () => {
      await loadSeats();
      render();
      schedulePoll();
    }, pollDelay());
  }
  function scheduleOpen() {
    clearTimeout(openTimer);
    if (state.openAt === null || state.locked) return;
    const wait = state.openAt - now();
    if (wait <= 0 || wait > 86400000) return;
    openTimer = setTimeout(() => {
      render();
      loadSeats().then(render);
    }, wait + 30);
  }

  // ---- 状态条 ----
  function formatDuration(ms) {
    const seconds = Math.max(0, Math.ceil(ms / 1000));
    const days = Math.floor(seconds / 86400);
    const clock = [Math.floor(seconds % 86400 / 3600), Math.floor(seconds % 3600 / 60), seconds % 60].map(App.pad).join(':');
    return days ? days + ' 天 ' + clock : clock;
  }
  function formatClock(ms) {
    const date = new Date(ms);
    return [date.getHours(), date.getMinutes(), date.getSeconds()].map(App.pad).join(':');
  }
  // 开放与截止时间按学校时区的原始文本显示，与设备时区无关。
  function formatSchoolTime(text) {
    const match = /^(\d{4})-(\d{2})-(\d{2}) (\d{2}):(\d{2})/.exec(text || '');
    return match ? Number(match[2]) + ' 月 ' + Number(match[3]) + ' 日 ' + match[4] + ':' + match[5] : '';
  }

  function renderStatus() {
    const current = phase();
    const openText = formatSchoolTime(state.openText);
    const closeText = formatSchoolTime(state.closeText);
    const until = closeText ? '截止时间 ' + closeText : '';
    // 已报名时社团名称和退选按钮在下方的“已报名”区域，状态条只写状态。
    const texts = {
      unset: ['开放时间待定', ''],
      countdown: ['距开放', [openText ? '开放时间 ' + openText : '', until].filter(Boolean).join('，')],
      open: ['报名中', ['每人限报一个社团', until].filter(Boolean).join('，')],
      chosen: ['已报名', until],
      locked: ['报名已截止', registeredId() ? '' : '未报名的学生由学校统一调剂']
    };
    const [title, detail] = texts[current];
    $('#status').dataset.phase = current;
    $('#status-title').textContent = title;
    $('#status-detail').textContent = detail;
    $('#status-detail').hidden = !detail;
    tick();
  }

  function tick() {
    const current = phase();
    if (current !== state.renderedPhase) {
      state.renderedPhase = current;
      render();
      return;
    }
    if (current === 'countdown') {
      $('#clock-label').textContent = '倒计时';
      $('#clock').textContent = formatDuration(state.openAt - now());
    } else {
      $('#clock-label').textContent = '当前时间';
      $('#clock').textContent = formatClock(now());
    }
  }

  // ---- 已报名 ----
  function renderMine() {
    const id = registeredId();
    const box = $('#mine');
    if (!id) {
      box.hidden = true;
      state.confirmCancel = false;
      delete $('#mine-actions').dataset.key;
      return;
    }
    const club = clubById(id);
    const info = splitRoom(club ? club.name : state.me.registered_club);
    box.hidden = false;
    $('#mine-name').textContent = info.name;
    const meta = club ? metaLine(club, info) : '';
    $('#mine-meta').textContent = meta;
    $('#mine-meta').hidden = !meta;
    const actions = $('#mine-actions');
    const key = state.locked ? 'locked' : state.pending && state.pending.kind === 'cancel'
      ? 'busy:' + state.pending.label : state.confirmCancel ? 'confirm' : 'idle';
    if (actions.dataset.key === key) return;
    actions.dataset.key = key;
    actions.replaceChildren();
    if (state.locked) return;
    if (state.pending && state.pending.kind === 'cancel') {
      const busy = document.createElement('button');
      busy.className = 'btn btn-secondary btn-sm';
      busy.type = 'button';
      actions.append(busy);
      App.busy(busy, state.pending.label);
      return;
    }
    if (state.confirmCancel) {
      actions.append(
        button('取消', 'btn btn-quiet btn-sm', () => { state.confirmCancel = false; renderMine(); }),
        button('确认退选', 'btn btn-danger btn-sm', cancelRegistration)
      );
    } else {
      actions.append(button('退选', 'btn btn-secondary btn-sm', () => { state.confirmCancel = true; renderMine(); }));
    }
  }

  function button(label, className, onClick) {
    const element = document.createElement('button');
    element.type = 'button';
    element.className = className;
    element.textContent = label;
    element.addEventListener('click', onClick);
    return element;
  }

  // ---- 社团卡片(按 id 复用节点) ----
  function createCard(club) {
    const card = document.createElement('article');
    card.className = 'club';
    card.dataset.id = club.id;
    card.innerHTML =
      '<div class="club-art"></div>' +
      '<div class="club-body">' +
      '<h3 class="club-name"></h3><p class="club-meta"></p><p class="club-desc"></p>' +
      '<button class="more" type="button" hidden>展开</button>' +
      '<div class="club-tags"></div>' +
      '<div class="seats"><span>剩余名额</span><b class="num"></b><span class="seats-total"></span></div>' +
      '<div class="meter"><i></i></div>' +
      '<div class="club-action"></div></div>';
    card.querySelector('.more').addEventListener('click', () => {
      if (state.expanded.has(club.id)) state.expanded.delete(club.id);
      else state.expanded.add(club.id);
      renderClubs();
    });
    return card;
  }

  function renderArt(card, club) {
    const art = card.querySelector('.club-art');
    const key = ClubArt.key(club);
    if (art.dataset.key === key) return;
    art.dataset.key = key;
    art.dataset.tone = String((Number(club.id) % 5) + 1);
    art.replaceChildren(ClubArt.node(club));
  }

  // 每 2 秒轮询一次；卡片内容没变时不碰 DOM，旧电脑上滚动和输入不卡。
  function updateCard(card, club) {
    const left = remaining(club);
    const mine = registeredId() === club.id;
    const restriction = eligibility(club);
    const next = actionFor(club, left, restriction, mine);
    const signature = [club.name, club.location, club.meeting_time, club.advisor_name, club.description,
      club.max_students, (club.allowed_grades || []).join(','), (club.allowed_classes || []).length,
      left, mine, restriction, next.key, state.expanded.has(club.id)].join('|');
    if (card.dataset.signature === signature) return;
    card.dataset.signature = signature;
    const info = splitRoom(club.name);
    renderArt(card, club);
    card.classList.toggle('is-mine', mine);
    card.querySelector('.club-name').textContent = info.name;
    const meta = card.querySelector('.club-meta');
    meta.textContent = metaLine(club, info);
    meta.hidden = !meta.textContent;
    const desc = card.querySelector('.club-desc');
    desc.textContent = club.description || '';
    desc.hidden = !club.description;
    desc.classList.toggle('is-open', state.expanded.has(club.id));
    const more = card.querySelector('.more');
    more.hidden = !club.description || club.description.length < 48;
    more.textContent = state.expanded.has(club.id) ? '收起' : '展开';

    const tags = card.querySelector('.club-tags');
    tags.replaceChildren();
    if (restriction) tags.append(tag(restriction, 'tag-warn'));
    tags.hidden = !tags.childElementCount;

    card.querySelector('.seats b').textContent = String(left);
    card.querySelector('.seats-total').textContent = '/ ' + club.max_students;
    const meter = card.querySelector('.meter');
    meter.classList.toggle('low', left > 0 && left <= Math.max(3, club.max_students * .1));
    meter.querySelector('i').style.width = (club.max_students ? Math.round(left / club.max_students * 100) : 0) + '%';

    const action = card.querySelector('.club-action');
    if (action.dataset.key === next.key) return;
    action.dataset.key = next.key;
    action.replaceChildren();
    card.classList.toggle('is-off', Boolean(next.off));
    if (next.busy) {
      const busy = document.createElement('button');
      busy.className = 'btn btn-primary';
      busy.type = 'button';
      action.append(busy);
      App.busy(busy, next.busy);
    } else if (next.register) {
      action.append(button('报名', 'btn btn-primary', () => register(club)));
    } else {
      const label = document.createElement('span');
      label.className = 'club-state';
      label.textContent = next.text;
      action.append(label);
    }
  }

  function tag(text, className) {
    const element = document.createElement('span');
    element.className = 'tag ' + className;
    element.textContent = text;
    return element;
  }

  function actionFor(club, left, restriction, mine) {
    const current = phase();
    if (state.pending && state.pending.kind === 'register' && state.pending.clubId === club.id) {
      return { key: 'busy:' + state.pending.label, busy: state.pending.label };
    }
    if (mine) return { key: 'mine', text: '已报名' };
    if (state.locked) return { key: 'locked', text: '已截止', off: true };
    if (registeredId()) return { key: 'other', text: '已报名其他社团', off: true };
    if (!club.enabled) return { key: 'disabled', text: '暂停报名', off: true };
    if (restriction) return { key: 'restricted', text: restriction, off: true };
    if (club.seat_sync_pending) return { key: 'syncing', text: '名额更新中' };
    if (left <= 0) return { key: 'full', text: '已满', off: true };
    if (current !== 'open') return { key: 'waiting', text: '待开放' };
    if (state.pending) return { key: 'waiting-other', text: '处理中' };
    return { key: 'register', register: true };
  }

  function renderClubs() {
    const grid = $('#clubs');
    if (grid.getAttribute('aria-busy') !== 'false') grid.setAttribute('aria-busy', 'false');
    grid.querySelectorAll('.sk, .empty').forEach(node => node.remove());
    const ordered = state.catalog.slice().sort((a, b) => a.id - b.id);
    const live = new Set(ordered.map(club => club.id));
    for (const [id, card] of cards) {
      if (!live.has(id)) {
        card.remove();
        cards.delete(id);
      }
    }
    let position = 0;
    ordered.forEach(club => {
      let card = cards.get(club.id);
      if (!card) {
        card = createCard(club);
        cards.set(club.id, card);
      }
      updateCard(card, club);
      const current = grid.children[position];
      if (current !== card) grid.insertBefore(card, current || null);
      position += 1;
    });
    if (grid.dataset.count !== String(ordered.length)) grid.dataset.count = String(ordered.length);
    if (!ordered.length) {
      const empty = document.createElement('p');
      empty.className = 'empty';
      empty.textContent = '社团名单待发布';
      grid.append(empty);
    }
  }

  function render() {
    state.renderedPhase = phase();
    renderStatus();
    renderMine();
    renderClubs();
  }

  // ---- 报名与退选：同一个 request_id 在排队或确认中自动重试 ----
  async function register(club) {
    const pending = { kind: 'register', clubId: club.id, requestId: App.requestId(), started: Date.now(), label: '提交中' };
    state.pending = pending;
    render();
    while (state.pending === pending) {
      const result = await App.request('/api/register_club', {
        method: 'POST',
        body: { club_id: club.id, request_id: pending.requestId }
      });
      if (state.pending !== pending) return;
      if (result.status === 200 && result.data) {
        state.pending = null;
        await settle(result.data, club);
        return;
      }
      if (result.status === 0 || result.status === 202 || result.status === 429 || result.status >= 500) {
        if (Date.now() - pending.started > RETRY_WINDOW_MS) {
          state.pending = null;
          App.toast('提交未完成，请重新报名', 'err');
          await refreshAll();
          return;
        }
        pending.label = result.status === 202 ? '确认中' : '排队中';
        render();
        await new Promise(resolve => setTimeout(resolve, result.retryAfter * 1000 + Math.random() * 500));
        continue;
      }
      state.pending = null;
      App.toast((result.data && result.data.message) || '提交未完成，请刷新页面', 'err');
      await refreshAll();
      return;
    }
  }

  async function settle(data, club) {
    App.toast(data.message, data.success ? 'ok' : 'err');
    await refreshAll();
    if (data.success) {
      const card = cards.get(club.id);
      if (card) {
        card.classList.add('stamp');
        setTimeout(() => card.classList.remove('stamp'), 600);
      }
    }
  }

  async function cancelRegistration() {
    const pending = { kind: 'cancel', started: Date.now(), label: '退选中' };
    state.pending = pending;
    state.confirmCancel = false;
    render();
    while (state.pending === pending) {
      const result = await App.request('/api/cancel_registration', { method: 'POST', body: {} });
      if (result.status === 200 && result.data) {
        state.pending = null;
        const done = result.data.success || result.data.code === 'not_registered';
        App.toast(done ? '已退选' : result.data.message, done ? 'ok' : 'err');
        await refreshAll();
        return;
      }
      if ((result.status === 0 || result.status === 202 || result.status >= 500) && Date.now() - pending.started < RETRY_WINDOW_MS) {
        pending.label = '确认中';
        render();
        await new Promise(resolve => setTimeout(resolve, result.retryAfter * 1000 + Math.random() * 500));
        continue;
      }
      state.pending = null;
      App.toast((result.data && result.data.message) || '退选未完成，请重试', 'err');
      await refreshAll();
      return;
    }
  }

  async function refreshAll() {
    await Promise.all([loadMe(), loadSeats()]);
    render();
    schedulePoll();
    // 名额快照有不到 1 秒的缓存，稍后再取一次以显示本次操作后的余量。
    setTimeout(() => loadSeats().then(render), 1500);
  }

  // ---- 启动 ----
  document.addEventListener('visibilitychange', () => {
    if (document.hidden) {
      clearTimeout(pollTimer);
      return;
    }
    Promise.all([syncTime(), loadMe()]).then(loadSeats).then(() => {
      render();
      scheduleOpen();
      schedulePoll();
    });
  });

  async function init() {
    await Promise.all([loadMe(), syncTime()]);
    await loadSeats();
    if (!state.catalog.length) await loadCatalog();
    render();
    scheduleOpen();
    schedulePoll();
    setInterval(() => { if (!document.hidden) tick(); }, 1000);
    setInterval(() => {
      if (!document.hidden) syncTime().then(scheduleOpen);
    }, 60000);
  }
  init();
})();
