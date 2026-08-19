// ===== 播放进度本地存储 =====
const VideoProgressStore = {
    STORAGE_KEY: 'videoProgress',

    get(filePath) {
        try {
            const data = JSON.parse(localStorage.getItem(this.STORAGE_KEY) || '{}');
            return data[filePath] || 0;
        } catch {
            return 0;
        }
    },

    save(filePath, seconds) {
        try {
            const data = JSON.parse(localStorage.getItem(this.STORAGE_KEY) || '{}');
            data[filePath] = seconds;
            localStorage.setItem(this.STORAGE_KEY, JSON.stringify(data));
        } catch (e) {
            console.warn('保存进度失败', e);
        }
    },
};
