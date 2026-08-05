// ===== 音乐播放器主应用 =====
class MusicPlayer {
    constructor() {
        this.player = null;
        this.playlists = null;
        this.lyrics = null;

        this._albums = [];
        this._allSongs = [];
        this._currentView = 'albums';
        this._currentAlbum = null;
        this._currentPlaylistView = null;
        this._initialized = false;
        this._searchDebounced = null;
    }

    async init() {
        if (this._initialized) return;
        this._initialized = true;

        this.player = new PlayerCore(this);
        this.playlists = new PlaylistManager(this);
        this.lyrics = new LyricsDisplay(this);

        this._bindUI();
        this._bindKeyboard();
        this._loadVolume();

        await this._scanAndLoad();
        this._updateStats();
        this._restorePlayback();
        this._restorePlayMode();
    }

    async _restorePlayMode() {
        try {
            const mode = await Bridge.call('music_get_config', 'last_play_mode', 0);
            if (typeof mode === 'number' && mode >= 0 && mode <= 2) {
                this.player.playMode = mode;
                const btn = document.getElementById('btn-play-mode');
                if (btn) {
                    const icons = ['🔁', '🔀', '🔂'];
                    const titles = ['顺序播放', '随机播放', '单曲循环'];
                    btn.textContent = icons[mode];
                    btn.title = titles[mode];
                }
            }
        } catch (e) {}
    }

    // ===== 扫描与加载 =====
    async _scanAndLoad() {
        try {
            const scanResult = await Bridge.call('music_scan', false);
            console.log('扫描完成:', scanResult);
        } catch (e) {
            console.warn('扫描失败:', e);
        }
        await this._loadAlbums();
        await this.playlists.loadPlaylists();
    }

    async _loadAlbums() {
        try {
            this._albums = await Bridge.call('music_albums', 'name') || [];
            this._renderAlbums(this._albums);
        } catch (e) {
            console.error('加载专辑失败:', e);
            this._renderAlbums([]);
        }
    }

    async _loadAllSongs() {
        try {
            this._allSongs = await Bridge.call('music_search', '') || [];
        } catch (e) {
            this._allSongs = [];
        }
    }

    _updateStats() {
        const el = document.getElementById('sidebar-stats');
        if (!el) return;
        const totalAlbums = this._albums.length;
        const totalSongs = this._albums.reduce((sum, a) => sum + (a.song_count || 0), 0);
        el.textContent = `${totalAlbums} 张专辑 · ${totalSongs} 首歌曲`;
    }

    // ===== UI 绑定 =====
    _bindUI() {
        this._searchDebounced = MusicUtils.debounce((kw) => this._doSearch(kw), 300);

        document.getElementById('search-input').addEventListener('input', (e) => {
            this._searchDebounced(e.target.value);
        });

        document.querySelectorAll('.nav-item').forEach(item => {
            item.addEventListener('click', () => {
                document.querySelectorAll('.nav-item').forEach(n => n.classList.remove('active'));
                item.classList.add('active');
                const view = item.dataset.view;
                this._switchView(view);
            });
        });

        document.getElementById('btn-play-pause').addEventListener('click', () => this.player.togglePlay());
        document.getElementById('btn-prev').addEventListener('click', () => this.player.prev());
        document.getElementById('btn-next').addEventListener('click', () => this.player.next());
        document.getElementById('btn-play-mode').addEventListener('click', () => this.player.cyclePlayMode());

        const progressBar = document.getElementById('progress-bar');
        progressBar.addEventListener('input', () => {
            if (this.player.audio.duration) {
                this.player.audio.currentTime = progressBar.value;
            }
        });

        const lyricsProgressBar = document.getElementById('lyrics-progress-bar');
        if (lyricsProgressBar) {
            lyricsProgressBar.addEventListener('input', () => {
                if (this.player.audio.duration) {
                    this.player.audio.currentTime = parseFloat(lyricsProgressBar.value);
                }
            });
        }

        const volumeBar = document.getElementById('volume-bar');
        volumeBar.addEventListener('input', () => {
            this.player.volume = parseFloat(volumeBar.value);
        });
        document.getElementById('btn-volume-icon').addEventListener('click', () => this.player.toggleMute());
        document.getElementById('btn-eq').addEventListener('click', () => this._toggleEQ());
        document.getElementById('player-cover').addEventListener('click', () => this._openLyrics());
        document.getElementById('btn-lyrics-back').addEventListener('click', () => this.lyrics.hide());
        document.getElementById('btn-lyrics-settings-page').addEventListener('click', () => this._openLyricsSettings());
        document.getElementById('btn-eq-close').addEventListener('click', () => this._hideEQ());
        document.getElementById('btn-eq-reset').addEventListener('click', () => this._resetEQ());
        document.getElementById('btn-eq-save').addEventListener('click', () => this._saveEQPreset());
        document.getElementById('eq-preset-select').addEventListener('change', (e) => this._loadEQPreset(e.target.value));

        this._buildEQBands();
        this._loadEQPresets();

        document.getElementById('btn-queue').addEventListener('click', () => this._toggleQueue());
        document.getElementById('btn-clear-queue').addEventListener('click', () => {
            this.player.queue = [];
            this.player.currentIndex = -1;
            this._renderQueue();
            document.getElementById('queue-popup').classList.add('hidden');
        });

        document.getElementById('btn-settings').addEventListener('click', () => {
            openSettingsModal({
                title: '音乐库设置',
                successMessage: '设置已保存',
                onSave: async (values) => {
                    const result = await Bridge.call('save_settings', values);
                    if (result.success) {
                        await this._scanAndLoad();
                        this._updateStats();
                        return { success: true };
                    }
                    return result;
                }
            });
        });

        document.getElementById('btn-rescan').addEventListener('click', async () => {
            Toast.info('正在扫描音乐文件...');
            try {
                const result = await Bridge.call('music_scan', true);
                Toast.success(`扫描完成: ${result.total_songs} 首歌曲, ${result.total_albums} 张专辑`);
                await this._loadAlbums();
                this._updateStats();
            } catch (e) {
                Toast.error('扫描失败');
            }
        });

        // 新建歌单
        document.getElementById('btn-new-playlist').addEventListener('click', () => {
            document.getElementById('modal-new-playlist').classList.add('active');
            document.getElementById('input-playlist-name').value = '';
            document.getElementById('input-playlist-name').focus();
        });
        document.getElementById('btn-modal-cancel').addEventListener('click', () => {
            document.getElementById('modal-new-playlist').classList.remove('active');
        });
        document.getElementById('btn-modal-confirm').addEventListener('click', async () => {
            const name = document.getElementById('input-playlist-name').value.trim();
            if (!name) { Toast.error('请输入歌单名称'); return; }
            document.getElementById('modal-new-playlist').classList.remove('active');
            await this.playlists.createPlaylist(name);
        });
        document.getElementById('modal-new-playlist').addEventListener('click', (e) => {
            if (e.target === document.getElementById('modal-new-playlist')) {
                document.getElementById('modal-new-playlist').classList.remove('active');
            }
        });

        // 队列弹窗外点击关闭
        document.addEventListener('click', (e) => {
            const popup = document.getElementById('queue-popup');
            if (!popup.classList.contains('hidden') &&
                !popup.contains(e.target) &&
                e.target !== document.getElementById('btn-queue')) {
                popup.classList.add('hidden');
            }
        });
    }

    // ===== 视图切换 =====
    async _switchView(view) {
        this._currentView = view;
        const views = ['view-albums', 'view-songs', 'view-favorites', 'view-recent', 'view-playlist-detail'];
        views.forEach(id => {
            const el = document.getElementById(id);
            if (el) el.classList.add('hidden');
        });

        if (view === 'albums') {
            document.getElementById('search-input').value = '';
            await this._loadAlbums();
            document.getElementById('view-albums').classList.remove('hidden');
        } else if (view === 'songs') {
            document.getElementById('search-input').value = '';
            await this._loadAllSongs();
            this._renderSongList('view-songs', 'song-list', this._allSongs, '全部歌曲', '');
            document.getElementById('view-songs').classList.remove('hidden');
        } else if (view === 'favorites') {
            document.getElementById('search-input').value = '';
            const state = await Bridge.call('music_get_state');
            this._renderSongList('view-favorites', 'favorites-list', state.favorites || [], '我喜欢', '');
            document.getElementById('view-favorites').classList.remove('hidden');
        } else if (view === 'recent') {
            document.getElementById('search-input').value = '';
            const state = await Bridge.call('music_get_state');
            this._renderSongList('view-recent', 'recent-list', state.recent || [], '最近播放', '');
            document.getElementById('view-recent').classList.remove('hidden');
        }
    }

    // ===== 专辑网格渲染 =====
    _renderAlbums(albums) {
        const grid = document.getElementById('album-grid');
        if (!grid) return;

        if (!albums || albums.length === 0) {
            grid.innerHTML = `
                <div class="empty-state" style="grid-column: 1/-1;">
                    <div class="empty-icon">📀</div>
                    <div class="empty-text">暂无音乐，请先设置音乐库目录</div>
                </div>`;
            return;
        }

        grid.innerHTML = albums.map(album => {
            const coverHtml = album.cover_path
                ? `<img src="${Bridge.originalUrl(album.cover_path)}" alt="cover" loading="lazy">`
                : '<div class="album-cover-placeholder">💿</div>';
            return `
                <div class="album-card" data-album-key="${MusicUtils.escapeHtml(album.key)}">
                    <div class="album-cover-wrapper">
                        ${coverHtml}
                        <button class="album-card-play" data-album-key="${MusicUtils.escapeHtml(album.key)}">▶</button>
                    </div>
                    <div class="album-card-body">
                        <div class="album-card-title">${MusicUtils.escapeHtml(album.name)}</div>
                        <div class="album-card-artist">${MusicUtils.escapeHtml(album.artist)}</div>
                        <div class="album-card-meta">${album.song_count} 首</div>
                    </div>
                </div>`;
        }).join('');

        grid.querySelectorAll('.album-card').forEach(card => {
            card.addEventListener('click', (e) => {
                if (e.target.classList.contains('album-card-play')) return;
                const key = card.dataset.albumKey;
                this._openAlbum(key);
            });
        });

        grid.querySelectorAll('.album-card-play').forEach(btn => {
            btn.addEventListener('click', async (e) => {
                e.stopPropagation();
                const key = btn.dataset.albumKey;
                const songs = await Bridge.call('music_album_songs', key);
                if (songs && songs.length > 0) {
                    this.player.setQueue(songs, 0);
                }
            });
        });
    }

    async _openAlbum(albumKey) {
        try {
            const songs = await Bridge.call('music_album_songs', albumKey);
            const album = this._albums.find(a => a.key === albumKey);
            this._currentAlbum = album;

            document.querySelectorAll('.nav-item').forEach(n => n.classList.remove('active'));
            Object.values(document.getElementsByClassName('view-content') || {}).forEach(el => {
                // handled in _renderSongList
            });

            this._renderSongList(
                'view-songs', 'song-list', songs,
                album ? album.name : '专辑详情',
                album ? album.artist : '',
                album ? album.cover_path : ''
            );

            document.getElementById('view-albums').classList.add('hidden');
            document.getElementById('view-favorites').classList.add('hidden');
            document.getElementById('view-recent').classList.add('hidden');
            document.getElementById('view-playlist-detail').classList.add('hidden');
            document.getElementById('view-songs').classList.remove('hidden');
        } catch (e) {
            Toast.error('加载专辑失败');
        }
    }

    // ===== 歌曲列表渲染 =====
    _renderSongList(viewId, listId, songs, title, subtitle, coverPath) {
        const view = document.getElementById(viewId);
        const list = document.getElementById(listId);
        const header = document.getElementById('song-list-header');

        if (!view || !list) return;

        if (header && title) {
            const coverHtml = coverPath
                ? `<img src="${Bridge.originalUrl(coverPath)}" alt="cover">`
                : '<div class="cover-placeholder">💿</div>';
            header.innerHTML = `
                <span class="back-btn" id="btn-back-to-albums">←</span>
                <div class="header-cover">${coverHtml}</div>
                <div class="header-info">
                    <h2>${MusicUtils.escapeHtml(title)}</h2>
                    <div class="header-artist">${MusicUtils.escapeHtml(subtitle || '')}</div>
                    <button class="btn btn-sm" id="btn-play-all">▶ 播放全部</button>
                </div>`;

            const backBtn = document.getElementById('btn-back-to-albums');
            if (backBtn) {
                backBtn.addEventListener('click', () => this._switchView('albums'));
                backBtn.style.cursor = 'pointer';
            }
            const playAllBtn = document.getElementById('btn-play-all');
            if (playAllBtn) {
                playAllBtn.addEventListener('click', () => {
                    if (songs && songs.length > 0) {
                        this.player.setQueue(songs, 0);
                    }
                });
            }
        }

        if (!songs || songs.length === 0) {
            list.innerHTML = `
                <div class="empty-state">
                    <div class="empty-icon">🎶</div>
                    <div class="empty-text">暂无歌曲</div>
                </div>`;
            return;
        }

        const currentSong = this.player.queue.length > 0 && this.player.currentIndex >= 0
            ? this.player.queue[this.player.currentIndex] : null;

        list.innerHTML = songs.map((song, idx) => {
            const isActive = currentSong && currentSong.id === song.id;
            const coverHtml = song.cover_path
                ? `<img src="${Bridge.originalUrl(song.cover_path)}" alt="">`
                : '<div class="cover-placeholder">🎵</div>';
            const favIcon = song.is_fav ? '❤️' : '♡';
            const favClass = song.is_fav ? 'fav-active' : '';

            return `
                <div class="song-row ${isActive ? 'active' : ''}" data-song-id="${MusicUtils.escapeHtml(song.id)}" data-song-index="${idx}">
                    <span class="sr-index">${idx + 1}</span>
                    <div class="sr-cover">${coverHtml}</div>
                    <div class="sr-info">
                        <span class="sr-title">${MusicUtils.escapeHtml(song.title)}</span>
                        <span class="sr-artist-album">${MusicUtils.escapeHtml(song.artist)} · ${MusicUtils.escapeHtml(song.album)}</span>
                    </div>
                    <div class="sr-duration">${MusicUtils.formatTime(song.duration)}</div>
                    <div class="sr-actions">
                        <button class="sr-action-btn ${favClass}" data-fav="${song.id}" title="收藏">${favIcon}</button>
                        <button class="sr-action-btn" data-add-queue="${song.id}" title="添加到队列">＋</button>
                    </div>
                </div>`;
        }).join('');

        list.querySelectorAll('.song-row').forEach(row => {
            row.addEventListener('click', (e) => {
                if (e.target.closest('.sr-actions')) return;
                const idx = parseInt(row.dataset.songIndex);
                if (!isNaN(idx) && songs[idx]) {
                    this.player.setQueue(songs, idx);
                    Bridge.call('music_update_recent', songs[idx].id).catch(() => {});
                }
            });

            row.addEventListener('dblclick', (e) => {
                if (e.target.closest('.sr-actions')) return;
                const idx = parseInt(row.dataset.songIndex);
                if (!isNaN(idx) && songs[idx]) {
                    this.player.setQueue(songs, idx);
                    Bridge.call('music_update_recent', songs[idx].id).catch(() => {});
                }
            });
        });

        list.querySelectorAll('.sr-action-btn[data-fav]').forEach(btn => {
            btn.addEventListener('click', async (e) => {
                e.stopPropagation();
                const songId = btn.dataset.fav;
                try {
                    const result = await Bridge.call('music_toggle_favorite', songId);
                    btn.textContent = result.is_fav ? '❤️' : '♡';
                    btn.classList.toggle('fav-active', result.is_fav);
                } catch (err) {
                    Toast.error('操作失败');
                }
            });
        });

        list.querySelectorAll('.sr-action-btn[data-add-queue]').forEach(btn => {
            btn.addEventListener('click', async (e) => {
                e.stopPropagation();
                const songId = btn.dataset.addQueue;
                const song = songs.find(s => s.id === songId);
                if (song) {
                    this.player.addToQueue([song]);
                    Toast.info('已添加到队列');
                }
            });
        });
    }

    // ===== 歌单详情 =====
    _showPlaylistDetail(playlist, songs) {
        this._currentPlaylistView = playlist;

        document.querySelectorAll('.nav-item').forEach(n => n.classList.remove('active'));
        document.getElementById('view-albums').classList.add('hidden');
        document.getElementById('view-songs').classList.add('hidden');
        document.getElementById('view-favorites').classList.add('hidden');
        document.getElementById('view-recent').classList.add('hidden');
        document.getElementById('view-playlist-detail').classList.remove('hidden');

        const header = document.getElementById('playlist-detail-header');
        const list = document.getElementById('playlist-detail-list');

        if (header) {
            header.innerHTML = `
                <span class="back-btn" id="btn-back-playlist">←</span>
                <div class="header-cover">
                    <div class="cover-placeholder">📋</div>
                </div>
                <div class="header-info">
                    <h2>${MusicUtils.escapeHtml(playlist.name)}</h2>
                    <div class="header-artist">${songs.length} 首歌曲</div>
                    <button class="btn btn-sm" id="btn-playlist-play-all">▶ 播放全部</button>
                    <button class="btn btn-sm btn-danger" id="btn-delete-playlist" style="margin-left:8px;">🗑 删除歌单</button>
                </div>`;

            document.getElementById('btn-back-playlist').addEventListener('click', () => this._switchView('albums'));
            document.getElementById('btn-playlist-play-all').addEventListener('click', () => {
                if (songs.length > 0) this.player.setQueue(songs, 0);
            });
            document.getElementById('btn-delete-playlist').addEventListener('click', async () => {
                if (confirm(`确定删除歌单「${playlist.name}」?`)) {
                    await this.playlists.deletePlaylist(playlist.id);
                    this._switchView('albums');
                }
            });
        }

        this._renderSimpleSongList(list, songs, playlist.id);
    }

    _renderSimpleSongList(list, songs, playlistId) {
        if (!list) return;
        if (!songs || songs.length === 0) {
            list.innerHTML = `
                <div class="empty-state">
                    <div class="empty-icon">🎶</div>
                    <div class="empty-text">歌单暂无歌曲</div>
                </div>`;
            return;
        }

        const currentSong = this.player.queue.length > 0 && this.player.currentIndex >= 0
            ? this.player.queue[this.player.currentIndex] : null;

        list.innerHTML = songs.map((song, idx) => {
            const isActive = currentSong && currentSong.id === song.id;
            return `
                <div class="song-row ${isActive ? 'active' : ''}" data-song-id="${MusicUtils.escapeHtml(song.id)}" data-song-index="${idx}">
                    <span class="sr-index">${idx + 1}</span>
                    <div class="sr-cover"><div class="cover-placeholder">🎵</div></div>
                    <div class="sr-info">
                        <span class="sr-title">${MusicUtils.escapeHtml(song.title)}</span>
                        <span class="sr-artist-album">${MusicUtils.escapeHtml(song.artist)}</span>
                    </div>
                    <div class="sr-duration">${MusicUtils.formatTime(song.duration)}</div>
                    <div class="sr-actions">
                        <button class="sr-action-btn" data-remove="${song.id}" title="移出歌单">✕</button>
                    </div>
                </div>`;
        }).join('');

        list.querySelectorAll('.song-row').forEach(row => {
            row.addEventListener('click', (e) => {
                if (e.target.closest('.sr-actions')) return;
                const idx = parseInt(row.dataset.songIndex);
                if (!isNaN(idx) && songs[idx]) {
                    this.player.setQueue(songs, idx);
                    Bridge.call('music_update_recent', songs[idx].id).catch(() => {});
                }
            });
        });

        list.querySelectorAll('.sr-action-btn[data-remove]').forEach(btn => {
            btn.addEventListener('click', async (e) => {
                e.stopPropagation();
                const songId = btn.dataset.remove;
                await this.playlists.removeFromPlaylist(playlistId, songId);
                const updatedSongs = songs.filter(s => s.id !== songId);
                this._renderSimpleSongList(list, updatedSongs, playlistId);
                Toast.info('已移出歌单');
            });
        });
    }

    // ===== 搜索 =====
    async _doSearch(keyword) {
        if (this._currentView === 'albums' && !keyword) {
            await this._loadAlbums();
            document.getElementById('view-albums').classList.remove('hidden');
            document.getElementById('view-songs').classList.add('hidden');
            document.getElementById('view-favorites').classList.add('hidden');
            document.getElementById('view-recent').classList.add('hidden');
            document.getElementById('view-playlist-detail').classList.add('hidden');
            return;
        }

        if (!keyword) return;

        try {
            const results = await Bridge.call('music_search', keyword);
            if (results && results.length > 0) {
                document.getElementById('view-albums').classList.add('hidden');
                document.getElementById('view-favorites').classList.add('hidden');
                document.getElementById('view-recent').classList.add('hidden');
                document.getElementById('view-playlist-detail').classList.add('hidden');
                document.getElementById('view-songs').classList.remove('hidden');
                this._renderSongList('view-songs', 'song-list', results, `搜索: ${keyword}`, `${results.length} 个结果`);
            } else {
                this._renderSongList('view-songs', 'song-list', [], '搜索无结果', keyword);
            }
        } catch (e) {
            console.error('搜索失败:', e);
        }
    }

    // ===== 播放队列弹窗 =====
    _toggleQueue() {
        const popup = document.getElementById('queue-popup');
        if (popup.classList.contains('hidden')) {
            this._renderQueue();
            popup.classList.remove('hidden');
        } else {
            popup.classList.add('hidden');
        }
    }

    _renderQueue() {
        const list = document.getElementById('queue-list');
        const count = document.getElementById('queue-count');
        if (!list || !count) return;

        count.textContent = this.player.queue.length;

        if (this.player.queue.length === 0) {
            list.innerHTML = '<div class="queue-item" style="color:var(--text-secondary);">播放队列为空</div>';
            return;
        }

        list.innerHTML = this.player.queue.map((song, idx) => {
            const isActive = idx === this.player.currentIndex;
            return `
                <div class="queue-item ${isActive ? 'active' : ''}" data-queue-idx="${idx}">
                    <span class="q-index">${idx + 1}</span>
                    <span class="q-title">${MusicUtils.escapeHtml(song.title)}</span>
                    <span class="q-artist">${MusicUtils.escapeHtml(song.artist)}</span>
                    <span class="q-remove" data-remove-idx="${idx}">✕</span>
                </div>`;
        }).join('');

        list.querySelectorAll('.queue-item').forEach(item => {
            item.addEventListener('click', (e) => {
                if (e.target.classList.contains('q-remove')) {
                    e.stopPropagation();
                    const idx = parseInt(e.target.dataset.removeIdx);
                    this.player.queue.splice(idx, 1);
                    if (this.player.currentIndex >= this.player.queue.length) {
                        this.player.currentIndex = this.player.queue.length - 1;
                    }
                    this._renderQueue();
                    return;
                }
                const idx = parseInt(item.dataset.queueIdx);
                if (!isNaN(idx)) {
                    this.player.currentIndex = idx;
                    this.player._loadAndPlay();
                    this._renderQueue();
                    document.getElementById('queue-popup').classList.add('hidden');
                }
            });
        });
    }

    // ===== 收藏按钮（底部播放栏） =====
    async _updateFavButton(song) {
        const btn = document.getElementById('btn-toggle-fav');
        if (!btn || !song) return;
        btn.textContent = song.is_fav ? '❤️' : '♡';
        btn.style.display = 'inline-block';
        btn.onclick = async () => {
            try {
                const result = await Bridge.call('music_toggle_favorite', song.id);
                btn.textContent = result.is_fav ? '❤️' : '♡';
            } catch (e) {}
        };
    }

    // ===== 事件回调 (由 PlayerCore 调用) =====
    _onSongChange(song) {
        if (!song) return;
        this._updateFavButton(song);
        this.lyrics.loadForSong(song.id);
        Bridge.call('music_update_recent', song.id).catch(() => {});
        this._renderQueue();
        this._updateSongRowHighlight(song.id);
        if (this.lyrics.isVisible()) {
            const titleEl = document.getElementById('player-title');
            document.getElementById('lyrics-page-title').textContent = titleEl ? titleEl.textContent : '';
        }
    }

    _updateSongRowHighlight(songId) {
        const lists = document.querySelectorAll('.song-list, #favorites-list, #recent-list, #playlist-detail-list');
        lists.forEach(list => {
            list.querySelectorAll('.song-row.active').forEach(r => r.classList.remove('active'));
            const row = list.querySelector(`[data-song-id="${MusicUtils.escapeHtml(songId)}"]`);
            if (row) row.classList.add('active');
        });
    }

    _onTimeUpdate() {
        if (this.player && this.player.audio) {
            this.lyrics.update(this.player.audio.currentTime);
            this._drawLyricsViz();
        }
    }

    _drawLyricsViz() {
        const canvas = document.getElementById('lyrics-viz-canvas');
        if (!canvas || !this.lyrics.isVisible()) return;

        const analyser = this.player.getAnalyser();
        if (!analyser) return;

        const ctx = canvas.getContext('2d');
        canvas.width = canvas.offsetWidth || canvas.parentElement.offsetWidth;
        canvas.height = canvas.offsetHeight || canvas.parentElement.offsetHeight;
        const w = canvas.width, h = canvas.height;
        if (w === 0 || h === 0) return;

        const data = new Uint8Array(analyser.frequencyBinCount);
        analyser.getByteFrequencyData(data);

        ctx.clearRect(0, 0, w, h);
        const bars = 64;
        const barW = w / bars;
        for (let i = 0; i < bars; i++) {
            const v = data[Math.floor(i * data.length / bars)] / 255;
            const bh = v * h * 0.8;
            const hue = 200 + i * 2;
            const alpha = 0.15 + v * 0.5;
            ctx.fillStyle = `hsla(${hue}, 80%, 60%, ${alpha})`;
            ctx.fillRect(i * barW + 1, h - bh, barW - 2, bh);
        }
    }

    async _openLyrics() {
        await this.lyrics.refreshSettings();
        const titleEl = document.getElementById('player-title');
        this.lyrics.show(titleEl ? titleEl.textContent : '');
    }

    _openLyricsSettings() {
        openSettingsModal({
            title: '歌词设置',
            successMessage: '歌词设置已保存',
            onSave: async (values) => {
                const result = await Bridge.call('save_settings', values);
                if (result.success) {
                    await this.lyrics.refreshSettings();
                    return { success: true };
                }
                return result;
            }
        });
    }

    // ===== 均衡器 =====
    _buildEQBands() {
        const container = document.getElementById('eq-bands');
        if (!container) return;
        const freqs = this.player.EQ_FREQS;
        const bands = this.player.getEqBands();
        container.innerHTML = freqs.map((freq, i) => {
            const label = freq >= 1000 ? (freq / 1000).toFixed(0) + 'K' : freq;
            return `<div class="eq-band">
                <span class="eq-band-value">${bands[i] > 0 ? '+' + bands[i] : bands[i]}dB</span>
                <input type="range" class="eq-band-slider" min="-12" max="12" value="${bands[i]}"
                       data-eq-index="${i}" orient="vertical">
                <span class="eq-band-label">${label}</span>
            </div>`;
        }).join('');

        container.querySelectorAll('.eq-band-slider').forEach(slider => {
            slider.addEventListener('input', () => {
                const idx = parseInt(slider.dataset.eqIndex);
                const gain = parseFloat(slider.value);
                this.player.setEqBand(idx, gain);
                const valSpan = slider.parentElement.querySelector('.eq-band-value');
                if (valSpan) valSpan.textContent = (gain > 0 ? '+' + gain : gain) + 'dB';
            });
        });
    }

    _toggleEQ() {
        const panel = document.getElementById('eq-panel');
        panel.classList.toggle('hidden');
        if (!panel.classList.contains('hidden')) {
            this._buildEQBands();
        }
    }

    _hideEQ() {
        document.getElementById('eq-panel').classList.add('hidden');
    }

    _resetEQ() {
        this.player.EQ_FREQS.forEach((_, i) => this.player.setEqBand(i, 0));
        this.player.setEqBands(this.player.EQ_FREQS.map(() => 0));
        this._buildEQBands();
        document.getElementById('eq-preset-select').value = '';
    }

    async _loadEQPresets() {
        try {
            const presets = await Bridge.call('music_list_eq_presets') || [];
            const select = document.getElementById('eq-preset-select');
            presets.forEach(p => {
                const opt = document.createElement('option');
                opt.value = p.id;
                opt.textContent = p.name;
                select.appendChild(opt);
            });
        } catch (e) {}
    }

    _loadEQPreset(presetId) {
        if (!presetId) { this._resetEQ(); return; }
        this.player._ensureEQ();
        const select = document.getElementById('eq-preset-select');
        const selected = select.options[select.selectedIndex];
        if (!selected) return;
        Bridge.call('music_list_eq_presets').then(presets => {
            const preset = presets.find(p => p.id === presetId);
            if (preset && preset.bands) {
                this.player.setEqBands(preset.bands);
                this._buildEQBands();
            }
        }).catch(() => {});
    }

    async _saveEQPreset() {
        const bands = this.player.getEqBands();
        const name = prompt('输入预设名称:');
        if (!name) return;
        try {
            await Bridge.call('music_save_eq_preset', name, bands);
            Toast.success('预设已保存');
            const select = document.getElementById('eq-preset-select');
            select.innerHTML = '<option value="">内置预设</option>';
            this._loadEQPresets();
        } catch (e) {
            Toast.error('保存失败');
        }
    }

    // ===== 音量 =====
    async _loadVolume() {
        try {
            const vol = await Bridge.call('music_get_config', 'last_volume', 1.0);
            if (vol !== null && vol !== undefined) {
                this.player.volume = vol;
                document.getElementById('volume-bar').value = vol;
            }
        } catch (e) {
            const saved = localStorage.getItem('musicPlayerVolume');
            if (saved !== null) {
                this.player.volume = parseFloat(saved);
                document.getElementById('volume-bar').value = parseFloat(saved);
            }
        }
    }

    async _restorePlayback() {
        try {
            const pb = await Bridge.call('music_get_playback');
            if (!pb || !pb.song_id) return;
            await this._loadAllSongs();
            this.player.restorePlaybackState(pb, this._allSongs);
        } catch (e) {
            console.log('恢复播放状态失败:', e);
        }
    }

    // ===== 键盘快捷键 =====
    _bindKeyboard() {
        document.addEventListener('keydown', (e) => {
            if (e.target.tagName === 'INPUT' || e.target.tagName === 'TEXTAREA') return;

            switch (e.key) {
                case ' ':
                    e.preventDefault();
                    this.player.togglePlay();
                    break;
                case 'ArrowLeft':
                    e.preventDefault();
                    this.player.seekDelta(-5);
                    break;
                case 'ArrowRight':
                    e.preventDefault();
                    this.player.seekDelta(5);
                    break;
                case 'ArrowUp':
                    e.preventDefault();
                    this.player.volume = Math.min(1, this.player.volume + 0.05);
                    document.getElementById('volume-bar').value = this.player.volume;
                    break;
                case 'ArrowDown':
                    e.preventDefault();
                    this.player.volume = Math.max(0, this.player.volume - 0.05);
                    document.getElementById('volume-bar').value = this.player.volume;
                    break;
                case 'n':
                case 'N':
                    e.preventDefault();
                    this.player.next();
                    break;
                case 'p':
                case 'P':
                    e.preventDefault();
                    this.player.prev();
                    break;
                case 'm':
                case 'M':
                    e.preventDefault();
                    this.player.toggleMute();
                    break;
                case 'l':
                case 'L':
                    e.preventDefault();
                    this.lyrics.toggle();
                    break;
                case 'Escape':
                    if (this.lyrics.isVisible()) {
                        this.lyrics.hide();
                    }
                    if (!document.getElementById('queue-popup').classList.contains('hidden')) {
                        document.getElementById('queue-popup').classList.add('hidden');
                    }
                    if (!document.getElementById('modal-new-playlist').classList.contains('hidden') &&
                        document.getElementById('modal-new-playlist').classList.contains('active')) {
                        document.getElementById('modal-new-playlist').classList.remove('active');
                    }
                    break;
            }
        });
    }
}
