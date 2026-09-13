// ============================================================
// 小说阅读器 — UI 控制器
// 分页 / 测量 / 翻页逻辑位于 reader-engine.js。
// ============================================================
class NovelReader {
    constructor() {
        this.novels = [];
        this.currentNovel = null;

        this.fontSize = 16;
        this.lineHeight = 1.8;
        this.letterSpacing = 0;
        this.theme = 'light';
        this.bgColor = '#ffffff';
        this.textColor = '#1a1a1a';
        this.encoding = 'auto';
        this.mode = 'page'; // 'page' | 'scroll'

        this._isReaderMode = false;
        this._sidebarMode = 'shelf';
        this._lastSavedChapter = -1;
        this._lastSavedPosition = -1;

        this.engine = new NovelReaderEngine(this);
        this._chapterListBuiltFor = null;
        this._dom = {};
        this.settings = null;
    }

    async init() {
        this._cacheDom();
        this._bindEvents();
        this._bindSettingsButton();
        this.settings = new ReaderSettingsStore(this);
        this.settings.load();
        this.engine.setMode(this.mode);
        await this._loadNovels();

        let resizeTimer = null;
        let savedScrollTop = 0;
        let resizing = false;
        window.addEventListener('resize', () => {
            if (!this._isReaderMode || !this._dom.contentArea) return;
            if (!resizing) {
                resizing = true;
                savedScrollTop = this._dom.contentArea.scrollTop;
                this._dom.contentArea.classList.add('resizing');
            }
            clearTimeout(resizeTimer);
            resizeTimer = setTimeout(() => {
                resizing = false;
                this._dom.contentArea.classList.remove('resizing');
                requestAnimationFrame(() => {
                    this._dom.contentArea.scrollTop = savedScrollTop;
                    this._updateProgressBar();
                });
            }, 220);
        });
    }

    _cacheDom() {
        this._dom = {
            contentArea: document.getElementById('novel-content-area'),
            chapterTitle: document.getElementById('novel-chapter-title'),
            progressFill: document.getElementById('novel-progress-fill'),
            modeSelect: document.getElementById('novel-mode-select'),
            searchInput: document.getElementById('novel-search'),
            browseGroup: document.getElementById('novel-browse-group'),
            readerNav: document.getElementById('novel-reader-nav'),
            readerSettingsBar: document.getElementById('novel-reader-settings-bar'),
            fontToggle: document.getElementById('novel-font-toggle'),
            shelfPanel: document.getElementById('novel-shelf-panel'),
            chapterPanel: document.getElementById('novel-chapter-panel'),
            shelfToggle: document.getElementById('sidebar-toggle-shelf'),
            chapterToggle: document.getElementById('sidebar-toggle-chapters'),
            shelfList: document.getElementById('novel-shelf-list'),
            chapterList: document.getElementById('novel-chapter-list'),
            fontSizeSlider: document.getElementById('novel-font-size'),
            fontSizeValue: document.getElementById('novel-font-size-value'),
            lineHeightSlider: document.getElementById('novel-line-height'),
            lineHeightValue: document.getElementById('novel-line-height-value'),
            letterSpacingSlider: document.getElementById('novel-letter-spacing'),
            letterSpacingValue: document.getElementById('novel-letter-spacing-value'),
            themeSelect: document.getElementById('novel-theme-select'),
            bgColorInput: document.getElementById('novel-bg-color'),
            textColorInput: document.getElementById('novel-text-color'),
            customColorLabel: document.getElementById('custom-color-label'),
            customTextLabel: document.getElementById('custom-text-label'),
            encodingSelect: document.getElementById('novel-encoding'),
        };
    }

    _bindSettingsButton() {
        const btn = document.getElementById('btn-settings');
        if (btn) btn.addEventListener('click', () => openSettingsModal({ title: '小说阅读设置' }));
    }

    _bindEvents() {
        if (this._dom.searchInput) {
            this._dom.searchInput.addEventListener('input',
                Utils.debounce((e) => this._search(e.target.value), 300));
        }
        if (this._dom.modeSelect) {
            this._dom.modeSelect.addEventListener('change', (e) => {
                this.mode = e.target.value === 'scroll' ? 'scroll' : 'page';
                this.engine.setMode(this.mode);
                this.settings.save();
                Toast.info(this.mode === 'scroll' ? '已切换到连续滚动模式' : '已切换到翻页模式');
            });
        }

        // 滚动模式：轻量节流地让引擎处理连续加载
        let lastScroll = 0;
        if (this._dom.contentArea) {
            this._dom.contentArea.addEventListener('scroll', () => {
                const now = performance.now();
                if (now - lastScroll < 80) return;
                lastScroll = now;
                this.engine.handleScroll();
                this._scheduleProgressSave();
            }, { passive: true });
        }
        if (this._dom.shelfToggle) this._dom.shelfToggle.addEventListener('click', () => this._switchSidebar('shelf'));
        if (this._dom.chapterToggle) this._dom.chapterToggle.addEventListener('click', () => this._switchSidebar('chapters'));
        if (this._dom.fontToggle) this._dom.fontToggle.addEventListener('click', () => this._toggleReaderSettings());

        if (this._dom.contentArea) {
            this._dom.contentArea.addEventListener('click', (e) => {
                if (!this._isReaderMode) return;
                const rect = this._dom.contentArea.getBoundingClientRect();
                const x = e.clientX - rect.left;
                if (x < rect.width * 0.25) this._turnPage(-1);
                else if (x > rect.width * 0.75) this._turnPage(1);
            });
        }

        this._bindSettingsEvents();
        this._bindKeyboard();
        window.addEventListener('beforeunload', () => this._saveCurrentProgress(true));
    }

    _bindSettingsEvents() {
        if (this._dom.fontSizeSlider) {
            this._dom.fontSizeSlider.addEventListener('input', (e) => {
                this.fontSize = parseInt(e.target.value);
                if (this._dom.fontSizeValue) this._dom.fontSizeValue.textContent = this.fontSize;
                this.settings.apply();
                this._updateProgressBar();
            });
        }
        if (this._dom.lineHeightSlider) {
            this._dom.lineHeightSlider.addEventListener('input', (e) => {
                this.lineHeight = parseFloat(e.target.value);
                if (this._dom.lineHeightValue) this._dom.lineHeightValue.textContent = this.lineHeight.toFixed(1);
                this.settings.apply();
                this._updateProgressBar();
            });
        }
        if (this._dom.letterSpacingSlider) {
            this._dom.letterSpacingSlider.addEventListener('input', (e) => {
                this.letterSpacing = parseFloat(e.target.value);
                if (this._dom.letterSpacingValue) this._dom.letterSpacingValue.textContent = `${this.letterSpacing}px`;
                this.settings.apply();
                this._updateProgressBar();
            });
        }
        if (this._dom.themeSelect) {
            this._dom.themeSelect.addEventListener('change', (e) => {
                this.theme = e.target.value;
                const isCustom = this.theme === 'custom';
                if (this._dom.customColorLabel) this._dom.customColorLabel.style.display = isCustom ? 'inline-flex' : 'none';
                if (this._dom.customTextLabel) this._dom.customTextLabel.style.display = isCustom ? 'inline-flex' : 'none';
                this.settings.apply();
            });
        }
        if (this._dom.bgColorInput) {
            this._dom.bgColorInput.addEventListener('change', (e) => {
                this.bgColor = e.target.value;
                this.settings.apply();
            });
        }
        if (this._dom.textColorInput) {
            this._dom.textColorInput.addEventListener('change', (e) => {
                this.textColor = e.target.value;
                this.settings.apply();
            });
        }
        if (this._dom.encodingSelect) {
            this._dom.encodingSelect.addEventListener('change', () => {
                this.encoding = e.target.value;
                if (this.currentNovel && this._isReaderMode) this._reloadNovel();
            });
        }
    }

    _bindKeyboard() {
        document.addEventListener('keydown', (e) => {
            if (!this._isReaderMode) return;
            switch (e.key) {
                case 'ArrowUp':
                case 'PageUp':
                    e.preventDefault();
                    this._scrollArea(-0.8);
                    break;
                case 'ArrowDown':
                case 'PageDown':
                case ' ':
                    e.preventDefault();
                    this._scrollArea(0.8);
                    break;
                case 'Escape':
                    e.preventDefault();
                    this._exitReader();
                    break;
            }
        });
    }

    _scrollArea(factor) {
        const el = this._dom.contentArea;
        if (el) el.scrollBy({ top: el.clientHeight * factor, behavior: 'smooth' });
    }

    _switchSidebar(mode) {
        this._sidebarMode = mode;
        const isShelf = mode === 'shelf';
        if (this._dom.shelfPanel) this._dom.shelfPanel.style.display = isShelf ? 'flex' : 'none';
        if (this._dom.chapterPanel) this._dom.chapterPanel.style.display = isShelf ? 'none' : 'flex';
        if (this._dom.shelfToggle) this._dom.shelfToggle.classList.toggle('active', isShelf);
        if (this._dom.chapterToggle) this._dom.chapterToggle.classList.toggle('active', !isShelf);
    }

    _setToolbarMode(mode) {
        const isReader = mode === 'reader';
        if (this._dom.browseGroup) this._dom.browseGroup.style.display = isReader ? 'none' : 'flex';
        if (this._dom.readerNav) this._dom.readerNav.style.display = isReader ? 'flex' : 'none';
        if (this._dom.fontToggle) this._dom.fontToggle.style.display = isReader ? 'inline-flex' : 'none';
        if (!isReader && this._dom.readerSettingsBar) this._dom.readerSettingsBar.style.display = 'none';
    }

    _toggleReaderSettings() {
        if (!this._dom.readerSettingsBar) return;
        const show = this._dom.readerSettingsBar.style.display !== 'flex';
        this._dom.readerSettingsBar.style.display = show ? 'flex' : 'none';
    }

    async _turnPage(dir) {
        if (!this._isReaderMode) return;
        const result = dir > 0 ? await this.engine.nextPage() : await this.engine.prevPage();
        if (result === 'end') {
            Toast.info(dir > 0 ? '已经是最后一页了' : '已经是第一页了');
            return;
        }
        if (result === 'moved') this._afterPageRender(false);
    }

    async _exitReader() {
        this._saveCurrentProgress(true);
        this._isReaderMode = false;
        this._setToolbarMode('browse');
        this._switchSidebar('shelf');
        if (this._dom.chapterTitle) this._dom.chapterTitle.textContent = '未打开小说';
        if (this._dom.progressFill) this._dom.progressFill.style.width = '0%';
        if (this._dom.contentArea) {
            this._dom.contentArea.innerHTML = `
                <div class="novel-empty">
                    <div class="novel-empty-icon">📖</div>
                    <div class="novel-empty-text">在左侧书架选择一本小说开始阅读</div>
                </div>
            `;
        }
        this._renderShelf();
    }

    async _loadNovels() {
        try {
            const result = await Bridge.call('novel_list');
            this.novels = result.novels || [];
            this._renderShelf();
        } catch (e) {
            console.error('加载小说列表失败:', e);
        }
    }

    _renderShelf(filteredNovels) {
        const container = this._dom.shelfList;
        if (!container) return;
        const novels = filteredNovels || this.novels;
        if (!novels.length) {
            container.innerHTML = `
                <div class="novel-empty">
                    <div class="novel-empty-icon">📖</div>
                    <div class="novel-empty-text">没有找到小说</div>
                    <div class="novel-empty-hint">请将 .txt 文件放入小说目录</div>
                </div>
            `;
            return;
        }
        container.innerHTML = novels.map(novel => `
            <div class="novel-shelf-item ${this.currentNovel && novel.id === this.currentNovel.id ? 'active' : ''}" data-id="${novel.id}">
                <div class="novel-shelf-title">${Utils.escapeHtml(novel.title)}</div>
                <div class="novel-shelf-author">${Utils.escapeHtml(novel.author)}</div>
                <div class="novel-shelf-meta">
                    <span>${novel.chapter_count || '?'} 章</span>
                    <span class="novel-shelf-progress">${Math.round((novel.progress || 0) * 100)}%</span>
                </div>
            </div>
        `).join('');
        if (window.Motion) Motion.stagger(container, '.novel-shelf-item');
        container.querySelectorAll('.novel-shelf-item').forEach(item => {
            item.addEventListener('click', () => this._openNovel(item.dataset.id));
        });
    }

    async _openNovel(novelId, startChapter = null, fraction = 0) {
        try {
            this._showLoading(true);
            this.currentNovel = this.novels.find(n => n.id === novelId);
            if (!this.currentNovel) return;
            if (this.currentNovel.encoding) {
                this.encoding = this.currentNovel.encoding;
                if (this._dom.encodingSelect) this._dom.encodingSelect.value = this.encoding;
            }

            const result = await Bridge.call('novel_get_chapters', novelId, this.encoding);
            const chapters = result.chapters || [];
            this.engine.setMode(this.mode);
            this.engine.reset(novelId, chapters, this.encoding);

            this._isReaderMode = true;
            this._setToolbarMode('reader');
            this._switchSidebar('chapters');
            this._renderShelf();
            this._chapterListBuiltFor = null;
            this._renderChapterList();

            const saved = startChapter !== null && startChapter !== undefined
                ? startChapter
                : (this.currentNovel.last_read_chapter || 0);
            const savedFraction = startChapter !== null && startChapter !== undefined
                ? fraction
                : (Number(this.currentNovel.scroll_position) || 0);

            await this.engine.goToChapter(
                Math.max(0, Math.min(saved, chapters.length - 1)), savedFraction);
            this._afterPageRender(true);
            this._showLoading(false);
        } catch (e) {
            console.error('打开小说失败:', e);
            this._showLoading(false);
        }
    }

    async _reloadNovel() {
        if (!this.currentNovel) return;
        const chapter = this.engine.currentChapterIndex;
        const fraction = this.engine.chapterFraction();
        await this._saveCurrentProgress(true);
        await this._openNovel(this.currentNovel.id, chapter, fraction);
    }

    _scheduleProgressSave() {
        clearTimeout(this._progressTimer);
        this._progressTimer = setTimeout(() => this._saveCurrentProgress(false), 900);
    }

    _afterPageRender(scrollList = false) {
        this._updateChapterListActive(scrollList);
        this._updateChapterTitle();
        this._updateProgressBar();
    }

    _updateChapterTitle() {
        const chapter = this.engine.chapters[this.engine.currentChapterIndex];
        if (this._dom.chapterTitle && chapter) {
            this._dom.chapterTitle.textContent = chapter.title;
        }
    }

    _updateProgressBar() {
        const fill = this._dom.progressFill;
        const chapters = this.engine.chapters;
        if (!fill || !chapters.length) return;
        const progress = (this.engine.currentChapterIndex + this.engine.chapterFraction()) / chapters.length;
        fill.style.width = `${(progress * 100).toFixed(2)}%`;
    }

    async _saveCurrentProgress(force = false) {
        if (!this.currentNovel || !this._isReaderMode) return;
        const chapter = this.engine.currentChapterIndex;
        const position = this.engine.chapterFraction();
        if (!force && chapter === this._lastSavedChapter
            && Math.abs(position - this._lastSavedPosition) < 0.001) return;
        try {
            const result = await Bridge.call(
                'novel_update_progress',
                this.currentNovel.id,
                chapter,
                position,
                this.encoding
            );
            if (result.success) {
                this._lastSavedChapter = chapter;
                this._lastSavedPosition = position;
            }
        } catch (e) {
            console.error('保存进度失败:', e);
        }
    }

    _renderChapterList() {
        const list = this._dom.chapterList;
        const chapters = this.engine.chapters;
        if (!list || !this.currentNovel) return;
        if (this._chapterListBuiltFor !== this.currentNovel.id) {
            list.innerHTML = chapters.map((chapter, index) => `
                <div class="novel-chapter-item" data-index="${index}">
                    <span>${Utils.escapeHtml(chapter.title)}</span>
                    <span class="chapter-words">${chapter.word_count}字</span>
                </div>
            `).join('');
            if (window.Motion) Motion.stagger(list, '.novel-chapter-item');
            list.querySelectorAll('.novel-chapter-item').forEach(item => {
                item.addEventListener('click', async () => {
                    this._saveCurrentProgress(true);
                    await this.engine.goToChapter(parseInt(item.dataset.index, 10), 0);
                    this._afterPageRender(true);
                });
            });
            this._chapterListBuiltFor = this.currentNovel.id;
        }
        this._updateChapterListActive(true);
    }

    _updateChapterListActive(scrollList = false) {
        const list = this._dom.chapterList;
        if (!list) return;
        list.querySelectorAll('.novel-chapter-item').forEach(item => {
            item.classList.toggle('active',
                parseInt(item.dataset.index, 10) === this.engine.currentChapterIndex);
        });
        if (scrollList) {
            const active = list.querySelector('.novel-chapter-item.active');
            if (active) active.scrollIntoView({ block: 'nearest' });
        }
    }

    _showLoading(show) {
        let loadingEl = document.getElementById('novel-loading');
        if (show) {
            if (!loadingEl) {
                loadingEl = document.createElement('div');
                loadingEl.id = 'novel-loading';
                loadingEl.className = 'novel-loading';
                loadingEl.innerHTML = '<div class="spinner"></div><span>加载中...</span>';
                if (this._dom.contentArea) this._dom.contentArea.appendChild(loadingEl);
            }
            loadingEl.style.display = 'flex';
        } else if (loadingEl) {
            loadingEl.style.display = 'none';
        }
    }

    _search(keyword) {
        if (!keyword) {
            this._renderShelf();
            return;
        }
        const filtered = this.novels.filter(novel =>
            novel.title.includes(keyword) ||
            novel.author.includes(keyword)
        );
        this._renderShelf(filtered);
    }

    destroy() {
        this._saveCurrentProgress(true);
        this.currentNovel = null;
        this._isReaderMode = false;
    }
}
