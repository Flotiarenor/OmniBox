// ============================================================
// OmniBox 媒体播放器 — 主应用
// 统一管理音乐 / 视频库视图、播放舞台、歌单、歌词与全屏联动
// ============================================================
// 页面 chrome（品牌块 / 左栏导航 / 歌单组 / 顶栏 / 搜索 / 状态栏 / 侧栏分隔条）由壳按
// manifest 的 frontend.chrome 渲染；本文件只维护内容：播放舞台、播放条、歌词沉浸页、
// 队列与 EQ 浮层、列表 / 卡片与三个内容弹窗。
//
// 与壳的通信：壳发 omnibox:view / omnibox:action / omnibox:query，本文件回
// omnibox:chrome（标题、副标题、视图、动作显隐、歌单导航组、状态）。
//
// 本文件只保留类骨架：构造函数、初始化、宿主消息与扫描轮询。其余方法按职责拆到同目录的
// app-ui-events / app-views / app-render / app-playback / app-stage 五个分片，
// 由它们各自 Object.assign(MediaPlayerApp.prototype, {...}) 扩回本原型。
// **加载顺序是硬约束**：五个分片必须排在 app.js 之后、实例化之前（见 index.html）；
// 顺序错或分片漏挂会在装载期就抛 `MediaPlayerApp is not defined`。
// 方法与顺序契约由 tests/js/media_player_app_split.mjs 把关。
// ============================================================

/** 壳左栏「媒体库」组的六个一级视图；`omnibox:view` 只对它们调 switchView。 */
const MP_LIBRARY_VIEWS = ['recent', 'audio-albums', 'all-audio', 'video-albums', 'all-video', 'favorites'];

class MediaPlayerApp {
    constructor() {
        this.core = null;
        this.playlists = null;
        this.lyrics = null;
        this.settings = {};

        this.currentView = 'recent';
        this.currentAlbum = null;
        this.currentPlaylist = null;
        this.currentNeteasePlaylist = null;
        this._neteasePlaylistBackView = 'ncm-playlists';
        this.favIds = new Set();

        this._initialized = false;
        this._addItemTarget = null;
        this._playlistModalMode = 'create';
        this._playlistModalId = '';
        this._playlistMenuId = '';
        this._contextMenuEl = null;

        this.isFullscreen = false;
        this.hideTimer = null;
        this.fsMoveHandler = null;
        this.controlsVisible = true;

        this._lastAlbums = [];
        this._currentListData = [];
        this._currentListOpts = {};
        this._scanning = false;
        this._loadSeq = 0;
        this._ncmCache = {};
        this._thumbObserver = null;      // 视频封面预取观察器
        this._thumbPrefetchSeen = new Set();

        // 壳渲染的 chrome 的运行期数据：标题 / 副标题由内容侧维护，动作、歌单导航组与
        // 状态在每次变化时由 _pushChrome() 整份上报。
        this.searchQuery = '';
        this._chromeTitle = '最近播放';
        this._chromeSub = '最近听过的媒体';
        this._statusText = '正在读取媒体库…';
        this._statusState = 'idle';
        this._actions = {};
        // 让壳的搜索框跟着内容页清空：一次性载荷，随下一次 chrome 上报发出。
        // 不能每帧上报关键词 —— 那会把用户正在输入的内容顶回去。
        this._searchSync = null;
        this._searchClearSeq = 0;
    }

    async init() {
        if (this._initialized) return;
        this._initialized = true;

        try {
            this.settings = (await Bridge.call('get_settings')) || {};
        } catch (e) {
            this.settings = {};
        }

        this.core = new MediaPlayerCore(this);
        this.core.setResumeMode(this.settings.resume_mode);
        this.playlists = new MediaPlaylistManager(this);
        this.lyrics = new MediaLyrics(this);

        this._bindUI();
        this._bindContentDelegation();
        this._bindKeyboard();
        this._bindThumbPrefetch();
        this._bindPluginLifecycle();
        // 与壳握手：先声明监听，再发 ready；壳收到 ready 后补发当前视图。
        this._bindHost();
        // 首帧先上报一次，长初始化（首次建索引可能较久）期间壳上的标题与状态不空白。
        this._pushChrome();

        try {
            const state = await Bridge.call('media_get_state');
            this.favIds = new Set((state.favorites || []).map(i => i.id));
        } catch (e) { }

        await this.playlists.load();
        await this._ensureIndex();
        await this._restorePlayback();
        await this._updateStats();
        await this._loadCurrentView();
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

    /** 上报 chrome：标题、副标题、视图、动作显隐、歌单导航组与状态文本。 */
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
            search: { enabled: true, ...(searchSync || {}) },
            status: { text: this._statusText, state: this._statusState },
        });
    }

    /** 清空搜索词，并让壳的输入框回空。切视图/进详情时调用：搜索词是全局的，
        换了视图还留着它会让新列表看起来"少了一部分内容"。 */
    _clearSearch() {
        this.searchQuery = '';
        this._searchSync = { value: '', clear: ++this._searchClearSeq };
    }

    /** 壳左栏的高亮视图 id：专辑详情是二级页（空串，不亮导航项）；歌单详情用
        `playlist:<id>` 命中也由壳渲染的动态歌单项；网易云视图用扩展入口的 view id。 */
    _chromeViewId() {
        if (this.currentView === 'album-detail') return '';
        if (this.currentView === 'playlist') {
            return this.currentPlaylist ? `playlist:${this.currentPlaylist.id}` : '';
        }
        if (this.currentView === 'ncm-playlist-detail') return '';
        return this.currentView;
    }

    /** 「我的歌单」动态导航组：整组 items 替换为当前歌单列表 + 末尾的「新建歌单」。
        歌单项 id 是 `playlist:<id>`，与 `omnibox:view` 的载荷共用同一套 id。 */
    _navPatch() {
        const items = (this.playlists ? this.playlists.playlists : []).map(pl => {
            const count = (pl.item_ids || []).length;
            return {
                id: `playlist:${pl.id}`,
                label: pl.name || '未命名歌单',
                icon: 'icon:list-music',
                badge: count ? String(count) : '',
            };
        });
        items.push({ id: 'playlist-new', label: '新建歌单', icon: 'icon:plus' });
        return { playlists: { items } };
    }

    /** 顶栏动作的可见性：视图级「刷新」（重拉网易云数据）只在网易云视图出现；
        设置始终在。本地视图的「同步 / 校验」由壳的 PluginChrome 挂载 freshness 组件。 */
    _updateActions() {
        const ncm = this.currentView.startsWith('ncm-');
        this._actions = {
            'netease-refresh': { visible: ncm, disabled: ncm && this._scanning },
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

    /** 壳左栏的导航项：六个一级视图、动态歌单（含「新建歌单」）与扩展入口的 view
        （网易云视图，id 统一以 `ncm-` 开头）。 */
    _applyHostView(viewId) {
        if (typeof viewId !== 'string' || !viewId) return;
        if (viewId.startsWith('ncm-')) {
            this.openNeteaseView(viewId);
            return;
        }
        if (viewId === 'playlist-new') {
            this._openPlaylistModal('create');
            // 壳点击后会把「新建歌单」当视图高亮；这条弹窗不是视图，立刻把当前视图重报回去
            this._pushChrome();
            return;
        }
        if (viewId.startsWith('playlist:')) {
            this.openPlaylist(viewId.slice('playlist:'.length));
            return;
        }
        if (MP_LIBRARY_VIEWS.includes(viewId)) this.switchView(viewId);
    }

    /** 壳顶栏动作：网易云视图的「刷新」、壳设置弹窗保存后的设置重读，以及统一刷新
        （同步 / 校验）完成后的 `refresh` 重载信号（freshness 组件由壳挂载）。 */
    async _applyHostAction(action) {
        if (action === 'netease-refresh' || action === 'scan') { await this._doScan(); return; }
        if (action === 'settings-saved') { await this._reloadSettings(); return; }
        if (action === 'refresh') {
            await this.playlists.load();
            await this._updateStats();
            await this._loadCurrentView();
            this._pushChrome();
        }
    }

    /** 内容页自己的设置入口（歌词页右上角）：弹窗归壳渲染，这里只请求壳打开同一份
        设置；打开后歌词页先收起，避免弹窗下面的沉浸页还在滚。 */
    _openSettings(title) {
        this._postHost({ type: 'omnibox:open-settings', title: title || '' });
    }

    /** 壳的设置弹窗保存后重读设置并应用：歌词配色/背景、自动隐藏与播放起点都读的是
        内存里的 `this.settings`，不重读要等下次启动才生效。 */
    async _reloadSettings() {
        try {
            this.settings = (await Bridge.call('get_settings')) || {};
        } catch (e) {
            return;
        }
        if (this.core) this.core.setResumeMode(this.settings.resume_mode);
        if (this.lyrics) {
            this.lyrics.applyStyleVars();
            this.lyrics.applyBg(this.core ? this.core.currentItem : null);
        }
        await this._updateStats();
        await this._loadCurrentView();
        this._pushChrome();
    }

    /** 壳搜索框的输入（壳侧已去抖 250ms）：作为全局搜索词，_loadCurrentView 按视图
        决定它是查本地媒体还是查网易云；详情页忽略。 */
    _onQuery(query) {
        const next = String(query == null ? '' : query).trim();
        if (next === this.searchQuery) return;
        this.searchQuery = next;
        this._loadCurrentView();
    }

    // ============================================================
    // 插件生命周期（壳注入的 window.PluginLifecycle，见 docs/plugin-ui-guide.md §7）
    // ============================================================
    // 本插件声明了 `keepAlive`：切走时 iframe 只被 v-show 隐藏，播放与 2s 进度保存
    // 必须继续（申请保活就是为了这个），所以 onHide 只停**纯视觉**的常驻工作 ——
    // 歌词页的频谱 rAF。onShow 恢复它；onDispose 在页面卸载前落一次进度并停掉定时器。
    _bindPluginLifecycle() {
        if (typeof window === 'undefined' || !window.PluginLifecycle) return;
        window.PluginLifecycle.onHide(() => {
            if (this.lyrics) this.lyrics.suspend();
        });
        window.PluginLifecycle.onShow(() => {
            if (this.lyrics) this.lyrics.resume();
        });
        window.PluginLifecycle.onDispose(() => {
            if (!this.core) return;
            try { this.core._saveProgress(); } catch (e) { /* 卸载路径不阻塞 */ }
            this.core._stopProgressSaver();
        });
    }

    // ============================================================
    // 初始化数据
    // ============================================================
    async _ensureIndex() {
        this._setLoading('正在准备媒体库…');
        try {
            const stats = await Bridge.call('media_stats');
            if (stats && stats.total > 0) { this._setLoading(''); return; }
            // 首次使用（索引为空）：统一刷新组件由壳挂载，内容页拿不到它的实例，
            // 这里走插件自己的兼容入口 `media_scan`（force=False 即一次同步）建立索引。
            // 后续增量同步 / 全量校验都点壳顶栏的「校验」，完成经 omnibox:action: refresh 回来。
            this._setLoading('首次使用，正在扫描媒体库…\n大媒体库可能需要一点时间');
            const result = await Bridge.call('media_scan', false);
            if (result && result.success === false) {
                Toast.error(result.error || '媒体库扫描失败');
            } else {
                const after = await Bridge.call('media_stats');
                Toast.success(`媒体库已就绪：${(after && after.total) || 0} 首`);
            }
            this._setLoading('');
        } catch (e) {
            console.error('媒体索引初始化失败:', e);
            this._setLoading('');
        }
    }

    async _restorePlayback() {
        try {
            const pb = await Bridge.call('media_get_playback');
            if (!pb) return;
            // 无条件恢复音量与播放模式：即使上次没有播放条目（如只调过音量）
            if (pb.volume !== undefined && pb.volume !== null) {
                this.core.volume = Math.max(0, Math.min(1, Number(pb.volume) || 1));
            }
            if (pb.loop_mode === 'one') this.core.playMode = 2;
            else if (pb.shuffle) this.core.playMode = 1;
            else this.core.playMode = 0;
            this.updatePlayModeUI();
            if (!pb.item_id) return;
            const item = await Bridge.call('media_get_item', pb.item_id);
            if (item && item.id) {
                await this.core.restorePlayback(item, pb);
            }
        } catch (e) {
            console.log('恢复播放状态失败:', e);
        }
    }

    /** 媒体统计进壳的状态栏：`N 音乐 · M 视频 · P 歌单`。只更新字段，
        由调用方决定何时 _pushChrome（列表重建与播放状态变化都会推）。 */
    async _updateStats() {
        try {
            const stats = await Bridge.call('media_stats');
            this._statusText = `${stats.audio} 音乐 · ${stats.video} 视频 · ${stats.playlists} 歌单`;
            this._statusState = this._playing ? 'ok' : 'idle';
        } catch (e) {
            this._statusText = '媒体库统计不可用';
            this._statusState = 'warn';
        }
    }

    /** 打开网易云原生视图（壳扩展入口点击后发 `omnibox:view`，view 值以 `ncm-` 开头）。
        入口本身由壳的 PluginExtensions 渲染，内容页只按 view id 打开对应视图。 */
    openNeteaseView(view) {
        const viewId = typeof view === 'string' ? view : ((view && view.view) || 'ncm-daily');
        this.currentView = viewId;
        this.currentAlbum = null;
        this.currentPlaylist = null;
        this.currentNeteasePlaylist = null;
        this.playlists.currentId = '';
        this._clearSearch();
        this._updateActions();
        this.playlists.syncNav();
        this._loadCurrentView();
    }
}
