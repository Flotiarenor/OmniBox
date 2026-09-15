/**
 * group-mesh 插件前端。
 *
 * 只做三件事：读状态、渲染、把用户动作转成 Bridge.call。所有判定都在后端 ——
 * 前端不持有任何"谁能做什么"的规则（设计文档 §6.3：客户端隐藏按钮不构成授权）。
 */
(function () {
  'use strict';

  var state = { status: null };

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

  // 弹窗用 data-open 而不是 hidden 控制可见性，原因见 group-mesh.css 里 .gm-modal
  // 的注释：`.gm-modal { display: flex }` 会盖掉浏览器给 [hidden] 的 display:none
  // （UA 样式同优先级输给作者样式），曾经导致三个弹窗在打开页面时同时显示。
  function openModal(id) {
    var node = el(id);
    if (node) { node.setAttribute('data-open', 'true'); }
  }

  function closeModal(id) {
    var node = el(id);
    if (node) { node.removeAttribute('data-open'); }
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

  function button(label, handler, ghost) {
    var node = document.createElement('button');
    node.type = 'button';
    node.className = 'gm-btn' + (ghost ? ' gm-btn-ghost' : '');
    node.textContent = label;
    node.addEventListener('click', handler);
    return node;
  }

  function fillActions(container, buttons) {
    container.innerHTML = '';
    buttons.forEach(function (b) { container.appendChild(b); });
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
  }

  function renderIdentity(status) {
    var body = el('identity-body');
    var actions = el('identity-actions');
    var identity = status.identity;

    if (!identity) {
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

    body.innerHTML = '<dl class="gm-kv">' +
      '<dt>主体</dt><dd>' + escapeHtml(identity.principal_name) + '</dd>' +
      '<dt>主体 ID</dt><dd>' + escapeHtml(identity.principal_id) + '</dd>' +
      '<dt>设备</dt><dd>' + escapeHtml(identity.device_name) + '（' + escapeHtml(identity.device_id) + '）</dd>' +
      '<dt>主体公钥</dt><dd>' + escapeHtml(shortKey(identity.principal_key)) + '</dd>' +
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

    var rows = roster.members.map(function (m) {
      return '<tr><td>' + escapeHtml(m.name) + '</td><td><code>' + escapeHtml(m.principal_id) +
        '</code></td><td>' + m.device_count + '</td></tr>';
    }).join('');

    var staleNote = '';
    if (roster.stale && roster.stale.expired) {
      staleNote = '<p class="gm-badge gm-badge-warn">名单已过期 ' +
        escapeHtml(roster.stale.expired_days) + ' 天（仅提示，不阻断通信）</p>';
    }

    body.innerHTML =
      '<dl class="gm-kv">' +
      '<dt>团体</dt><dd>' + escapeHtml(roster.group) + '</dd>' +
      '<dt>名单版本</dt><dd>' + escapeHtml(roster.version) + '</dd>' +
      '<dt>你的角色</dt><dd>' + roleBadge(roster.role) + '</dd>' +
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
    if (!status.shares.length) {
      body.innerHTML = '<p class="gm-empty">本机还没有共享项。共享根建议放在专门目录，' +
        '不要指向程序目录、配置目录或身份目录。</p>';
      return;
    }
    var rows = status.shares.map(function (share) {
      var acl = share.acl || {};
      var limit = share.max_bytes === null ? '不限制' : Math.round(share.max_bytes / 1048576) + ' MiB';
      return '<tr>' +
        '<td><code>' + escapeHtml(share.share_id) + '</code></td>' +
        '<td><code>' + escapeHtml(share.path) + '</code></td>' +
        '<td>读 ' + escapeHtml(acl.read) + ' / 写 ' + escapeHtml(acl.write) +
        ' / 删 ' + escapeHtml(acl.delete) + '</td>' +
        '<td>' + escapeHtml(limit) + '</td>' +
        '<td><button type="button" class="gm-btn gm-btn-ghost" data-remove="' +
        escapeHtml(share.share_id) + '">移除</button></td>' +
        '</tr>';
    }).join('');
    body.innerHTML = '<table class="gm-table"><thead><tr><th>标识</th><th>本机目录</th>' +
      '<th>ACL</th><th>容量上限</th><th></th></tr></thead><tbody>' + rows + '</tbody></table>';

    Array.prototype.forEach.call(body.querySelectorAll('[data-remove]'), function (node) {
      node.addEventListener('click', function () {
        var shareId = node.getAttribute('data-remove');
        if (!window.confirm('移除共享项「' + shareId + '」？只会取消共享，不会删除目录里的文件。')) { return; }
        call('remove_share', { share_id: shareId }).then(afterAction('已移除 ' + shareId)).catch(showError);
      });
    });
  }

  function renderNode(status) {
    var body = el('node-body');
    var actions = el('node-actions');
    var node = status.node;

    body.innerHTML = '<dl class="gm-kv">' +
      '<dt>状态</dt><dd>' + (node.running ? '运行中' : '未运行') + '</dd>' +
      '<dt>监听</dt><dd>' + escapeHtml(node.listening || '-') + '</dd>' +
      '<dt>监听设置</dt><dd>' + escapeHtml(status.settings.bind) + ':' +
      escapeHtml(status.settings.port) + '</dd>' +
      '</dl>' +
      (node.error ? '<p class="gm-badge gm-badge-warn">' + escapeHtml(node.error) + '</p>' : '');

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
    el('unsupported-list').innerHTML = (status.unsupported || []).map(function (item) {
      return '<li>' + escapeHtml(item) + '</li>';
    }).join('');
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
      return status;
    }).catch(function (err) {
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

    el('btn-add-share').addEventListener('click', function () {
      var shareId = el('share-id').value.trim();
      var path = el('share-path').value.trim();
      if (!shareId || !path) { toast('请填写共享标识与本机目录', true); return; }
      call('add_share', {
        share_id: shareId, path: path, read: 'group',
        write: el('share-write').value, delete: 'owner',
        max_bytes: 1024 * 1024 * 1024
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
    refresh();
  }

  if (document.readyState === 'loading') {
    document.addEventListener('DOMContentLoaded', init);
  } else {
    init();
  }
})();
