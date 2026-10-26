// Pixiv 同步 native 逻辑页。
//
// 本页不渲染界面：它把同步状态、任务进度与设置编译成壳的原生内容 blocks（见
// shell/frontend/src/core/plugin-content.ts），由壳的 NativePanel 与 SchemaRenderer
// 画出来。用户操作以 omnibox:native-action 回到这里，这里调后端后重新提交 blocks。
//
// 三条约束：
// - 本页在壳里是 display:none 的 iframe，页面内的 Toast / modal 用户看不见，
//   所以提示与两步确认一律用 text / actions block 表达，不弹窗；
// - 不读宿主 DOM，也不读 parent 上的宿主对象；后端能力只走 Bridge.call；
// - 轮询只在本页进行：重复的 omnibox:native-init 与反复调度都不会叠出第二个定时器，
//   iframe 被壳移除时（pagehide / beforeunload）停掉定时器。
(function () {
  'use strict';

  // 长时间运行的旧服务进程 / 旧缓存页面可能没有 /shell/native-client.js。
  // 那种情况下 NativeExtension undefined，直接调用会在装载期抛错，面板永远停在
  // “正在加载…”。这里先向壳报一条 native-error，让用户看到可操作的原因。
  if (!window.NativeExtension || typeof window.NativeExtension.create !== 'function') {
    try {
      if (window.parent && window.parent !== window) {
        window.parent.postMessage({
          type: 'omnibox:native-error',
          error: 'Pixiv 同步逻辑页缺少 /shell/native-client.js，请重启 OmniBox 后再打开本面板。',
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
    following: '同步画师',
    bookmarks: '同步喜欢',
    refresh_following: '刷新关注名单',
    refresh_bookmarks: '刷新喜欢名单',
  };
  const state = {
    view: 'main',           // main / oauth
    initialized: false,
    disposed: false,
    status: null,
    statusError: '',
    tokenConfigured: false,
    notice: null,           // {tone, text}
    pending: null,          // {text, confirm, cancel}：两步确认
    oauth: { url: '', code: '' },
    submitting: false,
  };

  let pollTimer = null;
  let client = null;
  let lastSignature = '';

  // ---------- 与壳的通道 ----------

  function render() {
    if (!client) return;
    lastSignature = signature();
    client.render(buildBlocks(), buildToolbar());
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

  // 主视图之外有表单（设置、OAuth 授权码）：轮询期间不重发 blocks，否则
  // SchemaRenderer 会按新 blocks 重建表单值，用户正在输入的内容被清掉。
  function signature() {
    return JSON.stringify({
      view: state.view,
      status: state.status,
      statusError: state.statusError,
      tokenConfigured: state.tokenConfigured,
      notice: state.notice,
      pending: state.pending,
    });
  }

  // ---------- blocks ----------

  function pushText(blocks, id, value, tone) {
    blocks.push({ type: 'text', id: id, text: value, tone: tone || 'default' });
  }

  function statusItems() {
    const status = state.status || {};
    return [
      { title: 'Token', sub: state.tokenConfigured ? '已配置' : '未配置' },
      { title: '下载根目录', sub: status.root_dir || '未知' },
      {
        title: '画师名单',
        sub: num(status.selected_artists) > 0
          ? num(status.selected_artists) + ' 位（只同步名单中的画师）'
          : '全部关注画师',
      },
    ];
  }

  /** 一行摘要：旧页两行状态栏压成可扫读的计数摘要，放在 kv 之上。 */
  function summaryText() {
    const status = state.status || {};
    const parts = [
      '已下载 ' + fmt(status.downloaded_total) + ' 张',
      '关注 ' + fmt(status.following_total)
        + '（待下 ' + fmt(status.pending_following) + '）',
      '喜欢 ' + fmt(status.bookmarks_total)
        + '（待下 ' + fmt(status.pending_bookmarks) + '）',
    ];
    if (num(status.other_done)) parts.push('其他 ' + fmt(status.other_done));
    if (num(status.failed_skipped)) parts.push('失败跳过 ' + fmt(status.failed_skipped));
    return parts.join(' · ');
  }

  function statusBlocks(blocks) {
    blocks.push({ type: 'heading', id: 'status-heading', text: '同步概览' });
    blocks.push({ type: 'text', id: 'status-summary', text: summaryText() });
    blocks.push({ type: 'kv', id: 'status-kv', items: statusItems() });
    const cooldown = num((state.status || {}).rate_limit_cooldown);
    if (cooldown > 0) {
      pushText(
        blocks, 'cooldown',
        'Pixiv 限流冷却约 ' + clock(cooldown) + '，建议冷却结束后再同步。',
        'danger',
      );
    }
    if (state.statusError && state.status) {
      pushText(blocks, 'status-warning', '状态刷新失败：' + state.statusError, 'danger');
    }
  }

  function taskBlocks(blocks) {
    const task = taskOf();
    blocks.push({ type: 'heading', id: 'task-heading', text: '同步任务' });
    if (!task) {
      pushText(
        blocks, 'task-empty',
        '尚无同步记录。点下方「同步画师」或「同步喜欢」开始；两者都会先更新名单再下载新作。',
        'muted',
      );
      return;
    }
    const label = TASK_STATES[String(task.state)] || String(task.state || '未知状态');
    if (taskActive(task)) {
      const total = num(task.total);
      const done = num(task.done);
      const percent = total > 0 ? Math.min(100, Math.round(done / total * 100)) : 0;
      blocks.push({
        type: 'progress',
        id: 'task-progress',
        value: done,
        total: total,
        text: taskKindLabel(task) + ' · ' + label,
        detail: task.current ? '正在处理：' + task.current : '准备中',
        cancel: { id: 'cancel-task', label: '取消任务' },
      });
      pushText(
        blocks, 'task-meta',
        '作品 ' + fmt(done) + '/' + fmt(total) + '（' + percent + '%）'
          + ' · 下载 ' + fmt(task.downloaded)
          + ' · 跳过 ' + fmt(task.skipped)
          + ' · 失败 ' + fmt(task.failed),
        'muted',
      );
      return;
    }
    const summary = taskKindLabel(task) + ' · ' + label
      + ' · 作品 ' + fmt(task.done) + '/' + fmt(task.total)
      + '，下载 ' + fmt(task.downloaded)
      + '，跳过 ' + fmt(task.skipped)
      + '，失败 ' + fmt(task.failed);
    pushText(blocks, 'task-summary', summary, task.state === 'failed' ? 'danger' : 'muted');
    if (task.error) pushText(blocks, 'task-error', '错误：' + task.error, 'danger');
    if (task.finished_at) {
      pushText(blocks, 'task-finished', '上次结束：' + timeText(task.finished_at), 'muted');
    }
  }

  function mainBlocks() {
    const blocks = [];
    statusBlocks(blocks);
    taskBlocks(blocks);

    if (!state.tokenConfigured) {
      blocks.push({
        type: 'empty',
        id: 'no-token',
        text: '尚未配置 Pixiv Refresh Token',
        hint: '没有 Token 无法同步。用「获取 Token」走内置 OAuth 登录，或在设置里粘贴 refresh_token。',
        submit: { id: 'oauth-start', label: '获取 Token' },
      });
    }

    const busy = taskBusy();
    // 没有 Token 时同步/刷新/重试都会在后台线程里认证失败，按钮直接禁用，
    // 把入口收窄到「获取 Token」与顶栏设置。
    const locked = busy || !state.tokenConfigured;
    blocks.push({ type: 'heading', id: 'actions-heading', text: '操作' });
    blocks.push({
      type: 'actions',
      id: 'main-actions',
      actions: [
        { id: 'sync-following', label: '同步画师', icon: 'icon:download', variant: 'primary', disabled: locked },
        { id: 'sync-bookmarks', label: '同步喜欢', icon: 'icon:heart', disabled: locked },
        { id: 'refresh-following', label: '刷新关注名单', icon: 'icon:refresh-cw', disabled: locked },
        { id: 'refresh-bookmarks', label: '刷新喜欢名单', icon: 'icon:refresh-cw', disabled: locked },
      ],
    });

    // 运行中的取消在 progress 块里；这里只留维护动作，避免同一屏出现两个「取消」。
    blocks.push({
      type: 'actions',
      id: 'maintenance-actions',
      actions: [{
        id: 'retry-failed', label: '重试失败作品', icon: 'icon:trash-2', variant: 'danger',
        disabled: locked,
      }],
    });

    const account = [];
    if (state.tokenConfigured) {
      account.push({ id: 'logout', label: '清除 Token', variant: 'danger', disabled: busy });
    }
    if (account.length) blocks.push({ type: 'actions', id: 'account-actions', actions: account });

    pushText(
      blocks, 'hint',
      '下载的画作会出现在「图片相册」中，重复作品按 Pixiv 作品 ID 自动跳过；'
        + '下载目录与 Token 等在设置弹窗里修改。',
      'muted',
    );
    return blocks;
  }

  function oauthBlocks() {
    const blocks = [{ type: 'heading', id: 'oauth-heading', text: '获取 Pixiv Token' }];
    pushText(
      blocks, 'oauth-warning',
      '下面步骤中不要重新点「获取 Token」，否则验证码会失效，需要重来。',
      'danger',
    );
    if (state.oauth.url) {
      blocks.push({
        type: 'kv',
        id: 'oauth-url',
        items: [{ title: '登录地址（选中后复制到已登录的浏览器标签页）', sub: state.oauth.url }],
      });
    } else {
      pushText(blocks, 'oauth-no-url', '还没有登录地址：点「重新获取登录地址」生成。', 'muted');
    }
    pushText(
      blocks, 'oauth-step-1',
      '1. 浏览器通常会自动打开 Pixiv 登录页；若没有，把上面的地址粘贴到已登录的浏览器新标签页回车。',
      'muted',
    );
    pushText(
      blocks, 'oauth-step-2',
      '2. 登录后页面会跳转或报「协议未知，无法导航」。按 F12 打开 Console，'
        + '从报错或 Network 里找到 callback?...&code=XXXX，复制 code= 后面那串。',
      'muted',
    );
    pushText(
      blocks, 'oauth-step-3',
      '3. 粘贴到下面并点「完成登录」；授权码几分钟内有效。',
      'muted',
    );
    blocks.push({
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
    });
    return blocks;
  }

  function pendingBlocks(blocks) {
    pushText(blocks, 'pending-text', state.pending.text, 'danger');
    blocks.push({
      type: 'actions',
      id: 'pending-actions',
      actions: [state.pending.confirm, state.pending.cancel],
    });
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
    const blocks = [];
    if (state.notice) {
      blocks.push({
        type: 'text', id: 'notice', text: state.notice.text,
        tone: state.notice.tone || 'default',
      });
    }
    if (state.pending) pendingBlocks(blocks);
    if (state.view === 'oauth') return blocks.concat(oauthBlocks());
    if (state.statusError && !state.status) {
      blocks.push({ type: 'heading', id: 'load-error-heading', text: '同步状态不可用' });
      blocks.push({
        type: 'empty',
        id: 'load-error',
        text: '读取同步状态失败',
        hint: state.statusError + '。请确认「图片相册」已加载，然后重试。',
        submit: { id: 'reload', label: '重试' },
      });
      return blocks;
    }
    if (!state.status) {
      blocks.push({
        type: 'empty',
        id: 'loading',
        text: '正在读取同步状态…',
        hint: '原生面板与插件后端通过本机接口通信。',
      });
      return blocks;
    }
    return blocks.concat(mainBlocks());
  }

  // ---------- 数据 ----------

  async function loadStatus() {
    try {
      const result = await Bridge.call('get_status');
      if (result && typeof result === 'object') {
        state.status = result;
        state.tokenConfigured = !!result.token_configured;
        state.statusError = '';
      } else {
        state.statusError = '后端返回的状态不是对象';
      }
    } catch (e) {
      state.statusError = errText(e);
    }
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
  }

  async function init() {
    if (state.initialized) return;
    state.initialized = true;
    await loadStatus();
    if (state.disposed) return;
    render();
    restartPoll(STATUS_RUNNING_MS);
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
    await loadStatus();
    state.notice = state.statusError
      ? { tone: 'danger', text: '状态刷新失败：' + state.statusError }
      : null;
    restartPoll(nextDelay());
  }

  async function cancelTask() {
    state.pending = null;
    try {
      const result = await Bridge.call('cancel_task');
      if (result && result.ok === false) {
        state.notice = { tone: 'danger', text: '取消失败：' + (result.error || '未知错误') };
      } else {
        state.notice = { tone: 'muted', text: '已请求取消，任务会在下一个检查点停止' };
      }
    } catch (e) {
      state.notice = { tone: 'danger', text: '取消失败：' + errText(e) };
    }
    await loadStatus();
    restartPoll(STATUS_RUNNING_MS);
  }

  async function retryFailed() {
    state.pending = null;
    try {
      const result = await Bridge.call('retry_failed');
      if (result && result.ok === false) {
        state.notice = { tone: 'danger', text: '重试启动失败：' + (result.error || '未知错误') };
      } else {
        const kind = result && result.kind === 'bookmarks' ? '同步喜欢' : '同步画师';
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
    try {
      // 空字符串是后端约定的"清除凭据"；掩码原样回传才表示"不改动"。
      const result = await Bridge.call('save_settings', { refresh_token: '' });
      if (result && result.success === false) {
        state.notice = { tone: 'danger', text: '清除失败：' + (result.error || '未知错误') };
      } else {
        state.notice = { tone: 'default', text: '已清除保存的 Refresh Token' };
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

  function askLogout() {
    state.notice = null;
    state.pending = {
      text: '清除已保存的 Pixiv Refresh Token？清除后需要重新登录才能同步。',
      confirm: { id: 'logout-confirm', label: '清除 Token', variant: 'danger' },
      cancel: { id: 'pending-cancel', label: '取消' },
    };
  }

  async function handleAction(action, payload) {
    if (!action) return;
    if (state.submitting && action !== 'pending-cancel') return;
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
          await startTask('sync_following', '同步画师');
          break;
        case 'sync-bookmarks':
          await startTask('sync_bookmarks', '同步喜欢');
          break;
        case 'refresh-following':
          await startTask('refresh_following_lists', '刷新关注名单');
          break;
        case 'refresh-bookmarks':
          await startTask('refresh_bookmarks_lists', '刷新喜欢名单');
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
