(() => {
  const key = 'easy-runners-theme';
  const media = window.matchMedia('(prefers-color-scheme: dark)');

  function preference() {
    const stored = window.localStorage.getItem(key);
    return ['light', 'dark', 'system'].includes(stored) ? stored : 'system';
  }

  function apply(value = preference()) {
    const theme = value === 'system' ? (media.matches ? 'dark' : 'light') : value;
    document.documentElement.dataset.theme = theme;
    document.documentElement.dataset.themePreference = value;
    return value;
  }

  function set(value) {
    window.localStorage.setItem(key, value);
    apply(value);
  }

  media.addEventListener('change', () => {
    if (preference() === 'system') apply('system');
  });

  window.EasyRunnersTheme = {apply, preference, set};
  apply();
})();
