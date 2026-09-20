// ============================================================
// OmniBox 媒体播放器 — 渲染：空状态、专辑、媒体列表与详情
//
// 本文件是 MediaPlayerApp.prototype 的一个分片：类骨架与构造函数在 app.js，
// 这里用 Object.assign 把方法扩回同一个原型。**加载顺序是硬约束** —— 必须在
// app.js 之后、实例化之前（见 index.html），契约由 tests/js/media_player_app_split.mjs 把关。
// ============================================================

// 在线歌单最多读取的曲目数：单个歌单可能有几千首，全量同步要按这个上限收敛耗时
const NCM_PLAYLIST_SONG_CAP = 1000;

Object.assign(MediaPlayerApp.prototype, {

    // ============================================================
    // 渲染：空状态 / 加载
    // ============================================================
    _setLoading(text) {
        const content = document.getElementById('media-content');
        // 一律转义：text 会带上后端扫描进度里的磁盘目录名（`s.current`），
        // 而这里是 innerHTML。换行交给 CSS 的 white-space: pre-line 渲染，
        // 因此调用方不需要（也不应该）传 HTML —— 历史实现让三个调用点直接拼
        // `<br>`，等于把这段标记的拼接权交给了每个调用方。
        content.innerHTML = `
            <div class="mp-loading">
                <div class="mp-spinner"></div>
                <div>${MPUtils.escapeHtml(text)}</div>
            </div>`;
    },

    _renderEmpty(icon, text, hint) {
        const content = document.getElementById('media-content');
        content.innerHTML = `
            <div class="empty-state">
                <div class="empty-state-icon">${icon}</div>
                <div class="empty-state-text">${MPUtils.escapeHtml(text)}</div>
                ${hint ? `<div class="empty-state-hint">${MPUtils.escapeHtml(hint)}</div>` : ''}
            </div>`;
    },

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
            const cover = album.cover_item_id
                ? MPUtils.coverImg(Bridge.thumbUrl(album.cover_item_id), kind === 'video' ? '🎬' : '💿',
                    kind === 'video' ? `data-mp-thumb-id="${album.cover_item_id}"` : '',
                    kind === 'video' ? album.cover_item_id : '')
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
        this._observeThumbImgs(grid);
    },

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
        this._observeThumbImgs(target);
    },

    _buildEmpty(icon, text, hint) {
        const div = document.createElement('div');
        div.className = 'empty-state';
        div.innerHTML = `
            <div class="empty-state-icon">${icon}</div>
            <div class="empty-state-text">${MPUtils.escapeHtml(text)}</div>
            ${hint ? `<div class="empty-state-hint">${MPUtils.escapeHtml(hint)}</div>` : ''}`;
        return div;
    },

    _rowHtml(item, index, opts) {
        const active = this.core.currentItem && this.core.currentItem.id === item.id;
        const isFav = this.favIds.has(item.id);
        const coverSrc = MPUtils.coverUrl(item);
        const videoId = item.kind === 'video' ? item.id : '';
        const cover = coverSrc
            ? MPUtils.coverImg(coverSrc, MPUtils.itemIcon(item),
                videoId ? `data-mp-thumb-id="${videoId}"` : '', videoId)
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
    },

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
    },

    _renderNeteaseCliMissing(status = {}) {
        const content = document.getElementById('media-content');
        if (!content) return;
        const version = status.ncm_cli_version || '';
        const error = status.error || '';
        content.innerHTML = `
            <div class="empty-state">
                <div class="empty-state-icon">⚠️</div>
                <div class="empty-state-text">未检测到 ncm-cli</div>
                <div class="empty-state-hint">网易云音乐插件需要 ncm-cli 才能登录和播放。</div>
                <div style="margin-top:14px;text-align:left;max-width:520px;margin-left:auto;margin-right:auto;line-height:1.9;font-size:13px;">
                    <div>安装命令：</div>
                    <pre style="background:var(--bg-hover);padding:8px 10px;border-radius:8px;overflow-x:auto;">npm install -g @music163/ncm-cli</pre>
                    <div style="margin-top:8px;">或者参考：</div>
                    <div><a href="https://www.npmjs.com/package/@music163/ncm-cli" target="_blank" rel="noopener">https://www.npmjs.com/package/@music163/ncm-cli</a></div>
                    ${version ? `<div style="margin-top:8px;">当前版本：${MPUtils.escapeHtml(version)}</div>` : ''}
                    ${error ? `<div style="margin-top:8px;color:var(--danger);">${MPUtils.escapeHtml(error)}</div>` : ''}
                </div>
                <button class="btn btn-primary" id="ncm-recheck-btn" style="margin-top:14px;">我已安装，重新检测</button>
            </div>`;
        document.getElementById('ncm-recheck-btn')?.addEventListener('click', () => this._loadCurrentView());
    },

    async _renderNeteaseLogin() {
        const content = document.getElementById('media-content');
        try {
            const login = await Bridge.callPlugin('netease-music', 'check_login');
            if (login && login.success) {
                // 同步能力只在登录完成后提供：未登录时这些接口必然失败
                content.innerHTML = `
            <div class="empty-state">
                <div class="empty-state-icon">✅</div>
                <div class="empty-state-text">已登录网易云音乐</div>
                <div class="empty-state-hint">本地歌单是在线歌单的镜像：只收录本地媒体库里已有的曲目（按歌名 + 歌手匹配），可反复同步。</div>
                <div style="margin-top:14px;display:flex;gap:10px;justify-content:center;flex-wrap:wrap;">
                    <button class="btn btn-primary" id="ncm-sync-all-btn">🔄 全量同步所有歌单</button>
                    <button class="btn" id="ncm-import-liked-btn">❤ 导入「喜欢」到我的喜欢</button>
                </div>
                <label class="empty-state-hint" style="display:inline-flex;align-items:center;gap:6px;margin-top:14px;cursor:pointer;">
                    <input type="checkbox" id="ncm-sync-collected"${this._syncCollectedEnabled() ? ' checked' : ''}>
                    全量同步也包含「收藏的歌单」（默认只同步自己创建的歌单）
                </label>
                <label class="empty-state-hint" style="display:inline-flex;align-items:center;gap:6px;margin-top:8px;cursor:pointer;">
                    <input type="checkbox" id="ncm-export-missing"${this._exportMissingEnabled() ? ' checked' : ''}>
                    同步时输出本地缺失曲目清单（写到媒体库根目录，便于补档）
                </label>
            </div>`;
                document.getElementById('ncm-sync-all-btn')
                    .addEventListener('click', () => this._syncAllNeteasePlaylists());
                document.getElementById('ncm-import-liked-btn')
                    .addEventListener('click', () => this._importNeteaseLiked());
                document.getElementById('ncm-sync-collected')
                    .addEventListener('change', (e) => this._rememberSyncCollected(e.target.checked));
                document.getElementById('ncm-export-missing')
                    .addEventListener('change', (e) => this._rememberExportMissing(e.target.checked));
                return;
            }
        } catch (e) { }
        content.innerHTML = `
            <div class="empty-state">
                <div class="empty-state-icon">👤</div>
                <div class="empty-state-text">未登录网易云音乐</div>
                <div class="empty-state-hint">请先在终端执行：ncm-cli configure 和 ncm-cli login</div>
                <button class="btn btn-primary" id="ncm-login-btn" style="margin-top:12px;">我已登录</button>
            </div>`;
        document.getElementById('ncm-login-btn').addEventListener('click', () => this._loadCurrentView());
    },

    // ============================================================
    // 网易云 → 本地：喜欢导入 / 歌单重建
    //
    // 两侧数据只能在前端汇合：在线曲目来自 netease-music 插件，本地曲目 id 来自
    // media-player 索引，匹配用的就是本页已加载的本地条目，不需要给后端加耦合。
    // ============================================================

    async _localAudioItems() {
        try {
            return (await Bridge.call('media_all_audio')) || [];
        } catch (e) {
            return [];
        }
    },

    async _importNeteaseLiked() {
        const btn = document.getElementById('ncm-import-liked-btn');
        const label = '⬇ 导入「喜欢」到我的喜欢';
        if (btn) {
            btn.disabled = true;
            btn.textContent = '⏳ 正在匹配本地媒体库…';
        }
        try {
            const liked = await Bridge.callPlugin('netease-music', 'get_liked_songs', 500);
            if (!liked || liked.success === false) {
                throw new Error((liked && liked.error) || '获取红心歌曲失败');
            }
            const songs = liked.results || [];
            const { ids, matched, missed } = MPUtils.matchNeteaseToLocal(songs, await this._localAudioItems());
            if (!ids.length) {
                Toast.info(`网易云喜欢 ${songs.length} 首，本地媒体库中没有可匹配的曲目`);
                return;
            }
            const result = await Bridge.call('media_add_favorites', ids);
            if (!result || result.success === false) {
                throw new Error((result && result.error) || '写入喜欢失败');
            }
            ids.forEach(id => this.favIds.add(id));
            const added = result.added || 0;
            Toast.success(`已加入 ${added} 首到我的喜欢`
                + `（匹配 ${matched} 首，本地媒体库缺失 ${missed} 首`
                + `${matched > added ? `，${matched - added} 首已在喜欢` : ''}）`);
        } catch (e) {
            Toast.error('导入失败：' + ((e && e.message) || e));
        } finally {
            if (btn) {
                btn.disabled = false;
                btn.textContent = label;
            }
        }
    },

    // 歌单详情页的曲目只加载了首屏，重建要用全量曲目（分页读取，上限防超长歌单打爆）
    async _fetchNeteasePlaylistSongs(playlistId, cap = NCM_PLAYLIST_SONG_CAP) {
        const page = 500;
        const songs = [];
        while (songs.length < cap) {
            const data = await Bridge.callPlugin('netease-music', 'get_playlist_tracks',
                playlistId, page, songs.length);
            if (data && data.success === false) {
                throw new Error(data.error || '读取歌单曲目失败');
            }
            const batch = (data && data.results) || [];
            songs.push(...batch);
            if (batch.length < page) break;
        }
        return songs.slice(0, cap);
    },

    async _rebuildLocalPlaylistFromNetease(playlist) {
        if (!playlist) return;
        const btn = document.querySelector('[data-hero-action="rebuild-local"]');
        if (btn) {
            btn.disabled = true;
            btn.textContent = '⏳ 正在匹配本地媒体库…';
        }
        try {
            const songs = await this._fetchNeteasePlaylistSongs(playlist.id);
            const { ids, matched, missed, missedSongs } = MPUtils.matchNeteaseToLocal(
                songs, await this._localAudioItems());
            const listPath = await this._exportMissingIfEnabled(playlist.name || '歌单', missedSongs);
            if (!ids.length) {
                Toast.info(`已读取 ${songs.length} 首，本地媒体库中没有可匹配的曲目`
                    + (listPath ? `；缺失清单：${listPath}` : ''));
                return;
            }
            // 带来源前缀 + 同名即更新：重建同一个在线歌单只会覆盖镜像歌单本身，
            // 不会碰到用户自己的同名歌单。
            const name = `网易云 · ${playlist.name || '歌单'}`;
            const existing = (this.playlists.playlists || []).find(p => p.name === name);
            const saved = await Bridge.call('media_playlist_save', name, existing ? existing.id : '', ids);
            if (!saved || saved.success === false) {
                throw new Error((saved && saved.error) || '写入歌单失败');
            }
            await this.playlists.load();
            Toast.success(`${existing ? '已重建' : '已创建'}本地歌单「${name}」`
                + `：命中 ${matched} 首，本地缺失 ${missed} 首`
                + (listPath ? `；缺失清单：${listPath}` : ''));
        } catch (e) {
            Toast.error('重建失败：' + ((e && e.message) || e));
        } finally {
            if (btn) {
                btn.disabled = false;
                btn.textContent = '⬇ 重建为本地歌单';
            }
        }
    },

    async _syncAllNeteasePlaylists() {
        const btn = document.getElementById('ncm-sync-all-btn');
        this._rememberExportMissing(this._exportMissingEnabled());
        this._rememberSyncCollected(this._syncCollectedEnabled());
        const exportMissing = this._exportMissingEnabled();
        // 默认只同步自己创建的歌单：收藏的歌单（别人的）动辄几千首，全量镜像又慢又没用
        const includeCollected = this._syncCollectedEnabled();
        if (btn) btn.disabled = true;
        const step = (text) => { if (btn) btn.textContent = text; };
        try {
            const sources = await Promise.all([
                Bridge.callPlugin('netease-music', 'get_created_playlists', 200),
                includeCollected
                    ? Bridge.callPlugin('netease-music', 'get_collected_playlists', 200)
                    : Promise.resolve(null),
            ]);
            const playlists = [];
            const seen = new Set();
            for (const source of sources) {
                for (const pl of (source && source.results) || []) {
                    if (pl && pl.id && !seen.has(pl.id)) {
                        seen.add(pl.id);
                        playlists.push(pl);
                    }
                }
            }
            if (!playlists.length) {
                Toast.info(includeCollected
                    ? '没有可同步的歌单：请先在终端完成 ncm-cli login，或账号下确实没有歌单'
                    : '没有可同步的自建歌单（收藏的歌单默认跳过，可在上面勾选后重试）');
                return;
            }
            const local = await this._localAudioItems();
            const missingLines = [];
            let matched = 0;
            let missed = 0;
            let written = 0;
            let capped = 0;
            for (let i = 0; i < playlists.length; i++) {
                const playlist = playlists[i];
                step(`⏳ ${i + 1}/${playlists.length}：${playlist.name || ''}`);
                const songs = await this._fetchNeteasePlaylistSongs(playlist.id);
                if (songs.length >= NCM_PLAYLIST_SONG_CAP) capped += 1;
                const result = MPUtils.matchNeteaseToLocal(songs, local);
                matched += result.matched;
                missed += result.missed;
                if (result.ids.length) {
                    const name = `网易云 · ${playlist.name || '歌单'}`;
                    const existing = (this.playlists.playlists || []).find(p => p.name === name);
                    const saved = await Bridge.call('media_playlist_save', name,
                        existing ? existing.id : '', result.ids);
                    if (saved && saved.success !== false) written += 1;
                }
                if (exportMissing) {
                    missingLines.push(...this._missingLines(playlist.name || '歌单', result.missedSongs));
                }
            }
            await this.playlists.load();
            const listPath = exportMissing
                ? await this._writeMissingList('网易云缺失曲目', missingLines) : '';
            Toast.success(`全量同步完成（${includeCollected ? '创建 + 收藏' : '仅自建'}歌单）：`
                + `${playlists.length} 个歌单 → 写入 ${written} 个本地歌单`
                + `（命中 ${matched} 首，本地缺失 ${missed} 首`
                + `${capped ? `；${capped} 个歌单超过 ${NCM_PLAYLIST_SONG_CAP} 首只取前 ${NCM_PLAYLIST_SONG_CAP} 首` : ''}）`
                + (listPath ? `；缺失清单：${listPath}` : ''));
        } catch (e) {
            Toast.error('全量同步失败：' + ((e && e.message) || e));
        } finally {
            if (btn) {
                btn.disabled = false;
                btn.textContent = '🔄 全量同步所有歌单';
            }
        }
    },

    // 「全量同步也包含收藏的歌单」：默认关 —— 收藏的多是别人的几千首大歌单，
    // 全量镜像既慢又没意义；想同步时在登录页勾一下（偏好存 localStorage）。
    _syncCollectedEnabled() {
        const box = document.getElementById('ncm-sync-collected');
        if (box) return !!box.checked;
        try {
            return localStorage.getItem('ncmSyncCollected') === '1';
        } catch (e) {
            return false;
        }
    },

    _rememberSyncCollected(enabled) {
        try {
            localStorage.setItem('ncmSyncCollected', enabled ? '1' : '0');
        } catch (e) { }
    },

    // 「同步时输出缺失曲目清单」：登录页有勾选框，其它入口（歌单详情重建）读存档值。
    // 默认开 —— 这份清单就是补档用的，关掉才需要显式操作。
    _exportMissingEnabled() {
        const box = document.getElementById('ncm-export-missing');
        if (box) return !!box.checked;
        try {
            return localStorage.getItem('ncmExportMissing') !== '0';
        } catch (e) {
            return true;
        }
    },

    _rememberExportMissing(enabled) {
        try {
            localStorage.setItem('ncmExportMissing', enabled ? '1' : '0');
        } catch (e) { }
    },

    _missingLines(playlistName, missedSongs) {
        const songs = missedSongs || [];
        if (!songs.length) return [];
        const lines = [`【${playlistName}】${songs.length} 首本地缺失`];
        for (const song of songs) {
            const artists = Array.isArray(song.artists)
                ? song.artists.join('/') : (song.artists || '');
            lines.push(`  ${artists || '未知歌手'} - ${song.name || '未知歌曲'}`);
        }
        return lines;
    },

    // 清单文件按范围分开：全量同步写「网易云缺失曲目.txt」，单个歌单重建写
    // 「网易云缺失曲目 · <歌单名>.txt」—— 单歌单的结果不该覆盖全量清单。
    async _writeMissingList(title, lines) {
        try {
            const result = await Bridge.call('media_export_missing', lines, title);
            return (result && result.success && result.path) || '';
        } catch (e) {
            return '';
        }
    },

    async _exportMissingIfEnabled(playlistName, missedSongs) {
        if (!this._exportMissingEnabled()) return '';
        return await this._writeMissingList(`网易云缺失曲目 · ${playlistName}`,
            this._missingLines(playlistName, missedSongs));
    },

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
        // 来源标签：从「我的歌单」进来时带 origin，搜索/推荐进来的（别人的歌单）留空
        const label = playlist.origin === 'created' ? '创建的歌单'
            : (playlist.origin === 'collected' ? '收藏的歌单' : '歌单');
        if (cached && cached.ts && (Date.now() - cached.ts < twoHours) && cached.results) {
            if (seq !== this._loadSeq) return;
            const items = (cached.results || []).map(song => this._neteaseToMediaItem(song));
            this._renderDetail(items, {
                label,
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
                label,
                title: playlist.name || '歌单',
                sub: `${playlist.track_count || 0} 首 · 播放 ${playlist.play_count || 0}`,
                cover: playlist.cover_url || '',
                kind: 'ncm-playlist',
            });
        } catch (e) {
            content.innerHTML = '<div class="empty-state empty-state--error">'
                + '<div class="empty-state-icon">⚠️</div>'
                + '<div class="empty-state-text">歌单加载失败</div>'
                + '<div class="empty-state-hint">检查网络 / 代理设置，或到「⚙ 设置」重新登录后再试</div>'
                + '</div>';
        }
    },

    _renderNeteasePlaylists(playlists, emptyText = '暂无推荐歌单') {
        const content = document.getElementById('media-content');
        if (!playlists || !playlists.length) {
            this._renderEmpty('📋', emptyText);
            return;
        }
        // 带 origin 的列表（我的歌单）按来源分段：创建在前、收藏在后，段头带数量。
        // 段头不带 data-idx，点击委派只认 .mp-row，索引映射不受影响。
        const counts = playlists.reduce((acc, p) => {
            if (p && p.origin) acc[p.origin] = (acc[p.origin] || 0) + 1;
            return acc;
        }, {});
        const groupTitle = {
            created: `创建的歌单 · ${counts.created || 0}`,
            collected: `收藏的歌单 · ${counts.collected || 0}`,
        };
        let current = '';
        content.innerHTML = `<div class="mp-list">${playlists.map((p, i) => {
            let head = '';
            if (p.origin && p.origin !== current) {
                current = p.origin;
                head = `<div class="mp-list-group">${MPUtils.escapeHtml(groupTitle[p.origin] || p.origin)}</div>`;
            }
            return head + `
            <div class="mp-row" data-idx="${i}">
                <span class="mp-row-index">${i + 1}</span>
                <div class="mp-row-cover">${p.cover_url ? `<img src="${MPUtils.escapeHtml(p.cover_url)}" onerror="this.outerHTML='📋'">` : '📋'}</div>
                <div class="mp-row-info">
                    <div class="mp-row-title">${MPUtils.escapeHtml(p.name)}</div>
                    <div class="mp-row-sub">${p.track_count} 首 · 播放 ${p.play_count}</div>
                </div>
            </div>`;
        }).join('')}</div>`;
        this._currentListData = playlists;
    },

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
    },

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
                this.core.saveQueueState();
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
    },

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
    },

    // ============================================================
    // 渲染：专辑 / 歌单详情
    // ============================================================
    _renderDetail(items, header) {
        const content = document.getElementById('media-content');
        // header.cover 兼容两种形态：远程 URL 字符串（网易云）/ 本地 item 对象（/thumbs）
        const coverSrc = MPUtils.coverSrc(header.cover);
        const totalDuration = items.reduce((sum, i) => sum + (i.duration || 0), 0);

        content.innerHTML = `
            <div class="mp-detail-hero" style="--hero-bg:${MPUtils.heroBg(coverSrc)}">
                <button class="mp-ghost-btn mp-hero-back" data-hero-action="back" title="返回">← 返回</button>
                <div class="mp-detail-cover">${coverSrc ? MPUtils.coverImg(coverSrc, header.kind === 'video' ? '🎬' : '💿',
                    (header.kind === 'video' && header.cover && header.cover.id) ? `data-mp-thumb-id="${header.cover.id}"` : '',
                    (header.kind === 'video' && header.cover && header.cover.id) ? header.cover.id : '') : (header.kind === 'video' ? '🎬' : '💿')}</div>
                <div class="mp-detail-info">
                    <div class="mp-detail-label">${MPUtils.escapeHtml(header.label)}</div>
                    <div class="mp-detail-title">${MPUtils.escapeHtml(header.title)}</div>
                    <div class="mp-detail-sub">${MPUtils.escapeHtml(header.sub || '')}</div>
                    <div class="mp-detail-meta">${items.length} 个媒体 · ${MPUtils.fmtDuration(totalDuration)}</div>
                </div>
                <div class="mp-detail-actions">
                    <button class="btn btn-primary" data-hero-action="play-all">▶ 播放全部</button>
                    ${header.kind === 'ncm-playlist' ? `
                        <button class="btn" data-hero-action="rebuild-local"
                                title="按歌名与歌手匹配本地媒体库，生成/覆盖同名镜像歌单">⬇ 重建为本地歌单</button>
                    ` : ''}
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
                } else if (action === 'rebuild-local' && header.kind === 'ncm-playlist') {
                    await this._rebuildLocalPlaylistFromNetease(this.currentNeteasePlaylist);
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
        this._observeThumbImgs(content);
    },

});
