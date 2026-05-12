class MangaReader {
    constructor() {
        this.container = document.getElementById('manga-reader-container');
        this.pages = [];
        this.currentIndex = 0;
        this._bindEvents();
    }

    open(pages, startIndex = 0) {
        this.pages = pages;
        this.currentIndex = startIndex || 0; 
        this.container.style.display = 'flex';
        this.renderPage();
    }

    close() {
        this.container.style.display = 'none';
        this.container.querySelector('img').src = '';
    }

    renderPage() {
        const img = this.container.querySelector('img');
        
        if (this.currentIndex >= 0 && this.currentIndex < this.pages.length) {
            const pageUrl = this.pages[this.currentIndex];
            // 优化：如果已经是完整URL则直接使用，否则拼接 FILE_SERVER
            img.src = pageUrl.startsWith('http') ? pageUrl : (bridge.FILE_SERVER || '') + pageUrl;
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

        document.addEventListener('keydown', (e) => {
            if (this.container.style.display !== 'flex') return;
            if (e.key === 'ArrowLeft' || e.key === 'a') this.prev();
            if (e.key === 'ArrowRight' || e.key === 'd') this.next();
            if (e.key === 'Escape') this.close();
        });
    }
}