// ===== 工具函数 =====
const MusicUtils = {
    formatTime(seconds) {
        if (isNaN(seconds) || !isFinite(seconds)) return '00:00';
        const m = Math.floor(seconds / 60);
        const s = Math.floor(seconds % 60);
        return `${m.toString().padStart(2, '0')}:${s.toString().padStart(2, '0')}`;
    },

    debounce(func, wait) {
        let timeout;
        return function (...args) {
            clearTimeout(timeout);
            timeout = setTimeout(() => func.apply(this, args), wait);
        };
    },

    escapeHtml(str) {
        const div = document.createElement('div');
        div.textContent = str;
        return div.innerHTML;
    },

    isAudioExt(filename) {
        const exts = ['mp3', 'flac', 'wav', 'aac', 'ogg', 'wma', 'm4a', 'opus', 'ape', 'wv'];
        const ext = filename.split('.').pop().toLowerCase();
        return exts.includes(ext);
    },
};
