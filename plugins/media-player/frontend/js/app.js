// ============================================================
// OmniBox 媒体播放器 — 主应用
// 统一管理音乐 / 视频库视图、播放舞台、歌单、歌词与全屏联动
// ============================================================
// 本文件只保留类骨架：构造函数、初始化与扫描轮询。其余方法按职责拆到同目录的
// app-ui-events / app-views / app-render / app-playback / app-stage 五个分片，
// 由它们各自 Object.assign(MediaPlayerApp.prototype, {...}) 扩回本原型。
// **加载顺序是硬约束**：五个分片必须排在 app.js 之后、实例化之前（见 index.html）；
// 顺序错或分片漏挂会在装载期就抛 `MediaPlayerApp is not defined`。
// 方法与顺序契约由 tests/js/media_player_app_split.mjs 把关。
// ============================================================

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
        this._searchDebounced = null;
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
        this._mountFreshness();
        this.loadExtensions();

        try {
            const state = await Bridge.call('media_get_state');
            this.favIds = new Set((state.favorites || []).map(i => i.id));
        } catch (e) { }

        await this.playlists.load();
        await this._ensureIndex();
        await this._restorePlayback();
        this._updateStats();
        await this._loadCurrentView();
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
            // 首次使用（索引为空）：被动同步会走一遍全库；目录多时它自己降级成
            // 后台校验（进度卡显示在右下角），这里只负责起始文案与收尾提示。
            this._setLoading('首次使用，正在扫描媒体库…\n大媒体库可能需要一点时间');
            this._firstScanPending = true;
            this._autoSyncFreshness();
        } catch (e) {
            console.error('媒体索引初始化失败:', e);
            this._setLoading('');
        }
    }

    /** 挂载共享的「同步状态 + 校验」控件（shell/freshness.js，见 plugin-guide §3.4）。 */
    _mountFreshness() {
        const host = document.getElementById('mp-freshness');
        if (!host || typeof Freshness === 'undefined') return;
        this.freshness = Freshness.mount({
            plugin: 'media-player',
            container: host,
            unit: '首',
            onChange: () => this._onFreshnessChanged(),
        });
    }

    /** 被动同步的触发点（切视图 / 首次进入）。组件去抖，壳侧还有最小间隔。 */
    _autoSyncFreshness() {
        if (this.freshness && typeof this.freshness.autoSync === 'function') {
            this.freshness.autoSync('');
        }
    }

    /** 同步发现变化 / 校验完成：清掉起始遮罩、刷新统计与当前视图。 */
    async _onFreshnessChanged(state) {
        if (this._firstScanPending && !(state && state.busy)) {
            this._firstScanPending = false;
            this._setLoading('');
            Toast.success(`媒体库已就绪：${state && state.entries ? state.entries : 0} 首`);
        }
        await this._updateStats();
        await this._loadCurrentView();
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

    async _updateStats() {
        const el = document.getElementById('mp-stats-text');
        if (!el) return;
        try {
            const stats = await Bridge.call('media_stats');
            el.textContent = `${stats.audio} 音乐 · ${stats.video} 视频 · ${stats.playlists} 歌单`;
        } catch (e) {
            el.textContent = '媒体库统计不可用';
        }
    }

    async loadExtensions() {
        const container = document.getElementById('mp-extensions');
        if (!container || typeof renderExtensions !== 'function') return;
        try {
            // 高亮由共享组件维护（见 base.js 的 renderExtensions）：这里只声明宿主侧栏项的
            // 选择器，让"点扩展入口"时自动取消侧栏项的选中；反向由 `_setNavActive` 调
            // `clearActive()`。
            this.extensions = await renderExtensions(container, 'media-player', 'sidebar', {
                title: '网易云音乐',
                navSelector: '.mp-nav-item',
                onOpen: (ext, btn) => this.openNeteaseView(ext, btn)
            });
        } catch (e) {
            console.error('加载扩展入口失败:', e);
        }
    }

    openNeteaseView(ext, btn) {
        this.currentView = ext.view || 'ncm-daily';
        this.currentAlbum = null;
        this.currentPlaylist = null;
        this.playlists.currentId = '';
        this._setNavActive({});
        if (this.extensions) this.extensions.activate(btn || null);
        this.playlists.renderSidebar();
        document.getElementById('media-search').value = '';
        document.getElementById('btn-search-clear').classList.add('hidden');
        this._loadCurrentView();
    }
}
