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
    if (typeof createLightbox === 'function') {
      this.lightbox = createLightbox({ getImageUrl: (item) => item.url || item });
    }
    this.updateStatus();
    await this.runScan();
  }

  /**
   * 把两个操作按钮挂到宿主（image-viewer 扩展面板）的头部去：内嵌页碰不到宿主头部，
   * 而"操作在顶栏"是其它插件的统一形态。只声明按钮，宿主用它的样式渲染；宿主不表态
   * 时整组留在本页工具栏（`mountToolbar` 会自动取消隐藏）。
   *
   * 挂两个而不是只挂设置：只搬一个的话，用户看到的是"设置上去了、重新扫描还在下面"，
   * 仍然是半成品。两个都挂，`#cleaner-actions` 整组收起，本页工具栏就只剩作用域信息。
   */
  _mountHostToolbar() {
    if (!window.HostChannel || typeof HostChannel.mountToolbar !== 'function') return;
    HostChannel.mountToolbar(
      [
        { id: 'btn-rescan', label: '重新扫描', icon: 'icon:refresh-cw', title: '忽略缓存，重新计算哈希' },
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
        document.getElementById('btn-rescan').addEventListener('click', () => this.runScan(true));
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

  async updateStatus() {
    const rootEl = document.getElementById('cleaner-root');
    if (!rootEl) return;
    try {
      const status = await Bridge.call('get_status');
      const root = status && status.root_dir;
      rootEl.textContent = root || '默认相册目录';
      rootEl.title = root || '默认相册目录';
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
    this.runScan();
  }

  async runScan(force = false) {
    const box = document.getElementById('cleaner-results');
    box.innerHTML = '<div class="empty-state">'
      + '<div class="empty-state-text">正在扫描…</div>'
      + '<div class="empty-state-hint">首次扫描要为每张图片算哈希，图库越大越久</div>'
      + '</div>';
    document.getElementById('cleaner-scanned').textContent = '';
    document.getElementById('cleaner-selected').textContent = '已选 0 张';
    this.selected.clear();
    try {
      const method = this.mode === 'dupe' ? 'duplicate_scan' : 'similar_scan';
      let result;

      // 非强制扫描时优先读取上次缓存，避免退出重进后全部重扫。
      if (!force) {
        try {
          result = await Bridge.call('get_cached_scan', this.mode);
        } catch (e) {
          result = null;
        }
        if (!result || !result.cached) {
          result = await Bridge.call(method);
        }
      } else {
        result = await Bridge.call(method);
      }

      this.groups = result.groups || [];
      const scanned = result.scanned || 0;
      this.visibleCount = this.pageSize;
      document.getElementById('cleaner-scanned').textContent = `已扫描 ${scanned} 张`;
      this.render();
    } catch (e) {
      console.error(e);
      box.innerHTML = '<div class="empty-state empty-state--error">'
        + '<div class="empty-state-icon"><svg class="obx-icon"><use href="#triangle-alert"></use></svg></div>'
        + '<div class="empty-state-text">扫描失败</div>'
        + '<div class="empty-state-hint">请确认「图片相册」已加载、相册目录可访问，然后点「重新扫描」</div>'
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
              <span title="${this._escapeAttr(f)}">${this._escapeHtml(f.split('/').pop())}</span>
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

      // 删除后不自动重新扫描，只从当前结果中移除已删除项；
      // 需要更新结果时由用户点击“重新扫描”触发。
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
