class MangaLibrary {
    constructor() {
        this.mangaGrid = null;
        this._initialized = false;
    }

    async init() {
        if (this._initialized) return;
        this._initialized = true;

        this.mangaGrid = createCardGrid(
            document.getElementById('manga-grid'),
            {
                cardRenderer: (manga) => ({
                    image: bridge.FILE_SERVER + manga.cover_url,
                    title: manga.title,
                    subtitle: manga.author,
                    badge: this._getBadgeText(manga),
                    badgeClass: this._getBadgeClass(manga)
                }),
                onClick: (manga) => {
                    console.log('打开漫画:', manga.title);
                    // TODO: 跳转到漫画阅读视图
                }
            }
        );

        this.bindSearch();
        await this.loadData();
    }

    _getBadgeText(manga) {
        // 根据你的数据结构调整
        return `${manga.page_count}页`;
    }

    _getBadgeClass(manga) {
        return '';
    }

    bindSearch() {
        const input = document.getElementById('manga-search');
        input.addEventListener('input', debounce(async (e) => {
            const keyword = e.target.value.trim();
            if (!keyword) {
                await this.loadData();
                return;
            }
            try {
                const data = await bridge.call('manga_search', keyword);
                this.mangaGrid.render(data);
            } catch (e) {
                console.error('搜索失败', e);
            }
        }, 300));
    }

    async loadData() {
        try {
            const data = await bridge.call('manga_list');
            this.mangaGrid.render(data);
            await this.loadFilters();
        } catch (e) {
            console.error('加载漫画列表失败', e);
        }
    }

    async loadFilters() {
        try {
            const data = await bridge.call('manga_get_filters');
            this.renderFilters('tag-list', data.tags, 'tag');
            this.renderFilters('author-list', data.authors, 'author');
        } catch (e) {}
    }

    renderFilters(containerId, items, filterType) {
        const container = document.getElementById(containerId);
        if (!container) return;
        container.innerHTML = '';
        items.forEach(item => {
            const li = document.createElement('li');
            li.textContent = item;
            li.addEventListener('click', async () => {
                const data = await bridge.call('manga_filter', filterType, item);
                this.mangaGrid.render(data);
            });
            container.appendChild(li);
        });
    }
}