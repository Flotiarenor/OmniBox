// ===== 播放进度本地存储（按媒体 item.id 记忆） =====
const MediaProgressStore = {
    STORAGE_KEY: 'cubbyMediaProgress',

    get(item) {
        const itemId = typeof item === 'string' ? item : (item && item.id);
        try {
            const data = JSON.parse(localStorage.getItem(this.STORAGE_KEY) || '{}');
            const current = Number(data[itemId]) || 0;
            if (current > 0) return current > 2 ? current : 0;
        } catch (e) { }
        return 0;
    },

    save(itemId, seconds) {
        if (!itemId) return;
        try {
            const data = JSON.parse(localStorage.getItem(this.STORAGE_KEY) || '{}');
            data[itemId] = seconds;
            localStorage.setItem(this.STORAGE_KEY, JSON.stringify(data));
        } catch (e) {
            console.warn('保存播放进度失败', e);
        }
    },

    clear(itemId) {
        try {
            const data = JSON.parse(localStorage.getItem(this.STORAGE_KEY) || '{}');
            if (itemId in data) {
                delete data[itemId];
                localStorage.setItem(this.STORAGE_KEY, JSON.stringify(data));
            }
        } catch (e) { }
    },
};
