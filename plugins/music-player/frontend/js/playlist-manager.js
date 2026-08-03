// ===== 播放列表管理 =====
class PlaylistManager {
    constructor(app) {
        this.app = app;
        this._playlists = [];
    }

    async loadPlaylists() {
        try {
            this._playlists = await Bridge.call('music_playlist_list') || [];
        } catch (e) {
            console.error('加载歌单失败:', e);
            this._playlists = [];
        }
        this.renderSidebar();
    }

    async createPlaylist(name) {
        try {
            const result = await Bridge.call('music_playlist_save', name, '', []);
            if (result.success) {
                this._playlists.push(result.playlist);
                this.renderSidebar();
                Toast.success('歌单已创建');
                return result.playlist;
            }
        } catch (e) {
            Toast.error('创建歌单失败');
        }
        return null;
    }

    async deletePlaylist(playlistId) {
        try {
            const result = await Bridge.call('music_playlist_delete', playlistId);
            if (result.success) {
                this._playlists = this._playlists.filter(p => p.id !== playlistId);
                this.renderSidebar();
                Toast.success('歌单已删除');
            }
        } catch (e) {
            Toast.error('删除歌单失败');
        }
    }

    async addToPlaylist(playlistId, songIds) {
        const pl = this._playlists.find(p => p.id === playlistId);
        if (!pl) return;
        const merged = [...new Set([...pl.song_ids, ...songIds])];
        try {
            const result = await Bridge.call('music_playlist_save', pl.name, playlistId, merged);
            if (result.success) {
                pl.song_ids = merged;
                this.renderSidebar();
                Toast.success(`已添加到「${pl.name}」`);
            }
        } catch (e) {
            Toast.error('添加失败');
        }
    }

    async removeFromPlaylist(playlistId, songId) {
        const pl = this._playlists.find(p => p.id === playlistId);
        if (!pl) return;
        const filtered = pl.song_ids.filter(id => id !== songId);
        try {
            const result = await Bridge.call('music_playlist_save', pl.name, playlistId, filtered);
            if (result.success) {
                pl.song_ids = filtered;
                this.renderSidebar();
            }
        } catch (e) { }
    }

    async getPlaylistSongs(playlistId) {
        const pl = this._playlists.find(p => p.id === playlistId);
        if (!pl || !pl.song_ids.length) return [];
        const songs = [];
        for (const sid of pl.song_ids) {
            try {
                const song = await Bridge.call('music_get_song', sid);
                if (song && song.id) songs.push(song);
            } catch (e) { }
        }
        return songs;
    }

    getPlaylistById(id) {
        return this._playlists.find(p => p.id === id);
    }

    // ===== 侧边栏渲染 =====
    renderSidebar() {
        const container = document.getElementById('playlist-list');
        if (!container) return;

        if (this._playlists.length === 0) {
            container.innerHTML = '<div class="playlist-item-empty">暂无歌单</div>';
            return;
        }

        container.innerHTML = this._playlists.map(pl => `
            <div class="playlist-item-sidebar" data-pl-id="${pl.id}">
                <span class="pl-icon">📋</span>
                <span class="pl-name">${MusicUtils.escapeHtml(pl.name)}</span>
                <span class="pl-count">${pl.song_ids.length}</span>
            </div>
        `).join('');

        container.querySelectorAll('.playlist-item-sidebar').forEach(item => {
            item.addEventListener('click', () => {
                const plId = item.dataset.plId;
                this._openPlaylist(plId);
            });
            item.addEventListener('contextmenu', (e) => {
                e.preventDefault();
                const plId = item.dataset.plId;
                const pl = this.getPlaylistById(plId);
                if (!pl) return;
                if (confirm(`确定删除歌单「${pl.name}」?`)) {
                    this.deletePlaylist(plId);
                }
            });
        });
    }

    async _openPlaylist(playlistId) {
        const pl = this.getPlaylistById(playlistId);
        if (!pl) return;

        const songs = await this.getPlaylistSongs(playlistId);
        this.app._showPlaylistDetail(pl, songs);
    }
}
