// ============================================================
// 文档阅读器 — 朗读控制器
//
// 形态（docs/document-reader-design.md §3）：右下角浮动小卡，展开三颗圆钮
// （收到侧边 / 暂停 / 停止），收起后只剩最左那一块贴右边缘 —— 正文宽度一分不占。
//
// 分工：
//   * 后端 `tts_segment` 切句（保持字符偏移，与书签同一坐标系）、`tts_speak` 合成；
//   * 本文件只管播放、队列、预取与正文高亮；
//   * **朗读不写任何状态文件**：阅读进度只记到章，朗读位置每次现算
//     （视口顶部那一句，或右键菜单选中的那一句）。
//
// 高亮用 CSS Custom Highlight API（不动 DOM —— 滚动模式下章节节点会被真的删掉，
// 任何 wrap 出来的 <span> 都会在裁剪时消失或错位）。浏览器不支持时退化为只画
// 浮动框，功能不受影响。
// ============================================================
// 图标名 → 标记。壳把图标集内联进文档后由 /shell/icons.generated.js 提供 Icons.html；
// 缺失时（本页脱离壳单独打开调试）返回空串，不写 emoji 兜底。
const nrIcon = (name) => (window.Icons && typeof window.Icons.html === 'function')
    ? window.Icons.html(name)
    : '';

class ReaderTts {
    constructor(app) {
        this.app = app;
        this.status = null;
        this.lastEngine = '';
        this.pieces = [];          // [{text, start, end}]（相对本章 textContent）
        this.chapter = 0;
        this.index = 0;
        this.audio = this._createAudio();   // 挂进 DOM，见 _createAudio 的说明
        this._seq = 0;             // 每次开播自增：过期的响应一律丢弃
        this._prefetched = new Map();
        this._state = 'idle';      // idle | loading | playing | paused
        this._collapsed = false;
        this._markedRange = null;
        this._chapterElement = null;
        this._card = null;
    }

    /**
     * 建播放元素并**挂进 DOM**（`hidden`）。
     *
     * 游离的 `new Audio()` 也能播，但窗口最小化 / 切到后台时不如在 DOM 里的元素可靠
     * —— media-player 的 `<video>` 就在 DOM 里，所以它切走不断音。这里对齐同一种做法。
     */
    _createAudio() {
        const audio = document.createElement('audio');
        audio.id = 'nr-tts-audio';
        audio.hidden = true;
        audio.preload = 'auto';
        document.body.appendChild(audio);
        return audio;
    }

    async init() {
        this._buildCard();
        this.audio.addEventListener('ended', () => this._onEnded());
        this.audio.addEventListener('error', () => {
            if (this._state === 'playing' || this._state === 'loading') {
                Toast.error('朗读音频播放失败');
                this.stop();
            }
        });
        await this.refreshStatus();
    }

    // ===== 浮动卡 =====

    _buildCard() {
        const card = document.createElement('div');
        card.className = 'nr-tts-card hidden';
        card.innerHTML = `
            <button type="button" class="nr-tts-btn nr-tts-collapse" id="nr-tts-collapse"
                    title="收到侧边">${nrIcon('icon:maximize-2')}</button>
            <button type="button" class="nr-tts-btn nr-tts-toggle" id="nr-tts-toggle"
                    title="暂停">${nrIcon('icon:pause')}</button>
            <button type="button" class="nr-tts-btn nr-tts-stop" id="nr-tts-stop"
                    title="停止">${nrIcon('icon:square')}</button>`;
        document.body.appendChild(card);
        this._card = card;
        card.querySelector('#nr-tts-collapse').addEventListener('click', () => this.toggleCollapse());
        card.querySelector('#nr-tts-toggle').addEventListener('click', () => this.toggle());
        card.querySelector('#nr-tts-stop').addEventListener('click', () => this.stop());
    }

    _syncCard() {
        if (!this._card) return;
        const active = this._state !== 'idle';
        this._card.classList.toggle('hidden', !active);
        this._card.classList.toggle('is-collapsed', this._collapsed);
        this._card.dataset.state = this._state;
        const toggle = this._card.querySelector('#nr-tts-toggle');
        if (toggle) {
            toggle.innerHTML = nrIcon(this._state === 'paused' ? 'icon:play' : 'icon:pause');
            toggle.title = this._state === 'paused' ? '继续' : '暂停';
        }
        const collapse = this._card.querySelector('#nr-tts-collapse');
        if (collapse) {
            collapse.innerHTML = nrIcon(this._collapsed ? 'icon:chevron-right' : 'icon:maximize-2');
            collapse.title = this._collapsed ? '展开' : '收到侧边';
        }
    }

    toggleCollapse() {
        this._collapsed = !this._collapsed;
        this._syncCard();
    }

    isPlaying() {
        return this._state === 'playing' || this._state === 'loading';
    }

    // ===== 引擎状态 =====

    async refreshStatus() {
        try {
            this.status = await Bridge.call('tts_status');
            const available = (this.status.engines || []).filter((item) => item.available);
            // 只留引擎短名（edge / openai / system）：底部状态行只有一行宽度，
            // 长标签会被截成 "edge-tts（神经音色，联网" 这种半截话
            if (!this.lastEngine && available.length) this.lastEngine = available[0].name;
            if (this.app && this.app._updateStatus) this.app._updateStatus();
        } catch (e) {
            this.status = null;
        }
        return this.status;
    }

    // ===== 起点 =====

    /** 右键菜单选中的那段：从"选中第一个字所在句子的开头"开始念。 */
    _selectionAnchor() {
        const selection = window.getSelection ? window.getSelection() : null;
        if (!selection || selection.isCollapsed || !selection.rangeCount) return null;
        // 章号由选区自己决定（engine.anchorFromRange 里做），这里不假定"当前章"
        return this.app.engine.anchorFromRange(selection.getRangeAt(0));
    }

    startFromSelection() {
        const anchor = this._selectionAnchor();
        if (!anchor) {
            // 选区可能在右键之后才丢的：退回"屏幕顶部那一句"，别给一个没反应的结果
            Toast.info('没读到选区，从屏幕顶部那一句开始');
            this.startFromViewport();
            return;
        }
        this._startAt(anchor);
    }

    /** 按已算好的锚点开播（右键菜单在打开那一刻就把锚点存好了）。 */
    startFromAnchor(anchor) {
        if (!anchor) {
            this.startFromViewport();
            return;
        }
        this._startAt(anchor);
    }

    startFromViewport() {
        const anchor = this.app.engine.anchorAtViewportTop();
        if (!anchor) {
            Toast.info('正文还没就绪');
            return;
        }
        this._startAt(anchor);
    }

    // ===== 播放 =====

    async _startAt(anchor, resumeIndex = null) {
        this.stop();
        const context = this.app.engine.getChapterContext(anchor.chapter);
        if (!context || !context.text) {
            Toast.error('这一章没有可朗读的文本');
            return;
        }
        // 起点可能在别的章（用户选中的是上面那一章）：朗读跟着那一章走，
        // 但**不跳转视图** —— 用户没要求跳，只是读那里。
        this.chapter = anchor.chapter;
        this._chapterElement = context.element;
        this.pieces = this._split(context.text, anchor.char);
        if (!this.pieces.length) {
            Toast.error('这一章没有可朗读的文本');
            return;
        }
        this.index = resumeIndex === null ? 0 : Math.max(0, Math.min(resumeIndex, this.pieces.length - 1));
        this._seq += 1;
        this._prefetched.clear();
        // 卡片立即出现并进入"加载中"：首次合成要 1~3 秒（联网引擎更久），
        // 这段时间界面必须动起来，否则用户点了"开始朗读"看到的是一片静止，
        // 会以为功能坏了接着去点第二次（于是上一段被 stop 掉，永远听不到声）。
        this._state = 'loading';
        this._syncCard();
        if (this._collapsed) {
            this._collapsed = false;
            this._syncCard();
        }
        await this._playCurrent();
    }

    /**
     * 把本章文本切成句子，起点之前的整句丢掉。
     *
     * 切点与后端 `tts_engine.split_text` 是同一套字符（。！？；…!?; 与换行）。
     * 前端自己切只是为了**保住字符偏移**（后端的切分与正文 textContent 逐字节相等，
     * 但跨进程传回来不如本地现切直接）。拼接结果必须等于原文 —— 这是高亮坐标系的根基。
     */
    _split(text, fromChar) {
        const pieces = [];
        const stops = '。！？；…!?;\n';
        let start = 0;
        let buffer = '';
        const push = (end) => {
            if (buffer) pieces.push({ text: buffer, start, end });
            buffer = '';
            start = end;
        };
        for (let i = 0; i < text.length; i += 1) {
            buffer += text[i];
            if (stops.includes(text[i]) && buffer.length >= 4) push(i + 1);
            else if (buffer.length >= 240) push(i + 1);
        }
        if (buffer) pieces.push({ text: buffer, start, end: text.length });

        const from = Math.max(0, Math.min(fromChar || 0, text.length));
        const kept = pieces.filter((piece) => piece.end > from);
        // 起点落在某一句中间时，那一句整句照念（不切掉半句），因此不需要改 start
        return kept;
    }

    async _requestSegment(piece) {
        let cacheKey = '';
        try {
            cacheKey = await this._hash(piece.text);
        } catch (e) {
            cacheKey = '';
        }
        return Bridge.call('tts_speak', piece.text, cacheKey, this.index);
    }

    /** 文本指纹：缓存键必须由内容决定，且在前端算能省一次往返。 */
    async _hash(text) {
        if (window.crypto && window.crypto.subtle && window.TextEncoder) {
            const data = new TextEncoder().encode(text);
            const digest = await window.crypto.subtle.digest('SHA-1', data);
            return Array.from(new Uint8Array(digest))
                .map((b) => b.toString(16).padStart(2, '0')).join('').slice(0, 32);
        }
        let hash = 0;
        for (let i = 0; i < text.length; i += 1) hash = (hash * 31 + text.charCodeAt(i)) | 0;
        return `f${(hash >>> 0).toString(16)}`;
    }

    async _playCurrent() {
        if (this.index >= this.pieces.length) {
            await this._advanceChapter();
            return;
        }
        const seq = this._seq;
        const piece = this.pieces[this.index];
        this._state = 'loading';
        this._syncCard();
        // 优先用预取结果：合成有 1.5~2.2 秒的固定网络开销，不等它才能连得上。
        // （之前只把预取结果塞进 _prefetched 却从没读过，等于每句都现等。）
        let result = this._prefetched.get(this.index) || null;
        this._prefetched.delete(this.index);
        if (result && (!result.url || result.error)) result = null;
        if (!result) {
            try {
                result = await this._requestSegment(piece);
            } catch (e) {
                result = { error: String(e) };
            }
        }
        if (seq !== this._seq) return;                 // 用户已经停/跳了
        if (!result || result.error) {
            Toast.error(`朗读失败：${(result && result.error) || '未知错误'}`);
            this.stop();
            return;
        }
        if (result.engine) {
            this.lastEngine = result.engine;
            if (this.app && this.app._updateStatus) this.app._updateStatus();
        }
        this.audio.src = result.url;
        this._highlight(piece);
        this._prefetchNext();
        try {
            await this.audio.play();
            this._state = 'playing';
        } catch (e) {
            // 自动播放被拦（理论上点菜单已算用户手势，但浏览器策略各异）
            this._state = 'paused';
            Toast.info('浏览器拦下了自动播放，点浮动卡的播放键继续');
        }
        this._syncCard();
    }

    /**
     * 预取下一句。
     *
     * 用 `fetch` 把音频取进 HTTP 缓存，而不是只建一个 Image 对象 —— 后者根本不请求
     * mp3（image 解不了音频），等于没预热。真正命中缓存的是 `<audio>` 那次请求。
     */
    _prefetchNext() {
        const next = this.pieces[this.index + 1];
        if (!next || this._prefetched.has(this.index + 1)) return;
        const seq = this._seq;
        this._requestSegment(next).then((result) => {
            if (seq !== this._seq || !result || !result.url || result.error) return;
            this._prefetched.set(this.index + 1, result);
            try {
                fetch(result.url).catch(() => {});      // 灌进浏览器缓存
            } catch (e) { /* 忽略：预取失败不影响正常播放 */ }
        }).catch(() => {});
    }

    _onEnded() {
        if (this._state !== 'playing' && this._state !== 'paused') return;
        this.index += 1;
        this._playCurrent();
    }

    /** 本章念完：滚动到下一章接着念（翻页续读）。 */
    async _advanceChapter() {
        const next = this.chapter + 1;
        if (next >= this.app.engine.chapters.length) {
            Toast.info('已经是最后一章了');
            // 念完了要把进度落盘：下次打开从这里继续
            this.app._saveCurrentProgress(true);
            this.stop();
            return;
        }
        const seq = this._seq;
        // 告诉 app：这次换章是朗读自己干的，别在滚动事件里把朗读停掉
        this.app._ttsAdvancing = true;
        try {
            await this.app.engine.goToChapter(next, 0);
        } finally {
            this.app._ttsAdvancing = false;
        }
        if (seq !== this._seq) return;
        this.app._afterPageRender();
        // 自动翻章也要存进度（否则听着听着关掉，下次还停在旧章）
        this.app._saveCurrentProgress(true);
        const context = this.app.engine.getChapterContext(next);
        if (!context || !context.text) {
            this.stop();
            return;
        }
        this.chapter = next;
        this._chapterElement = context.element;
        this.pieces = this._split(context.text, 0);
        this.index = 0;
        this._prefetched.clear();
        this._playCurrent();
    }

    // ===== 控制 =====

    toggle() {
        if (this._state === 'playing') {
            this.pause();
        } else if (this._state === 'paused') {
            this.resume();
        }
    }

    pause() {
        if (this._state !== 'playing' && this._state !== 'loading') return;
        this._state = 'paused';
        try { this.audio.pause(); } catch (e) { /* 忽略：没在播就无所谓 */ }
        this._syncCard();
    }

    resume() {
        if (this._state !== 'paused') return;
        this._state = 'playing';
        this._syncCard();
        this.audio.play().catch(() => {
            // 音频已被卸载（切章/清空 src）：重取当前这一句
            this._playCurrent();
        });
    }

    stop() {
        this._seq += 1;
        this._state = 'idle';
        try {
            this.audio.pause();
            this.audio.removeAttribute('src');
            this.audio.load();
        } catch (e) { /* 忽略 */ }
        this._prefetched.clear();
        this._pieces = [];
        this.pieces = [];
        this._clearHighlight();
        this._syncCard();
    }

    // ===== 高亮 =====

    /** 正在念的那一整句：整体加深加粗（书签页共用同一个位置概念）。 */
    _highlight(piece) {
        this._clearHighlight();
        const root = this._chapterElement;
        if (!root) return;
        const range = this.app.engine._rangeFor(root, piece.start, piece.end);
        if (!range) return;
        this._markedRange = range;
        this._scrollIntoView(range);
        if (typeof Highlight !== 'undefined' && CSS.highlights) {
            try {
                CSS.highlights.set('nr-reading', new Highlight(range));
                return;
            } catch (e) { /* 退化到不做高亮 */ }
        }
    }

    /**
     * 念到看不见的地方就把它带进视野（自动跟随）。
     *
     * 只在句子跑出视口时才滚，而且留 25% 余量 —— 每句都硬滚一次会让页面不停地
     * 抖动，用户想回看上一句都抓不住。滚动事件会顺带把阅读进度存下来（见 app.js 的
     * `_bindScroll`），所以"读到哪"在关掉应用后还在。
     */
    _scrollIntoView(range) {
        const el = this.app._dom.contentArea;
        const rect = range.getClientRects()[0];
        if (!el || !rect) return;
        const areaRect = el.getBoundingClientRect();
        const offset = rect.top - areaRect.top;          // 句子相对视口顶部的位移
        const margin = areaRect.height * 0.25;
        const above = offset < margin;                    // 跑到上边（含被滚上去）
        const below = offset + rect.height > areaRect.height - margin;
        if (!above && !below) return;
        // 目标是把它放到视口 35% 处：位移量就是"当前位移 - 目标位移"。
        // 这里**不能**再减一次 areaRect.height（那是把"目标位置"又当成"位移"算），
        // 否则 delta 恒为负、scrollTop 被夹在 0，表现是"怎么念都不滚"。
        const delta = offset - areaRect.height * 0.35;
        el.scrollTo({ top: el.scrollTop + delta, behavior: 'smooth' });
    }

    _clearHighlight() {
        this._markedRange = null;
        if (typeof CSS !== 'undefined' && CSS.highlights) {
            try { CSS.highlights.delete('nr-reading'); } catch (e) { /* 忽略 */ }
        }
    }
}
