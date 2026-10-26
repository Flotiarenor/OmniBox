/**
 * 漫画单页阅读器。
 *
 * 优先交给壳注入的共享灯箱 `createLightbox`：计数、点击/Enter 翻页、键盘左右、
 * Esc 关闭与关闭后的焦点归还都由它实现，本类只提供漫画页序列、章节/页码信息
 * 和页面卡片到灯箱的焦点交接。共享组件缺失或初始化失败时，退回本页自带的
 * `.manga-reader` 覆盖层，行为与迁移前一致（页面不会因此打不开）。
 */
class MangaReader {
    constructor() {
        this.container = document.getElementById('manga-reader-container');
        this.pages = [];
        this.currentIndex = 0;
        this._lightbox = null;
        this._mode = null;              // 'lightbox' | 'fallback'
        this._returnFocus = null;       // 旧覆盖层的焦点归还目标
        this._chapterLabel = '';

        // 点页码卡片时先把焦点交给它：共享灯箱在 show() 时记住当前焦点，
        // 关闭后据此把焦点还回这张卡片（鼠标点 div 本身不会产生焦点）。
        this._onPageCardClick = (event) => {
            const card = event.target && event.target.closest
                ? event.target.closest('.ml-card[data-page]')
                : null;
            if (!card) return;
            if (!card.hasAttribute('tabindex')) card.setAttribute('tabindex', '0');
            try {
                card.focus({ preventScroll: true });
            } catch (e) {
                try { card.focus(); } catch (e2) { /* 无 focus 的桩环境 */ }
            }
        };
        document.addEventListener('click', this._onPageCardClick, true);

        this._bindEvents();
    }

    open(pages, startIndex = 0) {
        this.pages = Array.isArray(pages) ? pages.slice() : [];
        if (this.pages.length === 0) {
            this.close();
            return;
        }
        this.currentIndex = Math.max(0, Math.min(startIndex || 0, this.pages.length - 1));
        this._readContext();
        this._returnFocus = this._currentFocus();

        if (this._openLightbox()) return;
        this._openFallback();
    }

    /** 阅读器是否处于打开状态（共享灯箱或旧覆盖层）。
        壳顶栏的返回与左栏导航在阅读器之上仍可点击，调用方据此先关闭最内层。 */
    isOpen() {
        return this._mode !== null;
    }

    close() {
        const mode = this._mode;
        this._mode = null;
        if (mode === 'lightbox' && this._lightbox) {
            try { this._lightbox.hide(); } catch (e) { /* 已经关闭 */ }
            if (typeof this._lightbox.getIndex === 'function') {
                this.currentIndex = this._lightbox.getIndex();
            }
        } else if (mode === 'fallback' && this._returnFocus) {
            const target = this._returnFocus;
            if (target.isConnected !== false && typeof target.focus === 'function') {
                try {
                    target.focus({ preventScroll: true });
                } catch (e) {
                    try { target.focus(); } catch (e2) { /* 元素已失效 */ }
                }
            }
        }
        this._returnFocus = null;
        this.container.style.display = 'none';
        const img = this.container.querySelector('img');
        if (img) img.src = '';
        this.pages = [];
    }

    prev() {
        if (this._mode === 'lightbox' && this._lightbox) {
            this._lightbox.navigate(-1);
            this.currentIndex = this._lightbox.getIndex();
            return;
        }
        if (this.currentIndex > 0) {
            this.currentIndex--;
            this.renderPage();
        }
    }

    next() {
        if (this._mode === 'lightbox' && this._lightbox) {
            this._lightbox.navigate(1);
            this.currentIndex = this._lightbox.getIndex();
            return;
        }
        if (this.currentIndex < this.pages.length - 1) {
            this.currentIndex++;
            this.renderPage();
        }
    }

    // ============================================================
    // 共享灯箱
    // ============================================================

    /** 共享灯箱缺失、页面不是本地文件路径或初始化失败时返回 null，由调用方回退。 */
    _ensureLightbox() {
        if (this._lightbox) return this._lightbox;
        if (typeof createLightbox !== 'function') return null;
        try {
            this._lightbox = createLightbox({
                counter: true,
                advanceOnClick: true,
                labels: {
                    dialog: '漫画阅读器',
                    image: '漫画页',
                    previous: '上一页',
                    next: '下一页',
                    close: '关闭阅读',
                    counter: '第 {current} / {total} 页',
                },
                getImageUrl: (item) => item.url,
                infoProvider: (item, index, items) => this._pageInfo(index, items.length),
            });
        } catch (e) {
            this._lightbox = null;
        }
        return this._lightbox;
    }

    _openLightbox() {
        // 共享灯箱无条件用 Bridge.originalUrl 包装路径；http 页面仍交给旧覆盖层。
        if (this.pages.some(url => /^https?:/i.test(String(url || '')))) return false;
        const lightbox = this._ensureLightbox();
        if (!lightbox) return false;
        try {
            this.container.style.display = 'none';
            lightbox.show(this.pages.map(url => ({ url })), this.currentIndex);
            this._mode = 'lightbox';
            return true;
        } catch (e) {
            try { lightbox.hide(); } catch (e2) { /* 未成功打开 */ }
            this._lightbox = null;
            return false;
        }
    }

    _pageInfo(index, total) {
        const lines = [];
        if (this._chapterLabel) lines.push(`章节：${this._chapterLabel}`);
        lines.push(`第 ${index + 1} / ${total} 页`);
        return lines;
    }

    /** 从详情页头部取当前章节名，供灯箱信息区使用。 */
    _readContext() {
        const chapterEl = document.querySelector('.ml-detail-author');
        this._chapterLabel = chapterEl ? chapterEl.textContent.trim() : '';
    }

    _currentFocus() {
        const el = document.activeElement;
        return el && el !== document.body ? el : null;
    }

    // ============================================================
    // 回退：本页自带的 `.manga-reader` 覆盖层
    // ============================================================

    _openFallback() {
        this._mode = 'fallback';
        this.container.style.display = 'flex';
        this.renderPage();
    }

    renderPage() {
        const img = this.container.querySelector('img');
        if (img && this.currentIndex >= 0 && this.currentIndex < this.pages.length) {
            const pageUrl = this.pages[this.currentIndex];
            img.src = pageUrl.startsWith('http') ? pageUrl : Bridge.originalUrl(pageUrl);
        }

        const info = this.container.querySelector('.reader-info');
        if (info) info.textContent = `${this.currentIndex + 1} / ${this.pages.length}`;
    }

    _bindEvents() {
        this.container.querySelector('.reader-close').addEventListener('click', () => this.close());
        this.container.querySelector('.reader-prev').addEventListener('click', () => this.prev());
        this.container.querySelector('.reader-next').addEventListener('click', () => this.next());

        // 鼠标左键点击阅读区 -> 向后翻页（仅回退覆盖层会显示并命中）
        const wrapper = this.container.querySelector('.reader-image-wrapper');
        wrapper.addEventListener('click', (e) => {
            if (e.target.closest('.reader-arrow')) return;
            this.next();
        });

        document.addEventListener('keydown', (e) => {
            if (this.container.style.display !== 'flex') return;
            if (e.target && (e.target.tagName === 'INPUT' || e.target.tagName === 'TEXTAREA')) return;
            if (e.key === 'ArrowLeft' || e.key === 'ArrowUp' || e.key === 'a' || e.key === 'w') this.prev();
            if (e.key === 'ArrowRight' || e.key === 'ArrowDown' || e.key === 'd' || e.key === 's') this.next();
            if (e.key === ' ' || e.key === 'Spacebar') {
                e.preventDefault();
                this.next();
            }
            if (e.key === 'Escape') this.close();
        });
    }
}
