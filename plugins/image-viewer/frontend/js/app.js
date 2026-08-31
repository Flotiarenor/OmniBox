// ============================================================
// 图片相册 v2：嵌套文件夹相册 + 时间线 / 最近添加 / 搜索 / 幻灯片
// ============================================================
class ImageViewer {
    constructor() {
        this.mode = 'albums';            // albums | children | images
        this.currentView = 'albums';     // albums | timeline | latest
        this.childParentPath = '';
        this.fromChildren = false;
        this.currentPath = '';
        this.currentPage = 1;
        this.currentItems = [];
        this.currentImages = [];
        this.currentAllImages = [];      // 连续浏览序列：整个混合视图按瀑布流顺序展开的全部图片
        this.currentAllOffset = 0;       // 当前页首项在连续序列中的起始偏移（分页对齐用）
        this.filteredSeqIndexes = null;  // 当前页搜索过滤后，瓦片在完整连续序列中的起始位置
        this.navStack = [];              // 混合瀑布流逐层点入时的返回栈
        this.currentSettings = {};
        this.currentRowHeight = 200;
        this.albums = [];
        this.albumConfig = { collapsed: [], promoted: [] };
        this.albumSortBy = 'name';       // 作者页面排序：name | mtime | count
        this.albumSortOrder = 'asc';     // 作者页面排序方向
        this.isMultiSelectMode = false;
        this.selectedImages = new Set();
        this.moveDestPath = '';
        this.slideshowTimer = null;
        this.scrollStack = [];             // 从列表进入详情后返回时恢复滚动位置
        this._rebuildStartTime = null;

        this.lightbox = null;
        this.pagination = null;
        this.contextMenu = null;
        this._initialized = false;
    }

    async init() {
        if (this._initialized) return;
        this._initialized = true;

        this.lightbox = createLightbox({ getImageUrl: (item) => item.url });
        this.pagination = createPagination(document.getElementById('pagination'), {
            onPageChange: (page) => this.loadImages(this.currentPath, page)
        });
        this.contextMenu = createContextMenu({
            items: [
                { label: '查看原图', action: 'view' },
                { label: '多选此图', action: 'select' },
                { label: '移动到此...', action: 'move' },
                { label: '删除', action: 'delete', danger: true }
            ],
            onSelect: (action) => this.handleContextAction(action)
        });

        this._bindUI();
        // 恢复作者页排序偏好
        try {
            this.albumSortBy = localStorage.getItem('iv.albumSortBy') || 'name';
            this.albumSortOrder = localStorage.getItem('iv.albumSortOrder') || 'asc';
        } catch (e) { }
        await this.loadSettings();
        await this.loadAlbums();
        this.loadExtensions();

        window.addEventListener('resize', () => {
            if (this.mode !== 'images' || !this.currentItems.length) return;
            const keyword = document.getElementById('iv-search').value.trim();
            if (keyword) {
                this.filterCurrentImages(keyword);
            } else {
                this.renderJustifiedLayout(this.currentItems);
            }
        });
    }

    async loadSettings() {
        try {
            this.currentSettings = await Bridge.call('get_settings', '');
            this.currentRowHeight = this.currentSettings.row_height || 200;
        } catch (e) { }
    }

    async loadExtensions() {
        const container = document.getElementById('iv-extensions');
        if (!container || typeof renderExtensions !== 'function') return;
        try {
            await renderExtensions(container, 'image-viewer', 'sidebar', {
                title: '相册清理',
                onEmbed: (ext) => this.openExtensionView(ext)
            });
            container.querySelectorAll('.obx-extension').forEach(btn => {
                btn.addEventListener('click', () => {
                    document.querySelectorAll('.iv-nav-item[data-view]').forEach(b => b.classList.remove('active'));
                    container.querySelectorAll('.obx-extension').forEach(b => b.classList.remove('active'));
                    btn.classList.add('active');
                });
            });
        } catch (e) {
            console.error('加载扩展入口失败:', e);
        }
    }

    openExtensionView(ext) {
        const view = document.getElementById('extension-view');
        const frame = document.getElementById('extension-frame');
        const title = document.getElementById('extension-view-title');
        if (!view || !frame) return;
        if (title) title.textContent = ext.label || '扩展';
        frame.src = ext.embedUrl || 'about:blank';
        view.classList.remove('hidden');
    }

    closeExtensionView() {
        const view = document.getElementById('extension-view');
        const frame = document.getElementById('extension-frame');
        if (!view || !frame) return;
        view.classList.add('hidden');
        frame.src = 'about:blank';
    }

    _bindUI() {
        document.querySelectorAll('.iv-nav-item[data-view]').forEach(btn => {
            btn.addEventListener('click', () => {
                this.closeExtensionView();
                this.currentView = btn.dataset.view;
                this.mode = 'albums';
                this.childParentPath = '';
                this.fromChildren = false;
                this.scrollStack = [];
                document.querySelectorAll('.iv-nav-item[data-view]').forEach(b => b.classList.toggle('active', b === btn));
                document.querySelectorAll('#iv-extensions .obx-extension').forEach(b => b.classList.remove('active'));
                this.showAlbums();
            });
        });

        document.getElementById('iv-back').addEventListener('click', () => this._handleBack());
        document.getElementById('btn-multi-select').addEventListener('click', () => this.toggleMultiSelectMode());
        document.getElementById('btn-delete-selected').addEventListener('click', () => this.deleteSelectedImages());
        document.getElementById('btn-move-selected').addEventListener('click', () => this.openMoveModal());
        document.getElementById('btn-refresh-thumbs').addEventListener('click', () => this.refreshSelectedThumbs());
        document.getElementById('btn-refresh').addEventListener('click', () => this.refreshView());
        document.getElementById('btn-rebuild').addEventListener('click', () => this.rebuildAll());
        const rebuildHide = document.getElementById('rebuild-progress-hide');
        if (rebuildHide) rebuildHide.addEventListener('click', () => this.hideRebuildProgress());
        const rebuildCancel = document.getElementById('rebuild-progress-cancel');
        if (rebuildCancel) rebuildCancel.addEventListener('click', () => this.cancelRebuild());
        document.getElementById('btn-slideshow').addEventListener('click', () => this.toggleSlideshow());
        document.getElementById('btn-settings').addEventListener('click', () => this.openSettingsModal());
        document.getElementById('settings-cancel').addEventListener('click', () => this.closeSettingsModal());
        document.getElementById('settings-save').addEventListener('click', () => this.saveSettings());
        document.getElementById('move-cancel').addEventListener('click', () => this.closeMoveModal());
        document.getElementById('move-confirm').addEventListener('click', () => this.confirmMove());
        document.getElementById('iv-new-album').addEventListener('click', () => this.openNewAlbumModal());
        document.getElementById('iv-new-album-cancel').addEventListener('click', () => this.closeNewAlbumModal());
        document.getElementById('iv-new-album-confirm').addEventListener('click', () => this.createAlbum());
        document.getElementById('extension-view-close').addEventListener('click', () => this.closeExtensionView());

        const search = document.getElementById('iv-search');
        search.addEventListener('input', Utils.debounce(() => {
            document.getElementById('iv-search-clear').classList.toggle('hidden', !search.value.trim());
            if (this.mode === 'images') {
                this.filterCurrentImages(search.value.trim());
            } else {
                this.showAlbums();
            }
        }, 250));
        document.getElementById('iv-search-clear').addEventListener('click', () => {
            search.value = '';
            document.getElementById('iv-search-clear').classList.add('hidden');
            if (this.mode === 'images') {
                this.filterCurrentImages('');
            } else {
                this.showAlbums();
            }
            search.focus();
        });

        // 作者页面排序栏
        document.getElementById('iv-album-sort-by').addEventListener('change', (e) => {
            this.albumSortBy = e.target.value;
            try { localStorage.setItem('iv.albumSortBy', this.albumSortBy); } catch (err) { }
            this.showAlbums();
        });
        document.getElementById('iv-album-sort-order').addEventListener('click', () => {
            this.albumSortOrder = this.albumSortOrder === 'asc' ? 'desc' : 'asc';
            try { localStorage.setItem('iv.albumSortOrder', this.albumSortOrder); } catch (err) { }
            this._syncAlbumSortBar();
            this.showAlbums();
        });

        document.getElementById('setting-row-height').addEventListener('input', (e) => {
            document.getElementById('setting-row-height-val').textContent = e.target.value;
        });

        const applyToFolder = document.getElementById('setting-apply-to-folder');
        if (applyToFolder) {
            applyToFolder.addEventListener('change', () => {
                document.getElementById('setting-root-dir').disabled = applyToFolder.checked;
            });
        }

        document.getElementById('image-grid').addEventListener('contextmenu', (e) => {
            const card = e.target.closest('.iv-image-card');
            if (!card || !card.dataset.url) return; // 相册卡片不参与图片右键菜单
            e.preventDefault();
            const imgUrl = card.dataset.url;
            if (!this.isMultiSelectMode || !this.selectedImages.has(imgUrl)) {
                this.clearSelection();
                this.toggleSelectImage(imgUrl, card);
            }
            this.contextMenu.show(e.clientX, e.clientY, { url: imgUrl });
        });

        createTree(document.getElementById('move-tree'), {
            data: [{ name: '根目录', path: '' }],
            onLoadChildren: async (path) => await Bridge.call('list_dir', path),
            onClick: (item) => { this.moveDestPath = item.path; }
        });

        document.querySelectorAll('.modal').forEach(modal => {
            modal.addEventListener('click', (e) => {
                if (e.target === modal) modal.classList.remove('active');
            });
        });
        document.addEventListener('click', () => this._closeAlbumMenu());
    }

    // ============================================================
    // 相册浏览
    // ============================================================
    async loadAlbums() {
        try {
            const result = await Bridge.call('list_albums');
            this.albums = (result && result.albums) || [];
            if (result && result.config) this.albumConfig = result.config;
        } catch (e) {
            this.albums = [];
        }
        this.showAlbums();
    }

    showAlbums() {
        this._stopSlideshow();
        this.mode = this.mode === 'images' ? 'albums' : this.mode;
        this.filteredSeqIndexes = null;
        if (this.isMultiSelectMode) {
            this.isMultiSelectMode = false;
            const multiBtn = document.getElementById('btn-multi-select');
            if (multiBtn) {
                multiBtn.classList.remove('active');
                multiBtn.textContent = '开启多选';
            }
            this.clearSelection();
        }
        document.getElementById('iv-search').placeholder = '搜索相册…';
        document.getElementById('iv-back').classList.toggle('hidden', this.mode !== 'children');
        document.getElementById('btn-slideshow').classList.add('hidden');
        document.getElementById('btn-multi-select').classList.add('hidden');
        document.getElementById('btn-delete-selected').classList.add('hidden');
        document.getElementById('btn-move-selected').classList.add('hidden');
        const selectionCount = document.getElementById('iv-selection-count');
        if (selectionCount) selectionCount.classList.add('hidden');
        document.getElementById('pagination').innerHTML = '';
        const imageGrid = document.getElementById('image-grid');
        imageGrid.innerHTML = '';
        imageGrid.style.height = '0';

        const keyword = document.getElementById('iv-search').value.trim().toLowerCase();
        let albums = this.albums;
        if (this.mode === 'children' && this.childParentPath) {
            albums = albums.filter(a => a.parent === this.childParentPath);
        } else {
            albums = this._filterVisibleAlbums(albums);
        }
        if (keyword) {
            albums = albums.filter(a =>
                a.name.toLowerCase().includes(keyword) || a.path.toLowerCase().includes(keyword));
        }

        // 作者页面（Pixiv 排序下的相册网格）排序栏 + 排序
        this._syncAlbumSortBar();
        if (this.currentView === 'albums') {
            albums = this._sortAlbums(albums);
        }

        const titleEl = document.getElementById('iv-view-title');
        const subEl = document.getElementById('iv-view-sub');
        const content = document.getElementById('iv-albums');
        content.innerHTML = '';

        if (this.mode === 'children') {
            titleEl.textContent = this.childParentPath.split('/').pop() || '未分类';
            subEl.textContent = `${albums.length} 个子相册`;
        } else if (this.currentView === 'timeline') {
            titleEl.textContent = '时间线';
            subEl.textContent = keyword ? `搜索 “${keyword}”` : '按添加时间归档';
        } else if (this.currentView === 'latest') {
            titleEl.textContent = '最近添加';
            subEl.textContent = keyword ? `搜索 “${keyword}”` : '最新更新的相册排在最前';
        } else {
            titleEl.textContent = '全部相册';
            subEl.textContent = keyword ? `搜索 “${keyword}”` : `${albums.length} 个相册`;
        }

        if (!albums.length) {
            content.innerHTML = this._emptyHtml('🖼️', '暂无相册', keyword ? '换个关键词试试' : '点击左侧「新建相册」开始整理');
            this._updateStats();
            return;
        }

        if (this.currentView === 'timeline' && this.mode === 'albums') {
            this._renderTimeline(content, albums);
        } else if (this.currentView === 'latest' && this.mode === 'albums') {
            this._renderAlbumCards(content, albums.slice(0, 60), { showTime: true });
        } else {
            this._renderAlbumCards(content, albums, {});
        }
        this._updateStats();
    }

    _renderTimeline(content, albums) {
        const groups = new Map();
        albums.forEach(a => {
            const key = this._monthKey(a.mtime);
            if (!groups.has(key)) groups.set(key, []);
            groups.get(key).push(a);
        });
        content.innerHTML = [...groups.entries()].map(([key, list], gi) => `
            <section class="iv-timeline-section">
                <div class="iv-time-label"><span>${key}</span><i>${list.length} 个相册</i></div>
                <div class="iv-grid" data-group="${gi}"></div>
            </section>`).join('');
        content.querySelectorAll('.iv-grid[data-group]').forEach((grid, gi) => {
            const list = [...groups.values()][gi];
            this._renderAlbumCards(grid, list, {});
        });
    }

    _renderAlbumCards(container, albums, opts = {}) {
        // 统一网格容器：container 不是 .iv-grid 时内部包一层，
        // 避免「全部相册 / 最近添加」中的卡片被拉伸成整行
        const target = container.classList.contains('iv-grid')
            ? container
            : this._ensureGrid(container);
        const collapsed = new Set(this.albumConfig.collapsed || []);
        const promoted = new Set(this.albumConfig.promoted || []);
        target.innerHTML = albums.map((album) => {
            const sub = album.path ? album.path : '根目录 · 未分类';
            const time = opts.showTime ? `<span class="iv-time-badge">${this._timeAgo(album.mtime)}</span>` : '';
            const badges = [];
            if (collapsed.has(album.path)) badges.push('<span class="iv-album-tag">📦 已收纳</span>');
            if (promoted.has(album.path)) badges.push('<span class="iv-album-tag iv-album-tag-hot">📌 已提升</span>');
            const menu = (album.depth >= 1 && album.path !== '')
                ? `<button class="iv-album-menu" data-path="${this._escapeAttr(album.path)}" title="相册设置">⋯</button>`
                : '';
            return `
            <div class="iv-album" data-path="${this._escapeAttr(album.path)}">
                <div class="iv-album-cover">
                    ${album.cover ? `<img src="${Bridge.thumbUrl(album.cover)}" loading="lazy" alt=""
                        onerror="if(!this.dataset.r){this.dataset.r='1';const u=new URL(this.src,location.origin);u.searchParams.set('r',Date.now());this.src=u.toString();}else{this.outerHTML='<div class=\'iv-cover-fallback\'>🖼️</div>';}">` : '<div class="iv-cover-fallback">🖼️</div>'}
                    <span class="iv-album-badge">${album.image_count} 张</span>
                    ${time}
                    ${badges.join('')}
                </div>
                <div class="iv-album-info">
                    <div class="iv-album-name">${this._escapeHtml(album.name)}</div>
                    <div class="iv-album-count">${this._escapeHtml(sub)}${album.has_children ? ' · 含子相册' : ''}</div>
                </div>
                ${menu}
            </div>`;
        }).join('');

        target.querySelectorAll('.iv-album').forEach(card => {
            card.addEventListener('click', (e) => {
                if (e.target.closest('.iv-album-menu')) return;
                this.openAlbum(card.dataset.path);
            });
        });
        target.querySelectorAll('.iv-album-menu').forEach(btn => {
            btn.addEventListener('click', (e) => {
                e.stopPropagation();
                this.showAlbumMenu(btn.dataset.path, e);
            });
        });
    }

    _filterVisibleAlbums(albums) {
        const collapsed = new Set(this.albumConfig.collapsed || []);
        const promoted = new Set(this.albumConfig.promoted || []);
        return albums.filter(a => {
            if (a.path === '' && a.direct_count === 0) return false; // 纯容器根目录不显示
            if (a.depth <= 1) return true;        // 顶层相册始终显示
            if (promoted.has(a.path)) return true; // 手动提升的相册浮到最外层
            const parts = a.path.split('/');
            for (let i = 1; i < parts.length; i++) {
                if (collapsed.has(parts.slice(0, i).join('/'))) return false;
            }
            return true;
        });
    }

    _ensureGrid(container) {
        let grid = container.querySelector(':scope > .iv-grid');
        if (!grid) {
            container.innerHTML = '';
            grid = document.createElement('div');
            grid.className = 'iv-grid';
            container.appendChild(grid);
        }
        return grid;
    }

    // ===== 作者页面排序（Pixiv 排序下的相册网格） =====

    _albumPageIsPixiv() {
        // 当前网格页面对应的文件夹（children → 父目录；albums → 根）是否生效 Pixiv 排序
        const parentPath = this.mode === 'children' ? this.childParentPath : '';
        const album = this.albums.find(a => a.path === parentPath);
        return !!(album && album.use_time_name);
    }

    _syncAlbumSortBar() {
        const bar = document.getElementById('iv-album-sort');
        if (!bar) return;
        const show = this.currentView === 'albums' && this._albumPageIsPixiv();
        bar.classList.toggle('hidden', !show);
        if (!show) return;
        document.getElementById('iv-album-sort-by').value = this.albumSortBy || 'name';
        document.getElementById('iv-album-sort-order').textContent =
            this.albumSortOrder === 'desc' ? '↓ 倒序' : '↑ 正序';
    }

    _sortAlbums(albums) {
        const by = this.albumSortBy || 'name';
        const dir = (this.albumSortOrder || 'asc') === 'desc' ? -1 : 1;
        const list = [...albums];
        if (by === 'mtime') {
            list.sort((a, b) => (a.mtime - b.mtime) * dir);
        } else if (by === 'count') {
            list.sort((a, b) => (a.image_count - b.image_count) * dir);
        } else {
            list.sort((a, b) => a.name.localeCompare(b.name, 'zh', { numeric: true }) * dir);
        }
        return list;
    }

    async openAlbum(path) {
        // 从列表/瀑布流点入时记录滚动位置，返回时恢复到刚刚浏览的位置
        if (this.mode === 'albums' || this.mode === 'children') {
            this._rememberScroll();
        }
        // 从瀑布流点入时记录当前视图状态，返回时原样恢复
        if (this.mode === 'images') {
            this.navStack.push({
                path: this.currentPath,
                fromChildren: this.fromChildren,
                childParentPath: this.childParentPath
            });
        }
        const album = this.albums.find(a => a.path === path);
        // 纯文件夹（只有子文件夹、没有直接图片）：
        // - Pixiv 排序的「配置点」（自身显式设置了 Pixiv 排序，如 pixiv 主文件夹）
        //   → 子相册网格（显示作者）；Pixiv 排序只考虑两层嵌套，配置点这层不做瀑布流。
        // - 仅继承 Pixiv 排序的子文件夹（作者层）→ 混合瀑布流（作品 p0 瓦片 + 多图连续浏览）。
        // - 其他排序 → 子相册网格。
        if (album && album.has_children && album.direct_count === 0) {
            let isPixiv = false;
            let pixivExplicit = false;
            try {
                const s = await Bridge.call('get_settings', path);
                isPixiv = !!s && s.sort_by === 'time_name';
                pixivExplicit = !!s && !!s.pixiv_explicit;
            } catch (e) { /* 忽略 */ }
            if (!isPixiv || pixivExplicit) {
                this.mode = 'children';
                this.childParentPath = path;
                this.fromChildren = true;
                this.currentPath = path;   // 同步当前浏览目录，保证设置基于当前目录
                this.showAlbums();
                return;
            }
        }
        this._stopSlideshow();
        this._showFolder(path);
    }

    _showFolder(path) {
        const album = this.albums.find(a => a.path === path);
        this.currentPath = path;
        this.currentPage = 1;
        this.mode = 'images';
        this.filteredSeqIndexes = null;
        document.getElementById('iv-search').value = '';
        document.getElementById('iv-search-clear').classList.add('hidden');
        document.getElementById('iv-search').placeholder = '搜索当前相册…';
        document.getElementById('iv-back').classList.remove('hidden');
        document.getElementById('btn-slideshow').classList.remove('hidden');
        document.getElementById('btn-multi-select').classList.remove('hidden');
        document.getElementById('iv-view-title').textContent = album ? album.name : (path.split('/').pop() || '未分类');
        document.getElementById('iv-view-sub').textContent = path || '根目录 · 未分类';
        document.getElementById('iv-albums').innerHTML = '';
        this.loadImages(path, 1);
    }

    _rememberScroll() {
        const content = document.getElementById('iv-content');
        if (content) this.scrollStack.push(content.scrollTop);
    }

    _restoreScroll() {
        const scrollTop = this.scrollStack.length ? this.scrollStack.pop() : null;
        if (scrollTop == null) return;
        const content = document.getElementById('iv-content');
        if (content) {
            // 等待渲染完成后再恢复，避免被浏览器重置到顶部
            requestAnimationFrame(() => {
                content.scrollTop = scrollTop || 0;
            });
        }
    }

    _popNavStack() {
        const prev = this.navStack.pop();
        if (!prev) return null;
        this.fromChildren = prev.fromChildren;
        this.childParentPath = prev.childParentPath;
        return prev.path;
    }

    _handleBack() {
        if (this.mode === 'images') {
            this._stopSlideshow();
            if (this.navStack.length) {
                const prev = this._popNavStack();
                if (prev === '') {
                    this.mode = 'albums';
                    this.childParentPath = '';
                    this.currentPath = '';
                    this.showAlbums();
                    this._restoreScroll();
                } else {
                    this._showFolder(prev);
                }
                return;
            }
            if (this.fromChildren && this.childParentPath) {
                this.mode = 'children';
                this.currentPath = this.childParentPath;
                this.showAlbums();
                this._restoreScroll();
            } else {
                this.mode = 'albums';
                this.childParentPath = '';
                this.currentPath = '';
                this.showAlbums();
                this._restoreScroll();
            }
        } else if (this.mode === 'children') {
            if (this.navStack.length) {
                const prev = this._popNavStack();
                if (prev === '') {
                    this.mode = 'albums';
                    this.childParentPath = '';
                    this.currentPath = '';
                    this.showAlbums();
                    this._restoreScroll();
                } else {
                    this._showFolder(prev);
                }
            } else {
                this.mode = 'albums';
                this.childParentPath = '';
                this.currentPath = '';
                this.showAlbums();
                this._restoreScroll();
            }
        }
    }

    showAlbumMenu(path, e) {
        this._closeAlbumMenu();
        const album = this.albums.find(a => a.path === path);
        if (!album) return;
        const collapsed = this.albumConfig.collapsed || [];
        const promoted = this.albumConfig.promoted || [];
        const items = [];
        if (album.depth === 1 && album.has_children) {
            items.push({
                label: collapsed.includes(path) ? '📂 展开子相册' : '📦 收纳子相册',
                action: collapsed.includes(path) ? 'expand' : 'collapse'
            });
        }
        if (album.depth > 1) {
            items.push({
                label: promoted.includes(path) ? '↩ 收回父相册' : '📌 提升到全部相册',
                action: promoted.includes(path) ? 'unpromote' : 'promote'
            });
        }
        items.push({
            label: '🖼 重建此相册缩略图',
            action: 'rebuild'
        });
        if (!items.length) return;
        const menuEl = document.createElement('div');
        menuEl.className = 'iv-context-menu';
        menuEl.innerHTML = items.map(it => `<button data-act="${it.action}">${it.label}</button>`).join('');
        menuEl.style.left = `${Math.min(e.clientX, window.innerWidth - 160)}px`;
        menuEl.style.top = `${Math.min(e.clientY, window.innerHeight - 90)}px`;
        document.body.appendChild(menuEl);
        this._albumMenuEl = menuEl;
        menuEl.addEventListener('click', async (ev) => {
            const act = ev.target.dataset.act;
            this._closeAlbumMenu();
            if (act === 'rebuild') {
                this.rebuildFolder(path);
                return;
            }
            try {
                const result = await Bridge.call('set_album_config', path, act);
                if (result && result.success) {
                    this.albumConfig = result.config;
                    Toast.success(act === 'collapse' ? '子相册已收纳' : act === 'expand' ? '子相册已展开' : act === 'promote' ? '已提升到全部相册' : '已收回父相册');
                    this.showAlbums();
                }
            } catch (err) {
                Toast.error('操作失败');
            }
        });
    }

    _closeAlbumMenu() {
        if (this._albumMenuEl) {
            this._albumMenuEl.remove();
            this._albumMenuEl = null;
        }
    }

    _updateStats() {
        const visible = this._filterVisibleAlbums(this.albums);
        const total = visible.reduce((sum, a) => sum + (a.direct_count || 0), 0);
        document.getElementById('iv-stats').textContent = `${visible.length} 个相册 · ${total} 张图片`;
    }

    // ============================================================
    // 新建相册
    // ============================================================
    openNewAlbumModal() {
        document.getElementById('iv-new-album-name').value = '';
        document.getElementById('new-album-modal').classList.add('active');
        setTimeout(() => document.getElementById('iv-new-album-name').focus(), 40);
    }

    closeNewAlbumModal() {
        document.getElementById('new-album-modal').classList.remove('active');
    }

    async createAlbum() {
        const name = document.getElementById('iv-new-album-name').value.trim();
        if (!name) {
            Toast.warning('请输入相册名称');
            return;
        }
        try {
            const result = await Bridge.call('create_folder', name);
            if (result.success) {
                this.closeNewAlbumModal();
                Toast.success(`相册「${name}」已创建`);
                await this.loadAlbums();
            } else {
                Toast.error(result.error || '创建失败');
            }
        } catch (e) {
            Toast.error('创建相册失败');
        }
    }

    // ============================================================
    // 图片网格
    // ============================================================
    async loadImages(path = '', page = 1) {
        const grid = document.getElementById('image-grid');
        const paginationEl = document.getElementById('pagination');
        grid.innerHTML = '<div class="loading">图片加载中…</div>';
        grid.style.height = 'auto';
        paginationEl.innerHTML = '';
        try {
            // 先取该文件夹生效的设置（含父文件夹/全局回退），保证 per-folder 设置首次进入即生效
            const eff = await Bridge.call('get_settings', path);
            if (eff) {
                this.currentSettings = eff;
                this.currentRowHeight = eff.row_height || 200;
            }
            const perPage = this.currentSettings.per_page || 40;
            const sortBy = this.currentSettings.sort_by || 'mtime';
            const sortOrder = this.currentSettings.sort_order || 'desc';
            const data = await Bridge.call('list_folder_items', path, page, perPage, sortBy, sortOrder);
            this.currentPage = page;
            this.currentItems = data.items || [];
            this.currentImages = this.currentItems.filter(it => it.type !== 'album');
            this.currentAllImages = data.all_images || this.currentImages;
            this.currentAllOffset = data.all_offset || 0;
            if (data.all_truncated) {
                Toast.warning('相册较大，连续浏览序列已截断（前 5000 张）');
            }
            if (data.settings) {
                this.currentSettings = data.settings;
                this.currentRowHeight = data.settings.row_height || 200;
            }
            const imgTotal = data.image_total != null ? data.image_total : this.currentImages.length;
            document.getElementById('iv-stats').textContent =
                (data.total === imgTotal) ? `共 ${data.total} 张图片` : `共 ${data.total} 项 · ${imgTotal} 张图片`;
            grid.innerHTML = '';
            this.filteredSeqIndexes = null;
            if (!this.currentItems.length) {
                grid.innerHTML = this._emptyHtml('🖼️', '此相册暂无图片');
                grid.style.height = 'auto';
                return;
            }
            const keyword = document.getElementById('iv-search').value.trim();
            if (keyword) {
                this.filterCurrentImages(keyword);
            } else {
                this.renderJustifiedLayout(this.currentItems);
            }
            this.pagination.render(data.page, Math.ceil(data.total / perPage));
        } catch (error) {
            grid.innerHTML = this._emptyHtml('⚠️', '图片加载失败');
            grid.style.height = 'auto';
        }
    }

    renderJustifiedLayout(items) {
        const grid = document.getElementById('image-grid');
        grid.innerHTML = '';
        const containerWidth = grid.clientWidth;
        if (containerWidth === 0 || !items.length) return;
        const gap = 5;
        const { cards, totalHeight } = JustifiedLayout.compute(items, containerWidth, this.currentRowHeight, gap);
        grid.style.height = `${totalHeight}px`;

        // 每个瓦片在连续浏览序列中的起始位置（子文件夹 → 其 p0，单图 → 自身）。
        // 分页时以 this.currentAllOffset 为基准：第 2+ 页的瓦片对应完整序列的中后段，
        // 否则点击会错位打开到序列开头的图片。
        // 搜索过滤时使用 filterCurrentImages 预计算的映射，保证灯箱仍从完整序列正确位置打开。
        // 注意：image_count 为直接图片数（容器为 0，不参与序列展开）。
        const seqIndex = new Map();
        if (this.filteredSeqIndexes && this.filteredSeqIndexes.size === items.length) {
            this.filteredSeqIndexes.forEach((value, key) => seqIndex.set(key, value));
        } else {
            let acc = this.currentAllOffset || 0;
            items.forEach((it, i) => {
                seqIndex.set(i, acc);
                acc += (it.type === 'album' ? (it.image_count || 0) : 1);
            });
        }

        // 圆圈数量角标：仅在该瓦片对应子文件夹自身生效的排序为「时间+文件名」时显示
        // （自己没设置则继承父级，后端 use_time_name 已算好）

        cards.forEach((cardData, index) => {
            const item = items[index];
            const card = document.createElement('div');
            card.className = 'iv-image-card';
            card.style.cssText = `left:${cardData.x}px;top:${cardData.y}px;width:${cardData.w}px;height:${cardData.h}px;`;

            if (item.type === 'album') {
                // 子文件夹直接用 p0 图片瓦片展示（不做文件夹卡片）
                card.dataset.path = item.path;
                const name = item.name;
                const isContainer = item.image_count === 0 && item.has_children;
                if (item.cover) {
                    const img = document.createElement('img');
                    img.src = Bridge.thumbUrl(item.cover);
                    img.loading = 'lazy';
                    img.alt = name;
                    img.onerror = function () {
                        if (!this.dataset.r) {
                            this.dataset.r = '1';
                            const u = new URL(this.src, location.origin);
                            u.searchParams.set('r', Date.now());
                            this.src = u.toString();
                        } else {
                            this.outerHTML = '<div class="iv-cover-fallback">🖼️</div>';
                        }
                    };
                    card.appendChild(img);
                } else {
                    const fb = document.createElement('div');
                    fb.className = 'iv-cover-fallback';
                    fb.textContent = '🖼️';
                    card.appendChild(fb);
                }
                if (item.use_time_name) {
                    const badge = document.createElement('span');
                    badge.className = 'iv-count-badge';
                    badge.textContent = item.total_count != null ? item.total_count : item.image_count;
                    card.appendChild(badge);
                }
                const p = document.createElement('p');
                p.textContent = name;
                card.appendChild(p);
                if (isContainer) {
                    // 纯容器（画师文件夹等）：点击进入其瀑布流（子作品 p0 瓦片）
                    card.addEventListener('click', () => this.openAlbum(item.path));
                } else {
                    // 作品文件夹：点击从 p0 打开灯箱，向右连续翻看该作品 p1 p2 … 及后续作品
                    card.addEventListener('click', () => {
                        this.lightbox.show(this.currentAllImages, seqIndex.get(index));
                    });
                }
            } else {
                // 单图卡片
                card.dataset.url = item.url;
                const img = document.createElement('img');
                img.src = Bridge.thumbUrl(item.url);
                img.loading = 'lazy';
                img.alt = item.url.split('/').pop();
                const p = document.createElement('p');
                p.textContent = item.url.split('/').pop();
                card.append(img, p);
                card.addEventListener('click', () => {
                    if (this.isMultiSelectMode) {
                        this.toggleSelectImage(item.url, card);
                    } else {
                        this.lightbox.show(this.currentAllImages, seqIndex.get(index));
                    }
                });
            }
            if (item.type !== 'album' && this.selectedImages.has(item.url)) {
                card.classList.add('selected');
            }
            grid.appendChild(card);
        });
    }

    filterCurrentImages(keyword = '') {
        const grid = document.getElementById('image-grid');
        const normalized = keyword.trim().toLowerCase();
        if (!normalized) {
            this.filteredSeqIndexes = null;
            if (this.currentItems.length) this.renderJustifiedLayout(this.currentItems);
            return;
        }

        const filtered = [];
        const seqIndexes = new Map();
        let acc = this.currentAllOffset || 0;
        this.currentItems.forEach((it, index) => {
            const haystack = [
                it.name || '',
                it.path || '',
                it.url ? it.url.split('/').pop() : ''
            ].join(' ').toLowerCase();
            if (haystack.includes(normalized)) {
                seqIndexes.set(filtered.length, acc);
                filtered.push(it);
            }
            acc += (it.type === 'album' ? (it.image_count || 0) : 1);
        });

        this.filteredSeqIndexes = seqIndexes;
        grid.innerHTML = '';
        grid.style.height = 'auto';
        if (!filtered.length) {
            grid.innerHTML = this._emptyHtml('🔍', '没有匹配的图片', '换个关键词试试');
            return;
        }
        this.renderJustifiedLayout(filtered);
    }

    // ===== 幻灯片 =====
    toggleSlideshow() {
        if (this.slideshowTimer) {
            this._stopSlideshow();
            return;
        }
        if (!this.currentAllImages.length) return;
        this.lightbox.show(this.currentAllImages, 0);
        this.slideshowTimer = setInterval(() => this.lightbox.navigate(1), 3000);
        document.getElementById('btn-slideshow').textContent = '⏸ 停止';
        Toast.info('幻灯片播放中，每 3 秒切换一张');
    }

    _stopSlideshow() {
        if (this.slideshowTimer) {
            clearInterval(this.slideshowTimer);
            this.slideshowTimer = null;
        }
        const btn = document.getElementById('btn-slideshow');
        if (btn) btn.textContent = '▶ 幻灯片';
    }

    // ============================================================
    // 多选 / 移动 / 删除
    // ============================================================
    toggleMultiSelectMode() {
        this.isMultiSelectMode = !this.isMultiSelectMode;
        const btn = document.getElementById('btn-multi-select');
        const deleteBtn = document.getElementById('btn-delete-selected');
        const moveBtn = document.getElementById('btn-move-selected');
        const thumbBtn = document.getElementById('btn-refresh-thumbs');
        btn.classList.toggle('active', this.isMultiSelectMode);
        btn.textContent = this.isMultiSelectMode ? '退出多选' : '开启多选';
        deleteBtn.classList.toggle('hidden', !this.isMultiSelectMode);
        moveBtn.classList.toggle('hidden', !this.isMultiSelectMode);
        if (thumbBtn) thumbBtn.classList.toggle('hidden', !this.isMultiSelectMode);
        const selectionCount = document.getElementById('iv-selection-count');
        if (selectionCount) selectionCount.classList.toggle('hidden', !this.isMultiSelectMode);
        if (!this.isMultiSelectMode) this.clearSelection();
    }

    _updateSelectionCount() {
        const el = document.getElementById('iv-selection-count');
        if (!el) return;
        el.textContent = `已选 ${this.selectedImages.size} 项`;
        el.classList.toggle('hidden', !this.isMultiSelectMode || this.selectedImages.size === 0);
    }

    toggleSelectImage(imgUrl, cardEl) {
        if (this.selectedImages.has(imgUrl)) {
            this.selectedImages.delete(imgUrl);
            cardEl.classList.remove('selected');
        } else {
            this.selectedImages.add(imgUrl);
            cardEl.classList.add('selected');
        }
        this._updateSelectionCount();
    }

    clearSelection() {
        this.selectedImages.clear();
        document.querySelectorAll('.iv-image-card.selected').forEach(el => el.classList.remove('selected'));
        this._updateSelectionCount();
    }

    handleContextAction(action) {
        const imgs = [...this.selectedImages];
        if (!imgs.length) return;
        if (action === 'view') {
            const idx = this.currentAllImages.findIndex(i => i.url === imgs[0]);
            this.lightbox.show(this.currentAllImages, Math.max(0, idx));
        } else if (action === 'select' && !this.isMultiSelectMode) {
            this.toggleMultiSelectMode();
        } else if (action === 'move') {
            this.openMoveModal();
        } else if (action === 'delete') {
            this.deleteSelectedImages();
        }
    }

    async deleteSelectedImages() {
        const imgs = [...this.selectedImages];
        if (!imgs.length) return;
        const ok = await confirmDialog(`确定删除 ${imgs.length} 张图片吗？`, { danger: true });
        if (!ok) return;
        try {
            const result = await Bridge.call('delete_files', imgs);
            if (result.errors.length) Toast.error(`部分删除失败: ${result.errors.join('; ')}`);
            else Toast.success(`已删除 ${imgs.length} 张图片`);
            this.clearSelection();
            await this.loadImages(this.currentPath, this.currentPage);
        } catch (e) {
            Toast.error('删除请求失败');
        }
    }

    openMoveModal() {
        if (!this.selectedImages.size) return;
        this.moveDestPath = '';
        document.getElementById('move-modal').classList.add('active');
    }

    closeMoveModal() {
        document.getElementById('move-modal').classList.remove('active');
    }

    async confirmMove() {
        if (!this.moveDestPath && this.moveDestPath !== '') {
            Toast.warning('请选择目标文件夹');
            return;
        }
        const imgs = [...this.selectedImages];
        if (!imgs.length) return;
        try {
            const result = await Bridge.call('move_files', imgs, this.moveDestPath);
            if (result.errors.length) Toast.error(`部分移动失败: ${result.errors.join('; ')}`);
            else Toast.success(`已移动 ${imgs.length} 张图片`);
            this.clearSelection();
            this.closeMoveModal();
            await this.loadImages(this.currentPath, this.currentPage);
        } catch (e) {
            Toast.error('移动请求失败');
        }
    }

    // ============================================================
    // 刷新 / 更新缩略图
    // ============================================================
    async refreshView() {
        let cacheCleared = false;
        try {
            await Bridge.call('refresh');
            cacheCleared = true;
        } catch (e) {
            // 后端 refresh API 不可用（旧版未重启）时降级：只重载视图，不清缓存
        }
        try {
            const result = await Bridge.call('list_albums');
            if (result && result.albums) this.albums = result.albums;
            if (result && result.config) this.albumConfig = result.config;
            if (this.mode === 'images') {
                this.loadImages(this.currentPath, this.currentPage);
            } else {
                this.showAlbums();
            }
            if (cacheCleared) {
                Toast.success('已刷新');
            } else {
                Toast.warning('已重新加载；完整刷新需重启应用（后端 refresh API 未生效）');
            }
        } catch (e) {
            Toast.error('刷新失败');
        }
    }

    async rebuildAll() {
        const ok = await confirmDialog('将清空旧缓存，并一次性生成全部缩略图。\n图片较多时可能需要较长时间，可以继续操作界面。确定继续吗？', { danger: true });
        if (!ok) return;
        await this._startRebuildTask(() => Bridge.call('rebuild_all', '', true), '全量重建');
    }

    async rebuildFolder(path) {
        const ok = await confirmDialog(`将重建「${path || '根目录'}」下的缩略图（跳过已有有效缓存），确定继续吗？`, { danger: true });
        if (!ok) return;
        await this._startRebuildTask(() => Bridge.call('rebuild_folder', path), '相册重建');
    }

    async _startRebuildTask(startCall, label = '全量重建') {
        const card = document.getElementById('rebuild-progress');
        if (card) card.classList.remove('hidden');
        this._rebuildStartTime = Date.now();
        this._updateRebuildProgress({ processed: 0, total: 0, current: '', errors: [], running: true });
        try {
            await startCall();
            const status = await this._waitRebuildDone();

            if (status.cancelled) {
                Toast.warning(`${label}已取消`);
                return;
            }

            const result = await Bridge.call('list_albums');
            if (result && result.albums) this.albums = result.albums;
            if (result && result.config) this.albumConfig = result.config;
            this.clearSelection();
            this.filteredSeqIndexes = null;
            if (this.mode === 'images') {
                this.loadImages(this.currentPath, 1);
            } else {
                this.showAlbums();
            }
            Toast.success(`${label}完成`);
        } catch (e) {
            Toast.error(`${label}失败：${e.message || e}`);
        } finally {
            if (card) card.classList.add('hidden');
            this._rebuildStartTime = null;
        }
    }

    async _waitRebuildDone() {
        // 轮询后端后台任务进度，直到完成
        while (true) {
            const status = await Bridge.call('rebuild_status');
            this._updateRebuildProgress(status);
            if (status.done) {
                if (status.cancelled) {
                    return status;
                }
                if (!status.success) {
                    const errors = status.errors || [];
                    throw new Error(errors.length ? `失败 ${errors.length} 个，示例：${errors.slice(0, 3).join('；')}` : '后台重建任务异常');
                }
                return status;
            }
            await new Promise(resolve => setTimeout(resolve, 500));
        }
    }

    _updateRebuildProgress(status = {}) {
        const total = status.total || 0;
        const processed = status.processed || 0;
        const text = document.getElementById('rebuild-progress-text');
        if (text) text.textContent = `${processed} / ${total}`;

        const bar = document.getElementById('rebuild-progress-bar');
        if (bar) {
            if (total > 0) {
                bar.style.width = `${Math.min(100, Math.round((processed / total) * 100))}%`;
                bar.style.animation = 'none';
            } else {
                bar.style.width = '40%';
                bar.style.animation = '';
            }
        }

        const currentEl = document.getElementById('rebuild-progress-current');
        if (currentEl) {
            currentEl.textContent = status.current ? `正在处理：${status.current}` : '正在扫描并生成缩略图，请稍候…';
        }

        const speedEl = document.getElementById('rebuild-progress-speed');
        if (speedEl && this._rebuildStartTime) {
            const elapsed = (Date.now() - this._rebuildStartTime) / 1000;
            if (elapsed > 0 && processed > 0) {
                const speed = processed / elapsed;
                const remaining = total > processed ? (total - processed) / speed : 0;
                speedEl.textContent = `${speed.toFixed(1)} 张/秒 · 剩余约 ${this._formatDuration(remaining)}`;
            } else {
                speedEl.textContent = '';
            }
        }

        const errorsEl = document.getElementById('rebuild-errors');
        if (errorsEl) {
            const errors = status.errors || [];
            errorsEl.textContent = errors.length ? `失败 ${errors.length} 个，示例：${errors.slice(0, 3).join('；')}` : '';
        }
    }

    hideRebuildProgress() {
        const card = document.getElementById('rebuild-progress');
        if (card) card.classList.add('hidden');
    }

    async cancelRebuild() {
        try {
            await Bridge.call('rebuild_cancel');
            Toast.info('正在取消全量重建…');
        } catch (e) {
            Toast.error('取消失败');
        }
    }

    async refreshSelectedThumbs() {
        const imgs = [...this.selectedImages];
        if (!imgs.length) return;
        const ok = await confirmDialog(`重新生成 ${imgs.length} 张图片的缩略图？\n（修复下载丢失/替换后残留的坏缩略图）`);
        if (!ok) return;
        try {
            const result = await Bridge.call('regenerate_thumbs', imgs);
            if (result.errors.length) Toast.error(`部分失败: ${result.errors.join('; ')}`);
            else Toast.success(`已重新生成 ${imgs.length} 张缩略图`);
            this.clearSelection();
            await this.loadImages(this.currentPath, this.currentPage);
        } catch (e) {
            Toast.error('更新缩略图失败');
        }
    }

    // ============================================================
    // 设置
    // ============================================================
    async openSettingsModal() {
        document.getElementById('settings-modal').classList.add('active');
        document.getElementById('setting-current-folder-name').textContent = this.currentPath || '根目录';
        const applyToFolder = document.getElementById('setting-apply-to-folder');
        if (applyToFolder) {
            // 每次打开默认保存到全局；勾选“仅当前文件夹”时才禁用根目录输入。
            // 根目录本身没有“文件夹级”概念，禁用该选项避免误导。
            applyToFolder.checked = false;
            applyToFolder.disabled = !this.currentPath;
            document.getElementById('setting-root-dir').disabled = false;
        }
        try {
            const s = await Bridge.call('get_settings', this.currentPath);
            document.getElementById('setting-row-height').value = s.row_height;
            document.getElementById('setting-row-height-val').textContent = s.row_height;
            document.getElementById('setting-per-page').value = s.per_page;
            document.getElementById('setting-sort-by').value = s.sort_by;
            document.getElementById('setting-sort-order').value = s.sort_order;
            const rootDir = await Bridge.call('get_root_dir');
            document.getElementById('setting-root-dir').value = rootDir || '';
        } catch (e) { }
    }

    closeSettingsModal() {
        document.getElementById('settings-modal').classList.remove('active');
    }

    async saveSettings() {
        const isFolderOnly = document.getElementById('setting-apply-to-folder').checked;
        const settings = {
            row_height: parseInt(document.getElementById('setting-row-height').value, 10),
            per_page: parseInt(document.getElementById('setting-per-page').value, 10),
            sort_by: document.getElementById('setting-sort-by').value,
            sort_order: document.getElementById('setting-sort-order').value
        };
        if (!isFolderOnly) {
            settings.root_dir = document.getElementById('setting-root-dir').value.trim() || undefined;
        }
        try {
            if (isFolderOnly) {
                await Bridge.call('save_settings', this.currentPath, settings);
            } else {
                await Bridge.call('save_settings', '', settings);
                if (this.currentPath) await Bridge.call('clear_folder_settings', this.currentPath);
            }
            this.currentSettings = settings;
            this.currentRowHeight = settings.row_height;
            this.closeSettingsModal();
            Toast.success('设置已保存');
            if (this.mode === 'images') this.loadImages(this.currentPath, 1);
        } catch (e) {
            Toast.error('保存设置失败');
        }
    }

    // ===== 工具 =====
    _formatDuration(seconds) {
        if (!isFinite(seconds) || seconds <= 0) return '--';
        seconds = Math.round(seconds);
        const h = Math.floor(seconds / 3600);
        const m = Math.floor((seconds % 3600) / 60);
        const s = seconds % 60;
        if (h > 0) return `${h} 小时 ${m} 分`;
        if (m > 0) return `${m} 分 ${s} 秒`;
        return `${s} 秒`;
    }

    _monthKey(mtime) {
        const d = new Date((mtime || 0) * 1000);
        return `${d.getFullYear()} 年 ${d.getMonth() + 1} 月`;
    }

    _timeAgo(mtime) {
        if (!mtime) return '';
        const diff = Date.now() / 1000 - mtime;
        if (diff < 3600) return `${Math.max(1, Math.floor(diff / 60))} 分钟前`;
        if (diff < 86400) return `${Math.floor(diff / 3600)} 小时前`;
        if (diff < 2592000) return `${Math.floor(diff / 86400)} 天前`;
        const d = new Date(mtime * 1000);
        return `${d.getFullYear()}-${String(d.getMonth() + 1).padStart(2, '0')}-${String(d.getDate()).padStart(2, '0')}`;
    }

    _emptyHtml(icon, text, hint) {
        return `<div class="iv-empty">
            <div class="iv-empty-icon">${icon}</div>
            <div class="iv-empty-text">${this._escapeHtml(text)}</div>
            ${hint ? `<div class="iv-empty-hint">${this._escapeHtml(hint)}</div>` : ''}
        </div>`;
    }

    _escapeHtml(str) {
        const div = document.createElement('div');
        div.textContent = str == null ? '' : String(str);
        return div.innerHTML;
    }

    _escapeAttr(str) {
        return this._escapeHtml(str).replace(/"/g, '&quot;');
    }
}
