// ===== 播放进度本地存储（按媒体 item.id 记忆，兼容旧插件进度） =====
const MediaProgressStore = {
    STORAGE_KEY: 'omniboxMediaProgress',
    LEGACY_KEYS: ['musicProgress', 'videoProgress'],

    _normalize(path) {
        return String(path || '').replace(/\\/g, '/');
    },

    _legacyValue(item) {
        if (!item || !item.path) return 0;
        const target = this._normalize(item.path);
        for (const key of this.LEGACY_KEYS) {
            try {
                const data = JSON.parse(localStorage.getItem(key) || '{}');
                for (const [legacyPath, seconds] of Object.entries(data)) {
                    const legacy = this._normalize(legacyPath);
                    if (legacy && (target.endsWith(legacy) || legacy.endsWith(target))) {
                        const value = Number(seconds) || 0;
                        delete data[legacyPath];
                        localStorage.setItem(key, JSON.stringify(data));
                        return value;
                    }
                }
            } catch (e) { }
        }
        return 0;
    },

    get(item) {
        const itemId = typeof item === 'string' ? item : (item && item.id);
        try {
            const data = JSON.parse(localStorage.getItem(this.STORAGE_KEY) || '{}');
            const current = Number(data[itemId]) || 0;
            if (current > 0) return current > 2 ? current : 0;
            const legacy = typeof item === 'string' ? 0 : this._legacyValue(item);
            if (legacy > 2) {
                this.save(itemId, legacy);
                return legacy;
            }
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
