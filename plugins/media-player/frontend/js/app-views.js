// ============================================================
// OmniBox 媒体播放器 — 扫描 / 设置 / 视图切换与加载
//
// 本文件是 MediaPlayerApp.prototype 的一个分片：类骨架与构造函数在 app.js，
// 这里用 Object.assign 把方法扩回同一个原型。**加载顺序是硬约束** —— 必须在
// app.js 之后、实例化之前（见 index.html），契约由 tests/js/media_player_app_split.mjs 把关。
// ============================================================

Object.assign(MediaPlayerApp.prototype, {

    // ============================================================
    // 扫描 / 设置
    // ============================================================
    async _doScan(deep = false) {
        if (this._scanning) return;
        this._scanning = true;
        const btn = document.getElementById('btn-scan');
        const deepBtn = document.getElementById('btn-deep-scan');
        btn.disabled = true;
        if (deepBtn) deepBtn.disabled = true;
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
        this._setLoading(deep
            ? '正在深度扫描媒体库…<br>将重新读取所有媒体元数据，耗时较长'
            : '正在扫描媒体库…');
        try {
            const started = await Bridge.call('media_scan', !!deep);
            if (started && started.error) {
                // 扫描已在运行（如另一标签页）：改为等待其完成
                const st = await this._waitScanDone();
                if (st && st.state === 'done') {
                    const ex = st.extra || {};
                    Toast.success(`扫描完成：音乐 ${ex.audio ?? '?'} · 视频 ${ex.video ?? '?'}`);
                }
            } else if (started) {
                const st = await this._waitScanDone((s) => {
                    if (s.state === 'running') {
                        this._setLoading(`正在扫描媒体库… ${s.processed}/${s.total}${s.current ? ' · ' + s.current : ''}`);
                    }
                });
                const ex = (st && st.extra) || {};
                if (st && st.state === 'cancelled') Toast.info('扫描已取消，已完成部分已保留');
                else if (st && st.state === 'done') Toast.success(`扫描完成：音乐 ${ex.audio ?? '?'} · 视频 ${ex.video ?? '?'}`);
                else if (!st) Toast.error('扫描超时，请稍后重试');
            }
            await this._updateStats();
        } catch (e) {
            Toast.error('扫描失败');
        } finally {
            this._scanning = false;
            btn.disabled = false;
            btn.textContent = '🔄 扫描';
            const deepBtn = document.getElementById('btn-deep-scan');
            if (deepBtn) deepBtn.disabled = false;
            await this._loadCurrentView();
        }
    },

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
    },

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
    },

    async openAlbum(key, kind) {
        this.currentView = 'album-detail';
        this.currentPlaylist = null;
        this.playlists.currentId = '';
        this.currentAlbum = { key, kind };
        document.querySelectorAll('.mp-nav-item').forEach(b => b.classList.remove('active'));
        this.playlists.renderSidebar();
        await this._loadCurrentView();
    },

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
    },

    _ncmCacheGet(key) {
        try {
            const raw = localStorage.getItem('ncmCache_' + key);
            if (raw) return JSON.parse(raw);
        } catch (e) { }
        return null;
    },

    _ncmCacheSet(key, data) {
        try {
            localStorage.setItem('ncmCache_' + key, JSON.stringify(data));
        } catch (e) { }
    },

    _isSameDay(dateStr) {
        if (!dateStr) return false;
        try {
            const d = new Date(dateStr);
            const now = new Date();
            return d.getFullYear() === now.getFullYear() && d.getMonth() === now.getMonth() && d.getDate() === now.getDate();
        } catch (e) {
            return false;
        }
    },

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
    },

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
    },

    async _loadCurrentView() {
        const seq = ++this._loadSeq;
        const keyword = (document.getElementById('media-search').value || '').trim();
        const titleEl = document.getElementById('mp-view-title');
        const subEl = document.getElementById('mp-view-sub');
        const scanBtn = document.getElementById('btn-scan');
        if (scanBtn) scanBtn.textContent = this.currentView.startsWith('ncm-') ? '🔄 刷新' : '🔄 扫描';
        const deepScanBtn = document.getElementById('btn-deep-scan');
        if (deepScanBtn) deepScanBtn.classList.toggle('hidden', this.currentView.startsWith('ncm-'));
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
                // 先确认 ncm-cli 是否可用，避免在 Windows 等环境里直接卡死
                try {
                    const status = await Bridge.callPlugin('netease-music', 'get_status');
                    if (seq !== this._loadSeq) return;
                    if (status && status.ncm_cli_available === false) {
                        this._renderNeteaseCliMissing(status);
                        return;
                    }
                } catch (e) { }
                if (this.currentView === 'ncm-login') {
                    await this._renderNeteaseLogin();
                    return;
                }
                if (keyword && this.currentView === 'ncm-playlists') {
                    const ncm = await Bridge.callPlugin('netease-music', 'search_playlist', keyword);
                    if (seq !== this._loadSeq) return;
                    this._renderNeteasePlaylists(ncm.results || [], '没有匹配的歌单');
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
                        if (cached && (cached.results || []).length && this._isSameDay(cached.date)) {
                            data = (cached.results || []).map(song => this._neteaseToMediaItem(song));
                            if (seq !== this._loadSeq) return;
                            this._renderList(data, { showAlbum: true, showTag: true });
                            return;
                        }
                        const ncm = await Bridge.callPlugin('netease-music', 'get_daily_recommend');
                        if (seq !== this._loadSeq) return;
                        // 与「我的歌单」同一个坑：失败返回的空结果被按日缓存，当天就再也刷不出来
                        if (ncm && ncm.success === false) {
                            this._renderEmpty('⚠️', '每日推荐加载失败',
                                ncm.error || '请先在「登录」中完成网易云登录');
                            return;
                        }
                        if ((ncm.results || []).length) {
                            this._ncmCacheSet(cacheKey, { date: new Date().toISOString(), results: ncm.results });
                        }
                        data = (ncm.results || []).map(song => this._neteaseToMediaItem(song));
                        this._renderList(data, { showAlbum: true, showTag: true });
                        return;
                    }
                    case 'ncm-playlists': {
                        const cacheKey = 'ncm-playlists';
                        const cached = this._ncmCacheGet(cacheKey);
                        const twoHours = 2 * 60 * 60 * 1000;
                        // 空结果不算命中：一次网络失败会把「空」缓存住，之后一直显示空列表
                        if (cached && (cached.results || []).length && cached.ts && (Date.now() - cached.ts < twoHours)) {
                            if (seq !== this._loadSeq) return;
                            this._renderNeteasePlaylists(cached.results || []);
                            return;
                        }
                        const ncm = await Bridge.callPlugin('netease-music', 'search_playlist', '推荐');
                        if (seq !== this._loadSeq) return;
                        if ((ncm.results || []).length) {
                            this._ncmCacheSet(cacheKey, { ts: Date.now(), results: ncm.results });
                        }
                        this._renderNeteasePlaylists(ncm.results || []);
                        return;
                    }
                    case 'ncm-liked': {
                        const cacheKey = 'ncm-liked';
                        const cached = this._ncmCacheGet(cacheKey);
                        if (cached && (cached.results || []).length) {
                            data = (cached.results || []).map(song => this._neteaseToMediaItem(song));
                            if (seq !== this._loadSeq) return;
                            this._renderList(data, { showAlbum: true, showTag: true });
                            return;
                        }
                        const ncm = await Bridge.callPlugin('netease-music', 'get_liked_songs', 100);
                        if (seq !== this._loadSeq) return;
                        if (ncm && ncm.success === false) {
                            this._renderEmpty('⚠️', '喜欢列表加载失败',
                                ncm.error || '请先在「登录」中完成网易云登录');
                            return;
                        }
                        if ((ncm.results || []).length) {
                            this._ncmCacheSet(cacheKey, { results: ncm.results });
                        }
                        data = (ncm.results || []).map(song => this._neteaseToMediaItem(song));
                        this._renderList(data, { showAlbum: true, showTag: true });
                        return;
                    }
                    case 'ncm-my-playlists': {
                        const cacheKey = 'ncm-my-playlists';
                        const cached = this._ncmCacheGet(cacheKey);
                        if (cached && (cached.results || []).length) {
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
                        const settled = [created, collected].map(r => (r.status === 'fulfilled' ? r.value : null));
                        add(settled[0] && settled[0].results);
                        add(settled[1] && settled[1].results);
                        const results = Array.from(map.values());
                        const failed = settled.filter(v => v && v.success === false);
                        // 两个接口都失败才是失败：单边失败仍可能是「创建 0 个 + 收藏若干」
                        if (!results.length && failed.length) {
                            this._renderEmpty('⚠️', '我的歌单加载失败',
                                failed[0].error || '请先在「登录」中完成网易云登录');
                            return;
                        }
                        if (results.length) this._ncmCacheSet(cacheKey, { results });
                        this._renderNeteasePlaylists(results, '暂无我的歌单');
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
                        cover: this._findAlbumCover(album.key) || items.find(i => i.has_cover) || items[0] || '',
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
                        cover: (pl.items || []).find(i => i.has_cover) || (pl.items || [])[0] || '',
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
    },

    _findAlbumName(key) {
        const album = this._lastAlbums.find(a => a.key === key);
        return album ? album.name : '';
    },

    _findAlbumCover(key) {
        const album = this._lastAlbums.find(a => a.key === key);
        // 返回 item 形态的封面引用（后端 albums 提供 cover_item_id，前端走 /thumbs）
        if (!album || !album.cover_item_id) return '';
        return { id: album.cover_item_id, has_cover: true };
    },

});
