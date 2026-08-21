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
};
