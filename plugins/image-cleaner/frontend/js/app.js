// 相册清理：image-viewer 的 Companion 插件前端
class ImageCleaner {
  constructor() {
    this.mode = 'dupe';
    this.groups = [];
    this.selected = new Set();
    this.pageSize = 20;
    this.visibleCount = 20;
    this.lightbox = null;
  }

  async init() {
    this._bind();
    this._mountHostToolbar();
    this._mountFreshness();
    if (typeof createLightbox === 'function') {
      this.lightbox = createLightbox({ getImageUrl: (item) => item.url || item });
    }
    this.updateStatus();
    await this._loadResults();
  }

  /**
   * 挂载共享的「同步状态 + 校验」控件（shell/freshness.js，见 plugin-guide §3.4）。
   *
   * 挂在内嵌态也可见的标签行，而不是那条 `html.is-embedded` 下整条隐藏的工具栏：
   * 本插件的主用法是嵌在图片相册的扩展面板里，工具栏那时看不到。
   * 原先的「重新扫描」按钮与它那一整套进度/轮询/取消（runScan / _waitForScan /
   * _showScanProgress）随之删除 —— 进度与取消由组件统一负责。
   */
  _mountFreshness() {
    const host = document.getElementById('cleaner-freshness');
    if (!host || typeof Freshness === 'undefined') return;
    this.freshness = Freshness.mount({
      plugin: 'image-cleaner',
      container: host,
      unit: '张',
      onChange: () => this._loadResults(),
    });
  }

  /**
   * 把「设置」挂到宿主（image-viewer 扩展面板）的头部去：内嵌页碰不到宿主头部，
   * 而"操作在顶栏"是其它插件的统一形态。宿主不表态时留在本页工具栏。
   *
   * 只声明「设置」：本插件的扫描入口是共享组件（`#cleaner-freshness`），它的 DOM
   * 归组件自己渲染，没法当按钮挂到宿主头部 —— 挂上去就会出现"头部一个校验、
   * 页内还有一个"的重复入口。
   */
  _mountHostToolbar() {
    if (!window.HostChannel || typeof HostChannel.mountToolbar !== 'function') return;
    HostChannel.mountToolbar(
      [
        { id: 'btn-settings', label: '设置', icon: 'icon:settings', title: '相似判定阈值等设置' },
      ],
      { container: 'host-toolbar', selector: '#cleaner-actions' }
    );
  }

  /**
   * 打开设置：优先请宿主渲染弹窗，宿主不在或不认这条请求时回落到本页的壳弹窗。
   *
   * 保存这件事必须留在本页：`Bridge` 是从当前 frame 往上找第一个带 pywebview.api 的
   * 窗口，本页嵌在 image-viewer 里时那仍然是壳，所以 `save_settings` 落的是**本插件**
   * 的设置。宿主那侧只拿到 values，且只负责回传，不碰存储。
   */
  async openSettings() {
    const fallback = () => openSettingsModal({ title: '相册清理设置' });
    if (!window.HostChannel || typeof HostChannel.requestSettings !== 'function') {
      fallback();
      return;
    }

    // 保存由宿主弹窗回传到这里执行：保存后整页重载本页（与本地弹窗一致的行为）。
    HostChannel.onCallback('ui', async (newValues) => {
      const result = await Bridge.call('save_settings', newValues);
      if (result && result.success === false) throw new Error(result.error || '保存失败');
      setTimeout(() => {
        window.location.href = window.location.href.split('?')[0] + '?_t=' + Date.now();
      }, 300);
      return result;
    });

    const res = await HostChannel.requestSettings('相册清理设置');
    if (!res.handled) fallback();
  }

  _bind() {
        // 扫描入口是共享组件（`_mountFreshness`），这里只剩设置与列表操作
        // 设置弹窗交给宿主渲染（image-viewer 的 `_serveHostChannel`）：本页是嵌进它的
        // iframe，`.modal{position:fixed}` 只相对本 iframe，弹窗没有整页遮罩、也贴不到
        // 宿主窗口中央。schema/values/保存仍然全归本插件 —— 宿主只负责画。
        // 宿主不表态（单独打开本页、或老版本壳）时回落到壳的统一设置弹窗。
        document.getElementById('btn-settings').addEventListener('click', () => this.openSettings());
    document.getElementById('btn-keep-one-all').addEventListener('click', () => this.keepOneForAll());
    document.getElementById('btn-delete').addEventListener('click', () => this.deleteSelected());
    document.getElementById('tab-dupe').addEventListener('click', () => this.switchMode('dupe'));
    document.getElementById('tab-similar').addEventListener('click', () => this.switchMode('similar'));
  }

  // 额外图库的虚拟路径带 `__<命名空间>/` 前缀（宿主的路由、缩略图与删除都按它走），
  // 显示时去掉这个标记，否则用户看到的是 `__额外图库/作者B/1.jpg` 这种"乱码"。
  _displayPath(rel) {
    return String(rel || '').replace(/^__/, '');
  }

  // 单独打开本页时把根目录填进工具栏；内嵌态这条工具栏整条不显示（根目录改在设置弹窗里，
  // 见后端 settings_schema 的 root_dir），没必要为一个隐藏节点再打一次后端。
  async updateStatus() {
    const rootEl = document.getElementById('cleaner-root');
    if (!rootEl || document.documentElement.classList.contains('is-embedded')) return;
    try {
      const status = await Bridge.call('get_status');
      const roots = (status && status.roots) || [];
      const root = (status && status.root_dir) || '';
      const extra = roots.length > 1 ? `（+${roots.length - 1} 个额外目录）` : '';
      rootEl.textContent = (root || '默认相册目录') + extra;
      rootEl.title = roots.length ? roots.join('\n') : (root || '默认相册目录');
    } catch (e) {
      rootEl.textContent = '默认相册目录';
      rootEl.title = '';
    }
  }

  switchMode(mode) {
    this.mode = mode;
    document.getElementById('tab-dupe').classList.toggle('active', mode === 'dupe');
    document.getElementById('tab-similar').classList.toggle('active', mode === 'similar');
    this.selected.clear();
    this.updateSelected();
    // 返回 Promise 便于调用方（与用例）await；点击处理器忽略返回值即可
    return this._loadResults();
  }

  /**
   * 读结果缓存并渲染（不再自己起扫描任务）。
   *
   * 扫描/校验由共享组件负责：它调 `system_freshness_verify` → 后端在整趟结束时
   * 重新分组写进缓存 → `onChange` 回调到这里重读。所以本方法只做两件事：
   * 读缓存、按 `stale` 提示用户该去校验。
   */
  async _loadResults() {
    const box = document.getElementById('cleaner-results');
    document.getElementById('cleaner-selected').textContent = '已选 0 张';
    this.selected.clear();
    try {
      const result = await Bridge.call('get_cached_scan', this.mode);
      this.groups = (result && result.groups) || [];
      const scanned = (result && result.scanned) || 0;
      this.visibleCount = this.pageSize;
      const stale = !!(result && result.stale);
      const scannedEl = document.getElementById('cleaner-scanned');
      if (!result || !result.cached) {
        scannedEl.textContent = '尚未校验';
        // 没结果就是"还没校验过"，不要说成"未发现重复图片" —— 那会让人以为
        // 图库是干净的。指向共享组件上的「校验」。
        box.innerHTML = '<div class="empty-state">'
          + '<div class="empty-state-icon"><svg class="obx-icon"><use href="#sparkles"></use></svg></div>'
          + '<div class="empty-state-text">尚未校验</div>'
          + '<div class="empty-state-hint">点「校验」开始比对（首次会为每张图算指纹，图库越大越久）</div>'
          + '</div>';
        this.groups = [];
        return;
      }
      scannedEl.textContent = `已扫描 ${scanned} 张` + (stale ? ' · 磁盘有变化，建议重新校验' : '');
      this.render();
    } catch (e) {
      console.error(e);
      box.innerHTML = '<div class="empty-state empty-state--error">'
        + '<div class="empty-state-icon"><svg class="obx-icon"><use href="#triangle-alert"></use></svg></div>'
        + '<div class="empty-state-text">读取结果失败</div>'
        + '<div class="empty-state-hint">请确认「图片相册」已加载、相册目录可访问，然后点「校验」</div>'
        + '</div>';
    }
  }

  render() {
    const box = document.getElementById('cleaner-results');
    const visibleGroups = this.groups.slice(0, this.visibleCount);
    if (!visibleGroups.length) {
      box.innerHTML = '<div class="empty-state">'
        + '<div class="empty-state-icon"><svg class="obx-icon"><use href="#sparkles"></use></svg></div>'
        + '<div class="empty-state-text">未发现' + (this.mode === 'dupe' ? '完全重复' : '相似') + '图片</div>'
        + '<div class="empty-state-hint">可切到「' + (this.mode === 'dupe' ? '相似图片' : '完全重复')
        + '」标签，或在「<svg class="obx-icon"><use href="#settings"></use></svg> 设置」里调整相似判定阈值</div>'
        + '</div>';
      return;
    }

    const moreHtml = this.groups.length > this.visibleCount
      ? `<button class="btn btn-sm cleaner-more" id="cleaner-more">显示更多（还有 ${this.groups.length - this.visibleCount} 组）</button>`
      : '';

    box.innerHTML = visibleGroups.map((group, gi) => `
      <div class="cleaner-group">
        <div class="cleaner-group-head">
          <span>${this.mode === 'dupe' ? `重复组 · ${group.files.length} 张 · ${(group.size / 1024 / 1024).toFixed(2)} MB` : `相似组 · ${group.files.length} 张`}</span>
          <button class="btn btn-sm" data-select-group="${gi}">全选组</button>
          <button class="btn btn-sm" data-keep-one="${gi}">只留一张</button>
        </div>
        <div class="cleaner-files">
          ${group.files.map(f => `
            <label class="cleaner-file">
              <input type="checkbox" data-file="${this._escapeAttr(f)}">
              <img src="${Bridge.thumbUrl(f)}" loading="lazy" alt="" data-view-file="${this._escapeAttr(f)}" data-group-index="${gi}" onerror="this.style.display='none'">
              <span title="${this._escapeAttr(this._displayPath(f))}">${this._escapeHtml(f.split('/').pop())}</span>
            </label>`).join('')}
        </div>
      </div>`).join('') + moreHtml;

    box.querySelectorAll('[data-select-group]').forEach(btn => {
      btn.addEventListener('click', () => {
        const group = this.groups[parseInt(btn.dataset.selectGroup, 10)];
        if (!group) return;
        const files = new Set(group.files);
        box.querySelectorAll('input[type="checkbox"][data-file]').forEach(cb => {
          if (files.has(cb.dataset.file)) {
            cb.checked = true;
            this.selected.add(cb.dataset.file);
          }
        });
        this.updateSelected();
      });
    });

    box.querySelectorAll('[data-keep-one]').forEach(btn => {
      btn.addEventListener('click', () => this.keepOneInGroup(parseInt(btn.dataset.keepOne, 10)));
    });

    box.querySelectorAll('[data-view-file]').forEach(img => {
      img.addEventListener('click', (e) => {
        e.preventDefault();
        e.stopPropagation();
        const group = this.groups[parseInt(img.dataset.groupIndex, 10)];
        const file = img.dataset.viewFile;
        if (!group) return;
        const items = group.files.map(f => ({ url: f }));
        const index = group.files.indexOf(file);
        const parentIv = parent && parent.imageViewer;
        if (parentIv && parentIv.lightbox) {
          // 优先使用 image-viewer 的全屏查看器，这样可以看到图片信息
          parentIv.lightbox.show(items, index);
        } else if (this.lightbox) {
          this.lightbox.show(items, index);
        }
      });
    });

    box.querySelectorAll('input[type="checkbox"][data-file]').forEach(cb => {
      cb.addEventListener('change', () => {
        if (cb.checked) this.selected.add(cb.dataset.file);
        else this.selected.delete(cb.dataset.file);
        this.updateSelected();
      });
    });

    const moreBtn = box.querySelector('#cleaner-more');
    if (moreBtn) moreBtn.addEventListener('click', () => this.showMore());
  }

  keepOneInGroup(index) {
    const box = document.getElementById('cleaner-results');
    const group = this.groups[index];
    if (!group || !box) return;
    const keep = group.files[0];
    const files = new Set(group.files);
    box.querySelectorAll('input[type="checkbox"][data-file]').forEach(cb => {
      if (!files.has(cb.dataset.file)) return;
      if (cb.dataset.file === keep) {
        cb.checked = false;
        this.selected.delete(keep);
      } else {
        cb.checked = true;
        this.selected.add(cb.dataset.file);
      }
    });
    this.updateSelected();
  }

  keepOneForAll() {
    this.groups.forEach(group => {
      const keep = group.files[0];
      group.files.forEach(f => {
        if (f === keep) this.selected.delete(f);
        else this.selected.add(f);
      });
    });
    this.render();
    this._syncCheckboxes();
    this.updateSelected();
  }

  _syncCheckboxes() {
    const box = document.getElementById('cleaner-results');
    if (!box) return;
    box.querySelectorAll('input[type="checkbox"][data-file]').forEach(cb => {
      cb.checked = this.selected.has(cb.dataset.file);
    });
  }

  showMore() {
    this.visibleCount += this.pageSize;
    this.render();
  }

  updateSelected() {
    document.getElementById('cleaner-selected').textContent = `已选 ${this.selected.size} 张`;
  }

  async deleteSelected() {
    const files = [...this.selected];
    if (!files.length) {
      Toast.warning('请先勾选要删除的图片');
      return;
    }
    const ok = await confirmDialog(`确定删除选中的 ${files.length} 张图片吗？删除后不可恢复。`, { danger: true });
    if (!ok) return;
    try {
      const result = await Bridge.call('delete_files', files);
      if (result.errors && result.errors.length) {
        Toast.error(`部分删除失败: ${result.errors.join('; ')}`);
      } else {
        Toast.success(`已删除 ${files.length} 张图片`);
      }

      // 删除后不自动重新分组，只从当前结果中移除已删除项；同时让后端的被动同步
      // 立刻看到这次删除（指纹与结果缓存随之作废，状态行会提示"建议重新校验"）。
      if (this.freshness && typeof this.freshness.autoSync === 'function') {
        this.freshness.autoSync('');
      }
      const deleted = new Set(result.deleted || []);
      if (deleted.size) {
        this.groups = this.groups
          .map(group => ({
            ...group,
            files: group.files.filter(f => !deleted.has(f))
          }))
          .filter(group => group.files.length >= 2);
        this.selected.clear();
        this.render();
        this.updateSelected();
      }
    } catch (e) {
      Toast.error('删除请求失败');
    }
  }

  // 统一走内核 window.Utils.escapeHtml（转义 &<>"' ，属性场景同样安全），
  // 并让 null/undefined 与其它插件一样返回空串（以前 String(null) 会得到 "null"）。
  _escapeHtml(str) {
    if (window.Utils && typeof window.Utils.escapeHtml === 'function') {
      return window.Utils.escapeHtml(str);
    }
    if (str == null) return '';
    return String(str).replace(/[&<>"']/g, c => ({
      '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;'
    }[c]));
  }

  _escapeAttr(str) {
    return this._escapeHtml(str);
  }
}
