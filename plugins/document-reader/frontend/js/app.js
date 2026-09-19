// ============================================================
// 文档阅读器 — UI 控制器
// 分页 / 测量 / 翻页逻辑位于 reader-engine.js。
// ============================================================
class DocumentReader {
    constructor() {
        this.documents = [];
        this.currentDocument = null;

        this.fontSize = 16;
        this.lineHeight = 1.8;
        this.letterSpacing = 0;
        this.theme = 'auto';
        this.bgColor = '#ffffff';
        this.textColor = '#1a1a1a';
        // 编码全自动、没有开关：后端的解码链（UTF-8 自证 → 中文编码 → gb18030）现在比
        // 让用户手选任何一个编码都强 —— 手选唯一能做到的"额外效果"是把本来能读的书
        // 解成乱码，所以界面上干脆不提供这个选择（见 parser.read_full_content）。
        this.encoding = 'auto';
        this.mode = 'scroll'; // 'page' | 'scroll'

        this._isReaderMode = false;
        this._sidebarMode = 'shelf';
        this._lastSavedChapter = -1;
        this._lastSavedPosition = -1;
        this._chromeChapterIndex = -1;

        this.engine = new DocumentReaderEngine(this);
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
        await this._loadDocuments();

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
            contentArea: document.getElementById('document-content-area'),
            chapterTitle: document.getElementById('document-chapter-title'),
            progressFill: document.getElementById('document-progress-fill'),
            modeSelect: document.getElementById('document-mode-select'),
            searchInput: document.getElementById('document-search'),
            browseGroup: document.getElementById('document-browse-group'),
            readerNav: document.getElementById('document-reader-nav'),
            readerSettingsBar: document.getElementById('document-reader-settings-bar'),
            fontToggle: document.getElementById('document-font-toggle'),
            shelfPanel: document.getElementById('document-shelf-panel'),
            chapterPanel: document.getElementById('document-chapter-panel'),
            shelfToggle: document.getElementById('sidebar-toggle-shelf'),
            chapterToggle: document.getElementById('sidebar-toggle-chapters'),
            shelfList: document.getElementById('document-shelf-list'),
            chapterList: document.getElementById('document-chapter-list'),
            fontSizeSlider: document.getElementById('document-font-size'),
            fontSizeValue: document.getElementById('document-font-size-value'),
            lineHeightSlider: document.getElementById('document-line-height'),
            lineHeightValue: document.getElementById('document-line-height-value'),
            letterSpacingSlider: document.getElementById('document-letter-spacing'),
            letterSpacingValue: document.getElementById('document-letter-spacing-value'),
            themeSelect: document.getElementById('document-theme-select'),
            bgColorInput: document.getElementById('document-bg-color'),
            textColorInput: document.getElementById('document-text-color'),
            customColorLabel: document.getElementById('custom-color-label'),
            customTextLabel: document.getElementById('custom-text-label'),
        };
    }

    _bindSettingsButton() {
        const btn = document.getElementById('btn-settings');
        if (btn) btn.addEventListener('click', () => openSettingsModal({ title: '阅读设置' }));
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

        // 滚动 / 滚轮走同一条节流通道，合并成"每 60ms 至多一次"，而不是"距上次不足
        // 80ms 就丢弃"：后者会丢掉最后一次滚动事件，滑到底停住后下一章永远不加载。
        // 另外内容填不满一屏时（短文）浏览器不会有滚动事件 —— 用户"滑不动"时唯一还
        // 能收到的事件就是 wheel，所以两个都要监听。
        let scrollTimer = null;
        const onScrollActivity = () => {
            this._scheduleProgressSave();
            if (scrollTimer) return;
            scrollTimer = setTimeout(() => {
                scrollTimer = null;
                this.engine.handleScroll().then(() => {
                    this._syncReaderChrome();
                    this._updateProgressBar();
                });
            }, 60);
        };
        if (this._dom.contentArea) {
            this._dom.contentArea.addEventListener('scroll', onScrollActivity, { passive: true });
            this._dom.contentArea.addEventListener('wheel', onScrollActivity, { passive: true });
        }
        if (this._dom.shelfToggle) this._dom.shelfToggle.addEventListener('click', () => this._switchSidebar('shelf'));
        if (this._dom.chapterToggle) this._dom.chapterToggle.addEventListener('click', () => this._switchSidebar('chapters'));
        if (this._dom.fontToggle) this._dom.fontToggle.addEventListener('click', () => this._toggleReaderSettings());

        if (this._dom.contentArea) {
            this._dom.contentArea.addEventListener('click', (e) => {
                if (!this._isReaderMode) return;
                // 连续滚动模式没有"翻页"这回事：点击触发的整章重建会让阅读位置跳走、
                // 内容区闪一下（用户看到的就是"点一下像刷新/卡住一次"）。
                if (this.mode === 'scroll') return;
                // 选中文字时的 click 不该被当成翻页
                const selection = window.getSelection ? String(window.getSelection()) : '';
                if (selection) return;
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
        this._setChapterTabVisible(true);
        this._switchSidebar('shelf');
        if (this._dom.chapterTitle) this._dom.chapterTitle.textContent = '未打开文档';
        if (this._dom.progressFill) this._dom.progressFill.style.width = '0%';
        if (this._dom.contentArea) {
            this._dom.contentArea.innerHTML = `
                <div class="document-empty">
                    <div class="document-empty-icon">📖</div>
                    <div class="document-empty-text">在左侧书架选择一份文档开始阅读</div>
                </div>
            `;
        }
        this._renderShelf();
    }

    async _loadDocuments() {
        try {
            const result = await Bridge.call('document_list');
            this.documents = result.documents || [];
            this._renderShelf();
        } catch (e) {
            console.error('加载文档列表失败:', e);
        }
    }

    _renderShelf(filteredDocuments) {
        const container = this._dom.shelfList;
        if (!container) return;
        const documents = filteredDocuments || this.documents;
        if (!documents.length) {
            container.innerHTML = `
                <div class="document-empty">
                    <div class="document-empty-icon">📖</div>
                    <div class="document-empty-text">没有找到文档</div>
                    <div class="document-empty-hint">请把 .txt / .md / .epub 放进文档目录</div>
                </div>
            `;
            return;
        }
        container.innerHTML = documents.map(document => `
            <div class="document-shelf-item ${this.currentDocument && document.id === this.currentDocument.id ? 'active' : ''}" data-id="${Utils.escapeHtml(document.id)}">
                <div class="document-shelf-title">
                    <span class="document-shelf-name">${Utils.escapeHtml(document.title)}</span>
                    <span class="document-shelf-kind">${Utils.escapeHtml((document.kind || 'txt').toUpperCase())}</span>
                </div>
                ${document.dir ? `<div class="document-shelf-dir">${Utils.escapeHtml(document.dir)}</div>` : ''}
                <div class="document-shelf-meta">
                    <span>${this._shelfMetaText(document)}</span>
                    <span class="document-shelf-progress">${Math.round((document.progress || 0) * 100)}%</span>
                </div>
            </div>
        `).join('');
        if (window.Motion) Motion.stagger(container, '.document-shelf-item');
        container.querySelectorAll('.document-shelf-item').forEach(item => {
            item.addEventListener('click', () => this._openDocument(item.dataset.id));
        });
    }

    // 只切"当前正在读"的高亮。开书时以前调的是 _renderShelf()：整列表重建 + 逐个
    // 入场动画，看起来就像点一下"刷新"了一次。
    _markShelfActive() {
        const container = this._dom.shelfList;
        if (!container) return;
        container.querySelectorAll('.document-shelf-item').forEach(item => {
            item.classList.toggle('active',
                !!this.currentDocument && item.dataset.id === this.currentDocument.id);
        });
    }

    _shelfMetaText(document) {
        if (document.kind === 'pdf' || document.kind === 'external') return '系统程序打开';
        // 章节数要解析过一次才知道；没解析过就显示体积，别显示 "?"
        if (document.chapter_count) return `${document.chapter_count} 章`;
        return DocumentUtils.formatSize(document.file_size);
    }

    // 非章节型文档（pdf / 外部打开）没有目录可看，藏掉那个标签页，避免点开是空的
    _setChapterTabVisible(visible) {
        if (this._dom.chapterToggle) this._dom.chapterToggle.style.display = visible ? '' : 'none';
    }

    async _openDocument(documentId, startChapter = null, fraction = 0) {
        try {
            this._showLoading(true);
            this.currentDocument = this.documents.find(n => n.id === documentId);
            if (!this.currentDocument) return;

            // pdf / 交给系统程序的格式没有章节模型，直接换内容区，不进阅读引擎
            if (this.currentDocument.kind === 'pdf' || this.currentDocument.kind === 'external') {
                this._showLoading(false);
                this._openNonChapter(this.currentDocument);
                return;
            }

            const result = await Bridge.call('document_get_chapters', documentId, this.encoding);
            const chapters = result.chapters || [];
            // 章节数现在知道了：回填到列表项，回到书架时那一行显示的就是真实章数
            this.currentDocument.chapter_count = chapters.length;
            this.engine.setMode(this.mode);
            this.engine.reset(documentId, chapters, this.encoding);

            this._isReaderMode = true;
            this._setToolbarMode('reader');
            this._setChapterTabVisible(true);
            this._switchSidebar('chapters');
            this._markShelfActive();
            this._chapterListBuiltFor = null;
            this._renderChapterList();

            const saved = startChapter !== null && startChapter !== undefined
                ? startChapter
                : (this.currentDocument.last_read_chapter || 0);
            const savedFraction = startChapter !== null && startChapter !== undefined
                ? fraction
                : (Number(this.currentDocument.scroll_position) || 0);

            await this.engine.goToChapter(
                Math.max(0, Math.min(saved, chapters.length - 1)), savedFraction);
            this._afterPageRender(true);
            this._showLoading(false);
        } catch (e) {
            console.error('打开文档失败:', e);
            this._showLoading(false);
        }
    }

    // 非章节型文档（pdf / 交给系统程序的格式）：不进阅读引擎，只换内容区。
    _openNonChapter(document) {
        this.engine.reset(document.id, [], 'auto');
        this._isReaderMode = true;
        this._setToolbarMode('reader');
        this._setChapterTabVisible(false);
        this._switchSidebar('shelf');
        this._markShelfActive();
        this._chapterListBuiltFor = null;
        this._renderChapterList();
        if (this._dom.chapterTitle) this._dom.chapterTitle.textContent = document.title;
        if (this._dom.progressFill) this._dom.progressFill.style.width = '0%';

        const area = this._dom.contentArea;
        if (!area) return;
        if (document.kind === 'pdf') {
            // WebView 自带 PDF 阅览器：同源 /file 路由直接嵌，零依赖
            const src = Bridge.originalUrl(document.file_path);
            area.innerHTML = `
                <iframe class="document-pdf-frame" src="${Utils.escapeHtml(src)}"
                        title="${Utils.escapeHtml(document.title)}"></iframe>
                <button class="btn document-pdf-open" id="document-open-external">↗ 系统程序打开</button>
            `;
        } else {
            area.innerHTML = `
                <div class="document-empty">
                    <div class="document-empty-icon">📄</div>
                    <div class="document-empty-text">${Utils.escapeHtml(document.title)}</div>
                    <div class="document-empty-hint">该格式不在阅读器内渲染，可交给系统默认程序打开</div>
                    <button class="btn" id="document-open-external">↗ 用系统程序打开</button>
                </div>
            `;
        }
        const btn = document.getElementById('document-open-external');
        if (btn) btn.addEventListener('click', () => this._openExternal(document));
    }

    async _openExternal(document) {
        try {
            const result = await Bridge.call('document_open_external', document.id);
            if (result && result.error) Toast.error(result.error);
            else Toast.success('已交给系统程序打开');
        } catch (e) {
            console.error('调用系统程序失败:', e);
            Toast.error('调用系统程序失败');
        }
    }

    async _reloadDocument() {
        if (!this.currentDocument) return;
        const chapter = this.engine.currentChapterIndex;
        const fraction = this.engine.chapterFraction();
        await this._saveCurrentProgress(true);
        await this._openDocument(this.currentDocument.id, chapter, fraction);
    }

    _scheduleProgressSave() {
        clearTimeout(this._progressTimer);
        this._progressTimer = setTimeout(() => this._saveCurrentProgress(false), 900);
    }

    _afterPageRender(scrollList = false) {
        // 记下界面当前反映的是哪一章：滚动时只有"真的换章"才需要重刷目录高亮
        this._chromeChapterIndex = this.engine.currentChapterIndex;
        this._updateChapterListActive(scrollList);
        this._updateChapterTitle();
        this._updateProgressBar();
    }

    /** 滚动过程中当前章会变：只有换章时才重刷目录/标题（每 60ms 刷一次太贵）。 */
    _syncReaderChrome() {
        if (this.engine.currentChapterIndex === this._chromeChapterIndex) return;
        this._afterPageRender(false);
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
        if (!this.currentDocument || !this._isReaderMode) return;
        if (!this.engine.chapters.length) return;   // pdf / 非章节型文档没有进度可存
        const chapter = this.engine.currentChapterIndex;
        const position = this.engine.chapterFraction();
        if (!force && chapter === this._lastSavedChapter
            && Math.abs(position - this._lastSavedPosition) < 0.001) return;
        try {
            const result = await Bridge.call(
                'document_update_progress',
                this.currentDocument.id,
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
        if (!list || !this.currentDocument) return;
        if (this._chapterListBuiltFor !== this.currentDocument.id) {
            list.innerHTML = chapters.map((chapter, index) => `
                <div class="document-chapter-item" data-index="${index}">
                    <span>${Utils.escapeHtml(chapter.title)}</span>
                    <span class="chapter-words">${chapter.word_count}字</span>
                </div>
            `).join('');
            if (window.Motion) Motion.stagger(list, '.document-chapter-item');
            list.querySelectorAll('.document-chapter-item').forEach(item => {
                item.addEventListener('click', async () => {
                    this._saveCurrentProgress(true);
                    await this.engine.goToChapter(parseInt(item.dataset.index, 10), 0);
                    this._afterPageRender(true);
                });
            });
            this._chapterListBuiltFor = this.currentDocument.id;
        }
        this._updateChapterListActive(true);
    }

    _updateChapterListActive(scrollList = false) {
        const list = this._dom.chapterList;
        if (!list) return;
        list.querySelectorAll('.document-chapter-item').forEach(item => {
            item.classList.toggle('active',
                parseInt(item.dataset.index, 10) === this.engine.currentChapterIndex);
        });
        if (scrollList) {
            const active = list.querySelector('.document-chapter-item.active');
            if (active) active.scrollIntoView({ block: 'nearest' });
        }
    }

    _showLoading(show) {
        let loadingEl = document.getElementById('document-loading');
        if (show) {
            if (!loadingEl) {
                loadingEl = document.createElement('div');
                loadingEl.id = 'document-loading';
                loadingEl.className = 'document-loading';
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
        const filtered = this.documents.filter(document =>
            document.title.includes(keyword) ||
            document.author.includes(keyword)
        );
        this._renderShelf(filtered);
    }

    destroy() {
        this._saveCurrentProgress(true);
        this.currentDocument = null;
        this._isReaderMode = false;
    }
}
