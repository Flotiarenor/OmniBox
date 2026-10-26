// ===== 歌单管理（音视频混编） =====
// 歌单的侧栏渲染已上移到壳：manifest 的 `frontend.chrome.nav` 声明了「我的歌单」动态组
// （id `playlists`），运行期由 `syncNav()` 把这组 items 整组上报（每个歌单一项 +
// 末尾的「新建歌单」）。本文件只管数据与后端调用。
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
        this.syncNav();
    }

    async create(name) {
        try {
            const result = await Bridge.call('media_playlist_save', name, '', []);
            if (result.success) {
                this.playlists.push(result.playlist);
                this.syncNav();
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
                this.syncNav();
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
                this.syncNav();
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
                this.syncNav();
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
                this.syncNav();
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
                this.syncNav();
                return true;
            }
        } catch (e) { }
        return false;
    }

    /** 把歌单列表作为壳侧「我的歌单」导航组上报；统计（歌单数）随之刷新。 */
    syncNav() {
        if (!this.app) return;
        this.app._updateStats()
            .then(() => this.app._pushChrome())
            .catch(() => this.app._pushChrome());
    }
}
