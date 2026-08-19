class NovelReader {
    constructor() {
        this.novels = [];
        this.currentNovel = null;
        this.currentChapterIndex = 0;
        this.chapters = [];

        this.fontSize = 16;
        this.lineHeight = 1.8;
        this.letterSpacing = 0;
        this.theme = 'light';
        this.bgColor = '#ffffff';
        this.textColor = '#1a1a1a';
        this.encoding = 'auto';

        this._isLoading = false;
        this._isReaderMode = false;
        this._sidebarMode = 'shelf';

        // 无限滚动状态
        this.loadedChapterStart = -1;
        this.loadedChapterEnd = -1;
        this._lastScrollTop = 0;
        this._scrollDirection = 'down';

        this._progressVersion = 0;
        this._lastSavedChapter = -1;
        this._lastSavedPosition = -1;

        this._dom = {};
        this.settings = null;
    }

    async init() {
        this._cacheDom();
        this._bindEvents();
        this._bindSettingsButton();
        this.settings = new ReaderSettingsStore(this);
        this.settings.load();
        await this._loadNovels();
    }

    _cacheDom() {
        this._dom = {
            contentArea: document.getElementById('novel-content-area'),
            chapterTitle: document.getElementById('novel-chapter-title'),
            progressFill: document.getElementById('novel-progress-fill'),
            backBtn: document.getElementById('novel-back'),
            prevBtn: document.getElementById('novel-prev'),
            nextBtn: document.getElementById('novel-next'),
            chapterBtn: document.getElementById('novel-chapter-btn'),
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
        if (btn) {
            btn.addEventListener('click', () => {
                openSettingsModal({ title: '小说阅读设置' });
            });
        }
    }

    _bindEvents() {
        if (this._dom.searchInput) {
            this._dom.searchInput.addEventListener('input',
                Utils.debounce((e) => this._search(e.target.value), 300));
        }
        if (this._dom.backBtn) {
            this._dom.backBtn.addEventListener('click', () => this._exitReader());
        }
        if (this._dom.prevBtn) {
            this._dom.prevBtn.addEventListener('click', () => this._prevChapter());
        }
        if (this._dom.nextBtn) {
            this._dom.nextBtn.addEventListener('click', () => this._nextChapter());
        }
        if (this._dom.chapterBtn) {
            this._dom.chapterBtn.addEventListener('click', () => this._switchSidebar('chapters'));
        }
        if (this._dom.shelfToggle) {
            this._dom.shelfToggle.addEventListener('click', () => this._switchSidebar('shelf'));
        }
        if (this._dom.chapterToggle) {
            this._dom.chapterToggle.addEventListener('click', () => this._switchSidebar('chapters'));
        }
        if (this._dom.fontToggle) {
            this._dom.fontToggle.addEventListener('click', () => this._toggleReaderSettings());
        }
        this._bindSettingsEvents();
        this._bindKeyboard();

        if (this._dom.contentArea) {
            let scrollTimer = null;
            this._dom.contentArea.addEventListener('scroll', () => {
                const { scrollTop, scrollHeight, clientHeight } = this._dom.contentArea;
                this._scrollDirection = scrollTop > this._lastScrollTop ? 'down' : 'up';
                this._lastScrollTop = scrollTop;

                if (this._scrollDirection === 'down' && scrollHeight > clientHeight) {
                    const scrollPercent = scrollTop / (scrollHeight - clientHeight);
                    if (scrollPercent > 0.85 && !this._isLoading && this.loadedChapterEnd < this.chapters.length - 1) {
                        this._autoLoadNextChapter();
                    }
                }

                if (this._scrollDirection === 'up' && scrollTop < 100 && !this._isLoading && this.loadedChapterStart > 0) {
                    this._autoLoadPrevChapter();
                }

                clearTimeout(scrollTimer);
                scrollTimer = setTimeout(() => this._autoSaveProgress(), 2000);
            });
        }

        window.addEventListener('beforeunload', () => this._saveProgress());
    }

    _bindSettingsEvents() {
        if (this._dom.fontSizeSlider) {
            this._dom.fontSizeSlider.addEventListener('input', (e) => {
                this.fontSize = parseInt(e.target.value);
                if (this._dom.fontSizeValue) this._dom.fontSizeValue.textContent = this.fontSize;
                this.settings.apply();
            });
        }
        if (this._dom.lineHeightSlider) {
            this._dom.lineHeightSlider.addEventListener('input', (e) => {
                this.lineHeight = parseFloat(e.target.value);
                if (this._dom.lineHeightValue) this._dom.lineHeightValue.textContent = this.lineHeight.toFixed(1);
                this.settings.apply();
            });
        }
        if (this._dom.letterSpacingSlider) {
            this._dom.letterSpacingSlider.addEventListener('input', (e) => {
                this.letterSpacing = parseFloat(e.target.value);
                if (this._dom.letterSpacingValue) this._dom.letterSpacingValue.textContent = `${this.letterSpacing}px`;
                this.settings.apply();
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
            this._dom.encodingSelect.addEventListener('change', (e) => {
                this.encoding = e.target.value;
                if (this.currentNovel && this._isReaderMode) {
                    this._resetAndLoadChapter();
                }
            });
        }
    }

    _bindKeyboard() {
        document.addEventListener('keydown', (e) => {
            if (!this._isReaderMode) return;
            switch(e.key) {
                case 'ArrowLeft':
                    e.preventDefault();
                    this._prevChapter();
                    break;
                case 'ArrowRight':
                    e.preventDefault();
                    this._nextChapter();
                    break;
                case 'ArrowUp':
                    e.preventDefault();
                    if (this._dom.contentArea) {
                        this._dom.contentArea.scrollBy({
                            top: -this._dom.contentArea.clientHeight * 0.8,
                            behavior: 'smooth'
                        });
                    }
                    break;
                case 'ArrowDown':
                    e.preventDefault();
                    if (this._dom.contentArea) {
                        this._dom.contentArea.scrollBy({
                            top: this._dom.contentArea.clientHeight * 0.8,
                            behavior: 'smooth'
                        });
                    }
                    break;
                case 'Escape':
                    e.preventDefault();
                    this._exitReader();
                    break;
                case ' ':
                    e.preventDefault();
                    if (this._dom.contentArea) {
                        this._dom.contentArea.scrollBy({
                            top: this._dom.contentArea.clientHeight * 0.8,
                            behavior: 'smooth'
                        });
                    }
                    break;
            }
        });
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

    async _exitReader() {
        await this._saveProgress();
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

        if (novels.length === 0) {
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

        container.querySelectorAll('.novel-shelf-item').forEach(item => {
            item.addEventListener('click', () => {
                this._openNovel(item.dataset.id);
            });
        });
    }

    async _openNovel(novelId) {
        try {
            this._showLoading(true);
            this.currentNovel = this.novels.find(n => n.id === novelId);

            if (this.currentNovel && this.currentNovel.encoding) {
                this.encoding = this.currentNovel.encoding;
                if (this._dom.encodingSelect) this._dom.encodingSelect.value = this.encoding;
            }

            const result = await Bridge.call('novel_get_chapters', novelId, this.encoding);
            this.chapters = result.chapters || [];

            if (this.currentNovel) {
                const savedChapter = this.currentNovel.last_read_chapter || 0;
                this.currentChapterIndex = Math.max(0, Math.min(savedChapter, this.chapters.length - 1));
                this._progressVersion = this.currentNovel.version || 0;
            } else {
                this.currentChapterIndex = 0;
            }

            this._isReaderMode = true;
            this._setToolbarMode('reader');
            this._switchSidebar('chapters');
            this._renderShelf();
            this._renderChapterList();
            this.loadedChapterStart = this.currentChapterIndex;
            this.loadedChapterEnd = this.currentChapterIndex;
            if (this._dom.contentArea) this._dom.contentArea.innerHTML = '';
            await this._loadChapter();
            this._showLoading(false);
        } catch (e) {
            console.error('打开小说失败:', e);
            this._showLoading(false);
        }
    }

    async _loadChapter() {
        if (this._isLoading) return;
        this._isLoading = true;

        if (!this.currentNovel || this.chapters.length === 0) {
            this._isLoading = false;
            return;
        }

        try {
            const result = await Bridge.call(
                'novel_get_content',
                this.currentNovel.id,
                this.currentChapterIndex,
                this.encoding
            );

            if (result.error) {
                console.error(result.error);
                this._isLoading = false;
                return;
            }

            const chapter = this.chapters[this.currentChapterIndex];
            if (this._dom.chapterTitle) {
                this._dom.chapterTitle.textContent = chapter ? chapter.title : '未知章节';
            }

            if (this._dom.contentArea) {
                this._dom.contentArea.innerHTML = `
                    <div class="chapter-content" data-chapter-index="${chapter.index}">
                        ${this._formatContent(result.content)}
                    </div>
                `;
            }

            this._renderChapterList();
            this._updateProgressBar();
            this._updateNavButtons();
            this._isLoading = false;

        } catch (e) {
            console.error('加载章节失败:', e);
            this._isLoading = false;
        }
    }

    async _autoLoadNextChapter() {
        this._isLoading = true;
        const nextIndex = this.loadedChapterEnd + 1;

        try {
            const result = await Bridge.call(
                'novel_get_content',
                this.currentNovel.id,
                nextIndex,
                this.encoding
            );

            if (!result.error) {
                const chapter = this.chapters[nextIndex];
                this._appendChapterContent(result.content, chapter);
                this.loadedChapterEnd = nextIndex;
                this.currentChapterIndex = nextIndex;
                this._renderChapterList();
                this._updateProgressBar();
                this._updateNavButtons();
            }
        } catch (e) {
            console.error('自动加载下一章失败:', e);
        }

        this._isLoading = false;
    }

    async _autoLoadPrevChapter() {
        this._isLoading = true;
        const prevIndex = this.loadedChapterStart - 1;

        try {
            const result = await Bridge.call(
                'novel_get_content',
                this.currentNovel.id,
                prevIndex,
                this.encoding
            );

            if (!result.error) {
                const chapter = this.chapters[prevIndex];
                const prevHeight = this._dom.contentArea.scrollHeight;

                this._prependChapterContent(result.content, chapter);
                this.loadedChapterStart = prevIndex;
                this.currentChapterIndex = prevIndex;

                // 关键：保持滚动位置不跳动
                const newHeight = this._dom.contentArea.scrollHeight;
                this._dom.contentArea.scrollTop += (newHeight - prevHeight);

                this._renderChapterList();
                this._updateProgressBar();
                this._updateNavButtons();
            }
        } catch (e) {
            console.error('自动加载上一章失败:', e);
        }

        this._isLoading = false;
    }

    _appendChapterContent(content, chapter) {
        const contentArea = this._dom.contentArea;
        if (!contentArea) return;

        const separator = document.createElement('div');
        separator.className = 'chapter-separator';
        separator.style.height = '2em';
        contentArea.appendChild(separator);

        const contentDiv = document.createElement('div');
        contentDiv.className = 'chapter-content';
        contentDiv.dataset.chapterIndex = chapter.index;
        contentDiv.innerHTML = this._formatContent(content);
        contentArea.appendChild(contentDiv);
    }

    _prependChapterContent(content, chapter) {
        const contentArea = this._dom.contentArea;
        if (!contentArea) return;

        const fragment = document.createDocumentFragment();

        const contentDiv = document.createElement('div');
        contentDiv.className = 'chapter-content';
        contentDiv.dataset.chapterIndex = chapter.index;
        contentDiv.innerHTML = this._formatContent(content);
        fragment.appendChild(contentDiv);

        const separator = document.createElement('div');
        separator.className = 'chapter-separator';
        separator.style.height = '2em';
        fragment.appendChild(separator);

        contentArea.insertBefore(fragment, contentArea.firstChild);
    }

    async _resetAndLoadChapter() {
        this._saveProgress();
        this.loadedChapterStart = this.currentChapterIndex;
        this.loadedChapterEnd = this.currentChapterIndex;
        if (this._dom.contentArea) this._dom.contentArea.innerHTML = '';
        await this._loadChapter();
        setTimeout(() => {
            if (this._dom.contentArea) this._dom.contentArea.scrollTop = 0;
        }, 100);
    }

    _prevChapter() {
        if (this.currentChapterIndex > 0) {
            this.currentChapterIndex--;
            this._resetAndLoadChapter();
        }
    }

    _nextChapter() {
        if (this.currentChapterIndex < this.chapters.length - 1) {
            this.currentChapterIndex++;
            this._resetAndLoadChapter();
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
        } else {
            if (loadingEl) loadingEl.style.display = 'none';
        }
    }

    _formatContent(text) {
        return NovelUtils.formatContent(text);
    }

    _updateProgressBar() {
        if (this._dom.progressFill && this.chapters.length > 0) {
            const progress = (this.currentChapterIndex + 1) / this.chapters.length;
            this._dom.progressFill.style.width = `${progress * 100}%`;
        }
    }

    _updateNavButtons() {
        if (this._dom.prevBtn) {
            const isFirst = this.currentChapterIndex <= 0;
            this._dom.prevBtn.disabled = isFirst;
            this._dom.prevBtn.style.opacity = isFirst ? '0.5' : '1';
            this._dom.prevBtn.style.cursor = isFirst ? 'not-allowed' : 'pointer';
        }
        if (this._dom.nextBtn) {
            const isLast = this.currentChapterIndex >= this.chapters.length - 1;
            this._dom.nextBtn.disabled = isLast;
            this._dom.nextBtn.style.opacity = isLast ? '0.5' : '1';
            this._dom.nextBtn.style.cursor = isLast ? 'not-allowed' : 'pointer';
        }
    }

    _renderChapterList() {
        const list = this._dom.chapterList;
        if (!list) return;
        list.innerHTML = this.chapters.map((chapter, index) => `
            <div class="novel-chapter-item ${index === this.currentChapterIndex ? 'active' : ''}"
                 data-index="${index}">
                <span>${Utils.escapeHtml(chapter.title)}</span>
                <span class="chapter-words">${chapter.word_count}字</span>
            </div>
        `).join('');
        list.querySelectorAll('.novel-chapter-item').forEach(item => {
            item.addEventListener('click', () => {
                const index = parseInt(item.dataset.index);
                this.currentChapterIndex = index;
                this._resetAndLoadChapter();
            });
        });
        const active = list.querySelector('.novel-chapter-item.active');
        if (active) active.scrollIntoView({ block: 'nearest' });
    }

    _getCurrentChapterIndexFromScroll() {
        if (!this._dom.contentArea) return this.currentChapterIndex;
        const contentArea = this._dom.contentArea;
        const viewCenter = contentArea.scrollTop + contentArea.clientHeight / 2;
        const chapterDivs = contentArea.querySelectorAll('.chapter-content');
        for (let div of chapterDivs) {
            const top = div.offsetTop;
            const bottom = top + div.offsetHeight;
            if (viewCenter >= top && viewCenter <= bottom) {
                return parseInt(div.dataset.chapterIndex);
            }
        }
        return this.currentChapterIndex;
    }

    async _autoSaveProgress() {
        if (!this.currentNovel) return;
        const contentArea = this._dom.contentArea;
        if (!contentArea) return;
        this.currentChapterIndex = this._getCurrentChapterIndexFromScroll();
        const scrollHeight = contentArea.scrollHeight - contentArea.clientHeight;
        const scrollPosition = scrollHeight > 0 ? contentArea.scrollTop / scrollHeight : 0;
        if (this.currentChapterIndex === this._lastSavedChapter &&
            Math.abs(scrollPosition - this._lastSavedPosition) < 0.01) return;
        await this._saveProgress(scrollPosition);
    }

    async _saveProgress(scrollPosition) {
        if (!this.currentNovel) return;
        const position = scrollPosition || 0;
        try {
            const result = await Bridge.call(
                'novel_update_progress',
                this.currentNovel.id,
                this.currentChapterIndex,
                position,
                this.encoding
            );
            if (result.success) {
                this._progressVersion = result.version;
                this._lastSavedChapter = this.currentChapterIndex;
                this._lastSavedPosition = position;
            }
        } catch (e) {
            console.error('保存进度失败:', e);
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
        this._saveProgress();
        this.currentNovel = null;
        this.chapters = [];
        this._isReaderMode = false;
    }
}
