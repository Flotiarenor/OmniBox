// ============================================================
// 文档阅读器 — 右键菜单与书签分片
//
// `DocumentReader.prototype` 的分片：加载顺序是硬约束（必须排在 app.js 之后、
// 实例化之前），见 index.html。
// ============================================================
Object.assign(DocumentReader.prototype, {
    _selectionText() {
        const selection = window.getSelection ? window.getSelection() : null;
        if (!selection || selection.isCollapsed) return '';
        const text = String(selection);
        if (!text.trim()) return '';
        // 只认落在正文里的选区：工具栏/侧栏被拖选时不该冒出"开始朗读"
        const area = this._dom.contentArea;
        if (area && selection.anchorNode && !area.contains(selection.anchorNode)) return '';
        return text;
    },

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
     */,

    _resolveAnchor(selectedText) {
        if (selectedText) {
            const fromSelection = this.tts._selectionAnchor();
            if (fromSelection) return fromSelection;
        }
        return this.engine.anchorAtViewportTop();
    },

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
    },

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
    },

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
    },

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
    },

    _anchorFromViewport() {
        return this.engine.anchorAtViewportTop();
    },

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

    /** 跳到书签：切章 + 把该字滚进视野（snippet 校验在 engine 里做）。 */,

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
     */,

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
    },

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
    },
});
