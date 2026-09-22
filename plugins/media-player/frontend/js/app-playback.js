// ============================================================
// OmniBox 媒体播放器 — 播放状态反馈、队列与歌单交互
//
// 本文件是 MediaPlayerApp.prototype 的一个分片：类骨架与构造函数在 app.js，
// 这里用 Object.assign 把方法扩回同一个原型。**加载顺序是硬约束** —— 必须在
// app.js 之后、实例化之前（见 index.html），契约由 tests/js/media_player_app_split.mjs 把关。
// ============================================================

Object.assign(MediaPlayerApp.prototype, {

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
            videoModeBtn.innerHTML = MPUtils.icon(this.core.videoMode ? 'icon:clapperboard' : 'icon:music');
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
    },

    _updateStageCover(item) {
        const coverEl = document.getElementById('mp-stage-cover');
        const img = document.getElementById('mp-stage-cover-img');
        const url = item ? MPUtils.coverUrl(item) : '';
        const fallback = () => {
            // 清除失败状态：否则同一元素再次设置相同 src 时浏览器不会重新加载，
            // 后续抽帧成功也无法通过重设 src 刷新（历史空白封面 bug 根因之一）
            img.removeAttribute('src');
            img.style.display = 'none';
            coverEl.classList.add('no-cover');
            coverEl.dataset.icon = item ? MPUtils.itemIcon(item) : 'icon:music';
        };
        if (url) {
            // 相同 src 时浏览器不会重新加载（可能覆盖抽帧成功的 &v= 状态）→ 强制重载
            img.src = (img.getAttribute('src') === url)
                ? url + (url.includes('?') ? '&' : '?') + 'r=' + Date.now()
                : url;
            img.style.display = '';
            img.onerror = () => {
                // 视频封面 404：先尝试前端抽帧（播放中的视频插队优先），失败再降级
                if (item && item.kind === 'video' && item.id) {
                    MediaFrameExtractor.request(item.id, img, fallback, true);
                } else {
                    fallback();
                }
            };
            coverEl.classList.remove('no-cover');
        } else {
            img.removeAttribute('src');
            img.style.display = 'none';
            coverEl.classList.add('no-cover');
            coverEl.dataset.icon = item ? MPUtils.itemIcon(item) : 'icon:music';
        }
        coverEl.classList.toggle('spinning', !!(item && item.kind === 'audio' && this._playing));
        coverEl.classList.toggle('paused', !!(item && item.kind === 'audio' && !this._playing));
    },

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
    },

    _updatePlayerCover(item) {
        const cover = document.getElementById('player-cover');
        if (!cover) return;
        const url = item ? MPUtils.coverUrl(item) : '';
        cover.innerHTML = url
            ? MPUtils.coverImg(url, MPUtils.itemIcon(item), '', item && item.kind === 'video' ? item.id : '')
            : MPUtils.itemIcon(item);
    },

    onPlayStateChange(playing) {
        this._playing = playing;
        const playBtn = document.getElementById('btn-play-pause');
        playBtn.innerHTML = MPUtils.icon(playing ? 'icon:pause' : 'icon:play');
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
        bigBtn.innerHTML = MPUtils.icon(playing ? 'icon:pause' : 'icon:play');

        this._updateMiniEq(playing);
        if (playing && this.isFullscreen) this._scheduleAutoHide();
        if (!playing && this.isFullscreen) this._showControls();
    },

    _updateMiniEq(playing) {
        document.getElementById('mp-mini-eq').classList.toggle('playing', !!playing);
    },

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
    },

    updatePlayModeUI() {
        const btn = document.getElementById('btn-play-mode');
        if (!btn) return;
        const icons = ['icon:repeat', 'icon:shuffle', 'icon:repeat-1'];
        const titles = ['顺序播放', '随机播放', '单曲循环'];
        btn.innerHTML = MPUtils.icon(icons[this.core.playMode] || 'icon:repeat');
        btn.title = titles[this.core.playMode] || '顺序播放';
    },

    updateVolumeUI() {
        const bar = document.getElementById('volume-bar');
        const icon = document.getElementById('btn-volume-icon');
        if (!bar) return;
        bar.value = this.core.volume;
        MPUtils.setRangePercent(bar, this.core.volume * 100);
        if (icon) {
            if (this.core._muted || this.core.volume === 0) icon.innerHTML = MPUtils.icon('icon:volume-x');
            else if (this.core.volume < 0.35) icon.innerHTML = MPUtils.icon('icon:volume-1');
            else if (this.core.volume < 0.7) icon.innerHTML = MPUtils.icon('icon:volume-1');
            else icon.innerHTML = MPUtils.icon('icon:volume-2');
        }
    },

    _updateFavButton(itemId) {
        const btn = document.getElementById('btn-toggle-fav');
        if (!btn) return;
        const fav = itemId && this.favIds.has(itemId);
        btn.innerHTML = MPUtils.icon('icon:heart');
        btn.classList.toggle('fav-active', !!fav);
        btn.title = fav ? '取消喜欢' : '喜欢';
    },

    async _toggleCurrentFavorite() {
        const item = this.core.currentItem;
        if (!item) return;
        try {
            const result = await Bridge.call('media_toggle_favorite', item.id);
            if (result.is_fav) this.favIds.add(item.id);
            else this.favIds.delete(item.id);
            item.is_fav = result.is_fav;
            const btn = document.getElementById('btn-toggle-fav');
            btn.innerHTML = MPUtils.icon('icon:heart');
            btn.classList.toggle('fav-active', result.is_fav);
            btn.classList.remove('fav-active');
            void btn.offsetWidth; // 重置动画
            if (result.is_fav) btn.classList.add('fav-active');
            this._refreshRowState();
        } catch (e) {
            Toast.error('操作失败');
        }
    },

    _highlightRows(item) {
        document.querySelectorAll('.mp-row').forEach(row => {
            row.classList.toggle('active', !!item && row.dataset.id === item.id);
        });
    },

    // ============================================================
    // 队列
    // ============================================================
    _toggleQueue() {
        const popup = document.getElementById('queue-popup');
        if (popup.classList.contains('hidden')) {
            this._renderQueue();
            this._positionPops();
            popup.classList.remove('hidden');
        } else {
            popup.classList.add('hidden');
        }
    },

    _clearQueue() {
        this.core.queue = [];
        this.core.currentIndex = -1;
        this.core.saveQueueState();
        this._renderQueue();
        Toast.info('播放队列已清空');
    },

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
                    <button class="q-remove" data-remove-idx="${idx}" title="移除">${MPUtils.icon('icon:x')}</button>
                </div>`;
        }).join('');

        list.querySelectorAll('.mp-queue-item').forEach(row => {
            row.addEventListener('click', (e) => {
                const remove = e.target.closest('.q-remove');
                if (remove) {
                    e.stopPropagation();
                    const idx = parseInt(remove.dataset.removeIdx, 10);
                    this.core.queue.splice(idx, 1);
                    this.core.saveQueueState();
                    // 移除的是当前曲目之前的条目：currentIndex 前移保持指向原曲目；
                    // 移除的恰是当前曲目：index 不变（自动指向队列中的下一首）
                    if (this.core.currentIndex > idx) this.core.currentIndex--;
                    else if (this.core.currentIndex >= this.core.queue.length) {
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
    },

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
    },

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
    },

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
                <span>${MPUtils.icon('icon:list-music')}</span>
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
    },

    showPlaylistMenu(playlistId, e) {
        this._playlistMenuId = playlistId;
        this._closePlaylistMenu();

        const menu = document.createElement('div');
        menu.className = 'mp-context-menu';
        menu.id = 'mp-context-menu';
        menu.innerHTML = `
            <button data-menu-act="rename">${MPUtils.icon('icon:pencil')} 重命名</button>
            <button data-menu-act="delete" class="danger">${MPUtils.icon('icon:trash-2')} 删除歌单</button>`;
        menu.style.left = `${Math.min(e.clientX, window.innerWidth - 150)}px`;
        menu.style.top = `${Math.min(e.clientY, window.innerHeight - 100)}px`;
        document.body.appendChild(menu);
        this._contextMenuEl = menu;

        menu.addEventListener('click', async (ev) => {
            // 必须用 closest 取动作：菜单项里有图标，点到图标字形时 ev.target 是 <svg>，
            // 它没有 dataset.menuAct，直接读会得到 undefined —— 菜单只是关掉、什么都不做。
            const actionEl = ev.target && ev.target.closest
                ? ev.target.closest('[data-menu-act]') : null;
            const act = actionEl ? actionEl.dataset.menuAct : '';
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
    },

    _closePlaylistMenu() {
        if (this._contextMenuEl) {
            this._contextMenuEl.remove();
            this._contextMenuEl = null;
        }
        this._playlistMenuId = '';
    },

});
