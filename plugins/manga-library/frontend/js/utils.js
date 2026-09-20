// ===== 漫画中心工具函数 =====
const MangaUtils = {
    // 统一走内核 window.Utils.escapeHtml（会转义引号，data-folder="${...}" 这类
    // 属性场景同样安全）；内核脚本尚未就绪时退化为等价实现。
    escapeHtml(str) {
        if (window.Utils && typeof window.Utils.escapeHtml === 'function') {
            return window.Utils.escapeHtml(str);
        }
        if (str == null) return '';
        return String(str)
            .replace(/&/g, '&amp;')
            .replace(/</g, '&lt;')
            .replace(/>/g, '&gt;')
            .replace(/"/g, '&quot;')
            .replace(/'/g, '&#39;');
    },

    coverImg(url, fallback = 'icon:library') {
        const icon = MangaUtils.escapeHtml(fallback);
        if (!url) return `<div class="ml-cover-fallback">${icon}</div>`;
        const iconJs = (window.Utils && window.Utils.jsString)
            ? window.Utils.jsString(fallback)
            : icon;
        return `<img src="${MangaUtils.escapeHtml(url)}" loading="lazy" alt=""
            onerror="this.parentElement.classList.add('ml-cover-fallback-parent');
                     this.outerHTML='<div class=\\'ml-cover-fallback\\'>${iconJs}</div>';">`;
    },
};

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
