/**
 * 图片浏览器视图 — 组装所有组件
 */
class ImageViewer {
    constructor() {
        this.currentPath = '';
        this.currentPage = 1;
        this.currentImages = [];
        this.currentSettings = {};
        this.currentRowHeight = 200;

        this.isMultiSelectMode = false;
        this.selectedImages = new Set();

        // 组件实例
        this.sidebarTree = null;
        this.moveTree = null;
        this.lightbox = null;
        this.pagination = null;
        this.contextMenu = null;

        this.moveDestPath = '';
    }

    async init() {
        this.lightbox = createLightbox({
            getImageUrl: (item) => item.url
        });

        this.pagination = createPagination(
            document.getElementById('pagination'),
            { onPageChange: (page) => this.loadImages(this.currentPath, page) }
        );

        this.contextMenu = createContextMenu({
            items: [
                { label: '查看原图', action: 'view' },
                { label: '多选此图', action: 'select' },
                { label: '移动到此...', action: 'move' },
                { label: '删除', action: 'delete', danger: true }
            ],
            onSelect: (action) => this.handleContextAction(action)
        });

        this.initSidebarTree();
        this.initMoveModal();
        this.bindToolbar();
        this.bindSettings();
        this.bindGridEvents();

        // 窗口resize时重新布局
        window.addEventListener('resize', () => {
            if (this.currentImages.length > 0) {
                this.renderJustifiedLayout(this.currentImages);
            }
        });
    }

    // ===== 侧边栏树 =====
    initSidebarTree() {
        this.sidebarTree = createTree(
            document.getElementById('folder-tree'),
            {
                data: [{ name: '根目录', path: '' }],
                onLoadChildren: async (path) => {
                    return await bridge.call('file_list_dir', path);
                },
                onClick: (item) => {
                    this.currentPath = item.path;
                    this.currentPage = 1;
                    this.loadImages(item.path);
                }
            }
        );
        // 自动展开根节点
        document.querySelector('#folder-tree .tree-label').click();
    }

    // ===== 移动弹窗树 =====
    initMoveModal() {
        this.moveTree = createTree(
            document.getElementById('move-tree'),
            {
                data: [{ name: '根目录', path: '' }],
                onLoadChildren: async (path) => {
                    return await bridge.call('file_list_dir', path);
                },
                onClick: (item) => {
                    this.moveDestPath = item.path;
                }
            }
        );
    }

    // ===== 工具栏 =====
    bindToolbar() {
        document.getElementById('btn-multi-select').addEventListener('click', () => {
            this.toggleMultiSelectMode();
        });
        document.getElementById('btn-delete-selected').addEventListener('click', () => {
            this.deleteSelectedImages();
        });
        document.getElementById('btn-move-selected').addEventListener('click', () => {
            this.openMoveModal();
        });
    }

    toggleMultiSelectMode() {
        this.isMultiSelectMode = !this.isMultiSelectMode;
        const btn = document.getElementById('btn-multi-select');
        const actionBtns = document.querySelectorAll('.tool-group .tool-btn:not(#btn-multi-select):not(#btn-settings)');
        if (this.isMultiSelectMode) {
            btn.classList.add('active');
            btn.textContent = '退出多选';
            actionBtns.forEach(b => b.classList.remove('hidden'));
        } else {
            btn.classList.remove('active');
            btn.textContent = '开启多选';
            actionBtns.forEach(b => b.classList.add('hidden'));
            this.clearSelection();
        }
    }

    toggleSelectImage(imgUrl, cardEl) {
        if (this.selectedImages.has(imgUrl)) {
            this.selectedImages.delete(imgUrl);
            cardEl.classList.remove('selected');
        } else {
            this.selectedImages.add(imgUrl);
            cardEl.classList.add('selected');
        }
    }

    clearSelection() {
        this.selectedImages.clear();
        document.querySelectorAll('.card.selected').forEach(el => el.classList.remove('selected'));
    }

    // ===== 右键菜单 =====
    bindGridEvents() {
        const grid = document.getElementById('image-grid');

        grid.addEventListener('contextmenu', (e) => {
            const card = e.target.closest('.card');
            if (!card) return;
            e.preventDefault();
            const imgUrl = card.dataset.url;
            if (!this.isMultiSelectMode || !this.selectedImages.has(imgUrl)) {
                this.clearSelection();
                this.toggleSelectImage(imgUrl, card);
            }
            this.contextMenu.show(e.clientX, e.clientY, { url: imgUrl });
        });
    }

    handleContextAction(action) {
        const imgs = [...this.selectedImages];
        if (imgs.length === 0) return;

        if (action === 'view') {
            const index = this.currentImages.findIndex(i => i.url === imgs[0]);
            this.lightbox.show(this.currentImages, index);
        } else if (action === 'select' && !this.isMultiSelectMode) {
            this.toggleMultiSelectMode();
        } else if (action === 'move') {
            this.openMoveModal();
        } else if (action === 'delete') {
            this.deleteSelectedImages();
        }
    }

    // ===== 删除 =====
    async deleteSelectedImages() {
        const imgs = [...this.selectedImages];
        if (imgs.length === 0) return;
        if (!confirm(`确定要删除 ${imgs.length} 张图片吗？此操作不可撤销！`)) return;

        try {
            const result = await bridge.call('file_delete', imgs);
            if (result.errors.length > 0) alert(`部分删除失败:\n${result.errors.join('\n')}`);
            result.deleted.forEach(url => {
                const card = document.querySelector(`.card[data-url="${url}"]`);
                if (card) card.remove();
                this.selectedImages.delete(url);
            });
        } catch (e) {
            alert('删除请求失败');
        }
    }

    // ===== 移动弹窗 =====
    openMoveModal() {
        if (this.selectedImages.size === 0) return;
        this.moveDestPath = '';
        document.getElementById('move-modal').classList.add('active');
        // 展开根节点
        const firstLabel = document.querySelector('#move-tree .tree-label');
        if (firstLabel && !firstLabel.classList.contains('active')) {
            firstLabel.click();
        }
    }

    closeMoveModal() {
        document.getElementById('move-modal').classList.remove('active');
    }

    async confirmMove() {
        if (this.moveDestPath === null || this.moveDestPath === undefined) {
            alert('请选择目标文件夹');
            return;
        }
        const imgs = [...this.selectedImages];
        if (imgs.length === 0) return;

        try {
            const result = await bridge.call('file_move', imgs, this.moveDestPath);
            if (result.errors.length > 0) alert(`部分移动失败:\n${result.errors.join('\n')}`);
            result.moved.forEach(url => {
                const card = document.querySelector(`.card[data-url="${url}"]`);
                if (card) card.remove();
                this.selectedImages.delete(url);
            });
            this.closeMoveModal();
        } catch (e) {
            alert('移动请求失败');
        }
    }

    // ===== 设置 =====
    bindSettings() {
        document.getElementById('btn-settings').addEventListener('click', async () => {
            document.getElementById('settings-modal').classList.add('active');
            document.getElementById('setting-current-folder-name').textContent = this.currentPath || '根目录';
            try {
                const settings = await bridge.call('settings_get', this.currentPath);
                document.getElementById('setting-row-height').value = settings.row_height;
                document.getElementById('setting-row-height-val').textContent = settings.row_height;
                document.getElementById('setting-per-page').value = settings.per_page;
                document.getElementById('setting-sort-by').value = settings.sort_by;
                document.getElementById('setting-sort-order').value = settings.sort_order;
            } catch (e) {}
        });

        document.getElementById('setting-row-height').addEventListener('input', (e) => {
            document.getElementById('setting-row-height-val').textContent = e.target.value;
        });
    }

    closeSettingsModal() {
        document.getElementById('settings-modal').classList.remove('active');
    }

    async saveSettings() {
        const isFolderOnly = document.getElementById('setting-apply-to-folder').checked;
        const settings = {
            row_height: parseInt(document.getElementById('setting-row-height').value),
            per_page: parseInt(document.getElementById('setting-per-page').value),
            sort_by: document.getElementById('setting-sort-by').value,
            sort_order: document.getElementById('setting-sort-order').value
        };

        try {
            await bridge.call('settings_save', isFolderOnly ? this.currentPath : '', settings);
            this.closeSettingsModal();
            this.loadImages(this.currentPath, 1);
        } catch (e) {
            alert('保存设置失败');
        }
    }

    // ===== 图片加载 =====
    async loadImages(path = '', page = 1) {
        const grid = document.getElementById('image-grid');
        const paginationEl = document.getElementById('pagination');
        const statsEl = document.getElementById('folder-stats');

        grid.innerHTML = '<div class="loading">图片加载中...</div>';
        paginationEl.innerHTML = '';

        try {
            const data = await bridge.call('image_list', path, page);
            this.currentPage = page;
            this.currentImages = data.images;

            if (data.settings && data.settings.row_height) {
                this.currentRowHeight = data.settings.row_height;
                this.currentSettings = data.settings;
            }

            statsEl.textContent = `共 ${data.total} 张图片`;
            grid.innerHTML = '';

            if (data.images.length === 0) {
                grid.innerHTML = '<div class="loading">此文件夹暂无图片</div>';
                grid.style.height = 'auto';
                return;
            }

            this.renderJustifiedLayout(data.images);

            const perPage = data.settings ? data.settings.per_page : 40;
            this.pagination.render(data.page, Math.ceil(data.total / perPage));

        } catch (error) {
            grid.innerHTML = '<div class="loading">图片加载失败</div>';
            grid.style.height = 'auto';
        }
    }

    // ===== Justified Layout =====
    renderJustifiedLayout(images) {
        const grid = document.getElementById('image-grid');
        grid.innerHTML = '';

        const containerWidth = grid.clientWidth;
        if (containerWidth === 0 || images.length === 0) return;

        const targetHeight = this.currentRowHeight;
        const gap = 5;
        const rows = [];
        let currentRow = { items: [], width: 0 };

        images.forEach(img => {
            const ratio = (img.width || 1) / (img.height || 1);
            const itemWidth = ratio * targetHeight;
            currentRow.items.push({ ...img, ratio, itemWidth });
            currentRow.width += itemWidth + gap;
            if (currentRow.width - gap >= containerWidth) {
                rows.push(currentRow);
                currentRow = { items: [], width: 0 };
            }
        });
        if (currentRow.items.length > 0) rows.push(currentRow);

        let currentTop = 0;
        const cards = [];

        rows.forEach(row => {
            const isLastRow = row === rows[rows.length - 1] && row.width - gap < containerWidth;
            const actualRowHeight = isLastRow
                ? targetHeight
                : (containerWidth - (row.items.length - 1) * gap) / (row.width - gap - (row.items.length - 1) * gap) * targetHeight;

            let currentLeft = 0;
            row.items.forEach(item => {
                const w = item.ratio * actualRowHeight;
                const h = actualRowHeight;
                cards.push({ url: item.url, x: currentLeft, y: currentTop, w, h });
                currentLeft += w + gap;
            });
            currentTop += actualRowHeight + gap;
        });

        grid.style.height = `${currentTop - gap}px`;

        cards.forEach((cardData, index) => {
            const card = document.createElement('div');
            card.className = 'card';
            card.dataset.url = cardData.url;
            card.style.cssText = `left:${cardData.x}px; top:${cardData.y}px; width:${cardData.w}px; height:${cardData.h}px;`;

            const img = document.createElement('img');
            img.src = bridge.thumbUrl(cardData.url);
            img.loading = 'lazy';
            img.alt = cardData.url.split('/').pop();

            const p = document.createElement('p');
            p.textContent = cardData.url.split('/').pop();

            card.appendChild(img);
            card.appendChild(p);
            grid.appendChild(card);

            card.addEventListener('click', () => {
                if (this.isMultiSelectMode) {
                    this.toggleSelectImage(cardData.url, card);
                } else {
                    this.lightbox.show(this.currentImages, index);
                }
            });
        });
    }
}