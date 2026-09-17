/**
 * group-mesh 插件前端。
 *
 * 只做四件事：读状态、切面板、渲染、把用户动作转成 Bridge.call。所有判定都在后端 ——
 * 前端不持有任何"谁能做什么"的规则（设计文档 §6.3：客户端隐藏按钮不构成授权）。
 *
 * 界面形态与仓库里其它插件一致（manga-library / media-player / image-cleaner）：
 * 壳的 `.view-sub-sidebar` 常驻侧栏 + `.view-body > .view-toolbar + .view-content`。
 * 面板切换是**纯显隐**（改 `data-active`，不销毁 DOM）：与壳"iframe 常驻、切走只是
 * v-show"的策略一致，也让所有 id 在任何面板下都存在，渲染函数不必关心当前在哪一页。
 * 面板的标题/副标题写在 index.html 的 `data-title` / `data-sub` 上（单一来源），
 * 新增一个面板 = 侧栏加一个 `button[data-panel]` + 一个 `section[data-panel]`。
 *
 * 远端分片（设备 → 共享项 → 目录 → 取回/上传）在 js/remote.js，由 init() 注入回调；
 * index.html 里 remote.js 必须排在 app.js 之前（契约见 tests/js/plugin_asset_contract.mjs）。
 */
(function () {
  'use strict';

  var state = {
    status: null,
    panel: 'machine'
  };

  // ── 工具 ────────────────────────────────────────────────────────────────

  function el(id) { return document.getElementById(id); }

  function toast(message, isError) {
    // 壳提供 Toast；独立打开本页时回退到自绘提示
    if (window.Toast && typeof window.Toast.success === 'function') {
      if (isError) { window.Toast.error(message); } else { window.Toast.success(message); }
      return;
    }
    var node = document.createElement('div');
    node.className = 'gm-toast' + (isError ? ' gm-toast-error' : '');
    node.textContent = message;
    document.body.appendChild(node);
    setTimeout(function () { node.remove(); }, 4200);
  }

  function call(method) {
    var args = Array.prototype.slice.call(arguments, 1);
    if (!window.Bridge || typeof window.Bridge.call !== 'function') {
      return Promise.reject(new Error('Bridge 不可用：请从 OmniBox 壳内打开本页面'));
    }
    return window.Bridge.call.apply(window.Bridge, [method].concat(args));
  }

  function escapeHtml(text) {
    return String(text === null || text === undefined ? '' : text)
      .replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;')
      .replace(/"/g, '&quot;').replace(/'/g, '&#39;');
  }

  /** 给刚渲染出来的条目编交错延迟（配合 effects.css 的 `.obx-stagger` 与
      行的 `--obx-i`）。Motion 由壳注入；脱离壳直接打开时安静跳过。 */
  function stagger(container, selector) {
    if (container && window.Motion && typeof window.Motion.stagger === 'function') {
      window.Motion.stagger(container, selector);
    }
  }

  /** 按状态给卡片换顶边色（.gm-card-ok/warn/error/muted）。状态只写在有意义的
      div 上，卡片本身由调用方拿 id 的直接父节点，避免再加一层包装。 */
  function cardOf(id, modifier) {
    var body = el(id);
    var card = body && body.closest ? body.closest('.gm-card') : null;
    if (!card) { return; }
    card.classList.remove('gm-card-ok', 'gm-card-warn', 'gm-card-error', 'gm-card-muted');
    if (modifier) { card.classList.add(modifier); }
  }

  // 弹窗用壳的 `.modal` + `.modal.active`（base.css）。
  // 不再自绘 `.gm-modal[data-open]`：壳的默认态没有任何 display 声明，因此不存在
  // "作者样式的 display:flex 盖掉浏览器给 [hidden] 的 display:none"那个级联陷阱
  // （事故记录见 docs/group-mesh-implementation-path.md §5.7，以及本插件 CSS 顶部注释）。
  function openModal(id) {
    var node = el(id);
    if (node) { node.classList.add('active'); }
  }

  function closeModal(id) {
    var node = el(id);
    if (node) { node.classList.remove('active'); }
  }

  function roleBadge(role) {
    if (!role) { return '<span class="gm-badge gm-badge-warn">不在名单</span>'; }
    var label = { owner: '群主', admin: '管理员', member: '成员' }[role] || role;
    return '<span class="gm-badge gm-badge-' + escapeHtml(role) + '">' + escapeHtml(label) + '</span>';
  }

  function shortKey(hex) {
    if (!hex) { return '-'; }
    return hex.slice(0, 16) + '…';
  }

  function button(label, handler, secondary) {
    var node = document.createElement('button');
    node.type = 'button';
    // 壳的 .btn 系列（base.css）：主操作用默认态，次要操作用 .btn-sm。
    node.className = secondary ? 'btn btn-sm' : 'btn';
    node.textContent = label;
    node.addEventListener('click', handler);
    return node;
  }

  function fillActions(container, buttons) {
    container.innerHTML = '';
    buttons.forEach(function (b) { container.appendChild(b); });
  }

  // ── 面板切换 ────────────────────────────────────────────────────────────

  function setPanel(name) {
    var sections = document.querySelectorAll('#gm-panels > .gm-panel');
    var found = false;
    Array.prototype.forEach.call(sections, function (section) {
      found = found || section.getAttribute('data-panel') === name;
    });
    if (!found) { return; }
    state.panel = name;
    Array.prototype.forEach.call(sections, function (section) {
      section.setAttribute('data-active',
        section.getAttribute('data-panel') === name ? 'true' : 'false');
    });
    Array.prototype.forEach.call(document.querySelectorAll('#gm-nav .gm-nav-item'), function (item) {
      item.setAttribute('data-active', item.getAttribute('data-panel') === name ? 'true' : 'false');
    });
    var section = document.querySelector('#gm-panels > .gm-panel[data-panel="' + name + '"]');
    if (section) {
      var title = el('gm-panel-title');
      var sub = el('gm-panel-sub');
      if (title) { title.textContent = section.getAttribute('data-title') || '团体组网'; }
      if (sub) { sub.textContent = section.getAttribute('data-sub') || ''; }
    }
    var content = el('gm-content');
    if (content) { content.scrollTop = 0; }
  }

  // ── 渲染 ────────────────────────────────────────────────────────────────

  function render() {
    var status = state.status;
    if (!status) { return; }

    el('kernel-missing').hidden = status.kernel.available;

    renderIdentity(status);
    renderRoster(status);
    renderShares(status);
    renderNode(status);
    renderUnsupported(status);
    renderSideSummary(status);
  }

  function protectionBadge(info) {
    var labels = {
      dpapi: 'Windows DPAPI',
      keyring: '系统 keyring',
      passphrase: '口令派生密钥',
      plain: '明文（未保护）'
    };
    var name = labels[(info && info.active) || ''] || (info && info.active) || '未知';
    if (info && info.protected) {
      return '<span class="gm-badge gm-badge-ok">已加密 · ' + escapeHtml(name) + '</span>';
    }
    return '<span class="gm-badge gm-badge-warn">未加密 · ' + escapeHtml(name) + '</span>';
  }

  function renderIdentity(status) {
    var body = el('identity-body');
    var actions = el('identity-actions');
    var identity = status.identity;

    if (!identity) {
      cardOf('identity-body', 'gm-card-muted');
      body.innerHTML = '<p class="gm-empty">本机还没有身份。创建后会生成主体密钥与首台设备密钥，' +
        '私钥保存在本机（<code>' + escapeHtml(status.identity_dir) + '</code>），不会经文件服务对外提供。</p>';
      fillActions(actions, [
        button('创建身份', function () {
          call('init_identity', { name: status.settings.principal_name || '' })
            .then(afterAction('身份已创建')).catch(showError);
        })
      ]);
      return;
    }

    var protection = identity.secret_protection || {};
    cardOf('identity-body', protection.protected ? 'gm-card-ok' : 'gm-card-warn');
    body.innerHTML = '<dl class="gm-kv">' +
      '<dt>主体</dt><dd>' + escapeHtml(identity.principal_name) + '</dd>' +
      '<dt>主体 ID</dt><dd>' + escapeHtml(identity.principal_id) + '</dd>' +
      '<dt>设备</dt><dd>' + escapeHtml(identity.device_name) + '（' + escapeHtml(identity.device_id) + '）</dd>' +
      '<dt>主体公钥</dt><dd>' + escapeHtml(shortKey(identity.principal_key)) + '</dd>' +
      '<dt>私钥保护</dt><dd>' + protectionBadge(identity.secret_protection) + '</dd>' +
      '</dl>';
    fillActions(actions, [
      button('显示公钥（发给群主登记）', function () {
        call('get_device_keys').then(function (res) {
          if (!res || !res.success) { toast((res && res.error) || '读取失败', true); return; }
          showInvite('本机公钥（主体 / 设备）',
            '主体: ' + res.principal + '\n设备: ' + res.device + '\n名称: ' + res.name);
        }).catch(showError);
      }, true),
      button('刷新', refresh, true)
    ]);
  }

  function renderRoster(status) {
    var body = el('roster-body');
    var actions = el('roster-actions');
    var roster = status.roster;

    if (!roster) {
      cardOf('roster-body', 'gm-card-muted');
      body.innerHTML = '<p class="gm-empty">本机还没有团体名单。可以创建一个新团体（你是群主），' +
        '或用群主给的邀请串加入已有团体。</p>';
      fillActions(actions, [
        button('创建团体', function () {
          // 走弹窗让用户**看到并确认**团体名，而不是用 window.confirm：
          // 内嵌 WebView 里原生对话框可能被宿主禁用，点了会毫无反馈。
          var nameInput = el('create-group-name');
          nameInput.value = (status.settings && status.settings.group_name) || 'omnibox-group';
          openModal('create-box');
          nameInput.focus();
          nameInput.select();
        }),
        button('加入 / 更新团体', function () { openModal('join-box'); }, true)
      ]);
      return;
    }

    var rows = roster.members.map(function (m, index) {
      return '<tr style="--obx-i:' + index + '"><td>' + escapeHtml(m.name) +
        '</td><td><code>' + escapeHtml(m.principal_id) +
        '</code></td><td>' + m.device_count + '</td></tr>';
    }).join('');

    var stale = roster.stale || {};
    var staleNote = '';
    if (stale.expired) {
      staleNote = '<p class="gm-hint"><span class="gm-badge gm-badge-warn">名单已过期 ' +
        escapeHtml(stale.expired_days) + ' 天</span> 仅提示，不阻断通信</p>';
    }

    // 名单过期只是提示（设计 §5.4：软件不做版本强制），因此顶边用 warn 而不是 error
    cardOf('roster-body', stale.expired ? 'gm-card-warn' : 'gm-card-ok');
    body.innerHTML =
      '<dl class="gm-kv">' +
      '<dt>团体</dt><dd>' + escapeHtml(roster.group) + '</dd>' +
      '<dt>名单版本</dt><dd>v' + escapeHtml(roster.version) + '</dd>' +
      '<dt>你的角色</dt><dd>' + roleBadge(roster.role) + '</dd>' +
      '<dt>成员</dt><dd>' + escapeHtml(roster.member_count) + ' 人（管理员 ' +
      escapeHtml(roster.admin_count) + '）</dd>' +
      '</dl>' + staleNote +
      '<table class="gm-table"><thead><tr><th>成员</th><th>主体 ID</th><th>设备</th></tr></thead>' +
      '<tbody>' + rows + '</tbody></table>';

    var buttons = [
      button('显示邀请串', function () {
        call('get_invite').then(function (res) {
          if (!res || !res.success) { toast((res && res.error) || '读取失败', true); return; }
          showInvite('邀请串（名单 v' + res.version + '）', res.invite);
        }).catch(showError);
      }),
      button('刷新', refresh, true)
    ];
    if (roster.role === 'owner' || roster.role === 'admin') {
      buttons.push(button('添加成员', function () { openModal('member-box'); }, true));
    }
    fillActions(actions, buttons);
  }

  function renderShares(status) {
    var body = el('shares-body');
    var count = el('gm-count-shares');
    if (count) { count.textContent = status.shares.length ? String(status.shares.length) : ''; }

    if (!status.shares.length) {
      cardOf('shares-body', 'gm-card-muted');
      body.innerHTML = '<p class="gm-empty"><strong>本机还没有共享项。</strong>' +
        '共享根建议放在专门目录，不要指向程序目录、配置目录或身份目录。</p>';
      return;
    }
    cardOf('shares-body', 'gm-card-ok');
    var rows = status.shares.map(function (share, index) {
      var acl = share.acl || {};
      var limit = share.max_bytes === null ? '不限制' : Math.round(share.max_bytes / 1048576) + ' MiB';
      // 共享根是**有状态的位置**：目录所在磁盘未接入时必须显示出来，
      // 否则对端看到的是空目录，而本机界面显示一切正常。
      var stateBadge = share.available === false
        ? '<span class="gm-badge gm-badge-warn">' + escapeHtml(share.reason || '不可用') + '</span>'
        : '<span class="gm-badge gm-badge-ok">可用</span>';
      return '<tr style="--obx-i:' + index + '">' +
        '<td><code>' + escapeHtml(share.share_id) + '</code></td>' +
        '<td><code>' + escapeHtml(share.path) + '</code></td>' +
        '<td>' + stateBadge + '</td>' +
        '<td>读 ' + escapeHtml(acl.read) + ' / 写 ' + escapeHtml(acl.write) + '</td>' +
        '<td>' + escapeHtml(limit) + '</td>' +
        '<td><button type="button" class="btn btn-sm btn-danger" data-remove="' +
        escapeHtml(share.share_id) + '">移除</button></td>' +
        '</tr>';
    }).join('');
    body.innerHTML = '<table class="gm-table"><thead><tr><th>标识</th><th>本机目录</th>' +
      '<th>状态</th><th>ACL</th><th>容量上限</th><th></th></tr></thead><tbody>' + rows + '</tbody></table>';

    Array.prototype.forEach.call(body.querySelectorAll('[data-remove]'), function (node) {
      node.addEventListener('click', function () {
        var shareId = node.getAttribute('data-remove');
        // 用壳的 confirmDialog（base.js）。原生 window.confirm 在内嵌 WebView 里
        // 可能被宿主禁用，点了毫无反馈 —— 本插件就踩过这个坑（见本文件"创建团体"
        // 的注释）。脱离壳打开时回退到原生 confirm。
        if (typeof window.confirmDialog !== 'function') {
          if (window.confirm('移除共享项「' + shareId + '」？只会取消共享，不会删除目录里的文件。')) {
            call('remove_share', { share_id: shareId }).then(afterAction('已移除 ' + shareId)).catch(showError);
          }
          return;
        }
        window.confirmDialog('移除共享项「' + shareId + '」？只会取消共享，不会删除目录里的文件。',
          { danger: true, okText: '移除' }).then(function (ok) {
          if (!ok) { return; }
          call('remove_share', { share_id: shareId }).then(afterAction('已移除 ' + shareId)).catch(showError);
        });
      });
    });
  }

  function renderNode(status) {
    var body = el('node-body');
    var actions = el('node-actions');
    var node = status.node;

    // 自动启动节点（有身份与团体时后端会自己起）：失败原因必须显示出来，
    // 否则用户看到的是"节点未运行"而不知道为什么。
    var autoNote = '';
    if (node.auto_start_error) {
      autoNote = '<p class="gm-hint"><span class="gm-badge gm-badge-warn">自动启动失败：' +
        escapeHtml(node.auto_start_error) + '</span></p>';
    }
    // 发布状态：节点在跑但没发布注册记录 = 本机对别人不可见（别人发现不了我）。
    var published = '';
    if (node.running) {
      published = node.published
        ? '<span class="gm-badge gm-badge-ok">已发布（别人可发现）</span>'
        : '<span class="gm-badge gm-badge-warn">未发布注册记录 —— 别人发现不了本机</span>';
    }

    if (node.running && node.published && !node.error) {
      cardOf('node-body', 'gm-card-ok');
    } else if (node.error || node.auto_start_error) {
      cardOf('node-body', 'gm-card-error');
    } else {
      cardOf('node-body', 'gm-card-warn');
    }

    body.innerHTML = '<dl class="gm-kv">' +
      '<dt>状态</dt><dd>' + (node.running ? '运行中' : '未运行') + ' ' + published + '</dd>' +
      '<dt>监听</dt><dd>' + escapeHtml(node.listening || '-') + '</dd>' +
      '<dt>监听设置</dt><dd>' + escapeHtml(status.settings.bind) + ':' +
      escapeHtml(status.settings.port) + '</dd>' +
      '<dt>下载目录</dt><dd>' + escapeHtml((status.locations || {}).downloads || '-') +
      ((status.locations || {}).downloads_custom ? '' : '（默认）') + '</dd>' +
      '</dl>' + autoNote +
      (node.error ? '<p class="gm-hint"><span class="gm-badge gm-badge-warn">' +
        escapeHtml(node.error) + '</span></p>' : '');

    if (node.running) {
      fillActions(actions, [button('停止节点', function () {
        call('stop_node').then(afterAction('节点已停止')).catch(showError);
      }, true)]);
    } else {
      fillActions(actions, [
        button('启动节点', function () {
          call('start_node').then(function (res) {
            if (!res || !res.success) { toast((res && res.error) || '启动失败', true); }
            else { toast('节点已启动'); }
            refresh();
          }).catch(showError);
        })
      ]);
    }
  }

  function renderUnsupported(status) {
    var items = status.unsupported || [];
    el('unsupported-list').innerHTML = items.map(function (item, index) {
      return '<li style="--obx-i:' + index + '">' + escapeHtml(item) + '</li>';
    }).join('');
  }

  /** 侧栏底部摘要：节点在不在跑、本机在哪个团体、名单第几版。
      这几条是"我到底能不能被别人连上"的最小集合，因此常驻可见。 */
  function renderSideSummary(status) {
    var dot = el('gm-foot-dot');
    var text = el('gm-foot-text');
    if (!dot || !text) { return; }

    var node = status.node || {};
    var roster = status.roster;
    var identity = status.identity;
    var parts = [];

    var level = 'idle';
    if (node.running && node.published) {
      level = 'ok';
      parts.push('节点运行中');
    } else if (node.running) {
      level = 'warn';
      parts.push('已运行 · 未发布');
    } else if (node.auto_start_error || node.error) {
      level = 'error';
      parts.push('节点启动失败');
    } else {
      parts.push(identity ? '节点未运行' : '尚未创建身份');
    }

    if (roster) {
      parts.push(roster.group + ' · v' + roster.version);
      if ((roster.stale || {}).expired) { level = level === 'ok' ? 'warn' : level; }
    }
    dot.setAttribute('data-state', level);
    text.textContent = parts.join(' · ');
    text.title = parts.join(' · ');
  }

  /** 首屏：get_status 还没回来时给骨架，而不是几张空卡片。 */
  function renderLoading() {
    ['identity-body', 'roster-body', 'node-body', 'shares-body'].forEach(function (id) {
      var body = el(id);
      if (!body || body.childNodes.length) { return; }
      body.innerHTML = '<span class="gm-skeleton-line obx-skeleton"></span>' +
        '<span class="gm-skeleton-line obx-skeleton" style="width:80%"></span>';
    });
    var summary = el('gm-foot-text');
    if (summary) { summary.textContent = '正在读取状态…'; }
  }

  // ── 动作 ────────────────────────────────────────────────────────────────

  function showError(err) {
    toast((err && err.message) || String(err), true);
  }

  function afterAction(message) {
    return function (res) {
      if (res && res.success === false) { toast(res.error || '操作失败', true); return; }
      toast(message);
      refresh();
    };
  }

  function refresh() {
    return call('get_status').then(function (status) {
      state.status = status;
      render();
      // 交错入场：每次状态刷新后重新编号，列表才有"更新了"的观感
      stagger(el('gm-panels'), '.gm-card');
      // 设备列表是独立的一次后端往返（要连对端），不阻塞首屏渲染
      if (window.GroupMeshRemote && typeof window.GroupMeshRemote.refreshPeers === 'function') {
        window.GroupMeshRemote.refreshPeers();
      }
      return status;
    }).catch(function (err) {
      var summary = el('gm-foot-text');
      if (summary) { summary.textContent = '读取状态失败'; }
      var dot = el('gm-foot-dot');
      if (dot) { dot.setAttribute('data-state', 'error'); }
      showError(err);
    });
  }

  function showInvite(title, text) {
    var box = el('invite-box');
    box.querySelector('h3').textContent = title;
    el('invite-text').value = text;
    openModal(box.id);
  }

  // ── 绑定 ────────────────────────────────────────────────────────────────

  function bind() {
    el('btn-refresh').addEventListener('click', refresh);

    el('btn-settings').addEventListener('click', function () {
      if (typeof window.openSettingsModal !== 'function') {
        toast('设置面板需要从 OmniBox 壳内打开', true);
        return;
      }
      window.openSettingsModal({ title: '团体组网设置' });
    });

    Array.prototype.forEach.call(document.querySelectorAll('#gm-nav .gm-nav-item'), function (item) {
      item.addEventListener('click', function () { setPanel(item.getAttribute('data-panel')); });
    });

    el('btn-add-share').addEventListener('click', function () {
      var shareId = el('share-id').value.trim();
      var path = el('share-path').value.trim();
      if (!shareId || !path) { toast('请填写共享标识与本机目录', true); return; }
      call('add_share', {
        share_id: shareId, path: path, read: 'group',
        write: el('share-write').value
      })
        .then(function (res) {
          if (!res || !res.success) { toast((res && res.error) || '挂载失败', true); return; }
          toast('共享项 ' + shareId + ' 已挂载');
          el('share-id').value = '';
          el('share-path').value = '';
          refresh();
        }).catch(showError);
    });

    el('btn-copy-invite').addEventListener('click', function () {
      var text = el('invite-text').value;
      if (navigator.clipboard && navigator.clipboard.writeText) {
        navigator.clipboard.writeText(text).then(function () { toast('已复制'); },
          function () { toast('复制失败，请手动选择文本', true); });
      } else {
        el('invite-text').select();
        toast('已选中文本，请按 Ctrl+C');
      }
    });
    el('btn-close-invite').addEventListener('click', function () { closeModal('invite-box'); });

    el('btn-close-join').addEventListener('click', function () { closeModal('join-box'); });
    el('btn-do-create').addEventListener('click', function () {
      var group = el('create-group-name').value.trim();
      if (!group) { toast('请填写团体名称', true); return; }
      call('create_group', { group: group }).then(function (res) {
        if (!res || !res.success) { toast((res && res.error) || '创建失败', true); return; }
        closeModal('create-box');
        refresh();
        // 创建成功的下一步是把邀请串发出去，因此直接把邀请串弹出来
        showInvite('团体已创建 —— 邀请串', res.invite || '');
      }).catch(showError);
    });
    el('btn-close-create').addEventListener('click', function () { closeModal('create-box'); });
    el('btn-do-join').addEventListener('click', function () {
      var invite = el('join-text').value.trim();
      if (!invite) { toast('请粘贴邀请串', true); return; }
      call('join_group', { invite: invite }).then(function (res) {
        if (!res || !res.success) { toast((res && res.error) || '加入失败', true); return; }
        closeModal('join-box');
        el('join-text').value = '';
        if (res.updated) {
          toast('名单已从 v' + res.previous_version + ' 更新到 v' + res.version);
        } else {
          toast('已加入团体 ' + res.group + '（名单 v' + res.version + '）');
        }
        if (res.warning) { toast(res.warning, true); }
        refresh();
      }).catch(showError);
    });

    el('btn-close-member').addEventListener('click', function () { closeModal('member-box'); });
    el('btn-do-add-member').addEventListener('click', function () {
      var principal = el('member-principal').value.trim();
      var device = el('member-device').value.trim();
      var name = el('member-name').value.trim();
      if (!principal) { toast('请填写主体公钥', true); return; }
      call('add_member', { principal: principal, device: device, name: name }).then(function (res) {
        if (!res || !res.success) { toast((res && res.error) || '添加失败', true); return; }
        closeModal('member-box');
        el('member-principal').value = '';
        el('member-device').value = '';
        el('member-name').value = '';
        toast(res.action + '，名单已更新到 v' + res.version);
        refresh();
      }).catch(showError);
    });
  }

  function init() {
    bind();
    setPanel(state.panel);
    renderLoading();
    // 远端分片（js/remote.js）：把本文件的能力注入进去，它自己不碰后端与提示。
    // 注入而非全局互引：装载期两者都不触碰 DOM（资源契约要求），运行时也只依赖
    // 这几个回调，因此分片可以独立测试。
    if (window.GroupMeshRemote && typeof window.GroupMeshRemote.init === 'function') {
      window.GroupMeshRemote.init({
        call: call,
        toast: toast,
        escapeHtml: escapeHtml,
        openModal: openModal,
        closeModal: closeModal,
        // 设备数由远端分片拿到后回报，侧栏计数不必自己去连对端
        onPeersLoaded: function (peers) {
          var count = el('gm-count-peers');
          if (count) { count.textContent = peers && peers.length ? String(peers.length) : ''; }
        }
      });
    }
    refresh();
  }

  // 装载期绝不触碰 DOM：tests/js/plugin_asset_contract.mjs 用空 DOM 替身按声明顺序
  // 装载全部脚本，装载期取元素（getElementById(...).addEventListener）会直接抛错。
  // 本脚本由 index.html 末尾的 <script src> 同步装载，DOMContentLoaded 必在其后触发。
  document.addEventListener('DOMContentLoaded', init);
})();
