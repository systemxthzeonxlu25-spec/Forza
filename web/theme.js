// 主题：在 <head> 里同步加载，赶在页面绘制之前设好 data-theme，避免先闪一下浅色。
// 没选过就不设，跟随系统（见 common.css 的 prefers-color-scheme）。
(function () {
  try {
    var t = localStorage.getItem("forza-theme");
    if (t === "light" || t === "dark") document.documentElement.setAttribute("data-theme", t);
  } catch (_) { /* 隐私模式等拿不到 localStorage：跟随系统 */ }
})();
