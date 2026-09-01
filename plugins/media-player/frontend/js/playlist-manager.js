// ===== 歌单管理（音视频混编） =====
class MediaPlaylistManager {
    constructor(app) {
        this.app = app;
        this.playlists = [];
        this.currentId = '';
    }

    async load() {
        try {
            this.playlists = await Bridge.call('media_playlist_list') || [];
        } catch (e) {
            console.error('加载歌单失败:', e);
            this.playlists = [];
        }
        this.renderSidebar();
    }

    async create(name) {
        try {
            const result = await Bridge.call('media_playlist_save', name, '', []);
            if (result.success) {
                this.playlists.push(result.playlist);
                this.renderSidebar();
                Toast.success(`歌单「${name}」已创建`);
                return result.playlist;
            }
            Toast.error(result.error || '创建失败');
        } catch (e) {
            Toast.error('创建歌单失败');
        }
        return null;
    }

    async rename(playlistId, name) {
        const pl = this.playlists.find(p => p.id === playlistId);
        if (!pl) return false;
        try {
            // 必须带上现有 item_ids：后端按传入列表整体覆盖，只传名称会清空歌单
            const result = await Bridge.call('media_playlist_save', name, playlistId, pl.item_ids || []);
            if (result.success) {
                pl.name = name;
                this.renderSidebar();
                return true;
            }
        } catch (e) { }
        return false;
    }

    async delete(playlistId) {
        try {
            const result = await Bridge.call('media_playlist_delete', playlistId);
            if (result.success) {
                this.playlists = this.playlists.filter(p => p.id !== playlistId);
                if (this.currentId === playlistId) this.currentId = '';
                this.renderSidebar();
                Toast.success('歌单已删除');
                return true;
            }
        } catch (e) {
            Toast.error('删除失败');
        }
        return false;
    }

    async get(playlistId) {
        try {
            const data = await Bridge.call('media_playlist_get', playlistId);
            if (data && data.id) return data;
        } catch (e) { }
        return null;
    }

    async addItems(playlistId, itemIds) {
        const pl = this.playlists.find(p => p.id === playlistId);
        if (!pl) return false;
        const merged = [...new Set([...(pl.item_ids || []), ...itemIds])];
        try {
            const result = await Bridge.call('media_playlist_save', pl.name, playlistId, merged);
            if (result.success) {
                pl.item_ids = merged;
                this.renderSidebar();
                return true;
            }
        } catch (e) { }
        return false;
    }

    async removeItem(playlistId, itemId) {
        const pl = this.playlists.find(p => p.id === playlistId);
        if (!pl) return false;
        const filtered = (pl.item_ids || []).filter(id => id !== itemId);
        try {
            const result = await Bridge.call('media_playlist_save', pl.name, playlistId, filtered);
            if (result.success) {
                pl.item_ids = filtered;
                this.renderSidebar();
                return true;
            }
        } catch (e) { }
        return false;
    }

    async updateItemIds(playlistId, itemIds) {
        const pl = this.playlists.find(p => p.id === playlistId);
        if (!pl) return false;
        try {
            const result = await Bridge.call('media_playlist_save', pl.name, playlistId, itemIds);
            if (result.success) {
                pl.item_ids = itemIds;
                this.renderSidebar();
                return true;
            }
        } catch (e) { }
        return false;
    }

    // ===== 侧边栏渲染 =====
    renderSidebar() {
        const container = document.getElementById('mp-playlist-list');
        if (!container) return;

        if (!this.playlists.length) {
            container.innerHTML = '<div class="mp-playlist-empty">暂无歌单，点击 ＋ 创建</div>';
            return;
        }

        container.innerHTML = this.playlists.map(pl => `
            <div class="mp-playlist-item ${pl.id === this.currentId ? 'active' : ''}" data-pl-id="${pl.id}">
                <span class="pl-name">📋 ${MPUtils.escapeHtml(pl.name)}</span>
                <span class="pl-count">${(pl.item_ids || []).length}</span>
                <button class="pl-more" data-pl-id="${pl.id}" title="更多操作">⋯</button>
            </div>
        `).join('');

        container.querySelectorAll('.mp-playlist-item').forEach(row => {
            const id = row.dataset.plId;
            row.addEventListener('click', (e) => {
                if (e.target.classList.contains('pl-more')) return;
                this.app.openPlaylist(id);
            });
            row.querySelector('.pl-more').addEventListener('click', (e) => {
                e.stopPropagation();
                this.app.showPlaylistMenu(id, e);
            });
        });

        if (this.app && this.app._updateStats) this.app._updateStats();
    }
}
