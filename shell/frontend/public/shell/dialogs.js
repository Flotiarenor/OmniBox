/**
 * This product includes software developed by flotiarenor.Copyright 2026 flotiarenor
 * 共享运行时 · 弹窗层：焦点与键盘公共工具（_focusableIn / _trapTab / _dialogKeydown …）、
 * 零侵入模态行为（window.Shell.dialog）、确认对话框（confirmDialog）。
 *
 * 「零侵入」是这套实现的语义：插件页里已有的 .modal 由一个文档级观察器补上 role /
 * aria-modal、焦点进入与归还、Esc、Tab 循环、点遮罩关闭与 modal:close 事件，插件侧
 * 不需要调用任何 API —— 因此 window.Shell.dialog 在文档加载时就自装，不能改成按需调用。
 * 设置弹窗、灯箱与右键菜单的 Tab 循环 / 焦点归还复用同一组工具函数。
 */

// ==================== 弹窗/菜单的键盘与焦点公共工具 ====================
// 旧调用只关心“弹窗何时关闭、返回值是什么”，这些工具只补可访问性，不改协议。
// 全部做能力探测：tests/js 的 DOM 桩没有 focus/getBoundingClientRect，不能因此抛错。
function _focusableIn(root) {
  if (!root || typeof root.querySelectorAll !== 'function') return [];
  const nodes = Array.from(root.querySelectorAll(
    'a[href],button,input,select,textarea,[tabindex]:not([tabindex="-1"])'));
  return nodes.filter((el) => {
    if (!el || el.disabled) return false;
    if (typeof el.getAttribute === 'function') {
      if (el.getAttribute('aria-hidden') === 'true') return false;
      if (el.getAttribute('tabindex') === '-1') return false;
    }
    if (typeof el.getBoundingClientRect === 'function') {
      const rect = el.getBoundingClientRect();
      if (!(rect.width > 0 || rect.height > 0)) return false;
    }
    return typeof el.focus === 'function';
  });
}

function _rememberFocus() {
  const el = document.activeElement;
  return el && el !== document.body ? el : null;
}

function _restoreFocus(el) {
  if (!el || el.isConnected === false || typeof el.focus !== 'function') return;
  try { el.focus({ preventScroll: true }); } catch (e) { try { el.focus(); } catch (e2) { /* 元素已失效 */ } }
}

function _focusFirst(root, fallback) {
  const list = _focusableIn(root);
  const target = list[0] || fallback;
  if (target && typeof target.focus === 'function') {
    try { target.focus({ preventScroll: true }); } catch (e) { /* 旧浏览器忽略参数 */ }
  }
}

function _trapTab(root, event) {
  const list = _focusableIn(root);
  if (!list.length) {
    if (typeof event.preventDefault === 'function') event.preventDefault();
    return;
  }
  const first = list[0];
  const last = list[list.length - 1];
  const active = document.activeElement;
  const inside = !!(root && typeof root.contains === 'function' && root.contains(active));
  if (event.shiftKey) {
    if (!inside || active === first) {
      event.preventDefault();
      try { last.focus(); } catch (e) { /* 元素已失效 */ }
    }
  } else if (!inside || active === last) {
    event.preventDefault();
    try { first.focus(); } catch (e) { /* 元素已失效 */ }
  }
}

function _dialogKeydown(root, event, close) {
  if (!event) return;
  if (event.key === 'Escape') {
    event.preventDefault();
    event.stopPropagation();
    close();
    return;
  }
  if (event.key === 'Tab') _trapTab(root, event);
}

// ==================== 模态行为（零侵入） ====================
/**
 * `.modal` 骨架的无障碍与键盘行为，插件侧零代码、零标记改动。
 *
 * 为什么由壳代管：内容页里的模态没有一个带 `role="dialog"`，document-reader / group-mesh /
 * image-viewer / manga-library 的弹窗都不响应 Esc，焦点只有"打开时 setTimeout 聚焦输入框"
 * 这半程，**没有一个**在关闭后把焦点还给触发元素。这些行为与弹窗长什么样无关，只与
 * "哪个元素是 `.modal`、何时带 `active`"有关，所以用 MutationObserver 监听 class：
 * 插件把 `active` 加上/去掉就完成了开/关，不必改 HTML、也不必改调用方式。
 *
 * 明确不接管的：
 *   · 已经带 `role="dialog"` 的元素 —— 本文件自己的 `openModal` / `confirmDialog` 就是
 *     这样，它们自己管焦点与 Esc，重复接管会让两套逻辑互相抢焦点；
 *   · 带 `data-shell-dialog` 的元素 —— 本模块的归属标记，用来保证同一元素只接一次。
 *
 * 插件作者需要知道的两处可观察变化：关闭时会在模态元素上派发 `modal:close`（可冒泡，
 * 用于清理自己的状态），Esc 不再被插件独占（本模块从 document 上收）。
 *
 * `tests/js` 的 DOM 替身没有 MutationObserver，`init()` 直接返回，不抛错。
 */
window.Shell = window.Shell || {};
window.Shell.dialog = (function () {
  var state = { initialized: false, observer: null, stack: [] };
  var titleSeq = 0;

  function attr(el, name) {
    return el && typeof el.getAttribute === 'function' ? el.getAttribute(name) : null;
  }

  function setAttr(el, name, value) {
    if (!el || typeof el.setAttribute !== 'function') return;
    try { el.setAttribute(name, value); } catch (e) { /* 桩环境 */ }
  }

  function removeAttr(el, name) {
    if (!el || typeof el.removeAttribute !== 'function') return;
    try { el.removeAttribute(name); } catch (e) { /* 桩环境 */ }
  }

  function hasClass(el, name) {
    return !!(el && el.classList && typeof el.classList.contains === 'function'
      && el.classList.contains(name));
  }

  function isModal(el) {
    return hasClass(el, 'modal');
  }

  function boxOf(el) {
    if (!el || typeof el.querySelector !== 'function') return null;
    try { return el.querySelector('.modal-box'); } catch (e) { return null; }
  }

  /** 初始焦点：`[autofocus]` → 输入控件 → 按钮 → 其它可聚焦元素 → 弹窗本体。 */
  function pickInitial(box) {
    if (!box) return null;
    if (typeof box.querySelector === 'function') {
      var auto = box.querySelector('[autofocus]');
      if (auto && typeof auto.focus === 'function') return auto;
    }
    var list = _focusableIn(box);
    var order = ['INPUT', 'SELECT', 'TEXTAREA', 'BUTTON'];
    for (var k = 0; k < order.length; k++) {
      for (var i = 0; i < list.length; i++) {
        if (String(list[i].tagName || '').toUpperCase() === order[k]) return list[i];
      }
    }
    return list[0] || null;
  }

  function dispatchClose(el) {
    if (!el || typeof el.dispatchEvent !== 'function') return;
    var event = null;
    try {
      if (typeof CustomEvent === 'function') {
        event = new CustomEvent('modal:close', { bubbles: true, cancelable: false });
      } else if (document && typeof document.createEvent === 'function') {
        event = document.createEvent('CustomEvent');
        event.initCustomEvent('modal:close', true, false, null);
      }
    } catch (e) { event = null; }
    if (!event) return;
    try { el.dispatchEvent(event); } catch (e) { /* 桩环境没有事件系统 */ }
  }

  function open(el) {
    if (!isModal(el) || el.__obxDialog) return;
    // 别人的无障碍标记不接管：它可能已经自己管着 Esc 与焦点（openModal / confirmDialog）
    if (attr(el, 'data-shell-dialog') === null && attr(el, 'role') === 'dialog') return;

    var box = boxOf(el) || el;
    var active = document.activeElement;
    var trigger = active && active !== document.body
      && !(box && typeof box.contains === 'function' && box.contains(active)) ? active : null;
    var info = { trigger: trigger, heading: null, headingId: null, label: false, boxTabindex: false };
    el.__obxDialog = info;

    setAttr(el, 'data-shell-dialog', '');
    setAttr(el, 'role', 'dialog');
    setAttr(el, 'aria-modal', 'true');

    if (box && typeof box.querySelector === 'function') {
      var heading = box.querySelector('h3');
      var text = heading ? String(heading.textContent || '').replace(/\s+/g, ' ').trim() : '';
      if (text) {
        var id = attr(heading, 'id');
        if (!id) {
          id = 'shell-dialog-title-' + (++titleSeq);
          setAttr(heading, 'id', id);
          info.heading = heading;
        }
        info.headingId = id;
        setAttr(el, 'aria-labelledby', id);
      }
    }
    // 没有标题时退回 aria-label：读屏软件至少能说出"这是个弹窗"
    if (attr(el, 'aria-labelledby') === null && attr(el, 'aria-label') === null) {
      setAttr(el, 'aria-label', '弹窗');
      info.label = true;
    }

    if (state.stack.indexOf(el) < 0) state.stack.push(el);

    var target = pickInitial(box);
    if (!target) {
      // 没有可聚焦元素时把焦点落在弹窗本体，别留在背景页面上
      target = box;
      if (target && attr(target, 'tabindex') === null) {
        setAttr(target, 'tabindex', '-1');
        info.boxTabindex = true;
      }
    }
    if (target && typeof target.focus === 'function') {
      try { target.focus({ preventScroll: true }); } catch (e) { /* 旧浏览器忽略参数 */ }
    }
  }

  function close(el) {
    if (!el || !el.__obxDialog) return;
    var info = el.__obxDialog;
    el.__obxDialog = null;

    var at = state.stack.indexOf(el);
    if (at >= 0) state.stack.splice(at, 1);

    removeAttr(el, 'role');
    removeAttr(el, 'aria-modal');
    removeAttr(el, 'aria-labelledby');
    if (info.heading && info.headingId) removeAttr(info.heading, 'id');
    if (info.label) removeAttr(el, 'aria-label');
    if (info.boxTabindex) removeAttr(boxOf(el) || el, 'tabindex');

    dispatchClose(el);
    _restoreFocus(info.trigger);
  }

  /** 关闭入口：去掉 `active`（与插件自己关的方式一致），再收尾并归还焦点。 */
  function requestClose(el) {
    if (!el) return;
    if (el.classList && typeof el.classList.remove === 'function') {
      try { el.classList.remove('active'); } catch (e) { /* 桩环境 */ }
    }
    close(el);
  }

  function top() {
    return state.stack.length ? state.stack[state.stack.length - 1] : null;
  }

  /**
   * 延后一拍再关。
   *
   * 插件自己的 Esc / pointerdown 处理器常拿 `.modal.active` 当守卫（"弹窗开着，这个键/
   * 这次点击不归我"）。同一个事件里我们先关掉，守卫就落空：document-reader 的阅读态把
   * Esc 解释成"退出阅读模式"，于是关目录弹窗的同时退回了书架。推后一个宏任务，
   * 让同一轮事件的所有处理器都先看到"弹窗还是开的"，再按原语义关闭。
   */
  function deferClose(el) {
    if (typeof setTimeout !== 'function') { requestClose(el); return; }
    setTimeout(function () { requestClose(el); }, 0);
  }

  function onMutations(records) {
    for (var i = 0; i < records.length; i++) {
      var record = records[i];
      if (!record) continue;
      var el = record.target;
      if (record.type === 'childList') {
        // 动态创建后直接插入的模态（`createElement` + `class="modal active"`）不会有
        // class 变更记录，只能从新增节点里找。只在新增节点上做一次查询，代价可忽略。
        var added = record.addedNodes || [];
        for (var j = 0; j < added.length; j++) {
          var node = added[j];
          if (!node || node.nodeType !== 1) continue;
          if (isModal(node) && hasClass(node, 'active')) open(node);
          if (typeof node.querySelectorAll === 'function') {
            var nested = node.querySelectorAll('.modal.active');
            for (var k = 0; k < nested.length; k++) open(nested[k]);
          }
        }
        continue;
      }
      if (!isModal(el)) continue;
      if (hasClass(el, 'active')) open(el);
      else close(el);
    }
  }

  function onKeydown(event) {
    if (!event) return;
    var dialog = top();
    if (!dialog) return;
    if (event.key === 'Escape' || event.key === 'Esc') {
      if (typeof event.preventDefault === 'function') event.preventDefault();
      deferClose(dialog);
      return;
    }
    if (event.key === 'Tab') _trapTab(boxOf(dialog) || dialog, event);
  }

  function onPointerDown(event) {
    var dialog = top();
    if (!dialog) return;
    // 只有按在遮罩本身（不是 .modal-box 内）才算点遮罩；pointerdown 而非 click，
    // 否则"在弹窗里按下、拖到遮罩上松开"会被算成点击遮罩。
    if (event && event.target === dialog) deferClose(dialog);
  }

  function init() {
    if (state.initialized) return;                                  // 幂等
    if (typeof document === 'undefined' || !document) return;
    if (typeof MutationObserver !== 'function') return;             // tests/js 的替身：安全退化
    if (typeof document.querySelectorAll !== 'function') return;
    var root = document.body || document.documentElement;
    if (!root) return;
    try {
      state.observer = new MutationObserver(onMutations);
      state.observer.observe(root, {
        attributes: true, attributeFilter: ['class'], childList: true, subtree: true,
      });
    } catch (e) {
      state.observer = null;
      return;
    }
    state.initialized = true;
    if (typeof document.addEventListener === 'function') {
      document.addEventListener('keydown', onKeydown);
      document.addEventListener('pointerdown', onPointerDown);
    }
    // 页面里已经处于打开态的模态（HTML 直接写了 `modal active`）也要接上
    var existing = document.querySelectorAll('.modal');
    for (var i = 0; i < existing.length; i++) {
      if (hasClass(existing[i], 'active')) open(existing[i]);
    }
  }

  return { open: open, close: requestClose, init: init };
})();

// base.js 在插件 <head> 里执行，此时 body 还不存在，所以自动接管要等 DOM 就绪。
// 壳页面不加载本文件，壳文档里的模态由壳自己的组件管。
(function autoInitShellDialog() {
  if (typeof document === 'undefined' || !document) return;
  if (typeof document.addEventListener !== 'function') return;
  if (document.readyState === 'loading') {
    document.addEventListener('DOMContentLoaded', function () { window.Shell.dialog.init(); });
  } else {
    window.Shell.dialog.init();
  }
})();

// ==================== 确认对话框（替代原生 confirm） ====================
function confirmDialog(message, options = {}) {
  return new Promise((resolve) => {
    const previousFocus = _rememberFocus();
    const messageId = 'confirm-message-' + Math.random().toString(36).slice(2, 8);
    const overlay = document.createElement('div');
    overlay.className = 'modal active';
    overlay.setAttribute('role', 'dialog');
    overlay.setAttribute('aria-modal', 'true');
    overlay.setAttribute('aria-label', options.title || '确认');
    overlay.setAttribute('aria-describedby', messageId);
    overlay.innerHTML = `
      <div class="modal-box modal-confirm">
        <div class="modal-body">
          <div class="confirm-message" id="${messageId}">${Utils.escapeHtml(message)}</div>
        </div>
        <div class="modal-footer">
          <button class="btn" data-act="cancel">取消</button>
          <button class="btn ${options.danger ? 'btn-danger-solid' : 'btn-primary'}" data-act="ok">${Utils.escapeHtml(options.okText || '确定')}</button>
        </div>
      </div>`;
    document.body.appendChild(overlay);
    const cancelBtn = overlay.querySelector('[data-act="cancel"]');
    const okBtn = overlay.querySelector('[data-act="ok"]');
    const onKeydown = (event) => _dialogKeydown(overlay, event, () => close(false));
    let settled = false;
    const close = (val) => {
      if (settled) return;
      settled = true;
      overlay.removeEventListener('keydown', onKeydown);
      overlay.remove();
      _restoreFocus(previousFocus);
      resolve(val);
    };
    overlay.addEventListener('keydown', onKeydown);
    // 危险操作默认聚焦“取消”；普通确认聚焦可执行的“确定”。
    const initial = options.danger ? cancelBtn : (okBtn || cancelBtn);
    if (initial && typeof initial.focus === 'function') {
      try { initial.focus({ preventScroll: true }); } catch (e) { /* 旧浏览器忽略参数 */ }
    } else {
      _focusFirst(overlay, cancelBtn);
    }
    // pointerdown 而非 click：click 的 target 是按下/松开的最近公共祖先，
    // 在弹窗内按下、拖到遮罩上松开会被误判成"点了遮罩"。这里只看按下的位置。
    overlay.addEventListener('pointerdown', (e) => { if (e.target === overlay) close(false); });
    cancelBtn.addEventListener('click', () => close(false));
    okBtn.addEventListener('click', () => close(true));
  });
}

