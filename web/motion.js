/* motion.js —— 学生端动效(渐进增强)
   所有函数在 GSAP 缺失、prefers-reduced-motion 或触屏/粗指针环境下都有即时退化,
   页面逻辑不依赖动画完成。第三方库见 /vendor/LICENSES.md。 */
(function () {
  'use strict';
  var root = document.documentElement;
  var reduce = !!(window.matchMedia && matchMedia('(prefers-reduced-motion: reduce)').matches);
  var fine = !!(window.matchMedia && matchMedia('(hover: hover) and (pointer: fine)').matches);
  var g = window.gsap;
  var ok = !!g && !reduce;
  var EASE = 'power3.out';

  function reveal() { root.classList.remove('motion'); }
  if (!ok) reveal();

  function list(x) {
    if (!x) return [];
    if (typeof x === 'string') return Array.prototype.slice.call(document.querySelectorAll(x));
    if (x.nodeType) return [x];
    return Array.prototype.slice.call(x);
  }
  function show(els) { els.forEach(function (e) { e.classList.remove('m-in'); }); }

  /* 页面进场:依次上浮、由模糊到清晰 */
  function enterPage(sel, opts) {
    var els = list(sel);
    if (!ok || !els.length) { show(els); reveal(); return null; }
    var o = Object.assign({ y: 16, stagger: .07, duration: .75, delay: .04 }, opts || {});
    g.set(els, { opacity: 0, y: o.y, filter: 'blur(6px)' });
    show(els); reveal();
    return g.to(els, { opacity: 1, y: 0, filter: 'blur(0px)', duration: o.duration, stagger: o.stagger, delay: o.delay, ease: EASE, clearProps: 'filter,transform' });
  }

  /* 一批新元素(如社团卡)交错进场 */
  function stagger(els) {
    els = list(els);
    if (!ok || !els.length) return;
    g.fromTo(els, { opacity: 0, y: 14, scale: .985 }, { opacity: 1, y: 0, scale: 1, duration: .55, stagger: .05, ease: EASE, clearProps: 'transform' });
  }
  function enter(el) {
    if (!ok) return;
    g.fromTo(el, { opacity: 0, y: 12, scale: .985 }, { opacity: 1, y: 0, scale: 1, duration: .45, ease: EASE, clearProps: 'transform' });
  }

  /* 数字从旧值滚到新值(首次直接写入) */
  function count(el, from, to, dur) {
    if (!ok || from === null || from === undefined || from === to) { el.textContent = String(to); return; }
    if (el._cnt) el._cnt.kill();
    var o = { v: from };
    el._cnt = g.to(o, {
      v: to, duration: dur || .7, ease: 'power2.out', snap: { v: 1 },
      onUpdate: function () { el.textContent = String(Math.round(o.v)); },
      onComplete: function () { el.textContent = String(to); el._cnt = null; }
    });
  }

  /* 时钟逐位渲染:只有变化的字符做一次上滚 */
  function digits(el, text, prefix) {
    var key = (prefix || '') + '|' + text.length;
    if (!ok || el._key !== key) {
      el.textContent = '';
      if (prefix) { var p = document.createElement('span'); p.className = 'clk-now'; p.textContent = prefix; el.appendChild(p); }
      if (!ok) { el.appendChild(document.createTextNode(text)); el._key = null; return; }
      el._chars = [];
      for (var i = 0; i < text.length; i++) {
        var s = document.createElement('span'); s.className = 'd'; s.textContent = text[i];
        el.appendChild(s); el._chars.push(s);
      }
      el._key = key;
      return;
    }
    for (var j = 0; j < text.length; j++) {
      var c = el._chars[j];
      if (c.textContent !== text[j]) {
        c.textContent = text[j];
        g.fromTo(c, { y: '.5em', opacity: 0 }, { y: 0, opacity: 1, duration: .3, ease: EASE, overwrite: true });
      }
    }
  }
  /* 最后十秒:每一跳轻微放大回弹 */
  function tick(el) {
    if (!ok) return;
    g.fromTo(el, { scale: 1.06 }, { scale: 1, duration: .5, ease: 'power2.out', transformOrigin: 'right center', overwrite: 'auto' });
  }

  function toast(el, ms) {
    if (!ok) { setTimeout(function () { el.remove(); }, ms); return; }
    g.fromTo(el, { opacity: 0, y: 14, scale: .96 }, { opacity: 1, y: 0, scale: 1, duration: .4, ease: EASE });
    g.to(el, { opacity: 0, y: 8, duration: .3, ease: 'power2.in', delay: ms / 1000, onComplete: function () { el.remove(); } });
  }

  /* 错误抖动(作用在未被 tilt 接管的外层容器) */
  function shake(el) {
    if (!ok) return;
    g.to(el, { keyframes: { x: [0, -9, 8, -6, 4, -2, 0] }, duration: .5, ease: 'power1.inOut', clearProps: 'transform' });
  }
  function flash(el) {
    if (!ok) return;
    g.fromTo(el, { opacity: 0, y: -4 }, { opacity: 1, y: 0, duration: .35, ease: EASE, clearProps: 'transform' });
  }

  /* 状态变化的扩散光环 */
  function ping(el, color) {
    if (!ok) return;
    var s = document.createElement('span'); s.className = 'm-ping'; s.setAttribute('aria-hidden', 'true');
    el.appendChild(s);
    var c = color || '0,113,227';
    g.fromTo(s, { boxShadow: '0 0 0 0 rgba(' + c + ',.5)' }, { boxShadow: '0 0 0 22px rgba(' + c + ',0)', duration: .9, ease: 'power2.out', onComplete: function () { s.remove(); } });
  }

  /* 登录玻璃卡:轻微 3D 倾斜 + 高光(仅精细指针设备) */
  function tilt(el) {
    if (!ok || !fine || !window.VanillaTilt || !el) return;
    VanillaTilt.init(el, { max: 4, speed: 900, perspective: 1400, scale: 1.006, glare: true, 'max-glare': .14, gyroscope: false, easing: 'cubic-bezier(.03,.98,.52,.99)' });
  }

  /* 登录页纸面光斑:随指针缓动;触屏则缓慢自动游走 */
  function spotlight() {
    if (!ok) return;
    var layer = document.createElement('div'); layer.className = 'home-light'; layer.setAttribute('aria-hidden', 'true');
    document.body.appendChild(layer);
    var pos = { x: innerWidth * .5, y: innerHeight * .42 };
    var apply = function () { layer.style.setProperty('--mx', pos.x + 'px'); layer.style.setProperty('--my', pos.y + 'px'); };
    apply();
    if (fine) {
      var qx = g.quickTo(pos, 'x', { duration: .9, ease: 'power3', onUpdate: apply });
      var qy = g.quickTo(pos, 'y', { duration: .9, ease: 'power3', onUpdate: apply });
      window.addEventListener('pointermove', function (e) { qx(e.clientX); qy(e.clientY); }, { passive: true });
    } else {
      g.to(pos, { x: function () { return innerWidth * (.3 + Math.random() * .4); }, y: function () { return innerHeight * (.25 + Math.random() * .4); }, duration: 7, ease: 'sine.inOut', repeat: -1, repeatRefresh: true, onUpdate: apply });
    }
    g.to(layer, { opacity: 1, duration: 1.4, delay: .3 });
  }

  /* 分段控件的滑动白块;返回 place(animate) 供点击后调用 */
  function segThumb(seg) {
    if (!ok || !seg) return null;
    var thumb = document.createElement('span'); thumb.className = 'seg-thumb'; thumb.setAttribute('aria-hidden', 'true');
    seg.appendChild(thumb); seg.classList.add('has-thumb');
    var place = function (animate) {
      var on = seg.querySelector('button.on'); if (!on) return;
      var v = { x: on.offsetLeft, width: on.offsetWidth };
      if (animate) g.to(thumb, Object.assign({ duration: .42, ease: EASE, overwrite: true }, v)); else g.set(thumb, v);
    };
    place(false);
    window.addEventListener('resize', function () { place(false); });
    return place;
  }

  /* 登录页进场编排:卡片起身 → 标题逐字 → 其余元素交错 */
  function loginIntro(card, items, title) {
    var els = list(items);
    if (!ok) { show(els); if (card) card.classList.remove('m-in'); if (title) title.classList.remove('m-in'); reveal(); return; }
    var chars = [];
    if (title && !title._split) {
      var text = title.textContent; title.textContent = '';
      for (var i = 0; i < text.length; i++) { var s = document.createElement('span'); s.className = 'ch'; s.textContent = text[i]; title.appendChild(s); chars.push(s); }
      title._split = true;
    }
    var tl = g.timeline({ defaults: { ease: EASE } });
    g.set(card, { opacity: 0, y: 26, scale: .97 });
    g.set(els, { opacity: 0, y: 12 });
    if (chars.length) g.set(chars, { opacity: 0, y: 10, filter: 'blur(6px)' });
    card.classList.remove('m-in'); if (title) title.classList.remove('m-in'); show(els); reveal();
    tl.to(card, { opacity: 1, y: 0, scale: 1, duration: .8, clearProps: 'transform' }, .05)
      .to(chars, { opacity: 1, y: 0, filter: 'blur(0px)', duration: .6, stagger: .055, clearProps: 'filter,transform' }, .3)
      .to(els, { opacity: 1, y: 0, duration: .6, stagger: .06, clearProps: 'transform' }, .4);
    return tl;
  }

  /* 离场后跳转 */
  function exitTo(url, el) {
    if (!ok || !el) { location.href = url; return; }
    if (el.vanillaTilt) el.vanillaTilt.destroy();
    g.to(el, { opacity: 0, scale: .97, y: -8, duration: .34, ease: 'power2.in', onComplete: function () { location.href = url; } });
  }

  /* 抢到名额的彩带,从按钮位置喷出 */
  function confetti(fromEl) {
    if (!ok || !window.confetti) return;
    var origin = { x: .5, y: .6 };
    if (fromEl && fromEl.getBoundingClientRect) {
      var r = fromEl.getBoundingClientRect();
      origin = { x: (r.left + r.width / 2) / innerWidth, y: (r.top + r.height / 2) / innerHeight };
    }
    window.confetti({ particleCount: 70, spread: 62, startVelocity: 26, gravity: 1.1, ticks: 160, scalar: .85, origin: origin,
      colors: ['#0071E3', '#5AC8FA', '#34C759', '#FFFFFF', '#FFD60A'], disableForReducedMotion: true });
  }

  window.Motion = { ok: ok, enterPage: enterPage, stagger: stagger, enter: enter, count: count, digits: digits, tick: tick, toast: toast,
    shake: shake, flash: flash, ping: ping, tilt: tilt, spotlight: spotlight, segThumb: segThumb, loginIntro: loginIntro, exitTo: exitTo, confetti: confetti };
})();
