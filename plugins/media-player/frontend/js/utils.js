// ===== 媒体播放器工具函数 =====
const MPUtils = {
    formatTime(seconds) {
        if (isNaN(seconds) || !isFinite(seconds) || seconds < 0) return '00:00';
        const total = Math.floor(seconds);
        const h = Math.floor(total / 3600);
        const m = Math.floor((total % 3600) / 60);
        const s = total % 60;
        if (h > 0) {
            return `${String(h).padStart(2, '0')}:${String(m).padStart(2, '0')}:${String(s).padStart(2, '0')}`;
        }
        return `${String(m).padStart(2, '0')}:${String(s).padStart(2, '0')}`;
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
        div.textContent = str == null ? '' : String(str);
        return div.innerHTML;
    },

    itemIcon(item) {
        return item && item.kind === 'video' ? '🎬' : '🎵';
    },

    // 媒体文件 / 封面都以绝对路径存储（支持跨多个媒体根目录），
    // 交给 Bridge.originalUrl 统一编码后走 /file?path= 路由，由后端做逐根目录安全检查。
    mediaUrl(path) {
        if (!path) return '';
        return Bridge.originalUrl(path);
    },

    coverUrl(item) {
        if (!item || !item.cover_path) return '';
        return MPUtils.mediaUrl(item.cover_path);
    },

    // 生成封面 HTML；图片缺失 / 加载失败时自动降级为 emoji 占位
    coverImg(url, fallbackIcon = '🎵', extra = '') {
        if (!url) {
            return `<div class="cover-fallback">${fallbackIcon}</div>`;
        }
        return `<img src="${url}" loading="lazy" alt="" ${extra}
            onerror="this.parentElement.dataset.fallback='${fallbackIcon}';
                     this.parentElement.classList.add('img-broken');
                     this.remove();">`;
    },

    timeAgo(text) {
        if (!text) return '';
        try {
            const time = new Date(text.replace(/-/g, '/'));
            const diff = Date.now() - time.getTime();
            if (isNaN(diff)) return text;
            const minutes = Math.floor(diff / 60000);
            if (minutes < 1) return '刚刚';
            if (minutes < 60) return `${minutes} 分钟前`;
            const hours = Math.floor(minutes / 60);
            if (hours < 24) return `${hours} 小时前`;
            const days = Math.floor(hours / 24);
            if (days < 30) return `${days} 天前`;
            return text.slice(0, 10);
        } catch (e) {
            return text;
        }
    },

    fmtDuration(seconds) {
        const s = Math.round(seconds || 0);
        if (s < 60) return `${s} 秒`;
        const m = Math.round(s / 60);
        if (m < 60) return `${m} 分钟`;
        return `${Math.floor(m / 60)} 小时 ${m % 60} 分`;
    },

    setRangePercent(input, percent) {
        if (!input) return;
        const pct = Math.max(0, Math.min(100, percent));
        input.style.setProperty('--range-val', `${pct}%`);
    },

    openModal(id) {
        const modal = document.getElementById(id);
        if (modal) {
            modal.classList.add('active');
            const input = modal.querySelector('input');
            if (input) setTimeout(() => input.focus(), 40);
        }
    },

    closeModal(id) {
        const modal = document.getElementById(id);
        if (modal) modal.classList.remove('active');
    },
};
