// ============================================================
// OmniBox 图片相册 — 图片网格、Justified 布局、幻灯与多选操作
//
// 本文件是 ImageViewer.prototype 的一个分片：类骨架与构造函数在 app.js，
// 这里用 Object.assign 把方法扩回同一个原型。**加载顺序是硬约束** —— 必须在
// app.js 之后、实例化之前（见 index.html），契约由 tests/js/image_viewer_app_split.mjs 把关。
// ============================================================

Object.assign(ImageViewer.prototype, {


    // ============================================================
    // 图片网格
    // ============================================================
    async loadImages(path = '', page = 1) {
        const grid = document.getElementById('image-grid');
        const paginationEl = document.getElementById('pagination');
        grid.innerHTML = '<div class="loading">图片加载中…</div>';
        grid.style.height = 'auto';
        paginationEl.innerHTML = '';
        try {
            // 先取该文件夹生效的设置（含父文件夹/全局回退），保证 per-folder 设置首次进入即生效
            const eff = await Bridge.call('get_settings', path);
            if (eff) {
                this.currentSettings = eff;
                this.currentRowHeight = eff.row_height || 200;
            }
            const perPage = this.currentSettings.per_page || 40;
            const sortBy = this.currentSettings.sort_by || 'mtime';
            const sortOrder = this.currentSettings.sort_order || 'desc';
            const data = await Bridge.call('list_folder_items', path, page, perPage, sortBy, sortOrder);
            this.currentPage = page;
            this.currentItems = data.items || [];
            this.currentImages = this.currentItems.filter(it => it.type !== 'album');
            this.currentAllImages = data.all_images || this.currentImages;
            this.currentAllOffset = data.all_offset || 0;
            if (data.all_truncated) {
                Toast.warning('相册较大，连续浏览序列已截断（前 5000 张）');
            }
            if (data.settings) {
                this.currentSettings = data.settings;
                this.currentRowHeight = data.settings.row_height || 200;
            }
            const imgTotal = data.image_total != null ? data.image_total : this.currentImages.length;
            document.getElementById('iv-stats').textContent =
                (data.total === imgTotal) ? `共 ${data.total} 张图片` : `共 ${data.total} 项 · ${imgTotal} 张图片`;
            grid.innerHTML = '';
            this.filteredSeqIndexes = null;
            if (!this.currentItems.length) {
                grid.innerHTML = this._emptyHtml('icon:images', '此相册暂无图片');
                grid.style.height = 'auto';
                return;
            }
            const keyword = document.getElementById('iv-search').value.trim();
            if (keyword) {
                this.filterCurrentImages(keyword);
            } else {
                this.renderJustifiedLayout(this.currentItems);
            }
            this.pagination.render(data.page, Math.ceil(data.total / perPage));
        } catch (error) {
            grid.innerHTML = this._emptyHtml('⚠️', '图片加载失败');
            grid.style.height = 'auto';
        }
    },

    renderJustifiedLayout(items) {
        const grid = document.getElementById('image-grid');
        grid.innerHTML = '';
        const containerWidth = grid.clientWidth;
        if (containerWidth === 0 || !items.length) return;
        const gap = 5;
        const { cards, totalHeight } = JustifiedLayout.compute(items, containerWidth, this.currentRowHeight, gap);
        grid.style.height = `${totalHeight}px`;

        // 每个瓦片在连续浏览序列中的起始位置（子文件夹 → 其 p0，单图 → 自身）。
        // 分页时以 this.currentAllOffset 为基准：第 2+ 页的瓦片对应完整序列的中后段，
        // 否则点击会错位打开到序列开头的图片。
        // 搜索过滤时使用 filterCurrentImages 预计算的映射，保证灯箱仍从完整序列正确位置打开。
        // 注意：image_count 为直接图片数（容器为 0，不参与序列展开）。
        const seqIndex = new Map();
        if (this.filteredSeqIndexes && this.filteredSeqIndexes.size === items.length) {
            this.filteredSeqIndexes.forEach((value, key) => seqIndex.set(key, value));
        } else {
            let acc = this.currentAllOffset || 0;
            items.forEach((it, i) => {
                seqIndex.set(i, acc);
                acc += (it.type === 'album' ? (it.image_count || 0) : 1);
            });
        }

        // 圆圈数量角标：仅在该瓦片对应子文件夹自身生效的排序为「时间+文件名」时显示
        // （自己没设置则继承父级，后端 use_time_name 已算好）

        cards.forEach((cardData, index) => {
            const item = items[index];
            const card = document.createElement('div');
            card.className = 'iv-image-card';
            card.style.cssText = `left:${cardData.x}px;top:${cardData.y}px;width:${cardData.w}px;height:${cardData.h}px;`;

            if (item.type === 'album') {
                // 子文件夹直接用 p0 图片瓦片展示（不做文件夹卡片）
                card.dataset.path = item.path;
                const name = item.name;
                const isContainer = item.image_count === 0 && item.has_children;
                if (item.cover) {
                    const img = document.createElement('img');
                    img.src = Bridge.thumbUrl(item.cover);
                    img.loading = 'lazy';
                    img.alt = name;
                    img.onerror = function () {
                        if (!this.dataset.r) {
                            this.dataset.r = '1';
                            const u = new URL(this.src, location.origin);
                            u.searchParams.set('r', Date.now());
                            this.src = u.toString();
                        } else {
                            this.outerHTML = '<div class="iv-cover-fallback">' + Icons.html('icon:image-off') + '</div>';
                        }
                    };
                    card.appendChild(img);
                } else {
                    const fb = document.createElement('div');
                    fb.className = 'iv-cover-fallback';
                    fb.innerHTML = Icons.html('icon:image-off');
                    card.appendChild(fb);
                }
                if (item.use_time_name) {
                    const badge = document.createElement('span');
                    badge.className = 'iv-count-badge';
                    badge.textContent = item.total_count != null ? item.total_count : item.image_count;
                    card.appendChild(badge);
                }
                const p = document.createElement('p');
                p.textContent = name;
                card.appendChild(p);
                if (isContainer) {
                    // 纯容器（画师文件夹等）：点击进入其瀑布流（子作品 p0 瓦片）
                    card.addEventListener('click', () => this.openAlbum(item.path));
                } else {
                    // 作品文件夹：点击从 p0 打开灯箱，向右连续翻看该作品 p1 p2 … 及后续作品
                    card.addEventListener('click', () => {
                        this.lightbox.show(this.currentAllImages, seqIndex.get(index));
                    });
                }
            } else {
                // 单图卡片
                card.dataset.url = item.url;
                const img = document.createElement('img');
                img.src = Bridge.thumbUrl(item.url);
                img.loading = 'lazy';
                img.alt = item.url.split('/').pop();
                const p = document.createElement('p');
                p.textContent = item.url.split('/').pop();
                card.append(img, p);
                card.addEventListener('click', () => {
                    if (this.isMultiSelectMode) {
                        this.toggleSelectImage(item.url, card);
                    } else {
                        this.lightbox.show(this.currentAllImages, seqIndex.get(index));
                    }
                });
            }
            if (item.type !== 'album' && this.selectedImages.has(item.url)) {
                card.classList.add('selected');
            }
            grid.appendChild(card);
        });
    },

    filterCurrentImages(keyword = '') {
        const grid = document.getElementById('image-grid');
        const normalized = keyword.trim().toLowerCase();
        if (!normalized) {
            this.filteredSeqIndexes = null;
            if (this.currentItems.length) this.renderJustifiedLayout(this.currentItems);
            return;
        }

        const filtered = [];
        const seqIndexes = new Map();
        let acc = this.currentAllOffset || 0;
        this.currentItems.forEach((it, index) => {
            const haystack = [
                it.name || '',
                it.path || '',
                it.url ? it.url.split('/').pop() : ''
            ].join(' ').toLowerCase();
            if (haystack.includes(normalized)) {
                seqIndexes.set(filtered.length, acc);
                filtered.push(it);
            }
            acc += (it.type === 'album' ? (it.image_count || 0) : 1);
        });

        this.filteredSeqIndexes = seqIndexes;
        grid.innerHTML = '';
        grid.style.height = 'auto';
        if (!filtered.length) {
            grid.innerHTML = this._emptyHtml('icon:search-x', '没有匹配的图片', '换个关键词试试');
            return;
        }
        this.renderJustifiedLayout(filtered);
    },

    // ===== 幻灯片 =====
    // startIndex：隐藏后重新显示时从原来的位置继续（默认从第一张开始）
    toggleSlideshow(startIndex) {
        if (this.slideshowTimer) {
            this._slideshowWanted = false;   // 用户主动停止：切回来不再自动继续
            this._slideshowResumeIndex = 0;
            this._stopSlideshow();
            return;
        }
        if (!this.currentAllImages.length) return;
        const index = Math.min(Math.max(Number(startIndex) || 0, 0), this.currentAllImages.length - 1);
        this._slideshowWanted = true;
        this.lightbox.show(this.currentAllImages, index);
        this.slideshowTimer = setInterval(() => this.lightbox.navigate(1), 3000);
        document.getElementById('btn-slideshow').textContent = '⏸ 停止';
        Toast.info('幻灯片播放中，每 3 秒切换一张');
    },

    _stopSlideshow() {
        if (this.slideshowTimer) {
            clearInterval(this.slideshowTimer);
            this.slideshowTimer = null;
        }
        const btn = document.getElementById('btn-slideshow');
        if (btn) btn.textContent = '▶ 幻灯片';
    },

    // 离开当前列表（换相册 / 换文件夹）：用户已经不在这个上下文里，
    // 隐藏再显示不应该自动续播旧列表的幻灯片
    _cancelSlideshow() {
        this._slideshowWanted = false;
        this._stopSlideshow();
    },

    // ============================================================
    // 多选 / 移动 / 删除
    // ============================================================
    toggleMultiSelectMode() {
        this.isMultiSelectMode = !this.isMultiSelectMode;
        const btn = document.getElementById('btn-multi-select');
        const deleteBtn = document.getElementById('btn-delete-selected');
        const moveBtn = document.getElementById('btn-move-selected');
        const thumbBtn = document.getElementById('btn-refresh-thumbs');
        btn.classList.toggle('active', this.isMultiSelectMode);
        btn.textContent = this.isMultiSelectMode ? '退出多选' : '开启多选';
        deleteBtn.classList.toggle('hidden', !this.isMultiSelectMode);
        moveBtn.classList.toggle('hidden', !this.isMultiSelectMode);
        if (thumbBtn) thumbBtn.classList.toggle('hidden', !this.isMultiSelectMode);
        const selectionCount = document.getElementById('iv-selection-count');
        if (selectionCount) selectionCount.classList.toggle('hidden', !this.isMultiSelectMode);
        if (!this.isMultiSelectMode) this.clearSelection();
    },

    _updateSelectionCount() {
        const el = document.getElementById('iv-selection-count');
        if (!el) return;
        el.textContent = `已选 ${this.selectedImages.size} 项`;
        el.classList.toggle('hidden', !this.isMultiSelectMode || this.selectedImages.size === 0);
    },

    toggleSelectImage(imgUrl, cardEl) {
        if (this.selectedImages.has(imgUrl)) {
            this.selectedImages.delete(imgUrl);
            cardEl.classList.remove('selected');
        } else {
            this.selectedImages.add(imgUrl);
            cardEl.classList.add('selected');
        }
        this._updateSelectionCount();
    },

    clearSelection() {
        this.selectedImages.clear();
        document.querySelectorAll('.iv-image-card.selected').forEach(el => el.classList.remove('selected'));
        this._updateSelectionCount();
    },

    handleContextAction(action) {
        const imgs = [...this.selectedImages];
        if (!imgs.length) return;
        if (action === 'view') {
            const idx = this.currentAllImages.findIndex(i => i.url === imgs[0]);
            this.lightbox.show(this.currentAllImages, Math.max(0, idx));
        } else if (action === 'select' && !this.isMultiSelectMode) {
            this.toggleMultiSelectMode();
        } else if (action === 'move') {
            this.openMoveModal();
        } else if (action === 'delete') {
            this.deleteSelectedImages();
        }
    },

    async deleteSelectedImages() {
        const imgs = [...this.selectedImages];
        if (!imgs.length) return;
        const ok = await confirmDialog(`确定删除 ${imgs.length} 张图片吗？`, { danger: true });
        if (!ok) return;
        try {
            const result = await Bridge.call('delete_files', imgs);
            if (result.errors.length) Toast.error(`部分删除失败: ${result.errors.join('; ')}`);
            else Toast.success(`已删除 ${imgs.length} 张图片`);
            this.clearSelection();
            await this.loadImages(this.currentPath, this.currentPage);
        } catch (e) {
            Toast.error('删除请求失败');
        }
    },

    openMoveModal() {
        if (!this.selectedImages.size) return;
        this.moveDestPath = '';
        document.getElementById('move-modal').classList.add('active');
    },

    closeMoveModal() {
        document.getElementById('move-modal').classList.remove('active');
    },

    async confirmMove() {
        if (!this.moveDestPath && this.moveDestPath !== '') {
            Toast.warning('请选择目标文件夹');
            return;
        }
        const imgs = [...this.selectedImages];
        if (!imgs.length) return;
        try {
            const result = await Bridge.call('move_files', imgs, this.moveDestPath);
            if (result.errors.length) Toast.error(`部分移动失败: ${result.errors.join('; ')}`);
            else Toast.success(`已移动 ${imgs.length} 张图片`);
            this.clearSelection();
            this.closeMoveModal();
            await this.loadImages(this.currentPath, this.currentPage);
        } catch (e) {
            Toast.error('移动请求失败');
        }
    },
});
