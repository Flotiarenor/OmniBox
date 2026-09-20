// ============================================================
// OmniBox 图片相册 — 格式化与转义工具
//
// 本文件是 ImageViewer.prototype 的一个分片：类骨架与构造函数在 app.js，
// 这里用 Object.assign 把方法扩回同一个原型。**加载顺序是硬约束** —— 必须在
// app.js 之后、实例化之前（见 index.html），契约由 tests/js/image_viewer_app_split.mjs 把关。
// ============================================================

Object.assign(ImageViewer.prototype, {

    // ===== 工具 =====
    _formatDuration(seconds) {
        if (!isFinite(seconds) || seconds <= 0) return '--';
        seconds = Math.round(seconds);
        const h = Math.floor(seconds / 3600);
        const m = Math.floor((seconds % 3600) / 60);
        const s = seconds % 60;
        if (h > 0) return `${h} 小时 ${m} 分`;
        if (m > 0) return `${m} 分 ${s} 秒`;
        return `${s} 秒`;
    },

    _monthKey(mtime) {
        const d = new Date((mtime || 0) * 1000);
        return `${d.getFullYear()} 年 ${d.getMonth() + 1} 月`;
    },

    _timeAgo(mtime) {
        if (!mtime) return '';
        const diff = Date.now() / 1000 - mtime;
        if (diff < 3600) return `${Math.max(1, Math.floor(diff / 60))} 分钟前`;
        if (diff < 86400) return `${Math.floor(diff / 3600)} 小时前`;
        if (diff < 2592000) return `${Math.floor(diff / 86400)} 天前`;
        const d = new Date(mtime * 1000);
        return `${d.getFullYear()}-${String(d.getMonth() + 1).padStart(2, '0')}-${String(d.getDate()).padStart(2, '0')}`;
    },

    _emptyHtml(icon, text, hint) {
        // 结构走壳的 .empty-state（base.css），插件不再自带一套 .iv-empty 样式。
        // 图标名 → 标记交给壳的 Icons（见 icons.generated.js）
        const iconHtml = window.Icons ? window.Icons.html(icon, 'empty-state-icon') : icon;
        return `<div class="empty-state">
            <div class="empty-state-icon">${iconHtml}</div>
            <div class="empty-state-text">${this._escapeHtml(text)}</div>
            ${hint ? `<div class="empty-state-hint">${this._escapeHtml(hint)}</div>` : ''}
        </div>`;
    },

    // 统一走内核 window.Utils.escapeHtml（转义 &<>"' ，属性场景同样安全）；
    // 以前用 textContent → innerHTML，不转义引号，属性场景要靠 _escapeAttr 补救。
    _escapeHtml(str) {
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

    _escapeAttr(str) {
        return this._escapeHtml(str);
    },
});
