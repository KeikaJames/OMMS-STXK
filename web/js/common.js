'use strict';

// 各页面共用：选择器、请求、提示、请求 ID 与退出登录。
if (!Element.prototype.replaceChildren) {
  Element.prototype.replaceChildren = function (...nodes) {
    this.textContent = '';
    this.append(...nodes);
  };
}
const $ = selector => document.querySelector(selector);
const $$ = selector => Array.from(document.querySelectorAll(selector));

const App = {
  async request(path, options = {}) {
    // Chrome 72、Safari 12.1 以前的 fetch 默认不带 Cookie，这里显式声明。
    const init = Object.assign({ cache: 'no-cache', credentials: 'same-origin' }, options);
    if (init.body && typeof init.body !== 'string' && !(init.body instanceof Blob)) {
      init.body = JSON.stringify(init.body);
      init.headers = Object.assign({ 'Content-Type': 'application/json' }, init.headers || {});
    }
    let response;
    try {
      response = await fetch(path, init);
    } catch (error) {
      return { status: 0, ok: false, data: null, retryAfter: 2 };
    }
    let data = null;
    try { data = await response.json(); } catch (error) { data = null; }
    if (response.status === 401 && !path.endsWith('login')) {
      location.href = '/';
    }
    const retry = parseInt(response.headers.get('Retry-After') || '', 10);
    return {
      status: response.status,
      ok: response.ok,
      data,
      retryAfter: Number.isFinite(retry) && retry > 0 ? Math.min(retry, 30) : 1
    };
  },

  toast(message, kind = '') {
    const box = $('#toasts');
    if (!box || !message) return;
    const item = document.createElement('div');
    item.className = 'toast' + (kind ? ' ' + kind : '');
    item.setAttribute('role', kind === 'err' ? 'alert' : 'status');
    item.textContent = message;
    box.appendChild(item);
    setTimeout(() => item.remove(), kind === 'err' ? 5200 : 3200);
  },

  requestId() {
    const bytes = new Uint8Array(12);
    crypto.getRandomValues(bytes);
    return Array.from(bytes, byte => byte.toString(16).padStart(2, '0')).join('');
  },

  busy(button, label) {
    if (label) {
      button.dataset.label = button.dataset.label || button.textContent;
      button.disabled = true;
      button.replaceChildren();
      const spinner = document.createElement('span');
      spinner.className = 'spin';
      spinner.setAttribute('aria-hidden', 'true');
      button.append(spinner, document.createTextNode(label));
    } else {
      button.disabled = false;
      button.textContent = button.dataset.label || button.textContent;
      delete button.dataset.label;
    }
  },

  pad: value => String(value).padStart(2, '0'),

  bindLogout() {
    const button = $('#logout');
    if (!button) return;
    button.addEventListener('click', async () => {
      await App.request('/api/logout', { method: 'POST', body: {} });
      location.href = '/';
    });
  }
};

document.addEventListener('DOMContentLoaded', () => App.bindLogout());
