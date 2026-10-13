(function () {
  const KEY = 'nexapp-theme';

  function systemTheme() {
    return window.matchMedia('(prefers-color-scheme: dark)').matches ? 'dark' : 'light';
  }

  function resolve(pref) {
    if (pref === 'light' || pref === 'dark') return pref;
    return systemTheme();
  }

  function apply(pref) {
    const resolved = resolve(pref || 'system');
    document.documentElement.setAttribute('data-theme', resolved);
    document.documentElement.setAttribute('data-theme-pref', pref || 'system');
    try {
      localStorage.setItem(KEY, pref || 'system');
    } catch (_) {}
  }

  function currentPref() {
    const attr = document.documentElement.getAttribute('data-theme-pref');
    if (attr) return attr;
    try {
      return localStorage.getItem(KEY) || 'system';
    } catch (_) {
      return 'system';
    }
  }

  function cycle() {
    const order = ['system', 'light', 'dark'];
    const cur = currentPref();
    const next = order[(order.indexOf(cur) + 1) % order.length];
    apply(next);
    document.dispatchEvent(new CustomEvent('nexapp:theme', { detail: { pref: next } }));
    return next;
  }

  // Initial paint (avoid flash if inline script already set)
  const boot = document.documentElement.getAttribute('data-theme-pref') || currentPref();
  apply(boot);

  window.matchMedia('(prefers-color-scheme: dark)').addEventListener('change', () => {
    if (currentPref() === 'system') apply('system');
  });

  window.NexAppTheme = { apply, cycle, currentPref, resolve };
})();
