// ============================================================
// OmniBox 图片相册 — 返回导航栈、相册右键菜单、统计与新建相册
//
// 本文件是 ImageViewer.prototype 的一个分片：类骨架与构造函数在 app.js，
// 这里用 Object.assign 把方法扩回同一个原型。**加载顺序是硬约束** —— 必须在
// app.js 之后、实例化之前（见 index.html），契约由 tests/js/image_viewer_app_split.mjs 把关。
// ============================================================

Object.assign(ImageViewer.prototype, {

    _handleBack() {
        if (this.mode === 'images') {
            this._cancelSlideshow();
            if (this.navStack.length) {
                const prev = this._popNavStack();
                if (prev === '') {
                    this.mode = 'albums';
                    this.childParentPath = '';
                    this.currentPath = '';
                    this.showAlbums();
                    this._restoreScroll();
                } else {
                    this._showFolder(prev);
                }
                return;
            }
            if (this.fromChildren && this.childParentPath) {
                this.mode = 'children';
                this.currentPath = this.childParentPath;
                this.showAlbums();
                this._restoreScroll();
            } else {
                this.mode = 'albums';
                this.childParentPath = '';
                this.currentPath = '';
                this.showAlbums();
                this._restoreScroll();
            }
        } else if (this.mode === 'children') {
            if (this.navStack.length) {
                const prev = this._popNavStack();
                if (prev === '') {
                    this.mode = 'albums';
                    this.childParentPath = '';
                    this.currentPath = '';
                    this.showAlbums();
                    this._restoreScroll();
                } else {
                    this._showFolder(prev);
                }
            } else {
                this.mode = 'albums';
                this.childParentPath = '';
                this.currentPath = '';
                this.showAlbums();
                this._restoreScroll();
            }
        }
    },

    showAlbumMenu(path, e) {
        this._closeAlbumMenu();
        const album = this.albums.find(a => a.path === path);
        if (!album) return;
        const promoted = this.albumConfig.promoted || [];
        const visibleEmpty = this.albumConfig.visible_empty_dirs || [];
        const items = [];
        // 子相册默认折叠：depth===1 的容器目录给出「展开/收纳」切换；
        // 更深层的目录由祖先决定是否可见，不在这里单独展开
        if (album.depth === 1 && album.has_children) {
            const isCollapsed = this._isCollapsed(album);
            items.push({
                label: isCollapsed ? '📂 展开子相册' : '📦 收纳子相册',
                action: isCollapsed ? 'expand' : 'collapse'
            });
        }
        if (album.depth > 1) {
            items.push({
                label: promoted.includes(path) ? '↩ 收回父相册' : '📌 提升到全部相册',
                action: promoted.includes(path) ? 'unpromote' : 'promote'
            });
        }
        // 「新建相册」建出来的空目录：可从视图中移除（目录本身保留）
        if (!album.readable && visibleEmpty.includes(path)) {
            items.push({
                label: '🙈 不再显示此空相册',
                action: 'forget-empty',
                danger: true
            });
        }
        items.push({
            label: '🖼 重建此相册缩略图',
            action: 'rebuild'
        });
        if (!items.length) return;
        const menuEl = document.createElement('div');
        menuEl.className = 'iv-context-menu';
        menuEl.innerHTML = items.map(it => `<button data-act="${it.action}">${it.label}</button>`).join('');
        menuEl.style.left = `${Math.min(e.clientX, window.innerWidth - 160)}px`;
        menuEl.style.top = `${Math.min(e.clientY, window.innerHeight - 90)}px`;
        document.body.appendChild(menuEl);
        this._albumMenuEl = menuEl;
        menuEl.addEventListener('click', async (ev) => {
            const act = ev.target.dataset.act;
            this._closeAlbumMenu();
            if (act === 'rebuild') {
                this.rebuildFolder(path);
                return;
            }
            if (act === 'forget-empty') {
                try {
                    const result = await Bridge.call('delete_folder', path);
                    if (result && result.success) {
                        Toast.success('空相册已从视图移除');
                        await this.loadAlbums();
                    } else {
                        Toast.error((result && result.error) || '操作失败');
                    }
                } catch (err) {
                    Toast.error('操作失败');
                }
                return;
            }
            try {
                const result = await Bridge.call('set_album_config', path, act);
                if (result && result.success) {
                    this.albumConfig = result.config;
                    Toast.success(act === 'collapse' ? '子相册已收纳' : act === 'expand' ? '子相册已展开' : act === 'promote' ? '已提升到全部相册' : '已收回父相册');
                    this.showAlbums();
                }
            } catch (err) {
                Toast.error('操作失败');
            }
        });
    },

    _closeAlbumMenu() {
        if (this._albumMenuEl) {
            this._albumMenuEl.remove();
            this._albumMenuEl = null;
        }
    },

    _updateStats() {
        const visible = this._filterVisibleAlbums(this.albums);
        const total = visible.reduce((sum, a) => sum + (a.direct_count || 0), 0);
        document.getElementById('iv-stats').textContent = `${visible.length} 个相册 · ${total} 张图片`;
    },

    // ===== 图片文件夹（多根目录）=====
    // 列表本身（渲染 / 增删 / 去重 / 目录选择器）是 Shell 共享组件
    // window.FolderPicker（shell/frontend/public/shell/folder-picker.js），
    // 插件只保留「保存时把列表写回 root_dir / extra_roots」这一件事。

    _resetRootsList() {
        const box = document.getElementById('setting-roots');
        if (!box) return;
        const picker = window.FolderPicker.createList({
            paths: [],
            placeholder: String.raw`输入目录绝对路径，如 D:\图库`,
            emptyText: '未添加任何目录，将使用默认数据目录（./data）',
        });
        box.replaceChildren(picker.element);
        this.rootsPicker = picker;
    },

    /** 列表里的全部路径（唯一真源是共享组件的 paths 数组）。 */
    _rootPaths() {
        return this.rootsPicker ? this.rootsPicker.getPaths() : [];
    },


    // ============================================================
    // 新建相册
    // ============================================================

    openNewAlbumModal() {
        document.getElementById('iv-new-album-name').value = '';
        document.getElementById('new-album-modal').classList.add('active');
        setTimeout(() => document.getElementById('iv-new-album-name').focus(), 40);
    },

    closeNewAlbumModal() {
        document.getElementById('new-album-modal').classList.remove('active');
    },

    async createAlbum() {
        const name = document.getElementById('iv-new-album-name').value.trim();
        if (!name) {
            Toast.warning('请输入相册名称');
            return;
        }
        try {
            const result = await Bridge.call('create_folder', name);
            if (result.success) {
                this.closeNewAlbumModal();
                Toast.success(`相册「${name}」已创建`);
                await this.loadAlbums();
            } else {
                Toast.error(result.error || '创建失败');
            }
        } catch (e) {
            Toast.error('创建相册失败');
        }
    },
});
