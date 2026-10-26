// ============================================================
// 漫画中心：漫画库 + 下载中心（合并插件）
//
// 页面 chrome（品牌块 / 左栏导航 / 顶栏 / 搜索 / 状态栏 / 分隔条）由壳按
// manifest 的 frontend.chrome 渲染；本文件只维护内容：
//   书架三视图（all / favorites / recent）
//   下载中心六种过滤（downloads:<filter>，toolbar 动作只在下载中心出现）
//   漫画详情 / 章节 / 图片，两个内容弹窗与沉浸式阅读器
//
// 与壳的通信：壳发 omnibox:view / omnibox:action / omnibox:query，本文件回
// omnibox:chrome（标题、副标题、视图、动作显隐与禁用、导航徽标、搜索可用性、状态）。
// 设置的分享组件（同步 / 校验）由壳的 PluginChrome 挂载，校验完成后的重载经
// omnibox:action: refresh 回来。
// ============================================================
class MangaLibraryApp {
    constructor() {
        this.currentView = 'all';
        this.downloadFilter = 'all';
        this.tasks = [];
        this.pollTimer = null;
        this._tasksKey = '';   // 上一次渲染所依据的任务快照，用于跳过无变化的重渲染
        this.reader = null;

        this.currentFolderName = '';
        this.currentDetail = null;
        this.isMultiChapter = false;
        this.viewLevel = 'home'; // home / chapters / images
        this._initialized = false;

        // 壳渲染的 chrome 的运行期数据：标题/副标题由内容侧维护，动作、徽标、
        // 搜索与状态的显隐由 _pushChrome() 在每次状态变化时整份上报。
        this.searchQuery = '';
        this._chromeTitle = '全部漫画';
        this._chromeSub = '';
        this._statusText = '正在读取…';
        this._statusState = 'idle';
        this._searchEnabled = true;
        // 让壳的搜索框跟着内容页清空：一次性载荷，随下一次 chrome 上报发出。
        // 不能每帧上报关键词 —— 那会把用户正在输入的内容顶回去。
        this._searchSync = null;
        this._searchClearSeq = 0;
        this._shelfCounts = { total: 0, favorites: 0, recent: 0 };
        this._actions = {};
    }

    async init() {
        if (this._initialized) return;
        this._initialized = true;

        this.reader = new MangaReader();
        this._bindUI();
        this._updateActions();
        await this.loadView();
        await this.loadTasks();
        this._bindPluginLifecycle();
        this._startPoll();
        // 与壳握手：先声明监听，再发 ready；壳收到 ready 后补发当前视图。
        this._bindHost();
        this._pushChrome();
    }

    // ============================================================
    // 与壳的页面骨架通信
    // ============================================================

    /** 内容页与壳同源：消息只发给父窗口，脱离壳单独打开时不发。 */
    _postHost(payload) {
        if (window.parent === window) return;
        try { window.parent.postMessage(payload, location.origin); } catch (e) { /* 非同源父窗口不发 */ }
    }

    /** 上报 chrome：标题、副标题、视图、动作显隐/禁用、导航徽标、搜索可用性与状态。 */
    _pushChrome() {
        const searchSync = this._searchSync;
        this._searchSync = null;
        this._postHost({
            type: 'omnibox:chrome',
            title: this._chromeTitle,
            subtitle: this._chromeSub,
            view: this._chromeViewId(),
            actions: this._actions,
            nav: this._navPatch(),
            search: { enabled: this._searchEnabled, ...(searchSync || {}) },
            status: { text: this._statusText, state: this._statusState },
        });
    }

    /** 清空搜索词，并让壳的输入框回空。切视图/进详情时调用：列表已经换了内容，
        框里还留着旧关键词会与当前视图对不上。 */
    _clearSearch() {
        this.searchQuery = '';
        this._searchSync = { value: '', clear: ++this._searchClearSeq };
    }

    /** 壳左栏的高亮视图 id：详情/阅读态发空串清掉高亮，下载中心用复合 id。 */
    _chromeViewId() {
        if (this.viewLevel !== 'home') return '';
        if (this.currentView === 'downloads') return `downloads:${this.downloadFilter}`;
        return this.currentView;
    }

    /** 导航徽标补丁：漫画库三个视图与下载中心的任务总数；值取空串表示清掉徽标。
        导航项当前始终可见，`visible` 与徽标一起上报，壳按同一份补丁渲染。 */
    _navPatch() {
        const item = (count) => ({ badge: count ? String(count) : '', visible: true });
        return {
            all: item(this._shelfCounts.total),
            favorites: item(this._shelfCounts.favorites),
            recent: item(this._shelfCounts.recent),
            'downloads:all': item(this.tasks.length),
        };
    }

    /** 壳的握手与反向下发：视图、顶栏动作、搜索输入。 */
    _bindHost() {
        window.addEventListener('message', (event) => {
            if (event.origin !== location.origin || event.source !== window.parent) return;
            const data = event.data || {};
            if (data.type === 'omnibox:view' && typeof data.view === 'string') {
                this._applyHostView(data.view);
                return;
            }
            if (data.type === 'omnibox:action') {
                this._applyHostAction(data.action);
                return;
            }
            if (data.type === 'omnibox:query') {
                this._onQuery(data.query);
            }
        });
        this._postHost({ type: 'omnibox:ready' });
    }

    /** 壳左栏的导航项：漫画库三视图与下载中心的六个过滤项（复合 id）。 */
    _applyHostView(viewId) {
        if (viewId.startsWith('downloads:')) {
            this.switchView('downloads', viewId.slice('downloads:'.length));
            return;
        }
        if (viewId === 'all' || viewId === 'favorites' || viewId === 'recent') this.switchView(viewId);
    }

    /** 壳顶栏动作：返回、下载中心的四个动作、统一刷新的重载信号，以及壳设置弹窗保存后的重载。 */
    async _applyHostAction(action) {
        if (action === 'back') { this._goBack(); return; }
        if (action === 'add-task') { this.showAddModal(); return; }
        if (action === 'settings-saved') {
            // 漫画根目录可能被改过：书架与下载任务都按新根重读一次。
            await this.loadView();
            await this.loadTasks();
            return;
        }
        if (action === 'refresh') {
            await this.loadView();
            await this.loadTasks();
            return;
        }
        if (action === 'start-all') await Bridge.call('download_start_all');
        else if (action === 'pause-all') await Bridge.call('download_pause_all');
        else if (action === 'clear-completed') await Bridge.call('download_clear_completed');
        else return;
        await this.loadTasks();
    }

    /** 壳左侧返回：阅读器 → 图片 → 章节（多章时）或书架；章节/详情 → 书架。 */
    _goBack() {
        // 阅读器是全屏覆盖层，比图片层更内层：壳返回先关它，层级不变。
        if (this._closeReader()) return;
        if (this.viewLevel === 'images') {
            if (this.isMultiChapter) {
                this.viewLevel = 'chapters';
                this.loadView();
            } else {
                this.switchView('all');
            }
            return;
        }
        if (this.viewLevel === 'chapters') this.switchView('all');
    }

    /** 关闭阅读器并返回是否真的关了一个：没有打开的阅读器时返回 false。 */
    _closeReader() {
        if (!this.reader || typeof this.reader.isOpen !== 'function' || !this.reader.isOpen()) {
            return false;
        }
        this.reader.close();
        return true;
    }

    /** 独立打开本页时保留内容内返回；壳内（content=1）返回由壳左侧按钮承担。 */
    _backButtonHtml() {
        return window.parent === window
            ? `<button class="btn ml-hero-back" id="ml-detail-back">${Icons.html('icon:arrow-left')} 返回</button>`
            : '';
    }

    /** 壳搜索框的输入（壳侧已去抖 250ms）；下载中心与详情页不参与搜索。 */
    _onQuery(query) {
        const next = String(query == null ? '' : query).trim();
        if (next === this.searchQuery) return;
        this.searchQuery = next;
        if (this.currentView === 'downloads' || this.viewLevel !== 'home') return;
        this.loadView();
    }

    /** 顶栏动作的运行期状态：返回只在详情/章节/图片层级出现，下载动作只在下载中心出现。 */
    _updateActions() {
        const isDownloads = this.currentView === 'downloads';
        const count = (status) => this.tasks.filter(t => t.status === status).length;
        this._actions = {
            back: { visible: this.viewLevel !== 'home' },
            'add-task': { visible: isDownloads },
            'start-all': { visible: isDownloads, disabled: count('paused') === 0 },
            'pause-all': { visible: isDownloads, disabled: count('downloading') === 0 },
            'clear-completed': { visible: isDownloads, disabled: count('completed') === 0 },
            settings: { visible: true },
        };
    }

    // ============================================================
    // 可见性通知
    // ============================================================

    // 常驻 iframe 切到后台后，没有任何理由继续每 2s 拉一次任务列表，
    // 轮询停掉，回到前台由 onShow 重新拉起。
    _bindPluginLifecycle() {
        if (!window.PluginLifecycle) return;
        const lifecycle = window.PluginLifecycle;
        lifecycle.onHide(() => this._stopPoll());
        lifecycle.onShow(() => this._startPoll());
        lifecycle.onDispose(() => this._stopPoll());
    }

    _startPoll() {
        if (this.pollTimer) return;
        this.pollTimer = setInterval(() => this.loadTasks(), 2000);
    }

    _stopPoll() {
        if (!this.pollTimer) return;
        clearInterval(this.pollTimer);
        this.pollTimer = null;
    }

    // ============================================================
    // UI 绑定（chrome 的监听全部换成 omnibox 消息，这里只剩内容弹窗）
    // ============================================================
    _bindUI() {
        document.getElementById('ml-cancel-add').addEventListener('click', () => this.closeAddModal());
        document.getElementById('ml-confirm-add').addEventListener('click', () => this.confirmAdd());
        document.getElementById('ml-close-detail').addEventListener('click', () => this.closeDetailModal());

        document.querySelectorAll('.modal').forEach(modal => {
            // pointerdown：按在遮罩上就关，按在弹窗内（哪怕拖到遮罩上松开）不关
            modal.addEventListener('pointerdown', (e) => {
                if (e.target === modal) modal.classList.remove('active');
            });
        });
    }

    switchView(view, dlFilter = 'all') {
        // 左栏导航是页面级跳转：阅读器盖在内容上时先关掉它，否则用户会看到
        // 「点了导航但画面没变」。返回动作由 `_goBack` 自己先关阅读器。
        this._closeReader();
        const filter = view === 'downloads' ? (dlFilter || 'all') : 'all';
        // 只有上下文真的变了才清搜索框：壳在加载/握手时会重发当前视图。
        const changed = this.currentView !== view || this.downloadFilter !== filter
            || this.viewLevel !== 'home' || !!this.currentDetail;
        this.currentView = view;
        this.downloadFilter = filter;
        this.viewLevel = 'home';
        this.currentDetail = null;
        if (changed) this._clearSearch();
        this._updateActions();
        this.loadView();
    }

    // ============================================================
    // 视图渲染
    // ============================================================
    async loadView() {
        const content = document.getElementById('ml-content');
        if (!content) return;
        const isDownloads = this.currentView === 'downloads';
        const isDetail = this.viewLevel === 'chapters' || this.viewLevel === 'images';
        const keyword = this.searchQuery;

        this._searchEnabled = !isDownloads && !isDetail;
        if (isDownloads) {
            this._chromeTitle = '下载中心';
            this._chromeSub = '漫画下载任务管理';
        } else if (isDetail) {
            this._chromeTitle = '漫画详情';
            this._chromeSub = '';
        } else if (keyword) {
            this._chromeTitle = `搜索 “${keyword}”`;
            this._chromeSub = '';
        } else {
            this._chromeTitle = { all: '全部漫画', favorites: '我的收藏', recent: '最近阅读' }[this.currentView] || '全部漫画';
            this._chromeSub = this.currentView === 'all' ? '按文件夹扫描' : '';
        }

        try {
            if (isDownloads) {
                this.renderDownloads();
                return;
            }
            await this._renderShelfView(content, keyword);
        } catch (e) {
            console.error('加载视图失败:', e);
            content.innerHTML = this._emptyHtml('icon:triangle-alert', '加载失败', String((e && e.message) || e));
        } finally {
            this.updateStats();
            this._updateActions();
            this._pushChrome();
        }
    }

    /** 书架三视图与详情页；搜索关键字只在一级书架视图生效。 */
    async _renderShelfView(content, keyword) {
        if (this.viewLevel === 'chapters') { this.renderChapters(content); return; }
        if (this.viewLevel === 'images') { await this.renderImages(content); return; }

        const state = await Bridge.call('manga_get_state');
        this._shelfCounts.favorites = (state.favorites || []).length;
        this._shelfCounts.recent = (state.recent || []).length;

        if (keyword) {
            const items = await Bridge.call('manga_search', keyword);
            this._chromeSub = `${items.length} 个结果`;
            // 搜索无结果与空库是两回事：前者不该提示去改漫画根目录。
            this._renderGrid(content, items, {
                single: true,
                empty: { icon: 'icon:search', text: '没有匹配的漫画' },
            });
            return;
        }
        if (this.currentView === 'all') {
            const items = await Bridge.call('manga_list');
            this._shelfCounts.total = items.length;
            this._chromeSub = '按文件夹扫描';
            this._renderGrid(content, items, { single: true });
        } else if (this.currentView === 'favorites') {
            this._chromeSub = `${this._shelfCounts.favorites} 部`;
            this._renderGrid(content, state.favorites, { single: true });
        } else if (this.currentView === 'recent') {
            this._chromeSub = `${this._shelfCounts.recent} 部`;
            this._renderGrid(content, state.recent, { single: true });
        }
    }

    _emptyHtml(icon, text, hint) {
        // 结构走壳的 .empty-state（base.css），插件不再自带一套 .ml-empty 样式
        return `<div class="empty-state">
            <div class="empty-state-icon">${Icons.html(icon)}</div>
            <div class="empty-state-text">${MangaUtils.escapeHtml(text)}</div>
            ${hint ? `<div class="empty-state-hint">${MangaUtils.escapeHtml(hint)}</div>` : ''}
        </div>`;
    }

    _renderGrid(container, items, opts = {}) {
        if (!container) return;
        if (!items || !items.length) {
            // opts.empty 给调用方换一整套空态文案（搜索无结果）；不传时是空库文案。
            const empty = opts.empty;
            const hint = empty ? (empty.hint || '') : (opts.single ? '可在设置中调整漫画根目录' : '');
            container.innerHTML = this._emptyHtml(
                (empty && empty.icon) || 'icon:library',
                (empty && empty.text) || '暂无漫画',
                hint,
            );
            return;
        }
        // 书架卡片走共享 ContentUI（结构、懒加载、role=button、Enter/Space 激活由它提供）；
        // 章节与图片网格仍是插件自己的 .ml-card，本函数只服务书架三视图与搜索结果。
        const target = opts.single ? this._ensureGrid(container) : container;
        target.replaceChildren();
        items.forEach((manga, index) => {
            target.appendChild(this._shelfCard(manga, index));
        });
    }

    /** 一张书架卡片：ContentUI.card + 本插件的封面降级。 */
    _shelfCard(manga, index) {
        const folderName = manga.folder_name;
        const title = manga.title || folderName;
        const author = manga.author || '';
        const coverUrl = manga.cover_url ? Bridge.originalUrl(manga.cover_url) : '';

        // 收藏星交给共享 actions + actionsPlacement('top-right')：位置、按钮
        // 外观与「操作区不激活卡片」的判定都在 ContentUI 内部，插件不再定位。
        const card = ContentUI.card({
            className: 'ml-shelf-card',
            image: coverUrl ? { src: coverUrl, alt: title } : null,
            title,
            sub: author,
            // 页数走封面上的只读层；正文不再重复一个 badge。
            mediaOverlay: { badge: `${manga.page_count}页` },
            ariaLabel: `${title}${author ? '，' + author : ''}，${manga.page_count}页`,
            data: { folder: folderName },
            actionsPlacement: 'top-right',
            actions: [{
                icon: 'icon:star',
                label: '',
                className: 'ml-fav-star' + (manga.is_fav ? ' active' : ''),
                title: manga.is_fav ? '取消收藏' : '收藏',
                ariaLabel: manga.is_fav ? '取消收藏' : '收藏',
                data: { folder: folderName },
                onClick: () => this.toggleFav(folderName),
            }],
            onActivate: () => this.openMangaDetail(folderName),
        });
        card.style.setProperty('--obx-i', String(Math.min(index, 32)));
        this._attachCoverFallback(card, coverUrl);
        return card;
    }

    /** 封面缺失或加载失败时用插件原来的图标降级（ContentUI 只标记 broken）。 */
    _attachCoverFallback(card, coverUrl) {
        const media = card.querySelector('.cui-media');
        if (!media) return;
        const fallback = () => {
            if (media.querySelector('.ml-cover-fallback')) return;
            media.classList.remove('cui-media--empty', 'cui-media--broken');
            media.removeAttribute('aria-hidden');
            const broken = media.querySelector('.cui-image');
            if (broken) broken.remove();
            const node = document.createElement('div');
            node.className = 'ml-cover-fallback';
            node.innerHTML = Icons.html('icon:library');
            media.appendChild(node);
        };
        if (!coverUrl) { fallback(); return; }
        const img = media.querySelector('.cui-image');
        if (!img) { fallback(); return; }
        img.addEventListener('error', fallback, { once: true });
        // 命中缓存的坏图不会等到下一个事件循环：创建时就已经 complete。
        if (img.complete && img.naturalWidth === 0) fallback();
    }

    _ensureGrid(container) {
        let grid = container.querySelector(':scope > .cui-grid');
        if (!grid) {
            container.innerHTML = '';
            grid = ContentUI.grid({ className: 'ml-shelf-grid', ariaLabel: '漫画书架' });
            container.appendChild(grid);
        }
        return grid;
    }

    async toggleFav(folderName) {
        try {
            const isFav = await Bridge.call('manga_toggle_favorite', folderName);
            document.querySelectorAll('.ml-fav-star').forEach(el => {
                if (el.dataset.folder === folderName) {
                    el.classList.toggle('active', isFav);
                    el.title = isFav ? '取消收藏' : '收藏';
                    el.setAttribute('aria-label', el.title);
                    Motion.retrigger(el, 'obx-anim-heart');
                }
            });
            this._shelfCounts.favorites = Math.max(0, this._shelfCounts.favorites + (isFav ? 1 : -1));
            this._pushChrome();
            if (this.currentView === 'favorites' || this.currentView === 'recent') this.loadView();
        } catch (e) {
            Toast.error('收藏操作失败');
        }
    }

    // ============================================================
    // 详情 / 章节 / 图片
    // ============================================================
    async openMangaDetail(folderName) {
        try {
            const detail = await Bridge.call('manga_get_detail', folderName);
            if (!detail.folder_name) return;
            this.currentFolderName = folderName;
            this.currentDetail = detail;
            this.isMultiChapter = detail.is_multi_chapter;
            this._chapterPath = '';
            this.viewLevel = this.isMultiChapter ? 'chapters' : 'images';
            // 详情是二级页：搜索只作用于一级书架，进入后把关键词与壳输入框一起清掉。
            this._clearSearch();
            await this.loadView();
        } catch (e) {
            console.error('打开漫画详情失败', e);
        }
    }

    renderChapters(content) {
        const detail = this.currentDetail;
        const infoHtml = this._detailInfoHtml(detail.info);
        content.innerHTML = `
            <div class="ml-detail-hero" style="--ml-hero-bg:${detail.chapters[0] && detail.chapters[0].cover_url ? `url("${Bridge.originalUrl(detail.chapters[0].cover_url)}")` : 'none'}">
                ${this._backButtonHtml()}
                <div class="ml-detail-cover">${detail.chapters[0] && detail.chapters[0].cover_url ? MangaUtils.coverImg(Bridge.originalUrl(detail.chapters[0].cover_url)) : Icons.html('icon:library')}</div>
                <div class="ml-detail-info">
                    <div class="ml-detail-label">漫画详情</div>
                    <div class="ml-detail-title">${MangaUtils.escapeHtml(detail.title)}</div>
                    <div class="ml-detail-author">${MangaUtils.escapeHtml(detail.author)}</div>
                </div>
                <div class="ml-detail-actions">
                    <button class="btn ${detail.is_fav ? 'btn-primary' : ''}" id="ml-detail-fav">${Icons.html('icon:star')} ${detail.is_fav ? '已收藏' : '收藏'}</button>
                </div>
            </div>
            ${infoHtml}
            <section class="ml-section">
                <h3 class="ml-section-title"><svg class="obx-icon"><use href="#book-open"></use></svg> 章节</h3>
                <div id="ml-chapters" class="ml-grid"></div>
            </section>`;

        const backBtn = document.getElementById('ml-detail-back');
        if (backBtn) backBtn.addEventListener('click', () => this._goBack());
        document.getElementById('ml-detail-fav').addEventListener('click', async (e) => {
            const btn = e.target;
            const isFav = await Bridge.call('manga_toggle_favorite', this.currentFolderName);
            btn.classList.toggle('btn-primary', isFav);
            btn.innerHTML = Icons.html('icon:star') + (isFav ? ' 已收藏' : ' 收藏');
            this._shelfCounts.favorites = Math.max(0, this._shelfCounts.favorites + (isFav ? 1 : -1));
            this._pushChrome();
        });

        const grid = document.getElementById('ml-chapters');
        grid.innerHTML = detail.chapters.map((ch, i) => `
            <div class="ml-card" data-chapter="${MangaUtils.escapeHtml(ch.path)}" style="--obx-i:${Math.min(i, 32)}">
                <div class="ml-cover">${ch.cover_url ? MangaUtils.coverImg(Bridge.originalUrl(ch.cover_url)) : `<div class="ml-cover-fallback">${Icons.html('icon:book-open')}</div>`}</div>
                <div class="ml-info"><div class="ml-card-title">${MangaUtils.escapeHtml(ch.name)}</div></div>
            </div>`).join('');
        grid.querySelectorAll('.ml-card').forEach(card => {
            card.addEventListener('click', async () => {
                this.viewLevel = 'images';
                this._chapterPath = card.dataset.chapter;
                await this.loadView();
            });
        });
    }

    async renderImages(content) {
        const detail = this.currentDetail;
        content.innerHTML = `
            <div class="ml-detail-hero">
                ${this._backButtonHtml()}
                <div class="ml-detail-info">
                    <div class="ml-detail-label">阅读</div>
                    <div class="ml-detail-title">${MangaUtils.escapeHtml(detail.title)}</div>
                    <div class="ml-detail-author">${MangaUtils.escapeHtml(this._chapterPath ? this.currentDetail.chapters.find(c => c.path === this._chapterPath)?.name || '' : '')}</div>
                </div>
            </div>
            ${this._detailInfoHtml(detail.info)}
            <section class="ml-section">
                <h3 class="ml-section-title"><svg class="obx-icon"><use href="#images"></use></svg> 图片</h3>
                <div id="ml-pages" class="ml-grid"></div>
            </section>`;
        const backBtn = document.getElementById('ml-detail-back');
        if (backBtn) backBtn.addEventListener('click', () => this._goBack());

        const grid = document.getElementById('ml-pages');
        grid.innerHTML = '<div class="empty-state"><div class="obx-anim-spin" style="width:32px;height:32px;border-radius:50%;border:3px solid var(--ml-accent-soft);border-top-color:var(--accent);"></div><div>图片加载中…</div></div>';
        try {
            const pages = await Bridge.call('manga_get_pages', this.currentFolderName, this._chapterPath || '');
            if (!pages.length) {
                grid.innerHTML = this._emptyHtml('icon:images', '无图片');
                return;
            }
            Bridge.call('manga_update_recent', this.currentFolderName, 0);
            grid.innerHTML = pages.map((url, i) => `
                <div class="ml-card" data-page="${i}" style="--obx-i:${Math.min(i, 32)}">
                    <div class="ml-cover" style="padding-top:140%;">${MangaUtils.coverImg(Bridge.originalUrl(url), 'icon:images')}</div>
                </div>`).join('');
            grid.querySelectorAll('.ml-card').forEach(card => {
                card.addEventListener('click', () => this.reader.open(pages, parseInt(card.dataset.page, 10)));
            });
        } catch (e) {
            grid.innerHTML = this._emptyHtml('icon:triangle-alert', '加载失败');
        }
    }

    _detailInfoHtml(info) {
        if (!info || !Object.keys(info).length) return '';
        const rows = [];
        const add = (label, value) => {
            if (value !== undefined && value !== null && value !== '') {
                rows.push(`<span class="ml-info-item">${MangaUtils.escapeHtml(label)}: ${MangaUtils.escapeHtml(value)}</span>`);
            }
        };
        add('ID', info.album_id);
        add('原名', info.oname);
        add('作者', info.author);
        add('下载时间', info.download_time);
        if (Array.isArray(info.actors) && info.actors.length) add('演员', info.actors.join(', '));
        const tags = (info.tags || []).map(t => `<span class="ml-tag">${MangaUtils.escapeHtml(t)}</span>`).join('');
        return `<div class="ml-info-box">${rows.join('')}${tags ? `<div style="margin-top:8px;">${tags}</div>` : ''}</div>`;
    }

    // ============================================================
    // 下载中心
    // ============================================================
    async loadTasks() {
        try {
            const result = await Bridge.call('download_list');
            this.tasks = result.tasks || [];
            this.updateStats();
            this._updateActions();
            // 数据没变就不重渲染：整段 innerHTML 重建会让每个 .ml-task 的入场动画
            // 从头播一遍，看起来就像页面每 2s 自己刷新一次。
            if (this.currentView === 'downloads' && JSON.stringify(this.tasks) !== this._tasksKey) {
                this.renderDownloads(false);
            }
            // 轮询到新的任务状态后，顶栏按钮的 visible/disabled 必须一起刷新，
            // 否则壳上的按钮会停在旧态（例如没有任务可暂停时仍可点）。
            this._pushChrome();
        } catch (e) {
            console.error('加载下载任务失败:', e);
        }
    }

    updateStats() {
        const total = this.tasks.length;
        const active = this.tasks.filter(t => t.status === 'downloading').length;
        const completed = this.tasks.filter(t => t.status === 'completed').length;
        const queued = this.tasks.filter(t => t.status === 'queued').length;
        const failed = this.tasks.filter(t => t.status === 'failed').length;
        this._statusText = `任务 ${total} · 下载中 ${active} · 完成 ${completed} · 排队 ${queued}`;
        this._statusState = active ? 'ok' : (failed ? 'warn' : 'idle');
        if (this.currentView === 'downloads') {
            this._chromeSub = `全部 ${total} · 下载中 ${active} · 已完成 ${completed} · 排队 ${queued}`;
        }
    }

    // animate=false 用于轮询驱动的刷新：只换内容、不重播入场动画（.ml-no-anim）。
    renderDownloads(animate = true) {
        const content = document.getElementById('ml-content');
        if (!content) return;
        this._tasksKey = JSON.stringify(this.tasks);
        content.classList.toggle('ml-no-anim', !animate);
        const filtered = this.downloadFilter === 'all'
            ? this.tasks
            : this.tasks.filter(t => t.status === this.downloadFilter);

        if (!filtered.length) {
            content.innerHTML = this._emptyHtml('icon:download', '暂无下载任务', '点击右上角「添加任务」开始下载漫画');
            return;
        }

        content.innerHTML = filtered.map((task, i) => {
            const percent = task.totalImages > 0 ? Math.round((task.completedImages / task.totalImages) * 100) : 0;
            const thumb = MangaUtils.escapeHtml(task.thumbUrl || '');
            return `
            <div class="ml-task" data-task-id="${task.id}" style="--obx-i:${Math.min(i, 24)}">
                ${thumb ? `<img class="ml-task-thumb" src="${thumb}" alt="" onerror="this.style.display='none'">` : `<div class="ml-task-thumb" style="display:flex;align-items:center;justify-content:center;font-size:22px;">${Icons.html('icon:library')}</div>`}
                <div class="ml-task-main">
                    <div class="ml-task-title">${MangaUtils.escapeHtml(task.title || `漫画 #${task.albumId}`)}</div>
                    <div class="ml-task-meta">
                        <span class="ml-status ${task.status}">${DownloadUtils.getStatusText(task.status)}</span>
                        <span>${task.completedImages || 0}/${task.totalImages || '?'} 页</span>
                        ${task.speed ? `<span>${DownloadUtils.formatSpeed(task.speed)}</span>` : ''}
                        ${task.eta ? `<span>剩余 ${DownloadUtils.formatTime(task.eta)}</span>` : ''}
                    </div>
                </div>
                <div class="ml-task-progress">
                    <div class="ml-progress-track"><div class="ml-progress-fill" style="width:${percent}%"></div></div>
                    <div class="ml-task-percent">${percent}%</div>
                </div>
                <div class="ml-task-actions">
                    ${task.status === 'downloading' || task.status === 'queued' ? `<button data-act="pause" title="暂停">${Icons.html('icon:pause')}</button>` : ''}
                    ${task.status === 'paused' ? `<button data-act="resume" title="继续">${Icons.html('icon:play')}</button>` : ''}
                    ${task.status === 'failed' ? `<button data-act="retry" title="重试">${Icons.html('icon:rotate-cw')}</button>` : ''}
                    <button data-act="detail" title="详情">ℹ</button>
                    <button data-act="delete" class="danger" title="删除"><svg class="obx-icon"><use href="#trash-2"></use></svg></button>
                </div>
            </div>`;
        }).join('');

        content.querySelectorAll('.ml-task-actions button').forEach(btn => {
            btn.addEventListener('click', async (e) => {
                e.stopPropagation();
                const taskId = btn.closest('.ml-task').dataset.taskId;
                const act = btn.dataset.act;
                if (act === 'pause') await Bridge.call('download_pause', taskId);
                else if (act === 'resume') await Bridge.call('download_resume', taskId);
                else if (act === 'retry') await Bridge.call('download_retry', taskId);
                else if (act === 'detail') { await this.showTaskDetail(taskId); return; }
                else if (act === 'delete') {
                    const task = this.tasks.find(t => t.id === taskId);
                    const ok = await confirmDialog(`确定删除任务「${task ? task.title : taskId}」？`, { danger: true });
                    if (!ok) return;
                    await Bridge.call('download_delete', taskId);
                }
                await this.loadTasks();
            });
        });
    }

    // ===== 添加 / 详情弹窗 =====
    showAddModal() {
        const modal = document.getElementById('ml-add-modal');
        modal.classList.add('active');
        document.getElementById('ml-input-id').value = '';
        setTimeout(() => document.getElementById('ml-input-id').focus(), 40);
    }

    closeAddModal() {
        document.getElementById('ml-add-modal').classList.remove('active');
    }

    async confirmAdd() {
        const albumId = document.getElementById('ml-input-id').value.trim();
        if (!albumId) {
            Toast.warning('请输入漫画 ID 或 URL');
            return;
        }
        const concurrency = parseInt(document.getElementById('ml-input-concurrency').value || '3', 10) || 3;
        const priority = document.getElementById('ml-input-priority').value || 'normal';
        const autoStart = document.getElementById('ml-input-auto-start').checked;
        try {
            await Bridge.call('download_submit', albumId, concurrency, priority, autoStart);
            this.closeAddModal();
            await this.loadTasks();
            Toast.success('任务已添加');
        } catch (e) {
            Toast.error('添加任务失败: ' + e.message);
        }
    }

    async showTaskDetail(taskId) {
        const task = this.tasks.find(t => t.id === taskId);
        if (!task) return;
        try {
            const info = await Bridge.call('download_get_album_info', task.albumId);
            const container = document.getElementById('ml-detail-content');
            container.innerHTML = `
                <div class="ml-field"><label>标题</label><div>${MangaUtils.escapeHtml(info.title || task.title || '未知')}</div></div>
                <div class="ml-field"><label>作者</label><div>${MangaUtils.escapeHtml(info.author || '未知')}</div></div>
                <div class="ml-field"><label>漫画 ID</label><div>${MangaUtils.escapeHtml(task.albumId)}</div></div>
                <div class="ml-field"><label>状态</label><div><span class="ml-status ${task.status}">${DownloadUtils.getStatusText(task.status)}</span></div></div>
                <div class="ml-field"><label>进度</label><div>${task.completedImages || 0} / ${task.totalImages || info.total_page_count || '?'} 页</div></div>
                <div class="ml-field"><label>下载速度</label><div>${task.speed ? DownloadUtils.formatSpeed(task.speed) : '0 B/s'}</div></div>
                <div class="ml-field"><label>剩余时间</label><div>${task.eta ? DownloadUtils.formatTime(task.eta) : '--'}</div></div>
                ${info.error ? `<div class="ml-field"><label>错误</label><div style="color:var(--danger);">${MangaUtils.escapeHtml(info.error)}</div></div>` : ''}`;
            document.getElementById('ml-detail-modal').classList.add('active');
        } catch (e) {
            Toast.error('获取任务详情失败');
        }
    }

    closeDetailModal() {
        document.getElementById('ml-detail-modal').classList.remove('active');
    }
}
