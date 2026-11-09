/**
 * This product includes software developed by flotiarenor.Copyright 2026 flotiarenor
 * 共享运行时 · 基础层：插件生命周期钩子（onShow / onHide / onDispose）、内核 → 插件的消息接收、
 * Bridge（与后端通信）、Utils（转义 / 图标 / 文本工具）。
 *
 * 注入顺序由 shell/backend/file_server.py 的引导片段决定，这里只记约束：
 *   - 必须排在 toast.js 之后：插件脚本跟着共享脚本在 <head> 里一起跑，晚加载就会让
 *     先执行的插件代码拿不到 window.Toast。
 *   - 必须排在 folder-picker.js / freshness.js / content-ui.js 之前：它们按全局名读
 *     window.Utils / window.Bridge；其余分段（dialogs / settings / host-channel /
 *     tree / lightbox / pagination / context-menu）也建立在同一组全局名上。
 * 壳页面（Vue）不加载本文件：那边由 App.vue 给共享组件垫 window.Bridge / window.Utils。
 */

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
        console.error('[Cubby] on' + label + ' 钩子抛异常:', e);
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
      try { fn(); } catch (e) { console.error('[Cubby] onShow 钩子抛异常:', e); }
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
      try { fn(); } catch (e) { console.error('[Cubby] onHide 钩子抛异常:', e); }
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
      try { fn(); } catch (e) { console.error('[Cubby] onDispose 钩子抛异常:', e); }
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
  if (data.type === 'cubby:appearance') {
    var alpha = Number(data.alpha);
    if (alpha === alpha) {
      alpha = Math.max(0, Math.min(100, Math.round(alpha)));
      document.documentElement.style.setProperty('--plugin-bg-alpha', alpha + '%');
      document.documentElement.setAttribute('data-plugin-bg', 'on');
    }
    return;
  }
  if (data.type === 'cubby:settings-changed') {
    // 壳通知设置已变更：整页重载（插件自身状态由设置重新拉取）
    window.location.href = window.location.href.split('?')[0] + '?_t=' + Date.now();
    return;
  }
  if (data.type === 'cubby:plugin-shown') {
    window.PluginLifecycle.setVisible(true);
    return;
  }
  if (data.type === 'cubby:plugin-hidden') {
    window.PluginLifecycle.setVisible(false);
    return;
  }
  if (data.type === 'cubby:plugin-dispose') {
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
// 实现只有一份：/shell/toast.js，由插件引导片段在 base.js 之前注入（壳首页引的也是
// 同一份）。这里不再定义 window.Toast —— 两个文档各存一份实现时，去重、无障碍属性、
// 悬停暂停这类改动只能生效在一侧，另一侧看起来没变、行为已经分叉。
// 本文件里其余用到 Toast 的地方（设置弹窗的保存反馈）照旧按全局名调用。

