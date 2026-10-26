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
        // 只刷新相册数据，不改变当前层级：`showAlbums()` 会把 mode 强制切回 albums，
        // 并清空当前目录与图片列表；在图片层点「校验」或保存设置时调用本方法，
        // 用户会被弹回全部相册，随后的 `loadImages` 也因 mode 已变而永不执行。
        if (this.mode === 'images') return;
        this.showAlbums();
    },

    showAlbums() {
        // 新导航代次：在途 loadImages 的响应从这一刻起作废，图片层状态也一并清空，
        // 避免旧请求把已切走的瀑布流重新画回相册层。
        this._navSeq += 1;
        this._resetImageListState();
        this._cancelSlideshow();
        this.mode = this.mode === 'images' ? 'albums' : this.mode;
        this.filteredSeqIndexes = null;
        if (this.isMultiSelectMode) {
            this.isMultiSelectMode = false;
            this.clearSelection();
        }
        document.getElementById('pagination').innerHTML = '';
        const imageGrid = document.getElementById('image-grid');
        imageGrid.innerHTML = '';
        imageGrid.style.height = '0';

        const keyword = this.searchQuery.trim().toLowerCase();
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

        const content = document.getElementById('iv-albums');
        content.innerHTML = '';

        if (this.mode === 'children') {
            this._chromeTitle = this.childParentPath.split('/').pop() || '未分类';
            this._chromeSub = `${albums.length} 个子相册`;
        } else if (this.currentView === 'timeline') {
            this._chromeTitle = '时间线';
            this._chromeSub = keyword ? `搜索 “${this.searchQuery}”` : '按添加时间归档';
        } else if (this.currentView === 'latest') {
            this._chromeTitle = '最近添加';
            this._chromeSub = keyword ? `搜索 “${this.searchQuery}”` : '最新更新的相册排在最前';
        } else {
            this._chromeTitle = '全部相册';
            this._chromeSub = keyword ? `搜索 “${this.searchQuery}”` : `${albums.length} 个相册`;
        }

        if (!albums.length) {
            content.innerHTML = this._emptyHtml('icon:images', '暂无相册', keyword ? '换个关键词试试' : '在顶栏点「新建相册」开始整理');
            this._updateStats();
            this._pushChrome();
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
        this._pushChrome();
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
                <div class="iv-grid cui-grid" data-group="${gi}"></div>
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
        const sets = {
            collapsed: new Set(this.albumConfig.collapsed || []),
            promoted: new Set(this.albumConfig.promoted || []),
            visibleEmpty: new Set(this.albumConfig.visible_empty_dirs || []),
        };
        const fragment = document.createDocumentFragment();
        albums.forEach((album) => fragment.appendChild(this._albumCard(album, opts, sets)));
        target.replaceChildren(fragment);
    },

    /** 单张相册卡：结构/键盘激活/操作菜单/封面叠加都由 ContentUI.card 提供。 */
    _albumCard(album, opts, sets) {
        const sub = album.path ? album.path : '根目录 · 未分类';
        // 含子相册的目录显示递归总数（后端 readable），纯图片目录显示直接图片数
        const total = album.readable != null ? album.readable : album.image_count;
        const countText = album.has_children && total !== album.direct_count
            ? `${total} 张 · 含子相册`
            : `${total} 张`;
        const state = [];
        if (sets.collapsed.has(album.path)) state.push('已收纳');
        if (sets.promoted.has(album.path)) state.push('已提升');
        if (sets.visibleEmpty.has(album.path) && !album.readable) state.push('空相册');
        const menuAction = (album.depth >= 1 && album.path !== '')
            ? this._albumMenuAction(album)
            : null;
        const card = ContentUI.card({
            className: 'iv-album',
            image: album.cover
                ? {
                    src: this._thumbUrl({ url: album.cover, mtime: album.mtime }),
                    alt: '',
                    data: { path: album.path },
                    onError: (event, img) => this._handleAlbumCoverError(img),
                }
                : null,
            mediaOverlay: {
                badge: state.join(' · ') || undefined,
                count: countText,
                time: opts.showTime ? this._timeAgo(album.mtime) : undefined,
            },
            title: album.name,
            sub,
            data: { path: album.path },
            ariaLabel: `${album.name}，${countText}`,
            onActivate: () => this.openAlbum(album.path),
            actions: menuAction ? [menuAction] : undefined,
            actionsPlacement: menuAction ? 'top-right' : undefined,
        });
        const media = card.querySelector('.cui-media');
        const body = card.querySelector('.cui-body');
        const title = card.querySelector('.cui-title');
        const subEl = card.querySelector('.cui-sub');
        if (media) media.classList.add('iv-album-cover');
        if (body) body.classList.add('iv-album-info');
        if (title) title.classList.add('iv-album-name');
        if (subEl) subEl.classList.add('iv-album-count');
        if (!album.cover) this._fillEmptyAlbumCover(card);
        if (menuAction) {
            // 相册菜单既可点右上角按钮，也可右键卡片打开；动作集合完全相同。
            card.addEventListener('contextmenu', (event) => {
                event.preventDefault();
                this.showAlbumMenu(album.path, event);
            });
        }
        return card;
    },

    _albumMenuAction(album) {
        return {
            label: '⋯',
            className: 'iv-album-menu',
            title: '相册设置',
            ariaLabel: `相册设置：${album.name}`,
            data: { path: album.path },
            onClick: (event) => {
                const btn = event.currentTarget;
                const rect = btn.getBoundingClientRect();
                const keyboard = event.detail === 0 || (!event.clientX && !event.clientY);
                this.showAlbumMenu(album.path, keyboard
                    ? { clientX: rect.right, clientY: rect.bottom }
                    : event);
            },
        };
    },

    /** 封面加载失败：重试一次（mtime/URL 不变时绕过一天强缓存），仍失败则换占位图。 */
    _handleAlbumCoverError(img) {
        if (!img) return;
        if (!img.dataset.r) {
            img.dataset.r = '1';
            const url = new URL(img.src, location.origin);
            url.searchParams.set('r', Date.now());
            img.src = url.toString();
            const media = img.closest('.cui-media');
            if (media) media.classList.remove('cui-media--broken');
            return;
        }
        const media = img.closest('.cui-media');
        if (!media) return;
        media.classList.remove('cui-media--broken');
        media.classList.remove('cui-media--empty');
        media.removeAttribute('aria-hidden');
        // 只替换图片本身，ContentUI 的 mediaOverlay 计数/时间/状态保留
        img.replaceWith(this._coverFallback());
    },

    /** 没有封面时 ContentUI 的空媒体框换成带图标的旧占位（封面视觉，不是共享 overlay）。 */
    _fillEmptyAlbumCover(card) {
        const media = card.querySelector('.cui-media');
        if (!media) return;
        media.classList.remove('cui-media--empty');
        media.removeAttribute('aria-hidden');
        media.appendChild(this._coverFallback());
    },

    _coverFallback() {
        const fallback = document.createElement('div');
        fallback.className = 'iv-cover-fallback';
        fallback.innerHTML = Icons.html('icon:image-off');
        return fallback;
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
            grid = ContentUI.grid({ className: 'iv-grid', min: '170px' });
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
        // 打开相册就进入新的导航代次：在途的旧 loadImages（例如当前瀑布流或上一页）
        // 立即作废，异步拿到设置后也要确认用户还在这个代次里。
        const navSeq = ++this._navSeq;
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
            if (navSeq !== this._navSeq) return;
            if (!isPixiv || pixivExplicit || album.root_scope) {
                this.mode = 'children';
                this.childParentPath = path;
                this.fromChildren = true;
                this.currentPath = path;   // 同步当前浏览目录，保证设置基于当前目录
                this.showAlbums();
                this._armViewReplayGuard();
                return;
            }
        }
        this._cancelSlideshow();
        this._showFolder(path);
    },

    _showFolder(path) {
        this._navSeq += 1;
        this._resetImageListState();
        const album = this.albums.find(a => a.path === path);
        this.currentPath = path;
        this.currentPage = 1;
        this.mode = 'images';
        this.filteredSeqIndexes = null;
        // 进相册层时清掉关键词：壳搜索框没有"让内容页回填输入值"的通道，输入框里
        // 可能还留着上一次的关键词，但过滤从这里重置（已知边界，见 AGENTS.md）。
        this.searchQuery = '';
        this._searchClear += 1;
        this._chromeTitle = album ? album.name : (path.split('/').pop() || '未分类');
        this._chromeSub = path || '根目录 · 未分类';
        document.getElementById('iv-albums').innerHTML = '';
        this._armViewReplayGuard();
        this._pushChrome();
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
