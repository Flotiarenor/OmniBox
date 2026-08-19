class ImageViewer {
    constructor() {
        this.currentPath = '';
        this.currentPage = 1;
        this.currentImages = [];
        this.currentSettings = {};   // 当前生效的设置（行高、排序等）
        this.currentRowHeight = 200;
        this.isMultiSelectMode = false;
        this.selectedImages = new Set();
        this.moveDestPath = '';

        this.lightbox = null;
        this.pagination = null;
        this.contextMenu = null;
        this._initialized = false;
    }

    async init() {
        if (this._initialized) return;
        this._initialized = true;

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

        window.addEventListener('resize', () => {
            if (this.currentImages.length > 0) {
                this.renderJustifiedLayout(this.currentImages);
            }
        });
    }

    initSidebarTree() {
        createTree(document.getElementById('folder-tree'), {
            data: [{ name: '根目录', path: '' }],
            onLoadChildren: async (path) => {
                return await Bridge.call('list_dir', path);
            },
            onClick: (item) => {
                this.currentPath = item.path;
                this.currentPage = 1;
                this.loadImages(item.path);
            }
        });
        this.loadImages('');
    }

    initMoveModal() {
        createTree(document.getElementById('move-tree'), {
            data: [{ name: '根目录', path: '' }],
            onLoadChildren: async (path) => {
                return await Bridge.call('list_dir', path);
            },
            onClick: (item) => { this.moveDestPath = item.path; }
        });
    }

    bindToolbar() {
        document.getElementById('btn-multi-select')
            .addEventListener('click', () => this.toggleMultiSelectMode());
        document.getElementById('btn-delete-selected')
            .addEventListener('click', () => this.deleteSelectedImages());
        document.getElementById('btn-move-selected')
            .addEventListener('click', () => this.openMoveModal());
    }

    toggleMultiSelectMode() {
        this.isMultiSelectMode = !this.isMultiSelectMode;
        const btn = document.getElementById('btn-multi-select');
        const deleteBtn = document.getElementById('btn-delete-selected');
        const moveBtn = document.getElementById('btn-move-selected');
        if (this.isMultiSelectMode) {
            btn.classList.add('active'); btn.textContent = '退出多选';
            deleteBtn.classList.remove('hidden');
            moveBtn.classList.remove('hidden');
        } else {
            btn.classList.remove('active'); btn.textContent = '开启多选';
            deleteBtn.classList.add('hidden');
            moveBtn.classList.add('hidden');
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

    bindGridEvents() {
        document.getElementById('image-grid').addEventListener('contextmenu', (e) => {
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
            const idx = this.currentImages.findIndex(i => i.url === imgs[0]);
            this.lightbox.show(this.currentImages, idx);
        } else if (action === 'select' && !this.isMultiSelectMode) {
            this.toggleMultiSelectMode();
        } else if (action === 'move') {
            this.openMoveModal();
        } else if (action === 'delete') {
            this.deleteSelectedImages();
        }
    }

    async deleteSelectedImages() {
        const imgs = [...this.selectedImages];
        if (imgs.length === 0) return;
        if (!confirm(`确定要删除 ${imgs.length} 张图片吗？`)) return;
        try {
            const result = await Bridge.call('delete_files', imgs);
            if (result.errors.length > 0) alert(`部分删除失败:\n${result.errors.join('\n')}`);
            const deletedSet = new Set(result.deleted || []);
            [...document.querySelectorAll('.card')].forEach(card => {
                if (deletedSet.has(card.dataset.url)) card.remove();
            });
            this.currentImages = this.currentImages.filter(img => !deletedSet.has(img.url));
            this.selectedImages.clear();
        } catch (e) { alert('删除请求失败'); }
    }

    openMoveModal() {
        if (this.selectedImages.size === 0) return;
        this.moveDestPath = '';
        document.getElementById('move-modal').classList.add('active');
    }
    closeMoveModal() { document.getElementById('move-modal').classList.remove('active'); }

    async confirmMove() {
        if (this.moveDestPath === null || this.moveDestPath === undefined) {
            alert('请选择目标文件夹'); return;
        }
        const imgs = [...this.selectedImages];
        if (imgs.length === 0) return;
        try {
            const result = await Bridge.call('move_files', imgs, this.moveDestPath);
            if (result.errors.length > 0) alert(`部分移动失败:\n${result.errors.join('\n')}`);
            const movedSet = new Set(result.moved || []);
            [...document.querySelectorAll('.card')].forEach(card => {
                if (movedSet.has(card.dataset.url)) card.remove();
            });
            this.currentImages = this.currentImages.filter(img => !movedSet.has(img.url));
            this.selectedImages.clear();
            this.closeMoveModal();
        } catch (e) { alert('移动请求失败'); }
    }

    bindSettings() {
        document.getElementById('btn-settings').addEventListener('click', async () => {
            document.getElementById('settings-modal').classList.add('active');
            document.getElementById('setting-current-folder-name').textContent =
                this.currentPath || '根目录';
            try {
                const s = await Bridge.call('get_settings', this.currentPath);
                document.getElementById('setting-row-height').value = s.row_height;
                document.getElementById('setting-row-height-val').textContent = s.row_height;
                document.getElementById('setting-per-page').value = s.per_page;
                document.getElementById('setting-sort-by').value = s.sort_by;
                document.getElementById('setting-sort-order').value = s.sort_order;
                const rootDir = await Bridge.call('get_root_dir');
                document.getElementById('setting-root-dir').value = rootDir || '';
            } catch (e) {}
        });
        document.getElementById('setting-row-height').addEventListener('input', (e) => {
            document.getElementById('setting-row-height-val').textContent = e.target.value;
        });
    }
    closeSettingsModal() { document.getElementById('settings-modal').classList.remove('active'); }

    async saveSettings() {
        const isFolderOnly = document.getElementById('setting-apply-to-folder').checked;
        const settings = {
            row_height: parseInt(document.getElementById('setting-row-height').value),
            per_page: parseInt(document.getElementById('setting-per-page').value),
            sort_by: document.getElementById('setting-sort-by').value,
            sort_order: document.getElementById('setting-sort-order').value,
            root_dir: document.getElementById('setting-root-dir').value.trim() || undefined
        };
        try {
            if (isFolderOnly) {
                await Bridge.call('save_settings', this.currentPath, settings);
            } else {
                // 保存全局设置
                await Bridge.call('save_settings', '', settings);
                // 如果当前文件夹有独立设置，则清除，使其回退到全局
                if (this.currentPath) {
                    await Bridge.call('clear_folder_settings', this.currentPath);
                }
            }
            // 更新当前设置缓存，并重新加载图片以应用新设置
            this.currentSettings = settings;
            this.currentRowHeight = settings.row_height;
            this.closeSettingsModal();
            this.loadImages(this.currentPath, 1);
        } catch (e) { alert('保存设置失败'); }
    }

    async loadImages(path = '', page = 1) {
        const grid = document.getElementById('image-grid');
        const paginationEl = document.getElementById('pagination');
        const statsEl = document.getElementById('folder-stats');
        grid.innerHTML = '<div class="loading">图片加载中...</div>';
        paginationEl.innerHTML = '';
        try {
            // 使用当前设置中的参数，若无则使用默认值
            const perPage = this.currentSettings.per_page || 40;
            const sortBy = this.currentSettings.sort_by || 'mtime';
            const sortOrder = this.currentSettings.sort_order || 'desc';
            const data = await Bridge.call('list_images', path, page, perPage, sortBy, sortOrder);
            this.currentPage = page;
            this.currentImages = data.images;
            // 更新设置（后端返回的最新设置）
            if (data.settings) {
                this.currentSettings = data.settings;
                this.currentRowHeight = data.settings.row_height || 200;
            }
            statsEl.textContent = `共 ${data.total} 张图片`;
            grid.innerHTML = '';
            if (data.images.length === 0) {
                grid.innerHTML = '<div class="loading">此文件夹暂无图片</div>';
                grid.style.height = 'auto';
                return;
            }
            this.renderJustifiedLayout(data.images);
            this.pagination.render(data.page, Math.ceil(data.total / perPage));
        } catch (error) {
            grid.innerHTML = '<div class="loading">图片加载失败</div>';
            grid.style.height = 'auto';
        }
    }

    renderJustifiedLayout(images) {
        const grid = document.getElementById('image-grid');
        grid.innerHTML = '';
        const containerWidth = grid.clientWidth;
        if (containerWidth === 0 || images.length === 0) return;
        const targetHeight = this.currentRowHeight;
        const gap = 5;
        const { cards, totalHeight } = JustifiedLayout.compute(images, containerWidth, targetHeight, gap);
        grid.style.height = `${totalHeight}px`;
        cards.forEach((cardData, index) => {
            const card = document.createElement('div');
            card.className = 'card';
            card.dataset.url = cardData.url;
            card.style.cssText = `left:${cardData.x}px;top:${cardData.y}px;width:${cardData.w}px;height:${cardData.h}px;`;
            const img = document.createElement('img');
            img.src = Bridge.thumbUrl(cardData.url);
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
