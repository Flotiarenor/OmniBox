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
        this.currentImages = [];
        this.currentSettings = {};
        this.currentRowHeight = 200;
        this.albums = [];
        this.albumConfig = { collapsed: [], promoted: [] };
        this.isMultiSelectMode = false;
        this.selectedImages = new Set();
        this.moveDestPath = '';
        this.slideshowTimer = null;
        this.cleanupMode = 'dupe';
        this.cleanupSelected = new Set();

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
        await this.loadSettings();
        await this.loadAlbums();

        window.addEventListener('resize', () => {
            if (this.mode === 'images' && this.currentImages.length) {
                this.renderJustifiedLayout(this.currentImages);
            }
        });
    }

    async loadSettings() {
        try {
            this.currentSettings = await Bridge.call('get_settings', '');
            this.currentRowHeight = this.currentSettings.row_height || 200;
        } catch (e) { }
    }

    _bindUI() {
        document.querySelectorAll('.iv-nav-item[data-view]').forEach(btn => {
            btn.addEventListener('click', () => {
                this.currentView = btn.dataset.view;
                this.mode = 'albums';
                this.childParentPath = '';
                this.fromChildren = false;
                document.querySelectorAll('.iv-nav-item[data-view]').forEach(b => b.classList.toggle('active', b === btn));
                this.showAlbums();
            });
        });

        document.getElementById('iv-back').addEventListener('click', () => this._handleBack());
        document.getElementById('btn-multi-select').addEventListener('click', () => this.toggleMultiSelectMode());
        document.getElementById('btn-delete-selected').addEventListener('click', () => this.deleteSelectedImages());
        document.getElementById('btn-move-selected').addEventListener('click', () => this.openMoveModal());
        document.getElementById('btn-slideshow').addEventListener('click', () => this.toggleSlideshow());
        document.getElementById('btn-cleanup').addEventListener('click', () => this.openCleanup());
        document.getElementById('cleanup-close').addEventListener('click', () => this.closeCleanup());
        document.getElementById('cleanup-delete').addEventListener('click', () => this.deleteCleanupSelected());
        document.getElementById('cleanup-tab-dupe').addEventListener('click', () => this.switchCleanupMode('dupe'));
        document.getElementById('cleanup-tab-similar').addEventListener('click', () => this.switchCleanupMode('similar'));
        document.getElementById('btn-settings').addEventListener('click', () => this.openSettingsModal());
        document.getElementById('settings-cancel').addEventListener('click', () => this.closeSettingsModal());
        document.getElementById('settings-save').addEventListener('click', () => this.saveSettings());
        document.getElementById('move-cancel').addEventListener('click', () => this.closeMoveModal());
        document.getElementById('move-confirm').addEventListener('click', () => this.confirmMove());
        document.getElementById('iv-new-album').addEventListener('click', () => this.openNewAlbumModal());
        document.getElementById('iv-new-album-cancel').addEventListener('click', () => this.closeNewAlbumModal());
        document.getElementById('iv-new-album-confirm').addEventListener('click', () => this.createAlbum());

        const search = document.getElementById('iv-search');
        search.addEventListener('input', Utils.debounce(() => {
            document.getElementById('iv-search-clear').classList.toggle('hidden', !search.value.trim());
            this.showAlbums();
        }, 250));
        document.getElementById('iv-search-clear').addEventListener('click', () => {
            search.value = '';
            document.getElementById('iv-search-clear').classList.add('hidden');
            this.showAlbums();
            search.focus();
        });

        document.getElementById('setting-row-height').addEventListener('input', (e) => {
            document.getElementById('setting-row-height-val').textContent = e.target.value;
        });

        document.getElementById('image-grid').addEventListener('contextmenu', (e) => {
            const card = e.target.closest('.iv-image-card');
            if (!card) return;
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
        document.getElementById('iv-back').classList.toggle('hidden', this.mode !== 'children');
        document.getElementById('btn-slideshow').classList.add('hidden');
        document.getElementById('btn-multi-select').classList.add('hidden');
        document.getElementById('btn-delete-selected').classList.add('hidden');
        document.getElementById('btn-move-selected').classList.add('hidden');
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

    openAlbum(path) {
        const album = this.albums.find(a => a.path === path);
        // 只有子文件夹、没有直接图片的相册 → 先展示其子相册
        if (album && album.has_children && album.direct_count === 0) {
            this.mode = 'children';
            this.childParentPath = path;
            this.fromChildren = true;
            this.showAlbums();
            return;
        }
        this.currentPath = path;
        this.currentPage = 1;
        this.mode = 'images';
        document.getElementById('iv-back').classList.remove('hidden');
        document.getElementById('btn-slideshow').classList.remove('hidden');
        document.getElementById('btn-cleanup').classList.remove('hidden');
        document.getElementById('btn-multi-select').classList.remove('hidden');
        document.getElementById('iv-view-title').textContent = album ? album.name : (path || '未分类');
        document.getElementById('iv-view-sub').textContent = path || '根目录 · 未分类';
        document.getElementById('iv-albums').innerHTML = '';
        this.loadImages(path, 1);
    }

    _handleBack() {
        if (this.mode === 'images') {
            this._stopSlideshow();
            if (this.fromChildren && this.childParentPath) {
                this.mode = 'children';
                this.showAlbums();
            } else {
                this.mode = 'albums';
                this.childParentPath = '';
                this.showAlbums();
            }
        } else if (this.mode === 'children') {
            this.mode = 'albums';
            this.childParentPath = '';
            this.showAlbums();
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
            const perPage = this.currentSettings.per_page || 40;
            const sortBy = this.currentSettings.sort_by || 'mtime';
            const sortOrder = this.currentSettings.sort_order || 'desc';
            const data = await Bridge.call('list_images', path, page, perPage, sortBy, sortOrder);
            this.currentPage = page;
            this.currentImages = data.images;
            if (data.settings) {
                this.currentSettings = data.settings;
                this.currentRowHeight = data.settings.row_height || 200;
            }
            document.getElementById('iv-stats').textContent = `共 ${data.total} 张图片`;
            grid.innerHTML = '';
            if (!data.images.length) {
                grid.innerHTML = this._emptyHtml('🖼️', '此相册暂无图片');
                grid.style.height = 'auto';
                return;
            }
            this.renderJustifiedLayout(data.images);
            this.pagination.render(data.page, Math.ceil(data.total / perPage));
        } catch (error) {
            grid.innerHTML = this._emptyHtml('⚠️', '图片加载失败');
            grid.style.height = 'auto';
        }
    }

    renderJustifiedLayout(images) {
        const grid = document.getElementById('image-grid');
        grid.innerHTML = '';
        const containerWidth = grid.clientWidth;
        if (containerWidth === 0 || !images.length) return;
        const gap = 5;
        const { cards, totalHeight } = JustifiedLayout.compute(images, containerWidth, this.currentRowHeight, gap);
        grid.style.height = `${totalHeight}px`;
        cards.forEach((cardData, index) => {
            const card = document.createElement('div');
            card.className = 'iv-image-card';
            card.dataset.url = cardData.url;
            card.style.cssText = `left:${cardData.x}px;top:${cardData.y}px;width:${cardData.w}px;height:${cardData.h}px;`;
            const img = document.createElement('img');
            img.src = Bridge.thumbUrl(cardData.url);
            img.loading = 'lazy';
            img.alt = cardData.url.split('/').pop();
            const p = document.createElement('p');
            p.textContent = cardData.url.split('/').pop();
            card.append(img, p);
            grid.appendChild(card);
            card.addEventListener('click', () => {
                if (this.isMultiSelectMode) {
                    this.toggleSelectImage(cardData.url, card);
                } else {
                    this.lightbox.show(this.currentImages, index);
                }
            });
        });
    }

    // ===== 幻灯片 =====
    toggleSlideshow() {
        if (this.slideshowTimer) {
            this._stopSlideshow();
            return;
        }
        if (!this.currentImages.length) return;
        this.lightbox.show(this.currentImages, 0);
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
    // 清理：完全重复 / 相似图片
    // ============================================================
    openCleanup() {
        document.getElementById('cleanup-modal').classList.add('active');
        this.cleanupSelected.clear();
        this.runCleanup();
    }

    closeCleanup() {
        document.getElementById('cleanup-modal').classList.remove('active');
    }

    switchCleanupMode(mode) {
        this.cleanupMode = mode;
        document.getElementById('cleanup-tab-dupe').classList.toggle('active', mode === 'dupe');
        document.getElementById('cleanup-tab-similar').classList.toggle('active', mode === 'similar');
        this.cleanupSelected.clear();
        this.runCleanup();
    }

    async runCleanup() {
        const box = document.getElementById('cleanup-results');
        box.innerHTML = '<div class="loading">扫描中…请稍候</div>';
        document.getElementById('cleanup-selected').textContent = '已选 0 张';
        document.getElementById('cleanup-scanned').textContent = '';
        try {
            const method = this.cleanupMode === 'dupe' ? 'duplicate_scan' : 'similar_scan';
            const result = await Bridge.call(method, this.currentPath);
            this.renderCleanupGroups(result.groups || [], result.scanned || 0);
        } catch (e) {
            box.innerHTML = `<div class="iv-empty"><div class="iv-empty-icon">⚠️</div><div class="iv-empty-text">扫描失败</div></div>`;
        }
    }

    renderCleanupGroups(groups, scanned) {
        const box = document.getElementById('cleanup-results');
        document.getElementById('cleanup-scanned').textContent = `已扫描 ${scanned} 张`;
        if (!groups.length) {
            box.innerHTML = `<div class="iv-empty"><div class="iv-empty-icon">✨</div><div class="iv-empty-text">未发现${this.cleanupMode === 'dupe' ? '完全重复' : '相似'}图片</div></div>`;
            return;
        }
        box.innerHTML = groups.map((group, gi) => `
            <div class="iv-cleanup-group">
                <div class="iv-cleanup-group-head">
                    <span>${this.cleanupMode === 'dupe' ? `重复组 · ${group.files.length} 张 · ${(group.size / 1024 / 1024).toFixed(2)} MB` : `相似组 · ${group.files.length} 张`}</span>
                    <button class="btn btn-sm" data-select-group="${gi}">全选组</button>
                </div>
                <div class="iv-cleanup-files">
                    ${group.files.map(f => `
                        <label class="iv-cleanup-file">
                            <input type="checkbox" data-file="${this._escapeAttr(f)}">
                            <img src="${Bridge.thumbUrl(f)}" loading="lazy" alt="" onerror="this.style.display='none'">
                            <span>${this._escapeHtml(f.split('/').pop())}</span>
                        </label>`).join('')}
                </div>
            </div>`).join('');

        box.querySelectorAll('[data-select-group]').forEach(btn => {
            btn.addEventListener('click', () => {
                const idx = parseInt(btn.dataset.selectGroup, 10);
                const group = groups[idx];
                const checkboxes = box.querySelectorAll(`input[type="checkbox"][data-file]`);
                const inGroup = new Set(group.files);
                checkboxes.forEach(cb => {
                    if (inGroup.has(cb.dataset.file)) {
                        cb.checked = true;
                        this.cleanupSelected.add(cb.dataset.file);
                    }
                });
                this.updateCleanupSelected();
            });
        });

        box.querySelectorAll('input[type="checkbox"][data-file]').forEach(cb => {
            cb.addEventListener('change', () => {
                if (cb.checked) this.cleanupSelected.add(cb.dataset.file);
                else this.cleanupSelected.delete(cb.dataset.file);
                this.updateCleanupSelected();
            });
        });
    }

    updateCleanupSelected() {
        document.getElementById('cleanup-selected').textContent = `已选 ${this.cleanupSelected.size} 张`;
    }

    async deleteCleanupSelected() {
        const files = [...this.cleanupSelected];
        if (!files.length) {
            Toast.warning('请先勾选要删除的图片');
            return;
        }
        const ok = await confirmDialog(`确定删除选中的 ${files.length} 张图片吗？`, { danger: true });
        if (!ok) return;
        try {
            const result = await Bridge.call('delete_files', files);
            if (result.errors.length) Toast.error(`部分删除失败: ${result.errors.join('; ')}`);
            else Toast.success(`已删除 ${files.length} 张图片`);
            this.cleanupSelected.clear();
            if (this.mode === 'images') await this.loadImages(this.currentPath, this.currentPage);
            await this.runCleanup();
        } catch (e) {
            Toast.error('删除请求失败');
        }
    }

    // ============================================================
    // 多选 / 移动 / 删除
    // ============================================================
    toggleMultiSelectMode() {
        this.isMultiSelectMode = !this.isMultiSelectMode;
        const btn = document.getElementById('btn-multi-select');
        const deleteBtn = document.getElementById('btn-delete-selected');
        const moveBtn = document.getElementById('btn-move-selected');
        btn.classList.toggle('active', this.isMultiSelectMode);
        btn.textContent = this.isMultiSelectMode ? '退出多选' : '开启多选';
        deleteBtn.classList.toggle('hidden', !this.isMultiSelectMode);
        moveBtn.classList.toggle('hidden', !this.isMultiSelectMode);
        if (!this.isMultiSelectMode) this.clearSelection();
    }

    toggleSelectImage(imgUrl, cardEl) {
        if (this.selectedImages.has(imgUrl)) {
            this.selectedImages.delete(imgUrl);
            cardEl.classList.remove('selected');
        } else {
            this.selectedImages.add(imgUrl);
            cardEl.classList.add('selected');
        }
    }

    clearSelection() {
        this.selectedImages.clear();
        document.querySelectorAll('.iv-image-card.selected').forEach(el => el.classList.remove('selected'));
    }

    handleContextAction(action) {
        const imgs = [...this.selectedImages];
        if (!imgs.length) return;
        if (action === 'view') {
            const idx = this.currentImages.findIndex(i => i.url === imgs[0]);
            this.lightbox.show(this.currentImages, idx);
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
            this.selectedImages.clear();
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
            this.selectedImages.clear();
            this.closeMoveModal();
            await this.loadImages(this.currentPath, this.currentPage);
        } catch (e) {
            Toast.error('移动请求失败');
        }
    }

    // ============================================================
    // 设置
    // ============================================================
    async openSettingsModal() {
        document.getElementById('settings-modal').classList.add('active');
        document.getElementById('setting-current-folder-name').textContent = this.currentPath || '根目录';
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
            sort_order: document.getElementById('setting-sort-order').value,
            root_dir: document.getElementById('setting-root-dir').value.trim() || undefined
        };
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
