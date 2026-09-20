// ============================================================
// 文档阅读器 — 目录弹窗分片
//
// `DocumentReader.prototype` 的分片：加载顺序是硬约束（必须排在 app.js 之后、
// 实例化之前），见 index.html。
// ============================================================
// 图标值（`icon:名字`）→ 标记。sprite 与 Icons.html 由壳注入的 /shell/icons.generated.js 提供；
// 缺失时（本页脱离壳单独打开）返回空串，不写 emoji 兜底。
const nrTocIcon = (name) => (window.Icons && typeof window.Icons.html === 'function')
    ? window.Icons.html(name)
    : '';

Object.assign(DocumentReader.prototype, {
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
    },

    _updateTocSub() {
        if (!this._dom.tocSub) return;
        const total = this.engine.chapters.length;
        if (!total) {
            this._dom.tocSub.textContent = '';
            return;
        }
        this._dom.tocSub.textContent = ` ${this.engine.currentChapterIndex + 1} / ${total}`;
    },

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
            list.innerHTML = this._emptyHtml('icon:search', '没有匹配的章节');
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
                        ${marks.length ? `<span class="nr-toc-mark" title="${Utils.escapeHtml(marks[0].snippet || '')}">${nrTocIcon('icon:star')}</span>` : ''}
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
    },
});
