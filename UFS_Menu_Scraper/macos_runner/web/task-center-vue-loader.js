/*
  任务中心 Vue 试点加载器。
  1. 只接管 #page-task-center：其他页面与其他模块的 DOM、焦点、请求周期不变；
  2. 同源加载固定构建产物 /vue/task-center.js|css，运行时不依赖 CDN 或开发服务器；
  3. localStorage 记忆选择，任意时刻可回切旧版；新版加载失败自动回退。
*/
(function () {
  'use strict';

  const STORAGE_KEY = 'task-center-vue-enabled';
  const legacyNode = () => document.getElementById('task-center-legacy');
  const hostNode = () => document.getElementById('task-center-vue');

  let mounted = false;
  let pending = false;
  let bundleReady = false;
  let failed = false;

  function enabled() {
    try {
      return localStorage.getItem(STORAGE_KEY) !== '0';
    } catch (error) {
      return true; // 隐私模式下无法读取，按默认启用处理。
    }
  }

  function remember(on) {
    try {
      localStorage.setItem(STORAGE_KEY, on ? '1' : '0');
    } catch (error) {
      /* 忽略存储失败，不阻断页面 */
    }
  }

  function pageActive() {
    return document.getElementById('page-task-center')?.classList.contains('active') === true;
  }

  function showLegacy() {
    const legacy = legacyNode();
    const host = hostNode();
    if (host) {
      host.hidden = true;
      host.replaceChildren();
    }
    if (legacy) legacy.hidden = false;
    const useVue = document.getElementById('history-use-vue');
    if (useVue) useVue.style.display = '';
    mounted = false;
  }

  function showVue() {
    const legacy = legacyNode();
    const host = hostNode();
    if (legacy) legacy.hidden = true;
    if (host) host.hidden = false;
    const useVue = document.getElementById('history-use-vue');
    if (useVue) useVue.style.display = 'none';
    mounted = true;
  }

  function fail(error) {
    failed = true;
    pending = false;
    console.error('[task-center-vue] 加载失败：', error);
    showLegacy();
    window.__consoleBridge?.toast?.('新版任务中心加载失败，已回退旧版');
    window.__loadLegacyHistory?.();
  }

  function mountVue() {
    if (!pageActive()) return;
    try {
      const bundle = window.MenuTaskCenter;
      if (!bundle || typeof bundle.mountTaskCenter !== 'function') {
        throw new Error('构建产物缺少 mountTaskCenter 入口');
      }
      bundle.mountTaskCenter({
        ...(window.__consoleBridge || {}),
        switchToLegacy: () => {
          remember(false);
          unload();
          showLegacy();
          window.__loadLegacyHistory?.();
        },
      });
      showVue();
    } catch (error) {
      fail(error);
    }
  }

  function loadBundle() {
    if (!enabled() || failed) return;
    if (bundleReady) {
      if (!mounted) mountVue();
      return;
    }
    if (pending) return;
    pending = true;
    const styleId = 'task-center-vue-style';
    if (!document.getElementById(styleId)) {
      const link = document.createElement('link');
      link.id = styleId;
      link.rel = 'stylesheet';
      link.href = '/vue/task-center.css';
      link.onerror = () => fail(new Error('样式资源加载失败'));
      document.head.append(link);
    }
    const script = document.createElement('script');
    script.src = '/vue/task-center.js';
    script.onload = () => {
      pending = false;
      bundleReady = true;
      if (enabled()) mountVue();
    };
    script.onerror = () => fail(new Error('脚本资源加载失败'));
    document.head.append(script);
  }

  function unload() {
    if (!mounted) return;
    try {
      window.MenuTaskCenter?.unmountTaskCenter?.();
    } catch (error) {
      /* 组件已卸载 */
    }
    mounted = false;
  }

  function sync() {
    if (!pageActive()) {
      unload();
      return;
    }
    if (enabled() && !failed) loadBundle();
  }

  // 新版页面内的「回切旧版」由组件通过 bridge 调用；旧版页面的「使用新版」在这里挂载。
  const useVueButton = document.getElementById('history-use-vue');
  if (useVueButton) {
    useVueButton.addEventListener('click', () => {
      remember(true);
      failed = false;
      sync();
    });
  }

  window.addEventListener('menu:page', (event) => {
    if (event.detail === 'task-center') sync();
    else unload();
  });

  window.__vueTaskCenterActive = () => mounted;
  window.__vueTaskCenterEnabled = () => enabled();

  if (!enabled()) {
    showLegacy();
  } else if (pageActive()) {
    loadBundle();
  }
})();
