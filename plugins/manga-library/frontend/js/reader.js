class MangaReader {
    constructor() {
        this.container = document.getElementById('manga-reader-container');
        this.pages = [];
        this.currentIndex = 0;
        this._bindEvents();
    }

    open(pages, startIndex = 0) {
        this.pages = pages || [];
        if (this.pages.length === 0) {
            this.close();
            return;
        }
        this.currentIndex = Math.max(0, Math.min(startIndex || 0, this.pages.length - 1));
        this.container.style.display = 'flex';
        this.renderPage();
    }

    close() {
        this.container.style.display = 'none';
        const img = this.container.querySelector('img');
        if (img) img.src = '';
    }

    renderPage() {
        const img = this.container.querySelector('img');

        if (this.currentIndex >= 0 && this.currentIndex < this.pages.length) {
            const pageUrl = this.pages[this.currentIndex];
            img.src = pageUrl.startsWith('http') ? pageUrl : Bridge.originalUrl(pageUrl);
        }

        const info = this.container.querySelector('.reader-info');
        info.textContent = `${this.currentIndex + 1} / ${this.pages.length}`;
    }

    prev() {
        if (this.currentIndex > 0) {
            this.currentIndex--;
            this.renderPage();
        }
    }

    next() {
        if (this.currentIndex < this.pages.length - 1) {
            this.currentIndex++;
            this.renderPage();
        }
    }

    _bindEvents() {
        this.container.querySelector('.reader-close').addEventListener('click', () => this.close());
        this.container.querySelector('.reader-prev').addEventListener('click', () => this.prev());
        this.container.querySelector('.reader-next').addEventListener('click', () => this.next());

        // 鼠标左键点击阅读区 -> 向后翻页
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
