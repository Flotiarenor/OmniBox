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
    // 初始化数据
    // ============================================================
    // 轮询扫描任务直至 done/cancelled/none；onProgress 每轮回调刷新进度文案
    async _waitScanDone(onProgress) {
        for (let i = 0; i < 1200; i++) {   // 上限约 10 分钟
            let st = null;
            try { st = await Bridge.call('media_scan_status'); } catch (e) { st = null; }
            if (!st || st.state === 'none' || st.state === 'done' || st.state === 'cancelled') return st;
            if (onProgress) onProgress(st);
            await new Promise(r => setTimeout(r, 500));
        }
        return null;
    }

    async _ensureIndex() {
        this._setLoading('正在准备媒体库…');
        try {
            const stats = await Bridge.call('media_stats');
            const st0 = await Bridge.call('media_scan_status');
            let needWait = false;
            if (st0 && st0.state === 'paused') {
                // 上次扫描被中断：断点续扫（已完成根目录自动跳过）
                this._setLoading('继续上次未完成的扫描…<br>已完成部分自动跳过');
                const started = await Bridge.call('media_scan', false);
                needWait = !(started && started.error);
            } else if (!stats || stats.total === 0) {
                this._setLoading('首次使用，正在扫描媒体库…<br>大媒体库可能需要一点时间');
                const started = await Bridge.call('media_scan', false);
                needWait = true; // 已在运行（如另一标签页触发）同样等待其完成
            }
            if (needWait) {
                const st = await this._waitScanDone();
                if (st && st.state === 'done') {
                    const ex = st.extra || {};
                    Toast.success(`扫描完成：音乐 ${ex.audio ?? '?'} · 视频 ${ex.video ?? '?'}`);
                }
            }
        } catch (e) {
            console.error('媒体索引初始化失败:', e);
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
            await renderExtensions(container, 'media-player', 'sidebar', {
                title: '网易云音乐',
                onOpen: (ext) => this.openNeteaseView(ext)
            });
            container.querySelectorAll('.obx-extension').forEach(btn => {
                btn.addEventListener('click', () => {
                    document.querySelectorAll('.mp-nav-item').forEach(b => b.classList.remove('active'));
                    container.querySelectorAll('.obx-extension').forEach(b => b.classList.remove('active'));
                    btn.classList.add('active');
                });
            });
        } catch (e) {
            console.error('加载扩展入口失败:', e);
        }
    }

    openNeteaseView(ext) {
        this.currentView = ext.view || 'ncm-daily';
        this.currentAlbum = null;
        this.currentPlaylist = null;
        document.getElementById('media-search').value = '';
        document.getElementById('btn-search-clear').classList.add('hidden');
        this._loadCurrentView();
    }
}
