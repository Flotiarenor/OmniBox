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
            // 被动同步：只校验当前目录（组件去抖，壳侧还会按目录做 mtime 短路）
            this._autoSyncFreshness(path);
            this.currentItems = data.items || [];
            this.currentImages = this.currentItems.filter(it => it.type !== 'album');
            this.currentAllImages = data.all_images || this.currentImages;
            this.currentAllOffset = data.all_offset || 0;
            // 序列里少了容器子目录的图片（后端不再为首屏展开整棵子树）：
            // 记住目录路径，等真正打开灯箱时再取完整序列补进来
            this._sequencePath = data.sequence_pending ? path : null;
            this._sequenceFull = null;
            // 只在真撞上 5000 条上限时提示：序列不完整还有另一个原因——里面有待补的
            // 容器瓦片（`sequence_pending`），那不是截断，不该弹「已截断」吓用户
            if (data.all_capped) {
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
            grid.innerHTML = this._emptyHtml('icon:triangle-alert', '图片加载失败');
            grid.style.height = 'auto';
        }
    },

    renderJustifiedLayout(items) {
        const grid = document.getElementById('image-grid');
        this._resetAlbumTiles();
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
                if (item.pending) card.classList.add('iv-tile-pending');
                if (item.cover) {
                    const img = document.createElement('img');
                    img.className = 'iv-cover-img';
                    // 带 mtime 版本号：缩略图被重建后 URL 才会变（壳的 /thumbs 有一天强缓存）
                    img.src = this._thumbUrl({ url: item.cover, mtime: item.mtime });
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
                // 角标：补到计数之前不渲染，避免先亮一个 0（见 _applyAlbumTile）
                if (item.use_time_name && item.total_count != null) {
                    const badge = document.createElement('span');
                    badge.className = 'iv-count-badge';
                    badge.textContent = item.total_count;
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
                        this._showFromCard(item, index, seqIndex);
                    });
                }
            } else {
                // 单图卡片
                card.dataset.url = item.url;
                const img = document.createElement('img');
                img.src = this._thumbUrl(item);
                img.loading = 'lazy';
                img.alt = item.url.split('/').pop();
                const p = document.createElement('p');
                p.textContent = item.url.split('/').pop();
                card.append(img, p);
                card.addEventListener('click', () => {
                    if (this.isMultiSelectMode) {
                        this.toggleSelectImage(item.url, card);
                    } else {
                        this._showFromCard(item, index, seqIndex);
                    }
                });
            }
            if (item.type !== 'album' && this.selectedImages.has(item.url)) {
                card.classList.add('selected');
            }
            grid.appendChild(card);
            if (item.pending) this._watchAlbumTile(item, card);
        });
        // 首批立即取：占位瓦片（首启等冷索引场景）不必等用户滚动才补
        this._flushAlbumTiles();
    },

    // ============================================================
    // 灯箱连续浏览序列（分级加载第三级）
    //
    // 后端列目录时不再展开容器子目录的图片（展开要读整棵子树，实测某个 4 万张的
    // 容器目录单是列出外层就要 3.3s），所以 `currentAllImages` 可能不完整。点开
    // 瓦片时先按现有序列**立刻**打开灯箱，再异步取完整序列交给
    // `lightbox.setItems()` 换掉（它自己会停在正在看的那张）—— 用户不用等，
    // 也不影响已经在看的那张图。
    // ============================================================
    _showFromCard(item, index, seqIndex) {
        const startIndex = seqIndex.get(index) || 0;
        this.lightbox.show(this.currentAllImages, startIndex);
        if (this._sequencePath) this._upgradeSequence(startIndex, item.url || item.cover);
    },

    async _upgradeSequence(startIndex, clickedUrl) {
        if (!this._sequencePath || this._sequenceFull) return;
        const path = this._sequencePath;
        let data = null;
        try {
            data = await Bridge.call('list_album_images', path);
        } catch (e) {
            return;                                  // 取不到就按现有序列用，不报错打断
        }
        // 期间用户已经换目录 / 换页 → 结果作废
        if (!data || !data.images || this._sequencePath !== path
                || this.currentPath !== path) {
            return;
        }
        this._sequenceFull = data.images;
        const lightbox = this.lightbox;
        if (!lightbox || typeof lightbox.setItems !== 'function') return;
        const shown = lightbox.items && lightbox.items.length
            ? lightbox.items[lightbox.getIndex()] : null;
        const focusUrl = (shown && shown.url) || clickedUrl || '';
        // 交给灯箱自己换序列：items / currentIndex 是它的闭包状态，外部赋值不生效
        lightbox.setItems(data.images, focusUrl, startIndex);
    },

    // ============================================================
    // 相册瓦片的二级加载（分级加载第二级）
    //
    // 后端列目录时只读当前目录一层：容器子目录的封面与递归图片数若不在索引缓存
    // 里，就以 `pending` 占位返回，不含封面、`total_count` 为 null。这里对**进入
    // 视口**的占位瓦片分批调 `load_album_tiles` 补齐，每批十几个目录。
    //
    // 补到之后只改这张卡自己的封面与角标，**不重算瀑布流布局**：布局在首屏已经
    // 定了，重排会让正在滚动的位置跳动。占位期用 1:1 的框（后端 width/height 给 1）
    // 兜底，补到封面后按真实比例显示。
    // ============================================================
    _resetAlbumTiles() {
        if (this._tileObserver) {
            this._tileObserver.disconnect();
            this._tileObserver = null;
        }
        if (this._tileTimer) {
            clearTimeout(this._tileTimer);
            this._tileTimer = null;
        }
        this._tileQueue = [];
        this._tileByPath = new Map();
        this._tileObserver = ('IntersectionObserver' in window)
            ? new IntersectionObserver((entries) => {
                let hit = false;
                entries.forEach((entry) => {
                    if (!entry.isIntersecting) return;
                    const path = entry.target.dataset.pendingPath;
                    if (path && this._tileByPath.has(path) && !this._tileQueue.includes(path)) {
                        this._tileQueue.push(path);
                        hit = true;
                    }
                });
                if (hit) this._flushAlbumTiles();
            }, { root: document.getElementById('iv-content'), rootMargin: '250px 0px' })
            : null;
    },

    _watchAlbumTile(item, card) {
        if (!this._tileByPath) this._resetAlbumTiles();
        this._tileByPath.set(item.path, { item, card });
        if (this._tileObserver) {
            card.dataset.pendingPath = item.path;
            this._tileObserver.observe(card);
        }
        // 没有 IntersectionObserver（或卡片尚未进入视口判定）时兜底：先排进队列，
        // 由 _flushAlbumTiles 按批取
        if (!this._tileObserver && !this._tileQueue.includes(item.path)) {
            this._tileQueue.push(item.path);
        }
    },

    _flushAlbumTiles() {
        if (this._tileTimer || !this._tileQueue || !this._tileQueue.length) return;
        // 合并同一帧内的多次入队：一次请求覆盖一批瓦片，而不是一张一个请求
        this._tileTimer = setTimeout(() => {
            this._tileTimer = null;
            this._loadAlbumTileBatch();
        }, this._TILE_BATCH_DELAY);
    },

    async _loadAlbumTileBatch() {
        if (!this._tileQueue || !this._tileQueue.length) return;
        const batch = this._tileQueue.splice(0, this._TILE_BATCH_SIZE);
        let tiles = {};
        try {
            const data = await Bridge.call('load_album_tiles', batch);
            tiles = (data && data.tiles) || {};
        } catch (e) {
            tiles = {};
        }
        batch.forEach((path) => {
            const entry = this._tileByPath && this._tileByPath.get(path);
            if (!entry) return;
            this._applyAlbumTile(entry.item, entry.card, tiles[path] || null);
            this._tileByPath.delete(path);
            if (this._tileObserver) this._tileObserver.unobserve(entry.card);
        });
        // 队列里还有（用户滚得快 / 首屏瓦片多）就继续下一批
        if (this._tileQueue.length) this._flushAlbumTiles();
    },

    _applyAlbumTile(item, card, tile) {
        if (!tile) return;
        item.pending = false;
        item.cover = tile.cover || '';
        item.total_count = tile.total_count;
        item.use_time_name = !!tile.use_time_name;
        if (tile.cover) {
            const img = card.querySelector('img.iv-cover-img')
                || card.querySelector('.iv-cover-fallback');
            if (img) {
                const real = document.createElement('img');
                real.className = 'iv-cover-img';
                real.alt = item.name || '';
                real.loading = 'lazy';
                real.onerror = function () {
                    this.outerHTML = '<div class="iv-cover-fallback">'
                        + Icons.html('icon:image-off') + '</div>';
                };
                real.src = this._thumbUrl({ url: tile.cover, mtime: tile.mtime });
                img.replaceWith(real);
            }
        }
        // 角标：Pixiv 排序下显示递归张数；补到之前不显示（避免先亮一个 0）
        if (item.use_time_name && tile.total_count != null) {
            let badge = card.querySelector('.iv-count-badge');
            if (!badge) {
                badge = document.createElement('span');
                badge.className = 'iv-count-badge';
                card.appendChild(badge);
            }
            badge.textContent = tile.total_count;
        }
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
        document.getElementById('btn-slideshow').innerHTML = Icons.html('icon:pause') + ' 停止';
        Toast.info('幻灯片播放中，每 3 秒切换一张');
    },

    _stopSlideshow() {
        if (this.slideshowTimer) {
            clearInterval(this.slideshowTimer);
            this.slideshowTimer = null;
        }
        const btn = document.getElementById('btn-slideshow');
        if (btn) btn.innerHTML = Icons.html('icon:play') + ' 幻灯片';
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
