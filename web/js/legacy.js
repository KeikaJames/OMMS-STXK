// 浏览器缺少本站脚本所需的能力时（IE10 及更早版本）显示提示。只用 ES3 语法。
(function () {
  var root = document.documentElement;
  var capable = !!(window.JSON && window.XMLHttpRequest && document.querySelector && root.addEventListener &&
    ('classList' in root) && ('dataset' in root) && window.Uint8Array);

  function show() {
    var note = document.getElementById('legacy');
    if (note) note.removeAttribute('hidden');
  }

  if (capable) return;
  if (document.readyState === 'complete' || document.readyState === 'interactive') show();
  else if (document.addEventListener) document.addEventListener('DOMContentLoaded', show, false);
  else if (window.attachEvent) window.attachEvent('onload', show);
})();
