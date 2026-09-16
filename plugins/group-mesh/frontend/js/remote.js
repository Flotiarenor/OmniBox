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
    body.innerHTML = '<div class="gm-remote-tools">' + up + '</div>' +
      '<table class="gm-table"><thead><tr><th>名称</th><th>大小</th><th></th></tr></thead>' +
      '<tbody>' + rows + '</tbody></table>';

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

  function bind() {
    var refresh = el('btn-peer-refresh');
    if (refresh) { refresh.addEventListener('click', refreshPeers); }
    var add = el('btn-peer-add');
    if (add) {
      add.addEventListener('click', function () {
        state.openModal('peer-box');
        var input = el('peer-endpoint');
        if (input) { input.focus(); }
      });
    }
    var close = el('btn-close-peer');
    if (close) {
      close.addEventListener('click', function () { state.closeModal('peer-box'); });
    }
    var doAdd = el('btn-do-add-peer');
    if (doAdd) {
      doAdd.addEventListener('click', function () {
        var endpoint = (el('peer-endpoint').value || '').trim();
        var name = (el('peer-name').value || '').trim();
        if (!endpoint) { toast('请填写对端地址', true); return; }
        state.call('peers', { action: 'add', endpoint: endpoint, name: name })
          .then(function (result) {
            if (!result || !result.success) {
              toast((result && result.error) || '添加失败', true);
              return;
            }
            state.closeModal('peer-box');
            el('peer-endpoint').value = '';
            el('peer-name').value = '';
            toast(result.existed ? '该地址已在列表里' : '已添加对端');
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
    renderPath();
    renderEntries();
  }

  return { init: init, refreshPeers: refreshPeers };
})();
