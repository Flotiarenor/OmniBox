// 相册清理：image-viewer 的 Companion 插件前端
class ImageCleaner {
  constructor() {
    this.mode = 'dupe';
    this.groups = [];
    this.selected = new Set();
    this.pageSize = 20;
    this.visibleCount = 20;
  }

  async init() {
    this._bind();
    this.updateStatus();
    await this.runScan();
  }

  _bind() {
    document.getElementById('btn-rescan').addEventListener('click', () => this.runScan(true));
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
    box.innerHTML = '<div class="cleaner-empty">扫描中…请稍候</div>';
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
      box.innerHTML = '<div class="cleaner-empty">⚠️ 扫描失败，请确认 image-viewer 已加载且相册目录可访问</div>';
    }
  }

  render() {
    const box = document.getElementById('cleaner-results');
    const visibleGroups = this.groups.slice(0, this.visibleCount);
    if (!visibleGroups.length) {
      box.innerHTML = '<div class="cleaner-empty">✨ 未发现' + (this.mode === 'dupe' ? '完全重复' : '相似') + '图片</div>';
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
              <img src="${Bridge.thumbUrl(f)}" loading="lazy" alt="" onerror="this.style.display='none'">
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
      await this.runScan(true);
    } catch (e) {
      Toast.error('删除请求失败');
    }
  }

  _escapeHtml(str) {
    return String(str).replace(/[&<>"']/g, c => ({
      '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;'
    }[c]));
  }

  _escapeAttr(str) {
    return this._escapeHtml(str);
  }
}
