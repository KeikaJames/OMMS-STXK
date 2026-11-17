// 各页面共用：兼容补丁、元素工具、请求、下载、对话框、提示。只用 ES5，IE11 与旧版 Chrome 可直接运行。
(function (window, document) {
  'use strict';

  // ---- 兼容补丁 ----
  if (typeof window.Promise !== 'function') {
    var Deferred = function (executor) {
      var self = this;
      self.state = 0;
      self.value = undefined;
      self.handlers = [];
      function settle(state, value) {
        if (self.state !== 0) return;
        self.state = state;
        self.value = value;
        setTimeout(function () {
          var waiting = self.handlers;
          self.handlers = [];
          for (var i = 0; i < waiting.length; i += 1) self.run(waiting[i]);
        }, 0);
      }
      function resolve(value) {
        if (value === self) return settle(2, new TypeError('promise cycle'));
        if (value && (typeof value === 'object' || typeof value === 'function')) {
          var then;
          try { then = value.then; } catch (error) { return settle(2, error); }
          if (typeof then === 'function') {
            var called = false;
            try {
              then.call(value, function (next) {
                if (!called) { called = true; resolve(next); }
              }, function (reason) {
                if (!called) { called = true; settle(2, reason); }
              });
            } catch (error) {
              if (!called) { called = true; settle(2, error); }
            }
            return undefined;
          }
        }
        return settle(1, value);
      }
      self.run = function (handler) {
        var callback = self.state === 1 ? handler.fulfilled : handler.rejected;
        if (typeof callback !== 'function') {
          if (self.state === 1) handler.resolve(self.value); else handler.reject(self.value);
          return;
        }
        var result;
        try { result = callback(self.value); } catch (error) { handler.reject(error); return; }
        handler.resolve(result);
      };
      try {
        executor(resolve, function (reason) { settle(2, reason); });
      } catch (error) {
        settle(2, error);
      }
    };
    Deferred.prototype.then = function (fulfilled, rejected) {
      var self = this;
      return new Deferred(function (resolve, reject) {
        var handler = { fulfilled: fulfilled, rejected: rejected, resolve: resolve, reject: reject };
        if (self.state === 0) self.handlers.push(handler);
        else setTimeout(function () { self.run(handler); }, 0);
      });
    };
    Deferred.prototype['catch'] = function (rejected) { return this.then(null, rejected); };
    Deferred.resolve = function (value) { return new Deferred(function (resolve) { resolve(value); }); };
    Deferred.reject = function (reason) { return new Deferred(function (resolve, reject) { reject(reason); }); };
    Deferred.all = function (items) {
      return new Deferred(function (resolve, reject) {
        var results = [];
        var left = items.length;
        if (!left) { resolve(results); return; }
        for (var i = 0; i < items.length; i += 1) {
          (function (index) {
            Deferred.resolve(items[index]).then(function (value) {
              results[index] = value;
              left -= 1;
              if (!left) resolve(results);
            }, reject);
          })(i);
        }
      });
    };
    window.Promise = Deferred;
  }
  if (typeof Object.assign !== 'function') {
    Object.assign = function (target) {
      for (var i = 1; i < arguments.length; i += 1) {
        var source = arguments[i];
        if (source) for (var key in source) if (Object.prototype.hasOwnProperty.call(source, key)) target[key] = source[key];
      }
      return target;
    };
  }
  if (!Array.prototype.find) {
    Array.prototype.find = function (test) {
      for (var i = 0; i < this.length; i += 1) if (test(this[i], i, this)) return this[i];
      return undefined;
    };
  }
  if (!Array.prototype.findIndex) {
    Array.prototype.findIndex = function (test) {
      for (var i = 0; i < this.length; i += 1) if (test(this[i], i, this)) return i;
      return -1;
    };
  }
  if (!Element.prototype.matches) {
    Element.prototype.matches = Element.prototype.msMatchesSelector || Element.prototype.webkitMatchesSelector;
  }
  if (!Element.prototype.closest) {
    Element.prototype.closest = function (selector) {
      var node = this;
      while (node && node.nodeType === 1) {
        if (node.matches(selector)) return node;
        node = node.parentNode;
      }
      return null;
    };
  }
  if (window.NodeList && !NodeList.prototype.forEach) NodeList.prototype.forEach = Array.prototype.forEach;

  // ---- 元素工具 ----
  function $(selector, root) { return (root || document).querySelector(selector); }
  function $$(selector, root) { return Array.prototype.slice.call((root || document).querySelectorAll(selector)); }
  function el(tag, className, text) {
    var node = document.createElement(tag);
    if (className) node.className = className;
    if (text !== undefined && text !== null) node.textContent = String(text);
    return node;
  }
  function add(parent) {
    for (var i = 1; i < arguments.length; i += 1) {
      var child = arguments[i];
      if (child === null || child === undefined || child === false) continue;
      parent.appendChild(typeof child === 'string' ? document.createTextNode(child) : child);
    }
    return parent;
  }
  function clear(node) {
    while (node && node.firstChild) node.removeChild(node.firstChild);
    return node;
  }
  function button(label, className, onClick) {
    var node = el('button', className, label);
    node.type = 'button';
    if (onClick) node.addEventListener('click', onClick);
    return node;
  }
  function toggleClass(node, name, on) {
    if (on) node.classList.add(name); else node.classList.remove(name);
  }
  function hidden() {
    return Boolean(document.hidden || document.msHidden);
  }

  // ---- 请求 ----
  function headerFilename(xhr) {
    var value = '';
    try { value = xhr.getResponseHeader('Content-Disposition') || ''; } catch (error) { value = ''; }
    var encoded = /filename\*=UTF-8''([^;]+)/i.exec(value);
    if (encoded) {
      try { return decodeURIComponent(encoded[1]); } catch (error) { return ''; }
    }
    var plain = /filename="?([^";]+)"?/i.exec(value);
    return plain ? plain[1] : '';
  }

  function request(path, options) {
    options = options || {};
    return new Promise(function (resolve) {
      var xhr = new XMLHttpRequest();
      var method = options.method || 'GET';
      var body = options.body;
      var done = false;
      function result(status, data, blob) {
        var retry = 1;
        try { retry = parseInt(xhr.getResponseHeader('Retry-After') || '', 10); } catch (error) { retry = 1; }
        if (status === 401 && !/login$/.test(path)) window.location.href = '/';
        resolve({
          status: status,
          ok: status >= 200 && status < 300,
          data: data,
          blob: blob,
          filename: blob ? headerFilename(xhr) : '',
          retryAfter: retry > 0 ? Math.min(retry, 30) : 1
        });
      }
      function finish() {
        if (done) return;
        done = true;
        var status = xhr.status || 0;
        if (options.blob) {
          if (status >= 200 && status < 300) { result(status, null, xhr.response); return; }
          if (!xhr.response || !window.FileReader) { result(status, null, null); return; }
          var reader = new FileReader();
          reader.onload = function () {
            var parsed = null;
            try { parsed = JSON.parse(reader.result); } catch (error) { parsed = null; }
            result(status, parsed, null);
          };
          reader.onerror = function () { result(status, null, null); };
          reader.readAsText(xhr.response);
          return;
        }
        var data = null;
        try { data = xhr.responseText ? JSON.parse(xhr.responseText) : null; } catch (error) { data = null; }
        result(status, data, null);
      }
      try {
        xhr.open(method, path, true);
        xhr.setRequestHeader('X-Requested-With', 'XMLHttpRequest');
        if (method === 'GET') {
          xhr.setRequestHeader('Cache-Control', 'no-cache');
          xhr.setRequestHeader('If-Modified-Since', 'Thu, 01 Jan 1970 00:00:00 GMT');
        }
        if (body !== undefined && body !== null && typeof body !== 'string' && !(window.Blob && body instanceof window.Blob)) {
          body = JSON.stringify(body);
          xhr.setRequestHeader('Content-Type', 'application/json');
        }
        var headers = options.headers || {};
        for (var name in headers) if (Object.prototype.hasOwnProperty.call(headers, name)) xhr.setRequestHeader(name, headers[name]);
        if (options.blob) xhr.responseType = 'blob';
        xhr.timeout = options.timeout || 60000;
        xhr.onreadystatechange = function () { if (xhr.readyState === 4) setTimeout(finish, 0); };
        xhr.ontimeout = function () { done = true; result(0, null, null); };
        xhr.send(body === undefined ? null : body);
      } catch (error) {
        done = true;
        result(0, null, null);
      }
    });
  }

  function upload(path, file, options) {
    var headers = { 'Content-Type': (options && options.type) || file.type || 'application/octet-stream' };
    if (file.name) headers['X-File-Name'] = encodeURIComponent(file.name);
    return request(path, { method: 'POST', body: file, headers: headers, timeout: 180000, blob: options && options.blob });
  }

  function saveBlob(blob, filename) {
    if (window.navigator && window.navigator.msSaveOrOpenBlob) {
      window.navigator.msSaveOrOpenBlob(blob, filename);
      return;
    }
    var url = window.URL.createObjectURL(blob);
    var link = el('a');
    link.href = url;
    link.download = filename;
    document.body.appendChild(link);
    link.click();
    document.body.removeChild(link);
    setTimeout(function () { window.URL.revokeObjectURL(url); }, 4000);
  }

  // ---- 提示 ----
  function toast(message, kind) {
    var box = $('#toasts');
    if (!box || !message) return;
    var item = el('div', 'toast' + (kind === 'err' ? ' err' : ''), message);
    item.setAttribute('role', kind === 'err' ? 'alert' : 'status');
    box.appendChild(item);
    setTimeout(function () { if (item.parentNode) item.parentNode.removeChild(item); }, kind === 'err' ? 5200 : 3200);
  }

  function busy(node, label) {
    if (!node) return;
    if (label) {
      if (!node.getAttribute('data-label')) node.setAttribute('data-label', node.textContent);
      node.disabled = true;
      clear(node);
      add(node, el('span', 'spinner'), label);
    } else if (node.getAttribute('data-label') !== null) {
      node.disabled = false;
      node.textContent = node.getAttribute('data-label');
      node.removeAttribute('data-label');
    }
  }

  function requestId() {
    var alphabet = 'ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789';
    var crypto = window.crypto || window.msCrypto;
    var values = [];
    if (crypto && crypto.getRandomValues) {
      var bytes = new Uint8Array(24);
      crypto.getRandomValues(bytes);
      for (var i = 0; i < bytes.length; i += 1) values.push(alphabet.charAt(bytes[i] % alphabet.length));
    } else {
      for (var j = 0; j < 24; j += 1) values.push(alphabet.charAt(Math.floor(Math.random() * alphabet.length)));
    }
    return values.join('');
  }

  function pad(value) { return value < 10 ? '0' + value : String(value); }

  // 开放与截止时间按学校时区的原始文本显示，与设备时区无关。
  function schoolTime(text) {
    var match = /^(\d{4})-(\d{2})-(\d{2}) (\d{2}):(\d{2})/.exec(text || '');
    return match ? Number(match[2]) + ' 月 ' + Number(match[3]) + ' 日 ' + match[4] + ':' + match[5] : '';
  }

  // ---- 对话框与表格页 ----
  var openLayers = [];

  function trapFocus(layer, event) {
    var focusable = $$('button:not([disabled]), input:not([disabled]), select:not([disabled]), textarea:not([disabled]), a[href], [tabindex="0"]', layer.panel);
    if (!focusable.length) return;
    var first = focusable[0];
    var last = focusable[focusable.length - 1];
    if (event.shiftKey && document.activeElement === first) { last.focus(); event.preventDefault(); }
    else if (!event.shiftKey && document.activeElement === last) { first.focus(); event.preventDefault(); }
  }

  document.addEventListener('keydown', function (event) {
    var layer = openLayers[openLayers.length - 1];
    if (!layer) return;
    var key = event.key || event.keyCode;
    if (key === 'Escape' || key === 'Esc' || key === 27) { event.preventDefault(); layer.dismiss(); }
    else if (key === 'Tab' || key === 9) trapFocus(layer, event);
  });

  // options：title、message、body（节点）、wide（表格页）、narrow（窄表格页）、footNote、buttons [{label, kind, onClick}]、onDismiss
  function layer(options) {
    var previous = document.activeElement;
    var backdrop = el('div', 'dialog-backdrop');
    var panel = el('div', options.wide ? (options.narrow ? 'sheet sheet-narrow' : 'sheet') : 'dialog');
    var titleId = 'dlg-' + requestId().slice(0, 8);
    panel.setAttribute('role', options.wide ? 'dialog' : 'alertdialog');
    panel.setAttribute('aria-modal', 'true');
    panel.setAttribute('aria-labelledby', titleId);
    var head = options.wide ? el('div', 'sheet-head') : panel;
    var title = el('h2', '', options.title);
    title.id = titleId;
    add(head, title);
    var subtitle = el('p', '', options.message || '');
    subtitle.hidden = !options.message;
    add(head, subtitle);
    if (options.wide) add(panel, head);
    if (options.body) add(panel, options.wide ? add(el('div', 'sheet-body'), options.body) : options.body);
    var actions = el('div', 'actions');
    var foot = options.wide ? add(el('div', 'sheet-foot'), options.footNote || el('span'), actions) : actions;
    var controller = {
      panel: panel,
      buttons: [],
      setMessage: function (text) { subtitle.textContent = text || ''; subtitle.hidden = !text; },
      close: function () {
        var index = openLayers.indexOf(controller);
        if (index >= 0) openLayers.splice(index, 1);
        if (backdrop.parentNode) backdrop.parentNode.removeChild(backdrop);
        if (!openLayers.length) document.body.classList.remove('has-dialog');
        if (previous && previous.focus) { try { previous.focus(); } catch (error) { previous = null; } }
      },
      dismiss: function () {
        if (options.onDismiss) options.onDismiss(); else controller.close();
      }
    };
    (options.buttons || []).forEach(function (spec) {
      var className = 'btn';
      if (spec.kind === 'primary') className += ' btn-primary';
      else if (spec.kind === 'destructive') className += ' btn-destructive btn-filled';
      var node = button(spec.label, className, function () { spec.onClick(controller); });
      controller.buttons.push(node);
      actions.appendChild(node);
    });
    add(panel, foot);
    backdrop.appendChild(panel);
    backdrop.addEventListener('mousedown', function (event) {
      if (event.target === backdrop && !options.wide) controller.dismiss();
    });
    document.body.appendChild(backdrop);
    document.body.classList.add('has-dialog');
    openLayers.push(controller);
    var focusTarget = controller.buttons[options.focus === undefined ? controller.buttons.length - 1 : options.focus];
    setTimeout(function () { if (focusTarget) focusTarget.focus(); }, 0);
    return controller;
  }

  // 确认框：resolve(true) 表示执行。破坏性操作默认聚焦“取消”。
  function confirm(options) {
    return new Promise(function (resolve) {
      var dialog = layer({
        title: options.title,
        message: options.message,
        focus: options.destructive ? 0 : 1,
        onDismiss: function () { dialog.close(); resolve(false); },
        buttons: [
          { label: options.cancel || '取消', onClick: function () { dialog.close(); resolve(false); } },
          { label: options.confirm || '好', kind: options.destructive ? 'destructive' : 'primary',
            onClick: function () { dialog.close(); resolve(true); } }
        ]
      });
    });
  }

  function alert(options) {
    return new Promise(function (resolve) {
      var dialog = layer({
        title: options.title,
        message: options.message,
        onDismiss: function () { dialog.close(); resolve(); },
        buttons: [{ label: '好', kind: 'primary', onClick: function () { dialog.close(); resolve(); } }]
      });
    });
  }

  function sheet(options) {
    return layer(Object.assign({ wide: true }, options));
  }

  function bindLogout() {
    var node = $('#logout');
    if (!node) return;
    node.addEventListener('click', function () {
      request('/api/logout', { method: 'POST', body: {} }).then(function () { window.location.href = '/'; });
    });
  }

  function onVisible(callback) {
    var name = typeof document.hidden !== 'undefined' ? 'visibilitychange' : 'msvisibilitychange';
    document.addEventListener(name, callback);
  }

  window.$ = $;
  window.$$ = $$;
  window.App = {
    el: el,
    add: add,
    clear: clear,
    button: button,
    toggleClass: toggleClass,
    hidden: hidden,
    onVisible: onVisible,
    request: request,
    upload: upload,
    saveBlob: saveBlob,
    toast: toast,
    busy: busy,
    requestId: requestId,
    pad: pad,
    schoolTime: schoolTime,
    confirm: confirm,
    alert: alert,
    sheet: sheet,
    bindLogout: bindLogout
  };
  bindLogout();
})(window, document);
