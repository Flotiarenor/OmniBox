// ===== OmniBox 统一文件浏览器内核 =====
// 供图片查看器、视频播放器等媒体类插件复用：
// 统一处理面包屑、目录树、当前目录枚举、搜索、多选和内容渲染外壳。
class FileBrowser {
    constructor(root, options = {}) {
        this.root = root;
        this.options = options;

        this.currentPath = options.initialPath || '';
        this.dirs = [];
        this.files = [];
        this.breadcrumbs = [];
        this.filter = '';
        this.viewMode = options.viewMode || 'list';
        this.multiSelectEnabled = false;
        this.selected = new Set();
        this.activePath = null;

        this._tree = null;
        this._destroyed = false;
        this._bindEvents();

        if (options.initialPath !== undefined) this.navigate(options.initialPath);
    }

    _bindEvents() {
        if (!this.root) return;
        this.root.innerHTML = `
            <div class="filebrowser-toolbar">
                <div class="filebrowser-crumbs" data-role="crumbs"></div>
                <div class="filebrowser-toolbar-actions">
                    <input type="text" class="search-input" data-role="filter" placeholder="搜索...">
                    <select class="filebrowser-view-select" data-role="view">
                        <option value="list">列表</option>
                        <option value="grid">网格</option>
                    </select>
                    <button class="btn btn-sm" data-role="multi">多选</button>
                </div>
            </div>
            <div class="filebrowser-body">
                <div class="filebrowser-tree" data-role="tree" style="display:none;"></div>
                <div class="filebrowser-content" data-role="content"></div>
            </div>
            <div class="filebrowser-selectionbar" data-role="selection" style="display:none;">
                <span data-role="selection-count">已选 0 项</span>
                <button class="btn btn-sm" data-role="clear-selection">清除选择</button>
            </div>
        `;

        this._crumbsEl = this.root.querySelector('[data-role="crumbs"]');
        this._filterEl = this.root.querySelector('[data-role="filter"]');
        this._viewEl = this.root.querySelector('[data-role="view"]');
        this._multiBtn = this.root.querySelector('[data-role="multi"]');
        this._treeEl = this.root.querySelector('[data-role="tree"]');
        this._contentEl = this.root.querySelector('[data-role="content"]');
        this._selectionEl = this.root.querySelector('[data-role="selection"]');
        this._selectionCountEl = this.root.querySelector('[data-role="selection-count"]');

        this._viewEl.value = this.viewMode;
        if (this.options.showTree !== false) this._treeEl.style.display = 'block';

        this._filterEl.addEventListener('input', () => {
            this.filter = this._filterEl.value.trim().toLowerCase();
            this._renderContent();
        });

        this._viewEl.addEventListener('change', () => {
            this.setViewMode(this._viewEl.value);
        });

        this._multiBtn.addEventListener('click', () => this.toggleMultiSelect());

        const clearBtn = this.root.querySelector('[data-role="clear-selection"]');
        if (clearBtn) clearBtn.addEventListener('click', () => this.clearSelection());

        this._initTree();
    }

    _initTree() {
        if (!this.options.showTree || !this.options.onLoadChildren) return;
        if (typeof createTree !== 'function') return;
        this._tree = createTree(this._treeEl, {
            data: [{ name: '根目录', path: '' }],
            onLoadChildren: (path) => this.options.onLoadChildren(path),
            onClick: (item) => this.navigate(item.path)
        });
    }

    async navigate(path) {
        if (this._destroyed) return;
        this.currentPath = path || '';
        this.clearSelection();
        if (this.options.onNavigate) this.options.onNavigate(path);
        try {
            const data = await this.options.onBrowse(path || '');
            this.dirs = data.dirs || [];
            this.files = data.files || [];
            this.breadcrumbs = data.breadcrumbs || [];
            this._renderCrumbs();
            this._renderContent();
            if (this._tree && typeof this._tree.selectPath === 'function') this._tree.selectPath(path || '');
        } catch (e) {
            console.error('FileBrowser navigate failed:', e);
            this.dirs = [];
            this.files = [];
            this._renderContent();
        }
    }

    async refresh() {
        await this.navigate(this.currentPath);
    }

    _renderCrumbs() {
        this._crumbsEl.innerHTML = '';
        const crumbs = this.breadcrumbs.length ? this.breadcrumbs : [{ name: '根目录', path: '' }];
        crumbs.forEach((crumb, index) => {
            const span = document.createElement('span');
            span.className = 'filebrowser-crumb' + (index === crumbs.length - 1 ? ' active' : '');
            span.textContent = crumb.name;
            if (index < crumbs.length - 1) {
                span.addEventListener('click', () => this.navigate(crumb.path));
            }
            this._crumbsEl.appendChild(span);
            if (index < crumbs.length - 1) {
                const sep = document.createElement('span');
                sep.className = 'filebrowser-crumb-sep';
                sep.textContent = '/';
                this._crumbsEl.appendChild(sep);
            }
        });
    }

    _filtered() {
        const dirs = this.filter
            ? this.dirs.filter(d => d.name.toLowerCase().includes(this.filter))
            : [...this.dirs];
        const files = this.filter
            ? this.files.filter(f => f.name.toLowerCase().includes(this.filter))
            : [...this.files];
        dirs.sort((a, b) => a.name.localeCompare(b.name));
        files.sort((a, b) => a.name.localeCompare(b.name));
        return { dirs, files };
    }

    _renderContent() {
        if (this._destroyed) return;
        this._contentEl.innerHTML = '';
        if (typeof this.options.renderContent === 'function') {
            this.options.renderContent(this, this._contentEl, this._filtered());
            return;
        }
        this._renderList();
    }

    _renderList() {
        const { dirs, files } = this._filtered();
        const list = document.createElement('div');
        list.className = 'filebrowser-list';

        const iconFor = (kind) => {
            if (kind === 'video') return '🎬';
            if (kind === 'audio') return '🎵';
            if (kind === 'image') return '🖼️';
            return '📄';
        };

        const makeRow = (text, icon, cls, filePath, onClick) => {
            const row = document.createElement('div');
            row.className = 'filebrowser-row ' + cls;
            if (filePath && filePath === this.activePath) row.classList.add('active');
            if (filePath && this.selected.has(filePath)) row.classList.add('selected');

            if (this.multiSelectEnabled && filePath) {
                const box = document.createElement('input');
                box.type = 'checkbox';
                box.checked = this.selected.has(filePath);
                box.addEventListener('change', () => this._toggleSelect(filePath));
                row.appendChild(box);
                row.addEventListener('click', (e) => {
                    if (e.target === box) return;
                    this._toggleSelect(filePath);
                });
            } else {
                row.addEventListener('click', onClick);
            }

            const iconSpan = document.createElement('span');
            iconSpan.className = 'filebrowser-icon';
            iconSpan.textContent = icon;
            const nameSpan = document.createElement('span');
            nameSpan.className = 'filebrowser-name';
            nameSpan.textContent = text;
            row.append(iconSpan, nameSpan);
            return row;
        };

        dirs.forEach(dir => {
            list.appendChild(makeRow(dir.name, '📁', 'folder', null, () => this.navigate(dir.path)));
        });

        files.forEach(file => {
            list.appendChild(makeRow(file.name, iconFor(file.kind), 'file', file.path, () => {
                if (this.options.onOpenFile) this.options.onOpenFile(file);
            }));
        });

        if (dirs.length === 0 && files.length === 0) {
            list.innerHTML = '<div class="filebrowser-empty">当前目录为空</div>';
        }
        this._contentEl.appendChild(list);
    }

    setActiveFile(path) {
        this.activePath = path || null;
        this._renderContent();
    }

    getCurrentFiles() {
        return this.files || [];
    }

    setViewMode(mode) {
        this.viewMode = mode === 'grid' ? 'grid' : 'list';
        if (this._viewEl) this._viewEl.value = this.viewMode;
        if (this.options.onViewModeChange) this.options.onViewModeChange(this.viewMode);
        this._renderContent();
    }

    toggleMultiSelect() {
        this.multiSelectEnabled = !this.multiSelectEnabled;
        this._multiBtn.classList.toggle('active', this.multiSelectEnabled);
        if (!this.multiSelectEnabled) this.clearSelection();
        this._renderContent();
    }

    _toggleSelect(path) {
        if (this.selected.has(path)) this.selected.delete(path);
        else this.selected.add(path);
        this._updateSelectionBar();
        this._renderContent();
    }

    clearSelection() {
        this.selected.clear();
        this._updateSelectionBar();
        this._renderContent();
    }

    getSelected() {
        return this.files.filter(f => this.selected.has(f.path));
    }

    _updateSelectionBar() {
        if (!this._selectionEl) return;
        const count = this.selected.size;
        this._selectionEl.style.display = count > 0 ? 'flex' : 'none';
        this._selectionCountEl.textContent = `已选 ${count} 项`;
        if (this.options.onSelectionChange) this.options.onSelectionChange(this.getSelected());
    }

    destroy() {
        this._destroyed = true;
        if (this.root) this.root.innerHTML = '';
    }
}

function createFileBrowser(root, options) {
    return new FileBrowser(root, options);
}
