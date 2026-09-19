// ============================================================
// 文档阅读器 — 主控制器
//
// 形态见 docs/document-reader-redesign.md：
//   书架态  左栏＝导航（全部/最近/书签）      主区＝封面网格
//   阅读态  左栏＝书名 + 三个入口（目录/阅读设置/朗读设置）  主区＝正文
//   目录与阅读设置是弹窗，朗读设置是覆盖主区的独立页（reader-voice-page.js）
//   朗读控制是右下角浮动小卡（reader-tts.js），不占正文宽度
//
// 分页 / 测量 / 滚动加载在 reader-engine.js；阅读偏好持久化在 settings.js。
// ============================================================
class DocumentReader {
    constructor() {
        this.documents = [];
        this.currentDocument = null;
        this.marks = {};              // {文档 id: [书签]}
        this.view = 'all';            // all | recent | marks
        this._isReaderMode = false;

        this.fontSize = 16;
        this.lineHeight = 1.8;
        this.letterSpacing = 0;
        this.theme = 'auto';
        this.bgColor = '#ffffff';
        this.textColor = '#1a1a1a';
        // 编码全自动、没有开关：后端的解码链（UTF-8 自证 → 中文编码 → gb18030）比让
        // 用户手选任何一个编码都强，手选唯一能做到的额外效果是把能读的书解成乱码。
        this.encoding = 'auto';
        this.mode = 'scroll';

        this.engine = new DocumentReaderEngine(this);
        this.tts = new ReaderTts(this);
        this.voicePage = new ReaderVoicePage(this);
        this._dom = {};
        this.settings = null;
        this._shelfSeq = 0;           // 书架渲染序号：丢弃过期的异步封面回填
    }

    async init() {
        this._cacheDom();
        this.settings = new ReaderSettingsStore(this);
        this._bindEvents();
        this._bindModalDismiss();
        await this._loadDocuments();
        await this._loadMarks();
        // 阅读偏好从后端设置读（跨设备持久化），读到之后再渲染书架与正文
        await this.settings.load();
        this.engine.setMode(this.mode);
        this._renderShelf();
        this.loadExtensions();
        // 朗读可以关掉（`document-reader-tts=off`）：浏览器用例不需要它，
        // 每次初始化都去打一遍引擎状态既慢又依赖网络。关掉后朗读入口依然在，
        // 点了会提示未启用。
        if (localStorage.getItem('document-reader-tts') !== 'off') await this.tts.init();
    }

    /** 左栏高亮与实际主区内容保持一致（书架/最近/书签/朗读设置）。 */
    _syncNavActive() {
        const voiceOpen = !!this._voicePaneOpen;
        document.querySelectorAll('.nr-nav-item').forEach((btn) => {
            if (btn.id === 'nr-open-voice') {
                btn.classList.toggle('active', voiceOpen);
                return;
            }
            if (!btn.dataset.view) return;
            btn.classList.toggle('active', !voiceOpen && btn.dataset.view === this.view);
        });
    }

    /** 通用扩展入口：朗读设置之类的能力型插件从这里挂进阅读器（见 plugin-guide §2.1）。 */
    async loadExtensions() {
        const container = this._dom.extensions;
        if (!container || typeof renderExtensions !== 'function') return;
        try {
            await renderExtensions(container, 'document-reader', 'sidebar', { title: '扩展' });
        } catch (e) {
            console.error('加载扩展入口失败:', e);
        }
    }

    // ===== DOM =====

    _cacheDom() {
        this._dom = {
            readerTools: document.getElementById('nr-reader-tools'),
            backToShelf: document.getElementById('nr-back-to-shelf'),
            shelfView: document.getElementById('nr-shelf-view'),
            readerView: document.getElementById('nr-reader-view'),
            grid: document.getElementById('nr-grid'),
            contentArea: document.getElementById('document-content-area'),
            progressFill: document.getElementById('document-progress-fill'),
            viewTitle: document.getElementById('nr-view-title'),
            viewSub: document.getElementById('nr-view-sub'),
            searchInput: document.getElementById('nr-search'),
            searchClear: document.getElementById('nr-search-clear'),
            tocList: document.getElementById('nr-toc-list'),
            tocSub: document.getElementById('nr-toc-sub'),
            tocFilter: document.getElementById('nr-toc-filter'),
            status: document.getElementById('nr-status'),
            extensions: document.getElementById('nr-extensions'),
            countAll: document.getElementById('nr-count-all'),
            countMarks: document.getElementById('nr-count-marks'),
        };
    }

    _bindEvents() {
        if (this._dom.searchInput) {
            this._dom.searchInput.addEventListener('input',
                Utils.debounce(() => this._onSearch(), 200));
        }
        if (this._dom.searchClear) {
            this._dom.searchClear.addEventListener('click', () => {
                this._dom.searchInput.value = '';
                this._dom.searchClear.classList.add('hidden');
                this._onSearch();
                this._dom.searchInput.focus();
            });
        }

        document.querySelectorAll('.nr-nav-item[data-view]').forEach((btn) => {
            btn.addEventListener('click', () => this.switchView(btn.dataset.view));
        });

        document.getElementById('nr-back-to-shelf')
            .addEventListener('click', () => this.returnToShelf());
        document.getElementById('nr-open-toc')
            .addEventListener('click', () => this.openToc());
        document.getElementById('nr-open-read-settings')
            .addEventListener('click', () => openModal('nr-read-settings-modal'));
        document.getElementById('nr-open-voice')
            .addEventListener('click', () => this.voicePage.open());
        document.getElementById('btn-settings')
            .addEventListener('click', () => openSettingsModal({ title: '文档阅读设置' }));

        if (this._dom.tocFilter) {
            this._dom.tocFilter.addEventListener('input', () => this._renderTocItems());
        }

        this._bindReaderSettings();
        this._bindKeyboard();
        this._bindPageTurn();
        this._bindContextMenu();
        this._bindScroll();
        this._bindLifecycle();
        window.addEventListener('beforeunload', () => this._saveCurrentProgress(true));
    }

    /** 弹窗：点遮罩关闭（按按下位置判定，与壳内其它弹窗一致）。 */
    _bindModalDismiss() {
        document.querySelectorAll('.modal').forEach((modal) => {
            modal.addEventListener('pointerdown', (event) => {
                if (event.target === modal) closeModal(modal.id);
            });
        });
        document.querySelectorAll('[data-nr-close]').forEach((btn) => {
            btn.addEventListener('click', () => closeModal(btn.dataset.nrClose));
        });
    }

    _bindReaderSettings() {
        const dom = this._dom;
        dom.fontSizeSlider = document.getElementById('document-font-size');
        dom.fontSizeValue = document.getElementById('document-font-size-value');
        dom.lineHeightSlider = document.getElementById('document-line-height');
        dom.lineHeightValue = document.getElementById('document-line-height-value');
        dom.letterSpacingSlider = document.getElementById('document-letter-spacing');
        dom.letterSpacingValue = document.getElementById('document-letter-spacing-value');
        dom.themeSelect = document.getElementById('document-theme-select');
        dom.modeSelect = document.getElementById('document-mode-select');
        dom.bgColorInput = document.getElementById('document-bg-color');
        dom.textColorInput = document.getElementById('document-text-color');
        dom.customColorRow = document.getElementById('custom-color-row');
        dom.customTextRow = document.getElementById('custom-text-row');

        dom.fontSizeSlider.addEventListener('input', (event) => {
            this.fontSize = parseInt(event.target.value, 10);
            dom.fontSizeValue.textContent = this.fontSize;
            this.settings.apply();
        });
        dom.lineHeightSlider.addEventListener('input', (event) => {
            this.lineHeight = parseFloat(event.target.value);
            dom.lineHeightValue.textContent = this.lineHeight.toFixed(1);
            this.settings.apply();
        });
        dom.letterSpacingSlider.addEventListener('input', (event) => {
            this.letterSpacing = parseFloat(event.target.value);
            dom.letterSpacingValue.textContent = `${this.letterSpacing}px`;
            this.settings.apply();
        });
        dom.themeSelect.addEventListener('change', (event) => {
            this.theme = event.target.value;
            this._syncCustomColorRows();
            this.settings.apply();
        });
        dom.bgColorInput.addEventListener('change', (event) => {
            this.bgColor = event.target.value;
            this.settings.apply();
        });
        dom.textColorInput.addEventListener('change', (event) => {
            this.textColor = event.target.value;
            this.settings.apply();
        });
        dom.modeSelect.addEventListener('change', (event) => {
            this.mode = event.target.value === 'page' ? 'page' : 'scroll';
            this.engine.setMode(this.mode);
            this.settings.save();
            Toast.info(this.mode === 'scroll' ? '已切换到连续滚动模式' : '已切换到翻页模式');
        });
        this._syncCustomColorRows();
    }

    _syncCustomColorRows() {
        const dom = this._dom;
        const custom = this.theme === 'custom';
        if (dom.customColorRow) dom.customColorRow.style.display = custom ? 'flex' : 'none';
        if (dom.customTextRow) dom.customTextRow.style.display = custom ? 'flex' : 'none';
    }

    _bindKeyboard() {
        document.addEventListener('keydown', (event) => {
            if (!this._isReaderMode) return;
            const tag = (event.target && event.target.tagName) || '';
            if (tag === 'INPUT' || tag === 'TEXTAREA' || tag === 'SELECT') return;
            if (document.querySelector('.modal.active')) return;
            switch (event.key) {
                case 'ArrowUp':
                case 'PageUp':
                    event.preventDefault();
                    this._scrollArea(-0.8);
                    break;
                case 'ArrowDown':
                case 'PageDown':
                case ' ':
                    event.preventDefault();
                    this._scrollArea(0.8);
                    break;
                case 'Escape':
                    event.preventDefault();
                    this.returnToShelf();
                    break;
                default:
                    break;
            }
        });
    }

    /**
     * 常驻插件被切到后台时的收尾。
     *
     * **刻意不绑 onHide 去暂停朗读**：切插件或最小化窗口时朗读应当继续（与
     * media-player 一致 —— 它压根不绑生命周期钩子，音频元素常驻所以不断）。
     * 之前这里写了 `onHide → tts.pause()`，表现就是"一切走朗读就断"。
     * 只保留 dispose：插件真被卸载/刷新时再收尾。
     */
    _bindLifecycle() {
        if (!window.PluginLifecycle) return;
        window.PluginLifecycle.onDispose(() => this.tts.stop());
    }

    _scrollArea(factor) {
        const el = this._dom.contentArea;
        if (el) el.scrollBy({ top: el.clientHeight * factor, behavior: 'smooth' });
    }

    /**
     * 点击翻页：左 25% 上一页、右 25% 下一页（中间 50% 不动）。
     *
     * 与朗读的分工：朗读只从右键菜单进，点击永远只管翻页 —— 否则"想选中一句话"
     * 会顺带翻一章（选中文字时本函数直接返回，这条判定是从旧实现继承下来的）。
     */
    _bindPageTurn() {
        const el = this._dom.contentArea;
        if (!el) return;
        el.addEventListener('click', async (event) => {
            if (!this._isReaderMode) return;
            if (this.mode === 'scroll') return;      // 连续滚动没有"翻页"这回事
            if (event.target.closest('.document-pdf-frame, [data-no-pageturn]')) return;
            const selection = window.getSelection ? String(window.getSelection()) : '';
            if (selection) return;
            const rect = el.getBoundingClientRect();
            const ratio = (event.clientX - rect.left) / rect.width;
            if (ratio < 0.25) await this._turnPage(-1);
            else if (ratio > 0.75) await this._turnPage(1);
        });
    }

    async _turnPage(direction) {
        const result = direction > 0 ? await this.engine.nextPage() : await this.engine.prevPage();
        if (result === 'end') {
            Toast.info(direction > 0 ? '已经是最后一章了' : '已经是第一章了');
            return;
        }
        if (result === 'moved') this._afterPageRender();
    }

    /**
     * 滚动：节流成"每 60ms 至多一次"，两条线共用这一个通道。
     *
     * 以前用"距上次不足 80ms 就丢弃"的节流，会丢掉最后一次滚动事件 —— 滑到底停住
     * 之后下一章永远不加载。这里改成挂起式节流（末尾必执行）。
     */
    _bindScroll() {
        const el = this._dom.contentArea;
        if (!el) return;
        let timer = null;
        const onScroll = () => {
            if (timer) return;
            timer = setTimeout(async () => {
                timer = null;
                if (!this._isReaderMode) return;
                await this.engine.handleScroll();
                this._afterPageRender();
                // 书签标记是按 Range 的矩形定位的，滚动后位置就变了
                this._flashMarks();
                // 手动滚动换了章：朗读跟着停（朗读位置不持久化，见设计文档 §4）。
                // 不自动续读是有意的 —— 用户可能只是翻回去看一下，突然念起来更烦人。
                // 但朗读自己翻章（_advanceChapter）也会触发滚动事件，那种情况不能停，
                // 否则"念完这章自动续下一章"刚跳过去就被自己停掉。
                if (this.tts.isPlaying() && !this._ttsAdvancing
                    && this.tts.chapter !== this.engine.currentChapterIndex) {
                    this.tts.stop();
                    Toast.info('已翻到别的章节，朗读已停止');
                }
            }, 60);
        };
        el.addEventListener('scroll', onScroll, { passive: true });
        el.addEventListener('wheel', onScroll, { passive: true });
        window.addEventListener('resize', () => {
            if (this._isReaderMode) this._afterPageRender();
        });
    }

    // ===== 书架 =====

    async _loadDocuments() {
        try {
            const result = await Bridge.call('document_list');
            this.documents = result.documents || [];
        } catch (e) {
            console.error('加载文档列表失败:', e);
            this.documents = [];
        }
    }

    async _loadMarks() {
        try {
            const result = await Bridge.call('marks_list', '');
            this.marks = {};
            (result.marks || []).forEach((mark) => {
                const id = mark.document_id;
                if (!id) return;
                (this.marks[id] = this.marks[id] || []).push(mark);
            });
        } catch (e) {
            this.marks = {};
        }
    }

    switchView(view) {
        this.view = view;
        // 切到书架分类就离开朗读设置面板
        if (this._voicePaneOpen) this.voicePage.close();
        document.querySelectorAll('.nr-nav-item[data-view]').forEach((btn) => {
            btn.classList.toggle('active', btn.dataset.view === view);
        });
        this._dom.readerTools.classList.add('hidden');
        this._dom.backToShelf.classList.add('hidden');
        this._dom.searchInput.parentElement.classList.remove('hidden');
        this.returnToShelf({ keepView: true });
        this._updateStatus();
    }

    returnToShelf(options = {}) {
        if (this._isReaderMode) this._saveCurrentProgress(true);
        this.tts.stop();
        if (this._voicePaneOpen) {
            this._voicePaneOpen = false;
            const pane = document.getElementById('nr-voice-page');
            if (pane) pane.classList.add('hidden');
        }
        this._isReaderMode = false;
        this.currentDocument = null;
        this._dom.shelfView.classList.remove('hidden');
        this._dom.readerView.classList.add('hidden');
        // 阅读入口随阅读态出现/消失：书架态只有"全部/最近/书签/朗读设置"
        this._dom.readerTools.classList.add('hidden');
        this._dom.backToShelf.classList.add('hidden');
        this._dom.searchInput.parentElement.classList.remove('hidden');
        if (!options.keepView) this.view = this.view || 'all';
        this._renderShelf();
        this._syncNavActive();
    }

    _onSearch() {
        const keyword = this._dom.searchInput.value.trim();
        this._dom.searchClear.classList.toggle('hidden', !keyword);
        this._renderShelf(keyword);
    }

    _visibleDocuments(keyword = '') {
        let items = this.documents;
        if (this.view === 'recent') {
            items = items.filter((doc) => doc.last_read_time)
                .sort((a, b) => String(b.last_read_time).localeCompare(String(a.last_read_time)));
        } else if (this.view === 'marks') {
            items = items.filter((doc) => (this.marks[doc.id] || []).length);
        }
        if (keyword) {
            items = items.filter((doc) => (doc.title || '').includes(keyword)
                || (doc.author || '').includes(keyword));
        }
        return items;
    }

    // ===== 书签视图 =====
    //
    // 书签是唯一带字级位置的状态（阅读进度只记到章）。列表按文档分组，
    // 每条能跳转（goToMark：切章 + 按 char 定位，engine 里带 snippet 校验）与删除。

    _updateStatus() {
        const total = Object.values(this.marks).reduce((sum, list) => sum + list.length, 0);
        // 侧栏两个计数徽标（.:empty 时自动隐藏，所以空值直接写空串）
        if (this._dom.countAll) {
            this._dom.countAll.textContent = this.view === 'all' ? String(this.documents.length) : '';
        }
        if (this._dom.countMarks) {
            this._dom.countMarks.textContent = total ? String(total) : '';
        }
        if (!this._dom.status) return;
        // 侧栏底部只有一行宽度：引擎名取冒号前那一段，不然 "edge-tts（神经音色，联网）"
        // 一定被截成 "edge-"，看着像坏了
        const engine = this.tts && this.tts.lastEngine
            ? ` · 朗读 ${String(this.tts.lastEngine).split(/[（(:：]/)[0].trim()}`
            : '';
        this._dom.status.textContent = `${this.documents.length} 本${engine}`;
    }

    // ===== 打开文档 / 阅读态 =====

    async openDocument(documentId, options = {}) {
        const doc = this.documents.find((item) => item.id === documentId);
        if (!doc) return null;
        this.currentDocument = doc;
        this._showLoading(true);
        try {
            if (doc.kind === 'pdf' || doc.kind === 'external') {
                this._enterReaderMode(doc);
                this._showLoading(false);
                this._openNonChapter(doc);
                return doc;
            }
            const result = await Bridge.call('document_get_chapters', documentId, this.encoding);
            const chapters = result.chapters || [];
            doc.chapter_count = chapters.length;
            this.engine.setMode(this.mode);
            this.engine.reset(documentId, chapters, this.encoding);
            this._enterReaderMode(doc);
            const chapter = options.chapter !== undefined && options.chapter !== null
                ? options.chapter
                : Math.max(0, Math.min(Number(doc.last_read_chapter) || 0, Math.max(0, chapters.length - 1)));
            await this.engine.goToChapter(chapter, 0);
            this._afterPageRender();
            this._showLoading(false);
            if (options.startTts) {
                // 从"左上角第一个字所在句子的开头"念起
                this.tts.startFromViewport();
            }
            return doc;
        } catch (e) {
            console.error('打开文档失败:', e);
            Toast.error('打开文档失败');
            this._showLoading(false);
            return null;
        }
    }

    _enterReaderMode(doc) {
        this._isReaderMode = true;
        this._dom.shelfView.classList.add('hidden');
        this._dom.readerView.classList.remove('hidden');
        this._dom.readerTools.classList.remove('hidden');
        this._dom.backToShelf.classList.remove('hidden');
        this._dom.searchInput.parentElement.classList.add('hidden');
        this._dom.viewTitle.textContent = doc.title || doc.id;
        // 副标题留空：壳的工具栏空间有限，"阅读" + 书名挤在一起会把书名截成单字
        // （实测显示成"书"），不如让书名自己占满
        this._dom.viewSub.textContent = '';
    }

    _openNonChapter(doc) {
        this.engine.reset(doc.id, [], 'auto');
        const area = this._dom.contentArea;
        if (!area) return;
        this._dom.progressFill.style.width = '0%';
        if (doc.kind === 'pdf') {
            // WebView 自带 PDF 阅览器：同源 /file 路由直接嵌，零依赖
            const src = Bridge.originalUrl(doc.file_path);
            area.innerHTML = `
                <iframe class="document-pdf-frame" src="${Utils.escapeHtml(src)}"
                        title="${Utils.escapeHtml(doc.title || doc.id)}"></iframe>
                <button type="button" class="btn document-pdf-open" id="document-open-external">↗ 系统程序打开</button>
            `;
        } else {
            area.innerHTML = `
                <div class="nr-empty">
                    <div class="nr-empty-icon">📄</div>
                    <div class="nr-empty-text">${Utils.escapeHtml(doc.title || doc.id)}</div>
                    <div class="nr-empty-hint">该格式不在阅读器内渲染，可交给系统默认程序打开</div>
                    <button type="button" class="btn" id="document-open-external">↗ 用系统程序打开</button>
                </div>`;
        }
        const btn = document.getElementById('document-open-external');
        if (btn) btn.addEventListener('click', () => this._openExternal(doc));
    }

    async _openExternal(doc) {
        try {
            const result = await Bridge.call('document_open_external', doc.id);
            if (result && result.error) Toast.error(result.error);
            else Toast.success('已交给系统程序打开');
        } catch (e) {
            Toast.error('调用系统程序失败');
        }
    }

    _afterPageRender() {
        this._updateProgressBar();
        this._updateTocSub();
        this._flashMarks();
    }

    _updateProgressBar() {
        const fill = this._dom.progressFill;
        const chapters = this.engine.chapters;
        if (!fill || !chapters.length) return;
        const progress = (this.engine.currentChapterIndex + this.engine.chapterFraction()) / chapters.length;
        fill.style.width = `${Math.max(0, Math.min(100, progress * 100)).toFixed(2)}%`;
    }

    // ===== 目录弹窗 =====

    // ===== 正文右键菜单 =====

    // ===== 书签 =====

    /** 当前视口顶部那一句的起点（朗读与书签共用的定位法则）。 */
    // ===== 进度 =====

    async _saveCurrentProgress(force = false) {
        if (!this._isReaderMode || !this.currentDocument) return;
        if (!this.engine.chapters.length) return;
        // 朗读时也会保存：朗读会带着滚动走，而"上次读到哪"正是用户关掉应用后
        // 最想接着看的地方（需求：加上朗读后，持久化成了必要项）。
        // 朗读自己没有状态文件，进度只记到章，所以两者不会互相污染。
        try {
            await Bridge.call('document_update_progress', this.currentDocument.id,
                this.engine.currentChapterIndex, this.engine.chapterFraction(), this.encoding);
        } catch (e) {
            console.error('保存进度失败:', e);
        }
    }

    async reloadDocument() {
        if (!this.currentDocument) return;
        const chapter = this.engine.currentChapterIndex;
        await this._saveCurrentProgress(true);
        await this.openDocument(this.currentDocument.id, { chapter });
    }

    _showLoading(show) {
        let el = document.getElementById('document-loading');
        if (show) {
            if (!el) {
                el = document.createElement('div');
                el.id = 'document-loading';
                el.className = 'document-loading';
                el.innerHTML = '<div class="spinner"></div><span>加载中...</span>';
                if (this._dom.contentArea) this._dom.contentArea.appendChild(el);
            }
            el.style.display = 'flex';
        } else if (el) {
            el.style.display = 'none';
        }
    }

    /** 给朗读用：当前章正文文本 + 它的 DOM 容器。 */
    getChapterContext() {
        return this.engine.getChapterContext();
    }

    destroy() {
        this.tts.stop();
        this.currentDocument = null;
        this._isReaderMode = false;
    }
}

// ===== 轻量弹窗开关（壳的模态样式 + .active 类） =====

function openModal(id) {
    const el = document.getElementById(id);
    if (el) el.classList.add('active');
}

function closeModal(id) {
    const el = document.getElementById(id);
    if (el) el.classList.remove('active');
}
