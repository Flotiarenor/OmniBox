// ==================== 目录列表 / 目录选择器（Shell 共享组件） ====================
//
// 这套东西原本长在 image-viewer 里（`plugins/image-viewer/frontend/js/app.js` 的
// `_renderRoots` / `_addRootFromInput` / `openDirBrowser` / `_loadDirBrowser`，样式是
// image-viewer.css 的 `.iv-root-*` 与 `.iv-dirbrowser-*`）。媒体播放器 / 漫画 / 小说
// 也需要同一套「多位置文件夹」界面，所以整体搬到这里，**实现只有这一份**：
//
//   - image-viewer：标签用「主要 / 额外」，空列表提示回退 ./data，引用回来即可；
//   - media-player / manga-library / novel-reader：走设置弹窗的
//     `settings_schema` type:"directory"，同一套 DOM 与样式，由 base.js 调用。
//
// 类名与样式值保持原样（`.iv-root-row` / `.iv-root-tag` / `.iv-dirbrowser-item` …）
// 并放在 `public/shell/folder-picker.css`，不拆散也不改名 —— 搬迁不引入视觉变化。
//
// 目录枚举走宿主接口 `system_browse_dir`（→ 共享基建
// `shell/backend/media_catalog.list_subdirectories`），所以用的是 `Bridge.callSystem`：
// `Bridge.call` 会自动带上插件前缀（`<plugin>__<method>`），打不到宿主方法上。
window.FolderPicker = (function () {
  // 「我的电脑」层：与后端 media_catalog.DRIVES_SENTINEL 保持一致
  const DRIVES_SENTINEL = '__drives__';
  const KIND_LABELS = { image: '图片', video: '视频', audio: '音乐' };

  function normalize(path) {
    return String(path == null ? '' : path).trim().replace(/[\\/]+$/, '');
  }

  /** 目录浏览弹窗：解析用户选中的绝对路径（取消/未选返回 null）。 */
  function openDirBrowser(startPath) {
    return new Promise((resolve) => {
      const overlay = document.createElement('div');
      overlay.className = 'modal active';
      overlay.innerHTML = `
        <div class="modal-box obx-anim-scale" style="width:520px;">
          <h3>选择目录</h3>
          <div class="iv-dirbrowser">
            <div class="iv-dirbrowser-bar">
              <button class="btn btn-sm" data-act="drives">💻 我的电脑</button>
              <button class="btn btn-sm" data-act="up">↑ 上级</button>
              <span class="iv-dirbrowser-path" data-act="path"></span>
            </div>
            <div class="iv-dirbrowser-list" data-act="list"></div>
          </div>
          <div class="modal-footer">
            <button class="btn" data-act="cancel">取消</button>
            <button class="btn btn-primary" data-act="select">选择当前目录</button>
          </div>
        </div>`;
      document.body.appendChild(overlay);

      const listEl = overlay.querySelector('[data-act="list"]');
      const pathEl = overlay.querySelector('[data-act="path"]');
      const upBtn = overlay.querySelector('[data-act="up"]');
      const drivesBtn = overlay.querySelector('[data-act="drives"]');
      const selectBtn = overlay.querySelector('[data-act="select"]');
      let currentPath = '';
      let parentPath = null;

      function finish(path) {
        overlay.remove();
        resolve(path || null);
      }

      async function load(path) {
        // 「我的电脑」层用共享基建的哨兵路径表示：从任意目录都能一步退回盘符列表
        const isDrives = path === DRIVES_SENTINEL;
        listEl.innerHTML = '<div class="loading">加载中…</div>';
        try {
          const data = await Bridge.callSystem('system_browse_dir', isDrives ? '' : (path || ''));
          currentPath = isDrives ? DRIVES_SENTINEL : (data.path || '');
          parentPath = isDrives ? null : data.parent;
          pathEl.textContent = isDrives ? '我的电脑' : (data.path || '我的电脑');
          upBtn.disabled = !parentPath;
          upBtn.style.opacity = parentPath ? '1' : '0.45';
          // 「我的电脑」按钮：进入盘符列表后它就是当前层，同理禁用
          const atDrives = !currentPath || currentPath === DRIVES_SENTINEL;
          drivesBtn.disabled = atDrives;
          drivesBtn.style.opacity = atDrives ? '1' : '0.45';
          selectBtn.disabled = atDrives;   // 盘符列表本身不是可选目录
          const entries = data.entries || [];
          if (data.error) {
            listEl.innerHTML = `<div class="iv-dirbrowser-item empty">${Utils.escapeHtml(data.error)}</div>`;
            return;
          }
          if (!entries.length) {
            listEl.innerHTML = '<div class="iv-dirbrowser-item empty">该目录下没有子文件夹</div>';
            return;
          }
          listEl.innerHTML = entries.map((entry) => {
            const kinds = entry.kinds || [];
            const label = kinds.map(k => KIND_LABELS[k] || '').filter(Boolean).join('·');
            return `
            <div class="iv-dirbrowser-item" data-path="${Utils.escapeHtml(entry.path)}">
                <span>📁</span><span>${Utils.escapeHtml(entry.name)}</span>
                ${label ? `<span class="iv-dirbrowser-hint">含 ${Utils.escapeHtml(label)}</span>` : ''}
            </div>`;
          }).join('');
          listEl.querySelectorAll('.iv-dirbrowser-item[data-path]').forEach((item) => {
            item.addEventListener('click', () => load(item.dataset.path));
          });
        } catch (e) {
          listEl.innerHTML = '<div class="iv-dirbrowser-item empty">目录读取失败</div>';
        }
      }

      upBtn.addEventListener('click', () => { if (parentPath) load(parentPath); });
      drivesBtn.addEventListener('click', () => load(DRIVES_SENTINEL));
      selectBtn.addEventListener('click', () => finish(currentPath));
      overlay.querySelector('[data-act="cancel"]').addEventListener('click', () => finish(null));
      overlay.addEventListener('click', (e) => { if (e.target === overlay) finish(null); });

      load(startPath || DRIVES_SENTINEL);
    });
  }

  /**
   * 目录列表控件（多位置文件夹）。
   *
   * opts.paths        初始路径数组（列表就是这个数组的唯一来源）
   * opts.labels       可选，每行右侧的补充标签（image-viewer 用它标「只含图片」）
   * opts.placeholder  输入框占位符
   * opts.emptyText    列表为空时的提示
   * opts.onBeforeOpen 打开选择器前的钩子，可在此时把新发现的目录 push 进 paths
   */
  function createList(opts) {
    const options = opts || {};
    const paths = options.paths || [];
    const root = document.createElement('div');
    root.className = 'iv-roots';
    // 两段式：列表与「添加目录」行分开，中间那条 2px 是刻意留的（与图片相册一致）
    const listBox = document.createElement('div');
    listBox.className = 'iv-roots-list';
    const addRow = document.createElement('div');
    addRow.className = 'iv-roots-add';
    addRow.innerHTML = `
      <input type="text" class="search-input" placeholder="${Utils.escapeHtml(options.placeholder || '输入目录绝对路径')}">
      <button class="btn btn-sm" data-act="browse">浏览…</button>
      <button class="btn btn-sm" data-act="add">添加</button>`;
    root.append(listBox, addRow);

    const input = addRow.querySelector('input');
    const browseBtn = addRow.querySelector('[data-act="browse"]');
    const addBtn = addRow.querySelector('[data-act="add"]');

    function render() {
      // 「主要」是**位置**属性而不是每行自带的标记：第一行就是主目录，
      // 于是每行都能删（和「额外」行完全一样），删掉主目录后下一行自动顶上，
      // 不会出现「列表里没有主目录」的中间状态。
      const rows = paths.map((path, index) => {
        const isPrimary = index === 0;
        const extra = options.labels ? options.labels(path) : '';
        return `
        <div class="iv-root-row${isPrimary ? ' is-primary' : ''}">
            <span class="iv-root-tag${isPrimary ? '' : ' iv-root-tag-extra'}">${isPrimary ? '主要' : '额外'}</span>
            <span class="iv-root-path" title="${Utils.escapeHtml(path)}">${Utils.escapeHtml(path)}</span>
            ${extra ? `<span class="iv-root-note">${Utils.escapeHtml(extra)}</span>` : ''}
            <button class="iv-root-remove" data-index="${index}" title="移除">✕</button>
        </div>`;
      }).join('');
      listBox.innerHTML = rows || `<div class="iv-roots-empty">${Utils.escapeHtml(options.emptyText || '未添加任何目录')}</div>`;
      listBox.querySelectorAll('.iv-root-remove').forEach((btn) => {
        btn.addEventListener('click', () => {
          paths.splice(Number(btn.dataset.index), 1);
          render();
        });
      });
    }

    function addPath(raw) {
      const value = normalize(raw);
      if (!value) {
        Toast.warning('请输入或浏览选择一个目录');
        return false;
      }
      if (paths.some(p => normalize(p) === value)) {
        Toast.warning('该目录已在列表中');
        return false;
      }
      paths.push(value);
      render();
      return true;
    }

    addBtn.addEventListener('click', () => { if (addPath(input.value)) input.value = ''; });
    input.addEventListener('keydown', (e) => {
      if (e.key === 'Enter') {
        e.preventDefault();
        if (addPath(input.value)) input.value = '';
      }
    });
    browseBtn.addEventListener('click', async () => {
      if (options.onBeforeOpen) options.onBeforeOpen();
      const picked = await openDirBrowser(input.value.trim());
      if (picked) addPath(picked);
    });

    render();

    return {
      element: root,
      input,
      render,
      addPath,
      getPaths: () => paths,
      setPaths(list) {
        paths.length = 0;
        (list || []).forEach(p => paths.push(p));
        render();
      },
    };
  }

  return { DRIVES_SENTINEL, KIND_LABELS, normalize, openDirBrowser, createList };
})();
