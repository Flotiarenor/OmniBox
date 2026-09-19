// ===== 小说阅读器工具函数 =====
const NovelUtils = {
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
};
