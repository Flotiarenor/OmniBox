class MangaLibrary {
    constructor() {
        this.mangaGrid = null;
        this.recentGrid = null;
        this.favGrid = null;
        this.contextMenu = null;
        
        // 视图层级状态管理
        this.currentViewLevel = 'home'; // 'home', 'chapters', 'images'
        this.isMultiChapter = false;
        this.currentFolderName = '';
        this.currentDetail = null;
        
        this._initialized = false;
    }

    async init() {
        if (this._initialized) return;
        this._initialized = true;

        const gridOptions = {
            cardRenderer: (manga) => ({
                image: (bridge.FILE_SERVER || '') + manga.cover_url,
                title: manga.title,
                subtitle: manga.author,
                badge: `${manga.page_count}页`,
                // 注入星星HTML
                extraHtml: `<span class="fav-star card-extra-item ${manga.is_fav ? 'active' : ''}" data-folder="${manga.folder_name}">${manga.is_fav ? '★' : '☆'}</span>`
            }),
            onClick: (manga) => this.openMangaDetail(manga.folder_name),
            onContextMenu: (manga, index, event) => {
                event.preventDefault();
                this.showContextMenu(event.clientX, event.clientY, manga);
            }
        };

        this.recentGrid = createCardGrid(document.getElementById('recent-grid'), gridOptions);
        this.favGrid = createCardGrid(document.getElementById('favorites-grid'), gridOptions);
        this.mangaGrid = createCardGrid(document.getElementById('manga-grid'), gridOptions);

        // 初始化右键菜单
        this.contextMenu = createContextMenu({
            items: [], // 动态生成
            onSelect: (action) => this.handleContextAction(action)
        });

        // 事件委托：处理星星点击
        ['recent-grid', 'favorites-grid', 'manga-grid'].forEach(id => {
            document.getElementById(id).addEventListener('click', (e) => {
                if (e.target.classList.contains('fav-star')) {
                    e.stopPropagation(); // 阻止触发卡片onClick
                    const folderName = e.target.dataset.folder;
                    this.toggleFav(folderName);
                }
            });
        });

        this.bindSearch();
        await this.loadHomeData();
    }

    async loadHomeData() {
        try {
            const [state, allManga] = await Promise.all([
                bridge.call('manga_get_state'),
                bridge.call('manga_list')
            ]);
            this.recentGrid.render(state.recent);
            this.favGrid.render(state.favorites);
            this.mangaGrid.render(allManga);
        } catch (e) {
            console.error('加载漫画首页失败', e);
        }
    }

    // ====== 收藏逻辑 ======
    async toggleFav(folderName) {
        try {
            const isFav = await bridge.call('manga_toggle_favorite', folderName);
            // 优化：手动更新DOM，避免重新渲染整个列表导致闪烁
            document.querySelectorAll(`.fav-star[data-folder="${folderName}"]`).forEach(el => {
                el.classList.toggle('active', isFav);
                el.textContent = isFav ? '★' : '☆';
            });
            // 静默刷新收藏和最近阅读列表数据
            const state = await bridge.call('manga_get_state');
            this.recentGrid.render(state.recent);
            this.favGrid.render(state.favorites);
        } catch (e) {
            console.error('收藏失败', e);
        }
    }

    showContextMenu(x, y, manga) {
        this.contextMenu.items = manga.is_fav 
            ? [{ label: '💔 取消收藏', action: 'unfav' }]
            : [{ label: '⭐ 收藏', action: 'fav' }];
        this.contextMenu.show(x, y, manga);
    }

    async handleContextAction(action) {
        const manga = this.contextMenu.getTargetData();
        if (!manga) return;
        if (action === 'fav' || action === 'unfav') {
            await this.toggleFav(manga.folder_name);
        }
    }

    // ====== 视图层级切换 ======
    showHome() {
        this.currentViewLevel = 'home';
        document.getElementById('manga-home').style.display = 'block';
        document.getElementById('manga-detail-view').style.display = 'none';
        this.loadHomeData(); // 返回首页时刷新数据
    }

    showChapters() {
        this.currentViewLevel = 'chapters';
        document.getElementById('manga-home').style.display = 'none';
        document.getElementById('manga-detail-view').style.display = 'block';
        const contentGrid = document.getElementById('detail-content-grid');
        contentGrid.innerHTML = '';
        
        const chapterGrid = createCardGrid(contentGrid, {
            cardRenderer: (ch) => ({
                image: (bridge.FILE_SERVER || '') + ch.cover_url,
                title: ch.name, subtitle: '', badge: ''
            }),
            onClick: (ch) => this.showImages(this.currentFolderName, ch.path)
        });
        chapterGrid.render(this.currentDetail.chapters);
        // 多章节根目录的返回按钮 -> 回首页
        document.getElementById('detail-back').onclick = () => this.showHome();
    }

    async showImages(folderName, chapterPath) {
        this.currentViewLevel = 'images';
        document.getElementById('manga-home').style.display = 'none';
        document.getElementById('manga-detail-view').style.display = 'block';
        // 返回按钮逻辑：多章节的图片页返回章节列表，单章节返回首页
        document.getElementById('detail-back').onclick = () => {
            if (this.isMultiChapter) {
                this.showChapters();
            } else {
                this.showHome();
            }
        };
        const contentGrid = document.getElementById('detail-content-grid');
        contentGrid.innerHTML = '<div class="loading">图片加载中...</div>';
        
        try {
            const pages = await bridge.call('manga_get_pages', folderName, chapterPath);
            contentGrid.innerHTML = '';
            if (pages.length === 0) {
                contentGrid.innerHTML = '<div class="empty-state">无图片</div>';
                return;
            }
            bridge.call('manga_update_recent', folderName, 0);
            const wrappedPages = pages.map(url => ({ url: url }));
            const imageGrid = createCardGrid(contentGrid, {
                cardRenderer: (item) => ({
                    image: (bridge.FILE_SERVER || '') + item.url,
                    title: '', subtitle: '', badge: ''
                }),
                onClick: (item, index) => {
                    if (!window.mangaReader) window.mangaReader = new MangaReader();
                    window.mangaReader.open(pages, index);
                }
            });
            imageGrid.render(wrappedPages);
        } catch (e) {
            contentGrid.innerHTML = '<div class="empty-state">加载失败</div>';
            console.error('加载图片列表失败', e);
        }
    }

    // ====== 入口 ======
    async openMangaDetail(folderName) {
        try {
            const detail = await bridge.call('manga_get_detail', folderName);
            if (!detail.folder_name) return;
            
            this.currentFolderName = folderName;
            this.currentDetail = detail;
            this.isMultiChapter = detail.is_multi_chapter;
            // 【核心修复】：统一在这里更新头部信息，无论单章节还是多章节
            document.getElementById('detail-title').textContent = detail.title;
            document.getElementById('detail-author').textContent = detail.author;
            const favBtn = document.getElementById('detail-fav-btn');
            favBtn.className = `btn btn-fav ${detail.is_fav ? 'is-fav' : ''}`;
            favBtn.textContent = detail.is_fav ? '★ 已收藏' : '☆ 收藏';
            favBtn.onclick = async () => {
                const isFav = await bridge.call('manga_toggle_favorite', folderName);
                favBtn.className = `btn btn-fav ${isFav ? 'is-fav' : ''}`;
                favBtn.textContent = isFav ? '★ 已收藏' : '☆ 收藏';
                document.querySelectorAll(`.fav-star[data-folder="${folderName}"]`).forEach(el => {
                    el.classList.toggle('active', isFav);
                    el.textContent = isFav ? '★' : '☆';
                });
            };
            this.renderDetailInfo(detail.info);
            // 根据类型决定渲染内容区
            if (this.isMultiChapter) {
                this.showChapters();
            } else {
                this.showImages(folderName, "");
            }
        } catch (e) {
            console.error('打开漫画详情失败', e);
        }
    }

    renderDetailInfo(info) {
        const box = document.getElementById('detail-info-box');
        box.innerHTML = '';
        if (!info || Object.keys(info).length === 0) return;
        
        const addItem = (label, value) => {
            if (!value) return;
            const span = document.createElement('span');
            span.className = 'info-item';
            span.textContent = `${label}: ${value}`;
            box.appendChild(span);
        };

        addItem('ID', info.album_id);
        addItem('原名', info.oname);
        addItem('下载时间', info.download_time);
        if (info.actors && info.actors.length > 0) addItem('演员', info.actors.join(', '));

        if (info.tags && info.tags.length > 0) {
            const tagContainer = document.createElement('div');
            tagContainer.style.marginTop = '8px';
            info.tags.forEach(t => {
                const tagSpan = document.createElement('span');
                tagSpan.className = 'info-tag';
                tagSpan.textContent = t;
                tagContainer.appendChild(tagSpan);
            });
            box.appendChild(tagContainer);
        }
    }

    bindSearch() {
        const input = document.getElementById('manga-search');
        input.addEventListener('input', debounce(async (e) => {
            const keyword = e.target.value.trim();
            try {
                const data = await bridge.call('manga_search', keyword);
                this.mangaGrid.render(data);
                this.showHome(); // 搜索时切回首页
                document.getElementById('section-all').scrollIntoView({ behavior: 'smooth' });
            } catch (e) {
                console.error('搜索失败', e);
            }
        }, 300));
    }
}