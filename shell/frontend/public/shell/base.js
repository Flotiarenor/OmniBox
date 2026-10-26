/**
 * This product includes software developed by flotiarenor.Copyright 2026 flotiarenor
 * Shell 注入的基础运行时
 * 提供：Bridge（通信）、Utils（工具）、通用 UI 组件函数
 */

// ==================== 插件生命周期（宿主 → 插件） ====================
// 常驻插件（默认 keep-alive，App.vue 用 v-show 隐藏）切换后其定时器、rAF 自循环与轮询
// 仍在跑；插件前端也没有统一的销毁钩子，事件监听器只增不减（实测 171 : 4）。
// 宿主因此在"可见性变化"时 postMessage 通知，这里把它转成三个注册钩子。
//
// 语义（与常驻策略配套）：onHide 表示"停止视觉与轮询类工作"，**不要求停止播放** ——
// 常驻正是为了切换时媒体不中断（readme.md），所以是否暂停由插件自行决定。
window.PluginLifecycle = (function() {
  var hooks = { show: [], hide: [], dispose: [] };
  var state = { visible: true, disposed: false, showSeen: false, hideSeen: false };

  function add(list, fn) {
    if (typeof fn !== 'function') return;
    list.push(fn);
  }

  function emit(list, label) {
    // 单个钩子抛异常不得影响其他钩子与宿主
    for (var i = 0; i < list.length; i++) {
      try {
        list[i]();
      } catch (e) {
        console.error('[OmniBox] on' + label + ' 钩子抛异常:', e);
      }
    }
  }

  /**
   * 注册"iframe 由隐藏转为显示"的回调（可注册多个，按注册顺序调用）。
   *
   * 触发时机：常驻插件被切回前台、窗口从最小化 / 其他标签页恢复。
   * 若注册时 iframe 已经可见，回调会立即执行一次，因此"进入即恢复"的写法无需额外判断。
   * @param {() => void} fn
   */
  function onShow(fn) {
    // 注册时若已可见（插件在 onShow 里做"进入即恢复"是常见写法），立刻补一次
    add(hooks.show, fn);
    if (state.visible && !state.disposed && typeof fn === 'function') {
      try { fn(); } catch (e) { console.error('[OmniBox] onShow 钩子抛异常:', e); }
    }
  }

  /**
   * 注册"iframe 由显示转为隐藏"的回调（可注册多个，按注册顺序调用）。
   *
   * 触发时机：常驻插件被切到后台、窗口最小化、切换到其他浏览器标签页。
   * 语义是"停止视觉与轮询类工作"，**不要求停止播放** —— 常驻正是为了媒体不中断。
   * @param {() => void} fn
   */
  function onHide(fn) {
    add(hooks.hide, fn);
    if (!state.visible && !state.disposed && typeof fn === 'function') {
      try { fn(); } catch (e) { console.error('[OmniBox] onHide 钩子抛异常:', e); }
    }
  }

  /**
   * 注册"iframe 即将销毁"的回调（可注册多个，按注册顺序调用）。
   *
   * 触发时机：不保活的插件离开页面时（iframe 即将卸载）、整个页面卸载时。
   * 用于摘掉 window/document 上的监听器、清掉定时器与 rAF 自循环。
   * @param {() => void} fn
   */
  function onDispose(fn) {
    add(hooks.dispose, fn);
    if (state.disposed && typeof fn === 'function') {
      try { fn(); } catch (e) { console.error('[OmniBox] onDispose 钩子抛异常:', e); }
    }
  }

  function setVisible(visible) {
    visible = !!visible;
    if (state.disposed || visible === state.visible) return;
    state.visible = visible;
    if (visible) {
      state.showSeen = true;
      emit(hooks.show, 'Show');
    } else {
      state.hideSeen = true;
      emit(hooks.hide, 'Hide');
    }
  }

  function dispose() {
    if (state.disposed) return;
    state.disposed = true;
    emit(hooks.dispose, 'Dispose');
  }

  function initial(visible) {
    // 初次注册时的一次性同步：只发"当前状态对应的那一个"钩子，避免插件刚注册
    // onShow / onHide 就同时收到两条矛盾通知。
    visible = !!visible;
    state.visible = visible;
    if (visible) {
      state.showSeen = true;
      emit(hooks.show, 'Show');
    } else {
      state.hideSeen = true;
      emit(hooks.hide, 'Hide');
    }
  }

  return {
    onShow: onShow, onHide: onHide, onDispose: onDispose,
    setVisible: setVisible, dispose: dispose, initial: initial, state: state,
  };
})();

// 插件最常用的三个入口同时挂到 window 上（base.js 先于插件脚本注入，注册不丢）
window.onShow = window.PluginLifecycle.onShow;
window.onHide = window.PluginLifecycle.onHide;
window.onDispose = window.PluginLifecycle.onDispose;

// ===== 内核 → 插件的消息接收 =====
// 消息必须校验来源：只接受"父窗口直接发来"的消息。仅校验 event.origin 不足以防
// 伪造 —— 宿主再内嵌一层 frame 时 origin 完全相同。
function isMessageFromShell(event) {
  if (!event) return false;
  var parentWindow;
  try {
    parentWindow = window.parent;
  } catch (e) {
    return false;
  }
  if (event.source && parentWindow && event.source !== parentWindow) return false;
  var origin;
  try {
    origin = window.location && window.location.origin;
  } catch (e) {
    origin = undefined;
  }
  if (origin && event.origin && event.origin !== origin) return false;
  return true;
}

window.addEventListener('message', function(event) {
  if (!isMessageFromShell(event)) return;
  var data = event.data;
  if (!data || typeof data.type !== 'string') return;
  if (data.type === 'omnibox:appearance') {
    var alpha = Number(data.alpha);
    if (alpha === alpha) {
      alpha = Math.max(0, Math.min(100, Math.round(alpha)));
      document.documentElement.style.setProperty('--plugin-bg-alpha', alpha + '%');
      document.documentElement.setAttribute('data-plugin-bg', 'on');
    }
    return;
  }
  if (data.type === 'omnibox:settings-changed') {
    // 壳通知设置已变更：整页重载（插件自身状态由设置重新拉取）
    window.location.href = window.location.href.split('?')[0] + '?_t=' + Date.now();
    return;
  }
  if (data.type === 'omnibox:plugin-shown') {
    window.PluginLifecycle.setVisible(true);
    return;
  }
  if (data.type === 'omnibox:plugin-hidden') {
    window.PluginLifecycle.setVisible(false);
    return;
  }
  if (data.type === 'omnibox:plugin-dispose') {
    window.PluginLifecycle.dispose();
  }
});

// ==================== Bridge ====================
window.Bridge = (function() {
  let API_PREFIX = '';

  // 兼容宿主再内嵌 iframe 的场景：从当前 frame 一直向上找拥有 pywebview.api 的窗口。
  function resolveApi() {
    let current = parent;
    while (current) {
      if (current.pywebview && current.pywebview.api) {
        return current.pywebview.api;
      }
      if (current === current.parent) break;
      current = current.parent;
    }
    return null;
  }

  async function call(method, ...args) {
    const api = resolveApi();
    if (!api) throw new Error('PyWebView API 不可用');
    const fullMethod = API_PREFIX ? `${API_PREFIX}__${method}` : method;
    return await api[fullMethod](...args);
  }

  async function callSystem(method, ...args) {
    const api = resolveApi();
    if (!api) throw new Error('PyWebView API 不可用');
    return await api[method](...args);
  }

  async function callPlugin(plugin, method, ...args) {
    const api = resolveApi();
    if (!api) throw new Error('PyWebView API 不可用');
    return await api[`${plugin}__${method}`](...args);
  }

  function originalUrl(path) {
    const plugin = API_PREFIX; // 如 'image-viewer'
    // 使用 query 参数传递 path，避免绝对路径中的 / 被 Flask 路由吞掉
    return `/file?path=${encodeURIComponent(path)}&plugin=${plugin}`;
  }

  function thumbUrl(path) {
    const plugin = API_PREFIX;
    // 逐段编码后拼回（保留 / 作为分隔符）：路径里的 % # ? 等字符原样拼进 URL 会被
    // 反代（nginx 对非法百分号转义直接 400）或浏览器（# 之后当 fragment 截断）吃掉，
    // 缩略图就再也加载不出来 —— 原图走 originalUrl() 有 encodeURIComponent 所以正常。
    const encoded = String(path == null ? '' : path)
      .split('/')
      .map((segment) => encodeURIComponent(segment))
      .join('/');
    return `/thumbs/${encoded}?plugin=${plugin}`;
  }

  function setPrefix(prefix) {
    API_PREFIX = prefix;
  }

  return { call, callSystem, callPlugin, originalUrl, thumbUrl, setPrefix };
})();

// 兼容旧代码的全局 bridge 别名
window.bridge = window.Bridge;

// ==================== Utils ====================
window.Utils = {
  debounce(func, wait) {
    let timeout;
    return function(...args) {
      clearTimeout(timeout);
      timeout = setTimeout(() => func.apply(this, args), wait);
    };
  },

  formatFileSize(bytes) {
    if (bytes < 1024) return bytes + ' B';
    if (bytes < 1048576) return (bytes / 1024).toFixed(1) + ' KB';
    return (bytes / 1048576).toFixed(1) + ' MB';
  },

  // HTML 转义：**同时转义引号**，因此可以直接用在属性值里（"..." / '...'）。
  // 只转 &<> 的写法（textContent → innerHTML）看着"够用"，一旦插进
  // data-x="${...}"，值里的一个引号就能逃出属性并注入 —— 而调用方无从知道
  // 哪个 helper 适合属性、哪个只适合文本。
  escapeHtml(str) {
    if (str == null) return '';
    return String(str)
      .replace(/&/g, '&amp;')
      .replace(/</g, '&lt;')
      .replace(/>/g, '&gt;')
      .replace(/"/g, '&quot;')
      .replace(/'/g, '&#39;');
  },

  /**
   * 图标值 → 可直接插入的标记。
   *
   * 接受两种写法，两种都**已经安全**（图标名来自仓库常量，emoji 走转义）：
   *   `icon:名字` —— 壳图标集（/shell/icons.generated.js 的 Icons.html）
   *   其它任何字符串 —— 转义后原样渲染（旧插件传 emoji，不能因为换了图标集就白屏）
   */
  iconHtml(value) {
    if (typeof value === 'string' && value.slice(0, 5) === 'icon:') {
      if (window.Icons && typeof window.Icons.html === 'function') {
        return window.Icons.html(value);
      }
      return '';
    }
    return this.escapeHtml(value || '');
  },

  // 内联事件处理器里的字符串参数（onerror="f('${...}')"）：
  // 先做 JS 字符串转义，再做 HTML 属性转义，两步缺一不可。
  jsString(value) {
    const safe = String(value == null ? '' : value)
      .replace(/\\/g, '\\\\')
      .replace(/'/g, "\\'")
      .replace(/\r?\n/g, '\\n');
    return Utils.escapeHtml(safe);
  }
};

// ==================== Toast 通知 ====================
window.Toast = (function() {
  let container = null;
  function ensureContainer() {
    if (!container) {
      container = document.createElement('div');
      container.className = 'toast-container';
      document.body.appendChild(container);
    }
    return container;
  }
  // 前缀图标走壳图标集：伪元素（CSS `content`）放不了 SVG，所以前缀由 JS 插入元素。
  const TOAST_ICONS = {
    success: 'icon:circle-check',
    error: 'icon:circle-x',
    warning: 'icon:triangle-alert',
    info: 'icon:info'
  };
  function show(message, type = 'info', duration = 2600) {
    const el = document.createElement('div');
    el.className = `toast toast-${type}`;
    const iconName = TOAST_ICONS[type] || TOAST_ICONS.info;
    el.innerHTML = `<span class="toast-icon">${Utils.iconHtml(iconName)}</span>${Utils.escapeHtml(message)}`;
    ensureContainer().appendChild(el);
    requestAnimationFrame(() => el.classList.add('show'));
    setTimeout(() => {
      el.classList.remove('show');
      setTimeout(() => el.remove(), 250);
    }, duration);
    return el;
  }
  return {
    show,
    info: (msg) => show(msg, 'info'),
    success: (msg) => show(msg, 'success'),
    warning: (msg) => show(msg, 'warning'),
    error: (msg) => show(msg, 'error')
  };
})();

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

// ==================== 设置表单（按 schema 渲染） ====================
function createSettingsForm(container, schema, values = {}) {
  const fieldEls = {};
  const baseId = 'cfg-' + Math.random().toString(36).slice(2, 8);

  (schema || []).forEach((field) => {
    // `hidden`：这个键**可写但不在面板里显示**。
    //
    // 为什么不能直接把键从 schema 里删掉：schema 同时是 `save_settings` 的写入白名单
    // （见 backend/plugin_base.py 的 allowed），删掉键等于把"同一批配置"从面板挪走
    // 的同时也让它存不进去。插件的自绘设置页（如 document-reader 的阅读设置）就是
    // 这种情况：入口在插件自己那边，值仍要走 save_settings。
    if (field.hidden === true) return;

    const wrap = document.createElement('div');
    wrap.className = 'field' + (field.type === 'checkbox' ? ' field-checkbox' : '');
    wrap.dataset.key = field.key;

    const label = document.createElement('label');
    label.className = 'field-label';
    label.htmlFor = `${baseId}-${field.key}`;
    label.textContent = field.label || field.key;
    if (field.required) label.classList.add('required');

    const current = values[field.key] !== undefined ? values[field.key] : field.default;
    let input;

    // 目录列表：实现是 Shell 共享组件 window.FolderPicker
    // （shell/frontend/public/shell/folder-picker.js，与 image-viewer 用的是同一份）。
    // 值仍是普通字符串：单值字段存一行路径，multi 字段存换行分隔的多行 ——
    // 与改造前的 text / textarea 格式一致，后端读取代码不用动。
    if (field.type === 'directory') {
      const raw = Array.isArray(current) ? current.join('\n') : String(current == null ? '' : current);
      const list = window.FolderPicker.createList({
        paths: raw.split('\n').map(line => line.trim()).filter(Boolean),
        placeholder: field.placeholder,
        emptyText: field.emptyText,
        // `multi` 是设置契约的一部分：非 multi 的目录字段是**单值**设置，
        // getValues() 只提交第一行。列表若仍允许多行，用户加的其余行会被静默丢弃，
        // 因此用 FolderPicker 的 single 模式（新路径替换旧路径）与之对齐。
        single: field.multi !== true,
        // 目录字段默认允许「网络位置」来源；声明 local_only 的字段（该目录本身就是
        // 产物，例如下载落点）不显示它 —— 在那类字段上选网络位置语义不成立。
        localOnly: field.local_only === true,
      });
      wrap.append(label, list.element);
      fieldEls[field.key] = list;
      if (field.help) {
        const help = document.createElement('p');
        help.className = 'field-help';
        help.textContent = field.help;
        wrap.appendChild(help);
      }
      container.appendChild(wrap);
      return;
    }

    // 只读信息行：值是后端算出来的（如"扫描的是哪个目录"），这里只展示、不可编辑。
    // 它**不登记进 fieldEls**，getValues() 因此取不到它 —— 展示用的一行不该被当成设置
    // 提交回后端（后端也把 info 字段排除在可写键之外，见 plugin_base.save_settings）。
    // 值优先取 values[key]（后端 get_settings()），其次取 schema 里的 value（静态说明）。
    if (field.type === 'info') {
      const shown = values[field.key] !== undefined ? values[field.key]
        : (field.value !== undefined ? field.value : field.default);
      const value = document.createElement('div');
      value.className = 'field-info';
      value.textContent = shown == null ? '' : String(shown);
      wrap.append(label, value);
      if (field.help) {
        const help = document.createElement('p');
        help.className = 'field-help';
        help.textContent = field.help;
        wrap.appendChild(help);
      }
      container.appendChild(wrap);
      return;
    }

    if (field.type === 'checkbox') {
      input = document.createElement('input');
      input.type = 'checkbox';
      input.id = `${baseId}-${field.key}`;
      input.checked = !!current;
      wrap.append(input, label);
    } else if (field.type === 'select') {
      input = document.createElement('select');
      input.id = `${baseId}-${field.key}`;
      (field.options || []).forEach(opt => {
        const o = document.createElement('option');
        o.value = (typeof opt === 'object') ? opt.value : opt;
        o.textContent = (typeof opt === 'object') ? opt.label : opt;
        input.appendChild(o);
      });
      if (current !== undefined) input.value = String(current);
    } else if (field.type === 'textarea') {
      input = document.createElement('textarea');
      input.id = `${baseId}-${field.key}`;
      input.value = current !== undefined ? current : '';
      if (field.placeholder) input.placeholder = field.placeholder;
    } else {
      const isRange = field.type === 'range';
      const isNumber = field.type === 'number';
      input = document.createElement('input');
      input.type = isRange ? 'range' : (isNumber ? 'number' : 'text');
      input.id = `${baseId}-${field.key}`;
      if (isRange || isNumber) {
        if (field.min !== undefined) input.min = field.min;
        if (field.max !== undefined) input.max = field.max;
        if (field.step !== undefined) input.step = field.step;
        input.value = current !== undefined ? current : (field.default !== undefined ? field.default : 0);
      } else {
        input.value = current !== undefined ? current : (field.default !== undefined ? field.default : '');
        if (field.placeholder) input.placeholder = field.placeholder;
      }
    }

    if (field.type === 'range') {
      const valueSpan = document.createElement('span');
      valueSpan.className = 'field-range-value';
      valueSpan.textContent = input.value;
      input.addEventListener('input', () => { valueSpan.textContent = input.value; });
      const row = document.createElement('div');
      row.className = 'field-range';
      row.append(input, valueSpan);
      wrap.append(label, row);
      fieldEls[field.key] = input;
    } else if (field.type === 'checkbox') {
      fieldEls[field.key] = input;
    } else {
      wrap.append(label, input);
      fieldEls[field.key] = input;
    }

    if (field.help) {
      const help = document.createElement('p');
      help.className = 'field-help';
      help.textContent = field.help;
      wrap.appendChild(help);
    }
    container.appendChild(wrap);
  });

  function getValues() {
    const out = {};
    (schema || []).forEach((field) => {
      const el = fieldEls[field.key];
      if (!el) return;
      if (field.type === 'directory') {
        const paths = el.getPaths();
        // 单值字段存一行路径，多值字段（multi）存换行分隔；两者都是字符串，
        // 与改造前的 text / textarea 值格式一致，后端读取代码不用动
        out[field.key] = field.multi ? paths.join('\n') : (paths[0] || '');
      }
      else if (field.type === 'checkbox') out[field.key] = el.checked;
      else if (field.type === 'number' || field.type === 'range') out[field.key] = Number(el.value);
      else out[field.key] = el.value;
    });
    return out;
  }

  function setValues(newValues) {
    (schema || []).forEach((field) => {
      const el = fieldEls[field.key];
      if (!el || newValues[field.key] === undefined) return;
      if (field.type === 'directory') {
        const raw = String(newValues[field.key] == null ? '' : newValues[field.key]);
        el.setPaths(raw.split('\n').map(line => line.trim()).filter(Boolean));
      }
      else if (field.type === 'checkbox') el.checked = !!newValues[field.key];
      else if (field.type === 'range') {
        el.value = newValues[field.key];
        const span = el.parentElement.querySelector('.field-range-value');
        if (span) span.textContent = el.value;
      } else el.value = newValues[field.key];
    });
  }

  return { getValues, setValues, element: container };
}

// ==================== 统一设置弹窗（插件按需调用） ====================
function openSettingsModal(options = {}) {
  const title = options.title || '设置';
  const onSave = options.onSave;
  return openModal({
    title: title,
    // 省略 schema/values 时在内部按当前插件后端拉取：这是"插件自己的设置弹窗"的默认用法
    schema: options.schema,
    values: options.values,
    emptyText: '该插件暂无设置项',
    onSave: onSave || (async (values) => {
      const result = await Bridge.call('save_settings', values);
      return { result: result };
    }),
  }).then(res => {
    if (!res || !res.saved) return null;
    return (res.result && res.result.values) || res.values;
  });
}

/**
 * 通用弹窗：Promise 化，`resolve(null)` = 取消，`resolve({saved: true, values, result})` = 已保存。
 *
 * 存在两个入口是有原因的：`openSettingsModal` 是"本页插件自己的设置"，省略 schema 时
 * 用本页的 `Bridge` 拉取与保存；而**上层替内嵌页渲染**设置时，schema/values/保存都归
 * 内嵌页自己（本页的 Bridge 指向的是这台宿主，直接存会写错插件）—— 那条路走
 * `HostChannel`，由 `defaultUiHandler` 调用本函数。
 *
 * `onSave` 抛异常 = 保存失败：弹窗保持打开、按钮恢复可点、错误以 toast 呈现。这个
 * 语义是刻意的，子插件回传失败时用户不会看到"弹窗关了但其实没存上"。
 */
function openModal(options = {}) {
  const title = options.title || '设置';
  const onSave = options.onSave;
  let form = null;

  return new Promise((resolve) => {
    (async () => {
      let schema = options.schema;
      let values = options.values;
      if (!schema) {
        try { schema = await Bridge.call('get_settings_schema'); } catch (e) { schema = []; }
      }
      if (values === undefined) {
        try { values = await Bridge.call('get_settings'); } catch (e) { values = {}; }
      }

      const overlay = document.createElement('div');
      overlay.className = 'modal active';
      const previousFocus = _rememberFocus();
      const titleId = 'modal-title-' + Math.random().toString(36).slice(2, 8);
      overlay.setAttribute('role', 'dialog');
      overlay.setAttribute('aria-modal', 'true');
      overlay.setAttribute('aria-labelledby', titleId);
      overlay.innerHTML = `
        <div class="modal-box">
          <h3 id="${titleId}">${Utils.escapeHtml(title)}</h3>
          <div class="modal-body settings-form"></div>
          <div class="modal-footer">
            <button class="btn" data-act="cancel">取消</button>
            <button class="btn btn-primary" data-act="save">保存</button>
          </div>
        </div>`;
      document.body.appendChild(overlay);
      let settled = false;
      const finish = (result) => {
        if (settled) return;
        settled = true;
        overlay.removeEventListener('keydown', onKeydown);
        overlay.remove();
        _restoreFocus(previousFocus);
        resolve(result);
      };
      const onKeydown = (event) => _dialogKeydown(overlay, event, () => finish(null));
      overlay.addEventListener('keydown', onKeydown);

      const body = overlay.querySelector('.modal-body');
      if (!schema || schema.length === 0) {
        body.innerHTML = '';
        const empty = document.createElement('div');
        empty.className = 'empty-state';
        empty.style.minHeight = '120px';
        empty.textContent = options.emptyText || '该插件暂无设置项';
        body.appendChild(empty);
      } else {
        form = createSettingsForm(body, schema, values || {});
      }
      // 初始焦点给第一个可编辑字段；没有字段时给“取消”，避免焦点留在背景页面。
      _focusFirst(overlay, overlay.querySelector('[data-act="cancel"]'));

      // pointerdown：按在遮罩上就关，按在弹窗内（哪怕拖到遮罩上松开）不关
      overlay.addEventListener('pointerdown', (e) => { if (e.target === overlay) finish(null); });
      overlay.querySelector('[data-act="cancel"]').addEventListener('click', () => finish(null));

      overlay.querySelector('[data-act="save"]').addEventListener('click', async () => {
        const saveBtn = overlay.querySelector('[data-act="save"]');
        saveBtn.disabled = true;
        try {
          const newValues = form ? form.getValues() : {};
          let result = null;
          if (onSave) result = await onSave(newValues);
          if (result && result.result && result.result.success === false) {
            throw new Error(result.result.error || '保存失败');
          }
          if (result && result.success === false) {
            throw new Error(result.error || '保存失败');
          }
          Toast.success((options.successMessage) || '设置已保存');
          finish({ saved: true, values: newValues, result: (result && result.result) || result });
        } catch (e) {
          saveBtn.disabled = false;
          Toast.error(e.message || '保存失败');
        }
      });
    })();
  });
}

// ==================== 宿主通道（子插件 → 上层） ====================
/**
 * 被内嵌的插件页向上层要一件事，而不是自己造一套 UI 或伸手去读上层的对象。
 *
 * 为什么需要：内嵌 iframe 是独立文档，`.modal { position: fixed }` 只相对**自己那个
 * iframe**，弹窗既不会盖住宿主侧栏、也没有整页遮罩；所以"设置/确认/提示"这类
 * chrome 必须由上层用自己的文档渲染。这里只搬运**数据**，不搬运 DOM。
 *
 * 一条刻意的边界：上层只读 `action`，`data` 原样透传。上层因此不需要知道是谁发的、
 * 内容是什么意思 —— 身份是 `event.source`（那个 iframe 的 window），不是插件名。
 * 子插件侧只有 `HostChannel.request()` 一个入口，不知道对面是壳还是别的插件。
 *
 * 三条消息，全部只在 **直接父子** 之间走（不跨层转发）：
 *   request / reply            子 → 上；`{exchange, action, data}` / `{exchange, ok, data}`
 *   probe / ready              子 → 上；探测上层是否 `serve()` 过，探测不到就不发请求
 *   mount / run                子 → 上 / 上 → 子；把按钮挂到上层工具栏，点击回发 {id}
 *   callback / callback-reply  上 → 子；用于"设置了但值给谁保存"这类**由子插件做主**的回合
 *                              （上层把值交回子插件，子插件自己调 Bridge 落盘）
 *
 * 校验与 `isMessageFromShell` 分开写是有意的：那条要求 `event.source === window.parent`，
 * 而壳是直投顶层 iframe 的 —— 内嵌页的 parent 是宿主而不是壳，套那条会全部被拒。
 */
window.HostChannel = (function() {
  var REQUEST = 'omnibox:host-request';
  var REPLY = 'omnibox:host-reply';
  var PROBE = 'omnibox:host-probe';
  var MOUNT = 'omnibox:host-mount';
  var RUN = 'omnibox:host-run';
  var CALLBACK = 'omnibox:host-callback';
  var CALLBACK_REPLY = 'omnibox:host-callback-reply';
  var PROBE_TIMEOUT = 500;
  var MOUNT_TIMEOUT = 2000;    // 挂载是渲染请求，不通就立刻把源按钮放回来，别拖
  var DEFAULT_TIMEOUT = 30000;

  var seq = 0;
  var pending = {};     // exchange -> {resolve, timer}
  var callbackSeq = 0;
  var callbacks = {};   // step -> {resolve, timer}
  var callbackHandlers = {};   // action -> fn(values, data)
  var probed = null;    // 探测结果的缓存：每次 request 都重探一次太吵

  function self() {
    try { return window.parent && window.parent !== window ? window.parent : null; }
    catch (e) { return null; }
  }

  /**
   * 出站消息的 targetOrigin。
   *
   * 这里曾写死 `'*'`：目标窗口（父窗口或某个内嵌 iframe）一旦被导航到外部页面，
   * 之后的消息就原样交给那个外部页 —— 而 callback 带的是用户在设置弹窗里输入的值，
   * 可能包含凭据。壳与插件页都由同一个本地文件服务发出，同源是唯一支持的拓扑；
   * 取不到 origin（`file://` 之类的 opaque origin）时才回退 `'*'`：那种环境下没有
   * 可保护的本地源，而字符串 `'null'` 不是合法的 targetOrigin（浏览器会抛错）。
   */
  function targetOrigin() {
    try {
      var origin = window.location && window.location.origin;
      if (origin && origin !== 'null') return origin;
    } catch (e) { /* 访问不到 location：按没有可用 origin 处理 */ }
    return '*';
  }

  function post(target, payload) {
    try { target.postMessage(payload, targetOrigin()); } catch (e) { /* 跨域/已销毁，交给超时 */ }
  }

  /**
   * 发一条消息并等一条回信。
   *
   * 超时/没有父窗口时**一定要把 resolve 掉**：pending 里挂着一个永不 settle 的 Promise
   * 会让调用方的回退分支永远不执行，界面看起来就是"点了没反应"。
   */
  function waitReply(listener, key, timeout) {
    return new Promise(function(resolve) {
      var target = self();
      if (!target) { resolve({ ok: false, data: null }); return; }
      pending[key] = {
        resolve: function(reply) { resolve(reply); },
        timer: setTimeout(function() {
          if (!pending[key]) return;
          delete pending[key];
          resolve({ ok: false, data: null });
        }, timeout)
      };
      post(target, listener);
    });
  }

  /** 上层是否在听（`serve()` 过）。false 时调用方应当回落到自己的实现。 */
  function probe(timeout) {
    if (probed) return Promise.resolve(probed === 'ready');
    return waitReply({ type: PROBE }, 'probe', timeout || PROBE_TIMEOUT).then(function(reply) {
      probed = reply.ok ? 'ready' : 'absent';
      return reply.ok;
    });
  }

  /** 向上层要一件事。上层没 `serve()`、没实现该 action、或超时，一律 `{ok: false}`。 */
  function request(action, data, timeout) {
    var exchange = ++seq;
    return waitReply(
      { type: REQUEST, exchange: exchange, action: action, data: data },
      exchange,
      timeout || DEFAULT_TIMEOUT
    );
  }

  /** 注册上层回调（供 `ui` 弹窗保存时把值交回来；见 defaultUiHandler）。 */
  function onCallback(action, fn) {
    if (typeof fn === 'function') callbackHandlers[action] = fn;
  }

  window.addEventListener('message', function(event) {
    var data = event.data;
    if (!data || typeof data.type !== 'string') return;
    var host = self();
    if (!host || event.source !== host) return;

    if (data.type === REPLY && pending[data.exchange]) {
      // `probe` 的回信也走这里，**不要**为它单开分支：`waitReply({type: PROBE}, 'probe', …)`
      // 把 pending 挂在键 'probe' 上，上层回的 `exchange` 也是 'probe'，两边天然对上、
      // 回填的就是下面这个 `{ok, data}` 对象（`probe()` 读的正是 `reply.ok`）。
      // 曾有一条专为 probe 写的分支把 `!!data.ok`（布尔）当回信对象 resolve，于是
      // "上层明明答应了"也被读成 undefined → probe 恒为 false → 内嵌插件的设置/确认弹窗
      // 永远回落进 iframe 自己画的那一份（没有整页遮罩、被 iframe 边框裁掉）。
      var p = pending[data.exchange];
      clearTimeout(p.timer);
      delete pending[data.exchange];
      p.resolve({ ok: !!data.ok, data: data.data });
      return;
    }
    if (data.type === RUN) {
      // 上层工具栏里点了本插件挂上去的按钮：点回子页里那个源按钮，处理逻辑只留一份
      var el = data.id ? document.getElementById(data.id) : null;
      if (!el && data.id) {
        try { el = document.querySelector(data.id); } catch (e) { el = null; }
      }
      if (el) el.click();
      else console.warn('[OmniBox] 上层点了按钮但本页找不到它:', data.id);
      return;
    }
    if (data.type === CALLBACK) {
      var fn = callbackHandlers[data.action];
      if (typeof fn !== 'function') return;
      var step = data.step;
      Promise.resolve()
        .then(function() { return fn(data.data); })
        .then(function(res) { post(host, { type: CALLBACK_REPLY, step: step, ok: true, data: res === undefined ? null : res }); })
        .catch(function(err) {
          post(host, { type: CALLBACK_REPLY, step: step, ok: false, data: { error: String((err && err.message) || err) } });
        });
      return;
    }
    if (data.type === CALLBACK_REPLY && callbacks[data.step]) {
      var c = callbacks[data.step];
      clearTimeout(c.timer);
      delete callbacks[data.step];
      c.resolve({ ok: !!data.ok, data: data.data });
    }
  });

  /**
   * 子插件用：把**自己的**操作按钮挂到上层的工具栏/头部去。
   *
   * 为什么需要：内嵌页是独立文档，宿主头部的"标题 + 返回"它碰不到。只让弹窗由宿主
   * 渲染还不够 —— 按钮本身留在内嵌页正文里，用户看到的还是"操作在子页里、顶栏是宿主的"，
   * 与其它插件（设置/刷新在顶栏）不一致。这里把按钮**搬上去**：子插件只声明
   * `{id, label, icon}`，上层用它的样式渲染在它的工具栏里。
   *
   * 按钮定义留在子插件（宿主的代码里一个按钮名都不出现）；上层匹配"哪个 iframe 发的"
   * 用 `iframe.src` 与子页 `location.href` 比对，不靠插件名。点击时上层回发
   * `{type:'omnibox:host-run', id}`，本模块在子页里点那个源按钮 —— 所以处理逻辑
   * 仍然只有一份，就在子页里。
   *
   * `selector` 传了的话，挂载成功后把内嵌页里那个源节点藏起来，避免两处重复；
   * 挂载失败（宿主不表态 / 没有对应容器）则保持原样。
   */
  function mountToolbar(buttons, options) {
    var opts = options || {};
    var toHide = null;
    if (opts.selector) {
      toHide = document.querySelector(opts.selector);
      if (toHide) {
        toHide.__obxDisplay = toHide.style.display;
        toHide.style.display = 'none';
      }
    }
    if (!self()) return;
    // 类型必须是 MOUNT：上层按 `msg.type === MOUNT` 分派（它**不查 handlers**，
    // 因为挂载点属于"宿主自己的界面"，不该由插件注册一个叫这个名字的处理器）。
    waitReply(
      { type: MOUNT, exchange: ++seq, data: { buttons: buttons, container: opts.container || '', url: window.location.href } },
      seq,
      MOUNT_TIMEOUT
    ).then(function(res) {
      if (res.ok) return;
      if (toHide) toHide.style.display = toHide.__obxDisplay || '';
      console.warn('[OmniBox] 上层没有可挂载的工具栏，按钮留在本页:', res.data && res.data.error);
    });
  }

  /**
   * 上层用：把值交给子插件处理（返回子插件回调的结果）。 */
  function invoke(source, action, data, timeout) {
    var step = ++callbackSeq;
    return new Promise(function(resolve, reject) {
      callbacks[step] = {
        resolve: function(res) { res.ok ? resolve(res.data) : reject(new Error((res.data && res.data.error) || '子插件回调失败')); },
        timer: setTimeout(function() { delete callbacks[step]; reject(new Error('子插件回调超时')); }, timeout || 15000)
      };
      post(source, { type: CALLBACK, step: step, action: action, data: data });
    });
  }

  /**
   * 上层用：把子插件声明的按钮渲染进**本页**的容器里。
   *
   * 定位顺序：`data.container` 先在 `containers` 映射里查（`serve({containers})` 给的自定义
   * 标签），找不到就当元素 id 查；都没给或元素不存在时抛错，子插件据此把按钮留在自己页里
   * （宁可留在原地，也不要"按钮点不到"）。
   *
   * "哪个 iframe 发的"不用插件名：拿 `data.url`（子页的 location.href）与页面上每个 iframe
   * 的 `src` 比路径末三段后比对 —— 只比文件名不行，各插件的内嵌页都叫 index.html。
   */
  function mountButtons(source, data, containers) {
    var label = data.container || 'host-toolbar';
    var box = containers[label] || document.getElementById(label);
    if (!box) throw new Error('no-container:' + label);

    var buttons = Array.isArray(data.buttons) ? data.buttons : [];
    var frame = null;
    var frames = document.querySelectorAll('iframe');
    for (var i = 0; i < frames.length; i++) {
      var src = frames[i].getAttribute('src') || '';
      if (src && frameKey(src) === frameKey(data.url || '')) { frame = frames[i]; break; }
    }
    if (!frame) throw new Error('no-frame');

    // 重挂载（页面重载后子插件会再发一次）时先清掉上一批，位置固定在最左：宿主自己的
    // 按钮（如"返回相册"）永远在最右，注入的按钮不会把它顶走。
    var previous = [].slice.call(box.querySelectorAll('.obx-host-action'));
    previous.forEach(function(el) { el.remove(); });
    // 倒着插到容器最前面：insertBefore 的结果正是声明的正序
    for (var k = buttons.length - 1; k >= 0; k--) {
      var spec = buttons[k] || {};
      var btn = document.createElement('button');
      btn.type = 'button';
      // `obx-toolbar-btn` 是壳的"工具栏动作按钮"组件：宿主头部不是 `.view-toolbar`，
      // 挂上来的按钮必须显式带上这个类，度量才与主工具栏那一排同源（base.css）。
      btn.className = 'btn obx-toolbar-btn obx-host-action';
      var icon = spec.icon ? iconHtml(spec.icon) + ' ' : '';
      var text = spec.label || spec.id || '';
      btn.innerHTML = icon + escapeHtml(text);
      if (spec.title) btn.title = spec.title;
      // 闭包要按次捕获 id，否则所有按钮都回发最后一个
      btn.addEventListener('click', (function(id) {
        return function() { post(source, { type: RUN, id: id, action: 'click' }); };
      })(spec.id || ''));
      // 记下归属：重挂载时能精确摘掉本模块插进去的那批（不误伤宿主自己的按钮）
      btn.__obxOwner = box;
      box.insertBefore(btn, box.firstChild);
    }
  }

  /**
   * 由一个 URL 得出"哪个插件的哪个页面"：取路径最后三段
   * （`/plugins/<插件>/frontend/index.html`）。
   */
  function frameKey(url) {
    var clean = String(url || '').split('?')[0].split('#')[0].replace(/\/+$/, '');
    var parts = clean.split('/').filter(Boolean);
    return parts.slice(-3).join('/');
  }

  /**
   * 本模块自己的转义与图标渲染。
   *
   * **不能用外层的 `Utils`**：`Utils` 在 base.js 里定义在 HostChannel **之后**，
   * IIFE 求值时它还不在作用域里（实测报 "Utils is not defined" 并被 post 的
   * try/catch 吞掉，表现为"宿主回了 ok:false 但看不出为什么"）。这里只依赖
   * `window.Icons`（由 icons.generated.js 先注入）。
   */
  function escapeHtml(value) {
    if (value == null) return '';
    return String(value).replace(/[&<>"']/g, function(c) {
      return { '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c];
    });
  }

  function iconHtml(value) {
    if (typeof value === 'string' && value.slice(0, 5) === 'icon:') {
      if (window.Icons && typeof window.Icons.html === 'function') return window.Icons.html(value);
      return '';
    }
    return escapeHtml(value || '');
  }

  /**
   * 子插件用：请上层用**它的文档**渲染本插件的设置弹窗。
   *
   * schema 与 values 由本页自己的 `Bridge` 取（本页的 Bridge 指向本插件），保存由调用方
   * 在 `HostChannel.onCallback('ui', fn)` 里自己做 —— 上层只画弹窗、只回传值。
   *
   * 回信只表示"上层接下了"，**不等用户在弹窗里点保存**：等下去的话用户把弹窗开着不动，
   * 请求就会先超时，调用方以为没人接、再弹一个本地弹窗 —— 变成两个弹窗叠着。
   * 保存走 callback（见 defaultUiHandler）。
   */
  async function requestSettings(title) {
    if (!self()) return { handled: false };
    if (!(await probe())) return { handled: false };
    var schema = [];
    var values = {};
    try {
      schema = await Bridge.call('get_settings_schema');
      values = await Bridge.call('get_settings');
    } catch (e) {
      // 取不到字段清单就让上层弹"暂无设置项"：比在本页弹一个字段不对的强
      console.error('[OmniBox] 读取设置字段失败:', e);
    }
    var res = await request('ui', {
      kind: 'settings',
      title: title || '设置',
      schema: schema,
      values: values,
    });
    return { handled: !!res.ok };
  }

  /**
   * 上层用：校验"这个窗口就是我嵌的" —— 只认 window 引用，不认插件名。
   *
   * 每次调用都现问 `frames()` 而不是吃一份快照：内嵌的 iframe 可能在 `serve()`
   * 之后才挂载（image-viewer 的 `#extension-frame` 就是），吃快照会永远认不出它。
   */
  function ownFrame(frames) {
    return function(w) {
      var list;
      try { list = (typeof frames === 'function' ? frames() : frames) || []; }
      catch (e) { return false; }
      for (var i = 0; i < list.length; i++) {
        var frame = list[i];
        if (!frame) continue;
        try {
          if (frame.contentWindow === w) return true;
        } catch (e) { /* 跨域读 contentWindow 不抛，但保险起见不影响判断 */ }
      }
      return false;
    };
  }

  /** 上层用：表态"我接得住子插件的请求"。不调用就等于没有这个能力。 */
  function serve(options) {
    var opts = options || {};
    var isOwnFrame = typeof opts.isOwnFrame === 'function' ? opts.isOwnFrame : function() { return false; };
    var handlers = opts.handlers || { ui: defaultUiHandler };
    // 子插件可以把按钮挂到这些容器里（见 mountButtons）：key 是容器 id 或自定义标签
    var containers = opts.containers || {};

    window.addEventListener('message', function(event) {
      var msg = event.data;
      if (!msg || typeof msg.type !== 'string') return;
      if (!isOwnFrame(event.source)) return;

      if (msg.type === PROBE) {
        post(event.source, { type: REPLY, exchange: 'probe', ok: true });
        return;
      }
      if (msg.type === MOUNT) {
        try {
          mountButtons(event.source, msg.data || {}, containers);
          post(event.source, { type: REPLY, exchange: msg.exchange, ok: true, data: null });
        } catch (e) {
          post(event.source, { type: REPLY, exchange: msg.exchange, ok: false, data: { error: String((e && e.message) || e) } });
        }
        return;
      }
      if (msg.type !== REQUEST) return;

      var exchange = msg.exchange;
      var handler = handlers[msg.action];
      if (typeof handler !== 'function') {
        post(event.source, { type: REPLY, exchange: exchange, ok: false, data: { error: 'unsupported:' + msg.action } });
        return;
      }
      Promise.resolve()
        .then(function() { return handler(msg.data || {}, event.source); })
        .then(function(result) {
          // 处理器返回函数 = "这次请求要多个回合，值交给子插件自己处理"。
          // 弹窗必须等子插件回调结束后再关，否则保存失败时用户看不到原因。
          if (typeof result === 'function') {
            return result(function(values) { return invoke(event.source, msg.action, values); });
          }
          return result;
        })
        .then(function(result) {
          post(event.source, { type: REPLY, exchange: exchange, ok: true, data: result === undefined ? null : result });
        })
        .catch(function(err) {
          post(event.source, { type: REPLY, exchange: exchange, ok: false, data: { error: String((err && err.message) || err) } });
        });
    });
  }

  /** 默认 `ui` 处理器：弹窗由**上层文档**渲染，但 schema 与保存都归子插件。 */
  function defaultUiHandler(data, source) {
    var kind = data && data.kind;
    if (kind === 'settings') {
      var schema = Array.isArray(data.schema) ? data.schema : [];
      // 返一个"先回信、再开弹窗"的处理器：回信必须在弹窗之前（见 requestSettings 的注释），
      // 而回信要等处理器的 promise 结算 —— 所以这里不能直接返回弹窗的 promise。
      return function(commit) {
        setTimeout(function() {
          openModal({
            title: data.title || '设置',
            schema: schema,
            values: data.values || {},
            emptyText: '该插件暂无设置项',
            // 保存的成败由子插件回传；抛错时 openModal 会保住弹窗并提示原因
            onSave: function(values) { return commit(values); },
          }).catch(function(err) {
            console.error('[OmniBox] 宿主弹窗渲染失败:', err);
            if (window.Toast) Toast.error('设置弹窗打开失败');
          });
        }, 0);
        return true;
      };
    }
    if (kind === 'confirm') {
      return confirmDialog(data.message || '', { danger: !!data.danger });
    }
    // 未知 kind 一律显式失败，调用方据此回落 —— 静默当成功会让子插件以为操作生效了
    return Promise.reject(new Error('unsupported-kind:' + kind));
  }

  return {
    request: request,
    requestSettings: requestSettings,
    mountToolbar: mountToolbar,
    probe: probe,
    onCallback: onCallback,
    serve: serve,
    ownFrame: ownFrame,
    invoke: invoke,
  };
})();

// ==================== 树组件 ====================
function createTree(container, options = {}) {
  const icon = Utils.iconHtml(options.icon || 'icon:folder');
  let selectedLabel = null;

  function renderNode(item, depth) {
    const itemDiv = document.createElement('div');
    itemDiv.className = 'tree-item';

    const label = document.createElement('div');
    label.className = 'tree-label';
    label.style.paddingLeft = (depth * 16 + 10) + 'px';

    const arrow = document.createElement('span');
    arrow.className = 'tree-arrow collapsed';
    // 折叠态由 CSS 的 rotate(-90deg) 表现（base.css 的 .tree-arrow.collapsed），
    // 这里只放图标本体；.tree-arrow 的字号决定图标尺寸。
    arrow.innerHTML = Utils.iconHtml('icon:chevron-down');

    const iconSpan = document.createElement('span');
    iconSpan.className = 'tree-icon';
    // icon 已经是 Utils.iconHtml 产出的安全标记（非 `icon:` 值在内部走 escapeHtml），
    // 所以这里写 innerHTML：写 textContent 会把标记当纯文本显示出来。
    iconSpan.innerHTML = icon;

    const nameSpan = document.createElement('span');
    nameSpan.className = 'tree-name';
    nameSpan.textContent = item.name;

    label.append(arrow, iconSpan, nameSpan);
    itemDiv.appendChild(label);

    const childrenDiv = document.createElement('div');
    childrenDiv.className = 'tree-children';
    itemDiv.appendChild(childrenDiv);

    label.addEventListener('click', async (e) => {
      e.stopPropagation();
      if (selectedLabel) selectedLabel.classList.remove('active');
      label.classList.add('active');
      selectedLabel = label;
      if (options.onClick) options.onClick(item);

      if (arrow.classList.contains('collapsed')) {
        arrow.classList.remove('collapsed');
        if (!childrenDiv.dataset.loaded) {
          if (options.onLoadChildren) {
            childrenDiv.innerHTML = '<div class="loading">加载中...</div>';
            try {
              const children = await options.onLoadChildren(item.path);
              childrenDiv.innerHTML = '';
              if (children.length === 0) {
                arrow.style.visibility = 'hidden';
              } else {
                children.forEach(child => childrenDiv.appendChild(renderNode(child, depth + 1)));
              }
              childrenDiv.dataset.loaded = 'true';
            } catch (err) {
              childrenDiv.innerHTML = '<div class="loading">加载失败</div>';
              arrow.classList.add('collapsed');
              return;
            }
          }
        }
        childrenDiv.classList.add('expanded');
      } else {
        arrow.classList.add('collapsed');
        childrenDiv.classList.remove('expanded');
      }
    });

    return itemDiv;
  }

  if (options.data) {
    options.data.forEach(item => container.appendChild(renderNode(item, 0)));
  }

  return { selectPath(path) { /* 简化实现 */ } };
}

// ==================== 灯箱组件 ====================
function createLightbox(options = {}) {
  const getImageUrl = options.getImageUrl || ((item) => item.url);
  const labels = Object.assign({
    dialog: '图片查看器',
    image: '原图查看',
    previous: '上一张',
    next: '下一张',
    close: '关闭',
    counter: null,
  }, options.labels || {});
  const counterEnabled = options.counter === true;
  const advanceOnClick = options.advanceOnClick === true || options.advance === true;
  const infoProvider = typeof options.infoProvider === 'function'
    ? options.infoProvider : null;
  const labelText = (value, fallback) => {
    const text = typeof value === 'string' && value ? value : fallback;
    if (typeof Utils.escapeHtml === 'function') return Utils.escapeHtml(text);
    return String(text).replace(/&/g, '&amp;').replace(/</g, '&lt;')
      .replace(/>/g, '&gt;').replace(/"/g, '&quot;').replace(/'/g, '&#39;');
  };

  const overlay = document.createElement('div');
  overlay.className = 'lightbox';
  overlay.tabIndex = -1;
  overlay.innerHTML = `
    <div class="lightbox-arrow left" role="button" tabindex="0" aria-label="${labelText(labels.previous, '上一张')}">${Utils.iconHtml('icon:chevron-left')}</div>
    <img id="lightbox-img" src="" alt="${labelText(labels.image, '原图查看')}" draggable="false">
    <div class="lightbox-arrow right" role="button" tabindex="0" aria-label="${labelText(labels.next, '下一张')}">${Utils.iconHtml('icon:chevron-right')}</div>
    <div class="lightbox-close" role="button" tabindex="0" aria-label="${labelText(labels.close, '关闭')}">${Utils.iconHtml('icon:x')}</div>
    <div class="lightbox-info" aria-live="polite"></div>
  `;
  if (typeof overlay.setAttribute === 'function') {
    overlay.setAttribute('role', 'dialog');
    overlay.setAttribute('aria-modal', 'true');
    overlay.setAttribute('aria-label', options.label
      || (typeof labels.dialog === 'string' && labels.dialog) || '图片查看器');
  }
  document.body.appendChild(overlay);

  const img = overlay.querySelector('#lightbox-img');
  const leftArrow = overlay.querySelector('.lightbox-arrow.left');
  const rightArrow = overlay.querySelector('.lightbox-arrow.right');
  const closeBtn = overlay.querySelector('.lightbox-close');
  const infoEl = overlay.querySelector('.lightbox-info');
  let counterEl = null;
  if (counterEnabled) {
    counterEl = document.createElement('div');
    counterEl.className = 'lightbox-counter';
    counterEl.setAttribute('aria-live', 'polite');
    overlay.appendChild(counterEl);
  }
  let previousFocus = null;

  let scale = 1, translate = { x: 0, y: 0 };
  let isDragging = false, dragStart = { x: 0, y: 0 }, dragDistance = 0;
  let suppressClick = false;
  let currentIndex = -1, items = [];
  //: 灯箱是否处于打开态。`isOpen()` 与 `onClose` 让插件能在壳返回/导航时先关灯箱，
  //: 而不是靠猜 class 或重复调用 hide()。
  let opened = false;
  let infoToken = 0;

  // ===== 解码位图内存缓存（"看过的图再打开瞬间整张出现"）=====
  //
  // 为什么需要它：客户端持有文件字节 ≠ 显示时不卡。实测同一张 10 MB / 数千像素的
  // 原图，重复打开会出现两种表现 —— 有时整张瞬出，有时**从上到下一行行刷**。后者是
  // 渲染进程把**解码后的位图**丢掉了，于是重新解码并渐进绘制。文件字节那层（HTTP
  // 缓存 + 系统页缓存）是无辜的：请求可能只回一个 304，但位图仍要重建。
  //
  // 保住位图的唯一现实做法是**让持有它的 <img> 元素活着**：位图的寿命跟着元素走，
  // 元素被回收位图就没了。灯箱本身只用一个 <img>，换 src 就等于丢掉上一张的位图，
  // 所以这里额外养一小池离屏 <img>，只负责把最近看过的位图留在渲染进程里。
  //
  // 代价按**像素**算而不是文件大小：单张 ≈ 宽 × 高 × 4 字节（4000×4000 ≈ 64 MB，
  // 6000×6000 ≈ 144 MB），且渲染进程往往还留一份 GPU 副本。因此上限必须可调，
  // 0 = 关闭。
  const decodedPool = new Map();     // url → { el, at }
  const KEEP_RECENT_MS = 20000;      // 只留"刚看过"的，不按张数硬顶
  let decodedMax = 8;

  function itemUrl(item) {
    return item ? Bridge.originalUrl(getImageUrl(item)) : '';
  }

  function trimDecodedPool() {
    const now = Date.now();
    decodedPool.forEach((entry, url) => {
      if (decodedPool.size <= decodedMax && (now - entry.at) < KEEP_RECENT_MS) return;
      decodedPool.delete(url);       // 丢掉引用 → 元素与其位图一起被回收
    });
  }

  function setDecodedLimit(value) {
    const parsed = Math.floor(Number(value));
    decodedMax = Number.isFinite(parsed) && parsed > 0 ? parsed : 0;
    if (decodedMax === 0) decodedPool.clear();
    else trimDecodedPool();
  }

  /** 把当前这张的位图留在渲染进程里（池满时先淘汰最久未用的）。 */
  function retainDecoded() {
    if (decodedMax <= 0) return;
    const url = img.currentSrc || img.src;
    if (!url) return;
    const hit = decodedPool.get(url);
    if (hit) hit.at = Date.now();
    else decodedPool.set(url, { el: img.cloneNode(false), at: Date.now() });
    while (decodedPool.size > decodedMax) {
      const oldest = decodedPool.keys().next().value;
      decodedPool.delete(oldest);
    }
  }

  /**
   * 缩放后的平移夹取：放大态不允许把图拖到视口外，避免"超大图溢出看不到边"。
   * 小图在 scale=1 时保持原尺寸居中，不主动放大（CSS 只限制 max，不设 width）。
   */
  function clampTranslate() {
    if (scale <= 1) {
      translate = { x: 0, y: 0 };
      return;
    }
    const width = Number(img.offsetWidth) || 0;
    const height = Number(img.offsetHeight) || 0;
    const viewW = Number(window.innerWidth) || 0;
    const viewH = Number(window.innerHeight) || 0;
    if (!width || !height || !viewW || !viewH) return;
    const maxX = Math.max(0, (width * scale - viewW) / 2);
    const maxY = Math.max(0, (height * scale - viewH) / 2);
    translate.x = Math.min(maxX, Math.max(-maxX, translate.x));
    translate.y = Math.min(maxY, Math.max(-maxY, translate.y));
  }

  function resetTransform() { scale = 1; translate = { x: 0, y: 0 }; applyTransform(); }
  function applyTransform() {
    clampTranslate();
    img.style.transform = `translate(${translate.x}px, ${translate.y}px) scale(${scale})`;
    img.style.cursor = scale > 1 ? 'grab' : 'zoom-out';
  }

  function counterText() {
    const current = currentIndex + 1;
    const total = items.length;
    if (typeof labels.counter === 'function') return String(labels.counter(current, total));
    if (typeof labels.counter === 'string' && labels.counter) {
      return labels.counter
        .replace(/\{current\}/g, String(current))
        .replace(/\{total\}/g, String(total));
    }
    return `${current} / ${total}`;
  }

  function updateCounter() {
    if (!counterEl) return;
    counterEl.textContent = items.length ? counterText() : '';
  }

  /** `infoProvider` 的返回值：字符串/数组/{items}/{text}；只用 textContent，不接收 HTML。 */
  function paintInfo(value) {
    if (!infoEl) return;
    infoEl.textContent = '';
    if (value === null || value === undefined || value === '') return;
    let entries;
    if (Array.isArray(value)) entries = value;
    else if (typeof value === 'object' && Array.isArray(value.items)) entries = value.items;
    else if (typeof value === 'object' && value.text !== undefined) entries = [value.text];
    else entries = [value];
    entries.forEach((entry) => {
      if (entry === null || entry === undefined || entry === '') return;
      const row = document.createElement('div');
      row.className = 'lightbox-info-item';
      row.textContent = String(entry);
      infoEl.appendChild(row);
    });
  }

  function updateInfo() {
    const item = items[currentIndex];
    const token = ++infoToken;
    if (!item) {
      infoEl.innerHTML = '';
      return;
    }
    // 新选项：信息由调用方给（漫画单页阅读、任意查看器），不耦合 image-viewer 的
    // get_image_info；支持同步返回或 Promise。
    if (infoProvider) {
      let result;
      try {
        result = infoProvider(item, currentIndex, items);
      } catch (e) {
        infoEl.textContent = '';
        return;
      }
      Promise.resolve(result).then((value) => {
        if (token !== infoToken) return;
        paintInfo(value);
      }).catch(() => {
        if (token === infoToken) infoEl.textContent = '';
      });
      return;
    }
    // 旧路径保持不变：item 自带尺寸直接显示，否则走桥的 get_image_info。
    const path = getImageUrl(item);
    if (item.size || item.width || item.height) {
      const sizeText = item.size ? Utils.formatFileSize(item.size) : '';
      const resolutionText = (item.width && item.height) ? `${item.width} × ${item.height}` : '';
      // size/width/height 来自插件或后端数据，不是本组件的输入：只走 textContent
      // （paintInfo），拼进 innerHTML 时值里的一个引号就能在这里插标记。
      paintInfo([sizeText, resolutionText].filter(Boolean));
      return;
    }

    infoEl.innerHTML = '<div class="lightbox-info-item">读取中…</div>';
    if (typeof Bridge.call !== 'function') return;
    Bridge.call('get_image_info', path).then(data => {
      if (token !== infoToken) return;
      if (currentIndex < 0 || !items[currentIndex]) return;
      if (getImageUrl(items[currentIndex]) !== path) return;
      if (data && data.success) {
        item.size = data.size;
        item.width = data.width;
        item.height = data.height;
        paintInfo([
          Utils.formatFileSize(data.size),
          (data.width != null && data.height != null) ? `${data.width} × ${data.height}` : '',
        ].filter(Boolean));
      } else {
        infoEl.innerHTML = '<div class="lightbox-info-item">无信息</div>';
      }
    }).catch(() => {
      if (token === infoToken) infoEl.innerHTML = '<div class="lightbox-info-item">无信息</div>';
    });
  }

  function show(itemList, index) {
    items = itemList; currentIndex = index;
    opened = true;
    previousFocus = _rememberFocus();
    img.src = Bridge.originalUrl(getImageUrl(items[currentIndex]));
    overlay.classList.add('active');
    updateInfo();
    updateCounter();
    resetTransform();
    retainDecoded();
    document.addEventListener('keydown', onKey);
    img.addEventListener('wheel', onWheel, { passive: false });
    img.addEventListener('mousedown', onDragStart);
    img.addEventListener('click', onImageClick);
    document.addEventListener('mousemove', onDragMove);
    document.addEventListener('mouseup', onDragEnd);
    // 旧行为：初始焦点给关闭按钮。漫画单页阅读（advanceOnClick）把焦点给灯箱本身，
    // 这样 Enter 是“下一页”，Esc 仍是关闭；Tab 再进入左右/关闭控件。
    const initial = advanceOnClick ? overlay : closeBtn;
    if (initial && typeof initial.focus === 'function') {
      try { initial.focus({ preventScroll: true }); } catch (e) { /* 桩/旧浏览器无 focus */ }
    }
  }

  function hide() {
    const wasActive = overlay.classList.contains('active');
    const wasOpen = opened;
    opened = false;
    overlay.classList.remove('active'); img.src = '';
    infoEl.innerHTML = '';
    infoToken += 1;
    decodedPool.clear();
    document.removeEventListener('keydown', onKey);
    img.removeEventListener('wheel', onWheel);
    img.removeEventListener('mousedown', onDragStart);
    img.removeEventListener('click', onImageClick);
    document.removeEventListener('mousemove', onDragMove);
    document.removeEventListener('mouseup', onDragEnd);
    if (wasActive) {
      _restoreFocus(previousFocus);
      previousFocus = null;
    }
    // 只有"真的从打开态关闭"才回调：重复 hide()（插件清理时常见）不该重复触发。
    if (wasOpen && typeof options.onClose === 'function') {
      try {
        options.onClose();
      } catch (e) {
        console.error('[OmniBox] 灯箱 onClose 回调抛异常:', e);
      }
    }
  }

  function navigate(dir) {
    if (!items.length) return;
    currentIndex += dir;
    if (currentIndex < 0) currentIndex = items.length - 1;
    if (currentIndex >= items.length) currentIndex = 0;
    img.src = Bridge.originalUrl(getImageUrl(items[currentIndex]));
    updateInfo();
    updateCounter();
    resetTransform();
    retainDecoded();
  }

  function onImageClick() {
    if (!advanceOnClick) return;
    if (suppressClick) {
      suppressClick = false;
      return;
    }
    // 放大态下的点击用于拖拽/查看局部，不翻页，避免误触。
    if (isDragging || scale > 1) return;
    navigate(1);
  }

  function onKey(e) {
    if (!overlay.classList.contains('active')) return;
    if (e.key === 'Tab') {
      _trapTab(overlay, e);
      return;
    }
    if (e.key === 'Escape') hide();
    if (e.key === 'ArrowLeft') navigate(-1);
    if (e.key === 'ArrowRight') navigate(1);
    if (e.key === 'Enter' && advanceOnClick) {
      e.preventDefault();
      navigate(1);
    }
  }
  function onWheel(e) {
    e.preventDefault();
    const delta = e.deltaY > 0 ? -0.1 : 0.1;
    scale = Math.max(0.5, Math.min(5, scale + delta));
    applyTransform();
  }
  function onDragStart(e) {
    if (scale <= 1) return;
    e.preventDefault(); isDragging = true;
    dragDistance = 0;
    dragStart.x = e.clientX - translate.x;
    dragStart.y = e.clientY - translate.y;
    img.style.cursor = 'grabbing';
  }
  function onDragMove(e) {
    if (!isDragging) return;
    const nextX = e.clientX - dragStart.x;
    const nextY = e.clientY - dragStart.y;
    dragDistance += Math.abs(nextX - translate.x) + Math.abs(nextY - translate.y);
    translate.x = nextX;
    translate.y = nextY;
    applyTransform();
  }
  function onDragEnd() {
    if (!isDragging) return;
    isDragging = false;
    if (dragDistance > 3) suppressClick = true;
    img.style.cursor = scale > 1 ? 'grab' : 'zoom-out';
  }

  overlay.addEventListener('pointerdown', (e) => { if (e.target === overlay) hide(); });
  closeBtn.addEventListener('click', hide);
  leftArrow.addEventListener('click', (e) => { e.stopPropagation(); navigate(-1); });
  rightArrow.addEventListener('click', (e) => { e.stopPropagation(); navigate(1); });
  // role=button 的 div 没有原生键盘激活，补 Enter/Space。
  [closeBtn, leftArrow, rightArrow].forEach((el) => {
    el.addEventListener('keydown', (e) => {
      if (e.key !== 'Enter' && e.key !== ' ') return;
      e.preventDefault();
      e.stopPropagation();
      el.click();
    });
  });

  /**
   * 就地替换浏览序列（列表随后异步补齐了更完整的序列时用）。
   *
   * `items` 与 `currentIndex` 是本组件的闭包状态，外部改不了，所以必须由这里换：
   * 插件拿到完整序列后调它，并给出 `focusUrl`（当前正在看的那张）—— 新序列里
   * 找到同一张就停在原地，找不到才退回 `fallbackIndex`。这样"补齐序列"对用户
   * 是透明的：图不闪、下标不乱跳，只是左右翻能翻到更多张。
   */
  function setItems(nextItems, focusUrl, fallbackIndex) {
    items = Array.isArray(nextItems) ? nextItems : [];
    let index = -1;
    if (focusUrl) index = items.findIndex(item => getImageUrl(item) === focusUrl);
    if (index < 0) index = Math.min(Math.max(Number(fallbackIndex) || 0, 0), items.length - 1);
    currentIndex = Math.max(index, 0);
    const item = items[currentIndex];
    if (!overlay.classList.contains('active') || !item) return;
    img.src = Bridge.originalUrl(getImageUrl(item));
    resetTransform();
    updateInfo();
    updateCounter();
    retainDecoded();
  }

  return {
    show, hide, navigate, setItems, setDecodedLimit,
    // 打开态查询：插件在壳返回/导航时先判断"要不要关灯箱"，不必自己记状态。
    isOpen: () => opened,
    // 只读：当前保留了几个解码位图。给用例一个可观测点 —— 池子是闭包状态，
    // 没有它就只能靠"数 cloneNode 调用"这类脆弱手法去间接推断。
    getDecodedCount: () => decodedPool.size,
    getIndex: () => currentIndex,
    getCount: () => items.length,
  };
}

// ==================== 分页组件 ====================
function createPagination(container, options = {}) {
  if (container && typeof container.setAttribute === 'function') {
    if (!container.getAttribute('role')) container.setAttribute('role', 'navigation');
    if (!container.getAttribute('aria-label')) container.setAttribute('aria-label', options.label || '分页');
  }
  // 插件常在 render 之前自己 `container.innerHTML = ''`（image-viewer 就是这样），
  // 焦点会先掉到 body。记住容器内最后聚焦的链接，用它判断“这次重绘该不该还焦点”。
  let lastFocusedLink = null;
  if (container && typeof container.addEventListener === 'function') {
    container.addEventListener('focusin', (event) => {
      if (event.target && container.contains && container.contains(event.target)) {
        lastFocusedLink = event.target;
      }
    });
  }

  function render(currentPage, totalPages) {
    const active = document.activeElement;
    const hadFocus = !!(container && typeof container.contains === 'function' && (
      container.contains(active)
      || (lastFocusedLink && lastFocusedLink.isConnected === false
          && (!active || active === document.body))
    ));
    container.innerHTML = '';
    if (totalPages <= 1) return;
    const delta = 2, pages = [];
    for (let i = 1; i <= totalPages; i++) {
      if (i === 1 || i === totalPages || (i >= currentPage - delta && i <= currentPage + delta)) {
        pages.push(i);
      }
    }
    container.appendChild(makeLink('«', 1, currentPage === 1, false, '第一页'));
    container.appendChild(makeLink('‹', currentPage - 1, currentPage === 1, false, '上一页'));
    let last = 0;
    pages.forEach(page => {
      if (last + 1 < page) {
        const ellipsis = document.createElement('span');
        ellipsis.className = 'ellipsis'; ellipsis.textContent = '...';
        ellipsis.setAttribute('aria-hidden', 'true');
        container.appendChild(ellipsis);
      }
      container.appendChild(makeLink(page, page, false, page === currentPage, `第 ${page} 页`));
      last = page;
    });
    container.appendChild(makeLink('›', currentPage + 1, currentPage === totalPages, false, '下一页'));
    container.appendChild(makeLink('»', totalPages, currentPage === totalPages, false, '最后一页'));
    if (hadFocus) {
      const current = container.querySelector('[aria-current="page"]')
        || container.querySelector('a:not(.disabled)');
      if (current && typeof current.focus === 'function') {
        try { current.focus({ preventScroll: true }); } catch (e) { /* 旧浏览器 */ }
        lastFocusedLink = current;
      }
    }
  }

  function makeLink(text, page, isDisabled, isCurrent, label) {
    const el = document.createElement('a');
    el.textContent = text;
    el.setAttribute('aria-label', label || text);
    if (isCurrent) {
      el.classList.add('current');
      el.setAttribute('aria-current', 'page');
      el.tabIndex = -1;
      el.setAttribute('href', '#');
    } else if (isDisabled) {
      el.classList.add('disabled');
      el.setAttribute('aria-disabled', 'true');
      el.tabIndex = -1;
    } else {
      el.setAttribute('href', '#');
      el.addEventListener('click', (event) => {
        if (event && typeof event.preventDefault === 'function') event.preventDefault();
        if (options.onPageChange) options.onPageChange(page);
      });
      // 分页链接是 a[href]：Enter 原生可用；补 Space 与 button 一致。
      el.addEventListener('keydown', (event) => {
        if (event.key !== ' ' && event.key !== 'Spacebar') return;
        event.preventDefault();
        el.click();
      });
    }
    return el;
  }

  return { render };
}

// ==================== 右键菜单组件 ====================
function createContextMenu(options = {}) {
  const menu = document.createElement('ul');
  menu.className = 'context-menu';
  menu.setAttribute('role', 'menu');
  menu.setAttribute('aria-label', options.label || '操作菜单');
  menu.tabIndex = -1;
  let previousFocus = null;

  function decorate() {
    Array.from(menu.children || []).forEach((li) => {
      if (!li || !li.tagName) return;
      const separator = String(li.className || '').includes('nr-menu-sep')
        || !(li.dataset && li.dataset.action);
      if (separator) {
        li.setAttribute('role', 'separator');
        li.setAttribute('aria-hidden', 'true');
        li.tabIndex = -1;
      } else {
        li.setAttribute('role', 'menuitem');
        li.tabIndex = -1;
      }
    });
  }

  (options.items || []).forEach(item => {
    const li = document.createElement('li');
    li.textContent = item.label;
    li.dataset.action = item.action;
    li.setAttribute('role', 'menuitem');
    li.tabIndex = -1;
    if (item.danger) li.classList.add('danger');
    li.addEventListener('click', () => {
      hide(false);
      if (options.onSelect) options.onSelect(item.action);
    });
    menu.appendChild(li);
  });
  document.body.appendChild(menu);

  function hide(restoreFocus) {
    if (menu.style.display === 'none') return;
    menu.style.display = 'none';
    if (restoreFocus) {
      _restoreFocus(previousFocus);
      previousFocus = null;
    }
  }

  function show(x, y, targetData) {
    previousFocus = _rememberFocus();
    decorate();
    menu._targetData = targetData;
    menu.style.display = 'block';
    menu.style.left = '0px';
    menu.style.top = '0px';
    // 视口夹取：先量实际尺寸再定位，右/下边缘溢出时翻到指针的左侧/上方。
    const rect = typeof menu.getBoundingClientRect === 'function'
      ? menu.getBoundingClientRect() : { width: 0, height: 0 };
    const margin = 8;
    const vw = Number(window.innerWidth) || 0;
    const vh = Number(window.innerHeight) || 0;
    let left = Number(x) || 0;
    let top = Number(y) || 0;
    if (vw > 0 && rect.width > 0) {
      left = Math.min(left, Math.max(margin, vw - rect.width - margin));
      left = Math.max(margin, left);
    }
    if (vh > 0 && rect.height > 0) {
      if (top + rect.height + margin > vh) top = top - rect.height;
      top = Math.min(top, Math.max(margin, vh - rect.height - margin));
      top = Math.max(margin, top);
    }
    menu.style.left = `${Math.round(left)}px`;
    menu.style.top = `${Math.round(top)}px`;
    const first = menu.querySelector('li[role="menuitem"]');
    if (first && typeof first.focus === 'function') {
      try { first.focus({ preventScroll: true }); } catch (e) { /* 旧浏览器 */ }
    } else if (typeof menu.focus === 'function') {
      try { menu.focus({ preventScroll: true }); } catch (e) { /* 旧浏览器 */ }
    }
  }

  menu.addEventListener('keydown', (event) => {
    const items = Array.from(menu.querySelectorAll('li[role="menuitem"]'));
    const active = document.activeElement;
    const index = items.indexOf(active);
    if (event.key === 'Escape') {
      event.preventDefault();
      event.stopPropagation();
      hide(true);
      return;
    }
    if (event.key === 'ArrowDown' || event.key === 'ArrowUp') {
      event.preventDefault();
      if (!items.length) return;
      const step = event.key === 'ArrowDown' ? 1 : -1;
      const next = index < 0
        ? (step > 0 ? 0 : items.length - 1)
        : (index + step + items.length) % items.length;
      try { items[next].focus(); } catch (e) { /* 元素已失效 */ }
      return;
    }
    if (event.key === 'Home' || event.key === 'End') {
      event.preventDefault();
      if (!items.length) return;
      const target = event.key === 'Home' ? items[0] : items[items.length - 1];
      try { target.focus(); } catch (e) { /* 元素已失效 */ }
      return;
    }
    if (event.key === 'Enter' || event.key === ' ') {
      if (active && active.getAttribute && active.getAttribute('role') === 'menuitem') {
        event.preventDefault();
        active.click();
      }
    }
  });

  // 点外部关闭；点菜单项由上面的 li/插件处理器自行执行动作。
  document.addEventListener('pointerdown', (event) => {
    if (menu.style.display !== 'block') return;
    if (event.target && menu.contains && menu.contains(event.target)) return;
    hide(true);
  });
  menu.addEventListener('click', (event) => {
    if (event.target && event.target.closest && event.target.closest('li')) hide(false);
  });

  function getTargetData() { return menu._targetData; }

  return { show, hide, getTargetData, element: menu };
}

// 注：这里曾有一个 `createCardGrid()`，实际采用数为 0，且它渲染的
// `.manga-card / .manga-cover / .manga-info …` 在壳与任何插件样式里都没有定义 ——
// 它是某个插件旧实现的视觉词汇被搬进"共享"组件，谁用谁拿到无样式 DOM，已删除。
// 卡片网格属各插件的内容区布局（image-viewer 用瀑布流、manga-library / media-player
// 用自适应栅格），由插件自建；见 docs/plugin-ui-guide.md §5。

