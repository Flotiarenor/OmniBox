// ============================================================
// 图片相册 v2：嵌套文件夹相册 + 时间线 / 最近添加 / 搜索 / 幻灯片
// ============================================================
// 本文件只保留类骨架：构造函数、初始化、生命周期与扩展入口、UI 事件绑定。其余方法按职责
// 拆到同目录的 app-albums / app-nav / app-grid / app-refresh / app-settings / app-utils
// 六个分片，由它们各自 Object.assign(ImageViewer.prototype, {...}) 扩回本原型。
// **加载顺序是硬约束**：六个分片必须排在 app.js 之后、实例化之前（见 index.html）；
// 顺序错或分片漏挂会在装载期就抛 `ImageViewer is not defined`。
// 方法与顺序契约由 tests/js/image_viewer_app_split.mjs 把关。
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
        this.albumConfig = { collapsed: [], promoted: [], expanded: [], visible_empty_dirs: [] };
        this.rootsPicker = null;         // 图片根目录列表（共享组件，主目录 + 额外目录）
        this.albumSortBy = 'mtime';      // 作者页面二次排序：mtime | name | count（设置项 album_sort_by）
        this.albumSortOrder = 'desc';    // 作者页面二次排序方向（设置项 album_sort_order）
        this._albumSortVisible = false;  // 设置弹窗里是否显示二次排序选项（生效 Pixiv 排序才显示）
        this.isMultiSelectMode = false;
        this.selectedImages = new Set();
        this.moveDestPath = '';
        this.slideshowTimer = null;
        this._slideshowWanted = false;     // 隐藏期间是否要继续播放（用户主动停止则置 false）
        this._slideshowResumeIndex = 0;    // 隐藏时记住第几张，回来从这个位置继续
        this._onResize = null;             // 具名 resize 处理器，dispose 时摘掉
        this.scrollStack = [];             // 从列表进入详情后返回时恢复滚动位置
        this._rebuildStartTime = null;

        this.lightbox = null;
        this.pagination = null;
        this.contextMenu = null;
        this._initialized = false;
    }

    // 表态"我接得住内嵌插件的请求"：`#extension-frame` 里的页面（image-cleaner /
    // pixiv-sync）可以用 `HostChannel.requestSettings(...)` 让**本页**渲染设置弹窗
    // （有整页遮罩、不会被 iframe 边界裁掉），也可以用 `HostChannel.mountToolbar([...])`
    // 把「设置」这类按钮挂进本页头部（`#extension-view-actions`）。
    //
    // 宿主不需要知道对面是谁：校验只认"这个 window 是我嵌的那个 iframe"（`event.source`）；
    // 按钮定义由对面给出，点击只回发 `host-run`，处理逻辑仍在对面自己的页面里。
    // 保存同样归对面：本页的 Bridge 指向本插件，替它保存会写错插件。
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
        this.loadExtensions();

        // resize 处理器留具名引用：dispose 时要能摘掉，否则监听器随常驻 iframe 只增不减
        this._onResize = () => {
            if (this.mode !== 'images' || !this.currentItems.length) return;
            const keyword = document.getElementById('iv-search').value.trim();
            if (keyword) {
                this.filterCurrentImages(keyword);
            } else {
                this.renderJustifiedLayout(this.currentItems);
            }
        };
        window.addEventListener('resize', this._onResize);

        this._bindPluginLifecycle();
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
        });
    }

    async loadSettings() {
        try {
            this.currentSettings = await Bridge.call('get_settings', '');
            this.currentRowHeight = this.currentSettings.row_height || 200;
            this._applyAlbumSortSettings();
        } catch (e) { }
    }

    // 作者视图（Pixiv 排序下的相册网格）二次排序：持久化在插件设置里，
    // 只在生效 Pixiv 排序的页面/文件夹上起作用。默认「更新时间 / 倒序」。
    _applyAlbumSortSettings() {
        const s = this.currentSettings || {};
        this.albumSortBy = s.album_sort_by || 'mtime';
        this.albumSortOrder = s.album_sort_order || 'desc';
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
        frame.src = this._embedUrl(ext.embedUrl);
        view.classList.remove('hidden');
    }

    // 内嵌进本面板的插件页要收起自己的工具栏（宿主已给标题与「返回相册」）：
    // 用 `?embed=1` 通知它，页面对应 html.is-embedded（见 docs/plugin-ui-guide.md §3.4）。
    _embedUrl(url) {
        if (!url) return 'about:blank';
        return url + (url.includes('?') ? '&' : '?') + 'embed=1';
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

        document.getElementById('setting-row-height').addEventListener('input', (e) => {
            document.getElementById('setting-row-height-val').textContent = e.target.value;
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
        // 这套实现原本长在这里（_renderRoots / openDirBrowser / _loadDirBrowser），
        // 因为媒体播放器 / 漫画 / 文档阅读也要同一套界面，已整体搬到
        // shell/frontend/public/shell/folder-picker.js + folder-picker.css。
        // 列表实例在 init() 里由 _resetRootsList() 建立。

        // 「仅应用于当前文件夹」只影响显示/排序类设置：图片文件夹列表始终是全局的
        // （换根目录不可能只对一个子文件夹生效），所以这里不再联动任何输入框

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
