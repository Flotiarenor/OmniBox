// frontend/js/bridge.js
const bridge = {
    FILE_SERVER: 'http://127.0.0.1:18080',
    _api: null,

    async init() {
        this._api = await this._waitForApi();
    },

    _waitForApi(maxWait = 5000) {
        return new Promise((resolve, reject) => {
            const start = Date.now();
            const check = () => {
                if (window.pywebview && window.pywebview.api) {
                    resolve(window.pywebview.api);
                } else if (Date.now() - start > maxWait) {
                    reject(new Error('PyWebView API 不可用'));
                } else {
                    setTimeout(check, 100);
                }
            };
            check();
        });
    },

    async call(method, ...args) {
        if (!this._api) await this.init();
        try {
            return await this._api[method](...args);
        } catch (e) {
            console.error(`API调用失败: ${method}`, e);
            throw e;
        }
    },

    // ===== 文件操作 =====
    async fileListDir(path = '') {
        return this.call('file_list_dir', path);
    },

    async fileDelete(paths) {
        return this.call('file_delete', paths);
    },

    async fileMove(paths, dest) {
        return this.call('file_move', paths, dest);
    },

    // ===== 图片操作 =====
    async imageList(path = '', page = 1, perPage = null, sortBy = null, sortOrder = null) {
        return this.call('image_list', path, page, perPage, sortBy, sortOrder);
    },

    // ===== 漫画操作 =====
    async mangaList() {
        return this.call('manga_list');
    },

    async mangaSearch(keyword) {
        return this.call('manga_search', keyword);
    },

    async mangaGetDetail(folderName) {
        return this.call('manga_get_detail', folderName);
    },

    async mangaGetPages(folderName, chapterPath = '') {
        return this.call('manga_get_pages', folderName, chapterPath);
    },

    async mangaToggleFavorite(folderName) {
        return this.call('manga_toggle_favorite', folderName);
    },

    async mangaUpdateRecent(folderName, page = 0) {
        return this.call('manga_update_recent', folderName, page);
    },
    async novelList() {
    return this.call('novel_list');
    },
    async novelGetChapters(novelId) {
        return this.call('novel_get_chapters', novelId);
    },
    async novelGetContent(novelId, chapterIndex, encoding = 'auto') {
        return this.call('novel_get_content', novelId, chapterIndex, encoding);
    },
    async novelUpdateProgress(novelId, chapterIndex, scrollPosition, encoding = 'auto') {
        return this.call('novel_update_progress', novelId, chapterIndex, scrollPosition, encoding);
    },
    async novelGetContentStream(novelId, chapterIndex, chunkSize = 4096, chunkIndex = 0) {
        return this.call('novel_get_content_stream', novelId, chapterIndex, chunkSize, chunkIndex);
    },

    // ===== 下载操作（精简后） =====
    async downloadSubmit(albumId) {
        return this.call('download_submit', albumId);
    },

    async downloadList() {
        return this.call('download_list');
    },

    async downloadGetAlbumInfo(albumId) {
        return this.call('download_get_album_info', albumId);
    },

    async dialogSelectDirectory() {
        return this.call('dialog_select_directory');
    },

    // ===== 设置操作 =====
    async settingsGet(path = '') {
        return this.call('settings_get', path);
    },

    async settingsSave(path, settings) {
        return this.call('settings_save', path, settings);
    },

    // ===== 便捷方法：获取资源URL =====
    originalUrl(path) {
        return `${this.FILE_SERVER}/images/${path}`;
    },

    thumbUrl(path) {
        return `${this.FILE_SERVER}/thumbs/${path}`;
    },

    mangaCoverUrl(folderName) {
        return `${this.FILE_SERVER}/covers/${folderName}`;
    }
};