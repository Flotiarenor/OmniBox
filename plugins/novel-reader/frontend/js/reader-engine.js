// ============================================================
// 小说阅读器 — 双模式引擎
// - page  翻页模式：同一章内自然滚动，章节之间用按钮/目录切换
// - scroll 滚动模式：跨章节连续滚动（append/prepend + 3 章窗口）
// 滚动容器统一禁用 scroll anchoring，补偿全部由本引擎完成。
// ============================================================
class NovelReaderEngine {
    constructor(app) {
        this.app = app;
        this.mode = 'page'; // 'page' | 'scroll'

        this.novelId = '';
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

    reset(novelId, chapters, encoding) {
        this.novelId = novelId;
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
        const promise = Bridge.call('novel_get_content', this.novelId, index, this.encoding)
            .then(result => {
                const html = result.error ? '' : NovelUtils.formatContent(result.content || '');
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
    handleScroll() {
        if (this.mode !== 'scroll') return;
        const el = this.contentArea;
        if (!el || this._busy) return;
        if (performance.now() < this._suppressUntil) return;

        const nearBottom = el.scrollHeight - el.scrollTop - el.clientHeight < 300;
        const nearTop = el.scrollTop < 200;

        if (nearBottom && !this._loadingNext && this.loadedEnd < this.chapters.length - 1) {
            this._loadingNext = true;
            this._loadAdjacent(this.loadedEnd + 1, 'append').finally(() => { this._loadingNext = false; });
        } else if (nearTop && !this._loadingPrev && this.loadedStart > 0) {
            this._loadingPrev = true;
            this._loadAdjacent(this.loadedStart - 1, 'prepend').finally(() => { this._loadingPrev = false; });
        }
    }

    async _loadAdjacent(index, mode) {
        const token = this._token;
        const html = await this._getChapterHtml(index);
        if (!html || token !== this._token || this._busy) return;
        if (mode === 'append' && index !== this.loadedEnd + 1) return;
        if (mode === 'prepend' && index !== this.loadedStart - 1) return;

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
            const removed = chapter.offsetHeight + (sep ? sep.offsetHeight : 0);
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

    _syncChapterFromScroll() {
        const el = this.contentArea;
        const target = el.scrollTop + el.clientHeight * 0.35;
        let current = this.currentChapterIndex;
        el.querySelectorAll('.chapter-content').forEach(div => {
            const top = div.offsetTop;
            if (top <= target) current = parseInt(div.dataset.chapterIndex, 10);
        });
        this.currentChapterIndex = current;
    }

    chapterFraction() {
        const el = this.contentArea;
        const range = el.scrollHeight - el.clientHeight;
        if (range <= 0) return 0;
        return Math.max(0, Math.min(1, el.scrollTop / range));
    }
}
