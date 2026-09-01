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

    // 媒体文件以绝对路径存储（支持跨多个媒体根目录），
    // 交给 Bridge.originalUrl 统一编码后走 /file?path= 路由，由后端做逐根目录安全检查。
    mediaUrl(path) {
        if (!path) return '';
        return Bridge.originalUrl(path);
    },

    // 本地封面统一按 item id 走 /thumbs/<id>（ThumbCache 懒生成 + SQLite 缓存，
    // 见 backend cover_generator）；生成失败时后端返回 404，由 coverImg 降级为 emoji。
    coverUrl(item) {
        if (!item) return '';
        // 网络流封面（网易云等）直接使用绝对地址，不走本地代理
        if (item.cover_path && (/^(https?:)?\/\//i.test(item.cover_path) || /^data:/i.test(item.cover_path))) {
            return item.cover_path;
        }
        if (item.has_cover && item.id) {
            const base = Bridge.thumbUrl(item.id);
            // 带源文件 mtime 版本号：视频/音频文件被替换后 mtime 变化 → URL 变化，
            // 强制浏览器绕过 /thumbs 的 1 天缓存（旧封面最长展示 24h 的问题）
            return item.mtime ? `${base}&v=${Math.round(item.mtime)}` : base;
        }
        return '';
    },

    // 封面来源兼容两种形态：远程 URL 字符串（网易云歌单封面）/ 本地 item 对象
    coverSrc(cover) {
        if (!cover) return '';
        if (typeof cover === 'string') return cover;
        return MPUtils.coverUrl(cover);
    },

    // 生成封面 HTML；图片缺失 / 加载失败时自动降级为 emoji 占位。
    // 传 itemId（视频条目）时，加载失败会先尝试前端 canvas 抽帧（MediaFrameExtractor），
    // 抽帧失败才降级。
    coverImg(url, fallbackIcon = '🎵', extra = '', itemId = '') {
        if (!url) {
            return `<div class="cover-fallback">${fallbackIcon}</div>`;
        }
        const onError = itemId
            ? `onerror="MPCoverFail(this,'${itemId}','${fallbackIcon}')"`
            : `onerror="MPUtils.fallbackCover(this,'${fallbackIcon}')"`;
        return `<img src="${url}" loading="lazy" alt="" ${extra} ${onError}>`;
    },

    // 封面降级：标记 broken 并移除 img，由父容器 CSS 显示 emoji 占位
    fallbackCover(img, fallbackIcon = '🎵') {
        const parent = img.parentElement;
        img.remove();
        if (parent) {
            parent.dataset.fallback = fallbackIcon;
            parent.classList.add('img-broken');
        }
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

// ===== 非线性音量映射：滑块位置(线性 0~1) ↔ 实际音量系数(指数映射) =====
// 存储/传输一律使用线性位置（localStorage、media_save_playback 均不变）；
// 仅在「赋值元素 volume」的使用点做指数映射——人耳对低音量区更敏感，
// 指数 >1 让低音量区在滑块上更精细。映射在前端完成：音量是纯播放层
// 概念，后端只是状态存档，无需也不应参与映射。
class VolumeMapper {
    constructor(exponent = 2.5) {
        if (!(exponent > 0)) throw new Error('指数必须大于0');
        this.exponent = exponent;
    }

    linearToActual(linear) {
        linear = Math.max(0, Math.min(1, linear));
        return Math.pow(linear, this.exponent);
    }

    actualToLinear(actual) {
        actual = Math.max(0, Math.min(1, actual));
        if (actual === 0) return 0;
        return Math.pow(actual, 1 / this.exponent);
    }
}

// 封面加载失败入口：视频条目先尝试前端 canvas 抽帧，失败再降级 emoji
function MPCoverFail(img, itemId, fallbackIcon) {
    if (window.MediaFrameExtractor) {
        MediaFrameExtractor.request(itemId, img, () => MPUtils.fallbackCover(img, fallbackIcon));
    } else {
        MPUtils.fallbackCover(img, fallbackIcon);
    }
}
