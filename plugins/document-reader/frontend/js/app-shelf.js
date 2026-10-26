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
        // 标题/副标题归壳的顶栏；这里只准备数据，`_updateStatus()` 统一上报
        this._chromeTitle = title;
        // 读取失败优先于空分类/空书签：两者都必须有各自的文案，不能把"后端没读到"
        // 显示成"这个分类还是空的"。
        if (this._loadError) {
            this._chromeSub = '书架加载失败';
            grid.innerHTML = this._errorHtml(this._loadError);
            const retry = grid.querySelector('[data-nr-retry]');
            if (retry) retry.addEventListener('click', () => this._retryShelf(retry));
            this._updateStatus();
            return;
        }
        if (this.view === 'marks' && !keyword) {
            this._renderMarks(grid);
            return;
        }

        const items = this._visibleDocuments(keyword);
        this._chromeSub = keyword
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
        const target = this._ensureShelfGrid();
        target.innerHTML = '';
        items.forEach((doc, index) => target.appendChild(this._shelfCard(doc, index)));
        if (window.Motion) Motion.stagger(target, '.nr-card');
        // 封面异步补：EPUB 要解包封面图，逐张回填，失败不影响其它卡片
        items.forEach((doc) => {
            if (doc.kind !== 'epub') return;
            Bridge.call('document_cover', doc.id).then((result) => {
                if (seq !== this._shelfSeq) return;    // 书架已重绘，别回填到旧节点
                if (!result || !result.cover) return;
                const img = target.querySelector(`.nr-card[data-id="${CSS.escape(doc.id)}"] .nr-cover-img`);
                if (img) img.src = result.cover;
            }).catch(() => {});
        });
        this._updateStatus();
    },

    /** 书架网格：第一次渲染时升级成壳的 ContentUI.grid（#nr-grid 与 .nr-grid
        保留给插件样式与外部选择器）。壳未注入共享组件时（旧壳/独立打开）沿用
        原生的 .nr-grid 容器，卡片也退回 `_cardHtml`。 */
    _ensureShelfGrid() {
        const current = this._dom.grid;
        if (!current) return current;
        const ui = window.ContentUI;
        if (!ui || typeof ui.grid !== 'function') return current;
        if (current.classList.contains('cui-grid')) return current;
        const next = ui.grid({ min: '150px', className: 'nr-grid', ariaLabel: '文档书架' });
        next.id = 'nr-grid';
        if (current.parentNode) current.replaceWith(next);
        this._dom.grid = next;
        return next;
    },

    /** 一张书架卡片：主路径走共享 ContentUI.card（结构 + role/tabindex +
        Enter/Space 激活 + 可见焦点），再补本插件独有的封面叠加层（格式徽标、
        书签数、阅读进度）。共享组件缺失时退回 `_cardHtml` 的静态结构。 */
    _shelfCard(doc, index) {
        const ui = window.ContentUI;
        if (!ui || typeof ui.card !== 'function') return this._legacyShelfCard(doc, index);

        const percent = Math.round((Number(doc.progress) || 0) * 100);
        const kind = String(doc.kind || 'txt').toUpperCase();
        const meta = doc.kind === 'pdf' || doc.kind === 'external'
            ? '系统程序打开'
            : (doc.chapter_count ? `${doc.chapter_count} 章` : DocumentUtils.formatSize(doc.file_size));
        const markCount = (this.marks[doc.id] || []).length;
        const title = doc.title || doc.id;
        const sub = [doc.author, meta].filter(Boolean).join(' · ');

        const card = ui.card({
            className: 'nr-card',
            image: { src: DocumentUtils.coverDataUrl(title), alt: '' },
            title: title,
            sub: sub,
            data: { id: doc.id },
            ariaLabel: `打开 ${title}`,
            onActivate: () => this.openDocument(doc.id),
            // 徽标、书签数、阅读进度走共享的封面叠加层（.cui-media-*），
            // 不再往 .cui-media 里手工 append 私有节点。
            mediaOverlay: {
                badge: kind,
                count: markCount ? `书签 ${markCount}` : null,
                progress: percent ? percent : null,
                progressLabel: '阅读进度',
            },
        });

        // 共享结构上的类名钩子：原有 .nr-cover/.nr-card-body/.nr-card-title/
        // .nr-card-sub 的样式与测试选择器都继续可用。
        const media = card.querySelector('.cui-media');
        const img = card.querySelector('.cui-image');
        const body = card.querySelector('.cui-body');
        const titleEl = card.querySelector('.cui-title');
        const subEl = card.querySelector('.cui-sub');
        if (media) media.classList.add('nr-cover');
        if (img) img.classList.add('nr-cover-img');
        if (body) body.classList.add('nr-card-body');
        if (titleEl) titleEl.classList.add('nr-card-title');
        if (subEl) subEl.classList.add('nr-card-sub');
        return card;
    },

    /** 共享组件缺失时的旧结构（与迁移前完全一致），只为旧壳/独立打开兜底。
        旧结构里的封面叠加层由 `.nr-card.is-legacy` 作用域的样式承接。 */
    _legacyShelfCard(doc, index) {
        const holder = document.createElement('div');
        holder.innerHTML = this._cardHtml(doc, index);
        const card = holder.firstElementChild;
        if (card) {
            card.classList.add('is-legacy');
            card.addEventListener('click', () => this.openDocument(card.dataset.id));
        }
        return card;
    },

    _renderMarks(grid) {
        const groups = this.documents
            .map((doc) => ({ doc, marks: this.marks[doc.id] || [] }))
            .filter((item) => item.marks.length);
        const total = groups.reduce((sum, item) => sum + item.marks.length, 0);
        this._chromeSub = `${total} 条 · 来自 ${groups.length} 本`;
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

    /** 读取失败的独立状态：错误图标 + 原因 + 重试。结构仍用壳的 .empty-state，
        错误配色用壳的 `.empty-state--error`，与"空分类"共用一套排版。 */
    _errorHtml(message) {
        const iconHtml = window.Icons ? window.Icons.html('icon:triangle-alert', 'empty-state-icon') : '';
        const reason = message ? `读取失败：${message}` : '读取文档目录失败';
        return `<div class="empty-state empty-state--error nr-empty-span">
            <div class="empty-state-icon">${iconHtml}</div>
            <div class="empty-state-text">书架加载失败</div>
            <div class="empty-state-hint">${Utils.escapeHtml(reason)}。请检查文档根目录设置后重试。</div>
            <button type="button" class="btn" data-nr-retry>重试</button>
        </div>`;
    },

    /** 错误态里的重试：重读书架与书签；失败会重新渲染错误态（按钮随之重建）。 */
    async _retryShelf(button) {
        if (button) {
            button.disabled = true;
            button.textContent = '重试中…';
        }
        await this._reloadShelf();
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
