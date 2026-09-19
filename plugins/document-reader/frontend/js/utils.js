// ===== 文档阅读器工具函数 =====
const DocumentUtils = {
    splitParagraphs(text) {
        if (!text) return [];
        return text.split(/\r?\n+/).map(line => line.trim()).filter(Boolean);
    },

    formatContent(text) {
        if (!text) return '';
        const paragraphs = text
            .split('\n')
            .filter(line => line.trim())
            .map(line => `<p>${Utils.escapeHtml(line.trim())}</p>`);
        return paragraphs.join('');
    },

    // 文件体积：章节数还不知道时（没解析过的书）列表里显示它，比显示 "?" 有用
    formatSize(bytes) {
        const size = Number(bytes) || 0;
        if (size < 1024) return `${size} B`;
        if (size < 1024 * 1024) return `${(size / 1024).toFixed(1)} KB`;
        if (size < 1024 * 1024 * 1024) return `${(size / 1024 / 1024).toFixed(1)} MB`;
        return `${(size / 1024 / 1024 / 1024).toFixed(2)} GB`;
    },

    // ===== 生成式封面 =====
    //
    // 没有封面图的书（txt / md / 外部格式，以及封面声明缺失或被清理过的 EPUB）
    // 用一张 SVG 兜底：外框 + 书脊 + 四条文字条。
    //
    // 为什么不写字：封面卡只有约 150×200px，书名写进去必然被截成两三个字，还不如
    // 放在卡片下方（与 manga-library 的 `.ml-card-title` 同位置）读得清楚。
    // 为什么是 SVG 而不是位图：任意缩放都清晰，一条 data URL 零请求、零缓存文件。
    // 配色来自书名哈希：同一本书的封面始终同色（重开应用不变），不同书之间能区分。

    /** 书名 → 稳定色相（0~359）。同一字符串永远同一结果，不依赖运行时状态。 */
    _hashHue(text) {
        let hash = 0;
        for (let i = 0; i < text.length; i += 1) {
            hash = (hash * 31 + text.charCodeAt(i)) % 360000;
        }
        return hash % 360;
    },

    /** 封面卡上的一条文字条：主色与浅色（同一色相，靠亮度区分层次）。 */
    coverColors(title) {
        const hue = this._hashHue(String(title || ''));
        return {
            frame: `hsl(${hue} 26% 62%)`,
            spine: `hsl(${hue} 30% 46%)`,
            bars: `hsl(${hue} 30% 88%)`,
        };
    },

    /** 生成式封面的 data URL（直接塞进 `img.src`）。 */
    coverDataUrl(title) {
        const { frame, spine, bars } = this.coverColors(title);
        const svg = `<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 300 400">`
            + `<rect width="300" height="400" rx="10" fill="${frame}"/>`
            + `<rect x="14" y="18" width="18" height="364" rx="9" fill="${spine}"/>`
            + `<rect x="58" y="108" width="196" height="22" rx="11" fill="${bars}"/>`
            + `<rect x="58" y="150" width="150" height="22" rx="11" fill="${bars}"/>`
            + `<rect x="58" y="192" width="218" height="22" rx="11" fill="${bars}"/>`
            + `<rect x="58" y="234" width="120" height="22" rx="11" fill="${bars}"/>`
            + `</svg>`;
        return `data:image/svg+xml;charset=utf-8,${encodeURIComponent(svg)}`;
    },
};
