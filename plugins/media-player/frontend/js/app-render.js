// ============================================================
// OmniBox 媒体播放器 — 渲染：空状态、专辑、媒体列表与详情
//
// 本文件是 MediaPlayerApp.prototype 的一个分片：类骨架与构造函数在 app.js，
// 这里用 Object.assign 把方法扩回同一个原型。**加载顺序是硬约束** —— 必须在
// app.js 之后、实例化之前（见 index.html），契约由 tests/js/media_player_app_split.mjs 把关。
// ============================================================

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
            <div class="mp-empty-state">
                <div class="empty-icon">${icon}</div>
                <div class="empty-text">${MPUtils.escapeHtml(text)}</div>
                ${hint ? `<div class="empty-hint">${MPUtils.escapeHtml(hint)}</div>` : ''}
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
        div.className = 'mp-empty-state';
        div.innerHTML = `
            <div class="empty-icon">${icon}</div>
            <div class="empty-text">${MPUtils.escapeHtml(text)}</div>
            ${hint ? `<div class="empty-hint">${MPUtils.escapeHtml(hint)}</div>` : ''}`;
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
            <div class="mp-empty-state">
                <div class="empty-icon">⚠️</div>
                <div class="empty-text">未检测到 ncm-cli</div>
                <div class="empty-hint">网易云音乐插件需要 ncm-cli 才能登录和播放。</div>
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
    },

    _renderNeteasePlaylists(playlists, emptyText = '暂无推荐歌单') {
        const content = document.getElementById('media-content');
        if (!playlists || !playlists.length) {
            this._renderEmpty('📋', emptyText);
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
        this._observeThumbImgs(content);
    },

});
