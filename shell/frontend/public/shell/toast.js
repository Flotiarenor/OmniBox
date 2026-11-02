/**
 * 壳与插件 iframe 共用的 Toast 实现。
 *
 * 为什么只有一个文件：壳页面与插件页是两个独立文档，同一句提示必须长得一样。
 * 之前两个文档各存一份实现（壳的 Vue 模块与 base.js 里各一段），结果去重、无障碍
 * 属性、悬停暂停这类改动只能落在其中一侧，另一侧看起来"还是那个 Toast"却在行为上
 * 悄悄分叉。这里挂 `window.Toast`，Vue 侧只做类型适配，插件侧零改动。
 *
 * 为什么 DOM 结构与类名不能变：样式在 /shell/shared.css 的 `.toast*` 规则里，
 * 既有调用方与页面都按"`.toast-container` + `.toast .toast-<类型> .toast-icon`"
 * 使用它。换结构等于两侧同时变样，而没有任何门禁能发现这种纯视觉回归。
 *
 * 纯 JS、无框架依赖、无构建期步骤：它要同时被 POST 的壳首页与插件引导片段加载。
 */
window.Toast = (function () {
  'use strict';

  var DEFAULT_DURATION = 2600;
  // 与 shared.css 里 `.toast` 的 transition 时长对齐：先摘 .show，等淡出结束再移除节点。
  var EXIT_MS = 250;
  // 同一段文本在这段时间内重复触发只弹第一次：失败轮询每个周期都报同一句，
  // 不去重就会把右上角堆满同一行字，读屏也会反复播报。
  var DEDUPE_MS = 1500;

  var ICONS = {
    success: 'icon:circle-check',
    error: 'icon:circle-x',
    warning: 'icon:triangle-alert',
    info: 'icon:info',
  };

  var container = null;
  // 键为"类型 + 文本"，值是上一次真正弹出来的节点与时间戳。
  var recent = {};

  function ensureContainer() {
    if (container && container.isConnected !== false) return container;
    container = document.createElement('div');
    container.className = 'toast-container';
    // role="status" 隐含 aria-live="polite"：提示不抢焦点，读屏在用户当前朗读结束后
    // 再播报，既不会打断操作，也不会让视觉提示对读屏用户完全消失。
    container.setAttribute('role', 'status');
    container.setAttribute('aria-live', 'polite');
    document.body.appendChild(container);
    return container;
  }

  /**
   * 前缀图标的标记。
   *
   * 图标名是本文件里的常量，标记由图标集的白名单生成；取不到图标集（脚本加载失败、
   * 或者在没有任何引导片段的裸页面里直接打开）时返回空串 —— 少一个图标比显示一个
   * 由系统字体决定的字符好。
   */
  function iconMarkup(name) {
    if (window.Icons && typeof window.Icons.html === 'function') return window.Icons.html(name);
    return '';
  }

  /** 系统开启了"减少动态效果"时不放入场动画，但提示本身照常出现。 */
  function prefersReducedMotion() {
    try {
      return typeof window.matchMedia === 'function'
        && window.matchMedia('(prefers-reduced-motion: reduce)').matches;
    } catch (e) {
      return false;
    }
  }

  function removeNode(el) {
    if (el.parentNode) el.parentNode.removeChild(el);
  }

  function dismiss(item) {
    if (item.closed) return;
    item.closed = true;
    clearTimeout(item.timer);
    item.timer = 0;
    if (item.reduced) {
      removeNode(item.el);
      return;
    }
    item.el.classList.remove('show');
    setTimeout(function () { removeNode(item.el); }, EXIT_MS);
  }

  function startTimer(item) {
    item.startedAt = Date.now();
    item.timer = setTimeout(function () {
      item.timer = 0;
      dismiss(item);
    }, item.remaining);
  }

  function pruneRecent(now) {
    var keys = Object.keys(recent);
    if (keys.length <= 32) return;
    keys.forEach(function (key) {
      if (now - recent[key].at >= DEDUPE_MS) delete recent[key];
    });
  }

  /**
   * 弹一条提示。
   *
   * 签名与旧实现一致（文本、类型、毫秒），返回节点，便于需要时自行收尾。
   */
  function show(message, type, duration) {
    var text = message == null ? '' : String(message);
    var kind = ICONS[type] ? type : 'info';
    var total = (typeof duration === 'number' && duration > 0) ? duration : DEFAULT_DURATION;
    var now = Date.now();
    var key = kind + '\u0000' + text;

    var previous = recent[key];
    if (previous && now - previous.at < DEDUPE_MS && previous.el.parentNode) return previous.el;
    pruneRecent(now);

    var el = document.createElement('div');
    el.className = 'toast toast-' + kind;
    var icon = document.createElement('span');
    icon.className = 'toast-icon';
    icon.innerHTML = iconMarkup(ICONS[kind]);
    // 消息只走文本节点：调用方传进来的是后端错误与用户输入，拼进 innerHTML 就是注入点。
    el.appendChild(icon);
    el.appendChild(document.createTextNode(text));

    var reduced = prefersReducedMotion();
    var item = { el: el, remaining: total, timer: 0, startedAt: 0, closed: false, reduced: reduced };

    // 悬停暂停：用户正在读的时候不该消失。移开后按剩余时间继续计时，
    // 而不是重新计满 —— 否则鼠标扫过一下就会让提示多停留一整段。
    el.addEventListener('mouseenter', function () {
      if (item.closed || !item.timer) return;
      clearTimeout(item.timer);
      item.timer = 0;
      item.remaining = Math.max(0, item.remaining - (Date.now() - item.startedAt));
    });
    el.addEventListener('mouseleave', function () {
      if (item.closed || item.timer || item.remaining <= 0) return;
      startTimer(item);
    });

    // 减少动态效果时先带上 .show 再插入：没有状态跃迁，过渡就不会播放。
    if (reduced) el.classList.add('show');
    ensureContainer().appendChild(el);
    if (!reduced) {
      if (typeof requestAnimationFrame === 'function') {
        requestAnimationFrame(function () { el.classList.add('show'); });
      } else {
        el.classList.add('show');
      }
    }

    recent[key] = { at: now, el: el };
    startTimer(item);
    return el;
  }

  return {
    show: show,
    success: function (message, duration) { return show(message, 'success', duration); },
    error: function (message, duration) { return show(message, 'error', duration); },
    warning: function (message, duration) { return show(message, 'warning', duration); },
    info: function (message, duration) { return show(message, 'info', duration); },
  };
})();
