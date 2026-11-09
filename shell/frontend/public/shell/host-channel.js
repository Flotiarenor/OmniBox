/**
 * This product includes software developed by flotiarenor.Copyright 2026 flotiarenor
 * 共享运行时 · 宿主通道（HostChannel）：内嵌插件页请上层用自己的文档渲染 chrome，而不是
 * 自造一套 UI 或伸手读上层对象。
 *
 * 只搬数据不搬 DOM：内嵌 iframe 是独立文档，`.modal { position: fixed }` 只相对它自己那个
 * iframe，弹窗既盖不住宿主侧栏也没有整页遮罩。四条消息（request / probe / mount / callback）
 * 与各自的校验写在区段头的注释里；Bridge、openModal、confirmDialog 都是运行期调用。
 */

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
      else console.warn('[Cubby] 上层点了按钮但本页找不到它:', data.id);
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
      console.warn('[Cubby] 上层没有可挂载的工具栏，按钮留在本页:', res.data && res.data.error);
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
      console.error('[Cubby] 读取设置字段失败:', e);
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
            console.error('[Cubby] 宿主弹窗渲染失败:', err);
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

