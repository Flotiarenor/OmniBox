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
// 伪造 —— 宿主再内嵌一层 frame 时 origin 完全相同（docs/core-contract-fixes.md §3.4.c）。
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

// ==================== 通用扩展入口渲染 ====================
// 宿主插件只需提供一个容器，并声明 host + placement：
//   renderExtensions(document.getElementById('extensions'), 'image-viewer', 'sidebar');
// 扩展插件通过 get_extensions() 注册到 Shell 后，会自动渲染到该容器。
function renderExtensions(container, host, placement, options = {}) {
  if (!container) return Promise.resolve();
  container.innerHTML = '';

  return Bridge.callSystem('system_get_plugin_extensions', host, placement)
    .then(list => {
      if (!Array.isArray(list) || list.length === 0) return;

      // 分组渲染：扩展可带 section 声明自己的区块标题（如「相册清理」/「Pixiv 同步」），
      // 没有 section 的扩展归入 options.title 区块（兼容旧宿主）。
      const groups = new Map();
      for (const ext of list) {
        const key = ext.section || options.title || '';
        if (!groups.has(key)) groups.set(key, []);
        groups.get(key).push(ext);
      }

      for (const [key, exts] of groups) {
        const section = document.createElement('div');
        section.className = 'obx-extensions' + (options.className ? ' ' + options.className : '');

        const groupTitle = exts[0] && exts[0].section ? exts[0].section : (options.title || '');
        if (groupTitle) {
          const title = document.createElement('div');
          title.className = 'obx-extensions-title';
          title.textContent = groupTitle;
          section.appendChild(title);
        }

        exts.forEach(ext => {
          const btn = document.createElement('button');
          btn.type = 'button';
          btn.className = 'obx-extension' + (options.itemClass ? ' ' + options.itemClass : '');
          btn.title = ext.description || ext.label || ext.id || '';
          btn.innerHTML =
            `<span class="obx-extension-icon">${Utils.escapeHtml(ext.icon || '🧩')}</span>` +
            `<span class="obx-extension-label">${Utils.escapeHtml(ext.label || ext.id || '扩展')}</span>`;

          btn.addEventListener('click', () => {
            // 0. 原生视图型扩展：由宿主直接渲染，复用宿主 UI/播放器
            if (ext.view && typeof options.onOpen === 'function') {
              options.onOpen(ext, btn);
              return;
            }
            // 1. 内嵌型扩展：在宿主内部打开 iframe 面板
            if (ext.embedUrl) {
              if (typeof options.onEmbed === 'function') {
                options.onEmbed(ext, btn);
              } else if (options.embedContainer) {
                options.embedContainer.innerHTML = '';
                const frame = document.createElement('iframe');
                frame.src = ext.embedUrl;
                frame.className = 'obx-embed-frame';
                options.embedContainer.appendChild(frame);
              }
              return;
            }
            // 2. 独立路由型扩展：跳转到插件自身页面
            if (ext.route) {
              const nav = parent && parent.__omniboxNavigate;
              if (typeof nav === 'function') {
                nav(ext.route);
              } else if (parent) {
                parent.location.href = ext.route;
              }
              return;
            }
            // 3. 纯后端方法型扩展：跨插件调用
            if (ext.method && ext.plugin) {
              Bridge.callPlugin(ext.plugin, ext.method).catch(err => {
                console.error('扩展调用失败:', err);
                if (window.Toast) Toast.error('扩展调用失败');
              });
            }
          });

          section.appendChild(btn);
        });

        container.appendChild(section);
      }
    })
    .catch(err => {
      console.error('加载扩展失败:', err);
    });
}

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
  // 哪个 helper 适合属性、哪个只适合文本（docs/code-review.md §4.3）。
  escapeHtml(str) {
    if (str == null) return '';
    return String(str)
      .replace(/&/g, '&amp;')
      .replace(/</g, '&lt;')
      .replace(/>/g, '&gt;')
      .replace(/"/g, '&quot;')
      .replace(/'/g, '&#39;');
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
  function show(message, type = 'info', duration = 2600) {
    const el = document.createElement('div');
    el.className = `toast toast-${type}`;
    el.textContent = message;
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

// ==================== 确认对话框（替代原生 confirm） ====================
function confirmDialog(message, options = {}) {
  return new Promise((resolve) => {
    const overlay = document.createElement('div');
    overlay.className = 'modal active';
    overlay.innerHTML = `
      <div class="modal-box modal-confirm">
        <div class="modal-body">
          <div class="confirm-message">${Utils.escapeHtml(message)}</div>
        </div>
        <div class="modal-footer">
          <button class="btn" data-act="cancel">取消</button>
          <button class="btn ${options.danger ? 'btn-danger-solid' : 'btn-primary'}" data-act="ok">${options.okText || '确定'}</button>
        </div>
      </div>`;
    document.body.appendChild(overlay);
    const close = (val) => { overlay.remove(); resolve(val); };
    // pointerdown 而非 click：click 的 target 是按下/松开的最近公共祖先，
    // 在弹窗内按下、拖到遮罩上松开会被误判成"点了遮罩"。这里只看按下的位置。
    overlay.addEventListener('pointerdown', (e) => { if (e.target === overlay) close(false); });
    overlay.querySelector('[data-act="cancel"]').addEventListener('click', () => close(false));
    overlay.querySelector('[data-act="ok"]').addEventListener('click', () => close(true));
  });
}

// ==================== 设置表单（按 schema 渲染） ====================
function createSettingsForm(container, schema, values = {}) {
  const fieldEls = {};
  const baseId = 'cfg-' + Math.random().toString(36).slice(2, 8);

  (schema || []).forEach((field) => {
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
async function openSettingsModal(options = {}) {
  const title = options.title || '设置';
  let schema = options.schema;
  let values = options.values;
  const onSave = options.onSave;

  if (!schema) {
    try { schema = await Bridge.call('get_settings_schema'); } catch (e) { schema = []; }
  }
  if (values === undefined) {
    try { values = await Bridge.call('get_settings'); } catch (e) { values = {}; }
  }

  const overlay = document.createElement('div');
  overlay.className = 'modal active';
  overlay.innerHTML = `
    <div class="modal-box">
      <h3>${Utils.escapeHtml(title)}</h3>
      <div class="modal-body settings-form"></div>
      <div class="modal-footer">
        <button class="btn" data-act="cancel">取消</button>
        <button class="btn btn-primary" data-act="save">保存</button>
      </div>
    </div>`;
  document.body.appendChild(overlay);

  const body = overlay.querySelector('.modal-body');
  let form = null;
  if (!schema || schema.length === 0) {
    body.innerHTML = '<div class="empty-state" style="min-height:120px;">该插件暂无设置项</div>';
  } else {
    form = createSettingsForm(body, schema, values || {});
  }

  const close = () => overlay.remove();
  // pointerdown：按在遮罩上就关，按在弹窗内（哪怕拖到遮罩上松开）不关
  overlay.addEventListener('pointerdown', (e) => { if (e.target === overlay) close(); });
  overlay.querySelector('[data-act="cancel"]').addEventListener('click', close);

  overlay.querySelector('[data-act="save"]').addEventListener('click', async () => {
    const saveBtn = overlay.querySelector('[data-act="save"]');
    const cancelBtn = overlay.querySelector('[data-act="cancel"]');
    saveBtn.disabled = true;
    try {
      const newValues = form ? form.getValues() : {};
      let result = newValues;
      if (onSave) {
        result = await onSave(newValues);
      } else {
        result = await Bridge.call('save_settings', newValues);
      }
      if (result && result.success === false) {
        throw new Error(result.error || '保存失败');
      }
      close();
      Toast.success((options.successMessage) || (result && result.message) || '设置已保存');
      setTimeout(() => { window.location.href = window.location.href.split('?')[0] + '?_t=' + Date.now(); }, 400);
    } catch (e) {
      saveBtn.disabled = false;
      Toast.error(e.message || '保存失败');
    }
  });
}

// ==================== 树组件 ====================
function createTree(container, options = {}) {
  const icon = options.icon || '📁';
  let selectedLabel = null;

  function renderNode(item, depth) {
    const itemDiv = document.createElement('div');
    itemDiv.className = 'tree-item';

    const label = document.createElement('div');
    label.className = 'tree-label';
    label.style.paddingLeft = (depth * 16 + 10) + 'px';

    const arrow = document.createElement('span');
    arrow.className = 'tree-arrow collapsed';
    arrow.textContent = '▼';

    const iconSpan = document.createElement('span');
    iconSpan.className = 'tree-icon';
    iconSpan.textContent = icon;

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

  const overlay = document.createElement('div');
  overlay.className = 'lightbox';
  overlay.innerHTML = `
    <div class="lightbox-arrow left">❮</div>
    <img id="lightbox-img" src="" alt="原图查看" draggable="false">
    <div class="lightbox-arrow right">❯</div>
    <div class="lightbox-close">✕</div>
    <div class="lightbox-info"></div>
  `;
  document.body.appendChild(overlay);

  const img = overlay.querySelector('#lightbox-img');
  const leftArrow = overlay.querySelector('.lightbox-arrow.left');
  const rightArrow = overlay.querySelector('.lightbox-arrow.right');
  const closeBtn = overlay.querySelector('.lightbox-close');
  const infoEl = overlay.querySelector('.lightbox-info');

  let scale = 1, translate = { x: 0, y: 0 };
  let isDragging = false, dragStart = { x: 0, y: 0 };
  let currentIndex = -1, items = [];

  function resetTransform() { scale = 1; translate = { x: 0, y: 0 }; applyTransform(); }
  function applyTransform() {
    img.style.transform = `translate(${translate.x}px, ${translate.y}px) scale(${scale})`;
    img.style.cursor = scale > 1 ? 'grab' : 'zoom-out';
  }

  function updateInfo() {
    const item = items[currentIndex];
    if (!item) {
      infoEl.innerHTML = '';
      return;
    }
    const path = getImageUrl(item);
    if (item.size || item.width || item.height) {
      const sizeText = item.size ? Utils.formatFileSize(item.size) : '';
      const resolutionText = (item.width && item.height) ? `${item.width} × ${item.height}` : '';
      infoEl.innerHTML =
        (sizeText ? `<div class="lightbox-info-item">${sizeText}</div>` : '') +
        (resolutionText ? `<div class="lightbox-info-item">${resolutionText}</div>` : '');
      return;
    }

    infoEl.innerHTML = '<div class="lightbox-info-item">读取中…</div>';
    if (typeof Bridge.call !== 'function') return;
    Bridge.call('get_image_info', path).then(data => {
      if (currentIndex < 0 || !items[currentIndex]) return;
      if (getImageUrl(items[currentIndex]) !== path) return;
      if (data && data.success) {
        item.size = data.size;
        item.width = data.width;
        item.height = data.height;
        infoEl.innerHTML =
          `<div class="lightbox-info-item">${Utils.formatFileSize(data.size)}</div>` +
          `<div class="lightbox-info-item">${data.width} × ${data.height}</div>`;
      } else {
        infoEl.innerHTML = '<div class="lightbox-info-item">无信息</div>';
      }
    }).catch(() => {
      infoEl.innerHTML = '<div class="lightbox-info-item">无信息</div>';
    });
  }

  function show(itemList, index) {
    items = itemList; currentIndex = index;
    img.src = Bridge.originalUrl(getImageUrl(items[currentIndex]));
    overlay.classList.add('active');
    updateInfo();
    resetTransform();
    document.addEventListener('keydown', onKey);
    img.addEventListener('wheel', onWheel, { passive: false });
    img.addEventListener('mousedown', onDragStart);
    document.addEventListener('mousemove', onDragMove);
    document.addEventListener('mouseup', onDragEnd);
  }

  function hide() {
    overlay.classList.remove('active'); img.src = '';
    infoEl.innerHTML = '';
    document.removeEventListener('keydown', onKey);
    img.removeEventListener('wheel', onWheel);
    img.removeEventListener('mousedown', onDragStart);
    document.removeEventListener('mousemove', onDragMove);
    document.removeEventListener('mouseup', onDragEnd);
  }

  function navigate(dir) {
    currentIndex += dir;
    if (currentIndex < 0) currentIndex = items.length - 1;
    if (currentIndex >= items.length) currentIndex = 0;
    img.src = Bridge.originalUrl(getImageUrl(items[currentIndex]));
    updateInfo();
    resetTransform();
  }

  function onKey(e) {
    if (e.key === 'Escape') hide();
    if (e.key === 'ArrowLeft') navigate(-1);
    if (e.key === 'ArrowRight') navigate(1);
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
    dragStart.x = e.clientX - translate.x;
    dragStart.y = e.clientY - translate.y;
    img.style.cursor = 'grabbing';
  }
  function onDragMove(e) {
    if (!isDragging) return;
    translate.x = e.clientX - dragStart.x;
    translate.y = e.clientY - dragStart.y;
    applyTransform();
  }
  function onDragEnd() {
    if (!isDragging) return;
    isDragging = false;
    img.style.cursor = scale > 1 ? 'grab' : 'zoom-out';
  }

  overlay.addEventListener('pointerdown', (e) => { if (e.target === overlay) hide(); });
  closeBtn.addEventListener('click', hide);
  leftArrow.addEventListener('click', (e) => { e.stopPropagation(); navigate(-1); });
  rightArrow.addEventListener('click', (e) => { e.stopPropagation(); navigate(1); });

  return { show, hide, navigate, getIndex: () => currentIndex };
}

// ==================== 分页组件 ====================
function createPagination(container, options = {}) {
  function render(currentPage, totalPages) {
    container.innerHTML = '';
    if (totalPages <= 1) return;
    const delta = 2, pages = [];
    for (let i = 1; i <= totalPages; i++) {
      if (i === 1 || i === totalPages || (i >= currentPage - delta && i <= currentPage + delta)) {
        pages.push(i);
      }
    }
    container.appendChild(makeLink('«', 1, currentPage === 1));
    container.appendChild(makeLink('‹', currentPage - 1, currentPage === 1));
    let last = 0;
    pages.forEach(page => {
      if (last + 1 < page) {
        const ellipsis = document.createElement('span');
        ellipsis.className = 'ellipsis'; ellipsis.textContent = '...';
        container.appendChild(ellipsis);
      }
      container.appendChild(makeLink(page, page, false, page === currentPage));
      last = page;
    });
    container.appendChild(makeLink('›', currentPage + 1, currentPage === totalPages));
    container.appendChild(makeLink('»', totalPages, currentPage === totalPages));
  }

  function makeLink(text, page, isDisabled, isCurrent) {
    const el = document.createElement('a');
    el.innerHTML = text;
    if (isCurrent) el.classList.add('current');
    if (isDisabled) el.classList.add('disabled');
    if (!isDisabled && !isCurrent) {
      el.addEventListener('click', () => { if (options.onPageChange) options.onPageChange(page); });
    }
    return el;
  }

  return { render };
}

// ==================== 右键菜单组件 ====================
function createContextMenu(options = {}) {
  const menu = document.createElement('ul');
  menu.className = 'context-menu';
  (options.items || []).forEach(item => {
    const li = document.createElement('li');
    li.textContent = item.label;
    li.dataset.action = item.action;
    if (item.danger) li.classList.add('danger');
    li.addEventListener('click', () => {
      menu.style.display = 'none';
      if (options.onSelect) options.onSelect(item.action);
    });
    menu.appendChild(li);
  });
  document.body.appendChild(menu);
  document.addEventListener('click', () => { menu.style.display = 'none'; });

  function show(x, y, targetData) {
    menu._targetData = targetData;
    menu.style.top = `${y}px`;
    menu.style.left = `${x}px`;
    menu.style.display = 'block';
  }
  function getTargetData() { return menu._targetData; }

  return { show, getTargetData, element: menu };
}

// 注：这里曾有一个 `createCardGrid()`，实际采用数为 0，且它渲染的
// `.manga-card / .manga-cover / .manga-info …` 在壳与任何插件样式里都没有定义 ——
// 它是某个插件旧实现的视觉词汇被搬进"共享"组件，谁用谁拿到无样式 DOM，已删除。
// 卡片网格属各插件的内容区布局（image-viewer 用瀑布流、manga-library / media-player
// 用自适应栅格），由插件自建；见 docs/plugin-ui-guide.md §5。
