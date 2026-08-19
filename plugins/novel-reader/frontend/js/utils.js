// ===== 小说阅读器工具函数 =====
const NovelUtils = {
    formatContent(text) {
        if (!text) return '';
        const paragraphs = text
            .split('\n')
            .filter(line => line.trim())
            .map(line => `<p>${Utils.escapeHtml(line.trim())}</p>`);
        return paragraphs.join('');
    },
};
