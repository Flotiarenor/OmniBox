// ============================================================
// OmniBox 图片相册 — 图片网格、Justified 布局、幻灯与多选操作
//
// 本文件是 ImageViewer.prototype 的一个分片：类骨架与构造函数在 app.js，
// 这里用 Object.assign 把方法扩回同一个原型。**加载顺序是硬约束** —— 必须在
// app.js 之后、实例化之前（见 index.html），契约由 tests/js/image_viewer_app_split.mjs 把关。
// ============================================================

//: 时间线「按图片」层一次给出往后多少页：后端只能保证"这一页填满了"，更远的页要真扫
//: 一遍才知道能不能排出，所以页码只按这个步长前瞻；翻过头时回到真实最后一页。
const IV_RECENT_PAGE_LOOKAHEAD = 5;

Object.assign(ImageViewer.prototype, {


    // ============================================================
    // 图片网格
    // ============================================================
    /**
     * 清空图片层状态并作废在途 loadImages。
     *
     * 切换视图、进出目录、返回相册层时调用：`_navSeq` 变化会让旧请求的结果失效，
     * `_loadSeq` 则让同一视图内被新请求取代的旧响应失效（翻页、刷新重取）。
     */
    _resetImageListState() {
        this._loadSeq += 1;
        this.currentItems = [];
        this.currentImages = [];
        this.currentAllImages = [];
        this.currentAllOffset = 0;
        this.currentPage = 1;
        this.filteredSeqIndexes = null;
        this._sequencePath = null;
        this._sequenceFull = null;
    },

    async loadImages(path = '', page = 1) {
        const requestId = ++this._loadSeq;
        const navSeq = this._navSeq;
        // 只允许"最新请求 + 当前导航代次 + 仍在图片层 + 路径未变"的响应写 DOM。
        // `_recentActive` 互斥：时间线「按图片」层的列表来自 list_recent_images，
        // 目录列表（list_folder_items）的响应不能再画进这一层。
        const isCurrent = () => requestId === this._loadSeq
            && navSeq === this._navSeq
            && this.mode === 'images'
            && !this._recentActive
            && this.currentPath === path;
        if (!isCurrent()) return;
        const grid = document.getElementById('image-grid');
        const paginationEl = document.getElementById('pagination');
        grid.innerHTML = '<div class="loading">图片加载中…</div>';
        grid.style.height = 'auto';
        paginationEl.innerHTML = '';
        try {
            // 先取该文件夹生效的设置（含父文件夹/全局回退），保证 per-folder 设置首次进入即生效
            const eff = await Bridge.call('get_settings', path);
            if (!isCurrent()) return;
            if (eff) {
                this.currentSettings = eff;
                this.currentRowHeight = eff.row_height || 200;
            }
            const perPage = this.currentSettings.per_page || 40;
            const sortBy = this.currentSettings.sort_by || 'mtime';
            const sortOrder = this.currentSettings.sort_order || 'desc';
            const data = await Bridge.call('list_folder_items', path, page, perPage, sortBy, sortOrder);
            if (!isCurrent()) return;
            this.currentPage = page;
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
            this._statusText = (data.total === imgTotal)
                ? `共 ${data.total} 张图片`
                : `共 ${data.total} 项 · ${imgTotal} 张图片`;
            this._statusState = 'ok';
            grid.innerHTML = '';
            this.filteredSeqIndexes = null;
            if (!this.currentItems.length) {
                grid.innerHTML = this._emptyHtml('icon:images', '此相册暂无图片');
                grid.style.height = 'auto';
                this._pushChrome();
                return;
            }
            const keyword = this.searchQuery;
            if (keyword) {
                this.filterCurrentImages(keyword);
            } else {
                this.renderJustifiedLayout(this.currentItems);
            }
            this.pagination.render(data.page, Math.ceil(data.total / perPage));
            this._pushChrome();
        } catch (error) {
            if (!isCurrent()) return;
            grid.innerHTML = this._emptyHtml('icon:triangle-alert', '图片加载失败');
            grid.style.height = 'auto';
            this._statusText = '图片加载失败';
            this._statusState = 'error';
            this._pushChrome();
        }
    },

    /**
     * 时间线「按图片」层的更新单元列表：后端按最近修改时间跨目录取一页"作品或单图"。
     *
     * 条目与 list_folder_items 的 `items` 同形：作品是 `type:'album'` 的瓦片（p0 封面 +
     * 张数角标），散图是 `type:'image'`；`all_images` 是本页单元按作品 p0→pN 展开的
     * 连续浏览序列。渲染直接走 renderJustifiedLayout（它已支持 album 瓦片与按张数
     * 累加的灯箱起点），本方法只负责取数、状态栏与分页。
     *
     * 与 loadImages 同一套请求代次守卫：只有"最新请求 + 当前导航代次 + 仍在图片层 +
     * 仍由「按图片」进入"的响应才能写 DOM，切走后迟到的响应不得重绘。
     */
    async loadRecentImages(page = 1) {
        const requestId = ++this._loadSeq;
        const navSeq = this._navSeq;
        const isCurrent = () => requestId === this._loadSeq
            && navSeq === this._navSeq
            && this.mode === 'images'
            && this._recentActive;
        if (!isCurrent()) return;
        const grid = document.getElementById('image-grid');
        const paginationEl = document.getElementById('pagination');
        grid.innerHTML = '<div class="loading">图片加载中…</div>';
        grid.style.height = 'auto';
        paginationEl.innerHTML = '';
        // 与 loadImages 一致：先取生效设置，保证 per_page / row_height 是当下的值。
        // 「按图片」层是跨相册视图，不属于任何目录，因此读全局设置而不是上一个目录的。
        try {
            const eff = await Bridge.call('get_settings', '');
            if (!isCurrent()) return;
            if (eff) {
                this.currentSettings = eff;
                this.currentRowHeight = eff.row_height || 200;
            }
        } catch (e) { /* 取不到就按现有设置渲染 */ }
        const perPage = this.currentSettings.per_page || 40;
        try {
            const data = await Bridge.call('list_recent_images', page, perPage);
            if (!isCurrent()) return;
            this.currentPage = page;
            this.currentItems = (data && data.items) || [];
            this.currentImages = this.currentItems.filter(it => it.type !== 'album');
            // 灯箱序列就是后端给的本页展开序列（作品按 p0→pN 逐张展开）：单元在序列
            // 里的起始下标由 renderJustifiedLayout 按"前面各单元的张数"累加得出，
            // 所以偏移恒为 0。
            this.currentAllImages = (data && data.all_images) || [];
            this.currentAllOffset = 0;
            // 最近图片列表没有"容器子目录待展开"的问题：不设 _sequencePath，
            // 打开灯箱时不会去取 list_album_images。
            this._sequencePath = null;
            this._sequenceFull = null;
            this.filteredSeqIndexes = null;
            // total 是单元数（作品 + 单图），image_total 才是图片张数：两者不同时必须
            // 分开说，否则"共 3 张"会让人以为本页只有 3 张图。
            const total = data && data.total != null ? data.total : this.currentItems.length;
            const imageTotal = data && data.image_total != null
                ? data.image_total
                : this.currentImages.length;
            const partial = !!(data && data.partial);
            // reach 是本次扫描预算内真正排列出的单元数：partial 时它才是列表的实际边界，
            // 页码与提示都以它为准（total 是整库单元数，深页在预算内到不了）。
            const reach = data && data.reach != null ? data.reach : total;
            // partial 为真表示目录扫描预算用尽：这一页不是全库扫描的结果，必须说出来，
            // 并用 warn 色状态点提示列表不完整。
            const countText = total === imageTotal
                ? `共 ${total} 张图片`
                : `共 ${total} 项 · ${imageTotal} 张图片`;
            this._statusText = partial
                ? `${countText}（仅列出最近 ${reach} 项）`
                : countText;
            this._statusState = partial ? 'warn' : 'ok';
            grid.innerHTML = '';
            if (!this.currentItems.length) {
                // 页码比实际能排出的更多：先回落到真实的最后一页（`reach` 是本次预算内
                // 排列出的单元数，total 是整库单元数），避免停在空页；回落目标等于当前页
                // 时才显示空态（防御分支）。
                if (total > 0) {
                    const lastPage = Math.max(1, Math.ceil(
                        (partial && reach > 0 ? reach : total) / perPage));
                    if (lastPage !== page) {
                        await this.loadRecentImages(lastPage);
                        return;
                    }
                }
                // 三种空态要分开：空库、页码超出整库、预算用尽后翻过了列表边界
                if (partial) {
                    grid.innerHTML = this._emptyHtml(
                        'icon:images', '已到扫描上限',
                        `最近的 ${reach} 项已列出，更早的图片未展开；点第 1 页回到最新`);
                } else if (total > 0) {
                    grid.innerHTML = this._emptyHtml(
                        'icon:images', '没有更多图片', '点第 1 页回到最新');
                } else {
                    grid.innerHTML = this._emptyHtml('icon:images', '最近没有图片');
                }
                grid.style.height = 'auto';
                if (total > 0) {
                    const currentPage = (data && data.page) || page;
                    this.pagination.render(currentPage, Math.max(1, currentPage));
                }
                this._pushChrome();
                return;
            }
            const keyword = this.searchQuery;
            if (keyword) {
                this.filterCurrentImages(keyword);
            } else {
                this.renderJustifiedLayout(this.currentItems);
            }
            // 分页：`has_next` 表示这一页填满了、还能往后翻，一次给出后 5 页；到边界那一页
            // （has_next 为假）只给到当前页。翻过头时上面的回落分支会落到真实最后一页。
            if (total > 0) {
                const currentPage = (data && data.page) || page;
                const canNext = !!(data && data.has_next);
                this.pagination.render(currentPage, Math.max(1,
                    canNext ? currentPage + IV_RECENT_PAGE_LOOKAHEAD : currentPage));
            }
            this._pushChrome();
        } catch (error) {
            if (!isCurrent()) return;
            grid.innerHTML = this._emptyHtml('icon:triangle-alert', '图片加载失败');
            grid.style.height = 'auto';
            this._statusText = '图片加载失败';
            this._statusState = 'error';
            this._pushChrome();
        }
    },

    /** 分页回调：按当前图片层的来源分派（最近图片 / 目录图片）。 */
    _reloadPage(page = 1) {
        if (this._recentActive) return this.loadRecentImages(page);
        return this.loadImages(this.currentPath, page);
    },

    /** 重载当前页（刷新、删除、移动、重建缩略图等操作结束后共用）。 */
    _reloadCurrentView() {
        if (this._recentActive) return this.loadRecentImages(this.currentPage || 1);
        if (this.mode === 'images') return this.loadImages(this.currentPath, this.currentPage || 1);
        return Promise.resolve();
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

        // 圆圈数量角标：子文件夹瓦片一律显示（数量已知时），与排序模式无关。
        // 原先只在「Pixiv 排序支持」下显示，普通排序下看不出一个作品有几页

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
                if (item.total_count != null) {
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
        // 角标：数量补齐后显示递归张数；补到之前不显示（避免先亮一个 0）
        if (tile.total_count != null) {
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
        this._pushChrome();
        Toast.info('幻灯片播放中，每 3 秒切换一张');
    },

    _stopSlideshow() {
        if (this.slideshowTimer) {
            clearInterval(this.slideshowTimer);
            this.slideshowTimer = null;
        }
        this._pushChrome();
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
        if (!this.isMultiSelectMode) this.clearSelection();
        else this._updateSelectionCount();
    },

    _updateSelectionCount() {
        // 计数与显隐由壳的顶栏动作渲染（多选态下才出现）；这里只负责上报。
        this._pushChrome();
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
            await this._reloadCurrentView();
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
            await this._reloadCurrentView();
        } catch (e) {
            Toast.error('移动请求失败');
        }
    },
});
