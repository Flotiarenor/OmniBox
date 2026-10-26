// ============================================================
// 图片相册 v2：嵌套文件夹相册 + 时间线 / 最近添加 / 搜索 / 幻灯片
// ============================================================
// 本文件只保留类骨架：构造函数、初始化、生命周期、与壳的 chrome 消息、UI 事件绑定。
// 其余方法按职责拆到同目录的 app-albums / app-nav / app-grid / app-refresh /
// app-settings / app-utils 六个分片，由它们各自 Object.assign(ImageViewer.prototype, {...})
// 扩回本原型。
// **加载顺序是硬约束**：六个分片必须排在 app.js 之后、实例化之前（见 index.html）；
// 顺序错或分片漏挂会在装载期就抛 `ImageViewer is not defined`。
// 方法与顺序契约由 tests/js/image_viewer_app_split.mjs 把关。
//
// 页面 chrome（品牌块 / 左栏 / 导航 / 扩展入口 / 顶栏 / 搜索 / 状态栏 / 分隔条）由壳按
// manifest 的 frontend.chrome 渲染；本文件只维护内容与 chrome 数据：
//   壳 → 内容  omnibox:view（albums / timeline / latest）、omnibox:action、omnibox:query
//   内容 → 壳  omnibox:chrome（标题、副标题、视图、动作显隐、搜索占位、状态）
// ============================================================

class ImageViewer {
    constructor() {
        // 相册瓦片二级加载：每批目录数 / 合批延迟。批量取是为了合并同一帧内进入
        // 视口的多个占位瓦片，一次请求覆盖一批，而不是一张一个请求。
        this._TILE_BATCH_SIZE = 16;
        this._TILE_BATCH_DELAY = 80;

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
        this.albumConfig = { collapsed: [], promoted: [], expanded: [], visible_empty_dirs: [] };
        this.rootsPicker = null;         // 图片根目录列表（共享组件，主目录 + 额外目录）
        this.albumSortBy = 'mtime';      // 作者页面二次排序：mtime | name | count（设置项 album_sort_by）
        this.albumSortOrder = 'desc';    // 作者页面二次排序方向（设置项 album_sort_order）
        this._albumSortVisible = false;  // 设置弹窗里是否显示二次排序选项（生效 Pixiv 排序才显示）
        this.searchQuery = '';           // 壳搜索框下发的关键词（omnibox:query）
        this._searchClear = 0;           // 进二级页时递增，通知壳清空搜索框
        this._appearance = { alpha: 84, follow: true, shellAlpha: 84 };
        this._appearanceBound = false;
        this.isMultiSelectMode = false;
        this.selectedImages = new Set();
        this.moveDestPath = '';
        this.slideshowTimer = null;
        this._slideshowWanted = false;     // 隐藏期间是否要继续播放（用户主动停止则置 false）
        this._slideshowResumeIndex = 0;    // 隐藏时记住第几张，回来从这个位置继续
        this._onResize = null;             // 具名 resize 处理器，dispose 时摘掉
        this.scrollStack = [];             // 从列表进入详情后返回时恢复滚动位置
        this._rebuildStartTime = null;
        this._tileObserver = null;         // 相册瓦片视口观察器（二级加载）
        this._tileQueue = [];              // 待补封面/计数的瓦片路径
        this._tileByPath = new Map();      // 路径 → {item, card}
        this._tileTimer = null;            // 合批定时器

        // 壳渲染的 chrome 的运行期数据：标题/副标题由内容侧维护，动作显隐、搜索占位
        // 与状态栏由 `_pushChrome()` 在每次状态变化时整份上报。
        this._chromeTitle = '全部相册';
        this._chromeSub = '';
        this._statusText = '正在读取…';
        this._statusState = 'idle';
        // 旧 embed 回落（新壳不调用）：内嵌扩展视图打开时取消导航高亮。
        this._extensionOpen = false;
        this._extensionTitle = '';
        this._extensionSub = '';

        // 视图/请求代次：每次切视图或目录时递增；在途 loadImages 发现代次变了就丢弃结果，
        // 避免旧响应把已经切走的视图重新画回来。
        this._navSeq = 0;
        this._loadSeq = 0;
        // 壳在 iframe load 后约 300ms 会补发一次当前视图（首帧握手兜底）。进入二级页后
        // 只拦截这一次迟到重复；后续同一视图仍按正常导航处理（点当前导航回相册根）。
        this._startupView = '';
        this._startupViewCount = 0;
        this._startupReplayUntil = Date.now() + 3000;
        this._loadSeenAt = null;
        this._viewReplay = null;
        this._onWindowLoad = null;

        this.lightbox = null;
        this.pagination = null;
        this.contextMenu = null;
        this._initialized = false;
    }

    // ============================================================
    // 与壳的页面骨架通信
    // ============================================================

    /** 内容页与壳同源：消息只发给父窗口，脱离壳单独打开（或沙箱里没有 window）时不发。 */
    _postHost(payload) {
        if (typeof window === 'undefined' || !window.parent || window.parent === window) return;
        const origin = typeof location !== 'undefined' ? location.origin : '';
        try { window.parent.postMessage(payload, origin); } catch (e) { /* 非同源父窗口不发 */ }
    }

    /**
     * 首帧补发窗口：壳在 iframe load 后补发一次当前视图，只有这段时间内才可能出现
     * 「用户已经点进相册、同一视图的补发才到」的迟到消息。窗口按真实 load 事件收窄，
     * 没有 load 事件的环境（无头用例）退回构造时的 3 秒上限，不影响正常导航。
     */
    _startupReplayWindowOpen() {
        return Date.now() <= this._startupReplayUntil;
    }

    /** 进入二级页时记下当前视图；首帧补发最多只有一条，因此只拦一次。 */
    _armViewReplayGuard() {
        if (!this._startupView
                || !this._startupReplayWindowOpen()
                || this._startupViewCount >= 2) return;
        this._viewReplay = {
            view: this._startupView,
            until: Math.min(Date.now() + 2000, this._startupReplayUntil),
        };
    }

    /** 这条视图消息是否只是首帧迟到补发：只消费一次，后续同一视图仍按正常导航处理。 */
    _consumeLateViewReplay(view) {
        const replay = this._viewReplay;
        if (!replay || view !== replay.view) return false;
        if (!this._startupReplayWindowOpen() || Date.now() > replay.until) {
            this._viewReplay = null;
            return false;
        }
        this._viewReplay = null;
        return true;
    }

    /** 上报 chrome：标题、副标题、视图（'' 表示旧嵌入口、壳取消导航高亮）、顶栏动作
        显隐/禁用/文案、导航组可见性、搜索占位与状态栏。 */
    _pushChrome() {
        const isImages = this.mode === 'images';
        const isChildren = this.mode === 'children';
        const selected = this.selectedImages.size;
        const multi = this.isMultiSelectMode;
        const slideshow = !!this.slideshowTimer;
        this._postHost({
            type: 'omnibox:chrome',
            title: this._extensionOpen
                ? (this._extensionTitle || '扩展')
                : this._chromeTitle,
            subtitle: this._extensionOpen ? this._extensionSub : this._chromeSub,
            view: this._extensionOpen ? '' : this.currentView,
            actions: {
                back: { visible: isImages || isChildren, placement: 'leading' },
                'new-album': { visible: true },
                slideshow: {
                    visible: isImages,
                    active: slideshow,
                    label: slideshow ? '停止' : '幻灯片',
                },
                'multi-select': {
                    visible: isImages,
                    active: multi,
                    label: multi ? '退出多选' : '开启多选',
                },
                'selection-count': {
                    visible: isImages && multi && selected > 0,
                    label: `已选 ${selected} 项`,
                },
                'delete-selected': { visible: isImages && multi, disabled: selected === 0 },
                'move-selected': { visible: isImages && multi, disabled: selected === 0 },
                'refresh-thumbs': { visible: isImages && multi, disabled: selected === 0 },
                settings: { visible: true },
            },
            nav: { browse: { visible: true } },
            search: {
                enabled: !this._extensionOpen,
                placeholder: isImages ? '搜索当前相册…' : '搜索相册…',
                clear: this._searchClear,
            },
            status: { text: this._statusText, state: this._statusState },
        });
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
                return;
            }
            if (data.type === 'omnibox:appearance') {
                this._appearance = {
                    alpha: Number(data.alpha) || 84,
                    follow: data.follow !== false,
                    shellAlpha: Number(data.shellAlpha) || 84,
                };
                this._syncAppearanceControls();
            }
        });
        this._postHost({ type: 'omnibox:ready' });
    }

    /** 壳左栏的三个浏览项：切视图并清掉二级页（子相册 / 图片层）的返回栈。 */
    _applyHostView(view) {
        if (view !== 'albums' && view !== 'timeline' && view !== 'latest') return;
        // 首帧握手通常会有两条同一视图（ready 回发 + iframe load 后补发）。只统计
        // 首帧窗口内的同名视图，并在进入二级页后拦截其中迟到的那一条。
        // 必须先于灯箱处理：用户进相册后很快打开灯箱时，这条重复消息不能替用户关灯箱。
        if (this._startupView && view === this._startupView && this._startupReplayWindowOpen()) {
            this._startupViewCount += 1;
        }
        if (this._consumeLateViewReplay(view)) return;
        // 灯箱是内容页最内层：壳导航先关它并吞掉本次动作，视图层级保持不变。
        if (this._closeLightbox()) return;
        this.closeExtensionView();
        // 立即作废在途 loadImages：即使旧请求随后返回，也不能再写当前视图。
        this._navSeq += 1;
        this.currentView = view;
        this.mode = 'albums';
        this.childParentPath = '';
        this.fromChildren = false;
        this.currentPath = '';
        this.navStack = [];
        this.scrollStack = [];
        this.showAlbums();
    }

    /**
     * 灯箱打开时先关掉它，返回是否真的关了一个。
     *
     * 壳的返回/导航在灯箱之上仍可点击；不关灯箱就会出现“点了返回/导航没反应
     * （内容在灯箱背后切层）”。关闭后补报一次当前视图：壳在发 omnibox:view 时
     * 已把高亮改到目标项，而本次动作被吞掉，不补报会让高亮与内容不一致。
     * 焦点归还在 `createLightbox().hide()` 内部完成（onClose/previousFocus）。
     */
    _closeLightbox() {
        const lightbox = this.lightbox;
        if (!lightbox || typeof lightbox.isOpen !== 'function' || !lightbox.isOpen()) {
            return false;
        }
        lightbox.hide();
        this._pushChrome();
        return true;
    }

    /** 壳顶栏动作：返回、新建、幻灯片、多选与批量操作、设置，以及统一刷新完成后的重载。 */
    async _applyHostAction(action) {
        if (action === 'back') this._handleBack();
        else if (action === 'new-album') this.openNewAlbumModal();
        else if (action === 'slideshow') this.toggleSlideshow();
        else if (action === 'multi-select') this.toggleMultiSelectMode();
        else if (action === 'delete-selected' || action === 'delete') this.deleteSelectedImages();
        else if (action === 'move-selected' || action === 'move') this.openMoveModal();
        else if (action === 'refresh-thumbs') this.refreshSelectedThumbs();
        else if (action === 'settings') this.openSettingsModal();
        else if (action === 'refresh') await this._onFreshnessChanged();
    }

    /** 壳搜索框的输入（壳侧已去抖 250ms）：相册层过滤相册，图片层过滤当前页。 */
    _onQuery(query) {
        const next = String(query == null ? '' : query).trim();
        if (next === this.searchQuery) return;
        this.searchQuery = next;
        if (this.mode === 'images') {
            this.filterCurrentImages(next);
        } else {
            this.showAlbums();
        }
    }

    // 表态"我接得住内嵌插件的请求"：`#extension-frame` 里的页面（image-cleaner /
    // pixiv-sync 的旧 embed 回落）可以用 `HostChannel.requestSettings(...)` 让本页渲染
    // 设置弹窗，也可以用 `HostChannel.mountToolbar([...])` 把「设置」挂进扩展面板头部。
    // 新壳用 native 面板渲染这两个附属插件，这条路径只留给旧壳。
    _serveHostChannel() {
        if (!window.HostChannel || typeof HostChannel.serve !== 'function') return;
        const frames = () => [document.getElementById('extension-frame')];
        HostChannel.serve({
            isOwnFrame: HostChannel.ownFrame(frames),
            containers: { 'host-toolbar': document.getElementById('extension-view-actions') },
        });
    }

    async init() {
        if (this._initialized) return;
        this._initialized = true;

        // 用真实 load 事件收窄首帧补发窗口：壳的补发定时器从 iframe load 起算。
        // 监听要在任何 await 之前注册，避免 list_albums 较慢时错过 load 事件。
        this._onWindowLoad = () => {
            this._loadSeenAt = Date.now();
            this._startupReplayUntil = Math.min(
                this._startupReplayUntil,
                this._loadSeenAt + 800,
            );
            if (this._viewReplay) {
                this._viewReplay.until = Math.min(this._viewReplay.until, this._startupReplayUntil);
            }
        };
        if (typeof window !== 'undefined' && typeof window.addEventListener === 'function') {
            window.addEventListener('load', this._onWindowLoad);
        }

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
        this._serveHostChannel();
        await this.loadSettings();
        await this.loadAlbums();

        // resize 处理器留具名引用：dispose 时要能摘掉，否则监听器随常驻 iframe 只增不减
        this._onResize = () => {
            if (this.mode !== 'images' || !this.currentItems.length) return;
            if (this.searchQuery) {
                this.filterCurrentImages(this.searchQuery);
            } else {
                this.renderJustifiedLayout(this.currentItems);
            }
        };
        window.addEventListener('resize', this._onResize);

        this._bindPluginLifecycle();
        // 与壳握手：先声明监听，再发 ready；壳收到 ready 后补发当前视图。
        // 记下首帧视图，供进入二级页后识别壳的迟到补发。
        this._startupView = this.currentView;
        this._bindHost();
        this._pushChrome();
    }

    // 宿主可见性通知：
    // 常驻插件切到后台后，页面不可见期间没有任何理由继续换图；定时器必须停。
    // 语义是"停视觉与轮询类工作"，不是"停止播放"——相册没有播放，但同样的
    // 原则适用于 media-player：它在 onHide 里只停 rAF 自循环，进度保存继续。
    _bindPluginLifecycle() {
        if (typeof window === 'undefined' || !window.PluginLifecycle) return;
        const lifecycle = window.PluginLifecycle;
        lifecycle.onHide(() => {
            // 记住当前是第几张，回来时从这个位置继续（不跳回第一张）
            if (this.slideshowTimer && this.lightbox && typeof this.lightbox.getIndex === 'function') {
                this._slideshowResumeIndex = this.lightbox.getIndex();
            }
            this._stopSlideshow();
            // 后台不必继续加载/渲染大图
            if (this.mode === 'images' && this.lightbox) {
                this.lightbox.hide();
            }
        });
        // 隐藏期间用户没有主动停止 → 重新显示后从原来的位置继续播放
        lifecycle.onShow(() => {
            if (this._slideshowWanted && !this.slideshowTimer && this.currentAllImages.length) {
                this.toggleSlideshow(this._slideshowResumeIndex || 0);
            }
        });
        lifecycle.onDispose(() => {
            this._cancelSlideshow();
            if (this._onResize) {
                window.removeEventListener('resize', this._onResize);
                this._onResize = null;
            }
            if (this._onWindowLoad) {
                window.removeEventListener('load', this._onWindowLoad);
                this._onWindowLoad = null;
            }
        });
    }

    async loadSettings() {
        try {
            this.currentSettings = await Bridge.call('get_settings', '');
            this.currentRowHeight = this.currentSettings.row_height || 200;
            this._applyAlbumSortSettings();
            // 灯箱的解码位图池张数（全局偏好）：`this.lightbox` 在 init() 里先于本
            // 方法创建，所以这里一定拿得到，开机即生效
            this._applyLightboxCache();
        } catch (e) { }
    }

    // 作者视图（Pixiv 排序下的相册网格）二次排序：持久化在插件设置里，
    // 只在生效 Pixiv 排序的页面/文件夹上起作用。默认「更新时间 / 倒序」。
    _applyAlbumSortSettings() {
        const s = this.currentSettings || {};
        this.albumSortBy = s.album_sort_by || 'mtime';
        this.albumSortOrder = s.album_sort_order || 'desc';
    }

    // 旧 embed 回落：新壳用 native 面板，扩展入口由壳的 PluginExtensions 渲染。
    openExtensionView(ext) {
        const view = document.getElementById('extension-view');
        const frame = document.getElementById('extension-frame');
        const title = document.getElementById('extension-view-title');
        const sub = document.getElementById('extension-view-sub');
        if (!view || !frame) return;
        // 标题取扩展声明的 label，说明取它自己声明的 description（宿主不写插件专有文案）。
        this._extensionOpen = true;
        this._extensionTitle = ext.label || '扩展';
        this._extensionSub = ext.description || '';
        if (title) title.textContent = this._extensionTitle;
        if (sub) sub.textContent = this._extensionSub;
        frame.src = this._embedUrl(ext.embedUrl);
        view.classList.remove('hidden');
        this._pushChrome();
    }

    // 内嵌进本面板的插件页要收起自己的工具栏（宿主已给标题与一行说明）：
    // 用 `?embed=1` 通知它，页面对应 html.is-embedded。
    _embedUrl(url) {
        if (!url) return 'about:blank';
        return url + (url.includes('?') ? '&' : '?') + 'embed=1';
    }

    // 关掉扩展视图并销毁内嵌文档（旧 embed 回落；新壳用 native 面板，不调用本方法）。
    closeExtensionView() {
        const wasOpen = this._extensionOpen;
        this._extensionOpen = false;
        this._extensionTitle = '';
        this._extensionSub = '';
        const view = document.getElementById('extension-view');
        const frame = document.getElementById('extension-frame');
        if (view) view.classList.add('hidden');
        if (frame) frame.src = 'about:blank';
        if (wasOpen) this._pushChrome();
    }

    _bindUI() {
        const rebuildHide = document.getElementById('rebuild-progress-hide');
        if (rebuildHide) rebuildHide.addEventListener('click', () => this.hideRebuildProgress());
        const rebuildCancel = document.getElementById('rebuild-progress-cancel');
        if (rebuildCancel) rebuildCancel.addEventListener('click', () => this.cancelRebuild());
        document.getElementById('settings-cancel').addEventListener('click', () => this.closeSettingsModal());
        document.getElementById('settings-save').addEventListener('click', () => this.saveSettings());
        document.getElementById('move-cancel').addEventListener('click', () => this.closeMoveModal());
        document.getElementById('move-confirm').addEventListener('click', () => this.confirmMove());
        document.getElementById('iv-new-album-cancel').addEventListener('click', () => this.closeNewAlbumModal());
        document.getElementById('iv-new-album-confirm').addEventListener('click', () => this.createAlbum());

        document.getElementById('setting-row-height').addEventListener('input', (e) => {
            document.getElementById('setting-row-height-val').textContent = e.target.value;
        });
        document.getElementById('setting-lightbox-cache').addEventListener('input', (e) => {
            document.getElementById('setting-lightbox-cache-val').textContent = e.target.value;
        });

        // 模糊匹配只作用于「Pixiv 排序支持」：当场切到该排序时就显示出来，
        // 而不是等保存刷新后再打开设置才看见（勾选值仍以当前生效设置为初值）
        document.getElementById('setting-sort-by').addEventListener('change', (e) => {
            const pixiv = e.target.value === 'time_name';
            document.getElementById('setting-pixiv-fuzzy-section').classList.toggle('hidden', !pixiv);
            if (pixiv) this._pixivFuzzyVisible = true;
        });

        // 图片文件夹：多位置列表（主要 / 额外 + 浏览…）由 Shell 共享组件提供，
        // 本插件只负责把列表里的路径在保存时写回 root_dir / extra_roots。
        // 列表实例在 init() 里由 _resetRootsList() 建立。

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
            // pointerdown：按在遮罩上就关，按在弹窗内（哪怕拖到遮罩上松开）不关
            modal.addEventListener('pointerdown', (e) => {
                if (e.target === modal) modal.classList.remove('active');
            });
        });
        document.addEventListener('click', () => this._closeAlbumMenu());
    }
}
