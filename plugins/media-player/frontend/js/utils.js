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

    // 统一走内核 window.Utils.escapeHtml（会转义引号，属性场景同样安全）；
    // 内核脚本尚未就绪时退化为等价实现。
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

    // 返回图标名（`icon:` 前缀），由调用方交给 Icons.html() 渲染
    itemIcon(item) {
        return item && item.kind === 'video' ? 'icon:clapperboard' : 'icon:music';
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
    // url 与内联处理器参数都必须转义：封面 URL 可能来自远程数据（网易云歌单），
    // 未转义时一个引号就能逃出属性注入标记。
    coverImg(url, fallbackIcon = 'icon:music', extra = '', itemId = '') {
        if (!url) {
            return `<div class="cover-fallback">${MPUtils.icon(fallbackIcon)}</div>`;
        }
        const onError = itemId
            ? `onerror="MPCoverFail(this,${MPUtils.jsString(itemId)},${MPUtils.jsString(fallbackIcon)})"`
            : `onerror="MPUtils.fallbackCover(this,${MPUtils.jsString(fallbackIcon)})"`;
        return `<img src="${MPUtils.escapeHtml(url)}" loading="lazy" alt="" ${MPUtils.safeExtraAttrs(extra)} ${onError}>`;
    },

    // 追加属性的白名单：只接受 `name="value"` 且字符集受限的形态（当前唯一用途是
    // `data-mp-thumb-id="<id>"`）。以前这里原样拼接 extra，而 extra 由调用方用后端
    // 数据（item.id / cover.id）拼出来 —— 一个引号就能插入新属性或新标记。
    // 认不出的片段直接丢弃：这段 HTML 的拼接权不该交给调用方。
    safeExtraAttrs(extra) {
        const text = String(extra == null ? '' : extra);
        const pattern = /([a-zA-Z][a-zA-Z0-9-]*)="([A-Za-z0-9_.:-]*)"/g;
        const attrs = [];
        let match;
        while ((match = pattern.exec(text)) !== null) {
            attrs.push(`${match[1]}="${match[2]}"`);
        }
        return attrs.join(' ');
    },

    // 详情页 hero 背景：值会落进 `style="--hero-bg:url("…")"` —— 属性上下文里的
    // **CSS** 上下文。cover_url 来自远程 API（网易云歌单封面），原实现直接拼
    // `url("${coverSrc}")`：一个 `"` 就能闭合属性插入标记，一个 `)` 就能改写
    // 后续声明。这里只放行预期的来源（http(s) / 站内相对路径 / data:image），
    // 并把能闭合 url() 或属性的字符百分号编码，最后再做 HTML 属性转义。
    heroBg(url) {
        const raw = String(url == null ? '' : url).trim();
        if (!raw || !/^(?:https?:\/\/|\/|data:image\/)/i.test(raw)) return 'none';
        // 注意：不能只靠 encodeURIComponent —— 它不编码 ( ) ' ! *，而 `)` 足以
        // 提前闭合 url()。这里对每个危险字符直接给出百分号编码（CSS 分词先于
        // URL 解码，所以转义后的引号只是数据，不会成为语法）。
        const encoded = raw.replace(/["'\\()<>\s]/g, (ch) => (
            `%${ch.charCodeAt(0).toString(16).toUpperCase().padStart(2, '0')}`
        ));
        return MPUtils.escapeHtml(`url("${encoded}")`);
    },

    // 内联事件处理器里的字符串参数：先 JS 字符串转义，再 HTML 属性转义。
    jsString(value) {
        if (window.Utils && typeof window.Utils.jsString === 'function') {
            return window.Utils.jsString(value);
        }
        const safe = String(value == null ? '' : value)
            .replace(/\\/g, '\\\\')
            .replace(/'/g, "\\'")
            .replace(/\r?\n/g, '\\n');
        return MPUtils.escapeHtml(safe);
    },

    // 图标名 → 标记。壳的图标集在引导脚本里内联，缺失时返回空串
    // （不写 emoji 兜底：那会让 emoji 字面量留在源码里，emoji 门禁永远清不掉）
    icon(name) {
        return (window.Icons && typeof window.Icons.html === 'function')
            ? window.Icons.html(name)
            : '';
    },

    // 封面降级：移除 img 并插入真实的占位元素。
    // 以前写 `parent.dataset.fallback` 交给 CSS 的 ::after content 渲染 emoji ——
    // 伪元素只能放文本，放不了 SVG 图标，所以改成插入元素（样式见 media-player.css）。
    fallbackCover(img, fallbackIcon = 'icon:music') {
        const parent = img.parentElement;
        img.remove();
        if (parent) {
            parent.classList.add('img-broken');
            const holder = document.createElement('div');
            holder.className = 'cover-fallback';
            holder.innerHTML = MPUtils.icon(fallbackIcon);
            parent.appendChild(holder);
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

    // ===== 网易云曲目 ↔ 本地媒体库匹配 =====
    // 「重建本地歌单」与「导入我的喜欢」共用：把在线曲目映射成本地条目 id。
    // 归一化：大小写 / 全半角 / 空白 / 常见标点与括号一律抹平，让「歌名 (Live)」
    // 这类后缀不至于把同一条曲目判成两首。
    normNcmText(text) {
        return String(text == null ? '' : text)
            .toLowerCase()
            .normalize('NFKC')
            .replace(/[\s\u3000]+/g, '')
            .replace(/[.,!?'"`~@#$%^&*()\[\]{}<>:;+\-_=|\\/、。，！？：；（）【】《》“”‘’·・]/g, '');
    },

    // 歌手串 → token 集合：本地标签常见 "A/B"、"A、B"，在线是多元素数组
    ncmArtistTokens(value) {
        const parts = (Array.isArray(value) ? value : [value])
            .flatMap(raw => String(raw == null ? '' : raw).split(/[\/、,，;；&]|\sfeat\.?\s|\sft\.?\s/i));
        const tokens = new Set();
        for (const part of parts) {
            const token = MPUtils.normNcmText(part);
            if (token) tokens.add(token);
        }
        return tokens;
    },

    /**
     * 按在线曲目顺序匹配本地条目，返回 `{ids, matched, missed, missedSongs}`。
     *
     * 判定保守优先，宁缺勿错配（错配会把别人的歌塞进本地歌单）：
     *   1. 归一化歌名必须一致；
     *   2. 歌手有交集（最可信）→ 命中；否则时长差 ≤3s → 命中；
     *   3. 两条都不满足时，只有当本地库里这个歌名唯一才接受（本地标签常缺歌手）。
     * 同一个本地条目只被认领一次，同名重复曲目不会互相抢。
     * `missedSongs` 是没匹配上的在线曲目本体（补档清单要用歌名 + 歌手）。
     */
    matchNeteaseToLocal(songs, localItems) {
        const byTitle = new Map();
        for (const item of localItems || []) {
            const key = MPUtils.normNcmText(item && item.title);
            if (!key) continue;
            if (!byTitle.has(key)) byTitle.set(key, []);
            byTitle.get(key).push(item);
        }
        const ids = [];
        const missedSongs = [];
        const used = new Set();
        for (const song of songs || []) {
            const candidates = (byTitle.get(MPUtils.normNcmText(song && song.name)) || [])
                .filter(c => c && !used.has(c.id));
            if (!candidates.length) {
                missedSongs.push(song);
                continue;
            }
            const wanted = MPUtils.ncmArtistTokens(song.artists);
            const duration = Number(song.duration || 0) / 1000;
            let best = null;
            let bestScore = 0;
            let bestDelta = Infinity;
            for (const candidate of candidates) {
                const delta = (candidate.duration && duration)
                    ? Math.abs(candidate.duration - duration) : Infinity;
                const overlap = [...MPUtils.ncmArtistTokens(candidate.artist)]
                    .some(a => wanted.has(a));
                const score = overlap ? 3 : (delta <= 3 ? 2 : (candidates.length === 1 ? 1 : 0));
                if (score > bestScore || (score === bestScore && delta < bestDelta)) {
                    best = candidate;
                    bestScore = score;
                    bestDelta = delta;
                }
            }
            if (!best) {
                missedSongs.push(song);
                continue;
            }
            used.add(best.id);
            ids.push(best.id);
        }
        return { ids, matched: ids.length, missed: missedSongs.length, missedSongs };
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
