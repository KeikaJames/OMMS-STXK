// 只用 ES3 语法：浏览器跑不了本站脚本时显示换浏览器的提示，并检测 flex 布局是否支持 gap。
(function () {
  var root = document.documentElement;
  var modern = typeof window.fetch === 'function' && typeof window.Promise === 'function' &&
    typeof String.prototype.padStart === 'function' &&
    !!(window.CSS && window.CSS.supports && window.CSS.supports('--a', '0'));

  function ready(callback) {
    if (document.readyState !== 'loading') callback();
    else if (document.addEventListener) document.addEventListener('DOMContentLoaded', callback);
    else window.attachEvent('onload', callback);
  }

  ready(function () {
    if (!modern) {
      root.className += ' legacy';
      var note = document.getElementById('legacy');
      if (note) note.removeAttribute('hidden');
      return;
    }
    var probe = document.createElement('div');
    probe.style.display = 'flex';
    probe.style.flexDirection = 'column';
    probe.style.rowGap = '1px';
    probe.appendChild(document.createElement('div'));
    probe.appendChild(document.createElement('div'));
    document.body.appendChild(probe);
    if (probe.scrollHeight !== 1) root.className += ' no-flex-gap';
    document.body.removeChild(probe);
  });
})();
