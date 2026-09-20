// ============================================================
// 文档阅读器 — 书架分片：封面网格 + 书签视图
//
// `DocumentReader.prototype` 的分片：加载顺序是硬约束（必须排在 app.js 之后、
// 实例化之前），见 index.html。
// ============================================================
// 图标值（`icon:名字`）→ 标记。sprite 与 Icons.html 由壳注入的 /shell/icons.generated.js 提供；
// 缺失时（本页脱离壳单独打开）返回空串，不写 emoji 兜底。
const nrShelfIcon = (name) => (window.Icons && typeof window.Icons.html === 'function')
    ? window.Icons.html(name)
    : '';

Object.assign(DocumentReader.prototype, {
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
                keyword ? 'icon:search' : 'icon:book-open',
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
    },

    _renderMarks(grid) {
        const groups = this.documents
            .map((doc) => ({ doc, marks: this.marks[doc.id] || [] }))
            .filter((item) => item.marks.length);
        const total = groups.reduce((sum, item) => sum + item.marks.length, 0);
        this._dom.viewSub.textContent = `${total} 条 · 来自 ${groups.length} 本`;
        if (!total) {
            grid.innerHTML = this._emptyHtml('icon:star', '还没有书签',
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
                        <button type="button" class="nr-mark-del" title="删除这条书签">${nrShelfIcon('icon:x')}</button>
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
    },

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
    },

    _emptyHtml(icon, text, hint = '') {
        // 结构走壳的 .empty-state（base.css）；`.nr-empty-span` 只负责网格里跨列占位。
        // 图标走壳的图标集：传 `icon:名字`。`window.Icons` 缺失时（插件页脱离壳单独打开）
        // 返回空串，不写 emoji 兜底。
        const iconHtml = window.Icons ? window.Icons.html(icon, 'empty-state-icon') : '';
        return `<div class="empty-state nr-empty-span">
            <div class="empty-state-icon">${iconHtml}</div>
            <div class="empty-state-text">${Utils.escapeHtml(text)}</div>
            ${hint ? `<div class="empty-state-hint">${Utils.escapeHtml(hint)}</div>` : ''}
        </div>`;
    },

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
                    ${markCount ? `<span class="nr-mark-count">${nrShelfIcon('icon:star')} ${markCount}</span>` : ''}
                    ${percent ? `<div class="nr-cover-progress"><span style="width:${percent}%"></span></div>` : ''}
                </div>
                <div class="nr-card-body">
                    <div class="nr-card-title" title="${Utils.escapeHtml(doc.title || doc.id)}">${Utils.escapeHtml(doc.title || doc.id)}</div>
                    <div class="nr-card-sub">${Utils.escapeHtml([doc.author, meta].filter(Boolean).join(' · '))}</div>
                </div>
            </div>`;
    },
});
