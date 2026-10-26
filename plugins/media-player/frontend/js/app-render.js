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
                <div class="empty-state-icon">${MPUtils.icon(icon)}</div>
                <div class="empty-state-text">${MPUtils.escapeHtml(text)}</div>
                ${hint ? `<div class="empty-state-hint">${MPUtils.escapeHtml(hint)}</div>` : ''}
            </div>`;
    },

    // ============================================================
    // 渲染：网易云状态页（错误 / 未登录 / 空白 / 搜索无结果）
    // ============================================================
    /** 网易云接口错误的类别。只按后端返回的 error 文本做前置条件归类，不解释业务数据。 */
    _ncmErrorKind(error) {
        const text = String(error == null ? '' : error);
        if (/api[\s_-]*key|appid|privatekey|未设置.*(key|密钥)|(key|密钥).*未设置/i.test(text)) return 'api-key';
        if (/登录|login|cookie|未授权|unauthor/i.test(text)) return 'login';
        return 'error';
    },

    /** 把后端 error 里可读的行留下来，去掉命令行形式的操作指引（安装/配置命令）。 */
    _ncmErrorDetail(error) {
        const lines = String(error == null ? '' : error)
            .split(/\r?\n/)
            .map(line => line.trim())
            .filter(Boolean)
            .filter(line => !/(ncm-cli|npm\s+install|config\s+set|appid|privatekey|运行\s|执行\s|安装命令|^\s*[-*]\s*)/i.test(line));
        return lines.join('\n').slice(0, 300).trim();
    },

    /**
     * 网易云状态页。
     *
     * state 取值：
     * - `api-key` / `cli-missing` / `error`：错误态，使用壳的 `.empty-state--error` 并给出重试；
     * - `login`：未登录前置条件，中性样式 + 重新检测（与系统错误区分）；
     * - `empty`：接口成功但没有数据（真空白），不给重试；
     * - `search`：搜索无结果，不给重试。
     * `data-ncm-state` 暴露状态，便于 UI 审计与后续自动化断言。
     */
    _renderNcmState(state, options = {}) {
        const content = document.getElementById('media-content');
        if (!content) return;
        const isError = state === 'api-key' || state === 'cli-missing' || state === 'error';
        const retryLabel = options.retryLabel || (state === 'login' || state === 'cli-missing' ? '重新检测' : '重试');
        const detail = options.detail
            ? `<div class="mp-ncm-detail">${MPUtils.escapeHtml(options.detail)}</div>`
            : '';
        content.innerHTML = `
            <div class="empty-state${isError ? ' empty-state--error' : ''}" data-ncm-state="${state}" role="${isError ? 'alert' : 'status'}">
                <div class="empty-state-icon">${MPUtils.icon(options.icon || 'icon:music')}</div>
                <div class="empty-state-text">${MPUtils.escapeHtml(options.title || '')}</div>
                ${options.hint ? `<div class="empty-state-hint">${MPUtils.escapeHtml(options.hint)}</div>` : ''}
                ${detail}
                ${options.retry ? `<button class="btn btn-primary mp-ncm-retry" id="ncm-retry-btn">${MPUtils.escapeHtml(retryLabel)}</button>` : ''}
            </div>`;
        const retry = document.getElementById('ncm-retry-btn');
        if (retry) {
            retry.addEventListener('click', () => {
                // 二级页的重试不能走 _loadCurrentView（它只认一级视图），由调用方给回调。
                if (typeof options.onRetry === 'function') options.onRetry();
                else this._loadCurrentView();
            });
        }
    },

    /** 网易云接口失败：API key / 未登录 / 其它错误分开表达，统一不展示原始 CLI 指令。 */
    _renderNcmError(error, onRetry) {
        const kind = this._ncmErrorKind(error);
        if (kind === 'login') {
            this._renderNcmState('login', {
                icon: 'icon:user',
                title: '尚未登录网易云音乐',
                hint: '请在侧栏「登录」入口完成网易云登录，然后点击重试。',
                retry: true,
                retryLabel: '重试',
                onRetry,
            });
            return;
        }
        if (kind === 'api-key') {
            this._renderNcmState('api-key', {
                icon: 'icon:triangle-alert',
                title: '网易云 API 未配置',
                hint: '网易云接口尚未完成 API 配置。完成配置后点击重试。',
                detail: 'API key 未设置',
                retry: true,
                onRetry,
            });
            return;
        }
        this._renderNcmState('error', {
            icon: 'icon:triangle-alert',
            title: '网易云数据加载失败',
            hint: '接口暂时不可用，请稍后重试。',
            detail: this._ncmErrorDetail(error),
            retry: true,
            onRetry,
        });
    },

    // ============================================================
    // 渲染：专辑卡片（共享 ContentUI）
    // ============================================================
    _renderAlbums(albums, kind) {
        const content = document.getElementById('media-content');
        if (!albums || !albums.length) {
            const isVideo = kind === 'video';
            this._renderEmpty(isVideo ? 'icon:disc-3' : 'icon:disc', isVideo ? '暂无视频专辑' : '暂无音乐专辑', '点击顶栏「校验」建立媒体索引');
            return;
        }

        const grid = ContentUI.grid({
            className: 'mp-card-grid',
            ariaLabel: kind === 'video' ? '视频专辑' : '音乐专辑',
        });
        albums.forEach((album, index) => grid.appendChild(this._buildAlbumCard(album, index, kind)));
        content.innerHTML = '';
        content.appendChild(grid);
        this._observeThumbImgs(grid);
    },

    _buildAlbumCard(album, index, kind) {
        const isVideo = kind === 'video';
        const icon = isVideo ? 'icon:clapperboard' : 'icon:disc';
        const coverUrl = album.cover_item_id ? Bridge.thumbUrl(album.cover_item_id) : '';
        const card = ContentUI.card({
            className: 'mp-card',
            image: coverUrl ? {
                src: coverUrl,
                alt: '',
                data: isVideo && album.cover_item_id ? { mpThumbId: album.cover_item_id } : null,
                onError: (event, img) => {
                    if (isVideo && album.cover_item_id && typeof MPCoverFail === 'function') {
                        MPCoverFail(img, album.cover_item_id, icon);
                    } else {
                        MPUtils.fallbackCover(img, icon);
                    }
                },
            } : null,
            title: album.name,
            sub: album.artist || '',
            // 时长放封面左下，正文只留标题/副标题；四插件 body 高度才能一致。
            mediaOverlay: {
                badge: `${album.count} 项`,
                count: MPUtils.fmtDuration(album.total_duration),
            },
            actionsPlacement: 'overlay',
            actions: [{
                label: '',
                icon: 'icon:play',
                className: 'mp-card-play',
                data: { key: album.key, kind: album.kind },
                title: '播放全部',
                ariaLabel: `播放专辑 ${album.name}`,
            }],
            data: { key: album.key, kind: album.kind },
            ariaLabel: `打开${isVideo ? '视频' : '音乐'}专辑 ${album.name}`,
            onActivate: (event) => {
                if (event && event.preventDefault) event.preventDefault();
                this.openAlbum(album.key, album.kind);
            },
        });
        card.style.setProperty('--i', Math.min(index, 26));
        const media = card.querySelector('.cui-media');
        if (!coverUrl && media) this._fillCoverFallback(media, icon);
        return card;
    },

    async _playAlbum(key, kind) {
        const items = await Bridge.call('media_album_items', key, kind);
        if (items && items.length) {
            this.core.setQueue(items, 0);
            Toast.info(`正在播放：${items[0].title}`);
        }
    },

    // ============================================================
    // 渲染：媒体列表（共享 ContentUI）
    // ============================================================
    _renderList(items, opts = {}, container = null) {
        const target = container || document.getElementById('media-content');
        if (!items || !items.length) {
            target.innerHTML = '';
            target.appendChild(this._buildEmpty('icon:music', '暂无媒体', '点击顶栏「校验」建立媒体索引'));
            return;
        }

        this._currentListData = items;
        this._currentListOpts = opts;
        this._currentListContainer = target;

        // 详情页的容器本身就是 .mp-list：直接复用，避免再套一层。
        const reuse = target.classList && target.classList.contains('mp-list');
        const list = reuse ? target : ContentUI.list({ className: 'mp-list', ariaLabel: opts.listLabel || '媒体列表' });
        target.innerHTML = '';
        if (!reuse) target.appendChild(list);
        items.forEach((item, index) => list.appendChild(this._buildMediaRow(item, index, opts)));
        this._observeThumbImgs(target);
    },

    _buildEmpty(icon, text, hint) {
        const div = document.createElement('div');
        div.className = 'empty-state';
        div.innerHTML = `
            <div class="empty-state-icon">${MPUtils.icon(icon)}</div>
            <div class="empty-state-text">${MPUtils.escapeHtml(text)}</div>
            ${hint ? `<div class="empty-state-hint">${MPUtils.escapeHtml(hint)}</div>` : ''}`;
        return div;
    },

    _buildRowSub(item, opts) {
        const parts = [];
        if (item.artist) parts.push(item.artist);
        if (opts.showAlbum && item.album) parts.push(item.album);
        if (opts.showPlayedAt && item.played_at) parts.push(MPUtils.timeAgo(item.played_at));
        return parts.join(' · ');
    },

    _rowActions(item, index, opts) {
        const isFav = this.favIds.has(item.id);
        const make = (action, icon, label, extraClass = '') => ({
            label: '',
            icon,
            className: `mp-row-action${extraClass ? ' ' + extraClass : ''}`,
            data: { mpAction: action, idx: index },
            title: label,
            ariaLabel: label,
        });
        if (opts.playlistId) {
            return [make('remove', 'icon:x', '移出歌单', 'danger')];
        }
        return [
            make('fav', 'icon:heart', isFav ? '取消喜欢' : '喜欢', isFav ? 'fav-active' : ''),
            make('queue', 'icon:plus', '添加到队列'),
            make('playlist', 'icon:list-music', '加入歌单'),
        ];
    },

    _buildMediaRow(item, index, opts) {
        const active = this.core.currentItem && this.core.currentItem.id === item.id;
        const coverSrc = MPUtils.coverUrl(item);
        const videoId = item.kind === 'video' ? item.id : '';
        const fallbackIcon = MPUtils.itemIcon(item);
        const sub = this._buildRowSub(item, opts);
        const row = ContentUI.row({
            className: 'mp-row',
            image: coverSrc ? {
                src: coverSrc,
                alt: '',
                data: videoId ? { mpThumbId: videoId } : null,
                onError: (event, img) => {
                    if (videoId && typeof MPCoverFail === 'function') MPCoverFail(img, videoId, fallbackIcon);
                    else MPUtils.fallbackCover(img, fallbackIcon);
                },
            } : null,
            title: item.title,
            sub,
            data: { idx: index, id: item.id },
            ariaLabel: `${item.kind === 'video' ? '视频' : '音频'}：${item.title}${sub ? ' · ' + sub : ''}`,
            actions: this._rowActions(item, index, opts),
            onActivate: (event) => this._activateRow(index, event),
        });
        row.style.setProperty('--i', Math.min(index, 30));
        if (active) row.classList.add('active');

        const indexEl = document.createElement('span');
        indexEl.className = 'mp-row-index';
        indexEl.textContent = String(index + 1);
        row.insertBefore(indexEl, row.firstChild);

        const media = row.querySelector('.cui-media');
        if (media) {
            media.classList.add('mp-row-cover');
            if (!coverSrc) this._fillCoverFallback(media, fallbackIcon);
        }
        const body = row.querySelector('.cui-body');
        if (body) body.classList.add('mp-row-info');
        const title = row.querySelector('.cui-title');
        if (title) title.classList.add('mp-row-title');
        const subEl = row.querySelector('.cui-sub');
        if (subEl) subEl.classList.add('mp-row-sub');

        const actions = row.querySelector('.cui-actions');
        if (opts.showTag) {
            const tag = document.createElement('span');
            tag.className = `mp-row-tag${item.kind === 'video' ? ' video' : ''}`;
            tag.textContent = item.kind === 'video' ? '视频' : '音乐';
            row.insertBefore(tag, actions || null);
        }
        const duration = document.createElement('span');
        duration.className = 'mp-row-duration';
        duration.textContent = MPUtils.formatTime(item.duration);
        row.insertBefore(duration, actions || null);
        return row;
    },

    /**
     * 行激活：鼠标点击由 ContentUI 的 role=button 处理，键盘 Enter/Space 同样走这里。
     * 用 `preventDefault` 标记“已处理”，内容代理里的旧分支据 defaultPrevented 跳过，避免重复播放。
     */
    async _activateRow(index, event) {
        if (event && event.preventDefault) event.preventDefault();
        const item = this._currentListData[index];
        if (!item) return;
        if (this.currentView === 'ncm-playlists' || this.currentView === 'ncm-my-playlists') {
            this.openNeteasePlaylist(item);
            return;
        }
        if (this.currentView.startsWith('ncm-')) {
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
        this.core.setQueue(this._currentListData, index);
    },

    _fillCoverFallback(media, icon) {
        media.classList.remove('cui-media--empty');
        media.removeAttribute('aria-hidden');
        // 媒体层里可能已经有 mediaOverlay / actions；只插占位，不覆盖它们。
        const holder = document.createElement('div');
        holder.className = 'cover-fallback';
        holder.innerHTML = MPUtils.icon(icon);
        media.insertBefore(holder, media.firstChild);
    },

    _rowHtml(item, index, opts) {
        // 兼容拆分契约里的成员检查；行渲染统一走 ContentUI，返回无事件的 HTML 快照。
        return this._buildMediaRow(item, index, opts).outerHTML;
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
        // 只说明“依赖不可用”，不再把安装命令、包地址或 ncm-cli 原始错误贴进界面。
        this._renderNcmState('cli-missing', {
            icon: 'icon:triangle-alert',
            title: '网易云组件不可用',
            hint: '网易云功能依赖的本机组件尚未就绪。完成安装与登录后点击重新检测。',
            detail: this._ncmErrorDetail(status.error),
            retry: true,
            retryLabel: '重新检测',
        });
    },

    async _renderNeteaseLogin() {
        const content = document.getElementById('media-content');
        let loginError = '';
        try {
            const login = await Bridge.callPlugin('netease-music', 'check_login');
            if (login && login.success) {
                // 同步能力只在登录完成后提供：未登录时这些接口必然失败
                content.innerHTML = `
            <div class="empty-state">
                <div class="empty-state-icon">${MPUtils.icon('icon:circle-check')}</div>
                <div class="empty-state-text">已登录网易云音乐</div>
                <div class="empty-state-hint">本地歌单是在线歌单的镜像：只收录本地媒体库里已有的曲目（按歌名 + 歌手匹配），可反复同步。</div>
                <div style="margin-top:14px;display:flex;gap:10px;justify-content:center;flex-wrap:wrap;">
                    <button class="btn btn-primary" id="ncm-sync-all-btn">${MPUtils.icon('icon:refresh-cw')} 全量同步所有歌单</button>
                    <button class="btn" id="ncm-import-liked-btn">${MPUtils.icon('icon:heart')} 导入「喜欢」到我的喜欢</button>
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
            loginError = (login && login.error) || '';
        } catch (e) {
            loginError = String((e && e.message) || e);
        }
        if (loginError) {
            const kind = this._ncmErrorKind(loginError);
            if (kind !== 'login') {
                this._renderNcmError(loginError);
                return;
            }
        }
        this._renderNcmState('login', {
            icon: 'icon:user',
            title: '尚未登录网易云音乐',
            hint: '请先在本机完成网易云登录，然后点击下方「我已登录」按钮重新检测。',
            retry: true,
            retryLabel: '我已登录',
        });
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
        const label = MPUtils.icon('icon:download') + ' 导入「喜欢」到我的喜欢';
        if (btn) {
            btn.disabled = true;
            btn.innerHTML = MPUtils.icon('icon:loader') + ' 正在匹配本地媒体库…';
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
                btn.innerHTML = label;
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
            btn.innerHTML = MPUtils.icon('icon:loader') + ' 正在匹配本地媒体库…';
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
                btn.innerHTML = MPUtils.icon('icon:download') + ' 重建为本地歌单';
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
        const step = (text) => { if (btn) btn.innerHTML = text; };
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
                    ? '没有可同步的歌单：请先完成网易云登录，或账号下确实没有歌单'
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
                step(MPUtils.icon('icon:loader') + ` ${i + 1}/${playlists.length}：${MPUtils.escapeHtml(playlist.name || '')}`);
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
                btn.innerHTML = MPUtils.icon('icon:refresh-cw') + ' 全量同步所有歌单';
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
        // 壳的「刷新」只在网易云视图出现，详情页也算；标题/副标题按歌单上报
        this._updateActions();
        this._chromeTitle = playlist.name || '歌单';
        this._chromeSub = `${playlist.track_count || 0} 首 · 播放 ${playlist.play_count || 0}`;
        this._pushChrome();
        const content = document.getElementById('media-content');
        if (!content) return;
        const cacheKey = 'ncm-playlist-detail-' + playlist.id;
        const cached = this._ncmCacheGet(cacheKey);
        const twoHours = 2 * 60 * 60 * 1000;
        // 来源标签：从「我的歌单」进来时带 origin，搜索/推荐进来的（别人的歌单）留空
        const label = playlist.origin === 'created' ? '创建的歌单'
            : (playlist.origin === 'collected' ? '收藏的歌单' : '歌单');
        if (cached && cached.ts && (Date.now() - cached.ts < twoHours) && (cached.results || []).length) {
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
            // 后端失败时 results 为空、success=false：不能当成“空歌单”缓存并渲染。
            if (data && data.success === false) {
                this._renderNcmError(data.error, () => this.openNeteasePlaylist(playlist));
                return;
            }
            const tracks = (data && Array.isArray(data.results)) ? data.results : [];
            if (!tracks.length) {
                this._renderNcmState('empty', {
                    icon: 'icon:list-music',
                    title: '歌单为空',
                    hint: '这个歌单暂时没有曲目。',
                });
                return;
            }
            this._ncmCacheSet(cacheKey, { ts: Date.now(), results: tracks });
            const items = tracks.map(song => this._neteaseToMediaItem(song));
            this._renderDetail(items, {
                label,
                title: playlist.name || '歌单',
                sub: `${playlist.track_count || 0} 首 · 播放 ${playlist.play_count || 0}`,
                cover: playlist.cover_url || '',
                kind: 'ncm-playlist',
            });
        } catch (e) {
            this._renderNcmError(String((e && e.message) || e), () => this.openNeteasePlaylist(playlist));
        }
    },

    _renderNeteasePlaylists(playlists) {
        const content = document.getElementById('media-content');
        // 空列表的兜底已删除：调用点在拿不到数据时都会先渲染 _renderNcmState('empty'/'search')，
        // 这里再回落只会把「暂无推荐歌单」的文案带回「我的歌单」。
        const items = Array.isArray(playlists) ? playlists : [];
        // 带 origin 的列表（我的歌单）按来源分段：创建在前、收藏在后，段头带数量。
        // 段头不带 data-idx，激活只认 .mp-row，索引映射不受影响。
        const counts = items.reduce((acc, p) => {
            if (p && p.origin) acc[p.origin] = (acc[p.origin] || 0) + 1;
            return acc;
        }, {});
        const groupTitle = {
            created: `创建的歌单 · ${counts.created || 0}`,
            collected: `收藏的歌单 · ${counts.collected || 0}`,
        };
        const list = ContentUI.list({ className: 'mp-list', ariaLabel: '网易云歌单' });
        let current = '';
        items.forEach((playlist, index) => {
            if (playlist.origin && playlist.origin !== current) {
                current = playlist.origin;
                const head = document.createElement('div');
                head.className = 'mp-list-group';
                head.textContent = groupTitle[playlist.origin] || playlist.origin;
                list.appendChild(head);
            }
            list.appendChild(this._buildNeteasePlaylistRow(playlist, index));
        });
        content.innerHTML = '';
        content.appendChild(list);
        this._currentListData = items;
        this._currentListContainer = list;
    },

    _buildNeteasePlaylistRow(playlist, index) {
        const row = ContentUI.row({
            className: 'mp-row',
            image: playlist.cover_url ? {
                src: playlist.cover_url,
                alt: '',
                onError: (event, img) => MPUtils.fallbackCover(img, 'icon:list-music'),
            } : null,
            title: playlist.name,
            sub: `${playlist.track_count || 0} 首 · 播放 ${playlist.play_count || 0}`,
            data: { idx: index },
            ariaLabel: `打开歌单 ${playlist.name}`,
            onActivate: (event) => this._activateRow(index, event),
        });
        const indexEl = document.createElement('span');
        indexEl.className = 'mp-row-index';
        indexEl.textContent = String(index + 1);
        row.insertBefore(indexEl, row.firstChild);
        const media = row.querySelector('.cui-media');
        if (media) {
            media.classList.add('mp-row-cover');
            if (!playlist.cover_url) this._fillCoverFallback(media, 'icon:list-music');
        }
        const body = row.querySelector('.cui-body');
        if (body) body.classList.add('mp-row-info');
        const title = row.querySelector('.cui-title');
        if (title) title.classList.add('mp-row-title');
        const sub = row.querySelector('.cui-sub');
        if (sub) sub.classList.add('mp-row-sub');
        return row;
    },

    _bindContentDelegation() {
        const content = document.getElementById('media-content');
        // ContentUI 的 action 点击会 stopPropagation；在捕获阶段接管卡片播放与行内动作，
        // 保持原有委托路径，同时不再给每个 action 挂 onClick。
        content.addEventListener('click', async (e) => {
            const playBtn = e.target.closest('.mp-card-play');
            if (playBtn) {
                e.stopPropagation();
                await this._playAlbum(playBtn.dataset.key, playBtn.dataset.kind);
                return;
            }
            const action = e.target.closest('.mp-row-action');
            if (!action) return;
            e.stopPropagation();
            const idx = parseInt(action.dataset.idx, 10);
            if (!isNaN(idx)) await this._handleRowAction(action.dataset.mpAction, idx);
        }, true);

        content.addEventListener('click', async (e) => {
            const card = e.target.closest('.mp-card');
            if (card && !e.defaultPrevented) {
                this.openAlbum(card.dataset.key, card.dataset.kind);
                return;
            }

            const row = e.target.closest('.mp-row');
            if (row && !e.defaultPrevented && !isNaN(parseInt(row.dataset.idx, 10))) {
                // ContentUI 的 role=button 已经处理过点击（preventDefault）；这里是旧 DOM 的回落。
                await this._activateRow(parseInt(row.dataset.idx, 10), e);
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
                favBtn.innerHTML = MPUtils.icon('icon:heart');
                favBtn.title = fav ? '取消喜欢' : '喜欢';
                favBtn.setAttribute('aria-label', fav ? '取消喜欢' : '喜欢');
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
                <button class="mp-ghost-btn mp-hero-back" data-hero-action="back" title="返回">${MPUtils.icon('icon:arrow-left')} 返回</button>
                <div class="mp-detail-cover">${coverSrc ? MPUtils.coverImg(coverSrc, header.kind === 'video' ? 'icon:clapperboard' : 'icon:disc',
                    (header.kind === 'video' && header.cover && header.cover.id) ? `data-mp-thumb-id="${header.cover.id}"` : '',
                    (header.kind === 'video' && header.cover && header.cover.id) ? header.cover.id : '') : (header.kind === 'video' ? MPUtils.icon('icon:clapperboard') : MPUtils.icon('icon:disc'))}</div>
                <div class="mp-detail-info">
                    <div class="mp-detail-label">${MPUtils.escapeHtml(header.label)}</div>
                    <div class="mp-detail-title">${MPUtils.escapeHtml(header.title)}</div>
                    <div class="mp-detail-sub">${MPUtils.escapeHtml(header.sub || '')}</div>
                    <div class="mp-detail-meta">${items.length} 个媒体 · ${MPUtils.fmtDuration(totalDuration)}</div>
                </div>
                <div class="mp-detail-actions">
                    <button class="btn btn-primary" data-hero-action="play-all">${MPUtils.icon('icon:play')} 播放全部</button>
                    ${header.kind === 'ncm-playlist' ? `
                        <button class="btn" data-hero-action="rebuild-local"
                                title="按歌名与歌手匹配本地媒体库，生成/覆盖同名镜像歌单">${MPUtils.icon('icon:download')} 重建为本地歌单</button>
                    ` : ''}
                    ${header.playlistId ? `
                        <button class="btn" data-hero-action="rename-pl">${MPUtils.icon('icon:pencil')} 重命名</button>
                        <button class="btn btn-danger" data-hero-action="delete-pl">${MPUtils.icon('icon:trash-2')} 删除</button>
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
