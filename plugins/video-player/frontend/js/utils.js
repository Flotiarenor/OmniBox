// ===== 视频播放器工具函数 =====
const VideoUtils = {
    VIDEO_EXTS: ['mp4', 'mkv', 'webm', 'avi', 'mov', 'flv', 'wmv'],

    isVideoName(filename) {
        const ext = (filename || '').split('.').pop().toLowerCase();
        return this.VIDEO_EXTS.includes(ext);
    },

    formatTime(seconds) {
        if (isNaN(seconds)) return '00:00';
        const m = Math.floor(seconds / 60);
        const s = Math.floor(seconds % 60);
        return `${m.toString().padStart(2, '0')}:${s.toString().padStart(2, '0')}`;
    },
};
