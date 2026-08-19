// ===== 下载中心工具函数 =====
const DownloadUtils = {
    getStatusText(status) {
        const map = {
            'downloading': '下载中',
            'paused': '已暂停',
            'completed': '已完成',
            'failed': '失败',
            'queued': '排队中'
        };
        return map[status] || status;
    },

    formatSpeed(bytesPerSecond) {
        if (!bytesPerSecond || bytesPerSecond === 0) return '0 B/s';
        const units = ['B/s', 'KB/s', 'MB/s', 'GB/s'];
        let i = 0;
        let speed = bytesPerSecond;
        while (speed >= 1024 && i < units.length - 1) {
            speed /= 1024;
            i++;
        }
        return `${speed.toFixed(1)} ${units[i]}`;
    },

    formatTime(seconds) {
        if (!seconds || seconds <= 0) return '--';
        if (seconds < 60) return `${Math.round(seconds)}秒`;
        if (seconds < 3600) return `${Math.floor(seconds / 60)}分${Math.round(seconds % 60)}秒`;
        const h = Math.floor(seconds / 3600);
        const m = Math.floor((seconds % 3600) / 60);
        return `${h}时${m}分`;
    },
};
