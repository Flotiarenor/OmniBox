// ============================================================
// 文档阅读器 — 双模式引擎
// - page  翻页模式：同一章内自然滚动，章节之间用按钮/目录切换
// - scroll 滚动模式：跨章节连续滚动（append/prepend + 3 章窗口）
// 滚动容器统一禁用 scroll anchoring，补偿全部由本引擎完成。
// ============================================================
class DocumentReaderEngine {
    constructor(app) {
        this.app = app;
        this.mode = 'page'; // 'page' | 'scroll'

        this.documentId = '';
        this.chapters = [];
        this.encoding = 'auto';
        this.currentChapterIndex = 0;

        // 章节 HTML 缓存
        this.chapterHtmlCache = new Map();
        this._htmlPromises = new Map();
        this._prefetching = new Set();

        // 滚动模式窗口
        this.loadedStart = -1;
        this.loadedEnd = -1;
        this._loadingNext = false;
        this._loadingPrev = false;
        this._token = 0;
        this._busy = false;
        this._suppressUntil = 0;
    }

    get contentArea() {
        return this.app._dom.contentArea;
    }

    setMode(mode) {
        this.mode = mode === 'scroll' ? 'scroll' : 'page';
    }

    reset(documentId, chapters, encoding) {
        this.documentId = documentId;
        this.chapters = chapters || [];
        this.encoding = encoding || 'auto';
        this.currentChapterIndex = 0;
        this.chapterHtmlCache.clear();
        this._htmlPromises.clear();
        this._prefetching.clear();
        this.loadedStart = -1;
        this.loadedEnd = -1;
        this._token++;
    }

    // ============================================================
    // 章节 HTML 缓存与预取
    // ============================================================
    _getChapterHtml(index) {
        if (this.chapterHtmlCache.has(index)) {
            return Promise.resolve(this.chapterHtmlCache.get(index));
        }
        if (this._htmlPromises.has(index)) return this._htmlPromises.get(index);
        const promise = Bridge.call('document_get_content', this.documentId, index, this.encoding)
            .then(result => {
                // 后端给两种内容：txt 是纯文本（这里转义成段落），md/epub 已经是后端
                // 白名单转换器产出的 HTML 片段，直接插入即可。
                const html = result.error
                    ? ''
                    : (result.format === 'html'
                        ? (result.content || '')
                        : DocumentUtils.formatContent(result.content || ''));
                this.chapterHtmlCache.set(index, html);
                this._htmlPromises.delete(index);
                return html;
            })
            .catch(e => {
                console.error('加载章节失败:', e);
                this._htmlPromises.delete(index);
                return '';
            });
        this._htmlPromises.set(index, promise);
        return promise;
    }

    _prefetchChapter(index) {
        if (index < 0 || index >= this.chapters.length) return;
        if (this.chapterHtmlCache.has(index) || this._prefetching.has(index)) return;
        this._prefetching.add(index);
        this._getChapterHtml(index).finally(() => this._prefetching.delete(index));
    }

    // ============================================================
    // 章节切换（两种模式通用：整章替换，不保留跨章拼接）
    // ============================================================
    async goToChapter(index, fraction = 0) {
        if (index < 0 || index >= this.chapters.length) return false;
        const token = ++this._token;
        this._busy = true;

        const html = await this._getChapterHtml(index);
        if (token !== this._token) { this._busy = false; return false; }

        const el = this.contentArea;
        el.innerHTML = '';
        this._appendChapterDiv(index, html);

        this.loadedStart = index;
        this.loadedEnd = index;
        this.currentChapterIndex = index;

        const range = el.scrollHeight - el.clientHeight;
        el.scrollTop = Math.max(0, Math.min(1, Number(fraction) || 0)) * range;
        requestAnimationFrame(() => {
            const r = el.scrollHeight - el.clientHeight;
            el.scrollTop = Math.max(0, Math.min(1, Number(fraction) || 0)) * r;
        });

        this._prefetchChapter(index - 1);
        this._prefetchChapter(index + 1);
        this._busy = false;
        // 滚动模式下刚打开的这一章可能填不满一屏（EPUB / Markdown 的小章节很常见），
        // 此时容器根本滚不动、浏览器不会产生滚动事件 —— 不主动补满就是"只显示一章、
        // 再也滑不动，只能用目录跳章"。
        if (this.mode === 'scroll') await this._fillViewport();
        return true;
    }

    async nextPage() {
        if (this.currentChapterIndex >= this.chapters.length - 1) return 'end';
        const ok = await this.goToChapter(this.currentChapterIndex + 1, 0);
        return ok ? 'moved' : 'busy';
    }

    async prevPage() {
        if (this.currentChapterIndex <= 0) return 'end';
        const ok = await this.goToChapter(this.currentChapterIndex - 1, 1);
        return ok ? 'moved' : 'busy';
    }

    // ============================================================
    // 滚动模式：连续滚动加载
    // ============================================================
    /**
     * 处理一次滚动。返回 Promise（调用方据此在加载完成后刷新界面）。
     *
     * 三处踩过的坑：
     *   - 以前只在"加载相邻章"时才同步当前章，于是章内滚动/滚到下一章时，左侧目录
     *     高亮、工具栏标题、进度条全都停在跳转前那一章不动；
     *   - 以前没有任何"补满视图"的动作：短文（EPUB / Markdown 常见的小章节）三章
     *     都填不满一屏时浏览器不会产生滚动事件，加载就此停死，只能靠点目录跳章；
     *   - 滚动到底后如果没有新事件，就再也没有下一次加载的机会。
     */
    handleScroll() {
        if (this.mode !== 'scroll') return Promise.resolve(false);
        const el = this.contentArea;
        if (!el || this._busy) return Promise.resolve(false);

        this._syncChapterFromScroll();
        if (performance.now() < this._suppressUntil) return Promise.resolve(false);

        const nearBottom = el.scrollHeight - el.scrollTop - el.clientHeight < 300;
        const nearTop = el.scrollTop < 200;

        if (nearBottom && !this._loadingNext && this.loadedEnd < this.chapters.length - 1) {
            this._loadingNext = true;
            return this._loadAdjacent(this.loadedEnd + 1, 'append')
                .then((loaded) => (loaded ? this._fillViewport() : false))
                .finally(() => { this._loadingNext = false; });
        } else if (nearTop && !this._loadingPrev && this.loadedStart > 0) {
            this._loadingPrev = true;
            return this._loadAdjacent(this.loadedStart - 1, 'prepend')
                .finally(() => { this._loadingPrev = false; });
        }
        return Promise.resolve(false);
    }

    /**
     * 把内容补到"能滚"为止：一章只有几行时，三章窗口填不满一屏 → 没有滚动事件 →
     * 永远不会再加载下一章。
     */
    async _fillViewport() {
        const el = this.contentArea;
        let guard = 0;
        while (this.loadedEnd < this.chapters.length - 1 && guard < 60) {
            if (el.scrollHeight > el.clientHeight + 120) break;
            guard++;
            const loaded = await this._loadAdjacent(this.loadedEnd + 1, 'append');
            if (!loaded) break;
        }
        return true;
    }

    /** 返回是否真的追加/插入了内容（调用方据此决定要不要继续补满视图）。 */
    async _loadAdjacent(index, mode) {
        const token = this._token;
        const html = await this._getChapterHtml(index);
        if (!html || token !== this._token || this._busy) return false;
        if (mode === 'append' && index !== this.loadedEnd + 1) return false;
        if (mode === 'prepend' && index !== this.loadedStart - 1) return false;

        const el = this.contentArea;
        if (mode === 'append') {
            this._appendChapterDiv(index, html, true);
            this.loadedEnd = index;
            this._trimTop();
        } else {
            const before = el.scrollHeight;
            this._prependChapterDiv(index, html);
            this.loadedStart = index;
            // 手动补偿（scroll anchoring 已禁用，浏览器不会再动 scrollTop）
            el.scrollTop += el.scrollHeight - before;
            this._suppressUntil = performance.now() + 300;
            this._trimBottom();
        }
        this._syncChapterFromScroll();
        this._prefetchChapter(mode === 'append' ? this.loadedEnd + 1 : this.loadedStart - 1);
        return true;
    }

    _appendChapterDiv(index, html, withSeparator = false) {
        const el = this.contentArea;
        if (withSeparator) {
            const sep = document.createElement('div');
            sep.className = 'chapter-separator';
            sep.style.height = '2em';
            el.appendChild(sep);
        }
        const div = document.createElement('div');
        div.className = 'chapter-content';
        div.dataset.chapterIndex = String(index);
        div.innerHTML = html;
        el.appendChild(div);
    }

    _prependChapterDiv(index, html) {
        const el = this.contentArea;
        const div = document.createElement('div');
        div.className = 'chapter-content';
        div.dataset.chapterIndex = String(index);
        div.innerHTML = html;
        const sep = document.createElement('div');
        sep.className = 'chapter-separator';
        sep.style.height = '2em';
        const fragment = document.createDocumentFragment();
        fragment.appendChild(div);
        fragment.appendChild(sep);
        el.insertBefore(fragment, el.firstChild);
    }

    _trimTop() {
        const el = this.contentArea;
        while (this.loadedEnd - this.loadedStart > 2 && el.firstElementChild) {
            const chapter = el.firstElementChild;
            const sep = chapter.nextElementSibling;
            const sepHeight = sep && sep.classList.contains('chapter-separator') ? sep.offsetHeight : 0;
            const removed = chapter.offsetHeight + sepHeight;
            // 删掉之后还得留下"一屏 + 一点余量"：小章节（EPUB / Markdown 常见，一章
            // 只有几行）删早了会让内容永远填不满视图，表现就是"显示不全、再也滑不动"。
            if (el.scrollHeight - removed < el.clientHeight + 200) break;
            // 视图里还看得见这一章时不要删（删了内容会整体上移）
            if (el.scrollTop < removed) break;
            el.scrollTop -= removed;
            chapter.remove();
            if (sep && sep.classList.contains('chapter-separator')) sep.remove();
            this.loadedStart++;
        }
    }

    _trimBottom() {
        const el = this.contentArea;
        while (this.loadedEnd - this.loadedStart > 2 && el.lastElementChild) {
            const chapter = el.lastElementChild;
            const prev = chapter.previousElementSibling;
            chapter.remove();
            if (prev && prev.classList.contains('chapter-separator')) prev.remove();
            this.loadedEnd--;
        }
        if (el.scrollTop > el.scrollHeight - el.clientHeight) {
            el.scrollTop = Math.max(0, el.scrollHeight - el.clientHeight);
        }
    }

    /** 按视口 35% 处所在的分章块判断"当前章"，返回是否发生了变化。 */
    _syncChapterFromScroll() {
        const el = this.contentArea;
        const target = el.scrollTop + el.clientHeight * 0.35;
        let current = this.currentChapterIndex;
        el.querySelectorAll('.chapter-content').forEach(div => {
            const top = div.offsetTop;
            if (top <= target) current = parseInt(div.dataset.chapterIndex, 10);
        });
        const changed = current !== this.currentChapterIndex;
        this.currentChapterIndex = current;
        return changed;
    }

    /**
     * 当前章的阅读进度（0~1）。
     *
     * 以前用的是整个滚动容器的比例，可滚动模式下容器里同时挂着 2~3 章，
     * 于是进度条与保存的位置都按"三章窗口"算，跳章/重开就会落到莫名其妙的地方。
     */
    chapterFraction() {
        const el = this.contentArea;
        const node = el.querySelector(`.chapter-content[data-chapter-index="${this.currentChapterIndex}"]`);
        if (node && node.offsetHeight > 0) {
            const within = (el.scrollTop - node.offsetTop) / node.offsetHeight;
            return Math.max(0, Math.min(1, within));
        }
        const range = el.scrollHeight - el.clientHeight;
        if (range <= 0) return 0;
        return Math.max(0, Math.min(1, el.scrollTop / range));
    }
}
