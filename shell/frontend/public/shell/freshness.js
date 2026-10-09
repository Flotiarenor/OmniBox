// ==================== 统一「同步 / 校验」（Shell 共享组件） ====================
//
// 每个插件原先各写一套"刷新"：名字有 6 种（刷新 / 全量重建 / 重新扫描 / 扫描 /
// 深度扫描 / 刷新记录 / 校验内容），轮询、进度、取消、去抖各实现一遍，行为也各不
// 相同。本组件把**界面与编排**收成一份，插件只需要：
//
//   const ctl = Freshness.mount({ plugin: 'media-player', container: el, unit: '首',
//                                 onChange: () => this.reload() });
//   ctl.autoSync('');            // 进视图 / 切目录时调用（组件内部去抖，壳侧再节流）
//
// 三个动作的语义由壳的基建定义（`shell/backend/freshness.py`）：
//   - **同步**：被动、增量、便宜。由 `autoSync()` 触发，没有按钮。
//   - **校验**：手动、全量、彻底。唯一的按钮，走后台任务，有进度与取消。
//   - **重建**：逃生门（丢弃派生缓存），只在壳的设置页出现，这里不提供入口。
//
// 为什么必须走 `Bridge.callSystem`：这些是**壳级**方法（`system_freshness_*`），
// 而 `Bridge.call` 会自动带上插件前缀（`<plugin>__<method>`），打不到壳的方法上。
//
// 派生资源（缩略图等）的陈旧由 `Freshness.assetUrl()` 统一处理：壳的 `/thumbs`
// 带 `Cache-Control: max-age=86400` 且 URL 里没有版本号，不带版本就重建也不会变
// （media-player 曾单独为此打补丁，见 frontend/js/utils.js 的 `&v=` 注释）。
window.Freshness = (function () {
  const POLL_MS = 500;        // 校验进行中的轮询间隔（与 media-player 的扫描轮询同档）
  const SYNC_DEBOUNCE_MS = 400;
  const CARD_HIDE_MS = 4000;  // 完成后的进度卡停留时间

  let seq = 0;                // 版本号自增源（同一页面内单调）

  function icon(name) {
    return (window.Utils && Utils.iconHtml) ? Utils.iconHtml('icon:' + name) : '';
  }

  function pad(n) {
    return String(n).padStart(2, '0');
  }

  /** 时间戳 → "刚刚 / 12 分钟前 / 今天 14:03 / 昨天 14:03 / 2026-02-01" */
  function timeAgo(ts) {
    if (!ts) return '';
    const d = new Date(ts * 1000);
    const now = new Date();
    const diff = (now - d) / 1000;
    if (diff < 60) return '刚刚';
    if (diff < 3600) return `${Math.floor(diff / 60)} 分钟前`;
    const hm = `${pad(d.getHours())}:${pad(d.getMinutes())}`;
    const sameDay = (a, b) => a.getFullYear() === b.getFullYear()
      && a.getMonth() === b.getMonth() && a.getDate() === b.getDate();
    if (sameDay(d, now)) return `今天 ${hm}`;
    const yst = new Date(now.getTime() - 86400000);
    if (sameDay(d, yst)) return `昨天 ${hm}`;
    return `${d.getFullYear()}-${pad(d.getMonth() + 1)}-${pad(d.getDate())}`;
  }

  function toast(kind, text) {
    if (!window.Toast) return;
    const fn = Toast[kind] || Toast.show;
    if (typeof fn === 'function') fn.call(Toast, text);
  }

  function call(method, ...args) {
    // 壳级方法：不能用 Bridge.call（它会加上插件前缀）
    return Bridge.callSystem(method, ...args);
  }

  /**
   * 挂载一套「同步 / 校验」控件。
   *
   * opts:
   *   plugin   插件名（壳的基建按名字找引擎）
   *   container 挂到哪个元素里；省略则只返回节点由调用方自行插入
   *   unit     进度单位（"张 / 首 / 本"），仅用于文案
   *   onChange 每次"真有变化"或校验完成后的回调（插件据此重载自己的视图）
   *   scope    初始作用域（一般传当前目录，"" = 全部）
   *   compact  紧凑模式（工具栏里用）
   */
  function mount(opts) {
    const o = opts || {};
    const plugin = o.plugin || (window.Bridge && Bridge.pluginName) || '';
    const unit = o.unit || '项';
    const id = 'fr' + (++seq);

    const group = document.createElement('div');
    group.className = 'obx-fresh' + (o.compact ? ' obx-fresh-compact' : '');
    group.innerHTML = `
      <span class="obx-fresh-status" data-fr="status"></span>
      <button type="button" class="btn obx-fresh-verify" data-fr="verify"
              title="全量校验：重新遍历并逐项校对指纹，清理已消失的条目与失效缓存">
        ${icon('refresh-cw')} 校验
      </button>
      <div class="obx-fresh-card hidden" data-fr="card">
        <div class="obx-fresh-card-head">
          <span data-fr="card-title">正在校验</span>
          <button type="button" class="obx-fresh-hide" data-fr="hide" title="隐藏">—</button>
        </div>
        <div class="obx-fresh-count" data-fr="count">0 / 0</div>
        <div class="obx-fresh-bar"><div class="obx-fresh-bar-inner" data-fr="bar"></div></div>
        <div class="obx-fresh-current" data-fr="current"></div>
        <div class="obx-fresh-errors" data-fr="errors"></div>
        <div class="obx-fresh-card-foot">
          <button type="button" class="btn btn-sm" data-fr="cancel">取消</button>
        </div>
      </div>`;
    if (o.container) o.container.appendChild(group);

    const el = {};
    group.querySelectorAll('[data-fr]').forEach((n) => { el[n.dataset.fr] = n; });

    let state = null;
    let scope = o.scope == null ? '' : o.scope;
    let timer = null;
    let syncTimer = null;
    let hideTimer = null;
    let disposed = false;
    let supported = true;

    // ----- 渲染 -----

    function renderStatus() {
      if (!supported) {
        el.status.textContent = '';
        el.verify.disabled = true;
        el.verify.title = '本插件未参与统一刷新';
        return;
      }
      const s = state || {};
      const task = s.task || {};
      if (s.busy && task.state === 'running') {
        const total = task.total || 0;
        const done = task.processed || 0;
        el.status.textContent = total
          ? `校验中 ${done}/${total}`
          : '校验中…';
        return;
      }
      if (s.version_stale) {
        el.status.textContent = '派生数据待重算';
        return;
      }
      if (s.last_verify_at) {
        el.status.textContent = `已是最新 · 上次校验 ${timeAgo(s.last_verify_at)}`;
        return;
      }
      if (s.entries) {
        el.status.textContent = `已索引 ${s.entries} ${unit} · 尚未校验`;
        return;
      }
      el.status.textContent = '尚未建立索引';
    }

    function renderCard() {
      const s = state || {};
      const task = s.task || {};
      const running = !!s.busy && !task.done;
      if (!running) {
        el.cancel.disabled = true;
        return;
      }
      el.cancel.disabled = false;
      const total = task.total || 0;
      const done = task.processed || 0;
      el.count.textContent = total ? `${done} / ${total} ${unit}` : '正在遍历…';
      el.bar.style.width = total ? `${Math.min(100, Math.round(done / total * 100))}%` : '30%';
      el.current.textContent = task.current ? `正在处理：${task.current}` : '';
      const errors = task.errors || [];
      el.errors.textContent = errors.length
        ? `失败 ${task.error_count || errors.length} 项，示例：${errors.slice(0, 2).join('；')}`
        : '';
    }

    function showCard(title) {
      if (hideTimer) { clearTimeout(hideTimer); hideTimer = null; }
      el['card-title'].textContent = title || '正在校验';
      el.card.classList.remove('hidden');
    }

    function finishCard(summary) {
      el['card-title'].textContent = summary || '校验完成';
      el.bar.style.width = '100%';
      el.count.textContent = '';
      el.current.textContent = '';
      el.cancel.disabled = true;
      hideTimer = setTimeout(() => el.card.classList.add('hidden'), CARD_HIDE_MS);
    }

    function render() {
      const before = state;
      renderStatus();
      renderCard();
      el.verify.disabled = !supported || !!(state && state.busy);
      return before;
    }

    // ----- 轮询 -----

    async function poll() {
      if (disposed) return;
      try {
        const next = await call('system_freshness_state', plugin);
        if (next && next.available === false) { supported = false; }
        const wasBusy = !!(state && state.busy);
        state = next || state;
        render();
        if (state && state.busy) {
          timer = setTimeout(poll, POLL_MS);
        } else if (wasBusy) {
          onSettled();
        }
      } catch (e) {
        supported = false;
        renderStatus();
      }
    }

    function onSettled() {
      const task = (state && state.task) || {};
      if (task.state === 'cancelled') {
        finishCard('校验已取消');
        toast('info', '校验已取消，已完成的指纹保留');
      } else {
        const extra = task.extra || {};
        const parts = [];
        if (extra.added) parts.push(`新增 ${extra.added}`);
        if (extra.changed) parts.push(`更新 ${extra.changed}`);
        if (extra.removed) parts.push(`清理 ${extra.removed}`);
        const errors = task.error_count || 0;
        finishCard(errors ? `校验完成（${errors} 项失败）` : '校验完成');
        toast(errors ? 'warning' : 'success',
          `校验完成${parts.length ? '：' + parts.join(' · ') : ''}${errors ? `，${errors} 项失败` : ''}`);
      }
      if (typeof o.onChange === 'function') o.onChange(state);
    }

    // ----- 对外动作 -----

    async function verify() {
      if (!supported) return;
      try {
        const res = await call('system_freshness_verify', plugin);
        if (res && res.error === 'unsupported') { supported = false; renderStatus(); return; }
        if (res && res.started === false && !res.running) {
          toast('error', (res && res.error) || '校验启动失败');
          return;
        }
        showCard('正在校验');
        if (!timer) poll();
      } catch (e) {
        toast('error', '校验启动失败：' + ((e && e.message) || e));
      }
    }

    async function cancel() {
      try {
        await call('system_freshness_cancel', plugin);
      } catch (e) { /* 忽略：任务可能刚好结束 */ }
    }

    /** 被动同步：进视图 / 切目录时调用；本地去抖，壳侧还有最小间隔。 */
    function autoSync(nextScope, opts2) {
      if (!supported) return;
      if (nextScope != null) scope = nextScope;
      const quiet = !!(opts2 && opts2.quiet);
      if (syncTimer) clearTimeout(syncTimer);
      syncTimer = setTimeout(async () => {
        syncTimer = null;
        if (disposed) return;
        try {
          const report = await call('system_freshness_sync', plugin, scope);
          if (!report) return;
          if (report.action === 'partial') {
            // 预算用尽：被动路径降级成一次后台全量校验（用户不必自己点）
            if (!quiet) showCard('正在校验（同步未覆盖全部目录）');
            if (!timer) poll();
          } else if (report.action === 'sync'
                     && (report.added || report.changed || report.removed)) {
            if (typeof o.onChange === 'function') o.onChange(report);
          }
        } catch (e) { /* 被动路径失败不打扰用户：下一次进视图会重试 */ }
      }, SYNC_DEBOUNCE_MS);
    }

    function destroy() {
      disposed = true;
      if (timer) clearTimeout(timer);
      if (syncTimer) clearTimeout(syncTimer);
      if (hideTimer) clearTimeout(hideTimer);
      if (group.parentNode) group.parentNode.removeChild(group);
    }

    el.verify.addEventListener('click', verify);
    el.cancel.addEventListener('click', cancel);
    el.hide.addEventListener('click', () => el.card.classList.add('hidden'));

    render();
    poll();

    return {
      el: group,
      verify,
      cancel,
      autoSync,
      destroy,
      state: () => state,
      setScope: (v) => { scope = v == null ? '' : v; },
      get supported() { return supported; },
    };
  }

  /**
   * 派生资源 URL + 版本号。
   *
   * 壳的 `/thumbs` 响应带 `Cache-Control: private, max-age=86400`，URL 不变就
   * 24 小时不回源 —— 于是"重建了缩略图但界面没变"。版本号取**源文件的 mtime**
   * 即可覆盖主要场景（文件被替换 → mtime 变 → URL 变 → 重新请求）；指纹没变
   * 的强制重建请把 `version` 传成单调递增值（例如重建完成时刻）。
   */
  function assetUrl(path, version) {
    if (!window.Bridge || typeof Bridge.thumbUrl !== 'function') return '';
    const base = Bridge.thumbUrl(path);
    return version ? `${base}&v=${encodeURIComponent(version)}` : base;
  }

  return { mount, assetUrl, timeAgo };
})();
