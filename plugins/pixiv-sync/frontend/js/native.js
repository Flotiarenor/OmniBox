// Pixiv 同步 native 逻辑页。
//
// 本页不渲染界面：它把同步状态、任务进度与设置编译成壳的原生内容 blocks（见
// shell/frontend/src/core/plugin-content.ts），由壳的 NativePanel 与 SchemaRenderer
// 画出来。用户操作以 cubby:native-action 回到这里，这里调后端后重新提交 blocks。
//
// 三条约束：
// - 本页在壳里是 display:none 的 iframe，页面内的 Toast / modal 用户看不见，
//   所以提示与两步确认一律用 banner / actions block 表达，不弹窗；
// - 不读宿主 DOM，也不读 parent 上的宿主对象；后端能力只走 Bridge.call；
// - 轮询只在本页进行：重复的 cubby:native-init 与反复调度都不会叠出第二个定时器，
//   iframe 被壳移除时（pagehide / beforeunload）停掉定时器。
//
// 版式：提交 blocks 时带 `layout: 'document'`，壳据此把正文收窄到 960px、组间放 24px。
// 页面结构是「状态行 → 指标 → （有任务时）同步任务 → 操作 → 账号 → 说明」；
// 主操作只有一个主按钮，刷新与重试是链接型第三级动作。
(function () {
  'use strict';

  // 长时间运行的旧服务进程 / 旧缓存页面可能没有 /shell/native-client.js。
  // 那种情况下 NativeExtension undefined，直接调用会在装载期抛错，面板永远停在
  // “正在加载…”。这里先向壳报一条 native-error，让用户看到可操作的原因。
  if (!window.NativeExtension || typeof window.NativeExtension.create !== 'function') {
    try {
      if (window.parent && window.parent !== window) {
        window.parent.postMessage({
          type: 'cubby:native-error',
          error: 'Pixiv 同步逻辑页缺少 /shell/native-client.js，请重启格子铺后再打开本面板。',
        }, window.location.origin);
      }
    } catch (e) {
      /* 没有可用的壳接收方：保持静默，避免二次异常 */
    }
    return;
  }

  const STATUS_IDLE_MS = 8000;
  const STATUS_RUNNING_MS = 1500;
  const STATUS_COOLDOWN_MS = 1000;
  const ACTIVE_STATES = { queued: true, running: true };
  const TASK_STATES = {
    queued: '排队中',
    running: '同步中',
    done: '完成',
    failed: '失败',
    cancelled: '已取消',
    paused: '已暂停（可重新同步续跑）',
  };
  const TASK_KINDS = {
    following: '同步关注作品',
    bookmarks: '同步收藏作品',
  };
  /** 文档式版：壳把正文收窄到 960px 居中，组间 24px。 */
  const state = {
    view: 'main',           // main / oauth
    initialized: false,
    disposed: false,
    status: null,
    statusError: '',
    tokenConfigured: false,
    notice: null,           // {tone, text}：操作结果，走 Toast，不进 blocks
    libraryBusy: false,     // 上一次 get_status 里本地校验是否在跑（用于完成时补提示）
    pending: null,          // {text, confirm, cancel}：可重来的 inline 二次确认
    confirm: null,          // {title, text, confirm, cancel}：不可逆操作的阻断确认
    oauth: { url: '', code: '' },
    submitting: false,
  };

  let pollTimer = null;
  let client = null;
  let freshness = null;
  let lastSignature = '';
  /** 已经作为 Toast 发出去的通知键，避免同一条结果反复弹。 */
  let sentNotice = '';

  // ---------- 与壳的通道 ----------

  function render() {
    if (!client) return;
    lastSignature = signature();
    const options = { layout: 'document' };
    // 操作结果走一次性 Toast：不进 blocks、不占版面，也不会被下一次轮询覆盖成残留文案。
    const notice = state.notice;
    if (notice && notice.text && notice.tone !== 'danger') {
      // 成功与中性结果走一次性 Toast：不占版面，也不会被下一次轮询覆盖成残留文案。
      const key = (notice.tone || 'default') + ':' + notice.text;
      if (key !== sentNotice) {
        sentNotice = key;
        options.toast = { text: notice.text, tone: notice.tone || 'default' };
      }
    } else {
      // 失败不走 Toast（会自己消失），改为常驻 banner；这里清空去重键。
      sentNotice = '';
    }
    client.render(buildBlocks(), buildToolbar(), options);
  }

  function errText(error) {
    if (error && error.message) return String(error.message);
    return String(error == null ? '' : error);
  }

  function num(value) {
    const parsed = Number(value);
    return Number.isFinite(parsed) ? parsed : 0;
  }

  /** 计数展示：上万的作品数不加分隔符会很难扫读。 */
  function fmt(value) {
    const parsed = num(value);
    try {
      return parsed.toLocaleString('zh-CN');
    } catch (e) {
      return String(parsed);
    }
  }

  function clock(seconds) {
    const total = Math.max(0, Math.floor(num(seconds)));
    const minutes = String(Math.floor(total / 60)).padStart(2, '0');
    return minutes + ':' + String(total % 60).padStart(2, '0');
  }

  function timeText(epochSeconds) {
    if (!epochSeconds) return '';
    try {
      return new Date(num(epochSeconds) * 1000).toLocaleTimeString();
    } catch (e) {
      return '';
    }
  }

  function taskOf() {
    return state.status && state.status.task ? state.status.task : null;
  }

  function taskActive(task) {
    return !!task && ACTIVE_STATES[String(task.state)] === true;
  }

  function taskBusy() {
    const status = state.status || {};
    return state.submitting || !!status.running || taskActive(taskOf());
  }

  function taskKindLabel(task) {
    return TASK_KINDS[String(task && task.kind)] || '同步任务';
  }

  function isMain() {
    return state.view === 'main';
  }

  // 主视图之外有表单（OAuth 授权码）：轮询期间不重发 blocks，否则
  // SchemaRenderer 会按新 blocks 重建表单值，用户正在输入的内容被清掉。
  function signature() {
    return JSON.stringify({
      view: state.view,
      status: state.status,
      statusError: state.statusError,
      tokenConfigured: state.tokenConfigured,
      notice: state.notice,
      pending: state.pending,
      confirm: state.confirm,
    });
  }

  // ---------- blocks ----------

  function text(id, value, tone) {
    return { type: 'text', id: id, text: value, tone: tone || 'muted' };
  }

  function collect() {
    const out = [];
    for (let i = 0; i < arguments.length; i += 1) {
      const item = arguments[i];
      if (!item) continue;
      if (Array.isArray(item)) out.push.apply(out, item);
      else out.push(item);
    }
    return out;
  }

  /** 一行概要：Token / 下载根 / 名单 / 上次同步，压在指标区之上。 */
  function metaText() {
    const status = state.status || {};
    const parts = [state.tokenConfigured ? '已连接' : '未配置 Token'];
    if (status.root_dir) parts.push('下载根 ' + status.root_dir);
    parts.push(
      '画师名单 ' + (num(status.selected_artists) > 0
        ? num(status.selected_artists) + ' 位'
        : '全部关注画师'),
    );
    const task = taskOf();
    if (task && !taskActive(task) && task.finished_at) {
      parts.push('上次同步 ' + timeText(task.finished_at));
    }
    return parts.join(' · ');
  }

  /**
   * 指标区：四格同级数字。
   *
   * 第 4 格在“有失败跳过”时让给失败计数，否则显示“其他作品”（`other_done`：
   * 既不在关注名单也不在收藏名单的已下载作品，例如手动同步或手动放入的图）；
   * 这两者是不同字段，不能互相顶替。
   */
  function statsBlock() {
    const status = state.status || {};
    const failed = num(status.failed_skipped);
    const metrics = [
      { label: '已下载', value: fmt(status.downloaded_total), hint: '张' },
      { label: '关注待下', value: fmt(status.pending_following), hint: '共 ' + fmt(status.following_total) },
      { label: '收藏待下', value: fmt(status.pending_bookmarks), hint: '共 ' + fmt(status.bookmarks_total) },
    ];
    if (failed > 0) {
      metrics.push({ label: '失败跳过', value: fmt(failed), hint: '可重试', tone: 'danger' });
    } else {
      metrics.push({ label: '其他作品', value: fmt(status.other_done), hint: '不在名单内' });
    }
    return { type: 'stats', id: 'status-stats', metrics: metrics };
  }

  /** 限流冷却与状态读取失败：横幅比正文里的红字更容易被看到。 */
  function alertBlocks() {
    const out = [];
    const cooldown = num((state.status || {}).rate_limit_cooldown);
    if (cooldown > 0) {
      out.push({
        type: 'banner',
        id: 'cooldown',
        tone: 'warning',
        icon: 'icon:triangle-alert',
        text: 'Pixiv 限流冷却约 ' + clock(cooldown) + '，建议冷却结束后再同步。',
      });
    }
    if (state.statusError && state.status) {
      out.push({
        type: 'banner',
        id: 'status-warning',
        tone: 'danger',
        icon: 'icon:triangle-alert',
        text: '状态刷新失败：' + state.statusError,
      });
    }
    return out;
  }

  /** 任务区：没有任务、或上次任务正常结束时整块不出现（结束时间在状态行里）。 */
  function taskSection() {
    const task = taskOf();
    if (!task) return null;
    const failed = String(task.state) === 'failed' || !!task.error;
    // 空闲页只有「状态行 + 指标 + 操作 + 账号」四块；失败是唯一需要占一块的终态，
    // 否则用户看不到错误原因。其余终态（完成/取消/暂停）一行放在状态行里。
    if (!taskActive(task) && !failed) return null;
    const label = TASK_STATES[String(task.state)] || String(task.state || '未知状态');
    const blocks = [];
    if (taskActive(task)) {
      const total = num(task.total);
      const done = num(task.done);
      // 阶段文案来自后端 current（"正在登录 Pixiv…" / "已扫描画师 12/30 位" / …）。
      // 没有阶段文案时才用启动兜底，并把已用时间带上：长任务最怕"只有一个准备中"。
      const phase = String(task.current || '').trim() || '正在启动…';
      const elapsed = num(task.started_at) > 0
        ? Math.max(0, Date.now() / 1000 - num(task.started_at))
        : 0;
      const parts = [phase, '已用 ' + clock(elapsed)];
      if (total > 0 || done > 0) {
        parts.push('作品 ' + fmt(done) + '/' + fmt(total)
          + ' · 下载 ' + fmt(task.downloaded)
          + ' · 跳过 ' + fmt(task.skipped)
          + ' · 失败 ' + fmt(task.failed));
      }
      blocks.push({
        type: 'progress',
        id: 'task-progress',
        value: done,
        total: total,
        // 扫名单阶段后端还没有总数：这时是“进行中”而不是 0%
        indeterminate: total <= 0,
        text: taskKindLabel(task) + ' · ' + label,
        detail: parts.join(' · '),
        cancel: { id: 'cancel-task', label: '取消任务', variant: 'link' },
      });
      return { type: 'section', id: 'task-section', title: '同步任务', blocks: blocks };
    }
    const summary = taskKindLabel(task) + ' · ' + label
      + ' · 作品 ' + fmt(task.done) + '/' + fmt(task.total)
      + '，下载 ' + fmt(task.downloaded)
      + '，跳过 ' + fmt(task.skipped)
      + '，失败 ' + fmt(task.failed);
    blocks.push(text('task-summary', summary, failed ? 'danger' : 'muted'));
    if (task.error) {
      blocks.push({
        type: 'banner',
        id: 'task-error',
        tone: 'danger',
        icon: 'icon:triangle-alert',
        text: '错误：' + task.error,
      });
    }
    return { type: 'section', id: 'task-section', title: '同步任务', blocks: blocks };
  }

  function actionsSection() {
    // 校验在跑时也禁用同步：两者都会改写已下载记录，后端也会拒绝。
    const library = (state.status && state.status.library) || null;
    const busy = taskBusy() || !!(library && library.busy);
    // 没有 Token 时同步/刷新/重试都会在后台线程里认证失败，按钮直接禁用，
    // 把入口收窄到「获取 Token」与顶栏设置。
    const locked = busy || !state.tokenConfigured;
    const main = [
      // 两个方向同权：都用 accent（强调非实心），不制造“主/次”的暗示。
      { id: 'sync-following', label: '同步关注作品', icon: 'icon:download', variant: 'accent', disabled: locked },
      { id: 'sync-bookmarks', label: '同步收藏作品', icon: 'icon:heart', variant: 'accent', disabled: locked },
    ];
    const secondary = [];
    if (num((state.status || {}).failed_skipped) > 0) {
      secondary.push({
        id: 'retry-failed',
        label: '重试失败作品（' + fmt((state.status || {}).failed_skipped) + '）',
        variant: 'link',
        disabled: locked,
      });
    }
    const blocks = [{ type: 'actions', id: 'main-actions', actions: main }];
    if (secondary.length) blocks.push({ type: 'actions', id: 'secondary-actions', actions: secondary });
    return {
      type: 'section',
      id: 'actions-section',
      title: '操作',
      blocks: blocks,
    };
  }

  function accountSection() {
    const status = state.status || {};
    const parts = ['Token 已配置'];
    const cooldown = num(status.rate_limit_cooldown);
    parts.push(cooldown > 0 ? '请求限速 冷却 ' + clock(cooldown) : '请求限速 正常');
    const library = status.library || null;
    if (library && num(library.entries) > 0) {
      parts.push('本地索引 ' + fmt(library.entries) + ' 项');
    }
    // 同步或校验在跑时，维护动作一律禁用（两者都会改写已下载记录，后端也互斥）。
    const busy = taskBusy() || !!(library && library.busy);
    const blocks = [text('account-status', parts.join(' · '))];
    blocks.push({
      type: 'actions',
      id: 'account-actions',
      actions: [
        // 本地核对是唯一的维护入口：同步任务不做本地遍历，手动放进目录的图
        // 必须点这里才会并入已下载记录（另一条兼容入口 refresh_downloaded 只做导入）。
        { id: 'verify-library', label: '校验本地内容', size: 'sm', disabled: busy },
        { id: 'logout', label: '清除 Token', variant: 'danger', size: 'sm', disabled: busy },
      ],
    });
    return { type: 'section', id: 'account-section', title: '账号与维护', blocks: blocks };
  }

  function mainBlocks() {
    const meta = text('status-meta', metaText());
    // 一行状态 + 语义色圆点：已连接是绿色，未配置是红色（后面还有空态兜底）。
    meta.dot = state.tokenConfigured ? 'success' : 'danger';
    const blocks = collect(
      failureBanner(),
      pendingBlock(),
      confirmBlock(),
      meta,
    );

    if (!state.tokenConfigured) {
      blocks.push({
        type: 'empty',
        id: 'no-token',
        icon: 'icon:key-round',
        text: '尚未配置 Pixiv Refresh Token',
        hint: '没有 Token 无法同步。用「获取 Token」走内置 OAuth 登录，或在设置里粘贴 refresh_token。',
        submit: { id: 'oauth-start', label: '获取 Token' },
      });
      return blocks;
    }

    return blocks.concat(collect(
      alertBlocks(),
      statsBlock(),
      taskSection(),
      librarySection(),
      actionsSection(),
      accountSection(),
      text(
        'hint',
        '下载的画作会出现在「图片相册」中，重复作品按 Pixiv 作品 ID 自动跳过；'
          + '下载目录、Token 与并发数在设置里修改。',
      ),
    ));
  }

  /**
   * 失败提示：常驻横幅 + 「知道了」关闭。
   *
   * 判据与成功相反——成功可以瞬时（Toast），失败必须留在原地让用户看到并处理，
   * 不能像 Toast 那样 2 秒多就消失。
   */
  function failureBanner() {
    if (!state.notice || state.notice.tone !== 'danger') return null;
    return {
      type: 'banner',
      id: 'notice-alert',
      tone: 'danger',
      icon: 'icon:triangle-alert',
      text: state.notice.text,
      action: { id: 'notice-dismiss', label: '知道了', variant: 'link' },
    };
  }

  /** 可重来的二次确认：就地横幅 + 两个按钮，不打断页面。 */
  function pendingBlock() {
    if (!state.pending) return null;
    return [
      { type: 'banner', id: 'pending-banner', tone: 'warning', icon: 'icon:triangle-alert', text: state.pending.text },
      { type: 'actions', id: 'pending-actions', actions: [state.pending.confirm, state.pending.cancel] },
    ];
  }

  /**
   * 不可逆操作的阻断确认：块交给壳的 NativePanel 画成模态层。
   * 只用于「清除 Token」这类破坏用户凭据的操作；派生数据的清除仍走 pendingBlock。
   */
  function confirmBlock() {
    if (!state.confirm) return null;
    return {
      type: 'confirm',
      id: 'confirm-dialog',
      tone: 'danger',
      title: state.confirm.title,
      text: state.confirm.text,
      confirm: state.confirm.confirm,
      cancel: state.confirm.cancel,
    };
  }

  function oauthBlocks() {
    const blocks = [{
      type: 'banner',
      id: 'oauth-warning',
      tone: 'danger',
      icon: 'icon:triangle-alert',
      text: '下面步骤中不要重新点「获取 Token」，否则验证码会失效，需要重来。',
    }];
    if (state.oauth.url) {
      blocks.push({
        type: 'code',
        id: 'oauth-url',
        language: '登录地址',
        text: state.oauth.url,
        copyable: true,
        wrap: true,
      });
    } else {
      blocks.push(text('oauth-no-url', '还没有登录地址：点顶栏「重新获取登录地址」生成。'));
    }
    blocks.push({
      type: 'section',
      id: 'oauth-steps',
      title: '登录步骤',
      blocks: [
        text(
          'oauth-step-1',
          '1. 浏览器通常会自动打开 Pixiv 登录页；若没有，把上面的地址粘贴到已登录的浏览器新标签页回车。',
        ),
        text(
          'oauth-step-2',
          '2. 登录后页面会跳转或报「协议未知，无法导航」。按 F12 打开 Console，'
            + '从报错或 Network 里找到 callback?...&code=XXXX，复制 code= 后面那串。',
        ),
        text('oauth-step-3', '3. 粘贴到下面并点「完成登录」；授权码几分钟内有效。'),
        {
          type: 'form',
          id: 'oauth-form',
          fields: [{
            key: 'code',
            label: '授权码 code',
            type: 'text',
            value: state.oauth.code,
            placeholder: '粘贴 code= 后面的字符串',
          }],
          submit: { id: 'oauth-finish', label: '完成登录' },
        },
      ],
    });
    return blocks;
  }

  function buildToolbar() {
    if (state.view === 'oauth') {
      return [
        { id: 'oauth-close', label: '返回', icon: 'icon:arrow-left' },
        {
          id: 'oauth-restart', label: '重新获取登录地址', icon: 'icon:key-round',
          disabled: state.submitting,
        },
      ];
    }
    const busy = taskBusy();
    return [
      {
        id: 'refresh-status', label: '刷新状态', icon: 'icon:refresh-cw',
        disabled: state.submitting,
      },
      { id: 'oauth-start', label: '获取 Token', icon: 'icon:key-round', disabled: busy },
      { id: 'open-config', label: '画师名单文件', icon: 'icon:folder-open', disabled: busy },
      { id: 'open-settings', label: '设置', icon: 'icon:settings', disabled: busy },
    ];
  }

  function buildBlocks() {
    // 失败横幅在每个视图都要能出现：OAuth 失败的提示不能因为换了视图就丢掉。
    if (state.view === 'oauth') return collect(failureBanner(), oauthBlocks());
    if (state.statusError && !state.status) {
      return collect(failureBanner(), [{
        type: 'empty',
        id: 'load-error',
        icon: 'icon:triangle-alert',
        text: '读取同步状态失败',
        hint: state.statusError + '。请确认「图片相册」已加载，然后重试。',
        submit: { id: 'reload', label: '重试' },
      }]);
    }
    if (!state.status) {
      return [{
        type: 'empty',
        id: 'loading',
        icon: 'icon:loader',
        text: '正在读取同步状态…',
        hint: '原生面板与插件后端通过本机接口通信。',
      }];
    }
    return mainBlocks();
  }

  // ---------- 数据 ----------

  async function loadStatus() {
    try {
      const result = await Bridge.call('get_status');
      if (result && typeof result === 'object') {
        state.status = result;
        state.tokenConfigured = !!result.token_configured;
        state.statusError = '';
        // 校验从"在跑"变成"不在跑"的这一刻补一条结果提示：进度块消失后，
        // 光看页面是看不出校验完成还是被停掉的。
        const library = result.library || null;
        const busy = !!(library && library.busy);
        if (state.libraryBusy && !busy) {
          state.notice = {
            tone: (library && library.task && String(library.task.state) === 'done')
              ? 'success' : 'muted',
            text: verifySummary(library && library.task),
          };
        }
        state.libraryBusy = busy;
      } else {
        state.statusError = '后端返回的状态不是对象';
      }
    } catch (e) {
      state.statusError = errText(e);
    }
  }

  // ---------- 统一刷新（被动同步 / 校验） ----------
  //
  // 已下载记录是"本地图片树 → 作品集合"的派生数据，由共享刷新基建驱动。控件只渲染
  // 状态并读一次投影，**它自己不扫盘** —— 被动同步必须由持有句柄的本页发起：打开面板
  // 时一次，之后由 `onChange` 在报告有变化时重读状态。本页是隐藏的逻辑 iframe，
  // 所以控件挂在隐藏节点上（不占面板版面）。

  /** 挂载刷新控件；`/shell/freshness.js` 缺失时静默跳过，不影响其余动作。 */
  function mountFreshness() {
    if (typeof window.Freshness === 'undefined' || !window.Freshness
        || typeof window.Freshness.mount !== 'function') return;
    let host = document.getElementById('pixiv-freshness');
    if (!host) {
      host = document.createElement('div');
      host.id = 'pixiv-freshness';
      host.hidden = true;
      document.body.appendChild(host);
    }
    try {
      freshness = window.Freshness.mount({
        plugin: 'pixiv-sync',
        container: host,
        unit: '张',
        // 被动同步/校验发现本地内容变化：索引条目数与待更新标记都在 get_status.library 里。
        onChange: function () { void reloadAfterSync(); },
      });
    } catch (e) {
      freshness = null;   // 控件不可用不影响面板其余部分
    }
  }

  /** 被动同步一次（去抖与单飞在控件与壳侧引擎里）。 */
  function syncFreshness() {
    if (freshness && typeof freshness.autoSync === 'function') {
      freshness.autoSync('');
    }
  }

  /** 报告有变化时重读本地内容：只更新状态，不重建 OAuth 表单里正在输入的值。 */
  async function reloadAfterSync() {
    if (state.disposed) return;
    await loadStatus();
    if (state.disposed) return;
    if (isMain() && signature() !== lastSignature) render();
  }

  // ---------- 轮询 ----------

  function stopPoll() {
    if (pollTimer) {
      clearTimeout(pollTimer);
      pollTimer = null;
    }
  }

  function schedulePoll(delay) {
    if (state.disposed || pollTimer) return;
    const wait = Number.isFinite(delay) && delay >= 0 ? delay : STATUS_IDLE_MS;
    pollTimer = setTimeout(tick, wait);
  }

  function restartPoll(delay) {
    stopPoll();
    schedulePoll(delay);
  }

  function nextDelay() {
    if (taskActive(taskOf())) return STATUS_RUNNING_MS;
    // 本地校验在跑时也要快轮询：它的进度是逐目录推进的，8s 一次会像卡住。
    if (state.status && state.status.library && state.status.library.busy) {
      return STATUS_RUNNING_MS;
    }
    if (num(state.status && state.status.rate_limit_cooldown) > 0) return STATUS_COOLDOWN_MS;
    return STATUS_IDLE_MS;
  }

  async function tick() {
    pollTimer = null;
    if (state.disposed) return;
    await loadStatus();
    if (state.disposed) return;
    if (isMain() && signature() !== lastSignature) render();
    schedulePoll(nextDelay());
  }

  function dispose() {
    state.disposed = true;
    stopPoll();
    // 控件在挂载时就开始轮询状态，文档销毁前把它停掉。
    if (freshness && typeof freshness.destroy === 'function') freshness.destroy();
    freshness = null;
  }

  async function init() {
    if (state.initialized) return;
    state.initialized = true;
    mountFreshness();
    await loadStatus();
    if (state.disposed) return;
    render();
    restartPoll(STATUS_RUNNING_MS);
    // 打开面板时做一次被动同步：外部改动（手动放进下载根、另一个窗口删了文件）
    // 只有走过一趟同步才会反映到「本地内容」的索引条目数与待更新标记上。
    syncFreshness();
  }

  // ---------- 动作 ----------

  async function startTask(method, label) {
    state.pending = null;
    try {
      const result = await Bridge.call(method);
      if (result && result.ok === false) {
        state.notice = { tone: 'danger', text: label + '启动失败：' + (result.error || '未知错误') };
      } else {
        state.notice = { tone: 'default', text: label + '已启动，进度会自动刷新' };
      }
    } catch (e) {
      state.notice = { tone: 'danger', text: label + '启动失败：' + errText(e) };
    }
    await loadStatus();
    restartPoll(STATUS_RUNNING_MS);
  }

  async function refreshNow() {
    // loadStatus 可能在"校验刚结束"时补一条结果提示；手动刷新不能把它清掉。
    const previous = state.notice;
    await loadStatus();
    if (state.statusError) {
      state.notice = { tone: 'danger', text: '状态刷新失败：' + state.statusError };
    } else if (state.notice === previous) {
      state.notice = null;
    }
    restartPoll(nextDelay());
  }

  async function cancelTask() {
    state.pending = null;
    // 乐观取消：点下去立刻就是"已取消"，不等后台线程。后端 `cancel_task` 会
    // 立刻把任务定稿为 cancelled，在途请求的结果随后被丢弃，所以两边看到的是
    // 同一个状态；这里先改本地状态是为了让按钮点击反馈不依赖一次网络往返。
    const task = taskOf();
    if (task) {
      task.state = 'cancelled';
      task.current = '';
      task.finished_at = task.finished_at || Date.now() / 1000;
    }
    state.notice = { tone: 'muted', text: '已取消' };
    render();
    try {
      const result = await Bridge.call('cancel_task');
      if (result && result.ok === false) {
        state.notice = { tone: 'danger', text: '取消失败：' + (result.error || '未知错误') };
      }
    } catch (e) {
      state.notice = { tone: 'danger', text: '取消失败：' + errText(e) };
    }
    await loadStatus();
    restartPoll(nextDelay());
  }

  async function retryFailed() {
    state.pending = null;
    try {
      const result = await Bridge.call('retry_failed');
      if (result && result.ok === false) {
        state.notice = { tone: 'danger', text: '重试启动失败：' + (result.error || '未知错误') };
      } else {
        const kind = result && result.kind === 'bookmarks' ? '同步收藏作品' : '同步关注作品';
        state.notice = {
          tone: 'default',
          text: '已清除 ' + num(result && result.cleared) + ' 条失败跳过记录，并按「' + kind + '」重新同步',
        };
      }
    } catch (e) {
      state.notice = { tone: 'danger', text: '重试启动失败：' + errText(e) };
    }
    await loadStatus();
    restartPoll(STATUS_RUNNING_MS);
  }

  async function startOauth() {
    state.pending = null;
    state.view = 'oauth';
    state.oauth.code = '';
    try {
      const result = await Bridge.call('start_oauth');
      if (result && result.ok === false) {
        state.oauth.url = '';
        state.notice = { tone: 'danger', text: '获取登录地址失败：' + (result.error || '未知错误') };
      } else {
        state.oauth.url = String((result && result.url) || '');
        state.notice = { tone: 'muted', text: '登录地址已生成，浏览器通常会自动打开' };
      }
    } catch (e) {
      state.oauth.url = '';
      state.notice = { tone: 'danger', text: '获取登录地址失败：' + errText(e) };
    }
  }

  async function finishOauth(payload) {
    const values = (payload && payload.values) || {};
    const code = String(values.code == null ? '' : values.code).trim();
    state.oauth.code = code;
    if (!code) {
      state.notice = { tone: 'danger', text: '请先粘贴授权码 code' };
      return;
    }
    try {
      const result = await Bridge.call('finish_oauth', code);
      if (result && result.ok) {
        state.oauth.url = '';
        state.oauth.code = '';
        state.view = 'main';
        state.notice = {
          tone: 'default',
          text: 'Token 获取成功并已保存' + (result.user_id ? '（用户 ' + result.user_id + '）' : ''),
        };
        await loadStatus();
        restartPoll(STATUS_RUNNING_MS);
      } else {
        state.notice = { tone: 'danger', text: '获取失败：' + ((result && result.error) || '未知错误') };
      }
    } catch (e) {
      state.notice = { tone: 'danger', text: '获取失败：' + errText(e) };
    }
  }

  async function clearToken() {
    state.pending = null;
    // 确认块一被移除，壳的模态层就关闭（开关由 blocks 派生）。
    state.confirm = null;
    try {
      // 空字符串是后端约定的"清除凭据"；掩码原样回传才表示"不改动"。
      const result = await Bridge.call('save_settings', { refresh_token: '' });
      if (result && result.success === false) {
        state.notice = { tone: 'danger', text: '清除失败：' + (result.error || '未知错误') };
      } else {
        state.notice = { tone: 'success', text: '已清除保存的 Refresh Token' };
      }
    } catch (e) {
      state.notice = { tone: 'danger', text: '清除失败：' + errText(e) };
    }
    await loadStatus();
  }

  async function openConfig() {
    try {
      const result = await Bridge.call('open_config');
      if (result && result.ok === false) {
        state.notice = { tone: 'danger', text: result.error || '打开画师名单文件失败' };
      } else {
        state.notice = {
          tone: 'default',
          text: '已打开画师名单文件所在目录' + (result && result.file ? '：' + result.file : ''),
        };
      }
    } catch (e) {
      state.notice = { tone: 'danger', text: '打开画师名单文件失败：' + errText(e) };
    }
  }

  function askRetryFailed() {
    state.notice = null;
    state.pending = {
      text: '清除失败跳过记录并立即重新同步一次？同步会先刷新名单再下载新作。',
      confirm: { id: 'retry-failed-confirm', label: '清除并重试', variant: 'danger' },
      cancel: { id: 'pending-cancel', label: '取消' },
    };
  }

  /**
   * 本地内容：索引概要（条目数 / 上次校验 / 是否待更新）与校验进度。
   *
   * 校验是后台任务（几万个文件、几十秒量级），进度由 `get_status.library` 投影过来：
   * 目录数 processed/total + 当前目录。空闲时保留一行概要，否则点完「校验本地内容」
   * 结果只留在一次性 Toast 里，面板上看不出上次校验过什么、索引是否落后。
   */
  function librarySection() {
    const lib = (state.status && state.status.library) || null;
    if (!lib || lib.available === false) return null;
    const entries = num(lib.entries);
    const verifiedAt = num(lib.last_verify_at);
    if (!lib.busy && !entries && !verifiedAt) return null;

    const parts = [];
    if (entries > 0) parts.push('已索引 ' + fmt(entries) + ' 项');
    parts.push(verifiedAt > 0 ? '上次校验 ' + timeText(verifiedAt) : '尚未校验');
    if (lib.version_stale) {
      const stale = num(lib.stale_dirs);
      parts.push(stale > 0 ? '索引待更新（' + fmt(stale) + ' 个目录）' : '索引待更新');
    }
    if (!lib.busy) {
      const summary = lastLibrarySummary(lib.task);
      if (summary) parts.push(summary);
    }

    const blocks = [text('library-meta', parts.join(' · '))];
    if (lib.busy) {
      const task = lib.task || {};
      const total = num(task.total);
      const processed = num(task.processed);
      const detail = [
        task.current ? '正在处理：' + task.current : '正在遍历目录…',
        '已处理 ' + fmt(processed) + (total > 0 ? ' / ' + fmt(total) : '') + ' 个目录',
      ].join(' · ');
      blocks.push({
        type: 'progress',
        id: 'library-progress',
        value: processed,
        total: total,
        indeterminate: total <= 0,
        text: '校验本地内容',
        detail: detail,
        cancel: { id: 'cancel-verify', label: '停止校验', variant: 'link' },
      });
    }
    return { type: 'section', id: 'library-section', title: '本地内容', blocks: blocks };
  }

  /** 上次校验的变更计数（引擎任务 extra 里的 pass_end 摘要）；没有变化时返回空串。 */
  function lastLibrarySummary(task) {
    if (!task || taskActive(task)) return '';
    if (String(task.state) === 'cancelled') return '上次校验已停止';
    const extra = task.extra || {};
    const parts = [];
    if (num(extra.imported)) parts.push('新增 ' + fmt(extra.imported));
    if (num(extra.stale_removed)) parts.push('移除失效 ' + fmt(extra.stale_removed));
    if (num(extra.incomplete)) parts.push('缺页重新入队 ' + fmt(extra.incomplete));
    if (num(extra.zero_removed)) parts.push('清 0 字节 ' + fmt(extra.zero_removed));
    return parts.join(' · ');
  }

  /** 校验结束时的结果文案；计数来自引擎任务 extra 里的 pass_end 摘要。 */
  function verifySummary(task) {
    if (task && String(task.state) === 'cancelled') return '本地内容校验已停止';
    const extra = (task && task.extra) || {};
    const total = num(extra.total);
    const imported = num(extra.imported);
    const stale = num(extra.stale_removed);
    const incomplete = num(extra.incomplete);
    const parts = ['本地内容已校验：共 ' + fmt(total) + ' 张'];
    if (imported) parts.push('新增 ' + fmt(imported));
    if (stale) parts.push('移除失效 ' + fmt(stale));
    if (incomplete) parts.push('缺页重新入队 ' + fmt(incomplete));
    if (parts.length === 1) parts.push('没有变化');
    return parts.join('，');
  }

  /** 校验本地内容：启动后台任务，进度由轮询的 get_status.library 带回来。 */
  async function verifyLibrary() {
    try {
      const result = await Bridge.call('verify_downloaded');
      if (result && result.ok === false) {
        state.notice = { tone: 'danger', text: '校验启动失败：' + (result.error || '未知错误') };
      } else if (result && result.finished) {
        // 旧壳没有刷新基建：同步走盘后直接拿到了结果
        state.notice = { tone: 'success', text: verifySummary({ extra: result, state: 'done' }) };
      } else {
        state.notice = { tone: 'muted', text: '正在校验本地内容，进度见「本地内容」' };
      }
    } catch (e) {
      state.notice = { tone: 'danger', text: '校验启动失败：' + errText(e) };
    }
    await loadStatus();
    restartPoll(STATUS_RUNNING_MS);
  }

  /** 停止本地内容校验。 */
  async function cancelVerify() {
    try {
      const result = await Bridge.call('cancel_verify');
      state.notice = result && result.ok === false
        ? { tone: 'danger', text: '停止校验失败：' + (result.error || '未知错误') }
        : { tone: 'muted', text: '已请求停止校验' };
    } catch (e) {
      state.notice = { tone: 'danger', text: '停止校验失败：' + errText(e) };
    }
    await loadStatus();
  }

  /** 清除凭据不可逆：走壳的阻断确认（模态），不是就地横幅。 */
  function askLogout() {
    state.notice = null;
    state.confirm = {
      title: '清除 Pixiv Refresh Token？',
      text: '清除后需要重新登录才能同步，已下载的作品不受影响。',
      confirm: { id: 'logout-confirm', label: '清除 Token' },
      cancel: { id: 'confirm-cancel', label: '取消' },
    };
  }

  async function handleAction(action, payload) {
    if (!action) return;
    // 两个「取消」任何时候都要能关闭确认层，否则弹窗会被 submitting 卡住。
    if (state.submitting && action !== 'pending-cancel' && action !== 'confirm-cancel') return;
    state.submitting = true;
    try {
      switch (action) {
        case 'reload':
          state.notice = null;
          await loadStatus();
          restartPoll(nextDelay());
          break;
        case 'refresh-status':
          await refreshNow();
          break;
        case 'sync-following':
          await startTask('sync_following', '同步关注作品');
          break;
        case 'sync-bookmarks':
          await startTask('sync_bookmarks', '同步收藏作品');
          break;
        case 'cancel-task':
          await cancelTask();
          break;
        case 'retry-failed':
          askRetryFailed();
          break;
        case 'retry-failed-confirm':
          await retryFailed();
          break;
        case 'pending-cancel':
          state.pending = null;
          break;
        case 'notice-dismiss':
          // 失败横幅的显式关闭：用户看过之后由他自己清掉。
          state.notice = null;
          break;
        case 'confirm-cancel':
          state.confirm = null;
          break;
        case 'oauth-start':
        case 'oauth-restart':
          await startOauth();
          break;
        case 'oauth-finish':
          await finishOauth(payload);
          break;
        case 'oauth-close':
          state.view = 'main';
          state.oauth = { url: '', code: '' };
          state.notice = null;
          break;
        case 'logout':
          askLogout();
          break;
        case 'verify-library':
          await verifyLibrary();
          break;
        case 'cancel-verify':
          await cancelVerify();
          break;
        case 'logout-confirm':
          await clearToken();
          break;
        case 'open-settings':
          // 由壳拦截后在壳文档里打开 PluginSettingsModal，不会下发到这里；
          // 保留空分支说明动作归属，避免以后误加一份内容区设置表单。
          break;
        case 'settings-saved':
          // 壳弹窗已提示保存成功；这里只重读状态（凭据、下载根目录与计数可能变化）。
          state.pending = null;
          state.notice = null;
          await loadStatus();
          restartPoll(nextDelay());
          break;
        case 'open-config':
          await openConfig();
          break;
        default:
          break;
      }
    } catch (e) {
      state.notice = { tone: 'danger', text: '操作失败：' + errText(e) };
    } finally {
      state.submitting = false;
    }
    render();
  }

  // ---------- 启动 ----------

  client = window.NativeExtension.create({
    onInit: init,
    onAction: handleAction,
    onDispose: dispose,
  });
})();
