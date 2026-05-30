// frontend/js/views/novel-reader.js

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
        
        // 无限滚动状态
        this.loadedChapterStart = -1;
        this.loadedChapterEnd = -1;
        this._lastScrollTop = 0;
        this._scrollDirection = 'down';
        
        this._progressVersion = 0;
        this._lastSavedChapter = -1;
        this._lastSavedPosition = -1;
        
        this._dom = {};
    }

    async init() {
        console.log('NovelReader 初始化...');
        this._cacheDom();
        this._bindEvents();
        this._loadSettings();
        await this._loadNovels();
    }

    _cacheDom() {
        this._dom = {
            container: document.getElementById('view-novel-reader'),
            listView: document.getElementById('novel-list-view'),
            readerView: document.getElementById('novel-reader-view'),
            novelGrid: document.getElementById('novel-grid'),
            searchInput: document.getElementById('novel-search'),
            contentArea: document.getElementById('novel-content-area'),
            chapterTitle: document.getElementById('novel-chapter-title'),
            progressFill: document.getElementById('novel-progress-fill'),
            backBtn: document.getElementById('novel-back'),
            prevBtn: document.getElementById('novel-prev'),
            nextBtn: document.getElementById('novel-next'),
            chapterBtn: document.getElementById('novel-chapter-btn'),
            chapterSelector: document.getElementById('novel-chapter-selector'),
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

    _bindEvents() {
        if (this._dom.searchInput) {
            this._dom.searchInput.addEventListener('input', 
                debounce((e) => this._search(e.target.value), 300));
        }
        if (this._dom.backBtn) {
            this._dom.backBtn.addEventListener('click', () => this._showList());
        }
        if (this._dom.prevBtn) {
            this._dom.prevBtn.addEventListener('click', () => this._prevChapter());
        }
        if (this._dom.nextBtn) {
            this._dom.nextBtn.addEventListener('click', () => this._nextChapter());
        }
        if (this._dom.chapterBtn) {
            this._dom.chapterBtn.addEventListener('click', () => this._toggleChapterSelector());
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
                this._applySettings();
            });
        }
        if (this._dom.lineHeightSlider) {
            this._dom.lineHeightSlider.addEventListener('input', (e) => {
                this.lineHeight = parseFloat(e.target.value);
                if (this._dom.lineHeightValue) this._dom.lineHeightValue.textContent = this.lineHeight.toFixed(1);
                this._applySettings();
            });
        }
        if (this._dom.letterSpacingSlider) {
            this._dom.letterSpacingSlider.addEventListener('input', (e) => {
                this.letterSpacing = parseFloat(e.target.value);
                if (this._dom.letterSpacingValue) this._dom.letterSpacingValue.textContent = `${this.letterSpacing}px`;
                this._applySettings();
            });
        }
        if (this._dom.themeSelect) {
            this._dom.themeSelect.addEventListener('change', (e) => {
                this.theme = e.target.value;
                const isCustom = this.theme === 'custom';
                if (this._dom.customColorLabel) this._dom.customColorLabel.style.display = isCustom ? 'inline-flex' : 'none';
                if (this._dom.customTextLabel) this._dom.customTextLabel.style.display = isCustom ? 'inline-flex' : 'none';
                this._applySettings();
            });
        }
        if (this._dom.bgColorInput) {
            this._dom.bgColorInput.addEventListener('change', (e) => {
                this.bgColor = e.target.value;
                this._applySettings();
            });
        }
        if (this._dom.textColorInput) {
            this._dom.textColorInput.addEventListener('change', (e) => {
                this.textColor = e.target.value;
                this._applySettings();
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
                    this._showList();
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

    async _loadNovels() {
        try {
            const result = await bridge.novelList();
            this.novels = result.novels || [];
            this._renderGrid();
        } catch (e) {
            console.error('加载小说列表失败:', e);
        }
    }

    _renderGrid(filteredNovels) {
        const novels = filteredNovels || this.novels;
        const container = this._dom.novelGrid;
        if (!container) return;

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
            <div class="novel-card" data-id="${novel.id}">
                <div class="novel-card-title">${this._escapeHtml(novel.title)}</div>
                <div class="novel-card-author">${this._escapeHtml(novel.author)}</div>
                <div class="novel-card-meta">
                    <span>${novel.chapter_count || '?'} 章</span>
                    <span class="novel-card-progress">${Math.round(novel.progress * 100)}%</span>
                </div>
            </div>
        `).join('');

        container.querySelectorAll('.novel-card').forEach(card => {
            card.addEventListener('click', () => {
                const novelId = card.dataset.id;
                this._openNovel(novelId);
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
            
            const result = await bridge.novelGetChapters(novelId, this.encoding);
            this.chapters = result.chapters || [];
            
            if (this.currentNovel) {
                this.currentChapterIndex = this.currentNovel.last_read_chapter || 0;
                this._progressVersion = this.currentNovel.version || 0;
            } else {
                this.currentChapterIndex = 0;
            }
            
            this._showReader();
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
            const result = await bridge.novelGetContent(
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
            const result = await bridge.novelGetContent(
                this.currentNovel.id, 
                nextIndex,
                this.encoding
            );
            
            if (!result.error) {
                const chapter = this.chapters[nextIndex];
                this._appendChapterContent(result.content, chapter);
                this.loadedChapterEnd = nextIndex;
                this.currentChapterIndex = nextIndex;
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
            const result = await bridge.novelGetContent(
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

        // 简单的换行分隔，不再使用分隔符
        const separator = document.createElement('div');
        separator.className = 'chapter-separator';
        separator.style.height = '2em'; // 空行高度
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

    _showList() {
        this._saveProgress().then(() => {
            this._isReaderMode = false;
            if (this._dom.listView) this._dom.listView.style.display = 'block';
            if (this._dom.readerView) this._dom.readerView.style.display = 'none';
        });
    }

    _showReader() {
        this._isReaderMode = true;
        if (this._dom.listView) this._dom.listView.style.display = 'none';
        if (this._dom.readerView) this._dom.readerView.style.display = 'flex';
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
        if (!text) return '';
        const paragraphs = text
            .split('\n')
            .filter(line => line.trim())
            .map(line => `<p>${this._escapeHtml(line.trim())}</p>`);
        return paragraphs.join('');
    }

    _escapeHtml(text) {
        const div = document.createElement('div');
        div.textContent = text;
        return div.innerHTML;
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

    _toggleChapterSelector() {
        const selector = this._dom.chapterSelector;
        if (!selector) return;
        if (selector.classList.contains('active')) {
            selector.classList.remove('active');
        } else {
            this._renderChapterList();
            selector.classList.add('active');
            const closeHandler = (e) => {
                if (e.target === selector) {
                    selector.classList.remove('active');
                    selector.removeEventListener('click', closeHandler);
                }
            };
            setTimeout(() => selector.addEventListener('click', closeHandler), 0);
        }
    }

    _renderChapterList() {
        const list = this._dom.chapterList;
        if (!list) return;
        list.innerHTML = this.chapters.map((chapter, index) => `
            <div class="novel-chapter-item ${index === this.currentChapterIndex ? 'active' : ''}"
                 data-index="${index}">
                ${this._escapeHtml(chapter.title)}
                <span style="float:right;font-size:12px;color:var(--text-secondary);">
                    ${chapter.word_count}字
                </span>
            </div>
        `).join('');
        list.querySelectorAll('.novel-chapter-item').forEach(item => {
            item.addEventListener('click', () => {
                const index = parseInt(item.dataset.index);
                this.currentChapterIndex = index;
                if (this._dom.chapterSelector) this._dom.chapterSelector.classList.remove('active');
                this._resetAndLoadChapter();
            });
        });
    }

    _applySettings() {
        const contentArea = this._dom.contentArea;
        if (!contentArea) return;
        contentArea.style.setProperty('--reader-font-size', `${this.fontSize}px`);
        contentArea.style.setProperty('--reader-line-height', this.lineHeight);
        contentArea.style.setProperty('--reader-letter-spacing', `${this.letterSpacing}px`);
        if (this.theme === 'custom') {
            contentArea.style.setProperty('--reader-bg-color', this.bgColor);
            contentArea.style.setProperty('--reader-text-color', this.textColor);
            contentArea.className = 'novel-content-area';
        } else {
            contentArea.className = `novel-content-area theme-${this.theme}`;
        }
        this._saveSettings();
    }

    _saveSettings() {
        const settings = {
            fontSize: this.fontSize,
            lineHeight: this.lineHeight,
            letterSpacing: this.letterSpacing,
            theme: this.theme,
            bgColor: this.bgColor,
            textColor: this.textColor,
            encoding: this.encoding
        };
        localStorage.setItem('novel-reader-settings', JSON.stringify(settings));
    }

    _loadSettings() {
        try {
            const saved = localStorage.getItem('novel-reader-settings');
            if (saved) {
                const settings = JSON.parse(saved);
                this.fontSize = settings.fontSize || 16;
                this.lineHeight = settings.lineHeight || 1.8;
                this.letterSpacing = settings.letterSpacing || 0;
                this.theme = settings.theme || 'light';
                this.bgColor = settings.bgColor || '#ffffff';
                this.textColor = settings.textColor || '#1a1a1a';
                this.encoding = settings.encoding || 'auto';
            }
        } catch (e) {
            console.error('加载设置失败:', e);
        }
        if (this._dom.fontSizeSlider) this._dom.fontSizeSlider.value = this.fontSize;
        if (this._dom.fontSizeValue) this._dom.fontSizeValue.textContent = this.fontSize;
        if (this._dom.lineHeightSlider) this._dom.lineHeightSlider.value = this.lineHeight;
        if (this._dom.lineHeightValue) this._dom.lineHeightValue.textContent = this.lineHeight.toFixed(1);
        if (this._dom.letterSpacingSlider) this._dom.letterSpacingSlider.value = this.letterSpacing;
        if (this._dom.letterSpacingValue) this._dom.letterSpacingValue.textContent = `${this.letterSpacing}px`;
        if (this._dom.themeSelect) this._dom.themeSelect.value = this.theme;
        if (this._dom.bgColorInput) this._dom.bgColorInput.value = this.bgColor;
        if (this._dom.textColorInput) this._dom.textColorInput.value = this.textColor;
        if (this._dom.encodingSelect) this._dom.encodingSelect.value = this.encoding;
        const isCustom = this.theme === 'custom';
        if (this._dom.customColorLabel) this._dom.customColorLabel.style.display = isCustom ? 'inline-flex' : 'none';
        if (this._dom.customTextLabel) this._dom.customTextLabel.style.display = isCustom ? 'inline-flex' : 'none';
        this._applySettings();
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
            const result = await bridge.novelUpdateProgress(
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
            this._renderGrid();
            return;
        }
        const filtered = this.novels.filter(novel => 
            novel.title.includes(keyword) || 
            novel.author.includes(keyword)
        );
        this._renderGrid(filtered);
    }

    destroy() {
        this._saveProgress();
        this.currentNovel = null;
        this.chapters = [];
        this._isReaderMode = false;
    }
}

function debounce(func, wait) {
    let timeout;
    return function(...args) {
        clearTimeout(timeout);
        timeout = setTimeout(() => func.apply(this, args), wait);
    };
}