// ============================================================
// 格子铺图片相册 — 格式化与转义工具
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
        // 图标名 → 标记交给壳的 Icons（见 icons.generated.js）；Icons 缺失时（本页脱离壳
        // 单独打开）返回空串，不写字形兜底 —— 兜底会把待迁移的 emoji 字面量留在源码里。
        // 影响范围：本插件 4 个空态调用点（app-albums.js:93、app-grid.js:50/62/200），
        // 均已传 `icon:名字` 形式。
        const iconHtml = window.Icons ? window.Icons.html(icon, 'empty-state-icon') : '';
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

    /**
     * 缩略图 URL（带版本号）。
     *
     * 壳的 `/thumbs` 响应带 `Cache-Control: private, max-age=86400`，URL 不变浏览
     * 器就一天不回源 —— 于是"更新了缩略图/重建了缓存，界面却还是旧的"。版本号取
     * 源文件的 mtime：图片被替换 → mtime 变 → URL 变 → 立刻重新请求。
     *
     * 相册与子相册卡片传 `cover_mtime`（封面文件自身的 mtime）：它们的 `mtime` 是
     * 目录或子树里最新文件的 mtime，替换同名封面时不会变，拿它当版本号等于不换 URL。
     * 普通图片没有 `cover_mtime`，仍用 `mtime`。
     *
     * 版本号用**毫秒**（`Math.round(秒 * 1000)`）：下载器或 `os.utime` 替换同名文件时
     * 两次 mtime 只差几百毫秒，秒级取整会算出同一个版本号，一天强缓存于是不换 URL。
     * 没有时间戳（`cover_mtime` 与 `mtime` 都是 0 / 缺失）时才省略版本号。
     *
     * 强制重建（「更新缩略图」/「重建」）不改源文件 mtime，只靠上面那条 URL 不会变。
     * 这类重建由后端给一个单调版本号（毫秒，见 `ThumbStore.bump_rebuild_version`），
     * 页面记在 `_thumbVersions`（逐条）或 `_thumbEpoch`（整批）里，在 mtime 版本之后
     * 再拼一段 —— 既是新 URL，又保留"之后 mtime 真变了继续换 URL"的语义。
     * 组件缺失时（本页脱离壳单独打开）退化成不带版本号的 URL，不影响显示。
     */
    _thumbUrl(item) {
        const path = (item && (item.url || item.cover)) || '';
        if (!path) return '';
        const coverStamp = Number(item && item.cover_mtime) || 0;
        // `cover_mtime` 缺失 / 为 0 时退回条目自身的 mtime（普通图片只有它）
        const stamp = coverStamp || Number(item && item.mtime) || 0;
        const stampVersion = stamp ? Math.round(stamp * 1000) : 0;
        const forced = this._forcedThumbVersion(path);
        const version = forced
            ? `${stampVersion}-${forced}`
            : (stampVersion ? String(stampVersion) : '');
        if (window.Freshness && typeof window.Freshness.assetUrl === 'function') {
            return window.Freshness.assetUrl(path, version);
        }
        const base = Bridge.thumbUrl(path);
        return version ? `${base}&v=${version}` : base;
    },

    /**
     * 某条路径当前的强制重建版本号（没有则 0）。
     *
     * 逐条优先（定向重建知道动了哪几张），其次整批 epoch（全量/文件夹重建不知道具体
     * 文件列表）。两者都由后端给的单调毫秒时间戳，同一毫秒连续重建也不会重复。
     */
    _forcedThumbVersion(path) {
        const overrides = this._thumbVersions;
        if (overrides && typeof overrides.get === 'function') {
            const value = overrides.get(String(path));
            if (value) return value;
        }
        return Number(this._thumbEpoch) || 0;
    },

    /**
     * 记录一次强制重建的版本号（`paths` 为空表示整批：写进 `_thumbEpoch`）。
     *
     * 后端在 `regenerate_thumbs` / `rebuild_status` 里给出 `version`；重新渲染时
     * `_thumbUrl` 优先用它，浏览器才会重新请求被重建的缩略图。
     */
    _rememberThumbVersions(paths, version) {
        const stamp = Number(version) || 0;
        if (!stamp) return;
        const list = Array.isArray(paths) ? paths.filter(Boolean) : [];
        if (!list.length) {
            this._thumbEpoch = stamp;
            return;
        }
        const overrides = this._thumbVersions || (this._thumbVersions = new Map());
        list.forEach((path) => overrides.set(String(path), stamp));
    },
});
