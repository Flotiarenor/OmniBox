// ============================================================
// OmniBox 媒体播放器 — 主应用
// 统一管理音乐 / 视频库视图、播放舞台、歌单、歌词与全屏联动
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
        this.playlists = new MediaPlaylistManager(this);
        this.lyrics = new MediaLyrics(this);

        this._bindUI();
        this._bindContentDelegation();
        this._bindKeyboard();
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
    async _ensureIndex() {
        this._setLoading('正在准备媒体库…');
        try {
            const stats = await Bridge.call('media_stats');
            if (!stats || stats.total === 0) {
                this._setLoading('首次使用，正在扫描媒体库…<br>大媒体库可能需要一点时间');
                const result = await Bridge.call('media_scan', false);
                Toast.success(`扫描完成：音乐 ${result.audio} · 视频 ${result.video}`);
            }
        } catch (e) {
            console.error('媒体索引初始化失败:', e);
        }
    }

    async _restorePlayback() {
        try {
            const pb = await Bridge.call('media_get_playback');
            if (!pb || !pb.item_id) return;
            const item = await Bridge.call('media_get_item', pb.item_id);
            if (item && item.id) {
                this.core.restorePlayback(item, pb);
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

    // ============================================================
    // UI 事件绑定
    // ============================================================
    _bindUI() {
        // 左侧导航
        document.querySelectorAll('.mp-nav-item').forEach(btn => {
            btn.addEventListener('click', () => this.switchView(btn.dataset.view));
        });

        // 工具栏
        document.getElementById('btn-scan').addEventListener('click', () => this._doScan());
        document.getElementById('btn-settings').addEventListener('click', () => this._openSettings());
        const search = document.getElementById('media-search');
        this._searchDebounced = MPUtils.debounce(() => this._loadCurrentView(), 300);
        search.addEventListener('input', () => {
            const value = search.value.trim();
            document.getElementById('btn-search-clear').classList.toggle('hidden', !value);
            this._searchDebounced();
        });
        search.addEventListener('keydown', (e) => {
            if (e.key === 'Escape') {
                search.value = '';
                document.getElementById('btn-search-clear').classList.add('hidden');
                this._loadCurrentView();
            }
        });
        document.getElementById('btn-search-clear').addEventListener('click', () => {
            search.value = '';
            document.getElementById('btn-search-clear').classList.add('hidden');
            this._loadCurrentView();
            search.focus();
        });

        // 歌单
        document.getElementById('btn-new-playlist').addEventListener('click', () => this._openPlaylistModal('create'));

        // 播放控制
        document.getElementById('btn-play-pause').addEventListener('click', () => this.core.togglePlay());
        document.getElementById('btn-prev').addEventListener('click', () => this.core.prev());
        document.getElementById('btn-next').addEventListener('click', () => this.core.next(false));
        document.getElementById('btn-stop').addEventListener('click', () => this.core.stop());
        document.getElementById('btn-play-mode').addEventListener('click', () => this.core.cyclePlayMode());
        document.getElementById('btn-toggle-fav').addEventListener('click', () => this._toggleCurrentFavorite());
        document.getElementById('player-cover').addEventListener('click', () => this._toggleLyrics());

        const progress = document.getElementById('progress-bar');
        progress.addEventListener('input', () => this.core.seekTo(parseFloat(progress.value)));
        progress.addEventListener('change', () => this.core.seekTo(parseFloat(progress.value)));

        // 音量
        const volumeBar = document.getElementById('volume-bar');
        volumeBar.addEventListener('input', () => {
            this.core.volume = parseFloat(volumeBar.value);
        });
        document.getElementById('btn-volume-icon').addEventListener('click', () => this.core.toggleMute());

        // 右侧工具
        document.getElementById('btn-queue').addEventListener('click', () => this._toggleQueue());
        document.getElementById('btn-clear-queue').addEventListener('click', () => this._clearQueue());
        document.getElementById('btn-eq').addEventListener('click', () => this._toggleEQ());
        document.getElementById('btn-eq-close').addEventListener('click', () => this._hideEQ());
        document.getElementById('btn-eq-reset').addEventListener('click', () => this._resetEQ());
        document.getElementById('btn-eq-save').addEventListener('click', () => MPUtils.openModal('modal-eq-name'));
        document.getElementById('eq-preset-select').addEventListener('change', (e) => this._applyEQPreset(e.target.value));
        document.getElementById('btn-video-mode').addEventListener('click', () => this._toggleVideoMode());
        document.getElementById('btn-wide').addEventListener('click', () => this._toggleWideMode());
          document.getElementById('btn-fullscreen').addEventListener('click', () => this.toggleFullscreen());

        // 视频舞台：音乐封面 → 歌词页；视频画面 → 全屏
        const stageViewport = document.querySelector('#mp-stage .mp-stage-viewport');
        stageViewport.addEventListener('click', (e) => this._onStageClick(e));
        document.getElementById('mp-stage-cover').addEventListener('click', () => this._toggleLyrics());
        document.getElementById('mp-stage-lyrics').addEventListener('click', () => this._toggleLyrics());
        document.getElementById('btn-stage-play').addEventListener('click', (e) => {
            e.stopPropagation();
            if (this._isVideoShowing()) this.core.togglePlay();
        });

        // 歌词页
        document.getElementById('btn-lyrics-back').addEventListener('click', () => this.lyrics.hide());
        document.getElementById('btn-lyrics-settings').addEventListener('click', () => {
            this.lyrics.hide();
            this._openSettings('歌词与播放设置');
        });
        const lyricsProgress = document.getElementById('lyrics-progress-bar');
        lyricsProgress.addEventListener('input', () => {
            this.core.seekTo(parseFloat(lyricsProgress.value));
        });

        // 歌单弹窗
        document.getElementById('btn-playlist-cancel').addEventListener('click', () => MPUtils.closeModal('modal-playlist'));
        document.getElementById('btn-playlist-cancel-2').addEventListener('click', () => MPUtils.closeModal('modal-playlist'));
        document.getElementById('btn-playlist-confirm').addEventListener('click', () => this._confirmPlaylistModal());
        document.getElementById('input-playlist-name').addEventListener('keydown', (e) => {
            if (e.key === 'Enter') this._confirmPlaylistModal();
        });

        // 加入歌单弹窗
        document.getElementById('btn-add-pl-close').addEventListener('click', () => MPUtils.closeModal('modal-add-to-playlist'));
        document.getElementById('btn-add-pl-new').addEventListener('click', () => {
            MPUtils.closeModal('modal-add-to-playlist');
            this._openPlaylistModal('create', null, true);
        });

        // EQ 预设命名弹窗
        document.getElementById('btn-eq-name-cancel').addEventListener('click', () => MPUtils.closeModal('modal-eq-name'));
        document.getElementById('btn-eq-name-cancel-2').addEventListener('click', () => MPUtils.closeModal('modal-eq-name'));
        document.getElementById('btn-eq-name-confirm').addEventListener('click', () => this._confirmEQName());
        document.getElementById('input-eq-name').addEventListener('keydown', (e) => {
            if (e.key === 'Enter') this._confirmEQName();
        });

        // 点击遮罩关闭弹窗 / 面板
        document.querySelectorAll('.mp-modal').forEach(modal => {
            modal.addEventListener('click', (e) => {
                if (e.target === modal) modal.classList.remove('active');
            });
        });

        document.addEventListener('click', (e) => this._onDocumentClick(e));
        document.addEventListener('fullscreenchange', () => {
            if (!document.fullscreenElement && this.isFullscreen) {
                this._exitFullscreen();
            }
        });

        this.updatePlayModeUI();
        this.updateVolumeUI();
        this._loadEQPresets();
    }

    _onStageClick(e) {
        if (e.target.closest('button, input, select, .mp-playerbar')) return;
        if (this._isVideoShowing()) {
            // 未全屏：点击视频画面进入全屏；全屏中：点击切换播放/暂停
            if (this.isFullscreen) this.core.togglePlay();
            else this._enterFullscreen();
        }
    }

    _onDocumentClick(e) {
        const queue = document.getElementById('queue-popup');
        if (queue && !queue.classList.contains('hidden') && !queue.contains(e.target)
            && e.target !== document.getElementById('btn-queue')) {
            queue.classList.add('hidden');
        }
        const eq = document.getElementById('eq-panel');
        if (eq && !eq.classList.contains('hidden') && !eq.contains(e.target)
            && e.target !== document.getElementById('btn-eq')) {
            eq.classList.add('hidden');
        }
        this._closePlaylistMenu();
    }

    // ============================================================
    // 扫描 / 设置
    // ============================================================
    async _doScan() {
        if (this._scanning) return;
        this._scanning = true;
        const btn = document.getElementById('btn-scan');
        btn.disabled = true;
        btn.textContent = this.currentView.startsWith('ncm-') ? '⏳ 刷新中…' : '⏳ 扫描中…';
        if (this.currentView.startsWith('ncm-')) {
            this._clearNeteaseCache();
            try {
                await this._loadCurrentView();
                Toast.success('网易云数据已刷新');
            } catch (e) {
                Toast.error('刷新失败');
            } finally {
                this._scanning = false;
                if (btn) {
                    btn.disabled = false;
                    btn.textContent = this.currentView.startsWith('ncm-') ? '🔄 刷新' : '🔄 扫描';
                }
            }
            return;
        }
        this._setLoading('正在重新扫描媒体库…<br>目录较大时请稍候');
        try {
            const result = await Bridge.call('media_scan', true);
            Toast.success(`扫描完成：音乐 ${result.audio} · 视频 ${result.video}`);
            await this._updateStats();
        } catch (e) {
            Toast.error('扫描失败');
        } finally {
            this._scanning = false;
            btn.disabled = false;
            btn.textContent = '🔄 扫描';
            await this._loadCurrentView();
        }
    }

    _openSettings(title = '媒体播放器设置') {
        openSettingsModal({
            title,
            successMessage: '设置已保存',
            onSave: async (values) => {
                const result = await Bridge.call('save_settings', values);
                if (result && result.success === false) return result;
                return { success: true };
            },
        });
    }

    // ============================================================
    // 视图切换与加载
    // ============================================================
    async switchView(view) {
        this.currentView = view;
        this.currentAlbum = null;
        this.currentPlaylist = null;
        this.playlists.currentId = '';
        document.querySelectorAll('.mp-nav-item').forEach(b => b.classList.toggle('active', b.dataset.view === view));
        this.playlists.renderSidebar();
        document.getElementById('media-search').value = '';
        document.getElementById('btn-search-clear').classList.add('hidden');
        await this._loadCurrentView();
    }

    async openAlbum(key, kind) {
        this.currentView = 'album-detail';
        this.currentPlaylist = null;
        this.playlists.currentId = '';
        this.currentAlbum = { key, kind };
        document.querySelectorAll('.mp-nav-item').forEach(b => b.classList.remove('active'));
        this.playlists.renderSidebar();
        await this._loadCurrentView();
    }

    async openPlaylist(playlistId) {
        const pl = await this.playlists.get(playlistId);
        if (!pl) {
            Toast.error('歌单不存在');
            return;
        }
        this.currentView = 'playlist';
        this.currentAlbum = null;
        this.currentPlaylist = pl;
        this.playlists.currentId = playlistId;
        document.querySelectorAll('.mp-nav-item').forEach(b => b.classList.remove('active'));
        this.playlists.renderSidebar();
        await this._loadCurrentView();
    }

    _ncmCacheGet(key) {
        try {
            const raw = localStorage.getItem('ncmCache_' + key);
            if (raw) return JSON.parse(raw);
        } catch (e) { }
        return null;
    }

    _ncmCacheSet(key, data) {
        try {
            localStorage.setItem('ncmCache_' + key, JSON.stringify(data));
        } catch (e) { }
    }

    _isSameDay(dateStr) {
        if (!dateStr) return false;
        try {
            const d = new Date(dateStr);
            const now = new Date();
            return d.getFullYear() === now.getFullYear() && d.getMonth() === now.getMonth() && d.getDate() === now.getDate();
        } catch (e) {
            return false;
        }
    }

    _clearNeteaseCache() {
        ['ncm-daily', 'ncm-playlists', 'ncm-liked', 'ncm-my-playlists'].forEach(key => {
            try { localStorage.removeItem('ncmCache_' + key); } catch (e) { }
        });
        // 同时清理所有歌单详情缓存
        try {
            const keys = [];
            for (let i = 0; i < localStorage.length; i++) {
                const k = localStorage.key(i);
                if (k && k.startsWith('ncmCache_ncm-playlist-detail-')) keys.push(k);
            }
            keys.forEach(k => localStorage.removeItem(k));
        } catch (e) { }
        this._ncmCache = {};
    }

    async _prepareNeteaseItems(items, onProgress) {
        const list = items || [];
        for (let i = 0; i < list.length; i++) {
            const item = list[i];
            if (item && item.ncm_encrypted_id && !item.stream_url) {
                try {
                    const data = await Bridge.callPlugin('netease-music', 'get_song_url', item.ncm_encrypted_id, item.original_id);
                    if (data && data.url) item.stream_url = data.url;
                } catch (e) { }
            }
            if (onProgress) onProgress(i + 1, list.length);
        }
    }

    async _loadCurrentView() {
        const seq = ++this._loadSeq;
        const keyword = (document.getElementById('media-search').value || '').trim();
        const titleEl = document.getElementById('mp-view-title');
        const subEl = document.getElementById('mp-view-sub');
        const scanBtn = document.getElementById('btn-scan');
        if (scanBtn) scanBtn.textContent = this.currentView.startsWith('ncm-') ? '🔄 刷新' : '🔄 扫描';
        const viewsTitle = {
            'recent': ['最近播放', '最近听过的媒体'],
            'audio-albums': ['音乐专辑', '按专辑标签聚合'],
            'all-audio': ['全部音乐', '媒体库中的音频'],
            'video-albums': ['视频专辑', '按目录聚合'],
            'all-video': ['全部视频', '媒体库中的视频'],
            'favorites': ['我的喜欢', '收藏的音乐与视频'],
            'ncm-daily': ['每日推荐', '网易云每日推荐'],
            'ncm-playlists': ['推荐歌单', '网易云推荐歌单'],
            'ncm-liked': ['我的喜欢', '网易云红心歌曲'],
            'ncm-my-playlists': ['我的歌单', '网易云创建/收藏的歌单'],
            'ncm-login': ['登录', '网易云账号登录'],
        };

        try {
            let data = [];
            let mode = 'list';

            if (this.currentView.startsWith('ncm-')) {
                if (this.currentView === 'ncm-login') {
                    await this._renderNeteaseLogin();
                    return;
                }
                if (keyword && this.currentView === 'ncm-playlists') {
                    const ncm = await Bridge.callPlugin('netease-music', 'search_playlist', keyword);
                    if (seq !== this._loadSeq) return;
                    this._renderNeteasePlaylists(ncm.results || []);
                    titleEl.textContent = `搜索 “${keyword}”`;
                    subEl.textContent = `${(ncm.results || []).length} 个歌单`;
                    return;
                }
                if (keyword) {
                    const ncm = await Bridge.callPlugin('netease-music', 'search_song', keyword);
                    if (seq !== this._loadSeq) return;
                    data = (ncm.results || []).map(song => this._neteaseToMediaItem(song));
                    titleEl.textContent = `搜索 “${keyword}”`;
                    subEl.textContent = `${data.length} 个结果`;
                    this._renderList(data, { showAlbum: true, showTag: true });
                    return;
                }
                switch (this.currentView) {
                    case 'ncm-daily': {
                        const cacheKey = 'ncm-daily';
                        const cached = this._ncmCacheGet(cacheKey);
                        if (cached && this._isSameDay(cached.date)) {
                            data = (cached.results || []).map(song => this._neteaseToMediaItem(song));
                            if (seq !== this._loadSeq) return;
                            this._renderList(data, { showAlbum: true, showTag: true });
                            return;
                        }
                        const ncm = await Bridge.callPlugin('netease-music', 'get_daily_recommend');
                        if (seq !== this._loadSeq) return;
                        this._ncmCacheSet(cacheKey, { date: new Date().toISOString(), results: ncm.results || [] });
                        data = (ncm.results || []).map(song => this._neteaseToMediaItem(song));
                        this._renderList(data, { showAlbum: true, showTag: true });
                        return;
                    }
                    case 'ncm-playlists': {
                        const cacheKey = 'ncm-playlists';
                        const cached = this._ncmCacheGet(cacheKey);
                        const twoHours = 2 * 60 * 60 * 1000;
                        if (cached && cached.ts && (Date.now() - cached.ts < twoHours)) {
                            if (seq !== this._loadSeq) return;
                            this._renderNeteasePlaylists(cached.results || []);
                            return;
                        }
                        const ncm = await Bridge.callPlugin('netease-music', 'search_playlist', '推荐');
                        if (seq !== this._loadSeq) return;
                        this._ncmCacheSet(cacheKey, { ts: Date.now(), results: ncm.results || [] });
                        this._renderNeteasePlaylists(ncm.results || []);
                        return;
                    }
                    case 'ncm-liked': {
                        const cacheKey = 'ncm-liked';
                        const cached = this._ncmCacheGet(cacheKey);
                        if (cached && cached.results) {
                            data = (cached.results || []).map(song => this._neteaseToMediaItem(song));
                            if (seq !== this._loadSeq) return;
                            this._renderList(data, { showAlbum: true, showTag: true });
                            return;
                        }
                        const ncm = await Bridge.callPlugin('netease-music', 'get_liked_songs', 100);
                        if (seq !== this._loadSeq) return;
                        this._ncmCacheSet(cacheKey, { results: ncm.results || [] });
                        data = (ncm.results || []).map(song => this._neteaseToMediaItem(song));
                        this._renderList(data, { showAlbum: true, showTag: true });
                        return;
                    }
                    case 'ncm-my-playlists': {
                        const cacheKey = 'ncm-my-playlists';
                        const cached = this._ncmCacheGet(cacheKey);
                        if (cached && cached.results) {
                            if (seq !== this._loadSeq) return;
                            this._renderNeteasePlaylists(cached.results || []);
                            return;
                        }
                        const [created, collected] = await Promise.allSettled([
                            Bridge.callPlugin('netease-music', 'get_created_playlists', 100),
                            Bridge.callPlugin('netease-music', 'get_collected_playlists', 100),
                        ]);
                        if (seq !== this._loadSeq) return;
                        const map = new Map();
                        const add = (arr) => (arr || []).forEach(p => {
                            if (p && p.id && !map.has(p.id)) map.set(p.id, p);
                        });
                        add(created.status === 'fulfilled' ? created.value.results : []);
                        add(collected.status === 'fulfilled' ? collected.value.results : []);
                        const results = Array.from(map.values());
                        this._ncmCacheSet(cacheKey, { results });
                        this._renderNeteasePlaylists(results);
                        return;
                    }
                }
            }

            if (keyword) {
                data = await Bridge.call('media_search', keyword);
                titleEl.textContent = `搜索 “${keyword}”`;
                subEl.textContent = `${data.length} 个结果`;
                this._renderList(data, { showAlbum: true, showTag: true });
                return;
            }

            if (viewsTitle[this.currentView]) {
                titleEl.textContent = viewsTitle[this.currentView][0];
                subEl.textContent = viewsTitle[this.currentView][1];
            }

            switch (this.currentView) {
                case 'recent': {
                    const state = await Bridge.call('media_get_state');
                    data = state.recent || [];
                    this._renderList(data, { showAlbum: true, showTag: true, showPlayedAt: true });
                    return;
                }
                case 'favorites': {
                    const state = await Bridge.call('media_get_state');
                    data = state.favorites || [];
                    this._renderList(data, { showAlbum: true, showTag: true });
                    return;
                }
                case 'audio-albums':
                    data = await Bridge.call('media_audio_albums');
                    this._lastAlbums = data;
                    this._renderAlbums(data, 'audio');
                    return;
                case 'video-albums':
                    data = await Bridge.call('media_video_albums');
                    this._lastAlbums = data;
                    this._renderAlbums(data, 'video');
                    return;
                case 'all-audio':
                    data = await Bridge.call('media_all_audio');
                    this._renderList(data, { showAlbum: true, showTag: false });
                    return;
                case 'all-video':
                    data = await Bridge.call('media_all_video');
                    this._renderList(data, { showAlbum: true, showTag: true });
                    return;
                case 'album-detail': {
                    const album = this.currentAlbum;
                    if (!album) return this._renderEmpty('🎧', '请选择一个专辑');
                    const items = await Bridge.call('media_album_items', album.key, album.kind);
                    titleEl.textContent = album.kind === 'video' ? '视频专辑' : '音乐专辑';
                    subEl.textContent = `${items.length} 个媒体`;
                    this._renderDetail(items, {
                        label: album.kind === 'video' ? '视频专辑' : '音乐专辑',
                        title: this._findAlbumName(album.key) || '专辑详情',
                        sub: '',
                        cover: this._findAlbumCover(album.key) || (items[0] && items[0].cover_path),
                        kind: album.kind,
                    });
                    return;
                }
                case 'playlist': {
                    const pl = this.currentPlaylist;
                    if (!pl) return this._renderEmpty('📋', '请选择一个歌单');
                    titleEl.textContent = pl.name || '歌单';
                    subEl.textContent = `${(pl.items || []).length} 个媒体`;
                    this._renderDetail(pl.items || [], {
                        label: '歌单',
                        title: pl.name,
                        sub: `创建于 ${pl.created_at || ''}`,
                        cover: (pl.items || []).length ? (pl.items[0].cover_path || '') : '',
                        kind: 'playlist',
                        playlistId: pl.id,
                    });
                    return;
                }
                default:
                    this._renderEmpty('🎧', '未知视图');
            }
        } catch (e) {
            console.error('加载视图失败:', e);
            this._renderEmpty('⚠️', '加载失败', String(e && e.message || e));
        }
    }

    _findAlbumName(key) {
        const album = this._lastAlbums.find(a => a.key === key);
        return album ? album.name : '';
    }

    _findAlbumCover(key) {
        const album = this._lastAlbums.find(a => a.key === key);
        return album ? album.cover_path : '';
    }

    // ============================================================
    // 渲染：空状态 / 加载
    // ============================================================
    _setLoading(text) {
        const content = document.getElementById('media-content');
        content.innerHTML = `
            <div class="mp-loading">
                <div class="mp-spinner"></div>
                <div>${text}</div>
            </div>`;
    }

    _renderEmpty(icon, text, hint) {
        const content = document.getElementById('media-content');
        content.innerHTML = `
            <div class="mp-empty-state">
                <div class="empty-icon">${icon}</div>
                <div class="empty-text">${MPUtils.escapeHtml(text)}</div>
                ${hint ? `<div class="empty-hint">${MPUtils.escapeHtml(hint)}</div>` : ''}
            </div>`;
    }

    // ============================================================
    // 渲染：专辑卡片
    // ============================================================
    _renderAlbums(albums, kind) {
        const content = document.getElementById('media-content');
        if (!albums || !albums.length) {
            const isVideo = kind === 'video';
            this._renderEmpty(isVideo ? '📀' : '💿', isVideo ? '暂无视频专辑' : '暂无音乐专辑', '点击右上角「扫描」建立媒体索引');
            return;
        }

        content.innerHTML = `<div class="mp-card-grid"></div>`;
        const grid = content.firstElementChild;

        albums.forEach((album, index) => {
            const card = document.createElement('div');
            card.className = 'mp-card';
            card.style.setProperty('--i', Math.min(index, 26));
            card.dataset.key = album.key;
            card.dataset.kind = album.kind;
            const cover = album.cover_path
                ? MPUtils.coverImg(MPUtils.mediaUrl(album.cover_path), kind === 'video' ? '🎬' : '💿')
                : `<div class="cover-fallback">${kind === 'video' ? '🎬' : '💿'}</div>`;
            card.innerHTML = `
                <div class="mp-card-cover">
                    ${cover}
                    <span class="mp-card-badge">${album.count} 项</span>
                    <button class="mp-card-play" data-key="${MPUtils.escapeHtml(album.key)}" data-kind="${album.kind}" title="播放全部">▶</button>
                </div>
                <div class="mp-card-body">
                    <div class="mp-card-title">${MPUtils.escapeHtml(album.name)}</div>
                    <div class="mp-card-sub">${MPUtils.escapeHtml(album.artist || '')}</div>
                    <div class="mp-card-meta">${MPUtils.fmtDuration(album.total_duration)}</div>
                </div>`;
            grid.appendChild(card);
        });
    }

    // ============================================================
    // 渲染：媒体列表
    // ============================================================
    _renderList(items, opts = {}, container = null) {
        const target = container || document.getElementById('media-content');
        if (!items || !items.length) {
            target.innerHTML = '';
            target.appendChild(this._buildEmpty('🎵', '暂无媒体', '点击右上角「扫描」建立媒体索引'));
            return;
        }

        this._currentListData = items;
        this._currentListOpts = opts;
        this._currentListContainer = target;

        const rows = items.map((item, index) => this._rowHtml(item, index, opts)).join('');
        target.innerHTML = `<div class="mp-list">${rows}</div>`;
    }

    _buildEmpty(icon, text, hint) {
        const div = document.createElement('div');
        div.className = 'mp-empty-state';
        div.innerHTML = `
            <div class="empty-icon">${icon}</div>
            <div class="empty-text">${MPUtils.escapeHtml(text)}</div>
            ${hint ? `<div class="empty-hint">${MPUtils.escapeHtml(hint)}</div>` : ''}`;
        return div;
    }

    _rowHtml(item, index, opts) {
        const active = this.core.currentItem && this.core.currentItem.id === item.id;
        const isFav = this.favIds.has(item.id);
        const cover = item.cover_path
            ? MPUtils.coverImg(MPUtils.coverUrl(item), MPUtils.itemIcon(item))
            : MPUtils.itemIcon(item);
        const subParts = [];
        if (item.artist) subParts.push(item.artist);
        if (opts.showAlbum && item.album) subParts.push(item.album);
        if (opts.showPlayedAt && item.played_at) subParts.push(MPUtils.timeAgo(item.played_at));
        const sub = MPUtils.escapeHtml(subParts.join(' · '));

        const actions = [];
        if (opts.playlistId) {
            actions.push(`<button class="mp-row-action danger" data-mp-action="remove" data-idx="${index}" title="移出歌单">✕</button>`);
        } else {
            actions.push(`<button class="mp-row-action ${isFav ? 'fav-active' : ''}" data-mp-action="fav" data-idx="${index}" title="${isFav ? '取消喜欢' : '喜欢'}">${isFav ? '❤️' : '♡'}</button>`);
            actions.push(`<button class="mp-row-action" data-mp-action="queue" data-idx="${index}" title="添加到队列">＋</button>`);
            actions.push(`<button class="mp-row-action" data-mp-action="playlist" data-idx="${index}" title="加入歌单">📋</button>`);
        }

        return `
            <div class="mp-row ${active ? 'active' : ''}" data-id="${MPUtils.escapeHtml(item.id)}" data-idx="${index}"
                 style="--i:${Math.min(index, 30)}">
                <span class="mp-row-index">${index + 1}</span>
                <div class="mp-row-cover">${cover}</div>
                <div class="mp-row-info">
                    <div class="mp-row-title">${MPUtils.escapeHtml(item.title)}</div>
                    <div class="mp-row-sub">${sub}</div>
                </div>
                ${opts.showTag ? `<span class="mp-row-tag ${item.kind === 'video' ? 'video' : ''}">${item.kind === 'video' ? '视频' : '音乐'}</span>` : ''}
                <span class="mp-row-duration">${MPUtils.formatTime(item.duration)}</span>
                <div class="mp-row-actions">${actions.join('')}</div>
            </div>`;
    }

    _neteaseToMediaItem(song) {
        return {
            id: 'ncm:' + (song.original_id || song.id || ''),
            original_id: song.original_id || song.id || '',
            ncm_encrypted_id: song.id || '',
            kind: 'audio',
            title: song.name || '未知歌曲',
            artist: (song.artists || []).join(', '),
            album: song.album || '',
            duration: (song.duration || 0) / 1000,
            cover_path: song.cover_url || '',
            path: '',
            online: true,
            is_fav: false,
        };
    }

    async _renderNeteaseLogin() {
        const content = document.getElementById('media-content');
        try {
            const login = await Bridge.callPlugin('netease-music', 'check_login');
            if (login && login.success) {
                content.innerHTML = '<div class="mp-empty-state"><div class="empty-icon">✅</div><div class="empty-text">已登录网易云音乐</div></div>';
                return;
            }
        } catch (e) { }
        content.innerHTML = `
            <div class="mp-empty-state">
                <div class="empty-icon">👤</div>
                <div class="empty-text">未登录网易云音乐</div>
                <div class="empty-hint">请先在终端执行：ncm-cli configure 和 ncm-cli login</div>
                <button class="btn btn-primary" id="ncm-login-btn" style="margin-top:12px;">我已登录</button>
            </div>`;
        document.getElementById('ncm-login-btn').addEventListener('click', () => this._loadCurrentView());
    }

    async openNeteasePlaylist(playlist) {
        if (!playlist) return;
        const seq = ++this._loadSeq;
        this.currentNeteasePlaylist = playlist;
        this._neteasePlaylistBackView = this.currentView;
        this.currentView = 'ncm-playlist-detail';
        this.currentAlbum = null;
        this.currentPlaylist = null;
        document.getElementById('media-search').value = '';
        document.getElementById('btn-search-clear').classList.add('hidden');
        const content = document.getElementById('media-content');
        if (!content) return;
        const cacheKey = 'ncm-playlist-detail-' + playlist.id;
        const cached = this._ncmCacheGet(cacheKey);
        const twoHours = 2 * 60 * 60 * 1000;
        if (cached && cached.ts && (Date.now() - cached.ts < twoHours) && cached.results) {
            if (seq !== this._loadSeq) return;
            const items = (cached.results || []).map(song => this._neteaseToMediaItem(song));
            this._renderDetail(items, {
                label: '歌单',
                title: playlist.name || '歌单',
                sub: `${playlist.track_count || 0} 首 · 播放 ${playlist.play_count || 0}`,
                cover: playlist.cover_url || '',
                kind: 'ncm-playlist',
            });
            return;
        }
        content.innerHTML = '<div class="mp-loading"><div class="mp-spinner"></div><div>加载歌单…</div></div>';
        try {
            const data = await Bridge.callPlugin('netease-music', 'get_playlist_tracks', playlist.id, 100, 0);
            if (seq !== this._loadSeq) return;
            this._ncmCacheSet(cacheKey, { ts: Date.now(), results: data.results || [] });
            const items = (data.results || []).map(song => this._neteaseToMediaItem(song));
            this._renderDetail(items, {
                label: '歌单',
                title: playlist.name || '歌单',
                sub: `${playlist.track_count || 0} 首 · 播放 ${playlist.play_count || 0}`,
                cover: playlist.cover_url || '',
                kind: 'ncm-playlist',
            });
        } catch (e) {
            content.innerHTML = '<div class="mp-empty-state"><div class="empty-icon">⚠️</div><div class="empty-text">歌单加载失败</div></div>';
        }
    }

    _renderNeteasePlaylists(playlists) {
        const content = document.getElementById('media-content');
        if (!playlists || !playlists.length) {
            this._renderEmpty('📋', '暂无推荐歌单');
            return;
        }
        content.innerHTML = `<div class="mp-list">${playlists.map((p, i) => `
            <div class="mp-row" data-idx="${i}">
                <span class="mp-row-index">${i + 1}</span>
                <div class="mp-row-cover">${p.cover_url ? `<img src="${MPUtils.escapeHtml(p.cover_url)}" onerror="this.outerHTML='📋'">` : '📋'}</div>
                <div class="mp-row-info">
                    <div class="mp-row-title">${MPUtils.escapeHtml(p.name)}</div>
                    <div class="mp-row-sub">${p.track_count} 首 · 播放 ${p.play_count}</div>
                </div>
            </div>`).join('')}</div>`;
        this._currentListData = playlists;
    }

    _bindContentDelegation() {
        document.getElementById('media-content').addEventListener('click', async (e) => {
            const playBtn = e.target.closest('.mp-card-play');
            if (playBtn) {
                e.stopPropagation();
                const items = await Bridge.call('media_album_items', playBtn.dataset.key, playBtn.dataset.kind);
                if (items && items.length) {
                    this.core.setQueue(items, 0);
                    Toast.info(`正在播放：${items[0].title}`);
                }
                return;
            }

            const card = e.target.closest('.mp-card');
            if (card) {
                this.openAlbum(card.dataset.key, card.dataset.kind);
                return;
            }

            const action = e.target.closest('.mp-row-action');
            if (action) {
                e.stopPropagation();
                const idx = parseInt(action.dataset.idx, 10);
                if (!isNaN(idx)) await this._handleRowAction(action.dataset.mpAction, idx);
                return;
            }

            const row = e.target.closest('.mp-row');
            if (row && !isNaN(parseInt(row.dataset.idx, 10))) {
                const idx = parseInt(row.dataset.idx, 10);
                if (this.currentView === 'ncm-playlists' || this.currentView === 'ncm-my-playlists') {
                    const playlist = this._currentListData[idx];
                    if (playlist) this.openNeteasePlaylist(playlist);
                    return;
                }
                if (this.currentView.startsWith('ncm-')) {
                    const item = this._currentListData[idx];
                    if (!item) return;
                    try {
                        const urlData = await Bridge.callPlugin('netease-music', 'get_song_url', item.ncm_encrypted_id || item.original_id, item.original_id);
                        if (!urlData || !urlData.url) {
                            Toast.error('获取播放地址失败');
                            return;
                        }
                        item.stream_url = urlData.url;
                    } catch (err) {
                        Toast.error('获取播放地址失败');
                        return;
                    }
                }
                this.core.setQueue(this._currentListData, idx);
            }
        });
    }

    async _handleRowAction(action, idx) {
        const item = this._currentListData[idx];
        if (!item) return;

        switch (action) {
            case 'fav': {
                try {
                    const result = await Bridge.call('media_toggle_favorite', item.id);
                    if (result.is_fav) this.favIds.add(item.id);
                    else this.favIds.delete(item.id);
                    item.is_fav = result.is_fav;
                    if (this.core.currentItem && this.core.currentItem.id === item.id) {
                        this._updateFavButton(item.id);
                    }
                    this._refreshRowState();
                } catch (err) {
                    Toast.error('操作失败');
                }
                break;
            }
            case 'queue':
                this.core.queue.push(item);
                this._renderQueue();
                Toast.info(`已加入队列：${item.title}`);
                break;
            case 'playlist':
                this._openAddToPlaylist(item);
                break;
            case 'remove':
                if (this.currentPlaylist) {
                    const ok = await this.playlists.removeItem(this.currentPlaylist.id, item.id);
                    if (ok) {
                        this.currentPlaylist.items = (this.currentPlaylist.items || []).filter(i => i.id !== item.id);
                        this._loadCurrentView();
                    }
                }
                break;
        }
    }

    _refreshRowState() {
        this._currentListData.forEach((item, index) => {
            const row = this._currentListContainer
                ? this._currentListContainer.querySelector(`.mp-row[data-idx="${index}"]`)
                : null;
            if (!row) return;
            const favBtn = row.querySelector('[data-mp-action="fav"]');
            if (favBtn) {
                const fav = this.favIds.has(item.id);
                favBtn.classList.toggle('fav-active', fav);
                favBtn.textContent = fav ? '❤️' : '♡';
                favBtn.title = fav ? '取消喜欢' : '喜欢';
            }
        });
    }

    // ============================================================
    // 渲染：专辑 / 歌单详情
    // ============================================================
    _renderDetail(items, header) {
        const content = document.getElementById('media-content');
        const coverUrl = header.cover ? MPUtils.coverUrl({ cover_path: header.cover }) : '';
        const totalDuration = items.reduce((sum, i) => sum + (i.duration || 0), 0);

        content.innerHTML = `
            <div class="mp-detail-hero" style="--hero-bg:${coverUrl ? `url("${coverUrl}")` : 'none'}">
                <button class="mp-ghost-btn mp-hero-back" data-hero-action="back" title="返回">← 返回</button>
                <div class="mp-detail-cover">${coverUrl ? MPUtils.coverImg(coverUrl, header.kind === 'video' ? '🎬' : '💿') : (header.kind === 'video' ? '🎬' : '💿')}</div>
                <div class="mp-detail-info">
                    <div class="mp-detail-label">${MPUtils.escapeHtml(header.label)}</div>
                    <div class="mp-detail-title">${MPUtils.escapeHtml(header.title)}</div>
                    <div class="mp-detail-sub">${MPUtils.escapeHtml(header.sub || '')}</div>
                    <div class="mp-detail-meta">${items.length} 个媒体 · ${MPUtils.fmtDuration(totalDuration)}</div>
                </div>
                <div class="mp-detail-actions">
                    <button class="btn btn-primary" data-hero-action="play-all">▶ 播放全部</button>
                    ${header.playlistId ? `
                        <button class="btn" data-hero-action="rename-pl">✎ 重命名</button>
                        <button class="btn btn-danger" data-hero-action="delete-pl">🗑 删除</button>
                    ` : ''}
                </div>
            </div>
            <div class="mp-list" id="mp-detail-list"></div>`;

        this._renderList(items, {
            showAlbum: true,
            showTag: true,
            playlistId: header.playlistId || '',
        }, content.querySelector('#mp-detail-list'));

        content.querySelectorAll('[data-hero-action]').forEach(btn => {
            btn.addEventListener('click', async () => {
                const action = btn.dataset.heroAction;
                if (action === 'back') {
                    if (header.kind === 'ncm-playlist') {
                        this.switchView(this._neteasePlaylistBackView || 'ncm-playlists');
                        return;
                    }
                    this.switchView(header.kind === 'video' ? 'video-albums' : (header.kind === 'playlist' ? 'recent' : 'audio-albums'));
                } else if (action === 'play-all') {
                    if (!items.length) return;
                    if (header.kind === 'ncm-playlist') {
                        // 立即开始播放，由 player-core 按需解析当前歌曲 URL；
                        // 剩余歌曲在后台预解析，避免阻塞 UI。
                        this.core.setQueue(items, 0);
                        this._prepareNeteaseItems(items.slice(1));
                    } else {
                        this.core.setQueue(items, 0);
                    }
                } else if (action === 'rename-pl' && header.playlistId) {
                    this._openPlaylistModal('rename', this.currentPlaylist);
                } else if (action === 'delete-pl' && header.playlistId) {
                    const ok = await confirmDialog(`确定删除歌单「${this.currentPlaylist.name}」？`, { danger: true });
                    if (ok) {
                        await this.playlists.delete(header.playlistId);
                        this.switchView('recent');
                    }
                }
            });
        });
    }

    // ============================================================
    // 播放状态反馈
    // ============================================================
    onTrackChange(item) {
        const stage = document.getElementById('mp-stage');
        const hasContent = !!item;
        const showVideo = !!(item && item.kind === 'video' && this.core.videoMode);

        stage.classList.toggle('has-content', hasContent);
        stage.classList.toggle('video-on', showVideo);

        // 舞台信息
        document.getElementById('mp-stage-title').textContent = item ? item.title : '未在播放';
        document.getElementById('mp-stage-sub').textContent = item
            ? `${item.artist || ''}${item.album ? ' · ' + item.album : ''}`
            : '从左侧选择音乐或视频开始';
        document.getElementById('mp-stage-kind').textContent = !item ? '现在播放' : (item.kind === 'video' ? (this.core.videoMode ? '视频播放' : '视频 · 仅声音') : '音乐播放');
        document.getElementById('mp-stage-meta').textContent = item ? MPUtils.formatTime(item.duration) : '';

        this._updateStageCover(item);
        this._updateStageBackdrop(item);

        document.getElementById('mp-video-hint-title').textContent = item ? item.title : '';

        // 底部播放栏
        document.getElementById('player-title').textContent = item ? item.title : '未在播放';
        document.getElementById('player-artist').textContent = item ? (item.artist || item.album || '') : '';
        this._updatePlayerCover(item);

        // 视频模式按钮
        const videoModeBtn = document.getElementById('btn-video-mode');
        const isVideo = !!(item && item.kind === 'video');
        videoModeBtn.classList.toggle('hidden', !isVideo);
        if (isVideo) {
            videoModeBtn.textContent = this.core.videoMode ? '🎬' : '🎵';
            videoModeBtn.title = this.core.videoMode ? '切换到仅声音' : '切换到画面';
        }

        const fsBtn = document.getElementById('btn-fullscreen');
        if (fsBtn) fsBtn.title = showVideo ? '全屏' : '沉浸歌词';

        // 收藏
        this._updateFavButton(item ? item.id : '');
        this._renderQueue();
        this._highlightRows(item);

        // 歌词
        if (item && item.kind === 'audio' && this.settings.lyrics_enabled !== false) {
            this.lyrics.loadForItem(item);
        }
        if (this.lyrics.isVisible()) {
            this.lyrics.applyBg(item);
            document.getElementById('lyrics-page-title').textContent = item ? item.title : '未在播放';
            if (!item || item.kind !== 'audio') this.lyrics.hide();
        }

        this.updateStageLyrics(this.lyrics.currentIndex);
        this._updateMiniEq(this._playing);
    }

    _updateStageCover(item) {
        const coverEl = document.getElementById('mp-stage-cover');
        const img = document.getElementById('mp-stage-cover-img');
        const url = item ? MPUtils.coverUrl(item) : '';
        if (url) {
            img.src = url;
            img.style.display = '';
            img.onerror = () => {
                img.style.display = 'none';
                coverEl.classList.add('no-cover');
                coverEl.dataset.icon = item ? MPUtils.itemIcon(item) : '🎵';
            };
            coverEl.classList.remove('no-cover');
        } else {
            img.removeAttribute('src');
            img.style.display = 'none';
            coverEl.classList.add('no-cover');
            coverEl.dataset.icon = item ? MPUtils.itemIcon(item) : '🎵';
        }
        coverEl.classList.toggle('spinning', !!(item && item.kind === 'audio' && this._playing));
        coverEl.classList.toggle('paused', !!(item && item.kind === 'audio' && !this._playing));
    }

    _updateStageBackdrop(item) {
        const backdrop = document.getElementById('mp-stage-backdrop');
        const url = item ? MPUtils.coverUrl(item) : '';
        if (url) {
            backdrop.style.backgroundImage = `url("${url}")`;
            backdrop.style.opacity = '0.5';
        } else {
            backdrop.style.backgroundImage = 'linear-gradient(135deg, #10101c, #1a1030)';
            backdrop.style.opacity = '1';
        }
    }

    _updatePlayerCover(item) {
        const cover = document.getElementById('player-cover');
        if (!cover) return;
        const url = item ? MPUtils.coverUrl(item) : '';
        cover.innerHTML = url
            ? MPUtils.coverImg(url, MPUtils.itemIcon(item))
            : MPUtils.itemIcon(item);
    }

    onPlayStateChange(playing) {
        this._playing = playing;
        const playBtn = document.getElementById('btn-play-pause');
        playBtn.textContent = playing ? '⏸' : '▶';
        playBtn.title = playing ? '暂停' : '播放';

        const stage = document.getElementById('mp-stage');
        stage.classList.toggle('playing', playing);
        const cover = document.getElementById('mp-stage-cover');
        const current = this.core.currentItem;
        const audioSpin = !!(current && current.kind === 'audio');
        cover.classList.toggle('spinning', audioSpin && playing);
        cover.classList.toggle('paused', audioSpin && !playing);

        // 视频中央播放按钮
        const bigBtn = document.getElementById('btn-stage-play');
        const showVideo = stage.classList.contains('video-on');
        bigBtn.classList.toggle('show', showVideo && !playing);
        bigBtn.textContent = playing ? '⏸' : '▶';

        this._updateMiniEq(playing);
        if (playing && this.isFullscreen) this._scheduleAutoHide();
        if (!playing && this.isFullscreen) this._showControls();
    }

    _updateMiniEq(playing) {
        document.getElementById('mp-mini-eq').classList.toggle('playing', !!playing);
    }

    onTimeUpdate() {
        const el = this.core.mediaElement;
        const progress = document.getElementById('progress-bar');
        if (el && el.duration && isFinite(el.duration)) {
            progress.max = el.duration;
            progress.value = el.currentTime;
            MPUtils.setRangePercent(progress, (el.currentTime / el.duration) * 100);
            document.getElementById('time-current').textContent = MPUtils.formatTime(el.currentTime);
            document.getElementById('time-duration').textContent = MPUtils.formatTime(el.duration);
        } else {
            progress.value = 0;
            MPUtils.setRangePercent(progress, 0);
            document.getElementById('time-current').textContent = '00:00';
            document.getElementById('time-duration').textContent = '00:00';
        }
        this.lyrics.update(el ? el.currentTime || 0 : 0);
    }

    updatePlayModeUI() {
        const btn = document.getElementById('btn-play-mode');
        if (!btn) return;
        const icons = ['🔁', '🔀', '🔂'];
        const titles = ['顺序播放', '随机播放', '单曲循环'];
        btn.textContent = icons[this.core.playMode] || '🔁';
        btn.title = titles[this.core.playMode] || '顺序播放';
    }

    updateVolumeUI() {
        const bar = document.getElementById('volume-bar');
        const icon = document.getElementById('btn-volume-icon');
        if (!bar) return;
        bar.value = this.core.volume;
        MPUtils.setRangePercent(bar, this.core.volume * 100);
        if (icon) {
            if (this.core._muted || this.core.volume === 0) icon.textContent = '🔇';
            else if (this.core.volume < 0.35) icon.textContent = '🔈';
            else if (this.core.volume < 0.7) icon.textContent = '🔉';
            else icon.textContent = '🔊';
        }
    }

    _updateFavButton(itemId) {
        const btn = document.getElementById('btn-toggle-fav');
        if (!btn) return;
        const fav = itemId && this.favIds.has(itemId);
        btn.textContent = fav ? '❤️' : '♡';
        btn.classList.toggle('fav-active', !!fav);
        btn.title = fav ? '取消喜欢' : '喜欢';
    }

    async _toggleCurrentFavorite() {
        const item = this.core.currentItem;
        if (!item) return;
        try {
            const result = await Bridge.call('media_toggle_favorite', item.id);
            if (result.is_fav) this.favIds.add(item.id);
            else this.favIds.delete(item.id);
            item.is_fav = result.is_fav;
            const btn = document.getElementById('btn-toggle-fav');
            btn.textContent = result.is_fav ? '❤️' : '♡';
            btn.classList.toggle('fav-active', result.is_fav);
            btn.classList.remove('fav-active');
            void btn.offsetWidth; // 重置动画
            if (result.is_fav) btn.classList.add('fav-active');
            this._refreshRowState();
        } catch (e) {
            Toast.error('操作失败');
        }
    }

    _highlightRows(item) {
        document.querySelectorAll('.mp-row').forEach(row => {
            row.classList.toggle('active', !!item && row.dataset.id === item.id);
        });
    }

    // ============================================================
    // 队列
    // ============================================================
    _toggleQueue() {
        const popup = document.getElementById('queue-popup');
        if (popup.classList.contains('hidden')) {
            this._renderQueue();
            popup.classList.remove('hidden');
        } else {
            popup.classList.add('hidden');
        }
    }

    _clearQueue() {
        this.core.queue = [];
        this.core.currentIndex = -1;
        this._renderQueue();
        Toast.info('播放队列已清空');
    }

    _renderQueue() {
        const list = document.getElementById('queue-list');
        const count = document.getElementById('queue-count');
        if (!list) return;
        count.textContent = this.core.queue.length;

        if (!this.core.queue.length) {
            list.innerHTML = '<div class="mp-queue-item" style="color:var(--text-muted);cursor:default;">播放队列为空</div>';
            return;
        }

        list.innerHTML = this.core.queue.map((item, idx) => {
            const active = idx === this.core.currentIndex;
            return `
                <div class="mp-queue-item ${active ? 'active' : ''}" data-queue-idx="${idx}">
                    <span class="q-index">${idx + 1}</span>
                    <span class="q-kind">${MPUtils.itemIcon(item)}</span>
                    <span class="q-title">${MPUtils.escapeHtml(item.title)}</span>
                    <button class="q-remove" data-remove-idx="${idx}" title="移除">✕</button>
                </div>`;
        }).join('');

        list.querySelectorAll('.mp-queue-item').forEach(row => {
            row.addEventListener('click', (e) => {
                const remove = e.target.closest('.q-remove');
                if (remove) {
                    e.stopPropagation();
                    const idx = parseInt(remove.dataset.removeIdx, 10);
                    this.core.queue.splice(idx, 1);
                    if (this.core.currentIndex >= this.core.queue.length) {
                        this.core.currentIndex = this.core.queue.length - 1;
                    }
                    this._renderQueue();
                    return;
                }
                const idx = parseInt(row.dataset.queueIdx, 10);
                if (!isNaN(idx)) {
                    this.core.playIndex(idx);
                    document.getElementById('queue-popup').classList.add('hidden');
                }
            });
        });
    }

    // ============================================================
    // 歌单交互
    // ============================================================
    _openPlaylistModal(mode = 'create', pl = null, keepTarget = false) {
        this._playlistModalMode = mode;
        this._playlistModalId = pl ? pl.id : '';
        this._playlistKeepTarget = keepTarget;
        const title = document.getElementById('playlist-modal-title');
        const input = document.getElementById('input-playlist-name');
        const confirm = document.getElementById('btn-playlist-confirm');
        title.textContent = mode === 'rename' ? '重命名歌单' : '新建歌单';
        confirm.textContent = mode === 'rename' ? '保存' : '创建';
        input.value = mode === 'rename' && pl ? pl.name : '';
        MPUtils.openModal('modal-playlist');
    }

    async _confirmPlaylistModal() {
        const input = document.getElementById('input-playlist-name');
        const name = input.value.trim();
        if (!name) {
            Toast.error('请输入歌单名称');
            return;
        }
        MPUtils.closeModal('modal-playlist');

        if (this._playlistModalMode === 'rename') {
            await this.playlists.rename(this._playlistModalId, name);
            if (this.currentPlaylist && this.currentPlaylist.id === this._playlistModalId) {
                this.currentPlaylist.name = name;
                this._loadCurrentView();
            }
            return;
        }

        const pl = await this.playlists.create(name);
        if (pl && this._playlistKeepTarget && this._addItemTarget) {
            await this.playlists.addItems(pl.id, [this._addItemTarget.id]);
            Toast.success(`已加入歌单「${name}」`);
            this._addItemTarget = null;
            this._playlistKeepTarget = false;
        }
    }

    _openAddToPlaylist(item) {
        this._addItemTarget = item;
        const list = document.getElementById('add-pl-list');
        MPUtils.openModal('modal-add-to-playlist');

        if (!this.playlists.playlists.length) {
            list.innerHTML = '<div class="mp-playlist-empty">还没有歌单，点击下方按钮创建</div>';
            return;
        }

        list.innerHTML = this.playlists.playlists.map(pl => `
            <div class="mp-add-pl-item" data-pl-id="${pl.id}">
                <span>📋</span>
                <span>${MPUtils.escapeHtml(pl.name)}</span>
                <span class="pl-count">${(pl.item_ids || []).length} 项</span>
            </div>`).join('');

        list.querySelectorAll('.mp-add-pl-item').forEach(row => {
            row.addEventListener('click', async () => {
                const id = row.dataset.plId;
                const ok = await this.playlists.addItems(id, [item.id]);
                if (ok) {
                    Toast.success('已加入歌单');
                    MPUtils.closeModal('modal-add-to-playlist');
                    this._addItemTarget = null;
                }
            });
        });
    }

    showPlaylistMenu(playlistId, e) {
        this._playlistMenuId = playlistId;
        this._closePlaylistMenu();

        const menu = document.createElement('div');
        menu.className = 'mp-context-menu';
        menu.id = 'mp-context-menu';
        menu.innerHTML = `
            <button data-menu-act="rename">✎ 重命名</button>
            <button data-menu-act="delete" class="danger">🗑 删除歌单</button>`;
        menu.style.left = `${Math.min(e.clientX, window.innerWidth - 150)}px`;
        menu.style.top = `${Math.min(e.clientY, window.innerHeight - 100)}px`;
        document.body.appendChild(menu);
        this._contextMenuEl = menu;

        menu.addEventListener('click', async (ev) => {
            const act = ev.target.dataset.menuAct;
            this._closePlaylistMenu();
            const pl = this.playlists.playlists.find(p => p.id === playlistId);
            if (!pl) return;
            if (act === 'rename') {
                this._openPlaylistModal('rename', pl);
            } else if (act === 'delete') {
                const ok = await confirmDialog(`确定删除歌单「${pl.name}」？`, { danger: true });
                if (ok) {
                    await this.playlists.delete(playlistId);
                    if (this.currentPlaylist && this.currentPlaylist.id === playlistId) {
                        this.switchView('recent');
                    }
                }
            }
        });
    }

    _closePlaylistMenu() {
        if (this._contextMenuEl) {
            this._contextMenuEl.remove();
            this._contextMenuEl = null;
        }
        this._playlistMenuId = '';
    }

    // ============================================================
    // 均衡器
    // ============================================================
    _toggleEQ() {
        const panel = document.getElementById('eq-panel');
        const willOpen = panel.classList.contains('hidden');
        panel.classList.toggle('hidden', !willOpen);
        if (willOpen) this._buildEQBands();
    }

    _hideEQ() {
        document.getElementById('eq-panel').classList.add('hidden');
    }

    _buildEQBands() {
        const container = document.getElementById('eq-bands');
        if (!container) return;
        this.core.ensureAudioGraph();
        const bands = this.core.getEqBands();

        container.innerHTML = this.core.EQ_FREQS.map((freq, i) => {
            const label = freq >= 1000 ? `${(freq / 1000).toFixed(0)}K` : String(freq);
            const gain = bands[i] || 0;
            const percent = ((gain + 12) / 24) * 100;
            return `
                <div class="mp-eq-band">
                    <span class="mp-eq-value" data-eq-val="${i}">${gain > 0 ? '+' + gain : gain}dB</span>
                    <input type="range" class="mp-eq-slider" min="-12" max="12" step="1" value="${gain}"
                           data-eq-idx="${i}" style="--range-val:${percent}%">
                    <span class="mp-eq-label">${label}</span>
                </div>`;
        }).join('');

        container.querySelectorAll('.mp-eq-slider').forEach(slider => {
            slider.addEventListener('input', () => {
                const idx = parseInt(slider.dataset.eqIdx, 10);
                const gain = parseFloat(slider.value);
                this.core.setEqBand(idx, gain);
                const percent = ((gain + 12) / 24) * 100;
                MPUtils.setRangePercent(slider, percent);
                const valueEl = container.querySelector(`[data-eq-val="${idx}"]`);
                if (valueEl) valueEl.textContent = (gain > 0 ? '+' + gain : gain) + 'dB';
                try {
                    localStorage.setItem('omniboxMediaEQ', JSON.stringify(this.core.getEqBands()));
                } catch (e) { }
            });
        });
    }

    _resetEQ() {
        this.core.resetEq();
        this._buildEQBands();
        document.getElementById('eq-preset-select').value = '';
    }

    async _loadEQPresets() {
        const select = document.getElementById('eq-preset-select');
        if (!select) return;
        try {
            const presets = await Bridge.call('media_list_eq_presets') || [];
            select.innerHTML = '<option value="">内置预设</option>';
            presets.forEach(p => {
                const opt = document.createElement('option');
                opt.value = p.id;
                opt.textContent = p.name;
                select.appendChild(opt);
            });
        } catch (e) { }
    }

    async _applyEQPreset(id) {
        if (!id) {
            this._resetEQ();
            return;
        }
        try {
            const presets = await Bridge.call('media_list_eq_presets') || [];
            const preset = presets.find(p => p.id === id);
            if (preset && Array.isArray(preset.bands)) {
                this.core.applyEqBands(preset.bands);
                this._buildEQBands();
            }
        } catch (e) {
            Toast.error('加载预设失败');
        }
    }

    async _confirmEQName() {
        const name = document.getElementById('input-eq-name').value.trim();
        if (!name) {
            Toast.error('请输入预设名称');
            return;
        }
        MPUtils.closeModal('modal-eq-name');
        document.getElementById('input-eq-name').value = '';
        try {
            const result = await Bridge.call('media_save_eq_preset', name, this.core.getEqBands());
            if (result.success) {
                Toast.success('预设已保存');
                await this._loadEQPresets();
            } else {
                Toast.error(result.error || '保存失败');
            }
        } catch (e) {
            Toast.error('保存失败');
        }
    }

    // ============================================================
    // 歌词 / 视频模式
    // ============================================================
    // 未全屏时的迷你歌词：显示当前句前后各两句
    updateStageLyrics(index) {
        const el = document.getElementById('mp-stage-lyrics');
        if (!el) return;
        const item = this.core.currentItem;
        const canShow = item && (item.kind === 'audio' || !this.core.videoMode)
            && this.settings.lyrics_enabled !== false
            && !this.lyrics.isVisible();

        if (!canShow) {
            el.classList.remove('show');
            el.innerHTML = '';
            return;
        }

        const lines = this.lyrics.lines || [];
        if (!lines.length) {
            el.classList.add('show');
            el.innerHTML = '<div class="mp-stage-lyric-empty">暂无歌词 · 点击打开歌词页</div>';
            return;
        }

        let idx = Number.isInteger(index) && index >= 0 ? index : this.lyrics.currentIndex;
        if (idx < 0) idx = 0;
        const start = Math.max(0, idx - 2);
        const end = Math.min(lines.length - 1, idx + 2);
        let html = '';
        for (let i = start; i <= end; i++) {
            const cls = i === idx ? 'current'
                : (i < idx ? (i === idx - 1 ? 'prev1' : 'prev2')
                    : (i === idx + 1 ? 'next1' : 'next2'));
            html += `<div class="mp-stage-lyric ${cls}">${MPUtils.escapeHtml(lines[i].text)}</div>`;
        }
        el.innerHTML = html;
        el.classList.add('show');
    }

    // 宽屏模式：隐藏下方媒体列表，舞台铺满主区域（左侧导航保留）
    _toggleWideMode() {
        if (!this.core.currentItem) {
            Toast.info('请先播放一个媒体');
            return;
        }
        const appEl = document.getElementById('app');
        const btn = document.getElementById('btn-wide');
        const active = appEl.classList.toggle('wide-mode');
        btn.classList.toggle('wide-active', active);
        btn.title = active ? '恢复普通模式' : '宽屏模式';
        Toast.info(active ? '宽屏模式已开启' : '已恢复普通模式');
    }

    _toggleLyrics() {
        const item = this.core.currentItem;
        if (!item) return;
        if (this.settings.lyrics_enabled === false) {
            Toast.info('歌词显示已在设置中关闭');
            return;
        }
        if (item.kind !== 'audio' && this.core.videoMode) {
            Toast.info('仅声音模式下可查看歌词');
            return;
        }
        this.lyrics.toggle(item, item.title);
    }

    _toggleVideoMode() {
        const item = this.core.currentItem;
        if (!item || item.kind !== 'video') return;
        this.core.setVideoMode(!this.core.videoMode);
        const btn = document.getElementById('btn-video-mode');
        btn.textContent = this.core.videoMode ? '🎬' : '🎵';
        btn.title = this.core.videoMode ? '切换到仅声音' : '切换到画面';
        Bridge.call('media_set_config', 'default_video_mode', this.core.videoMode ? 'video' : 'audio').catch(() => { });
    }

    _isVideoShowing() {
        return document.getElementById('mp-stage').classList.contains('video-on');
    }

    // ============================================================
    // 全屏
    // ============================================================
    toggleFullscreen() {
        const item = this.core.currentItem;
        if (!item) {
            Toast.info('请先播放一个媒体');
            return;
        }
        // 音乐（或视频的“仅声音”模式）的全屏就是沉浸式歌词页
        if (!this._isVideoShowing()) {
            this._toggleLyrics();
            return;
        }
        if (this.isFullscreen) this._exitFullscreen();
        else this._enterFullscreen();
    }

    _enterFullscreen() {
        this.isFullscreen = true;
        const stage = document.getElementById('mp-stage');
        stage.classList.add('fs-on');
        this._showControls();

        try {
            parent.document.documentElement.setAttribute('data-video-fullscreen', 'true');
        } catch (e) { }
        try {
            if (stage.requestFullscreen) stage.requestFullscreen().catch(() => { });
        } catch (e) { }
        try {
            const api = parent.pywebview && parent.pywebview.api;
            if (api && api.system_toggle_fullscreen) api.system_toggle_fullscreen();
        } catch (e) { }

        this._bindFsMove();
        if (this.settings.auto_hide_enabled !== false) this._scheduleAutoHide();
    }

    _exitFullscreen() {
        this.isFullscreen = false;
        const stage = document.getElementById('mp-stage');
        stage.classList.remove('fs-on', 'controls-hidden');
        this._showControls();
        this._stopAutoHide();

        try {
            parent.document.documentElement.removeAttribute('data-video-fullscreen');
        } catch (e) { }
        try {
            if (document.fullscreenElement && document.exitFullscreen) {
                document.exitFullscreen().catch(() => { });
            }
        } catch (e) { }
        try {
            const api = parent.pywebview && parent.pywebview.api;
            if (api && api.system_toggle_fullscreen) api.system_toggle_fullscreen();
        } catch (e) { }
    }

    _bindFsMove() {
        if (this.fsMoveHandler) document.removeEventListener('mousemove', this.fsMoveHandler);
        this.fsMoveHandler = () => {
            this._showControls();
            if (this.settings.auto_hide_enabled !== false) this._scheduleAutoHide();
        };
        document.addEventListener('mousemove', this.fsMoveHandler);
    }

    _stopAutoHide() {
        if (this.fsMoveHandler) {
            document.removeEventListener('mousemove', this.fsMoveHandler);
            this.fsMoveHandler = null;
        }
        if (this.hideTimer) {
            clearTimeout(this.hideTimer);
            this.hideTimer = null;
        }
    }

    _scheduleAutoHide() {
        if (this.hideTimer) clearTimeout(this.hideTimer);
        const delay = Math.max(1, (this.settings.auto_hide_delay ?? 3) || 1) * 1000;
        this.hideTimer = setTimeout(() => {
            const el = this.core.mediaElement;
            if (this.isFullscreen && el && !el.paused) this._hideControls();
        }, delay);
    }

    _showControls() {
        this.controlsVisible = true;
        document.getElementById('mp-stage').classList.remove('controls-hidden');
    }

    _hideControls() {
        this.controlsVisible = false;
        document.getElementById('mp-stage').classList.add('controls-hidden');
    }

    // ============================================================
    // 键盘快捷键
    // ============================================================
    _bindKeyboard() {
        document.addEventListener('keydown', (e) => {
            if (e.target.tagName === 'INPUT' || e.target.tagName === 'TEXTAREA' || e.target.tagName === 'SELECT') return;

            switch (e.key) {
                case ' ':
                    e.preventDefault();
                    this.core.togglePlay();
                    break;
                case 'ArrowLeft':
                    e.preventDefault();
                    this.core.seekDelta(-5);
                    break;
                case 'ArrowRight':
                    e.preventDefault();
                    this.core.seekDelta(5);
                    break;
                case 'ArrowUp':
                    e.preventDefault();
                    this.core.volume = Math.min(1, this.core.volume + 0.05);
                    break;
                case 'ArrowDown':
                    e.preventDefault();
                    this.core.volume = Math.max(0, this.core.volume - 0.05);
                    break;
                case 'n':
                case 'N':
                    e.preventDefault();
                    this.core.next(false);
                    break;
                case 'p':
                case 'P':
                    e.preventDefault();
                    this.core.prev();
                    break;
                case 'm':
                case 'M':
                    e.preventDefault();
                    this.core.toggleMute();
                    break;
                case 'l':
                case 'L':
                    e.preventDefault();
                    this._toggleLyrics();
                    break;
                case 'f':
                case 'F':
                    e.preventDefault();
                    this.toggleFullscreen();
                    break;
                case 'Escape':
                    if (this.isFullscreen) {
                        this._exitFullscreen();
                        return;
                    }
                    if (this.lyrics.isVisible()) this.lyrics.hide();
                    document.getElementById('queue-popup').classList.add('hidden');
                    document.getElementById('eq-panel').classList.add('hidden');
                    document.querySelectorAll('.mp-modal.active').forEach(m => m.classList.remove('active'));
                    this._closePlaylistMenu();
                    break;
            }
        });
    }
}
