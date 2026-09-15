// ============================================================
// OmniBox 图片相册 — 相册树浏览、渲染与排序
//
// 本文件是 ImageViewer.prototype 的一个分片：类骨架与构造函数在 app.js，
// 这里用 Object.assign 把方法扩回同一个原型。**加载顺序是硬约束** —— 必须在
// app.js 之后、实例化之前（见 index.html），契约由 tests/js/image_viewer_app_split.mjs 把关。
// ============================================================

Object.assign(ImageViewer.prototype, {


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
    },

    showAlbums() {
        this._cancelSlideshow();
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

        // 作者网格二次排序：只在生效 Pixiv 排序的页面应用（与设置页显示该组选项的
        // 条件一致）；子相册网格与普通相册页保持原有行为（文件名正序）
        const isPixivGrid = this._gridIsPixiv();
        if (this.mode === 'children' || this.currentView === 'albums') {
            albums = isPixivGrid
                ? this._sortAlbums(albums)
                : this._sortAlbums(albums, 'name', 'asc');
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
    },

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
    },

    _renderAlbumCards(container, albums, opts = {}) {
        // 统一网格容器：container 不是 .iv-grid 时内部包一层，
        // 避免「全部相册 / 最近添加」中的卡片被拉伸成整行
        const target = container.classList.contains('iv-grid')
            ? container
            : this._ensureGrid(container);
        const collapsed = new Set(this.albumConfig.collapsed || []);
        const promoted = new Set(this.albumConfig.promoted || []);
        const visibleEmpty = new Set(this.albumConfig.visible_empty_dirs || []);
        target.innerHTML = albums.map((album) => {
            const sub = album.path ? album.path : '根目录 · 未分类';
            const time = opts.showTime ? `<span class="iv-time-badge">${this._timeAgo(album.mtime)}</span>` : '';
            const badges = [];
            if (collapsed.has(album.path)) badges.push('<span class="iv-album-tag">📦 已收纳</span>');
            if (promoted.has(album.path)) badges.push('<span class="iv-album-tag iv-album-tag-hot">📌 已提升</span>');
            // 空目录默认不显示，显示出来的都是「新建相册」保留可见的
            if (visibleEmpty.has(album.path) && !album.readable) {
                badges.push('<span class="iv-album-tag">📁 空相册</span>');
            }
            const menu = (album.depth >= 1 && album.path !== '')
                ? `<button class="iv-album-menu" data-path="${this._escapeAttr(album.path)}" title="相册设置">⋯</button>`
                : '';
            // 含子相册的目录显示递归总数（后端 readable），纯图片目录显示直接图片数
            const total = album.readable != null ? album.readable : album.image_count;
            const countText = album.has_children && total !== album.direct_count
                ? `${total} 张 · 含子相册`
                : `${total} 张`;
            return `
            <div class="iv-album" data-path="${this._escapeAttr(album.path)}">
                <div class="iv-album-cover">
                    ${album.cover ? `<img src="${Bridge.thumbUrl(album.cover)}" loading="lazy" alt=""
                        onerror="if(!this.dataset.r){this.dataset.r='1';const u=new URL(this.src,location.origin);u.searchParams.set('r',Date.now());this.src=u.toString();}else{this.outerHTML='<div class=\'iv-cover-fallback\'>🖼️</div>';}">` : '<div class="iv-cover-fallback">🖼️</div>'}
                    <span class="iv-album-badge">${countText}</span>
                    ${time}
                    ${badges.join('')}
                </div>
                <div class="iv-album-info">
                    <div class="iv-album-name">${this._escapeHtml(album.name)}</div>
                    <div class="iv-album-count">${this._escapeHtml(sub)}</div>
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
    },

    _isCollapsed(album, config = this.albumConfig) {
        // 子相册默认折叠：只有被显式「展开」过的目录才展示下级，
        // 「收纳子相册」是显式撤销展开（兼容旧配置里的 collapsed 记录）
        const path = album.path;
        if (new Set((config && config.collapsed) || []).has(path)) return true;
        const expanded = (config && config.expanded) || [];
        return (album.has_children || false) && !expanded.includes(path);
    },

    _filterVisibleAlbums(albums) {
        const promoted = new Set(this.albumConfig.promoted || []);
        const visibleEmpty = new Set(this.albumConfig.visible_empty_dirs || []);
        const byPath = new Map(albums.map(a => [a.path, a]));
        // 「空目录」保留可见：自身标记，或它的**下级**有标记。「新建相册」只标记
        // 新建的那一层，但用户也可能只给深层目录打标记，上级必须跟着显示，
        // 否则那层永远点不进去。
        const emptyVisible = new Map();
        const isEmptyVisible = (path) => {
            if (visibleEmpty.has(path)) return true;
            if (emptyVisible.has(path)) return emptyVisible.get(path);
            emptyVisible.set(path, false);            // 防御环形数据
            let found = false;
            for (const other of visibleEmpty) {
                if (other.startsWith(`${path}/`)) {
                    found = true;
                    break;
                }
            }
            emptyVisible.set(path, found);
            return found;
        };
        // 空目录 = 递归（含下级）都没有可读图片，后端聚合出的 readable 已经算好，
        // 这里只做单层判断：某层为空，它下面必然全空，整棵一起隐藏
        const knownEmpty = (path) => {
            if (isEmptyVisible(path)) return false;
            const album = byPath.get(path);
            if (!album) return false;
            return (album.readable != null ? album.readable : album.image_count) === 0;
        };
        const hiddenEmptyCache = new Map();
        const hiddenEmpty = (path) => {
            if (hiddenEmptyCache.has(path)) return hiddenEmptyCache.get(path);
            const parts = (path || '').split('/').filter(Boolean);
            const branch = [];
            for (let i = 1; i <= parts.length; i++) branch.push(parts.slice(0, i).join('/'));
            const hidden = branch.some(knownEmpty);
            hiddenEmptyCache.set(path, hidden);
            return hidden;
        };
        return albums.filter(a => {
            if (a.path === '' && a.direct_count === 0) return false; // 纯容器根目录不显示
            if (hiddenEmpty(a.path)) return false;
            if (a.depth <= 1) return true;        // 顶层相册始终显示
            if (promoted.has(a.path)) return true; // 手动提升的相册浮到最外层
            const parts = a.path.split('/');
            for (let i = 1; i < parts.length; i++) {
                const ancestor = byPath.get(parts.slice(0, i).join('/'));
                if (ancestor && this._isCollapsed(ancestor)) return false;
            }
            return true;
        });
    },

    _ensureGrid(container) {
        let grid = container.querySelector(':scope > .iv-grid');
        if (!grid) {
            container.innerHTML = '';
            grid = document.createElement('div');
            grid.className = 'iv-grid';
            container.appendChild(grid);
        }
        return grid;
    },

    // ===== 作者网格二次排序（Pixiv 排序下的相册网格） =====

    _gridConfigPath() {
        // 当前网格页面作为「配置点」的目录：children 视图是进入的那个文件夹，
        // 其他视图是相册树的合成根目录（第一根 + 各命名空间根）
        return this.mode === 'children' ? this.childParentPath : '';
    },

    _gridIsPixiv() {
        // 该页面是否按 Pixiv 树展示：配置点自己生效 Pixiv 排序（如额外根目录
        // 的命名空间节点，它本身不存设置、恒从全局继承），或者它下面有目录
        // 生效 Pixiv 排序（如根目录下的作者目录被单独配置过）。
        const configPath = this._gridConfigPath();
        const own = this.albums.find(a => a.path === configPath);
        if (own && own.use_time_name) return true;
        const prefix = configPath ? `${configPath}/` : '';
        return this.albums.some(a =>
            a.path !== configPath && a.path.startsWith(prefix) && a.use_time_name);
    },

    _albumPageIsPixiv() {
        return this._gridIsPixiv();
    },

    // by / order 缺省取设置项（album_sort_by / album_sort_order）
    _sortAlbums(albums, by = this.albumSortBy, order = this.albumSortOrder) {
        const key = by || 'mtime';
        const dir = (order || 'desc') === 'desc' ? -1 : 1;
        const list = [...albums];
        if (key === 'mtime') {
            list.sort((a, b) => (a.mtime - b.mtime) * dir);
        } else if (key === 'count') {
            list.sort((a, b) => (a.image_count - b.image_count) * dir);
        } else {
            list.sort((a, b) => a.name.localeCompare(b.name, 'zh', { numeric: true }) * dir);
        }
        return list;
    },

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
        // - Pixiv 排序的「配置点」（自身显式设置了 Pixiv 排序 / 模糊匹配，或
        //   第一根、额外根目录的命名空间节点）→ 子相册网格（显示作者）；
        //   Pixiv 排序只考虑两层嵌套，配置点这层不做瀑布流。
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
            if (!isPixiv || pixivExplicit || album.root_scope) {
                this.mode = 'children';
                this.childParentPath = path;
                this.fromChildren = true;
                this.currentPath = path;   // 同步当前浏览目录，保证设置基于当前目录
                this.showAlbums();
                return;
            }
        }
        this._cancelSlideshow();
        this._showFolder(path);
    },

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
    },

    _rememberScroll() {
        const content = document.getElementById('iv-content');
        if (content) this.scrollStack.push(content.scrollTop);
    },

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
    },

    _popNavStack() {
        const prev = this.navStack.pop();
        if (!prev) return null;
        this.fromChildren = prev.fromChildren;
        this.childParentPath = prev.childParentPath;
        return prev.path;
    },
});
