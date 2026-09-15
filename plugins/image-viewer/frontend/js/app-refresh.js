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
    async refreshView() {
        let cacheCleared = false;
        try {
            await Bridge.call('refresh');
            cacheCleared = true;
        } catch (e) {
            // 后端 refresh API 不可用（旧版未重启）时降级：只重载视图，不清缓存
        }
        try {
            const result = await Bridge.call('list_albums');
            if (result && result.albums) this.albums = result.albums;
            if (result && result.config) this.albumConfig = result.config;
            if (this.mode === 'images') {
                this.loadImages(this.currentPath, this.currentPage);
            } else {
                this.showAlbums();
            }
            if (cacheCleared) {
                Toast.success('已刷新');
            } else {
                Toast.warning('已重新加载；完整刷新需重启应用（后端 refresh API 未生效）');
            }
        } catch (e) {
            Toast.error('刷新失败');
        }
    },

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
                this.loadImages(this.currentPath, 1);
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
            await this.loadImages(this.currentPath, this.currentPage);
        } catch (e) {
            Toast.error('更新缩略图失败');
        }
    },
});
