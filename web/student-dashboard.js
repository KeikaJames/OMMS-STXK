'use strict';

// Presentation controller for the student page. Registration truth always
// comes from the same-origin API; imagery and motion never decide state.
const $ = selector => document.querySelector(selector);

const UI = Object.freeze({
  clubArt: [
    '/img/editorial-study.jpg',
    '/img/editorial-telescope.jpg',
    '/img/editorial-bird.jpg'
  ],
  stateArt: Object.freeze({
    loading: '/img/editorial-desk.jpg',
    idle: '/img/editorial-desk.jpg',
    countdown: '/img/editorial-telescope.jpg',
    open: '/img/editorial-grassland.jpg',
    reconciling: '/img/editorial-magnet.jpg',
    selected: '/img/editorial-bird.jpg'
  }),
  pollingMinMs: 4000,
  pollingJitterMs: 2500,
  timeRefreshMs: 30000,
  profileRefreshMs: 60000,
  reconcileDelaysMs: [1000, 2000, 4000, 8000]
});

let me = null;
let canRegister = false;
let openAt = null;
let inflight = false;
let backoffUntil = 0;
let lastTimeRefresh = 0;
let lastProfileRefresh = 0;
let reconcileTimer = null;
let reconcileGeneration = 0;
let reconcileTarget = null;
let lastClubs = [];
let confirmingCancelFor = null;
let activeMediaState = 'loading';
let mediaSwapGeneration = 0;

const clubNodes = new Map();
const reducedMotion = window.matchMedia('(prefers-reduced-motion: reduce)');

function toast(message, kind = '') {
  const item = document.createElement('div');
  item.className = 'toast' + (kind ? ' ' + kind : '');
  item.setAttribute('role', kind === 'err' ? 'alert' : 'status');
  item.textContent = message;
  $('#toasts').appendChild(item);
  window.setTimeout(() => item.remove(), kind === 'err' ? 5200 : 3600);
}

async function api(path, options) {
  const response = await fetch(path, Object.assign({
    headers: {'Content-Type': 'application/json'}
  }, options));
  if (response.status === 401) {
    location.href = '/';
    throw new Error('unauth');
  }
  return response;
}

function splitRoom(name) {
  const match = name.match(/^(.*?)（\s*([0-9A-Za-z]+)\s*）\s*$/);
  return match
    ? {name: match[1].trim(), room: match[2] + ' 室'}
    : {name, room: null};
}

function formatTime(date) {
  const pad = value => String(value).padStart(2, '0');
  return pad(date.getHours()) + ':' + pad(date.getMinutes()) + ':' + pad(date.getSeconds());
}

function tickClock() {
  const now = new Date();
  const pad = value => String(value).padStart(2, '0');
  const label = $('#clock-label');
  const clock = $('#clock');

  if (canRegister) {
    label.textContent = '当前时间';
    clock.innerHTML = '<span class="clk-now">现在</span>' + formatTime(now);
    return;
  }

  if (openAt === null) {
    label.textContent = '当前时间';
    clock.textContent = formatTime(now);
    return;
  }

  label.textContent = '距开放';
  let seconds = Math.max(0, Math.round((openAt - now.getTime()) / 1000));
  const hours = Math.floor(seconds / 3600);
  const minutes = Math.floor((seconds % 3600) / 60);
  seconds %= 60;
  clock.textContent = (hours > 0 ? pad(hours) + ':' : '') + pad(minutes) + ':' + pad(seconds);
}

function deriveViewState() {
  if (reconcileTarget) return 'reconciling';
  if (me && me.registered_club) return 'selected';
  if (canRegister) return 'open';
  if (openAt !== null) return 'countdown';
  return 'idle';
}

function setStateMedia(state) {
  if (state === activeMediaState) return;
  const media = $('#state-media');
  const image = $('#state-image');
  const source = UI.stateArt[state] || UI.stateArt.idle;
  const generation = ++mediaSwapGeneration;
  const preload = new Image();

  const commit = () => {
    if (generation !== mediaSwapGeneration) return;
    const swap = () => {
      image.src = source;
      media.dataset.state = state;
      activeMediaState = state;
      requestAnimationFrame(() => media.classList.remove('is-changing'));
    };
    if (reducedMotion.matches) swap();
    else window.setTimeout(swap, 110);
  };

  if (!reducedMotion.matches) media.classList.add('is-changing');
  preload.onload = commit;
  preload.onerror = commit;
  preload.src = source;
}

function renderStatus() {
  const state = deriveViewState();
  const bar = $('#statusbar');
  const statusText = $('#status-text');
  const detail = $('#status-detail');
  const sync = $('#sync-state');
  bar.className = 'status ' + state;

  if (state === 'reconciling') {
    statusText.textContent = '正在确认结果';
    detail.textContent = '请不要重复提交，页面会自动读取最终状态';
  } else if (state === 'selected') {
    const selected = splitRoom(me.registered_club);
    statusText.textContent = '已完成选择';
    detail.textContent = selected.name + (selected.room ? ' · ' + selected.room : '');
  } else if (state === 'open') {
    statusText.textContent = '开抢进行中';
    detail.textContent = '现在可以选择下方任一项目';
  } else if (state === 'countdown') {
    statusText.textContent = '等待开放';
    detail.textContent = '到达开放时间后，报名按钮会自动启用';
  } else {
    statusText.textContent = '尚未设置开放时间';
    detail.textContent = '请等待老师设置本次选课时间';
  }

  sync.hidden = state !== 'reconciling';
  setStateMedia(state);
  tickClock();
}

function stableArtIndex(club) {
  const numericId = Number(club.id);
  if (Number.isInteger(numericId) && numericId > 0) {
    return (numericId - 1) % UI.clubArt.length;
  }
  const key = String(club.id ?? club.name ?? '');
  let hash = 0;
  for (let index = 0; index < key.length; index += 1) {
    hash = ((hash << 5) - hash + key.charCodeAt(index)) | 0;
  }
  return Math.abs(hash) % UI.clubArt.length;
}

function fallbackArt(club) {
  return UI.clubArt[stableArtIndex(club)];
}

function safeProjectImage(club) {
  const value = club.image_path || club.image_url;
  if (typeof value !== 'string') return null;
  try {
    const url = new URL(value, location.origin);
    return url.origin === location.origin && url.pathname.startsWith('/club-images/')
      ? url.pathname + url.search
      : null;
  } catch (error) {
    return null;
  }
}

function protectMedia(media) {
  media.addEventListener('contextmenu', event => event.preventDefault());
  media.addEventListener('dragstart', event => event.preventDefault());
  media.addEventListener('copy', event => event.preventDefault());
}

function createClubCard(club) {
  const key = String(club.id ?? club.name);
  const safeId = key.replace(/[^0-9A-Za-z_-]/g, '-');
  const card = document.createElement('article');
  card.dataset.clubKey = key;
  card.setAttribute('aria-labelledby', 'club-title-' + safeId);

  const media = document.createElement('div');
  media.className = 'club-media';
  const picture = document.createElement('picture');
  const stillSource = document.createElement('source');
  stillSource.media = '(prefers-reduced-motion: reduce)';
  const image = document.createElement('img');
  image.alt = '';
  image.draggable = false;
  image.loading = 'lazy';
  image.decoding = 'async';
  image.width = 1400;
  image.height = 787;
  image.addEventListener('error', () => {
    const fallback = image.dataset.fallback;
    if (fallback && image.getAttribute('src') !== fallback) image.src = fallback;
  });
  picture.append(stillSource, image);
  const indexLabel = document.createElement('span');
  indexLabel.className = 'club-index num';
  indexLabel.setAttribute('aria-hidden', 'true');
  media.append(picture, indexLabel);
  protectMedia(media);

  const content = document.createElement('div');
  content.className = 'club-content';
  const top = document.createElement('div');
  top.className = 'club-top';
  const name = document.createElement('h3');
  name.className = 'club-name';
  name.id = 'club-title-' + safeId;
  const nameText = document.createElement('span');
  nameText.className = 'club-label';
  const room = document.createElement('span');
  room.className = 'rm';
  name.append(nameText, room);
  top.appendChild(name);

  const seats = document.createElement('div');
  seats.className = 'seats';
  const count = document.createElement('span');
  count.className = 'n';
  const unit = document.createElement('span');
  unit.className = 'u';
  seats.append(count, unit);

  const capacity = document.createElement('div');
  capacity.className = 'bar';
  capacity.setAttribute('role', 'progressbar');
  const fill = document.createElement('i');
  capacity.appendChild(fill);

  const action = document.createElement('div');
  action.className = 'act';
  content.append(top, seats, capacity, action);
  card.append(media, content);
  return card;
}

function actionState(label) {
  const state = document.createElement('span');
  state.className = 'state';
  state.textContent = label;
  return state;
}

function renderAction(card, club, mode) {
  const action = card.querySelector('.act, .confirm');
  if (action.dataset.mode === mode) return;
  action.dataset.mode = mode;
  action.replaceChildren();

  if (mode === 'confirm') {
    action.className = 'confirm';
    const yes = document.createElement('button');
    yes.className = 'btn btn-secondary btn-sm btn-danger-text';
    yes.type = 'button';
    yes.textContent = '确认退选';
    yes.onclick = () => doCancel(yes);
    const no = document.createElement('button');
    no.className = 'btn btn-primary btn-sm';
    no.type = 'button';
    no.textContent = '保留选择';
    no.onclick = () => {
      confirmingCancelFor = null;
      renderClubs(lastClubs);
    };
    action.append(yes, no);
    return;
  }

  action.className = 'act';
  if (mode === 'mine') {
    const badge = document.createElement('span');
    badge.className = 'badge-ok grow';
    badge.textContent = '你已选择';
    const cancel = document.createElement('button');
    cancel.className = 'btn btn-secondary btn-sm';
    cancel.type = 'button';
    cancel.textContent = '退选';
    cancel.onclick = () => {
      confirmingCancelFor = String(club.id ?? club.name);
      renderClubs(lastClubs);
    };
    action.append(badge, cancel);
  } else if (mode === 'other') {
    action.appendChild(actionState('已选其他项目'));
  } else if (mode === 'full') {
    action.appendChild(actionState('已满'));
  } else if (mode === 'not-started') {
    action.appendChild(actionState('未开始'));
  } else {
    const register = document.createElement('button');
    register.className = 'btn btn-primary btn-sm';
    register.type = 'button';
    register.textContent = '报名';
    register.onclick = () => doRegister(register, club.id);
    action.appendChild(register);
  }
}

function updateClubCard(card, club, mine, index) {
  const isMine = Boolean(mine && mine === club.name);
  const full = club.current_students >= club.max_students;
  const remaining = Math.max(0, club.max_students - club.current_students);
  const urgent = !full && remaining <= 3;
  const remainingPercent = club.max_students
    ? Math.round((remaining / club.max_students) * 100)
    : 0;
  const info = splitRoom(club.name);
  const key = String(club.id ?? club.name);
  const fallback = fallbackArt(club);
  const source = safeProjectImage(club) || fallback;
  const image = card.querySelector('.club-media img');
  const stillSource = card.querySelector('.club-media source');

  image.dataset.fallback = fallback;
  stillSource.srcset = fallback;
  if (image.dataset.source !== source) {
    image.dataset.source = source;
    image.src = source;
  }
  card.className = 'club' + (isMine ? ' mine' : '') + (full ? ' full' : '') + (urgent ? ' urgent' : '');
  card.querySelector('.club-index').textContent = String(index + 1).padStart(2, '0');
  card.querySelector('.club-label').textContent = info.name;
  const room = card.querySelector('.rm');
  room.textContent = info.room || '';
  room.hidden = !info.room;

  const count = card.querySelector('.n');
  count.className = 'n' + (full ? ' gone' : (urgent ? ' urgent' : ''));
  count.textContent = String(remaining);
  card.querySelector('.u').textContent = full
    ? '已满 · 共 ' + club.max_students
    : '个剩余名额 · 共 ' + club.max_students;

  const capacity = card.querySelector('.bar');
  const level = full ? '' : (urgent ? 'urgent' : (remaining <= 6 ? 'warm' : ''));
  capacity.className = 'bar' + (level ? ' ' + level : '');
  capacity.setAttribute('aria-valuemin', '0');
  capacity.setAttribute('aria-valuemax', String(club.max_students));
  capacity.setAttribute('aria-valuenow', String(remaining));
  capacity.setAttribute('aria-label', '剩余 ' + remaining + ' 个名额，共 ' + club.max_students + ' 个');
  capacity.querySelector('i').style.width = (full ? 0 : remainingPercent) + '%';

  let mode = 'register';
  if (isMine && confirmingCancelFor === key) mode = 'confirm';
  else if (isMine) mode = 'mine';
  else if (mine) mode = 'other';
  else if (full) mode = 'full';
  else if (!canRegister) mode = 'not-started';
  renderAction(card, club, mode);
}

function clearClubNodes() {
  clubNodes.forEach(node => node.remove());
  clubNodes.clear();
}

function renderClubs(clubs) {
  lastClubs = Array.isArray(clubs) ? clubs : [];
  const box = $('#clubs');
  const mine = me && me.registered_club;
  $('#project-count').textContent = String(lastClubs.length);
  $('#hint').textContent = mine
    ? '已选 1 / 1'
    : (canRegister ? '请选择 1 个项目' : '开放后可选择 1 个项目');
  box.setAttribute('aria-busy', 'false');

  if (!lastClubs.length) {
    clearClubNodes();
    box.replaceChildren();
    const empty = document.createElement('div');
    empty.className = 'empty';
    empty.textContent = '暂无选修项目';
    box.appendChild(empty);
    return;
  }

  if (box.querySelector('.empty, .sk')) box.replaceChildren();
  const liveKeys = new Set();
  lastClubs.forEach((club, index) => {
    const key = String(club.id ?? club.name);
    liveKeys.add(key);
    let card = clubNodes.get(key);
    if (!card) {
      card = createClubCard(club);
      clubNodes.set(key, card);
    }
    updateClubCard(card, club, mine, index);
    box.appendChild(card);
  });

  for (const [key, node] of clubNodes) {
    if (!liveKeys.has(key)) {
      node.remove();
      clubNodes.delete(key);
    }
  }
}

function updateFreshness() {
  $('#updated-at').textContent = '最近更新 ' + formatTime(new Date());
}

async function refresh({profile = false} = {}) {
  try {
    const needTime = openAt === null || Date.now() - lastTimeRefresh >= UI.timeRefreshMs;
    const needProfile = profile || me === null || Date.now() - lastProfileRefresh >= UI.profileRefreshMs;
    const [clubsResponse, timeResponse, studentResponse] = await Promise.all([
      api('/api/get_clubs'),
      needTime ? api('/api/check_registration_time') : Promise.resolve(null),
      needProfile ? api('/api/get_student_info') : Promise.resolve(null)
    ]);

    const clubs = await clubsResponse.json();
    if (timeResponse) {
      const time = await timeResponse.json();
      canRegister = Boolean(time.can_register);
      openAt = time.start_time
        ? new Date(time.start_time.replace(/-/g, '/')).getTime()
        : null;
      lastTimeRefresh = Date.now();
    } else if (!canRegister && openAt !== null && Date.now() >= openAt) {
      canRegister = true;
    }

    if (studentResponse && studentResponse.ok) {
      const profileData = await studentResponse.json();
      if (profileData && typeof profileData.student_id !== 'undefined') {
        me = profileData;
        lastProfileRefresh = Date.now();
      }
    }

    renderStatus();
    renderClubs(clubs);
    updateFreshness();
  } catch (error) {
    if (error.message !== 'unauth') console.error(error);
  }
}

function busy(button, on) {
  if (on) {
    button.dataset.txt = button.textContent;
    button.disabled = true;
    button.innerHTML = '<span class="spin" aria-hidden="true"></span><span>处理中</span>';
  } else {
    button.disabled = false;
    button.textContent = button.dataset.txt || button.textContent;
  }
}

function finishReconciliation(generation) {
  if (generation !== reconcileGeneration) return;
  reconcileTarget = null;
  reconcileTimer = null;
  renderStatus();
  renderClubs(lastClubs);
}

function reconciliationResolved() {
  if (reconcileTarget === 'registered') return Boolean(me && me.registered_club);
  if (reconcileTarget === 'cleared') return Boolean(me && !me.registered_club);
  return false;
}

function reconcileFinalState(target) {
  const generation = ++reconcileGeneration;
  if (reconcileTimer) clearTimeout(reconcileTimer);
  const delays = UI.reconcileDelaysMs;
  let round = 0;
  reconcileTarget = target;
  backoffUntil = Math.max(backoffUntil, Date.now() + delays.reduce((sum, delay) => sum + delay, 0));
  renderStatus();
  renderClubs(lastClubs);

  const step = () => {
    lastProfileRefresh = 0;
    refresh({profile: true}).finally(() => {
      if (generation !== reconcileGeneration) return;
      if (reconciliationResolved()) {
        finishReconciliation(generation);
        return;
      }
      if (round < delays.length) {
        reconcileTimer = setTimeout(step, delays[round++]);
      } else {
        finishReconciliation(generation);
        toast('最终结果仍在确认，请稍后查看个人信息', 'err');
      }
    });
  };
  reconcileTimer = setTimeout(step, delays[round++]);
}

async function guardedWrite(button, operation) {
  if (inflight) return;
  if (Date.now() < backoffUntil) {
    toast('系统繁忙，请稍候重试', 'err');
    return;
  }
  inflight = true;
  busy(button, true);
  try {
    await operation();
  } catch (error) {
    if (error.message !== 'unauth') {
      console.error(error);
      toast('网络连接异常，请稍后重试', 'err');
    }
  } finally {
    inflight = false;
    busy(button, false);
  }
}

function doRegister(button, clubId) {
  guardedWrite(button, async () => {
    const response = await fetch('/api/register_club', {
      method: 'POST',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({club_id: clubId})
    });
    if (response.status === 401) {
      location.href = '/';
      return;
    }
    if (response.status === 503 || response.status === 429) {
      const retryAfter = parseInt(response.headers.get('Retry-After') || '2', 10);
      const delay = Number.isNaN(retryAfter) ? 2 : retryAfter;
      backoffUntil = Math.max(backoffUntil, Date.now() + delay * 1000);
      toast('请求已提交，正在确认最终报名状态', 'pending');
      lastProfileRefresh = 0;
      reconcileFinalState('registered');
      return;
    }
    const data = await response.json();
    toast(data.message || (data.success ? '报名成功' : '报名失败'), data.success ? 'ok' : 'err');
    await refresh({profile: true});
  });
}

function doCancel(button) {
  guardedWrite(button, async () => {
    const response = await api('/api/cancel_registration', {method: 'POST', body: '{}'});
    if (response.status === 503 || response.status === 429) {
      const retryAfter = parseInt(response.headers.get('Retry-After') || '2', 10);
      const delay = Number.isNaN(retryAfter) ? 2 : retryAfter;
      backoffUntil = Math.max(backoffUntil, Date.now() + delay * 1000);
      let data = null;
      try { data = await response.json(); } catch (error) { /* response body is optional */ }
      toast((data && data.message) || '退选请求已提交，正在确认最终状态', 'pending');
      lastProfileRefresh = 0;
      confirmingCancelFor = null;
      reconcileFinalState('cleared');
      return;
    }
    const data = await response.json();
    confirmingCancelFor = null;
    toast(data.message || (data.success ? '已退选' : '退选失败'), data.success ? 'ok' : 'err');
    await refresh({profile: true});
  });
}

$('#logout').onclick = async () => {
  try {
    await fetch('/api/logout', {
      method: 'POST',
      headers: {'Content-Type': 'application/json'},
      body: '{}'
    });
  } catch (error) {
    // Logout remains local even if the network disappears.
  }
  location.href = '/';
};

protectMedia($('#state-media'));
setInterval(tickClock, 1000);
window.addEventListener('focus', () => refresh({profile: true}));

if ('requestIdleCallback' in window) {
  requestIdleCallback(() => {
    Object.values(UI.stateArt).forEach(source => {
      const image = new Image();
      image.src = source;
    });
  });
}

(function poll() {
  refresh().finally(() => {
    const delay = UI.pollingMinMs + Math.floor(Math.random() * UI.pollingJitterMs);
    setTimeout(poll, delay);
  });
})();
