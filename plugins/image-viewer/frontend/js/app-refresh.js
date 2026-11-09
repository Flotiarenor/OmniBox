// ============================================================
// OmniBox 图片相册 — 刷新与缩略图重建
//
// 本文件是 ImageViewer.prototype 的一个分片：类骨架与构造函数在 app.js，
// 这里用 Object.assign 把方法扩回同一个原型。**加载顺序是硬约束** —— 必须在
// app.js 之后、实例化之前（见 index.html），契约由 tests/js/image_viewer_app_split.mjs 把关。
// ============================================================

Object.assign(ImageViewer.prototype, {


    // ============================================================
    // 刷新 / 更新缩略图
    // ============================================================
    /**
     * 兼容入口：重新扫描并重载当前视图。
     *
     * 走的是后端的 `refresh` API，它现在转发到统一刷新的「同步」。保留这个方法是
     * 为了"保存设置后刷新"这类既有调用点与调试脚本；界面上的「校验」入口由壳的
     * PluginChrome 挂载（manifest 的 `frontend.chrome.freshness`）。
     */
    async refreshView() {
        try {
            const report = await Bridge.call('refresh');
            if (report && report.action === 'skip' && report.reason === 'busy') {
                Toast.info('后台正在校验，完成后会自动更新');
                return;
            }
        } catch (e) {
            // 后端 refresh API 不可用（旧版未重启）时降级：只重载视图
        }
        await this._onFreshnessChanged();
    },

    // ============================================================
    // 统一刷新基建（同步 / 校验）
    // ============================================================
    // 「同步状态 + 校验」控件（shell/frontend/public/shell/freshness.js）由壳的
    // PluginChrome 挂载（manifest 的 frontend.chrome.freshness），内容页不再持有点击
    // 入口；校验完成或同步发现变化后，壳发 `omnibox:action: refresh`，本页重载相册树与
    // 当前目录的图片。

    /**
     * 同步一次当前作用域：重新可见时调用（壳侧另有首帧与导航切换的 `autoSync`）。
     *
     * 用 `Freshness.sync(plugin, scope)` 而不是控件句柄：控件由壳挂载，句柄在壳手里，
     * 内容页拿不到。`reload` 为真且报告里确实有变化时才重载 —— 增量同步的报告为空
     * 表示"什么都没变"，此时重载只是白白重取一遍并打断当前浏览。
     *
     * "有变化"包含目录集合信号（`new_dirs` / `vanished_dirs`，引擎给数组、插件的
     * `dirs_changed` 钩子给计数）：新建一个空目录时条目层没有任何增删，只靠
     * added/changed/removed 判断会把这次变化整个漏掉，相册层就看不到新目录。
     *
     * `skip`（引擎忙 / 节流 / 非法作用域）不重载：没有任何变化被确认，交给下一次
     * 重新可见重试。
     */
    async _syncFreshness(reload) {
        const api = (typeof window !== 'undefined') ? window.Freshness : null;
        // 旧壳 / 旧版共享运行时没有 `sync`（那时只有挂载控件）：静默跳过
        if (!api || typeof api.sync !== 'function') return null;
        let report = null;
        try {
            report = await api.sync('image-viewer', this._freshnessScope());
        } catch (e) {
            // 被动路径失败不打扰用户：下一次重新可见会重试
            return null;
        }
        if (!report || report.action === 'skip') return report;
        const dirSignal = (key) => {
            let total = 0;
            [report, report.dirs_changed].forEach((group) => {
                if (!group || typeof group !== 'object') return;
                const value = group[key];
                total += Array.isArray(value) ? value.length : (Number(value) || 0);
            });
            return total;
        };
        const changed = !!report.invalidated
            || dirSignal('new_dirs') > 0
            || dirSignal('vanished_dirs') > 0
            || [report.added, report.changed, report.removed]
                .some((value) => (Number(value) || 0) > 0);
        if (changed && reload) await this._onFreshnessChanged();
        return report;
    },

    /** 同步/校验发现了变化：重载相册树，图片视图则在当前层重取（目录或最近图片）。 */
    async _onFreshnessChanged() {
        await this.loadAlbums();
        await this._reloadCurrentView();
    },

    // ============================================================
    // 缩略图预生成 / 定向重建（保留给"一次生成整库"的场景，不再挂在工具栏）
    // ============================================================
    async rebuildAll() {
        const ok = await confirmDialog('将清空旧缓存，并一次性生成全部缩略图。\n图片较多时可能需要较长时间，可以继续操作界面。确定继续吗？', { danger: true });
        if (!ok) return;
        await this._startRebuildTask(() => Bridge.call('rebuild_all', '', true), '全量重建');
    },

    async rebuildFolder(path) {
        const ok = await confirmDialog(`将重建「${path || '根目录'}」下的缩略图（跳过已有有效缓存），确定继续吗？`, { danger: true });
        if (!ok) return;
        await this._startRebuildTask(() => Bridge.call('rebuild_folder', path), '相册重建');
    },

    async _startRebuildTask(startCall, label = '全量重建') {
        const card = document.getElementById('rebuild-progress');
        if (card) card.classList.remove('hidden');
        this._rebuildStartTime = Date.now();
        this._updateRebuildProgress({ processed: 0, total: 0, current: '', errors: [], running: true });
        try {
            await startCall();
            const status = await this._waitRebuildDone();

            if (status.cancelled) {
                Toast.warning(`${label}已取消`);
                return;
            }

            const result = await Bridge.call('list_albums');
            if (result && result.albums) this.albums = result.albums;
            if (result && result.config) this.albumConfig = result.config;
            this.clearSelection();
            this.filteredSeqIndexes = null;
            if (this.mode === 'images') {
                this._reloadCurrentView();
            } else {
                this.showAlbums();
            }
            Toast.success(`${label}完成`);
        } catch (e) {
            Toast.error(`${label}失败：${e.message || e}`);
        } finally {
            if (card) card.classList.add('hidden');
            this._rebuildStartTime = null;
        }
    },

    async _waitRebuildDone() {
        // 轮询后端后台任务进度，直到完成
        while (true) {
            const status = await Bridge.call('rebuild_status');
            this._updateRebuildProgress(status);
            if (status.done) {
                if (status.cancelled) {
                    return status;
                }
                if (!status.success) {
                    const errors = status.errors || [];
                    throw new Error(errors.length ? `失败 ${errors.length} 个，示例：${errors.slice(0, 3).join('；')}` : '后台重建任务异常');
                }
                return status;
            }
            await new Promise(resolve => setTimeout(resolve, 500));
        }
    },

    _updateRebuildProgress(status = {}) {
        const total = status.total || 0;
        const processed = status.processed || 0;
        const text = document.getElementById('rebuild-progress-text');
        if (text) text.textContent = `${processed} / ${total}`;

        const bar = document.getElementById('rebuild-progress-bar');
        if (bar) {
            if (total > 0) {
                bar.style.width = `${Math.min(100, Math.round((processed / total) * 100))}%`;
                bar.style.animation = 'none';
            } else {
                bar.style.width = '40%';
                bar.style.animation = '';
            }
        }

        const currentEl = document.getElementById('rebuild-progress-current');
        if (currentEl) {
            currentEl.textContent = status.current ? `正在处理：${status.current}` : '正在扫描并生成缩略图，请稍候…';
        }

        const speedEl = document.getElementById('rebuild-progress-speed');
        if (speedEl && this._rebuildStartTime) {
            const elapsed = (Date.now() - this._rebuildStartTime) / 1000;
            if (elapsed > 0 && processed > 0) {
                const speed = processed / elapsed;
                const remaining = total > processed ? (total - processed) / speed : 0;
                speedEl.textContent = `${speed.toFixed(1)} 张/秒 · 剩余约 ${this._formatDuration(remaining)}`;
            } else {
                speedEl.textContent = '';
            }
        }

        const errorsEl = document.getElementById('rebuild-errors');
        if (errorsEl) {
            const errors = status.errors || [];
            errorsEl.textContent = errors.length ? `失败 ${errors.length} 个，示例：${errors.slice(0, 3).join('；')}` : '';
        }
    },

    hideRebuildProgress() {
        const card = document.getElementById('rebuild-progress');
        if (card) card.classList.add('hidden');
    },

    async cancelRebuild() {
        try {
            await Bridge.call('rebuild_cancel');
            Toast.info('正在取消全量重建…');
        } catch (e) {
            Toast.error('取消失败');
        }
    },

    async refreshSelectedThumbs() {
        const imgs = [...this.selectedImages];
        if (!imgs.length) return;
        const ok = await confirmDialog(`重新生成 ${imgs.length} 张图片的缩略图？\n（修复下载丢失/替换后残留的坏缩略图）`);
        if (!ok) return;
        try {
            const result = await Bridge.call('regenerate_thumbs', imgs);
            if (result.errors.length) Toast.error(`部分失败: ${result.errors.join('; ')}`);
            else Toast.success(`已重新生成 ${imgs.length} 张缩略图`);
            this.clearSelection();
            await this._reloadCurrentView();
        } catch (e) {
            Toast.error('更新缩略图失败');
        }
    },
});
