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
    locked: false,
    offset: 0,
    filter: 'all',
    query: '',
    favorites: new Set(),
    expanded: new Set(),
    pending: null,
    confirmCancel: false,
    renderedPhase: null
  };
  const cards = new Map();
  const now = () => Date.now() + state.offset;

  // ---- 收藏(只存在本机) ----
  const favoriteKey = () => 'omms:fav:' + (state.me ? state.me.student_id : '');
  function loadFavorites() {
    try { state.favorites = new Set(JSON.parse(localStorage.getItem(favoriteKey()) || '[]')); }
    catch (error) { state.favorites = new Set(); }
  }
  function saveFavorites() {
    try { localStorage.setItem(favoriteKey(), JSON.stringify(Array.from(state.favorites))); }
    catch (error) { /* 浏览器禁止存储时收藏只在本页有效 */ }
  }

  // ---- 状态 ----
  function registeredId() {
    return state.me && state.me.registered_club_id ? Number(state.me.registered_club_id) : null;
  }
  function phase() {
    if (state.locked) return 'locked';
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
    if (grades.length && !grades.includes(state.me.grade)) return '面向' + grades.join('、');
    if (classes.length && !classes.includes(state.me.class)) return '面向指定班级';
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

  // ---- 数据 ----
  async function loadMe() {
    const result = await App.request('/api/get_student_info');
    if (!result.ok || !result.data) return;
    const first = !state.me;
    state.me = result.data;
    if (first) loadFavorites();
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
  // 开放时间按学校时区的原始文本显示，与设备时区无关。
  function formatOpenAt() {
    const match = /^(\d{4})-(\d{2})-(\d{2}) (\d{2}):(\d{2})/.exec(state.openText || '');
    return match ? Number(match[2]) + ' 月 ' + Number(match[3]) + ' 日 ' + match[4] + ':' + match[5] : '';
  }

  function renderStatus() {
    const current = phase();
    const mine = clubById(registeredId());
    const texts = {
      unset: ['报名时间待定', '老师设置后，这里会显示倒计时'],
      countdown: ['距离开放', formatOpenAt() ? '开放时间 ' + formatOpenAt() + '，按钮会自动亮起' : '到点后按钮会自动亮起'],
      open: ['报名进行中', '每人一个，选好后点「报名」'],
      chosen: ['已选好', (mine ? splitRoom(mine.name).name : (state.me && state.me.registered_club) || '') + ' · 退选后可以重新选择'],
      locked: ['名单已锁定', '报名和退选已结束']
    };
    const [title, detail] = texts[current];
    $('#status').dataset.phase = current;
    $('#status-title').textContent = title;
    $('#status-detail').textContent = detail;
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

  // ---- 我的社团 ----
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
    const meta = [info.room, club && club.meeting_time, club && club.location, club && club.advisor_name].filter(Boolean);
    $('#mine-meta').textContent = meta.join(' · ') || '报名时间 ' + (state.me.registration_time || '').slice(0, 16);
    const actions = $('#mine-actions');
    const key = state.locked ? 'locked' : state.pending && state.pending.kind === 'cancel'
      ? 'busy:' + state.pending.label : state.confirmCancel ? 'confirm' : 'idle';
    if (actions.dataset.key === key) return;
    actions.dataset.key = key;
    actions.replaceChildren();
    if (state.locked) return;
    if (state.pending && state.pending.kind === 'cancel') {
      const button = document.createElement('button');
      button.className = 'btn btn-secondary btn-sm';
      button.type = 'button';
      actions.append(button);
      App.busy(button, state.pending.label);
      return;
    }
    if (state.confirmCancel) {
      const confirm = button('确认退选', 'btn btn-danger btn-sm', cancelRegistration);
      const keep = button('保留', 'btn btn-quiet btn-sm', () => { state.confirmCancel = false; renderMine(); });
      actions.append(keep, confirm);
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
      '<div class="club-art"><button class="fav" type="button" aria-pressed="false" aria-label="收藏">☆</button></div>' +
      '<div class="club-body">' +
      '<h3 class="club-name"></h3><p class="club-meta"></p><p class="club-desc"></p>' +
      '<button class="more" type="button" hidden>展开简介</button>' +
      '<div class="club-tags"></div>' +
      '<div class="seats"><b class="num"></b><span></span></div><div class="meter"><i></i></div>' +
      '<div class="club-action"></div></div>';
    card.querySelector('.fav').addEventListener('click', () => {
      if (state.favorites.has(club.id)) state.favorites.delete(club.id);
      else state.favorites.add(club.id);
      saveFavorites();
      renderClubs();
    });
    card.querySelector('.more').addEventListener('click', () => {
      if (state.expanded.has(club.id)) state.expanded.delete(club.id);
      else state.expanded.add(club.id);
      renderClubs();
    });
    return card;
  }

  function renderArt(card, club) {
    const art = card.querySelector('.club-art');
    const source = typeof club.image_path === 'string' && club.image_path.startsWith('/club-images/') ? club.image_path : null;
    if (art.dataset.source === String(source)) return;
    art.dataset.source = String(source);
    art.querySelectorAll('img, .tile-char').forEach(node => node.remove());
    art.dataset.tone = String((Number(club.id) % 5) + 1);
    if (source) {
      const image = document.createElement('img');
      image.src = source;
      image.alt = '';
      image.loading = 'lazy';
      image.decoding = 'async';
      art.prepend(image);
    } else {
      const char = document.createElement('span');
      char.className = 'tile-char';
      char.setAttribute('aria-hidden', 'true');
      char.textContent = Array.from(splitRoom(club.name).name || '社')[0];
      art.prepend(char);
    }
  }

  function updateCard(card, club) {
    const info = splitRoom(club.name);
    const left = remaining(club);
    const mine = registeredId() === club.id;
    renderArt(card, club);
    card.classList.toggle('is-mine', mine);
    const fav = card.querySelector('.fav');
    fav.setAttribute('aria-pressed', String(state.favorites.has(club.id)));
    fav.textContent = state.favorites.has(club.id) ? '★' : '☆';
    card.querySelector('.club-name').textContent = info.name;
    card.querySelector('.club-meta').textContent =
      [info.room, club.location, club.meeting_time, teacher(club.advisor_name)].filter(Boolean).join(' · ') || '时间地点待定';
    const desc = card.querySelector('.club-desc');
    desc.textContent = club.description || '';
    desc.hidden = !club.description;
    desc.classList.toggle('is-open', state.expanded.has(club.id));
    const more = card.querySelector('.more');
    more.hidden = !club.description || club.description.length < 48;
    more.textContent = state.expanded.has(club.id) ? '收起' : '展开简介';

    const tags = card.querySelector('.club-tags');
    tags.replaceChildren();
    const restriction = eligibility(club);
    const grades = club.allowed_grades || [];
    if (grades.length) tags.append(tag('面向' + grades.join('、'), restriction ? 'tag-warn' : 'tag'));
    if ((club.allowed_classes || []).length) tags.append(tag('指定班级', restriction ? 'tag-warn' : 'tag'));
    if (mine) tags.append(tag('我的选择', 'tag-accent'));
    tags.hidden = !tags.childElementCount;

    card.querySelector('.seats b').textContent = String(left);
    card.querySelector('.seats span').textContent = left > 0 ? '个名额剩余 · 共 ' + club.max_students : '名额已满 · 共 ' + club.max_students;
    const meter = card.querySelector('.meter');
    meter.classList.toggle('low', left > 0 && left <= Math.max(3, club.max_students * .1));
    meter.querySelector('i').style.width = (club.max_students ? Math.round(left / club.max_students * 100) : 0) + '%';

    const action = card.querySelector('.club-action');
    const next = actionFor(club, left, restriction, mine);
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
    if (mine) return { key: 'mine', text: '已选择' };
    if (state.locked) return { key: 'locked', text: '名单已锁定', off: true };
    if (registeredId()) return { key: 'other', text: '已选其他社团', off: true };
    if (!club.enabled) return { key: 'disabled', text: '暂停报名', off: true };
    if (restriction) return { key: 'restricted', text: restriction, off: true };
    if (club.seat_sync_pending) return { key: 'syncing', text: '名额更新中' };
    if (left <= 0) return { key: 'full', text: '已满', off: true };
    if (current !== 'open') return { key: 'waiting', text: current === 'countdown' ? '开放后可报名' : '等待开放' };
    if (state.pending) return { key: 'waiting-other', text: '等待上一项完成' };
    return { key: 'register', register: true };
  }

  function visible(club) {
    if (state.query) {
      const haystack = [club.name, club.advisor_name, club.location, club.meeting_time, club.description].join(' ').toLowerCase();
      if (!haystack.includes(state.query)) return false;
    }
    if (state.filter === 'eligible') return club.enabled && !eligibility(club);
    if (state.filter === 'open') return club.enabled && remaining(club) > 0;
    if (state.filter === 'fav') return state.favorites.has(club.id);
    return true;
  }

  function renderClubs() {
    const grid = $('#clubs');
    grid.setAttribute('aria-busy', 'false');
    grid.querySelectorAll('.sk, .empty').forEach(node => node.remove());
    const ordered = state.catalog
      .filter(visible)
      .sort((a, b) => Number(state.favorites.has(b.id)) - Number(state.favorites.has(a.id)) || a.id - b.id);
    const live = new Set(ordered.map(club => club.id));
    for (const [id, card] of cards) {
      if (!live.has(id)) {
        card.remove();
        if (!clubById(id)) cards.delete(id);
      }
    }
    ordered.forEach(club => {
      let card = cards.get(club.id);
      if (!card) {
        card = createCard(club);
        cards.set(club.id, card);
      }
      updateCard(card, club);
      grid.append(card);
    });
    if (!ordered.length) {
      const empty = document.createElement('p');
      empty.className = 'empty';
      empty.textContent = state.catalog.length ? '没有符合条件的社团' : '社团名单正在准备中';
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
          App.toast('网络较忙，请稍后再点一次', 'err');
          await refreshAll();
          return;
        }
        pending.label = result.status === 202 ? '确认中' : '排队中';
        render();
        await new Promise(resolve => setTimeout(resolve, result.retryAfter * 1000 + Math.random() * 500));
        continue;
      }
      state.pending = null;
      App.toast((result.data && result.data.message) || '请求未完成，请刷新后重试', 'err');
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
        App.toast(done ? '已退选，可以重新选择' : result.data.message, done ? 'ok' : 'err');
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
      App.toast((result.data && result.data.message) || '退选未完成，请稍后再试', 'err');
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

  // ---- 交互与启动 ----
  $('#search').addEventListener('input', event => {
    state.query = event.target.value.trim().toLowerCase();
    renderClubs();
  });
  $$('.chip').forEach(chip => {
    chip.addEventListener('click', () => {
      state.filter = chip.dataset.filter;
      $$('.chip').forEach(other => other.setAttribute('aria-pressed', String(other === chip)));
      renderClubs();
    });
  });
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
