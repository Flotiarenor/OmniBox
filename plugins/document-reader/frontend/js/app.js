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

    /** 常驻插件切走后不该继续朗读（听不见还费流量），也顺手暂停进度保存。 */
    _bindLifecycle() {
        if (!window.PluginLifecycle) return;
        window.PluginLifecycle.onHide(() => this.tts.pause(true));
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

    _renderShelf(keyword = '') {
        const grid = this._dom.grid;
        if (!grid) return;
        const labels = { all: ['全部文档', '按目录递归扫描'], recent: ['最近阅读', '按上次打开时间'], marks: ['书签', '点一条跳到那一句'] };
        const [title, sub] = labels[this.view] || labels.all;
        this._dom.viewTitle.textContent = title;
        if (this._dom.countAll) this._dom.countAll.textContent = this.documents.length || '';
        if (this._dom.countMarks) {
            const total = Object.values(this.marks).reduce((sum, list) => sum + list.length, 0);
            this._dom.countMarks.textContent = total || '';
        }
        if (this.view === 'marks' && !keyword) {
            this._renderMarks(grid);
            return;
        }

        const items = this._visibleDocuments(keyword);
        this._dom.viewSub.textContent = keyword
            ? `匹配 ${items.length} / ${this.documents.length} 本`
            : `${items.length} 本 · ${sub}`;

        if (!items.length) {
            grid.innerHTML = this._emptyHtml(
                keyword ? '🔍' : '📚',
                keyword ? '没有匹配的文档' : '这个分类还是空的',
                keyword ? '换个关键词试试' : '把 .txt / .md / .epub 放进文档目录');
            this._updateStatus();
            return;
        }

        const seq = ++this._shelfSeq;
        grid.innerHTML = items.map((doc, index) => this._cardHtml(doc, index)).join('');
        if (window.Motion) Motion.stagger(grid, '.nr-card');
        grid.querySelectorAll('.nr-card').forEach((card) => {
            card.addEventListener('click', () => this.openDocument(card.dataset.id));
        });
        // 封面异步补：EPUB 要解包封面图，逐张回填，失败不影响其它卡片
        items.forEach((doc) => {
            if (doc.kind !== 'epub') return;
            Bridge.call('document_cover', doc.id).then((result) => {
                if (seq !== this._shelfSeq) return;    // 书架已重绘，别回填到旧节点
                if (!result || !result.cover) return;
                const img = grid.querySelector(`.nr-card[data-id="${CSS.escape(doc.id)}"] .nr-cover-img`);
                if (img) img.src = result.cover;
            }).catch(() => {});
        });
        this._updateStatus();
    }

    // ===== 书签视图 =====
    //
    // 书签是唯一带字级位置的状态（阅读进度只记到章）。列表按文档分组，
    // 每条能跳转（goToMark：切章 + 按 char 定位，engine 里带 snippet 校验）与删除。

    _renderMarks(grid) {
        const groups = this.documents
            .map((doc) => ({ doc, marks: this.marks[doc.id] || [] }))
            .filter((item) => item.marks.length);
        const total = groups.reduce((sum, item) => sum + item.marks.length, 0);
        this._dom.viewSub.textContent = `${total} 条 · 来自 ${groups.length} 本`;
        if (!total) {
            grid.innerHTML = this._emptyHtml('⭐', '还没有书签',
                '在正文里选中一句话 → 右键 → 添加书签');
            this._updateStatus();
            return;
        }
        grid.innerHTML = groups.map(({ doc, marks }) => `
            <section class="nr-mark-group">
                <div class="nr-mark-head">
                    <span class="nr-mark-book">${Utils.escapeHtml(doc.title || doc.id)}</span>
                    <button type="button" class="nr-mark-clear" data-doc="${Utils.escapeHtml(doc.id)}"
                            title="删除这本书的全部书签">清空</button>
                </div>
                ${marks.map((mark) => `
                    <div class="nr-mark-item" data-doc="${Utils.escapeHtml(doc.id)}"
                         data-chapter="${mark.chapter}" data-char="${mark.char}">
                        <div class="nr-mark-text">${Utils.escapeHtml(mark.snippet || '（无摘录）')}</div>
                        <div class="nr-mark-meta">
                            <span>第 ${Number(mark.chapter) + 1} 章</span>
                            <span>${Utils.escapeHtml(mark.time || '')}</span>
                        </div>
                        <button type="button" class="nr-mark-del" title="删除这条书签">✕</button>
                    </div>`).join('')}
            </section>`).join('');

        grid.querySelectorAll('.nr-mark-item').forEach((item) => {
            item.addEventListener('click', async (event) => {
                if (event.target.closest('.nr-mark-del')) return;
                await this.goToMark(item.dataset.doc, {
                    chapter: parseInt(item.dataset.chapter, 10),
                    char: parseInt(item.dataset.char, 10),
                    snippet: '',
                });
            });
        });
        grid.querySelectorAll('.nr-mark-del').forEach((btn) => {
            btn.addEventListener('click', async (event) => {
                event.stopPropagation();
                const item = btn.closest('.nr-mark-item');
                await this._removeMark(item.dataset.doc,
                    parseInt(item.dataset.chapter, 10), parseInt(item.dataset.char, 10));
            });
        });
        grid.querySelectorAll('.nr-mark-clear').forEach((btn) => {
            btn.addEventListener('click', async (event) => {
                event.stopPropagation();
                const docId = btn.dataset.doc;
                const marks = [...(this.marks[docId] || [])];
                for (const mark of marks) {
                    await this._removeMark(docId, mark.chapter, mark.char, true);
                }
                Toast.success('已清空这本书的书签');
                this._renderShelf();
            });
        });
        this._updateStatus();
    }

    async _removeMark(documentId, chapter, char, quiet = false) {
        try {
            const result = await Bridge.call('marks_remove', documentId, chapter, char);
            if (result && result.success) {
                this.marks[documentId] = result.marks || [];
                if (!this.marks[documentId].length) delete this.marks[documentId];
                if (!quiet) {
                    Toast.success('已删除书签');
                    this._renderShelf();
                }
            } else if (!quiet) {
                Toast.error((result && result.error) || '删除书签失败');
            }
        } catch (e) {
            if (!quiet) Toast.error('删除书签失败');
        }
    }

    _emptyHtml(icon, text, hint = '') {
        return `<div class="nr-empty">
            <div class="nr-empty-icon">${icon}</div>
            <div class="nr-empty-text">${Utils.escapeHtml(text)}</div>
            ${hint ? `<div class="nr-empty-hint">${Utils.escapeHtml(hint)}</div>` : ''}
        </div>`;
    }

    _cardHtml(doc, index) {
        const percent = Math.round((Number(doc.progress) || 0) * 100);
        const kind = String(doc.kind || 'txt').toUpperCase();
        const meta = doc.kind === 'pdf' || doc.kind === 'external'
            ? '系统程序打开'
            : (doc.chapter_count ? `${doc.chapter_count} 章` : DocumentUtils.formatSize(doc.file_size));
        const markCount = (this.marks[doc.id] || []).length;
        return `
            <div class="nr-card" data-id="${Utils.escapeHtml(doc.id)}" style="--obx-i:${Math.min(index, 32)}">
                <div class="nr-cover">
                    <img class="nr-cover-img" alt="" loading="lazy"
                         src="${DocumentUtils.coverDataUrl(doc.title || doc.id)}">
                    <span class="nr-badge">${Utils.escapeHtml(kind)}</span>
                    ${markCount ? `<span class="nr-mark-count">⭐ ${markCount}</span>` : ''}
                    ${percent ? `<div class="nr-cover-progress"><span style="width:${percent}%"></span></div>` : ''}
                </div>
                <div class="nr-card-body">
                    <div class="nr-card-title" title="${Utils.escapeHtml(doc.title || doc.id)}">${Utils.escapeHtml(doc.title || doc.id)}</div>
                    <div class="nr-card-sub">${Utils.escapeHtml([doc.author, meta].filter(Boolean).join(' · '))}</div>
                </div>
            </div>`;
    }

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

    openToc() {
        if (!this._isReaderMode) return;
        const filter = this._dom.tocFilter;
        if (filter) filter.value = '';
        this._renderTocItems();
        this._updateTocSub();
        openModal('nr-toc-modal');
        // 打开时把当前章滚进视野：目录是"偶尔翻一下"的东西，
        // 每次打开都从第一章开始找，用起来就是白开一次。
        requestAnimationFrame(() => {
            const active = this._dom.tocList.querySelector('.document-chapter-item.active');
            if (active) active.scrollIntoView({ block: 'center' });
        });
    }

    _updateTocSub() {
        if (!this._dom.tocSub) return;
        const total = this.engine.chapters.length;
        if (!total) {
            this._dom.tocSub.textContent = '';
            return;
        }
        this._dom.tocSub.textContent = ` ${this.engine.currentChapterIndex + 1} / ${total}`;
    }

    _renderTocItems() {
        const list = this._dom.tocList;
        if (!list) return;
        const keyword = (this._dom.tocFilter.value || '').trim();
        const chapters = this.engine.chapters;
        const rows = chapters.map((chapter, index) => ({ chapter, index }))
            .filter(({ chapter, index }) => !keyword
                || String(chapter.title || '').includes(keyword)
                || String(index + 1) === keyword);
        if (!rows.length) {
            list.innerHTML = this._emptyHtml('🔍', '没有匹配的章节');
            return;
        }
        list.innerHTML = rows.map(({ chapter, index }) => {
            const marks = (this.marks[this.engine.documentId] || []).filter((m) => m.chapter === index);
            const read = index < this.engine.currentChapterIndex;
            return `
                <div class="document-chapter-item ${index === this.engine.currentChapterIndex ? 'active' : ''}
                            ${read ? 'is-read' : ''}" data-index="${index}">
                    <span class="nr-toc-title">${Utils.escapeHtml(chapter.title || `第${index + 1}章`)}</span>
                    <span class="nr-toc-meta">
                        ${marks.length ? `<span class="nr-toc-mark" title="${Utils.escapeHtml(marks[0].snippet || '')}">⭐</span>` : ''}
                        ${chapter.word_count ? `<span class="chapter-words">${chapter.word_count}字</span>` : ''}
                    </span>
                </div>`;
        }).join('');
        list.querySelectorAll('.document-chapter-item').forEach((item) => {
            item.addEventListener('click', async () => {
                const index = parseInt(item.dataset.index, 10);
                closeModal('nr-toc-modal');
                this.tts.stop();
                await this._saveCurrentProgress(true);
                await this.engine.goToChapter(index, 0);
                this._afterPageRender();
            });
        });
    }

    // ===== 正文右键菜单 =====

    _selectionText() {
        const selection = window.getSelection ? window.getSelection() : null;
        if (!selection || selection.isCollapsed) return '';
        const text = String(selection);
        if (!text.trim()) return '';
        // 只认落在正文里的选区：工具栏/侧栏被拖选时不该冒出"开始朗读"
        const area = this._dom.contentArea;
        if (area && selection.anchorNode && !area.contains(selection.anchorNode)) return '';
        return text;
    }

    _bindContextMenu() {
        const area = this._dom.contentArea;
        if (!area || typeof createContextMenu !== 'function') return;
        // 壳的 createContextMenu 在构造时就把菜单项定死了，而这里每次右键的可用项都不同
        // （有没有选中文字、是不是调试模式）。所以只借它的定位与关闭逻辑，
        // 每次右键用它的 element 重建列表 —— 比改壳的公共组件影响面小得多。
        this._contextMenu = createContextMenu({
            items: [],
            onSelect: (action) => this._onContextAction(action),
        });
        area.addEventListener('contextmenu', async (event) => {
            if (!this._isReaderMode) return;
            // 链接上保留浏览器原生菜单（复制链接地址仍有用）
            if (event.target.closest('a')) return;
            event.preventDefault();
            const selected = this._selectionText();
            const debug = await this._debugEnabled();
            // **在右键这一刻就把锚点算出来存下**：菜单从打开到被点击之间，选区可能
            // 因为任何原因消失（失焦、点击落在正文外、浏览器策略），那时再回头读
            // `getSelection()` 就是空的 —— 表现正是"我明明选中了，点朗读却没反应"。
            const anchor = this._resolveAnchor(selected);
            this._menuAnchor = anchor;
            this._buildContextMenuItems(!!selected, debug);
            this._contextMenu.show(event.clientX, event.clientY, { selected, anchor });
        });
    }

    /**
     * 决定"从哪儿开始念/记书签"：优先用选区，没有选区就退回视口顶部那一句。
     *
     * 退回而不是拒绝，是因为"选中"这件事在不同机器上并不可靠（选区可能在右键的瞬间
     * 就没了）。宁可读当前屏幕上那一句，也不要给一个"没反应"的结果。
     */
    _resolveAnchor(selectedText) {
        if (selectedText) {
            const fromSelection = this.tts._selectionAnchor();
            if (fromSelection) return fromSelection;
        }
        return this.engine.anchorAtViewportTop();
    }

    _buildContextMenuItems(hasSelection, debug) {
        const items = [];
        if (hasSelection) {
            items.push({ label: '复制', action: 'copy' });
            items.push({ label: '开始朗读（选中处）', action: 'speak' });
            items.push({ label: '添加书签', action: 'bookmark' });
            items.push({ label: '——', action: '' });
        } else {
            items.push({ label: '开始朗读（屏幕顶部那一句）', action: 'speak' });
            items.push({ label: '——', action: '' });
        }
        items.push({ label: '刷新', action: 'reload' });
        if (debug) items.push({ label: '检查', action: 'inspect' });

        const menu = this._contextMenu.element;
        menu.innerHTML = '';
        items.forEach((item) => {
            const li = document.createElement('li');
            if (!item.action) {
                li.className = 'nr-menu-sep';
                li.textContent = '';
            } else {
                li.textContent = item.label;
                li.dataset.action = item.action;
                li.addEventListener('click', () => {
                    menu.style.display = 'none';
                    this._onContextAction(item.action);
                });
            }
            menu.appendChild(li);
        });
    }

    async _debugEnabled() {
        if (this._debugFlag !== undefined) return this._debugFlag;
        this._debugFlag = false;
        try {
            const config = await Bridge.callSystem('system_get_config');
            this._debugFlag = !!(config && config.debug && config.debug.status_debug);
        } catch (e) {
            this._debugFlag = false;
        }
        return this._debugFlag;
    }

    async _onContextAction(action) {
        const target = (this._contextMenu && this._contextMenu.getTargetData()) || {};
        const selected = target.selected || this._selectionText();
        // 锚点在右键那一刻就算好了（见 _bindContextMenu）：这里现算是兜底
        const anchor = target.anchor || this._resolveAnchor(selected);
        switch (action) {
            case 'copy':
                try {
                    await navigator.clipboard.writeText(selected);
                    Toast.success('已复制');
                } catch (e) {
                    Toast.error('复制失败');
                }
                break;
            case 'reload':
                await this.reloadDocument();
                break;
            case 'speak':
                if (!anchor) {
                    Toast.info('正文章节还没就绪');
                    break;
                }
                if (!target.selected) Toast.info('没有选中文字，从屏幕顶部那一句开始');
                this.tts.startFromAnchor(anchor);
                break;
            case 'bookmark':
                await this.addBookmark(anchor);
                break;
            case 'inspect':
                this._openDevTools();
                break;
            default:
                break;
        }
    }

    _openDevTools() {
        // WebView 里没有 F12：pywebview 提供窗口级 devtools 入口，浏览器模式下
        // 只能提示用户用宿主自身的开发者工具（同源 iframe 里右键 → 检查）。
        try {
            if (window.pywebview && window.pywebview.api && window.pywebview.api.system_open_devtools) {
                window.pywebview.api.system_open_devtools();
                return;
            }
            if (window.parent && window.parent !== window && window.parent.pywebview) return;
            Toast.info('请在宿主窗口使用开发者工具（本插件运行在同源 iframe 内）');
        } catch (e) {
            Toast.info('开发者工具不可用');
        }
    }

    // ===== 书签 =====

    /** 当前视口顶部那一句的起点（朗读与书签共用的定位法则）。 */
    _anchorFromViewport() {
        return this.engine.anchorAtViewportTop();
    }

    async addBookmark(anchor = null) {
        const target = anchor || this._resolveAnchor(this._selectionText());
        if (!target) {
            Toast.info('正文章节还没就绪');
            return;
        }
        try {
            // 书签是唯一要字级位置的状态（阅读进度只记到章），见设计文档 §4
            const result = await Bridge.call('marks_add', this.currentDocument.id,
                target.chapter, target.char, target.snippet, '');
            if (result && result.success) {
                this.marks[this.currentDocument.id] = result.marks || [];
                Toast.success(`已添加书签（第 ${target.chapter + 1} 章）`);
                this._updateStatus();
                // 把刚加的这一章一起画出来：它可能不在视口 35% 处（用户选的是上面那一章）
                this._flashMarks(target.chapter);
            } else {
                Toast.error((result && result.error) || '添加书签失败');
            }
        } catch (e) {
            Toast.error('添加书签失败');
        }
    }

    /** 跳到书签：切章 + 把该字滚进视野（snippet 校验在 engine 里做）。 */
    async goToMark(documentId, mark) {
        if (!this.currentDocument || this.currentDocument.id !== documentId) {
            await this.openDocument(documentId, { chapter: mark.chapter });
        } else {
            if (mark.chapter !== this.engine.currentChapterIndex) {
                await this.engine.goToChapter(mark.chapter, 0);
            }
            this.tts.stop();
        }
        await this.engine.scrollToChar(mark.char, mark.snippet || '');
        this._afterPageRender();
    }

    /**
     * 正文里的书签标记：给**当前章**每一个书签画一个"浮动书签"。
     *
     * 用一个覆盖层按 Range 的矩形定位，**不往正文里插 DOM** —— 滚动模式下章节节点
     * 会被真的删掉，插进去的标记要么留在已删除的节点里，要么在重排后错位。
     *
     * `fallbackChapter` 是"刚加书签的那一章"：滚动模式下窗口里挂着 2~3 章，用户完全
     * 可能在上面那一章选中文字加书签，而 `currentChapterIndex` 指的是视口 35% 处那一章。
     * 传了它就能把刚加的标记画出来（否则加完书签屏幕上什么都不出现，像没生效）。
     */
    _flashMarks(fallbackChapter = null) {
        const layer = this._markLayer();
        layer.innerHTML = '';
        const area = this._dom.contentArea;
        const docId = this.currentDocument && this.currentDocument.id;
        if (!area || !docId) return;
        const markDoc = this.marks[docId] || [];
        const wanted = fallbackChapter === null
            ? [this.engine.currentChapterIndex]
            : [this.engine.currentChapterIndex, fallbackChapter];
        const marks = markDoc.filter((mark) => wanted.includes(mark.chapter));
        if (!marks.length) return;
        const areaRect = area.getBoundingClientRect();
        marks.forEach((mark) => {
            const chapterEl = this.engine.chapterElement(mark.chapter);
            if (!chapterEl) return;
            const range = this.engine._rangeFor(chapterEl, mark.char,
                mark.char + Math.max(1, (mark.snippet || '').length));
            const rect = range && range.getClientRects()[0];
            if (!rect) return;
            const pin = document.createElement('div');
            pin.className = 'nr-mark-pin';
            pin.dataset.chapter = String(mark.chapter);
            pin.title = mark.snippet || '书签';
            pin.style.top = `${rect.top - areaRect.top + area.scrollTop + rect.height / 2}px`;
            pin.addEventListener('click', () => {
                this.engine.scrollToChar(mark.char, mark.snippet || '');
            });
            layer.appendChild(pin);
        });
    }

    _markLayer() {
        const area = this._dom.contentArea;
        let layer = document.getElementById('nr-mark-layer');
        if (!layer) {
            layer = document.createElement('div');
            layer.id = 'nr-mark-layer';
            layer.className = 'nr-mark-layer';
            area.appendChild(layer);
        }
        return layer;
    }

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
