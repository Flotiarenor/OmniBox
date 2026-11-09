// ==================== 统一「同步 / 校验」（Shell 共享组件） ====================
//
// 每个插件原先各写一套"刷新"：名字有 6 种（刷新 / 全量重建 / 重新扫描 / 扫描 /
// 深度扫描 / 刷新记录 / 校验内容），轮询、进度、取消、去抖各实现一遍，行为也各不
// 相同。本组件把**界面与编排**收成一份，插件只需要：
//
//   const ctl = Freshness.mount({ plugin: 'media-player', container: el, unit: '首',
//                                 scope: 当前目录, onChange: (report) => this.reload() });
//   ctl.autoSync('');            // 进视图 / 切目录时调用（组件内部去抖，壳侧再节流）
//   await Freshness.sync('media-player', '');   // 不经组件、立即同步一次并拿到报告
//
// **只挂载不触发 = 永远看不到外部变化**：`mount` 只渲染控件并读一次状态，它不扫盘。
// 壳挂载型（`PluginChrome` 挂、内容页没有句柄）必须自己调 `autoSync(scope)`，
// 否则控件「校验」按钮之外没有任何自动入口 —— 门禁 `tools/check_plugins.py`
// 的壳挂载检查会拦住这种写法。
//
// 控件在无任务时每 30 秒重读一次状态投影（只读，不扫盘；`idlePollMs` 可调，
// 0 关闭），用户停留在页面上时「有变化 · 需要校验」才会自己出现。窗口不可见、
// 或控件所在页被 `v-show` 隐藏（keepAlive 的插件页仍在 DOM 里）时不读。
// 被动同步被后端跳过（去抖 / 正在校验 / 作用域无效）时，状态行在**已有文案之后**
// 补一句可重试的原因：跳过只解释"这一次为什么没扫"，不能顶掉"是否需要校验"或
// "部分目录不可达"这类事实。
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
  const IDLE_POLL_MS = 30000; // 没有任务时的状态重读间隔（只读状态投影，不扫盘）
  const SYNC_NOTE_MS = 6000;  // 同步被跳过时状态行停留时间
  const SYNC_DEBOUNCE_MS = 400;
  const CARD_HIDE_MS = 4000;  // 完成后的进度卡停留时间
  const VERIFY_STALE_MS = 7 * 24 * 60 * 60 * 1000; // 上次校验超过 7 天就提示重新校验
  // 状态行的三种"没读到"各有原因，文案不能混：读状态失败 / 校验启动失败 / 状态暂不可用。
  // 混用会把用户引到错误的排查方向（例如一次校验启动失败被当成状态投影坏了）。
  const STATE_ERROR_TEXT = '状态读取失败，可重试';
  const VERIFY_ERROR_TEXT = '校验启动失败，可重试';
  const STATE_UNAVAILABLE_TEXT = '状态暂不可用，可重试';

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
   *   idlePollMs 没有任务时重读状态的间隔（毫秒，0 = 关闭；缺省 30 秒）
   */
  function mount(opts) {
    const o = opts || {};
    const plugin = o.plugin || (window.Bridge && Bridge.pluginName) || '';
    const unit = o.unit || '项';
    const idlePollMs = Number.isFinite(o.idlePollMs) && o.idlePollMs >= 0
      ? o.idlePollMs : IDLE_POLL_MS;

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
    let timer = null;           // 校验进行中的轮询定时器
    let idleTimer = null;       // 无任务时的低频状态重读
    let syncTimer = null;
    let hideTimer = null;
    let disposed = false;
    let supported = true;
    //: 最近一次"读取任务状态"失败（瞬时错误）。只影响状态文案与按钮可用性，
    //: 不能据此认定插件未接入统一刷新 —— 那会让一次 5xx / 超时永久禁用校验。
    let stateError = false;
    //: stateError 时状态行显示哪一句（三种原因见文件头的常量）。
    let stateErrorText = STATE_ERROR_TEXT;
    //: 刚发起一次校验、还没读到能判定收尾的状态。收尾不能只看"上一份 state 是 busy"：
    //: 发起时必然不是 busy，而任务可能在第一次读取之前就结束（空根 / 小库只需几毫秒）。
    let verifyPending = false;
    //: 卡片是否处于"已完成、正在自动隐藏"的状态（此时没有任务在跑，但不该立刻收掉）
    let cardSettled = false;
    //: 上次"替用户自动起校验"的时间：预算型 partial 可能每次进视图都出现，得限流
    let lastAutoVerifyAt = 0;
    const AUTO_VERIFY_COOLDOWN_MS = 3 * 60 * 1000;
    //: 本次校验刚结束的短暂状态（仅用于状态文案，不是后端字段）
    let completion = null;
    let completionTimer = null;
    //: 最近一次被动同步被后端跳过的原因（`busy` / `throttled` / `bad_scope`）。
    //: 跳过不是错误，但状态行必须说出来，否则"点了没更新"没有任何线索。
    let syncNote = null;
    let syncNoteTimer = null;

    // ----- 渲染 -----

    function clearCompletion() {
      if (completionTimer) { clearTimeout(completionTimer); completionTimer = null; }
      completion = null;
    }

    /** 一次校验结束：只有这一瞬间才允许说"校验完成/未发现变化"。 */
    function markCompletion(task) {
      clearCompletion();
      const extra = (task && task.extra) || {};
      const changeKeys = ['added', 'changed', 'removed', 'pruned'];
      const changedCount = changeKeys
        .reduce((sum, key) => sum + (Number(extra[key]) || 0), 0);
      // 没有变化计数时不能默认"无变化"：只有后端确实给了 extra 才敢下结论。
      const known = typeof extra === 'object'
        && (changeKeys.some((key) => Object.prototype.hasOwnProperty.call(extra, key))
          || Object.prototype.hasOwnProperty.call(extra, 'invalidated'));
      completion = {
        errors: Number(task && task.error_count) || 0,
        changed: changedCount > 0 || !!extra.invalidated,
        known,
      };
      completionTimer = setTimeout(() => {
        completion = null;
        completionTimer = null;
        renderStatus();
      }, CARD_HIDE_MS);
      renderStatus();
    }

    /** `unreachable_roots` 是数量（旧字段形状不确定时也接受数组）。 */
    function unreachableRoots(s) {
      const raw = s && s.unreachable_roots;
      if (Array.isArray(raw)) return raw.length;
      const n = Number(raw);
      return Number.isFinite(n) && n > 0 ? Math.floor(n) : 0;
    }

    function renderStatus() {
      if (!supported) {
        el.status.textContent = '';
        el.verify.disabled = true;
        el.verify.title = '本插件未参与统一刷新';
        return;
      }
      const s = state || {};
      const task = s.task || {};
      if (completion) {
        if (completion.errors) {
          el.status.textContent = `校验完成 · ${completion.errors} 项失败`;
        } else if (completion.changed) {
          el.status.textContent = '校验完成 · 有变化';
        } else if (completion.known) {
          el.status.textContent = '校验完成 · 未发现变化';
        } else {
          el.status.textContent = '校验完成';
        }
        return;
      }
      if (stateError) {
        // 三种来源（读状态 / 启动校验 / 指纹库瞬时打不开）各说各的，不再一律报"状态读取失败"。
        el.status.textContent = stateErrorText;
        return;
      }
      if (s.busy && task.state === 'running') {
        const total = task.total || 0;
        const done = task.processed || 0;
        el.status.textContent = total
          ? `校验中 ${done}/${total}`
          : '校验中…';
        return;
      }
      let text;
      const unreachable = unreachableRoots(s);
      if (unreachable) {
        // 有根没挂上：新增文件看不见是"盘不在"而不是"需要校验"，说清原因比催校验有用。
        text = `部分目录不可达（${unreachable}）· 挂载后自动同步`;
      } else if (s.version_stale) {
        text = '有变化 · 需要校验';
      } else if (s.last_verify_at) {
        const verifiedAt = Number(s.last_verify_at) * 1000;
        const stale = Number.isFinite(verifiedAt) && (Date.now() - verifiedAt > VERIFY_STALE_MS);
        text = `上次校验 ${timeAgo(s.last_verify_at)}${stale ? ' · 建议重新校验' : ''}`;
      } else if (s.entries) {
        text = `已索引 ${s.entries} ${unit} · 尚未校验`;
      } else {
        text = '尚未校验';
      }
      // 跳过原因只解释"这一次为什么没扫"，不能顶掉"是否需要校验 / 目录不可达"这类事实，
      // 否则快速切几个目录之后，用户就再也看不到真正需要注意的那一条了。
      el.status.textContent = syncNote ? `${text} · ${syncNote.text}` : text;
    }

    function renderCard() {
      const s = state || {};
      const task = s.task || {};
      const running = !!s.busy && !task.done;
      if (!running) {
        // 没有任务在跑就**不要留着上一次的数字**：卡片里的「0 / 0」是静态占位符，
        // 早期实现在这里直接 return，于是"弹了卡片但没起任务"时它会一直挂着。
        el.cancel.disabled = true;
        if (!cardSettled) hideCard();
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
      cardSettled = false;
      el['card-title'].textContent = title || '正在校验';
      el.card.classList.remove('hidden');
    }

    /** 收掉卡片（含"弹了但没起任务"的情况），不留下任何陈旧数字。 */
    function hideCard() {
      if (hideTimer) { clearTimeout(hideTimer); hideTimer = null; }
      cardSettled = false;
      el.card.classList.add('hidden');
      el.count.textContent = '0 / 0';
      el.bar.style.width = '0%';
      el.current.textContent = '';
      el.errors.textContent = '';
    }

    function finishCard(summary) {
      el['card-title'].textContent = summary || '校验完成';
      el.bar.style.width = '100%';
      el.count.textContent = '';
      el.current.textContent = '';
      el.cancel.disabled = true;
      cardSettled = true;
      hideTimer = setTimeout(() => el.card.classList.add('hidden'), CARD_HIDE_MS);
    }

    function render() {
      const before = state;
      renderStatus();
      renderCard();
      // stateError 时即使上一次 state 还残留 busy，也要让按钮可点（重试入口）。
      el.verify.disabled = !supported || (!stateError && !!(state && state.busy));
      return before;
    }

    // ----- 轮询 -----

    function clearIdleTimer() {
      if (idleTimer) { clearTimeout(idleTimer); idleTimer = null; }
    }

    /**
     * 控件此刻该不该做低频状态重读。
     *
     * 两个"看不见"都要挡住：整个窗口不可见，以及 keepAlive 的插件页被 `v-show` 隐藏后
     * 控件仍留在 DOM 里（`offsetParent === null` 就是没有布局盒）。两者都不该继续
     * 每 30 秒问一次后端。
     */
    function idleBlocked() {
      if (window.document && window.document.visibilityState === 'hidden') return true;
      return !!group && group.offsetParent === null;
    }

    /**
     * 没有任务时低频重读一次状态。
     *
     * 状态投影读一次之后不会自己变：没有这一步，用户停留在页面上时「有变化 · 需要校验」
     * 永远不会出现，外部新增的文件要等下一次进视图才被提示。这里只读 `state`
     * （不扫盘、不建库），间隔 30 秒；不可见时见 {@link idleBlocked}。
     */
    function scheduleIdlePoll() {
      clearIdleTimer();
      if (disposed || !supported || !(idlePollMs > 0)) return;
      if (window.document && window.document.visibilityState === 'hidden') return;
      idleTimer = setTimeout(() => {
        idleTimer = null;
        if (disposed || timer) return;   // 校验轮询在跑时交给它
        if (idleBlocked()) {
          // 隐藏期间只续定时、不发请求：切回来（v-show 重新显示）时没有事件可听，
          // 只能靠这个低频定时器自己恢复。
          scheduleIdlePoll();
          return;
        }
        poll();
      }, idlePollMs);
    }

    function onVisibilityChange() {
      if (!window.document || disposed) return;
      if (window.document.visibilityState === 'hidden') clearIdleTimer();
      else if (!timer) scheduleIdlePoll();
    }

    /**
     * `available:false` 是"插件确实没接入统一刷新"，还是读指纹库时的瞬时失败？
     *
     * 后端两处都会返回 `available:false`：`plugin_manager.freshness_state`（未加载 /
     * 未声明 `freshness_spec`，带"未参与统一刷新"的错误）与 `FreshnessEngine.state`
     * （sqlite 打不开，例如库被占用、盘掉线）。后者重试就能好；把它当成"未接入"会
     * 永久禁用控件（状态行空白、按钮禁用），比一次可重试的失败更糟。
     */
    function unsupportedState(next) {
      const error = String((next && next.error) || '');
      return !error || error === 'unsupported' || error.includes('未参与统一刷新');
    }

    async function poll() {
      if (disposed) return;
      clearIdleTimer();
      try {
        const next = await call('system_freshness_state', plugin);
        const unavailable = !!(next && next.available === false);
        if (unavailable && unsupportedState(next)) {
          supported = false;
        } else if (unavailable) {
          // 瞬时：保留 supported，状态行给可重试的原因，按钮保持可点。
          stateError = true;
          stateErrorText = STATE_UNAVAILABLE_TEXT;
        } else {
          stateError = false;
          stateErrorText = STATE_ERROR_TEXT;
        }
        const wasBusy = !!(state && state.busy);
        state = next || state;
        // 新一轮任务开始后，上一次“校验完成”不再成立。
        if (state && state.busy) clearCompletion();
        render();
        if (state && state.busy) {
          timer = setTimeout(poll, POLL_MS);
        } else {
          // 一次轮询到此结束：把 timer 复位。verify() 用 `if (!timer)` 判断
          // "现在没有轮询在跑"，旧的（已经 fire 过的）定时器 id 为真会让第二次
          // 点「校验」只弹卡片、永远不再 poll —— 卡片停在 0 / 0、状态停在旧文案。
          timer = null;
          // 收尾看两件事：这一趟观察到了 busy → 正常结算；或者本次校验是我们刚发起的
          // （`verifyPending`）却一次 busy 都没看到 —— 任务在第一次读取之前就跑完了，
          // 不结算就没有完成文案、也没有 onChange（内容页不会重载）。
          const settle = wasBusy || verifyPending;
          verifyPending = false;
          if (settle) onSettled();
          scheduleIdlePoll();
        }
      } catch (e) {
        // 一次状态读取失败 ≠ 插件未接入统一刷新：只有后端明确 available === false
        // 才置 supported=false。这里保留 supported，给出可重试的状态与按钮。
        // `verifyPending` 也保留：下一次读到终态时仍要把这次校验结算掉。
        timer = null;
        stateError = true;
        stateErrorText = STATE_ERROR_TEXT;
        render();
        scheduleIdlePoll();
      }
    }

    function onSettled() {
      const task = (state && state.task) || {};
      const extra = task.extra || {};
      if (task.state === 'cancelled') {
        clearCompletion();
        finishCard('校验已取消');
        toast('info', '校验已取消，已完成的指纹保留');
      } else {
        const parts = [];
        if (extra.added) parts.push(`新增 ${extra.added}`);
        if (extra.changed) parts.push(`更新 ${extra.changed}`);
        if (extra.removed) parts.push(`清理 ${extra.removed}`);
        const errors = task.error_count || 0;
        finishCard(errors ? `校验完成（${errors} 项失败）` : '校验完成');
        if (task.state === 'done') markCompletion(task);
        else clearCompletion();
        toast(errors ? 'warning' : 'success',
          `校验完成${parts.length ? '：' + parts.join(' · ') : ''}${errors ? `，${errors} 项失败` : ''}`);
      }
      if (typeof o.onChange === 'function') {
        // 校验跑完整趟：无论有没有变化都要通知调用方重载当前视图（校验的价值就是
        // 「现在读到的就是最新的」）。入参与同步报告同形，调用方不必区分两条来源。
        o.onChange({
          action: 'verify',
          added: extra.added || 0,
          changed: extra.changed || 0,
          removed: extra.removed || 0,
          invalidated: !!extra.invalidated,
          task: task,
        });
      }
    }

    // ----- 对外动作 -----

    async function verify(opts2) {
      if (!supported) return;
      const auto = !!(opts2 && opts2.auto);
      clearCompletion();
      clearSyncNote();
      clearIdleTimer();
      renderStatus();
      try {
        const res = await call('system_freshness_verify', plugin);
        // 只有后端**明确**说"不支持"才永久禁用；其他失败都按可重试处理。
        if (res && res.error === 'unsupported') { supported = false; renderStatus(); return; }
        // 同步正在进行（引擎单飞）：没有任务可轮询，弹卡片只会停在静态占位符上，
        // 而这一趟同步本身会把变化经 onChange 送回来。
        if (res && res.started === false && !res.running
            && (res.reason === 'busy' || res.sync_active)) {
          hideCard();
          if (!auto) toast('info', '同步进行中，完成后再试');
          scheduleIdlePoll();
          return;
        }
        if (res && res.started === false && !res.running) {
          if (!auto) toast('error', (res && res.error) || '校验启动失败');
          hideCard();
          scheduleIdlePoll();
          return;
        }
        stateError = false;
        stateErrorText = STATE_ERROR_TEXT;
        // 从这里开始：这一趟校验归我们结算，即使第一次读取时它已经结束（见 poll 的收尾条件）。
        verifyPending = true;
        showCard(auto ? '正在校验（同步未覆盖全部目录）' : '正在校验');
        if (!timer) poll();
      } catch (e) {
        hideCard();
        stateError = true;
        // 启动失败与状态读取失败是两件事：混用同一句会把人引到错误的排查方向。
        stateErrorText = VERIFY_ERROR_TEXT;
        render();
        // 这次没起来不代表以后不行：把低频状态重读接回去，否则"需要校验"再也不出现。
        scheduleIdlePoll();
        if (!auto) toast('error', '校验启动失败：' + ((e && e.message) || e));
      }
    }

    async function cancel() {
      try {
        await call('system_freshness_cancel', plugin);
      } catch (e) { /* 忽略：任务可能刚好结束 */ }
    }

    /**
     * 报告里"目录集合变了"的数量。
     *
     * `new_dirs` / `vanished_dirs` 是引擎给的目录键数组（各自最多 50 条），
     * `new_dirs_truncated` / `vanished_dirs_truncated` 是溢出计数。`dirs_changed` 是
     * 插件钩子的返回值（无钩子或没有目录变化时恒为 `{}`），不能当判据。
     */
    function dirSignalCount(report) {
      if (!report || typeof report !== 'object') return 0;
      let total = 0;
      ['new_dirs', 'vanished_dirs'].forEach((key) => {
        if (Array.isArray(report[key])) total += report[key].length;
      });
      ['new_dirs_truncated', 'vanished_dirs_truncated'].forEach((key) => {
        total += Number(report[key]) || 0;
      });
      return total;
    }

    /**
     * 报告里是否真有变化：`invalidated`（远端来源丢弃缓存）、三个条目计数，以及
     * **目录集合信号**。新建空目录 / 整个目录被移走时条目层没有任何增删，只看
     * added/changed/removed 会把这类变化整个漏掉 —— 内容页也就看不到新目录。
     */
    function reportChanged(report) {
      return !!(report
        && (report.added || report.changed || report.removed || report.invalidated
          || dirSignalCount(report) > 0));
    }

    function clearSyncNote() {
      if (syncNoteTimer) { clearTimeout(syncNoteTimer); syncNoteTimer = null; }
      syncNote = null;
    }

    const SKIP_TEXT = {
      busy: '后台校验进行中 · 完成后再同步',
      throttled: '同步被节流 · 稍后重试',
      bad_scope: '作用域无效 · 未同步',
      // 远端来源在新鲜期内本来就不该重取：不是"被跳过"的事故，只是说明。
      fresh: '缓存仍在新鲜期内 · 无需同步',
    };

    /**
     * 被动同步被后端跳过：状态行说明原因，几秒后恢复常规文案。
     *
     * `skip` 不是失败（下一趟会接着做），但"界面没更新"必须有个能读到的原因。
     */
    function noteSkipped(report) {
      clearSyncNote();
      syncNote = { text: SKIP_TEXT[report && report.reason] || '同步已跳过' };
      syncNoteTimer = setTimeout(() => {
        syncNoteTimer = null;
        syncNote = null;
        renderStatus();
      }, SYNC_NOTE_MS);
      renderStatus();
    }

    /**
     * 真跑一次被动同步并返回后端报告（不去抖；失败返回 null）。
     *
     * `partial` 且原因是 `budget`（预算用尽、还有目录要处理）时**真的起一次后台
     * 校验**把剩下的活干完：原先只把卡片弹出来轮询，没有任何任务在跑，卡片就停在
     * 静态占位符「0 / 0」上永不消失（`renderCard` 在"没有任务"时直接 return，
     * `onSettled` 也不会触发）。`dir_cap`（只是没走完，不代表还有活）不起校验，
     * 否则每次进视图都要跑一趟全量。
     */
    async function runSync(nextScope, opts2) {
      if (nextScope != null) scope = nextScope;
      const quiet = !!(opts2 && opts2.quiet);
      try {
        const report = await call('system_freshness_sync', plugin, scope);
        if (!report) return null;
        if (report.action === 'skip') {
          noteSkipped(report);
        } else if (report.action === 'partial' && report.reason === 'budget') {
          clearSyncNote();
          // 这一趟已经走到的目录里可能真有变化（含目录集合变化）：先把结论交给内容页，
          // 再替用户把剩下的目录校验完。两件事不互斥，只是分属两个阶段。
          if (reportChanged(report) && typeof o.onChange === 'function') o.onChange(report);
          if (!quiet && !(state && state.busy)
              && Date.now() - lastAutoVerifyAt >= AUTO_VERIFY_COOLDOWN_MS) {
            lastAutoVerifyAt = Date.now();
            verify({ auto: true });
          }
        } else {
          clearSyncNote();
          if (reportChanged(report) && typeof o.onChange === 'function') o.onChange(report);
        }
        return report;
      } catch (e) {
        // 被动路径失败不打扰用户：下一次进视图 / 视图变化会再试一次
        return null;
      }
    }

    /** 被动同步：进视图 / 切目录时调用；本地去抖，壳侧还有最小间隔。 */
    function autoSync(nextScope, opts2) {
      if (!supported) return;
      if (nextScope != null) scope = nextScope;
      if (syncTimer) clearTimeout(syncTimer);
      syncTimer = setTimeout(() => {
        syncTimer = null;
        if (disposed) return;
        void runSync(scope, opts2);
      }, SYNC_DEBOUNCE_MS);
    }

    /**
     * 立即同步一次并返回报告（与 `autoSync` 同一套编排，只去掉去抖与异步等待）。
     *
     * 给"手里有挂载句柄、但需要报告本身"的调用方：`autoSync` 是 fire-and-forget，
     * 报告只经 `onChange` 回调出去。
     */
    function syncNow(nextScope, opts2) {
      if (!supported) return Promise.resolve(null);
      if (syncTimer) { clearTimeout(syncTimer); syncTimer = null; }
      return runSync(nextScope == null ? scope : nextScope, opts2);
    }

    function destroy() {
      disposed = true;
      if (timer) clearTimeout(timer);
      if (idleTimer) clearTimeout(idleTimer);
      if (syncTimer) clearTimeout(syncTimer);
      if (hideTimer) clearTimeout(hideTimer);
      if (syncNoteTimer) clearTimeout(syncNoteTimer);
      if (window.document && window.document.removeEventListener) {
        window.document.removeEventListener('visibilitychange', onVisibilityChange);
      }
      clearCompletion();
      if (group.parentNode) group.parentNode.removeChild(group);
    }

    el.verify.addEventListener('click', verify);
    el.cancel.addEventListener('click', cancel);
    el.hide.addEventListener('click', () => el.card.classList.add('hidden'));
    if (window.document && window.document.addEventListener) {
      window.document.addEventListener('visibilitychange', onVisibilityChange);
    }

    render();
    poll();

    return {
      el: group,
      verify,
      cancel,
      autoSync,
      sync: syncNow,
      destroy,
      state: () => state,
      setScope: (v) => { scope = v == null ? '' : v; },
      get supported() { return supported; },
    };
  }

  /**
   * 直接同步一次（不经挂载句柄）：`system_freshness_sync(plugin, scope)`，返回后端报告。
   *
   * 报告形状：`action` ∈ `sync`（真扫了）/ `partial`（预算用尽）/ `skip`（去抖、被占用、
   * 非法作用域）。调用方按 `added` / `changed` / `removed` / `invalidated` 决定是否重载；
   * `skip` 不是错误，不必提示。失败时抛出（调用方自行决定忽略）。
   *
   * 这是**壳级方法**，和 `mount` 内部的同步走同一条 `callSystem` 通道。
   *
   * @param {string} plugin 插件名
   * @param {string} [scope] 同步作用域（目录相对路径，'' = 全部根）
   * @returns {Promise<Object|null>} 后端报告
   */
  async function sync(plugin, scope) {
    if (!plugin) return null;
    return await call('system_freshness_sync', plugin, scope == null ? '' : scope);
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

  return { mount, sync, assetUrl, timeAgo };
})();
