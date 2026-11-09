// 相册清理 native 逻辑页。
//
// 本页不渲染界面：它把状态编译成壳的原生内容 blocks（见 shell/frontend/src/core/plugin-content.ts），
// 由壳的 NativePanel 与 SchemaRenderer 画出来。用户操作以 omnibox:native-action 回到这里，
// 这里调后端后重新提交 blocks。宿主能力只走 Bridge，不读宿主窗口上的对象。
(function () {
  'use strict';

  const PAGE_SIZE = 20;
  const FILES_PAGE_SIZE = 12;
  const FILES_PAGE_STEP = 24;
  const POLL_MS = 600;
  const SCAN_START_TIMEOUT_MS = 5000;

  const state = {
    mode: 'dupe',
    groups: [],
    scanned: 0,
    cached: false,
    stale: false,
    error: false,
    selected: new Set(),
    visibleCount: PAGE_SIZE,
    /** 每组的文件窗口：key 是组索引，缺省 FILES_PAGE_SIZE。 */
    fileLimits: {},
    pendingDelete: false,
    notice: null,
    busy: false,
    task: null,
  };

  let freshness = null;
  let pollTimer = null;
  let initialized = false;
  let client = null;
  let refreshPromise = null;
  let scanPending = false;
  let scanStartedAt = 0;
  /** 已作为 Toast 发出去的通知键，避免同一条结果反复弹。 */
  let sentNotice = '';

  // ---------- 与壳的通道 ----------

  function render() {
    if (!client) return;
    const options = {};
    const notice = state.notice;
    // 成功与中性结果走一次性 Toast；失败留在常驻 banner 里，不能一闪而过。
    if (notice && notice.text && notice.tone !== 'danger') {
      const key = (notice.tone || 'default') + ':' + notice.text;
      if (key !== sentNotice) {
        sentNotice = key;
        options.toast = { text: notice.text, tone: notice.tone || 'default' };
      }
    } else {
      sentNotice = '';
    }
    client.render(buildBlocks(), buildToolbar(), options);
  }

  function errText(error) {
    if (error && error.message) return String(error.message);
    return String(error == null ? '' : error);
  }

  // ---------- 展示工具 ----------

  function displayPath(rel) {
    return String(rel == null ? '' : rel).replace(/^__/, '');
  }

  function formatSize(bytes) {
    const size = Number(bytes);
    if (!Number.isFinite(size) || size <= 0) return '';
    if (size >= 1024 * 1024) return (size / 1024 / 1024).toFixed(2) + ' MB';
    return Math.max(1, Math.round(size / 1024)) + ' KB';
  }

  // ---------- blocks ----------

  function fileLimitFor(index) {
    const limit = state.fileLimits[index];
    return Number.isFinite(limit) && limit > 0 ? limit : FILES_PAGE_SIZE;
  }

  function groupBlocks() {
    const visible = state.groups.slice(0, state.visibleCount);
    return visible.map(function (group, index) {
      const files = Array.isArray(group.files) ? group.files : [];
      const limit = fileLimitFor(index);
      const shown = files.slice(0, limit);
      const hidden = files.length - shown.length;
      const size = state.mode === 'dupe' && formatSize(group.size)
        ? ' · ' + formatSize(group.size)
        : '';
      const title = (state.mode === 'dupe' ? '重复组' : '相似组')
        + ' · ' + files.length + ' 张' + size
        + (hidden > 0 ? ' · 显示 ' + shown.length + '/' + files.length : '');
      const actions = [
        { id: 'select-group', label: '全选组', payload: { index: index } },
        { id: 'keep-one-group', label: '只留一张', payload: { index: index } },
      ];
      if (hidden > 0) {
        actions.push({
          id: 'show-group-more',
          label: '显示更多（还有 ' + hidden + ' 张）',
          payload: { index: index },
        });
      }
      return {
        title: title,
        actions: actions,
        items: shown.map(function (rel) {
          return {
            value: String(rel),
            label: displayPath(rel),
            image: Bridge.thumbUrl(String(rel)),
            checked: state.selected.has(String(rel)),
          };
        }),
      };
    });
  }

  function emptyBlock() {
    if (state.error) {
      return {
        type: 'empty', id: 'empty-error', text: '读取结果失败',
        hint: '请确认「图片相册」已加载、相册目录可访问，然后重新校验',
        submit: { id: 'reload', label: '重试' },
      };
    }
    if (!state.cached) {
      return {
        type: 'empty', id: 'empty-unscanned', text: '尚未校验',
        hint: '点「校验」开始比对（首次会为每张图算指纹，图库越大越久）',
        submit: { id: 'verify', label: '开始校验' },
      };
    }
    const other = state.mode === 'dupe' ? 'similar' : 'dupe';
    return {
      type: 'empty', id: 'empty-none',
      text: '未发现' + (state.mode === 'dupe' ? '完全重复' : '相似') + '图片',
      hint: '可切换模式；相似判定阈值在「设置」里调整',
      submit: {
        id: 'switch-mode',
        label: '查看' + (other === 'similar' ? '相似图片' : '完全重复'),
        payload: { mode: other },
      },
    };
  }

  function buildToolbar() {
    const busy = state.busy;
    return [
      {
        id: 'switch-mode', label: '完全重复',
        variant: state.mode === 'dupe' ? 'primary' : 'default',
        active: state.mode === 'dupe',
        payload: { mode: 'dupe' }, disabled: busy,
      },
      {
        id: 'switch-mode', label: '相似图片',
        variant: state.mode === 'similar' ? 'primary' : 'default',
        active: state.mode === 'similar',
        payload: { mode: 'similar' }, disabled: busy,
      },
      { id: 'verify', label: '校验', icon: 'icon:refresh-cw', disabled: busy },
      { id: 'open-settings', label: '设置', icon: 'icon:settings', disabled: busy },
    ];
  }

  function buildBlocks() {
    const blocks = [];
    const busy = state.busy;

    let statusText = '';
    let statusDot = 'muted';
    if (busy) {
      statusText = state.task && state.task.current
        ? '校验中：' + state.task.current
        : '正在校验…';
      statusDot = 'accent';
    } else if (!state.cached) {
      statusText = '尚未校验';
      statusDot = 'muted';
    } else {
      statusText = '已扫描 ' + state.scanned + ' 张'
        + (state.stale ? ' · 磁盘有变化，建议重新校验' : '');
      statusDot = state.stale ? 'warning' : 'success';
    }
    blocks.push({ type: 'text', id: 'scanned', text: statusText, tone: 'muted', dot: statusDot });

    // 失败：常驻横幅 + 「知道了」，不能被下一次提交静默吞掉。
    if (state.notice && state.notice.tone === 'danger') {
      blocks.push({
        type: 'banner', id: 'notice-alert', tone: 'danger', icon: 'icon:triangle-alert',
        text: state.notice.text,
        action: { id: 'notice-dismiss', label: '知道了', variant: 'link' },
      });
    }

    if (busy) {
      const task = state.task || {};
      const total = Number(task.total) || 0;
      blocks.push({
        type: 'progress', id: 'scan-progress',
        value: Number(task.processed) || 0,
        total: total,
        // 总数未知时不要用 0/N 假装进度，交给壳画进行中。
        indeterminate: total <= 0,
        text: task.state === 'running' ? '校验中' : '准备中',
        detail: task.current ? '正在处理：' + task.current : '',
        cancel: { id: 'cancel-scan', label: '取消' },
      });
    }

    // 删除不可逆：走壳的阻断确认（模态），确认块由 blocks 派生。
    if (state.pendingDelete) {
      blocks.push({
        type: 'confirm', id: 'delete-dialog', tone: 'danger',
        title: '删除 ' + state.selected.size + ' 张图片？',
        text: '删除后不可恢复，勾选的文件会直接从这里移除。',
        confirm: { id: 'delete-confirm', label: '确认删除' },
        cancel: { id: 'delete-cancel', label: '取消' },
      });
    }

    blocks.push({ type: 'text', id: 'selected-count', text: '已选 ' + state.selected.size + ' 张' });

    if (state.groups.length) {
      blocks.push({ type: 'checkbox-list', id: 'groups', groups: groupBlocks() });
      const remaining = state.groups.length - state.visibleCount;
      if (remaining > 0) {
        blocks.push({
          type: 'actions', id: 'more-actions',
          actions: [{ id: 'show-more', label: '显示更多（还有 ' + remaining + ' 组）' }],
        });
      }
    } else {
      blocks.push(emptyBlock());
    }

    blocks.push({
      type: 'actions', id: 'bulk-actions',
      actions: [
        { id: 'select-all', label: '全选', disabled: busy || !state.groups.length },
        { id: 'keep-one-all', label: '每组只留一张', disabled: busy || !state.groups.length },
        {
          id: 'delete-selected', label: '删除选中', variant: 'danger',
          disabled: busy || !state.selected.size,
        },
      ],
    });

    return blocks;
  }

  // ---------- 任务状态 ----------

  function snapshotTask() {
    if (!freshness || typeof freshness.state !== 'function') return null;
    try {
      return freshness.state();
    } catch (e) {
      return null;
    }
  }

  function syncTaskState() {
    const snap = snapshotTask();
    const task = (snap && snap.task) || null;
    const wasBusy = state.busy;
    state.busy = !!(snap && snap.busy);
    state.task = task;
    if (scanPending && task && task.done) {
      scanPending = false;
      void refreshAfterScan(snap);
      return;
    }
    if (wasBusy && !state.busy) {
      void refreshAfterScan(snap);
    }
  }

  /**
   * settle（被动同步发现变化 / 校验收尾）后的刷新：保留勾选与分页。
   *
   * 这一条与「切模式 / 显式重读」（`loadResults()`，会清空勾选与分页）不是同一件事：
   * 后台把数据变了，用户可能正在勾选，清空选择会让他白勾一遍。只把结果里已经不存在
   * 的文件从选中集合里剔掉。同一时刻只跑一次，后到的调用等前一次完成。
   */
  function reloadAfterSettle() {
    if (refreshPromise) return refreshPromise;
    refreshPromise = (async function () {
      await loadResults(true);
      render();
    })().finally(function () {
      refreshPromise = null;
    });
    return refreshPromise;
  }

  /** 校验收尾：先重读缓存，再按任务终态给出提示。 */
  async function refreshAfterScan(snap) {
    const task = (snap && snap.task) || state.task || {};
    await reloadAfterSettle();
    if (task.cancelled) {
      state.notice = { tone: 'muted', text: '校验已取消，已完成的指纹保留' };
    } else if (task.state === 'done' && task.success === false) {
      state.notice = { tone: 'danger', text: '校验失败，请重试或到状态页查看' };
    } else {
      state.notice = { tone: 'default', text: '校验完成，已重新分组' };
    }
    render();
  }

  function startPolling() {
    if (pollTimer) return;
    pollTimer = setInterval(function () {
      const before = JSON.stringify({ busy: state.busy, task: state.task });
      syncTaskState();
      const after = JSON.stringify({ busy: state.busy, task: state.task });
      if (before !== after) render();
      if (scanPending && !state.busy && Date.now() - scanStartedAt > SCAN_START_TIMEOUT_MS) {
        scanPending = false;
        state.notice = { tone: 'danger', text: '未能启动校验，请稍后重试' };
        render();
      }
    }, POLL_MS);
  }

  // ---------- 数据 ----------

  function mountFreshness() {
    if (typeof Freshness === 'undefined' || !Freshness || typeof Freshness.mount !== 'function') return;
    let host = document.getElementById('cleaner-freshness');
    if (!host) {
      host = document.createElement('div');
      host.id = 'cleaner-freshness';
      host.hidden = true;
      document.body.appendChild(host);
    }
    freshness = Freshness.mount({
      plugin: 'image-cleaner',
      container: host,
      unit: '张',
      // 被动同步发现变化时重读缓存：分组结果会因指纹变化被标成过期，状态行要跟着变。
      // 走 settle 路径，勾选与分页保留（用户可能正在勾选）。
      onChange: function () { void reloadAfterSettle(); },
    });
  }

  async function loadResults(preserve) {
    let failed = false;
    try {
      const result = await Bridge.call('get_cached_scan', state.mode);
      const groups = result && Array.isArray(result.groups) ? result.groups : [];
      state.groups = groups.filter(function (group) {
        return group && Array.isArray(group.files) && group.files.length >= 2;
      });
      state.scanned = Number((result && result.scanned) || 0);
      state.cached = !!(result && result.cached);
      state.stale = !!(result && result.stale);
      state.error = false;
    } catch (e) {
      failed = true;
      state.groups = [];
      state.scanned = 0;
      state.cached = false;
      state.stale = false;
      state.error = true;
    }
    if (!preserve) {
      state.selected.clear();
      state.visibleCount = PAGE_SIZE;
      state.fileLimits = {};
      state.pendingDelete = false;
      return;
    }
    // 保留勾选与分页：只把结果里已经没有的文件从选中集合里剔掉。
    // 读取失败时同理（此时结果为空，选中集合自然清空）。
    if (failed) {
      state.selected.clear();
      state.pendingDelete = false;
      return;
    }
    const alive = new Set();
    state.groups.forEach(function (group) {
      group.files.forEach(function (rel) { alive.add(rel); });
    });
    Array.from(state.selected).forEach(function (rel) {
      if (!alive.has(rel)) state.selected.delete(rel);
    });
    if (!state.selected.size) state.pendingDelete = false;
  }

  async function init() {
    if (initialized) return;
    initialized = true;
    mountFreshness();
    await loadResults();
    syncTaskState();
    render();
    startPolling();
    // 打开面板时做一次被动同步：外部新增/删除的图片只有走过一趟同步才会让结果
    // 缓存被标成过期，否则「已扫描 N 张」会一直显示打开面板那一刻的结论。
    if (freshness && typeof freshness.autoSync === 'function') {
      freshness.autoSync('');
    }
  }

  // ---------- 动作 ----------

  async function runVerify() {
    state.notice = null;
    state.pendingDelete = false;
    if (!freshness) {
      state.notice = { tone: 'danger', text: '统一校验组件不可用' };
      return;
    }
    scanPending = true;
    scanStartedAt = Date.now();
    try {
      await freshness.verify();
    } catch (e) {
      scanPending = false;
      state.notice = { tone: 'danger', text: '校验启动失败：' + errText(e) };
      return;
    }
    if (freshness.supported === false) {
      scanPending = false;
      state.notice = { tone: 'danger', text: '本插件未接入统一校验' };
      return;
    }
    syncTaskState();
  }

  async function cancelScan() {
    state.notice = { tone: 'muted', text: '正在取消…' };
    try {
      if (freshness && typeof freshness.cancel === 'function') {
        await freshness.cancel();
      } else {
        await Bridge.callSystem('system_freshness_cancel', 'image-cleaner');
      }
    } catch (e) {
      state.notice = { tone: 'danger', text: '取消失败：' + errText(e) };
    }
  }

  async function confirmDelete() {
    const files = Array.from(state.selected);
    state.pendingDelete = false;
    if (!files.length) {
      state.notice = { tone: 'danger', text: '请先勾选要删除的图片' };
      return;
    }
    try {
      const result = await Bridge.call('delete_files', files);
      const deleted = new Set((result && Array.isArray(result.deleted)) ? result.deleted : []);
      const errors = (result && Array.isArray(result.errors)) ? result.errors : [];
      if (deleted.size) {
        state.groups = state.groups.map(function (group) {
          return {
            ...group,
            files: group.files.filter(function (rel) { return !deleted.has(rel); }),
          };
        }).filter(function (group) { return group.files.length >= 2; });
        state.selected.clear();
        state.fileLimits = {};
      }
      if (errors.length) {
        state.notice = { tone: 'danger', text: '部分删除失败：' + errors.join('；') };
      } else {
        state.notice = { tone: 'default', text: '已删除 ' + files.length + ' 张图片' };
      }
      if (freshness && typeof freshness.autoSync === 'function') freshness.autoSync('');
    } catch (e) {
      state.notice = { tone: 'danger', text: '删除请求失败：' + errText(e) };
    }
  }

  function selectGroup(index, keepFirst) {
    const group = state.groups[index];
    if (!group) return;
    group.files.forEach(function (rel, position) {
      if (keepFirst && position === 0) state.selected.delete(rel);
      else state.selected.add(rel);
    });
  }

  function selectAll() {
    state.groups.forEach(function (group) {
      group.files.forEach(function (rel) { state.selected.add(rel); });
    });
  }

  function keepOneAll() {
    state.groups.forEach(function (group) {
      group.files.forEach(function (rel, position) {
        if (position === 0) state.selected.delete(rel);
        else state.selected.add(rel);
      });
    });
  }

  async function handleAction(action, payload) {
    switch (action) {
      case 'switch-mode': {
        const requested = typeof payload === 'string' ? payload : (payload && payload.mode);
        if ((requested === 'dupe' || requested === 'similar') && requested !== state.mode) {
          state.mode = requested;
          state.notice = null;
          await loadResults();
        }
        break;
      }
      case 'toggle': {
        const value = payload && payload.value;
        if (typeof value === 'string' && value) {
          if (payload.checked) state.selected.add(value);
          else state.selected.delete(value);
          state.pendingDelete = false;
        }
        break;
      }
      case 'select-group':
      case 'keep-one-group': {
        if (state.busy) break;
        const index = typeof payload === 'number' ? payload : (payload && payload.index);
        selectGroup(index, action === 'keep-one-group');
        break;
      }
      case 'select-all':
        if (!state.busy) selectAll();
        break;
      case 'keep-one-all':
        if (!state.busy) keepOneAll();
        break;
      case 'show-more':
        state.visibleCount += PAGE_SIZE;
        break;
      case 'show-group-more': {
        const index = typeof payload === 'number' ? payload : (payload && payload.index);
        const group = state.groups[index];
        if (group) {
          const current = fileLimitFor(index);
          state.fileLimits[index] = Math.min(
            group.files.length, current + FILES_PAGE_STEP);
        }
        break;
      }
      case 'delete-selected':
        if (state.busy) break;
        if (!state.selected.size) {
          state.notice = { tone: 'danger', text: '请先勾选要删除的图片' };
          state.pendingDelete = false;
        } else {
          state.notice = null;
          state.pendingDelete = true;
        }
        break;
      case 'delete-confirm':
        await confirmDelete();
        break;
      case 'delete-cancel':
        state.pendingDelete = false;
        state.notice = null;
        break;
      case 'notice-dismiss':
        // 失败横幅的显式关闭：用户看过之后由他自己清掉。
        state.notice = null;
        break;
      case 'reload':
        state.notice = null;
        await loadResults();
        break;
      case 'verify':
        if (!state.busy) await runVerify();
        break;
      case 'cancel-scan':
        await cancelScan();
        break;
      case 'settings-saved':
        // 壳设置弹窗保存成功：阈值可能已变更，重读缓存（stale 标记会反映相似分组过期）。
        state.notice = null;
        await loadResults();
        break;
      default:
        break;
    }
    render();
  }

  // ---------- 启动 ----------

  client = window.NativeExtension.create({
    onInit: init,
    onAction: handleAction,
    onDispose: function () {
      if (pollTimer) { clearInterval(pollTimer); pollTimer = null; }
      // 控件在挂载时就开始轮询状态，文档销毁前把它停掉并摘掉节点。
      if (freshness && typeof freshness.destroy === 'function') freshness.destroy();
      freshness = null;
    },
  });

})();
