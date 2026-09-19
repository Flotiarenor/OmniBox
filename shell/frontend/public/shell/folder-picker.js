// ==================== 目录列表 / 目录选择器（Shell 共享组件） ====================
//
// 这套东西原本长在 image-viewer 里（`plugins/image-viewer/frontend/js/app.js` 的
// `_renderRoots` / `_addRootFromInput` / `openDirBrowser` / `_loadDirBrowser`，样式是
// image-viewer.css 的 `.iv-root-*` 与 `.iv-dirbrowser-*`）。媒体播放器 / 漫画 / 文档
// 也需要同一套「多位置文件夹」界面，所以整体搬到这里，**实现只有这一份**：
//
//   - image-viewer：标签用「主要 / 额外」，空列表提示回退 ./data，引用回来即可；
//   - media-player / manga-library / document-reader：走设置弹窗的
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
      // pointerdown：只看按下位置，弹窗内按下再拖到遮罩上松开不应关闭
      overlay.addEventListener('pointerdown', (e) => { if (e.target === overlay) finish(null); });

      load(startPath || DRIVES_SENTINEL);
    });
  }

  // ===== 网络位置（把远端共享项取到本地一个目录，再加进列表） =====
  //
  // 「网络位置」**不是一种新类型的路径**：本组件与所有消费方（后端都是 `os.path`
  // 那一套）只认本地绝对路径，所以"添加网络位置"的产物仍然是一个本地目录 ——
  // 由**提供方插件**负责把远端内容取到那里，再把该目录回填进来。这样：
  //   - 消费方一行不用改（`getPaths()` 的语义没变）；
  //   - 壳不需要知道任何具体插件名（下面用扩展声明发现提供方）；
  //   - 插件之间不需要声明依赖。
  //
  // 提供方的声明方式（与 image-cleaner 注册侧栏入口同一套宿主/扩展机制）：
  //   get_extensions() -> {'placement': 'network-location', 'label': …, 'embedUrl': …}
  // **刻意不写 `host`**：本组件出现在任意插件的设置里，提供方应当对所有宿主可用。
  const NETWORK_MESSAGE_TYPE = 'omnibox:network-location';

  /** 发现"网络位置"提供方：声明了该 placement 且有 embedUrl 的插件扩展。 */
  async function loadNetworkProviders() {
    try {
      const list = await Bridge.callSystem('system_get_plugin_extensions', null, 'network-location');
      return (Array.isArray(list) ? list : []).filter(
        (ext) => ext && typeof ext.embedUrl === 'string' && ext.embedUrl);
    } catch (e) {
      // 宿主接口不可用（桩环境、老壳）时按"没有提供方"处理，不把设置页弄崩
      return [];
    }
  }

  /**
   * 提供方 → 宿主 的回填协议：只接受**来自那个 iframe**、形状正确的消息。
   *
   * 校验来源是必须的：任何同源页面都能 `postMessage` 到本窗口，不校验就等于让
   * 任意页面往用户的文件夹列表里塞路径。形状不对一律返回 null（静默忽略）。
   */
  function readProviderMessage(event, sourceWindow) {
    if (!event || !sourceWindow || event.source !== sourceWindow) return null;
    const data = event.data;
    if (!data || data.type !== NETWORK_MESSAGE_TYPE) return null;
    if (data.action === 'cancelled') return { action: 'cancelled' };
    if (data.action !== 'picked') return null;
    const path = normalize(data.path);
    if (!path) return null;
    return { action: 'picked', path, label: typeof data.label === 'string' ? data.label : '' };
  }

  /**
   * 提供方菜单里的一行。
   *
   * 单独抽成函数而不是写在模板里直接 `.map(...)`：转义门禁要求模板里**每一个**插值
   * 都在 `tools/check_frontend_escape.cjs` 的登记表里登记，而一个跨多行的嵌套模板
   * 只能整段抄进登记表（缩进错一格就失效）。拆开之后外层是
   * `${providers.map(providerRow).join('')}`，内层三处也各自是一行。
   * `icon` / `label` 都来自插件声明，**必须转义**。
   */
  function providerRow(ext, index) {
    return `
                <div class="iv-dirbrowser-item" data-index="${index}">
                  <span>${Utils.escapeHtml(ext.icon || '🌐')}</span>
                  <span>${Utils.escapeHtml(ext.label || ext.plugin || '网络位置')}</span>
                </div>`;
  }

  /** 提供方选择菜单（只有一个提供方时不会走到这里）。 */
  function openProviderMenu(providers) {
    return new Promise((resolve) => {
      const overlay = document.createElement('div');
      overlay.className = 'modal active';
      overlay.innerHTML = `
        <div class="modal-box obx-anim-scale" style="width:420px;">
          <h3>选择网络位置来源</h3>
          <div class="iv-dirbrowser">
            <div class="iv-dirbrowser-list" data-act="list">
              ${providers.map(providerRow).join('')}
            </div>
          </div>
          <div class="modal-footer">
            <button class="btn" data-act="cancel">取消</button>
          </div>
        </div>`;
      document.body.appendChild(overlay);
      function finish(result) {
        overlay.remove();
        resolve(result || null);
      }
      overlay.querySelectorAll('[data-index]').forEach((item) => {
        item.addEventListener('click', () => finish(providers[Number(item.dataset.index)]));
      });
      overlay.querySelector('[data-act="cancel"]').addEventListener('click', () => finish(null));
      overlay.addEventListener('pointerdown', (e) => { if (e.target === overlay) finish(null); });
    });
  }

  /** 打开提供方页面，等它回填一个本地目录（取消 / 关窗返回 null）。 */
  function openNetworkPicker(provider) {
    return new Promise((resolve) => {
      const overlay = document.createElement('div');
      overlay.className = 'modal active';
      overlay.innerHTML = `
        <div class="modal-box obx-anim-scale" style="width:640px;">
          <h3>${Utils.escapeHtml(provider.label || '网络位置')}</h3>
          <iframe class="iv-network-frame" src="${Utils.escapeHtml(provider.embedUrl)}"
                  title="${Utils.escapeHtml(provider.label || '网络位置')}"></iframe>
          <div class="modal-footer">
            <button class="btn" data-act="cancel">取消</button>
          </div>
        </div>`;
      document.body.appendChild(overlay);
      const frame = overlay.querySelector('iframe');
      const source = frame ? frame.contentWindow : null;
      function finish(result) {
        window.removeEventListener('message', onMessage);
        overlay.remove();
        resolve(result || null);
      }
      function onMessage(event) {
        const picked = readProviderMessage(event, source);
        if (picked) finish(picked);
      }
      window.addEventListener('message', onMessage);
      overlay.querySelector('[data-act="cancel"]').addEventListener('click', () => finish(null));
      overlay.addEventListener('pointerdown', (e) => { if (e.target === overlay) finish(null); });
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
   * opts.localOnly    可选，true 时**不显示「🌐 网络位置」**：该字段只接受本机目录。
   *                    用于"目录本身就是产物"的字段（如 group-mesh 的远端下载目录）——
   *                    在那种字段上选"网络位置"语义是错的（远端内容要取到本地，
   *                    而下载目录正是落点），见 docs/group-mesh-design.md §1.4。
   */
  function createList(opts) {
    const options = opts || {};
    const paths = options.paths || [];
    const localOnly = options.localOnly === true;
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
    // 「网络位置」只在允许远端来源的字段里给：目录本身就是产物时（下载落点）选了它
    // 语义不成立，按钮放出来只会误导。用 DOM 追加而不是写进上面的模板 ——
    // 模板里的条件插值要额外走转义登记（tools/check_frontend_escape.cjs）。
    if (!localOnly) {
      addRow.insertAdjacentHTML('beforeend',
        '<button class="btn btn-sm" data-act="network" '
        + 'title="从其它设备取一个共享项到本地目录">🌐 网络位置</button>');
    }
    root.append(listBox, addRow);

    const input = addRow.querySelector('input');
    const browseBtn = addRow.querySelector('[data-act="browse"]');
    const addBtn = addRow.querySelector('[data-act="add"]');
    const networkBtn = addRow.querySelector('[data-act="network"]');
    // 通过网络位置加进来的目录 → 一行说明（"团体组网 · 设备A/相册"）。
    // 与 `options.labels` 分开存：后者是宿主自己算的（如「只含图片」），两者都要显示。
    const networkLabels = new Map();

    function render() {
      // 「主要」是**位置**属性而不是每行自带的标记：第一行就是主目录，
      // 于是每行都能删（和「额外」行完全一样），删掉主目录后下一行自动顶上，
      // 不会出现「列表里没有主目录」的中间状态。
      const rows = paths.map((path, index) => {
        const isPrimary = index === 0;
        const extra = options.labels ? options.labels(path) : '';
        const network = networkLabels.get(normalize(path)) || '';
        const note = [network, extra].filter(Boolean).join(' · ');
        return `
        <div class="iv-root-row${isPrimary ? ' is-primary' : ''}">
            <span class="iv-root-tag${isPrimary ? '' : ' iv-root-tag-extra'}">${isPrimary ? '主要' : '额外'}</span>
            <span class="iv-root-path" title="${Utils.escapeHtml(path)}">${Utils.escapeHtml(path)}</span>
            ${note ? `<span class="iv-root-note">${Utils.escapeHtml(note)}</span>` : ''}
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
    // localOnly 时这个按钮根本不在 DOM 里（见上面的模板），因此要判空
    if (networkBtn) networkBtn.addEventListener('click', async () => {
      if (options.onBeforeOpen) options.onBeforeOpen();
      const providers = await loadNetworkProviders();
      if (!providers.length) {
        Toast.warning('没有可用的网络位置来源：需要安装提供该能力的插件（如「团体组网」）');
        return;
      }
      const provider = providers.length === 1 ? providers[0] : await openProviderMenu(providers);
      if (!provider) return;
      const picked = await openNetworkPicker(provider);
      if (!picked || picked.action !== 'picked') return;
      if (addPath(picked.path) && picked.label) {
        networkLabels.set(normalize(picked.path), picked.label);
        render();
      }
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

  return {
    DRIVES_SENTINEL,
    KIND_LABELS,
    NETWORK_MESSAGE_TYPE,
    normalize,
    openDirBrowser,
    createList,
    loadNetworkProviders,
    readProviderMessage,
    providerRow,
  };
})();
