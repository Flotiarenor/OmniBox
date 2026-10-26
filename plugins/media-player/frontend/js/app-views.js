// ============================================================
// OmniBox 媒体播放器 — 扫描 / 设置 / 视图切换与加载
//
// 本文件是 MediaPlayerApp.prototype 的一个分片：类骨架与构造函数在 app.js，
// 这里用 Object.assign 把方法扩回同一个原型。**加载顺序是硬约束** —— 必须在
// app.js 之后、实例化之前（见 index.html），契约由 tests/js/media_player_app_split.mjs 把关。
// ============================================================

// 「我的歌单」缓存有效期，与歌单详情的 2h 一致。没有 TTL 时，别的设备增删歌单后
// 本机只能靠顶栏「刷新」或重启才看到；v2 缓存缺 ts（旧版本写入）同样按过期处理。
const NCM_MY_PLAYLISTS_TTL_MS = 2 * 60 * 60 * 1000;

Object.assign(MediaPlayerApp.prototype, {

    // ============================================================
    // 扫描 / 设置
    // ============================================================
    /**
     * 顶栏「刷新」：网易云视图的视图级刷新 = 丢弃在线数据缓存后重新拉取。
     *
     * 媒体库本地的刷新（同步 / 校验）由壳的 `PluginChrome` 挂载
     * `shell/freshness.js`，这里只保留视图级刷新 —— 两者语义不同：一个重拉
     * 远端数据，一个核对本地磁盘。本地视图上这个动作是空操作（按钮也不可见）。
     */
    async _doScan() {
        if (this._scanning) return;
        if (!this.currentView.startsWith('ncm-')) return;
        this._scanning = true;
        this._updateActions();
        this._setLoading('正在刷新网易云数据…');
        this._clearNeteaseCache();
        try {
            await this._loadCurrentView();
            Toast.success('网易云数据已刷新');
        } catch (e) {
            Toast.error('刷新失败');
        } finally {
            this._scanning = false;
            this._updateActions();
            this._pushChrome();
        }
    },

    // ============================================================
    // 视图切换与加载
    // ============================================================
    /** 切到壳左栏「媒体库」组的一级视图。导航高亮由壳按上报的 view 处理，内容页
        只维护当前视图与歌单导航组。搜索词是全局的，切视图时连同壳的输入框一起清掉。 */
    async switchView(view) {
        // 只有视图真的变了才清：壳在加载/握手时会重发当前视图，那不是用户切分类。
        const changed = this.currentView !== view;
        this.currentView = view;
        this.currentAlbum = null;
        this.currentPlaylist = null;
        this.playlists.currentId = '';
        if (changed) this._clearSearch();
        this._updateActions();
        this.playlists.syncNav();
        await this._loadCurrentView();
    },

    /** 专辑详情是二级页：上报空 view，壳取消导航高亮。 */
    async openAlbum(key, kind) {
        this.currentView = 'album-detail';
        this.currentPlaylist = null;
        this.playlists.currentId = '';
        this.currentAlbum = { key, kind };
        this._clearSearch();
        this._updateActions();
        this.playlists.syncNav();
        await this._loadCurrentView();
    },

    /** 歌单详情：上报 `playlist:<id>`，壳据此高亮「我的歌单」组里的对应项。 */
    async openPlaylist(playlistId) {
        const pl = await this.playlists.get(playlistId);
        if (!pl) {
            Toast.error('歌单不存在');
            this._pushChrome();
            return;
        }
        this.currentView = 'playlist';
        this.currentAlbum = null;
        this.currentPlaylist = pl;
        this.playlists.currentId = playlistId;
        this._clearSearch();
        this._updateActions();
        this.playlists.syncNav();
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
        // 详情页不参与搜索：搜索词留在壳的输入框里，只在一级视图生效
        const detail = ['album-detail', 'playlist', 'ncm-playlist-detail'].includes(this.currentView);
        const keyword = detail ? '' : this.searchQuery;
        this._updateActions();
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
        if (viewsTitle[this.currentView]) {
            this._chromeTitle = viewsTitle[this.currentView][0];
            this._chromeSub = viewsTitle[this.currentView][1];
        }

        try {
            let data = [];

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
                    this._chromeTitle = `搜索 “${keyword}”`;
                    if (ncm && ncm.success === false) {
                        this._chromeSub = '加载失败';
                        this._renderNcmError(ncm.error);
                        return;
                    }
                    const results = ncm && Array.isArray(ncm.results) ? ncm.results : [];
                    if (!results.length) {
                        this._chromeSub = '0 个歌单';
                        this._renderNcmState('search', {
                            icon: 'icon:search',
                            title: '没有匹配的歌单',
                            hint: '换个关键词试试。',
                        });
                        return;
                    }
                    this._chromeSub = `${results.length} 个歌单`;
                    this._renderNeteasePlaylists(results);
                    return;
                }
                if (keyword) {
                    const ncm = await Bridge.callPlugin('netease-music', 'search_song', keyword);
                    if (seq !== this._loadSeq) return;
                    this._chromeTitle = `搜索 “${keyword}”`;
                    if (ncm && ncm.success === false) {
                        this._chromeSub = '加载失败';
                        this._renderNcmError(ncm.error);
                        return;
                    }
                    data = ((ncm && ncm.results) || []).map(song => this._neteaseToMediaItem(song));
                    this._chromeSub = `${data.length} 个结果`;
                    if (!data.length) {
                        this._renderNcmState('search', {
                            icon: 'icon:search',
                            title: '没有找到匹配的歌曲',
                            hint: '换个关键词试试。',
                        });
                        return;
                    }
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
                        // 失败与“空推荐”必须分开：失败是错误态（可重试），空推荐是真正的空白。
                        if (ncm && ncm.success === false) {
                            this._renderNcmError(ncm.error);
                            return;
                        }
                        const results = (ncm && Array.isArray(ncm.results)) ? ncm.results : [];
                        if (!results.length) {
                            this._renderNcmState('empty', {
                                icon: 'icon:music',
                                title: '今日推荐为空',
                                hint: '网易云暂时没有返回推荐内容。',
                            });
                            return;
                        }
                        this._ncmCacheSet(cacheKey, { date: new Date().toISOString(), results });
                        data = results.map(song => this._neteaseToMediaItem(song));
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
                        if (ncm && ncm.success === false) {
                            this._renderNcmError(ncm.error);
                            return;
                        }
                        const results = (ncm && Array.isArray(ncm.results)) ? ncm.results : [];
                        if (!results.length) {
                            this._renderNcmState('empty', {
                                icon: 'icon:list-music',
                                title: '暂无推荐歌单',
                                hint: '网易云暂时没有返回推荐歌单。',
                            });
                            return;
                        }
                        this._ncmCacheSet(cacheKey, { ts: Date.now(), results });
                        this._renderNeteasePlaylists(results);
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
                            this._renderNcmError(ncm.error);
                            return;
                        }
                        const results = (ncm && Array.isArray(ncm.results)) ? ncm.results : [];
                        if (!results.length) {
                            this._renderNcmState('empty', {
                                icon: 'icon:heart',
                                title: '还没有喜欢的歌曲',
                                hint: '在网易云点亮红心后，这里会同步显示。',
                            });
                            return;
                        }
                        this._ncmCacheSet(cacheKey, { results });
                        data = results.map(song => this._neteaseToMediaItem(song));
                        this._renderList(data, { showAlbum: true, showTag: true });
                        return;
                    }
                    case 'ncm-my-playlists': {
                        const cacheKey = 'ncm-my-playlists';
                        const cached = this._ncmCacheGet(cacheKey);
                        // v2：旧缓存没有 origin，认了会渲染成不分段的平表（读到就该重拉）。
                        // ts：超过 2h 或缺少时间戳都视为过期，避免别的设备改完歌单后本机一直看旧列表。
                        const cachedFresh = cached && cached.v === 2
                            && Array.isArray(cached.results) && cached.results.length
                            && Number.isFinite(Number(cached.ts))
                            && Date.now() - Number(cached.ts) < NCM_MY_PLAYLISTS_TTL_MS;
                        if (cachedFresh) {
                            if (seq !== this._loadSeq) return;
                            this._renderNeteasePlaylists(cached.results);
                            return;
                        }
                        const [created, collected] = await Promise.allSettled([
                            Bridge.callPlugin('netease-music', 'get_created_playlists', 100),
                            Bridge.callPlugin('netease-music', 'get_collected_playlists', 100),
                        ]);
                        if (seq !== this._loadSeq) return;
                        const map = new Map();
                        // 标出来源：创建 / 收藏要分得清（列表分段，全量同步只默认同步创建的）
                        const add = (arr, origin) => (arr || []).forEach(p => {
                            if (p && p.id && !map.has(p.id)) map.set(p.id, { ...p, origin });
                        });
                        const settled = [created, collected].map(r => (r.status === 'fulfilled' ? r.value : null));
                        add(settled[0] && settled[0].results, 'created');
                        add(settled[1] && settled[1].results, 'collected');
                        const results = Array.from(map.values())
                            .sort((a, b) => (a.origin === b.origin ? 0 : (a.origin === 'created' ? -1 : 1)));
                        const failed = settled.filter(v => v && v.success === false);
                        // 两个接口都失败才是失败：单边失败仍可能是「创建 0 个 + 收藏若干」
                        if (!results.length) {
                            if (failed.length) {
                                this._renderNcmError(failed[0].error);
                                return;
                            }
                            this._renderNcmState('empty', {
                                icon: 'icon:list-music',
                                title: '还没有创建或收藏的歌单',
                                hint: '网易云账号下暂时没有歌单。',
                            });
                            return;
                        }
                        // 单边失败的列表仍要显示（总比整页错误强），但不能写缓存：
                        // 写进去后缺的那半边会一直缺到过期（2h）或用户手动刷新。
                        if (!failed.length) this._ncmCacheSet(cacheKey, { v: 2, ts: Date.now(), results });
                        this._renderNeteasePlaylists(results);
                        return;
                    }
                }
            }

            if (keyword) {
                data = await Bridge.call('media_search', keyword);
                this._chromeTitle = `搜索 “${keyword}”`;
                this._chromeSub = `${data.length} 个结果`;
                if (!data.length) {
                    this._renderEmpty('icon:search', '没有找到匹配的媒体', '换个关键词试试。');
                    return;
                }
                this._renderList(data, { showAlbum: true, showTag: true });
                return;
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
                    if (!album) return this._renderEmpty('icon:headphones', '请选择一个专辑');
                    const items = await Bridge.call('media_album_items', album.key, album.kind);
                    this._chromeTitle = album.kind === 'video' ? '视频专辑' : '音乐专辑';
                    this._chromeSub = `${items.length} 个媒体`;
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
                    if (!pl) return this._renderEmpty('icon:list-music', '请选择一个歌单');
                    this._chromeTitle = pl.name || '歌单';
                    this._chromeSub = `${(pl.items || []).length} 个媒体`;
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
                    this._renderEmpty('icon:headphones', '未知视图');
            }
        } catch (e) {
            console.error('加载视图失败:', e);
            this._renderEmpty('icon:triangle-alert', '加载失败', String(e && e.message || e));
        } finally {
            // 只让最后发起的那次加载上报 chrome：切视图时前一次请求可能后到
            if (seq === this._loadSeq) {
                await this._updateStats();
                this._pushChrome();
            }
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
