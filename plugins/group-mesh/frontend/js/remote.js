/**
 * group-mesh 插件的「远端共享」分片：设备 → 共享项 → 目录 → 取回。
 *
 * 为什么单独一个文件而不是并进 app.js：这一步只加了一页，而下一步还要做侧边栏
 * 与设计系统对齐的整体改版。两件事放在同一个文件里改，冲突与回归都难定位。
 * 分片契约由 tests/js/plugin_asset_contract.mjs 把关：js/ 下不得有未被
 * index.html 引用的脚本，且装载期不得触碰 DOM（本文件只在定义函数）。
 *
 * 与 app.js 的分工：本文件不持有界面状态，只负责"远端"这一页的取数与渲染；
 * 调后端与弹提示都由 app.js 传进来的回调完成（见 GroupMeshRemote.init）。
 */
window.GroupMeshRemote = (function () {
  'use strict';

  var state = {
    peers: [],          // list_peers 的结果
    peersLoaded: false,
    peerErrors: [],
    cache: {},          // "设备ID/共享标识" -> remote_cache 里那一项
    cacheBytes: 0,
    current: null,       // { device_id, name, shareId, path }
    entries: [],
    uploadTask: null,    // 正在轮询的上传任务 id
    uploadTimer: null,   // 轮询定时器
    // init() 注入的依赖
    call: null,
    toast: null,
    escapeHtml: null,
    openModal: null,
    closeModal: null,
  };

  function el(id) { return document.getElementById(id); }

  function esc(text) { return state.escapeHtml(text); }

  function toast(message, isError) { state.toast(message, isError); }

  // ── 渲染：设备与共享项 ──────────────────────────────────────────────────

  function renderPeers() {
    var body = el('peers-body');
    if (!body) { return; }

    if (!state.peersLoaded && !state.peers.length) {
      body.innerHTML = '<p class="gm-empty">正在读取设备…</p>';
      return;
    }

    if (!state.peers.length) {
      var hint = '<p class="gm-empty">还没有可用的对端设备。<br>' +
        '· 团体成员启动节点后，本机会从它的注册记录里自动发现；<br>' +
        '· 第一次发现某台设备时，用上面的「添加对端」填它的地址。</p>';
      if (state.peerErrors.length) {
        hint += state.peerErrors.map(function (item) {
          return '<p class="gm-badge gm-badge-warn">' +
            esc(item.device) + '：' + esc(item.error) + '</p>';
        }).join('');
      }
      body.innerHTML = hint;
      return;
    }

    body.innerHTML = state.peers.map(function (peer) {
      var shares = peer.shares || [];
      var current = state.current;
      var items = shares.length
        ? shares.map(function (shareId) {
          var active = current && current.device_id === peer.device_id &&
            current.shareId === shareId;
          var cached = state.cache[peer.device_id + '/' + shareId];
          var badge = cached
            ? '<span class="gm-remote-badge" title="' + esc(cached.root || '') + '">已物化 ' +
              esc(String(cached.entries)) + ' 项</span>'
            : '';
          return '<div class="gm-remote-row' + (active ? ' gm-remote-item-active' : '') + '">' +
            '<button type="button" class="gm-remote-item" data-device="' + esc(peer.device_id) +
            '" data-share="' + esc(shareId) + '">' +
            '<span>📁</span><span>' + esc(shareId) + '</span></button>' + badge +
            '<button type="button" class="btn btn-sm" data-materialize="' + esc(shareId) +
            '" data-device="' + esc(peer.device_id) + '" title="把目录结构缓存到本地，' +
            '之后读文件时按需取回">缓存</button>' +
            '</div>';
        }).join('')
        : '<div class="gm-remote-note">' +
          (peer.note ? esc(peer.note) : '对方没有你可读的共享项') + '</div>';

      var endpoint = peer.endpoint
        ? esc(peer.endpoint[0] + ':' + peer.endpoint[1])
        : '（没有可用端点）';
      return '<div class="gm-remote-peer">' +
        '<div class="gm-remote-peer-head">' +
        '<span class="gm-remote-peer-name">' + esc(peer.name || peer.device_id.slice(0, 12)) +
        '</span>' +
        '<span class="gm-remote-peer-ep" title="' + esc(endpoint) + '">' + endpoint + '</span>' +
        '</div>' + items + '</div>';
    }).join('');

    Array.prototype.forEach.call(body.querySelectorAll('.gm-remote-item'), function (node) {
      node.addEventListener('click', function () {
        listDirectory(node.getAttribute('data-device'), node.getAttribute('data-share'), '.');
      });
    });
    Array.prototype.forEach.call(body.querySelectorAll('[data-materialize]'), function (node) {
      node.addEventListener('click', function () {
        materialize(node.getAttribute('data-device'), node.getAttribute('data-materialize'));
      });
    });

    if (state.peerErrors.length) {
      body.innerHTML += state.peerErrors.map(function (item) {
        return '<p class="gm-badge gm-badge-warn">' + esc(item.device) + '：' +
          esc(item.error) + '</p>';
      }).join('');
    }
  }

  // ── 渲染：目录 ──────────────────────────────────────────────────────────

  function renderPath() {
    var head = el('remote-path');
    if (!head) { return; }
    if (!state.current) {
      head.textContent = '未选择共享项';
      return;
    }
    var label = state.current.path && state.current.path !== '.' ? state.current.path : '/';
    head.innerHTML = '<span class="gm-remote-crumb">' +
      esc((state.current.name || '') + ' · ' + state.current.shareId + ' · ' + label) +
      '</span><button type="button" id="btn-remote-refresh" class="btn btn-sm">刷新</button>';
    var refresh = el('btn-remote-refresh');
    if (refresh) {
      refresh.addEventListener('click', function () {
        listDirectory(state.current.device_id, state.current.shareId, state.current.path);
      });
    }
  }

  function renderEntries() {
    var body = el('remote-body');
    if (!body) { return; }

    if (!state.current) {
      body.innerHTML = '<p class="gm-empty">左边选一个共享项，列它的目录。</p>';
      return;
    }
    if (!state.entries.length) {
      body.innerHTML = '<p class="gm-empty">这个目录是空的。</p>';
      return;
    }

    var rows = state.entries.map(function (entry) {
      var action = entry.dir
        ? '<button type="button" class="btn btn-sm" data-enter="' +
          esc(entry.name) + '">进入</button>'
        : '<button type="button" class="btn btn-sm" data-download="' +
          esc(entry.name) + '">取回</button>';
      return '<tr>' +
        '<td>' + (entry.dir ? '📁 ' : '📄 ') + esc(entry.name) + '</td>' +
        '<td>' + (entry.dir ? '目录' : formatSize(entry.size)) + '</td>' +
        '<td>' + action + '</td>' +
        '</tr>';
    }).join('');

    var up = '';
    if (state.current.path && state.current.path !== '.') {
      up = '<button type="button" id="btn-remote-up" class="btn btn-sm">↑ 上级</button>';
    }
    body.innerHTML = '<div class="gm-remote-tools">' +
      '<button type="button" id="btn-remote-upload" class="btn btn-sm btn-primary">上传文件到此处</button>' +
      up + '</div>' +
      '<table class="gm-table"><thead><tr><th>名称</th><th>大小</th><th></th></tr></thead>' +
      '<tbody>' + rows + '</tbody></table>';

    var uploadBtn = el('btn-remote-upload');
    if (uploadBtn) {
      uploadBtn.addEventListener('click', openUpload);
    }
    var upBtn = el('btn-remote-up');
    if (upBtn) {
      upBtn.addEventListener('click', function () {
        var parent = state.current.path.replace(/\\/g, '/').replace(/\/[^/]+\/?$/, '');
        listDirectory(state.current.device_id, state.current.shareId, parent || '.');
      });
    }
    Array.prototype.forEach.call(body.querySelectorAll('[data-enter]'), function (node) {
      node.addEventListener('click', function () {
        var name = node.getAttribute('data-enter');
        var base = state.current.path === '.' ? '' : state.current.path + '/';
        listDirectory(state.current.device_id, state.current.shareId, base + name);
      });
    });
    Array.prototype.forEach.call(body.querySelectorAll('[data-download]'), function (node) {
      node.addEventListener('click', function () {
        download(node.getAttribute('data-download'));
      });
    });
  }

  function formatSize(bytes) {
    var value = Number(bytes || 0);
    if (value < 1024) { return value + ' B'; }
    if (value < 1048576) { return (value / 1024).toFixed(1) + ' KB'; }
    if (value < 1073741824) { return (value / 1048576).toFixed(1) + ' MB'; }
    return (value / 1073741824).toFixed(2) + ' GB';
  }

  function renderProgress(text, isError) {
    var box = el('remote-progress');
    if (!box) { return; }
    if (!text) {
      box.hidden = true;
      box.textContent = '';
      return;
    }
    box.hidden = false;
    box.className = 'gm-remote-progress' + (isError ? ' gm-remote-progress-error' : '');
    box.textContent = text;
  }

  // ── 取数与动作 ──────────────────────────────────────────────────────────

  function showError(err) {
    toast((err && err.message) || String(err), true);
  }

  function refreshMyEndpoint() {
    var box = el('my-endpoint');
    if (!box) { return Promise.resolve(); }
    return state.call('my_endpoint').then(function (result) {
      if (!result || !result.success) {
        box.textContent = (result && result.error) || '读取失败';
        return result;
      }
      state.myEndpointText = result.text || '';
      state.myEndpoints = result.endpoints || [];
      if (!result.running) {
        box.textContent = '节点未运行 —— 先启动节点，对方才连得上你';
        return result;
      }
      if (!state.myEndpoints.length) {
        box.textContent = '尚未发布地址（启动节点后会自动发布）';
        return result;
      }
      box.textContent = state.myEndpoints.map(function (pair) {
        return pair[0].indexOf(':') >= 0 ? '[' + pair[0] + ']:' + pair[1]
          : pair[0] + ':' + pair[1];
      }).join('   ');
      return result;
    }).catch(function () { box.textContent = '读取失败'; });
  }

  function copyMyEndpoint() {
    var text = state.myEndpointText || '';
    if (!text) {
      toast('还没有可复制的地址：请先启动节点', true);
      return;
    }
    if (navigator.clipboard && navigator.clipboard.writeText) {
      navigator.clipboard.writeText(text).then(function () {
        toast('已复制，发给对方即可');
      }, function () { toast('复制失败，请手动选择文本', true); });
    } else {
      toast('当前环境不支持自动复制，请对照界面手动抄写', true);
    }
  }

  function updatePeerEndpoint() {
    var endpointInput = el('peer-endpoint');
    var deviceInput = el('peer-device');
    var endpoint = (endpointInput && endpointInput.value || '').trim();
    var deviceId = (deviceInput && deviceInput.value || '').trim();
    if (!endpoint) {
      toast('请粘贴对方的地址', true);
      return;
    }
    // 后端的 update 会按 device_id（填了的话）或地址替换旧条目，因此对方换端口后
    // 不会留下一条永远连不上的死地址。
    state.call('peers', { action: 'update', endpoint: endpoint, device_id: deviceId })
      .then(function (result) {
        if (!result || !result.success) {
          toast((result && result.error) || '更新失败', true);
          return;
        }
        if (endpointInput) { endpointInput.value = ''; }
        if (deviceInput) { deviceInput.value = ''; }
        toast('已更新该设备的地址，正在刷新…');
        return refreshPeers();
      }).catch(showError);
  }

  function refreshPeers() {
    var body = el('peers-body');
    if (body) { body.innerHTML = '<p class="gm-empty">正在读取设备…</p>'; }
    return state.call('list_peers', { refresh: true }).then(function (result) {
      state.peersLoaded = true;
      if (!result || !result.success) {
        state.peers = [];
        state.peerErrors = [];
        renderPeers();
        toast((result && result.error) || '读取设备失败', true);
        return result;
      }
      state.peers = result.peers || [];
      state.peerErrors = result.errors || [];
      return refreshCache();
    }).then(function () {
      renderPeers();
      if (state.current && !state.peers.some(function (peer) {
        return peer.device_id === state.current.device_id &&
          (peer.shares || []).indexOf(state.current.shareId) >= 0;
      })) {
        state.current = null;
        state.entries = [];
        renderPath();
        renderEntries();
      }
      return state.peers;
    }).catch(showError);
  }

  function refreshCache() {
    return state.call('remote_cache').then(function (result) {
      if (!result || !result.success) { return result; }
      var map = {};
      (result.items || []).forEach(function (item) {
        map[item.device_id + '/' + item.share_id] = item;
      });
      state.cache = map;
      state.cacheBytes = result.total_bytes || 0;
      renderCacheSummary();
      return result;
    }).catch(function () { /* 缓存信息是附加信息，读不到不影响主流程 */ });
  }

  function renderCacheSummary() {
    var box = el('remote-cache');
    if (!box) { return; }
    var count = Object.keys(state.cache).length;
    if (!count) {
      box.hidden = true;
      box.textContent = '';
      return;
    }
    box.hidden = false;
    box.innerHTML = '本地已物化 ' + esc(String(count)) + ' 个共享项，占用 ' +
      esc(formatSize(state.cacheBytes)) +
      ' <button type="button" id="btn-clear-cache" class="btn btn-sm">清理缓存</button>';
    var clear = el('btn-clear-cache');
    if (clear) {
      clear.addEventListener('click', function () {
        state.call('clear_remote_cache', {}).then(function (result) {
          if (!result || !result.success) {
            toast((result && result.error) || '清理失败', true);
            return;
          }
          toast('已清理本地缓存（释放 ' + formatSize(result.freed_bytes) + '）');
          refreshCache().then(renderPeers);
        }).catch(showError);
      });
    }
  }

  function materialize(deviceId, shareId) {
    renderProgress('正在物化 ' + shareId + ' 的目录结构…');
    return state.call('materialize_remote', { device_id: deviceId, share_id: shareId })
      .then(function (result) {
        if (!result || !result.success) {
          renderProgress((result && result.error) || '物化失败', true);
          toast((result && result.error) || '物化失败', true);
          return result;
        }
        var hint = result.truncated ? '（达到条目上限，只物化了一部分）' : '';
        renderProgress('已物化到 ' + result.root + '：' + result.dirs + ' 个目录、' +
          result.files + ' 个文件占位' + hint +
          '。把它作为目录加进 image-viewer / media-player 等插件即可浏览，' +
          '字节会在第一次读取时按需取回。');
        toast('已物化 ' + shareId + hint);
        return refreshCache();
      }).then(function () {
        renderPeers();
      }).catch(function (err) { renderProgress('物化失败', true); showError(err); });
  }

  function listDirectory(deviceId, shareId, path) {
    renderProgress('正在读取对端目录…');
    return state.call('list_remote', { device_id: deviceId, share_id: shareId,
      path: path || '.' })
      .then(function (result) {
        renderProgress('');
        if (!result || !result.success) {
          toast((result && result.error) || '读取目录失败', true);
          return result;
        }
        state.current = { device_id: result.peer_device_id || result.device_id,
          name: result.name, shareId: shareId, path: result.path || path || '.' };
        state.entries = result.entries || [];
        renderPeers();
        renderPath();
        renderEntries();
        return result;
      }).catch(function (err) { renderProgress(''); showError(err); });
  }

  function download(name) {
    var base = state.current.path === '.' ? '' : state.current.path + '/';
    var remotePath = base + name;
    renderProgress('正在取回 ' + name + '…');
    return state.call('download_remote', { device_id: state.current.device_id,
      share_id: state.current.shareId, path: remotePath })
      .then(function (result) {
        if (!result || !result.success) {
          renderProgress((result && result.error) || '取回失败', true);
          toast((result && result.error) || '取回失败', true);
          return result;
        }
        if (result.skipped) {
          renderProgress('本机已有 ' + name + '：' + result.local_path);
          toast('本机已有该文件，未重复下载');
          return result;
        }
        renderProgress('已取回 ' + name + '（' + formatSize(result.size) + '）到 ' +
          result.local_path);
        toast('已取回 ' + name);
        return result;
      }).catch(function (err) { renderProgress('取回失败', true); showError(err); });
  }

  // ── 上传 ────────────────────────────────────────────────────────────────
  //
  // 与"取回"的方向相反：本机出文件、对端落盘。界面这一侧只负责取三样东西
  // （本机路径、目标文件名、是否覆盖），越界与权限都由对端判定 —— 客户端隐藏
  // 按钮不构成授权（设计文档 §6.3）。

  function openUpload() {
    if (!state.current) {
      toast('先在左边选一个共享项', true);
      return;
    }
    var label = state.current.path && state.current.path !== '.' ? state.current.path : '/';
    var target = el('upload-target');
    if (target) {
      target.textContent = '目标：' + (state.current.name || '') + ' · ' +
        state.current.shareId + ' · ' + label;
    }
    var local = el('upload-local');
    if (local) { local.value = ''; }
    var name = el('upload-name');
    if (name) { name.value = ''; }
    var overwrite = el('upload-overwrite');
    if (overwrite) { overwrite.checked = false; }
    setUploadRunning(false);
    renderUploadProgress('');
    state.openModal('upload-box');
    if (local) { local.focus(); }
  }

  /** 运行中把"上传"按钮换成"取消上传"，并锁住关闭按钮 —— 任务在后台跑，
      关掉弹窗只会让用户以为它停了。 */
  function setUploadRunning(running) {
    var doBtn = el('btn-do-upload');
    var cancelBtn = el('btn-cancel-upload');
    var closeBtn = el('btn-close-upload');
    // 用 style.display 而不是 hidden 属性：壳的 .btn 自带 display，作者样式会盖掉
    // 浏览器给 [hidden] 的 display:none（本插件踩过这个级联陷阱，见 CSS 顶部注释）。
    if (doBtn) {
      doBtn.disabled = running;
      doBtn.textContent = running ? '上传中…' : '上传';
    }
    if (cancelBtn) {
      cancelBtn.style.display = running ? '' : 'none';
      cancelBtn.disabled = false;
      cancelBtn.textContent = '取消上传';
    }
    if (closeBtn) { closeBtn.disabled = running; }
  }

  function renderUploadProgress(text) {
    var box = el('upload-progress');
    if (!box) { return; }
    box.textContent = text || '';
    box.style.display = text ? '' : 'none';
    renderProgress(text);
  }

  function uploadText(task) {
    var sent = Number(task.sent || 0);
    var total = Number(task.total || 0);
    var prefix = task.resumed_from > 0 ? '续传中' : '正在上传';
    return prefix + ' ' + formatSize(sent) + ' / ' + formatSize(total) +
      '（' + Number(task.percent || 0) + '%）';
  }

  function stopUploadPolling() {
    if (state.uploadTimer) {
      clearTimeout(state.uploadTimer);
      state.uploadTimer = null;
    }
    state.uploadTask = null;
  }

  function pollUpload(taskId) {
    if (state.uploadTask !== taskId) { return; }   // 已被取消或换了任务
    state.call('upload_status', { task_id: taskId }).then(function (result) {
      if (state.uploadTask !== taskId) { return; }
      var task = (result && result.task) || null;
      if (!task) {
        stopUploadPolling();
        setUploadRunning(false);
        renderUploadProgress('读取上传进度失败：' + ((result && result.error) || '未知原因'));
        return;
      }
      if (task.state === 'done') {
        stopUploadPolling();
        setUploadRunning(false);
        renderUploadProgress('已上传 ' + formatSize(task.total) + ' → ' + task.remote_path);
        toast('已上传 ' + task.remote_path);
        state.closeModal('upload-box');
        listDirectory(state.current.device_id, state.current.shareId, state.current.path);
        return;
      }
      if (task.state === 'failed') {
        stopUploadPolling();
        setUploadRunning(false);
        renderUploadProgress('上传失败：' + (task.error || '未知原因'));
        toast(task.error || '上传失败', true);
        return;
      }
      if (task.state === 'cancelled') {
        stopUploadPolling();
        setUploadRunning(false);
        // 已传的字节留在对端 .part 里：再点一次「上传」就是续传，所以这里明确说出来
        renderUploadProgress('已取消，已传 ' + formatSize(task.sent) +
          '（再点「上传」会从这里续传）');
        return;
      }
      renderUploadProgress(uploadText(task) +
        (task.state === 'cancelling' ? '，正在取消…' : ''));
      state.uploadTimer = setTimeout(function () { pollUpload(taskId); }, 300);
    }).catch(function (err) {
      if (state.uploadTask !== taskId) { return; }
      stopUploadPolling();
      setUploadRunning(false);
      renderUploadProgress('读取上传进度失败');
      showError(err);
    });
  }

  function doUpload() {
    if (!state.current) { return Promise.resolve(); }
    var local = (el('upload-local').value || '').trim();
    if (!local) { toast('请填写要上传的本机文件路径', true); return Promise.resolve(); }
    var name = (el('upload-name').value || '').trim().replace(/\\/g, '/');
    if (!name) { name = local.replace(/\\/g, '/').split('/').pop(); }
    var base = state.current.path === '.' ? '' : state.current.path + '/';
    var remotePath = base + name;
    var overwrite = !!(el('upload-overwrite') && el('upload-overwrite').checked);

    setUploadRunning(true);
    renderUploadProgress('正在上传 ' + name + '…');
    return state.call('upload_remote', { device_id: state.current.device_id,
      share_id: state.current.shareId, local_path: local, remote_path: remotePath,
      overwrite: overwrite })
      .then(function (result) {
        if (!result || !result.success) {
          setUploadRunning(false);
          renderUploadProgress('上传失败：' + ((result && result.error) || '未知原因'), true);
          toast((result && result.error) || '上传失败', true);
          return result;
        }
        state.uploadTask = result.task_id;
        pollUpload(result.task_id);
        return result;
      }).catch(function (err) {
        setUploadRunning(false);
        renderUploadProgress('上传失败', true);
        showError(err);
      });
  }

  function cancelUpload() {
    var taskId = state.uploadTask;
    if (!taskId) { return Promise.resolve(); }
    var btn = el('btn-cancel-upload');
    if (btn) { btn.disabled = true; btn.textContent = '正在取消…'; }
    return state.call('cancel_upload', { task_id: taskId }).then(function (result) {
      if (!result || !result.success) {
        toast((result && result.error) || '取消失败', true);
        if (btn) { btn.disabled = false; btn.textContent = '取消上传'; }
        return result;
      }
      // 状态由轮询收敛到 cancelled（取消在分块边界生效，可能需要等一个分块）
      var task = result.task || {};
      if (task.state === 'cancelling') {
        renderUploadProgress('正在取消…已传 ' + formatSize(task.sent));
      }
      return result;
    }).catch(function (err) {
      if (btn) { btn.disabled = false; btn.textContent = '取消上传'; }
      showError(err);
    });
  }

  function bind() {
    var refresh = el('btn-peer-refresh');
    if (refresh) { refresh.addEventListener('click', refreshPeers); }
    var copy = el('btn-copy-my-endpoint');
    if (copy) { copy.addEventListener('click', copyMyEndpoint); }
    var update = el('btn-update-peer');
    if (update) { update.addEventListener('click', updatePeerEndpoint); }
    var add = el('btn-peer-add');
    if (add) {
      add.addEventListener('click', function () {
        state.openModal('peer-box');
        var input = el('peer-manual-endpoint');
        if (input) { input.focus(); }
      });
    }
    var close = el('btn-close-peer');
    if (close) {
      close.addEventListener('click', function () { state.closeModal('peer-box'); });
    }
    var uploadClose = el('btn-close-upload');
    if (uploadClose) {
      uploadClose.addEventListener('click', function () { state.closeModal('upload-box'); });
    }
    var uploadDo = el('btn-do-upload');
    if (uploadDo) { uploadDo.addEventListener('click', doUpload); }
    var uploadCancel = el('btn-cancel-upload');
    if (uploadCancel) { uploadCancel.addEventListener('click', cancelUpload); }
    var doAdd = el('btn-do-add-peer');
    if (doAdd) {
      doAdd.addEventListener('click', function () {
        var endpoint = (el('peer-manual-endpoint').value || '').trim();
        var name = (el('peer-name').value || '').trim();
        if (!endpoint) { toast('请填写对端地址', true); return; }
        state.call('peers', { action: 'add', endpoint: endpoint, name: name })
          .then(function (result) {
            if (!result || !result.success) {
              toast((result && result.error) || '添加失败', true);
              return;
            }
            state.closeModal('peer-box');
            el('peer-manual-endpoint').value = '';
            el('peer-name').value = '';
            toast(result.existed ? '该地址已在列表里' : '已登记对端');
            refreshPeers();
          }).catch(showError);
      });
    }
  }

  /**
   * 由 app.js 在 DOMContentLoaded 之后调用。
   *
   * 依赖注入而不是直接引用 app.js 内部的函数：装载期不得触碰 DOM（资源契约），
   * 且 app.js 的回调（call / toast / 弹窗开关）本身就是它需要对外提供的全部能力。
   */
  function init(deps) {
    state.call = deps.call;
    state.toast = deps.toast;
    state.escapeHtml = deps.escapeHtml;
    state.openModal = deps.openModal;
    state.closeModal = deps.closeModal;
    bind();
    refreshMyEndpoint();
    renderPath();
    renderEntries();
  }

  return { init: init, refreshPeers: refreshPeers, refreshMyEndpoint: refreshMyEndpoint };
})();
