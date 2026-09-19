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

    // ============================================================
    // 正文坐标系（朗读与书签共用）
    //
    // 字符偏移的坐标系就是 `chapterEl.textContent`：与后端切句用的是同一份文本，
    // 与书签存的 `char` 也是同一个数，因此"高亮的那一句"、"书签记的那个字"、
    // "朗读起点"三者天然对齐，不需要任何换算。
    //
    // 注意**不能缓存 DOM 节点或 Range**：滚动模式下 2~3 章共存且会被 _trimTop /
    // _trimBottom 真的删掉，缓存下来的引用会指向已移除的节点（Range.setStart 直接抛）。
    // 所以下面每个方法都是"当场遍历、当场用完"。
    // ============================================================

    /** 当前章的正文元素（可能为空：pdf / 加载中）。 */
    chapterElement(index = this.currentChapterIndex) {
        const el = this.contentArea;
        if (!el) return null;
        return el.querySelector(`.chapter-content[data-chapter-index="${index}"]`);
    }

    /** 给朗读用：当前章的文本、DOM 与字符区间。 */
    getChapterContext(index = this.currentChapterIndex) {
        const el = this.chapterElement(index);
        if (!el) return null;
        return {
            chapter: index,
            element: el,
            text: el.textContent || '',
            startChar: 0,
        };
    }

    /** 收集章节里所有非空文本节点（文档顺序）。 */
    static _textNodes(root) {
        const nodes = [];
        if (!root) return nodes;
        const walker = document.createTreeWalker(root, NodeFilter.SHOW_TEXT);
        let node = walker.nextNode();
        while (node) {
            if (node.nodeValue) nodes.push(node);
            node = walker.nextNode();
        }
        return nodes;
    }

    /** 字符偏移 → (文本节点, 节点内偏移)。越界时夹到末尾。 */
    static _pointAt(root, offset) {
        const nodes = DocumentReaderEngine._textNodes(root);
        if (!nodes.length) return null;
        let remaining = Math.max(0, offset | 0);
        for (const node of nodes) {
            const length = node.nodeValue.length;
            if (remaining <= length) return { node, offset: remaining };
            remaining -= length;
        }
        const last = nodes[nodes.length - 1];
        return { node: last, offset: last.nodeValue.length };
    }

    /** 字符区间 → Range（拿不到就返回 null）。 */
    _rangeFor(root, start, end) {
        const from = DocumentReaderEngine._pointAt(root, start);
        const to = DocumentReaderEngine._pointAt(root, end);
        if (!from || !to) return null;
        try {
            const range = document.createRange();
            range.setStart(from.node, from.offset);
            range.setEnd(to.node, to.offset);
            return range;
        } catch (e) {
            return null;
        }
    }

    /** 视口顶部对应的字符偏移：二分查找第一个落在视口线以下的字符。 */
    _charAtViewportTop(el, root) {
        const line = el.getBoundingClientRect().top + 2;
        let low = 0;
        let high = root.textContent.length;
        while (low < high) {
            const mid = (low + high) >> 1;
            const range = this._rangeFor(root, mid, Math.min(mid + 2, root.textContent.length));
            const rect = range && range.getClientRects()[0];
            if (!rect || rect.bottom >= line) high = mid;
            else low = mid + 1;
        }
        return low;
    }

    /** 句子起点：往前找句末标点（与后端 split_text 的切点同一套字符）。 */
    static sentenceStart(text, offset) {
        const stops = '。！？；…!?;\n';
        let index = Math.max(0, Math.min(offset, text.length));
        while (index > 0 && !stops.includes(text[index - 1])) index -= 1;
        return index;
    }

    /**
     * 视口顶部那一句的锚点：`{chapter, char, snippet}`。
     *
     * 这是"开始朗读"与"添加书签"共用的定位法则 —— 用户看着哪儿，就从哪儿开始。
     */
    anchorAtViewportTop() {
        const el = this.contentArea;
        const root = this.chapterElement();
        if (!el || !root) return null;
        const text = root.textContent || '';
        if (!text) return null;
        const raw = this._charAtViewportTop(el, root);
        const char = DocumentReaderEngine.sentenceStart(text, raw);
        return {
            chapter: this.currentChapterIndex,
            char,
            snippet: text.slice(char, char + 16),
        };
    }

    /** 某个节点落在哪一章（滚动模式下窗口里同时挂着 2~3 章）。找不到返回 -1。 */
    chapterIndexForNode(node) {
        let current = node;
        while (current && current !== this.contentArea) {
            if (current.classList && current.classList.contains('chapter-content')) {
                return parseInt(current.dataset.chapterIndex, 10);
            }
            current = current.parentNode;
        }
        return -1;
    }

    /**
     * 选中文本的锚点：`{chapter, char, snippet}`。
     *
     * **章号由选区自己决定，不是 `currentChapterIndex`** —— 滚动模式下"当前章"是
     * 视口 35% 处那一章，而用户完全可能选上面/下面那一章的句子。以前按当前章找，
     * 选区不在那一章时直接返回 null（表现是"明明选中了，却提示请先选中一段文字"）。
     */
    anchorFromRange(range) {
        if (!range) return null;
        const root = this.contentArea;
        if (!root || !root.contains(range.startContainer)) return null;
        const chapter = this.chapterIndexForNode(range.startContainer);
        const chapterEl = this.chapterElement(chapter);
        if (!chapterEl) return null;
        const nodes = DocumentReaderEngine._textNodes(chapterEl);
        let total = 0;
        for (const node of nodes) {
            if (node === range.startContainer) {
                const offset = total + range.startOffset;
                const text = chapterEl.textContent || '';
                const char = DocumentReaderEngine.sentenceStart(text, offset);
                return { chapter, char, snippet: text.slice(char, char + 16) };
            }
            total += node.nodeValue.length;
        }
        return null;
    }

    /** 把某个字符偏移滚到视口顶部（书签跳转用）。 */
    scrollToChar(char, snippet = '') {
        const el = this.contentArea;
        const root = this.chapterElement();
        if (!el || !root) return false;
        const text = root.textContent || '';
        let offset = Math.max(0, Math.min(Number(char) || 0, text.length));
        // snippet 校验：解析器换了输出、正文节点漂移之后，按 char 直接定位会**静默**
        // 落到隔壁段落。对不上就在本章内搜一次；再找不到就退回章首 —— 宁可从头，
        // 也不落到错的地方。
        if (snippet && text.slice(offset, offset + snippet.length) !== snippet) {
            const found = text.indexOf(snippet.slice(0, 8));
            offset = found >= 0 ? found : 0;
        }
        const range = this._rangeFor(root, offset, offset + 1);
        const rect = range && range.getClientRects()[0];
        if (!rect) return false;
        el.scrollTop += rect.top - el.getBoundingClientRect().top - 8;
        return true;
    }
}
