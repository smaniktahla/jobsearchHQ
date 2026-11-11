/* Light / dark / system theme. Loaded in <head> so the right palette is set before first paint.
   Preference is stored per browser (localStorage 'jshq-theme': 'system' | 'light' | 'dark'). */
(function () {
  var KEY = 'jshq-theme', root = document.documentElement;
  function get() { try { var v = localStorage.getItem(KEY); return v === 'light' || v === 'dark' ? v : 'system'; } catch (e) { return 'system'; } }
  function effective(mode) {
    if (mode === 'light' || mode === 'dark') return mode;
    return window.matchMedia && window.matchMedia('(prefers-color-scheme: light)').matches ? 'light' : 'dark';
  }
  function apply() { root.setAttribute('data-theme', effective(get())); }
  window.getTheme = get;
  window.setTheme = function (mode) {
    try { if (mode === 'system') localStorage.removeItem(KEY); else localStorage.setItem(KEY, mode); } catch (e) {}
    apply();
    document.dispatchEvent(new CustomEvent('themechange', { detail: mode }));
  };
  apply();
  if (window.matchMedia) {
    var mq = window.matchMedia('(prefers-color-scheme: light)');
    var on = function () { if (get() === 'system') apply(); };
    if (mq.addEventListener) mq.addEventListener('change', on); else if (mq.addListener) mq.addListener(on);
  }
})();
