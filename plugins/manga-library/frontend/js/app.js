// ============================================================
// 漫画中心：漫画库 + 下载中心（合并插件）
//
// 页面 chrome（品牌块 / 左栏导航 / 顶栏 / 搜索 / 状态栏 / 分隔条）由壳按
// manifest 的 frontend.chrome 渲染；本文件只维护内容：
//   书架三视图（all / favorites / recent）
//   下载中心六种过滤（downloads:<filter>，toolbar 动作只在下载中心出现）
//   漫画详情 / 章节 / 图片，两个内容弹窗与沉浸式阅读器
//
// 与壳的通信：壳发 omnibox:view / omnibox:action / omnibox:query 与
// omnibox:freshness（统一刷新报告），本文件回 omnibox:chrome（标题、副标题、视图、
// 动作显隐与禁用、导航徽标、搜索可用性、同步作用域、状态）。
// 同步 / 校验控件由壳的 PluginChrome 挂载；内容页在切回前台时自己按当前作用域同步，
// 变化后的重载经 omnibox:freshness 与兼容的 omnibox:action: refresh 进来。壳把同一份
// 报告附在 refresh 动作的 payload 上，因此"两条消息属于同一次同步"不用靠时间窗猜。
// ============================================================

class MangaLibraryApp {
    constructor() {
        this.currentView = 'all';
        this.downloadFilter = 'all';
        this.tasks = [];
        this.pollTimer = null;
        this._tasksKey = '';   // 上一次渲染所依据的任务快照，用于跳过无变化的重渲染
        // 上一次任务轮询得到的快照签名：2 秒一次轮询里任务没变就不重报 chrome
        this._tasksSignature = '';
        // 已见过的 completed 任务 id 与"是否已经轮询过一次"：轮询里发现新完成的任务时
        // 重载书架（下载中心与书架共用同一个数据根）。
        this._completedIds = new Set();
        this._tasksLoaded = false;
        // 最近一次"写阅读页码"的桥调用。打开与关闭各写一次，两次写必须**串行**：
        // 同一次快速开关里，打开那次写还没落地就发出关闭那次写，两条桥调用由宿主各起
        // 一条线程执行，落地顺序不确定 —— 于是"关闭时最后停留的页码"可能被打开时的
        // 页码覆盖回去。这里把每次写串在上一次的后面（见 `_reportReadingPage`）。
        this._readerWrite = null;
        // 从「最近阅读」进入时还待定位的页码；null 表示没有续读意图
        this._resumePage = null;
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
        // 阅读进度只在"打开"与"关闭"各写一次：关闭路径（灯箱自己的 ✕ / Esc / 壳顶栏
        // 返回）都汇到 `onClose`，翻阅过程中不落盘。两次写经 `_reportReadingPage`
        // 串行化，后写覆盖前写。
        this.reader.onClose = (page) => {
            this._reportReadingPage(page);
        };
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

    /** 内容页与壳同源：消息只发给父窗口，脱离壳单独打开时不发。
        实现只有一份（/shell/chrome-client.js），这里只是本页的调用出口。 */
    _postHost(payload) {
        window.ChromeClient.post(payload);
    }

    /** 上报 chrome：标题、副标题、视图、动作显隐/禁用、导航徽标、搜索可用性、
        同步作用域与状态。 */
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
            freshness_scope: this._freshnessScope(),
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

    /** 壳的握手与反向下发：视图、顶栏动作、搜索输入、统一刷新报告。
        校验、分发与 ready 握手都在 /shell/chrome-client.js（四个内容页共用一份）。 */
    _bindHost() {
        this._chrome = window.ChromeClient.create({
            onView: (view) => this._applyHostView(view),
            onAction: (action, payload) => this._applyHostAction(action, payload),
            onQuery: (query) => this._onQuery(query),
            onMessage: (data) => this._onHostMessage(data),
        });
        this._chrome.bind();
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
    async _applyHostAction(action, payload) {
        if (action === 'back') { await this._goBack(); return; }
        if (action === 'add-task') { this.showAddModal(); return; }
        if (action === 'settings-saved') {
            // 漫画根目录可能被改过：书架与下载任务都按新根重读一次。
            await this.loadView();
            await this.loadTasks();
            return;
        }
        if (action === 'refresh') {
            // 新壳在 omnibox:freshness 之后紧跟一条 legacy refresh，并把同一份报告附在
            // 动作的 payload 上：富事件已经重载过，这条只当回声跳过。没有 payload 的
            // refresh 来自旧壳，按原语义重载。
            if (payload != null) return;
            await this._reloadAfterFreshness();
            return;
        }
        if (action === 'start-all') await Bridge.call('download_start_all');
        else if (action === 'pause-all') await Bridge.call('download_pause_all');
        else if (action === 'clear-completed') await Bridge.call('download_clear_completed');
        else return;
        await this.loadTasks();
    }

    /** 壳下发的非 view / action / query 消息；目前只消费统一刷新报告。 */
    _onHostMessage(data) {
        if (!data || data.type !== 'omnibox:freshness') return;
        if (data.plugin && data.plugin !== 'manga-library') return;
        if (!this._freshnessChanged(data.report)) return;
        void this._reloadAfterFreshness();
    }

    /** 壳左侧返回：阅读器 → 图片 → 章节（多章时）或书架；章节/详情 → 书架。 */
    async _goBack() {
        // 阅读器是全屏覆盖层，比图片层更内层：壳返回先关它，层级不变。
        if (await this._closeReader()) return;
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

    /** 关闭阅读器并返回是否真的关了一个：没有打开的阅读器时返回 false。
        关闭会把最后停留的页码写回「最近阅读」（由阅读器的 `onClose` 发起，灯箱自己的
        ✕ / Esc 也走这条），这里等那次写入落定再返回，避免紧接着的视图切换与它抢同一
        份状态文件。灯箱已经自己关掉时（`isOpen()` 为假）仍要等那次待写入。 */
    async _closeReader() {
        const wasOpen = !!this.reader && typeof this.reader.isOpen === 'function'
            && this.reader.isOpen();
        if (wasOpen) this.reader.close();
        if (this._readerWrite) {
            const pending = this._readerWrite;
            this._readerWrite = null;
            await pending;
        }
        return wasOpen;
    }

    /** 把阅读页码写回「最近阅读」（后端按 0 起）。

    页码与漫画目录在**调用时**取定，再把这次写串到上一次写之后：打开与关闭各写一次，
    快速开关时若两次桥调用并发落地，关闭时的最后页码可能被打开时的页码顶掉。返回值
    是这次写（含之前排队的写）的 Promise，`_closeReader` 据此等它落定。

    失败只影响续读位置，不打断看图：`_writeReadingPage` 内部吞掉桥调用异常。
     */
    _reportReadingPage(page) {
        const folder = this.currentFolderName;
        if (!folder) return Promise.resolve();
        const index = Math.max(0, Number(page) || 0);
        const previous = this._readerWrite || Promise.resolve();
        const next = previous.catch(() => { }).then(
            () => this._writeReadingPage(folder, index));
        this._readerWrite = next;
        return next;
    }

    /** 真正发出一次 `manga_update_recent`；失败静默（下次打开会重新记录）。 */
    async _writeReadingPage(folder, index) {
        try {
            await Bridge.call('manga_update_recent', folder, index);
        } catch (e) {
            // 记录页码失败不影响看图
        }
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
    // 轮询停掉，回到前台由 onShow 重新拉起；同时按当前作用域同步一次磁盘，
    // 让切走期间新增或删除的漫画在回来时就能看到。
    _bindPluginLifecycle() {
        if (!window.PluginLifecycle) return;
        const lifecycle = window.PluginLifecycle;
        lifecycle.onHide(() => this._stopPoll());
        lifecycle.onShow(() => {
            this._startPoll();
            void this._syncFreshness(true);
        });
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
    // 统一刷新（同步 / 校验）
    // ============================================================

    /** 同步作用域：书架与下载中心用全部根，详情/章节/图片层用它所属的漫画目录。 */
    _freshnessScope() {
        if (this.viewLevel === 'home') return '';
        return this.currentFolderName || '';
    }

    /** 报告里是否真有变化：三个计数与 `invalidated` 同等看待；`skip` 不算。 */
    _freshnessChanged(report) {
        if (!report || report.action === 'skip') return false;
        return !!(report.added || report.changed || report.removed || report.invalidated);
    }

    /** 按当前作用域同步一次，有变化且 `reload` 为真时就地重载。 */
    async _syncFreshness(reload) {
        if (!window.Freshness || typeof window.Freshness.sync !== 'function') return null;
        let report = null;
        try {
            report = await window.Freshness.sync('manga-library', this._freshnessScope());
        } catch (e) {
            // 被动同步失败不打扰用户：下一次切回前台会再试一次
            return null;
        }
        if (reload && this._freshnessChanged(report)) await this._reloadAfterFreshness();
        return report;
    }

    /**
     * 磁盘有新内容后就地重载：不改变层级、不关阅读器。
     *
     * 阅读器覆盖在内容之上，重绘它背后的网格只会让用户手里那一页失去焦点，因此
     * 阅读器打开时只重读下载任务。章节/图片层要重取详情再按原层级重绘；章节文件
     * 消失时退回章节层，而不是退回书架。
     */
    async _reloadAfterFreshness() {
        if (this.reader && typeof this.reader.isOpen === 'function' && this.reader.isOpen()) {
            await this.loadTasks();
            return;
        }
        if (this.viewLevel !== 'home' && this.currentFolderName) {
            let detail = null;
            let failed = false;
            try {
                detail = await Bridge.call('manga_get_detail', this.currentFolderName);
            } catch (e) {
                failed = true;
            }
            if (failed) {
                // 后端暂时读不到：层级与详情都保持原样，下一次刷新再重试
            } else if (!detail || !detail.folder_name) {
                // 目录已经不在了：退回书架，下一次渲染自然用新列表
                this.currentDetail = null;
                this.viewLevel = 'home';
                this.currentFolderName = '';
                this._chapterPath = '';
            } else {
                this.currentDetail = detail;
                this.isMultiChapter = detail.is_multi_chapter;
                const chapters = detail.chapters || [];
                if (chapters.length === 0) {
                    // 整本都没图片了：书架卡片也不该再留它，回到书架重读列表
                    this.currentDetail = null;
                    this.viewLevel = 'home';
                    this.currentFolderName = '';
                    this._chapterPath = '';
                } else if (this.viewLevel === 'images' && this._chapterPath
                    && !chapters.some((chapter) => chapter.path === this._chapterPath)) {
                    // 正在看的章节没了：退回章节层，而不是退回书架
                    this._chapterPath = '';
                    if (this.isMultiChapter) this.viewLevel = 'chapters';
                } else if (this.viewLevel === 'chapters' && !this.isMultiChapter) {
                    this.viewLevel = 'images';
                    this._chapterPath = '';
                }
            }
        }
        await this.loadView();
        await this.loadTasks();
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
        // 关闭时的页码写入在 `_closeReader` 里 await，这里不等：导航必须立刻生效，
        // 那一次状态写入与本次视图渲染互不影响。
        void this._closeReader();
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
        // 「最近阅读」的条目带上次读到第几页（后端按 0 起记）；从这一层打开时
        // 直接定位到那一页，其它视图没有这个字段，也就没有续读意图。搜索结果来自
        // `manga_search`，不带 `page`：缺字段一律按"没有续读意图"处理，否则一本
        // 从没读过的书会被自动打开到第 1 页。
        const resumePage = this.currentView === 'recent' && manga.page !== undefined
            && manga.page !== null
            ? Math.max(0, Number(manga.page) || 0)
            : null;

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
            onActivate: () => this.openMangaDetail(folderName, { resumePage }),
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
    async openMangaDetail(folderName, options = {}) {
        try {
            const detail = await Bridge.call('manga_get_detail', folderName);
            if (!detail.folder_name) return;
            this.currentFolderName = folderName;
            this.currentDetail = detail;
            this.isMultiChapter = detail.is_multi_chapter;
            this._chapterPath = '';
            this.viewLevel = this.isMultiChapter ? 'chapters' : 'images';
            // 续读页码：只有调用方明确给了才生效（从「最近阅读」进入）。多章本子要
            // 先选章节，页码在章节层没有意义，`renderChapters` 会把它清掉。
            this._resumePage = options.resumePage == null
                ? null
                : Math.max(0, Number(options.resumePage) || 0);
            // 详情是二级页：搜索只作用于一级书架，进入后把关键词与壳输入框一起清掉。
            this._clearSearch();
            await this.loadView();
        } catch (e) {
            console.error('打开漫画详情失败', e);
        }
    }

    renderChapters(content) {
        // 多章本子停在章节层：页码是"某一章里的第几页"，没有章节信息时无处安放，
        // 留着会在用户点进任意章节时把人带到错误的位置。
        this._resumePage = null;
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
            grid.innerHTML = pages.map((url, i) => `
                <div class="ml-card" data-page="${i}" style="--obx-i:${Math.min(i, 32)}">
                    <div class="ml-cover" style="padding-top:140%;">${MangaUtils.coverImg(Bridge.originalUrl(url), 'icon:images')}</div>
                </div>`).join('');
            grid.querySelectorAll('.ml-card').forEach(card => {
                card.addEventListener('click', () => this._openReaderAt(pages, parseInt(card.dataset.page, 10)));
            });
            // 从「最近阅读」进来的续读意图：图片列表就绪后直接打开那一页。只消费一次，
            // 之后本层级的任何重绘都不再自动打开阅读器。
            const resume = this._resumePage;
            this._resumePage = null;
            if (resume !== null) await this._openReaderAt(pages, resume);
        } catch (e) {
            grid.innerHTML = this._emptyHtml('icon:triangle-alert', '加载失败');
        }
    }

    /** 从指定页打开阅读器，并把页码交给「最近阅读」——下次接着读的是这一页。
        打开与关闭各写一次（关闭由 `MangaReader.onClose` 发起），浏览图片层本身不写。 */
    async _openReaderAt(pages, index) {
        const total = Array.isArray(pages) ? pages.length : 0;
        if (!total) return;
        const page = Math.max(0, Math.min(Number(index) || 0, total - 1));
        this.reader.open(pages, page);
        await this._reportReadingPage(page);
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
            const signature = JSON.stringify(this.tasks);
            // 任务有没有变：2 秒一次的轮询里没变就不必重报 chrome，
            // 否则壳那边每 2 秒收到一份完全相同的动作/状态补丁。
            const changed = signature !== this._tasksSignature;
            this._tasksSignature = signature;
            // 有任务刚从别的状态变成 completed：下载目录已经写进漫画根，书架缓存已由
            // 后端作废，停在书架视图时这里把列表重读一遍（否则新书要切一次视图才出现）。
            const finished = this._hasNewCompleted();
            this.updateStats();
            this._updateActions();
            // 数据没变就不重渲染：整段 innerHTML 重建会让每个 .ml-task 的入场动画
            // 从头播一遍，看起来就像页面每 2s 自己刷新一次。
            if (this.currentView === 'downloads' && signature !== this._tasksKey) {
                this.renderDownloads(false);
            }
            // 轮询到新的任务状态后，顶栏按钮的 visible/disabled 必须一起刷新，
            // 否则壳上的按钮会停在旧态（例如没有任务可暂停时仍可点）。
            if (changed) this._pushChrome();
            if (finished && this.viewLevel === 'home' && this.currentView !== 'downloads') {
                await this.loadView();
            }
        } catch (e) {
            console.error('加载下载任务失败:', e);
        }
    }

    /** 本次轮询里是否出现了新的 completed 任务；顺带更新已见集合。
        首次轮询只登记不重载：应用刚启动时书架已经读过一次，历史任务不算"刚下完"。 */
    _hasNewCompleted() {
        const completed = new Set(
            this.tasks.filter(task => task.status === 'completed').map(task => task.id));
        const wasLoaded = this._tasksLoaded;
        this._tasksLoaded = true;
        const finished = wasLoaded
            && Array.from(completed).some(id => !this._completedIds.has(id));
        this._completedIds = completed;
        return finished;
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
