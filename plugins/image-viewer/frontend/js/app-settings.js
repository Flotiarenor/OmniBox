// ============================================================
// OmniBox 图片相册 — 设置读写
//
// 本文件是 ImageViewer.prototype 的一个分片：类骨架与构造函数在 app.js，
// 这里用 Object.assign 把方法扩回同一个原型。**加载顺序是硬约束** —— 必须在
// app.js 之后、实例化之前（见 index.html），契约由 tests/js/image_viewer_app_split.mjs 把关。
// ============================================================

Object.assign(ImageViewer.prototype, {


    // ============================================================
    // 设置
    // ============================================================

    /**
     * 把「灯箱内存缓存」张数交给壳的灯箱组件（`base.js` 的 `setDecodedLimit`）。
     *
     * 为什么由插件下发：保位图的那一小池离屏 `<img>` 属于灯箱组件，而"给多少张"
     * 是用户偏好 —— 组件不该自己拍一个内存预算。组件缺失（页面脱离壳单独打开）或
     * 旧版壳没有这个方法时静默跳过，不影响其他功能。
     */
    _applyLightboxCache() {
        if (!this.lightbox || typeof this.lightbox.setDecodedLimit !== 'function') return;
        const value = (this.currentSettings || {}).lightbox_cache;
        this.lightbox.setDecodedLimit(value != null ? value : 8);
    },

    async openSettingsModal() {
        document.getElementById('settings-modal').classList.add('active');
        // 图片文件夹列表由共享组件建立（这里补建是因为设置弹窗可能晚于 init 才打开）
        if (!this.rootsPicker) this._resetRootsList();
        document.getElementById('setting-current-folder-name').textContent = this.currentPath || '根目录';
        const applyToFolder = document.getElementById('setting-apply-to-folder');
        if (applyToFolder) {
            // 每次打开默认保存到全局；根目录本身没有「文件夹级」概念，
            // 所以禁用该选项避免误导。图片文件夹列表始终是全局设置，与它无关。
            applyToFolder.checked = false;
            applyToFolder.disabled = !this.currentPath;
        }
        // 读取失败时保持隐藏，避免表单里出现一组"不知道作用于哪"的选项
        this._albumSortVisible = false;
        this._pixivFuzzyVisible = false;
        document.getElementById('setting-album-sort-section').classList.add('hidden');
        document.getElementById('setting-pixiv-fuzzy-section').classList.add('hidden');
        try {
            const s = await Bridge.call('get_settings', this.currentPath);
            document.getElementById('setting-row-height').value = s.row_height;
            document.getElementById('setting-row-height-val').textContent = s.row_height;
            document.getElementById('setting-per-page').value = s.per_page;
            document.getElementById('setting-sort-by').value = s.sort_by;
            document.getElementById('setting-sort-order').value = s.sort_order;
            // 模糊匹配：只影响「Pixiv 排序支持」，所以只有当前文件夹生效 Pixiv
            // 排序时才出现；值取自当前文件夹的生效设置（含继承），保存时与
            // 排序方式同作用域（仅当前文件夹 / 全局）。
            this._pixivFuzzyVisible = s.sort_by === 'time_name';
            document.getElementById('setting-pixiv-fuzzy-section')
                .classList.toggle('hidden', !this._pixivFuzzyVisible);
            document.getElementById('setting-pixiv-fuzzy').checked = !!s.pixiv_fuzzy;
            // 作者视图二次排序：当前文件夹生效 Pixiv 排序时才出现；值是全局偏好，
            // 所以从全局设置（而非当前文件夹）读。改成 Pixiv 排序并保存后页面会刷新，
            // 下次打开设置就能看到这组选项。
            this._albumSortVisible = s.sort_by === 'time_name';
            document.getElementById('setting-album-sort-section')
                .classList.toggle('hidden', !this._albumSortVisible);
            const global = await Bridge.call('get_settings', '');
            document.getElementById('setting-album-sort-by').value = global.album_sort_by || 'mtime';
            document.getElementById('setting-album-sort-order').value = global.album_sort_order || 'desc';
            // 灯箱缓存张数是全局偏好（内存占用跟着进程走，不跟目录），值也从全局读：
            // 否则在子目录里打开设置会显示继承值、保存时又写错作用域。
            const cacheCount = global.lightbox_cache != null ? global.lightbox_cache : 8;
            document.getElementById('setting-lightbox-cache').value = cacheCount;
            document.getElementById('setting-lightbox-cache-val').textContent = cacheCount;
            // 图片文件夹列表（列表即唯一入口：主目录 + 额外目录，保存时写回
            // root_dir / extra_roots），不再单列「数据根目录」输入框。
            // 列表控件由共享组件持有，这里只把后端读到的路径灌进去。
            const roots = await Bridge.call('list_roots');
            if (!this.rootsPicker) this._resetRootsList();
            if (this.rootsPicker) this.rootsPicker.setPaths((roots || []).map(r => r.path));
        } catch (e) { }
    },

    closeSettingsModal() {
        document.getElementById('settings-modal').classList.remove('active');
    },

    async saveSettings() {
        const isFolderOnly = document.getElementById('setting-apply-to-folder').checked;
        const settings = {
            row_height: parseInt(document.getElementById('setting-row-height').value, 10),
            per_page: parseInt(document.getElementById('setting-per-page').value, 10),
            sort_by: document.getElementById('setting-sort-by').value,
            sort_order: document.getElementById('setting-sort-order').value
        };
        // 作者视图二次排序是全局偏好，且只在当前文件夹生效 Pixiv 排序时才在表单里
        // 出现：选项被隐藏时不动已有设置（勾选「仅当前文件夹」时同样不写进文件夹级设置）
        const albumSort = this._albumSortVisible ? {
            album_sort_by: document.getElementById('setting-album-sort-by').value,
            album_sort_order: document.getElementById('setting-album-sort-order').value
        } : null;
        // 灯箱缓存张数是全局偏好（内存占用跟着进程走，不跟目录），与二次排序同样处理：
        // 始终写全局，勾「仅当前文件夹」时也不落进文件夹级设置。
        const lightboxCache = {
            lightbox_cache: parseInt(document.getElementById('setting-lightbox-cache').value, 10)
        };
        // 模糊匹配与排序方式同作用域；未选 Pixiv 排序时它是死设置，不写入，
        // 也让文件夹级设置回退到全局值（否则在别的文件夹取消勾选后会留下残留值）
        const fuzzy = (this._pixivFuzzyVisible && settings.sort_by === 'time_name')
            ? { pixiv_fuzzy: document.getElementById('setting-pixiv-fuzzy').checked }
            : null;
        // 图片文件夹列表是全局设置（勾「仅应用于当前文件夹」时不写）：
        // 第一行写回 root_dir，其余写回 extra_roots，列表就是唯一入口。
        // 列表被清空时**显式清掉 root_dir**：后端会回退到默认数据目录，
        // 否则旧路径会悄悄继续生效，和界面显示的「未添加任何目录」不一致。
        const roots = this._rootPaths();
        if (!isFolderOnly) {
            settings.root_dir = roots.length ? roots[0] : '';
            settings.extra_roots = roots.slice(1).join('\n');
        }
        try {
            if (isFolderOnly) {
                await Bridge.call('save_settings', this.currentPath,
                    { ...settings, ...(fuzzy || {}) });
                if (albumSort) await Bridge.call('save_settings', '', albumSort);
                await Bridge.call('save_settings', '', lightboxCache);
            } else {
                await Bridge.call('save_settings', '',
                    { ...settings, ...(albumSort || {}), ...(fuzzy || {}), ...lightboxCache });
                if (this.currentPath) await Bridge.call('clear_folder_settings', this.currentPath);
            }
            this.currentSettings = {
                ...(this.currentSettings || {}), ...settings,
                ...(albumSort || {}), ...(fuzzy || {}), ...lightboxCache
            };
            this.currentRowHeight = settings.row_height;
            this._applyLightboxCache();
            this._applyAlbumSortSettings();
            this.closeSettingsModal();
            Toast.success('设置已保存');
            if (!isFolderOnly) {
                // 根目录/额外目录可能变了：作废后端相册索引缓存后再重新拉取
                await Bridge.call('refresh');
                await this.loadAlbums();
            }
            if (this.mode === 'images') this.loadImages(this.currentPath, 1);
            else this.showAlbums();
        } catch (e) {
            Toast.error('保存设置失败');
        }
    },
});
