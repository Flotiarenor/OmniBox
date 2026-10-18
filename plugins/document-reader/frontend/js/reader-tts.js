// ============================================================
// 文档阅读器 — 朗读控制器
//
// 形态：右下角浮动小卡，展开三颗圆钮
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

// 预取深度：播放当前句时同时去要后面几句（后端设置项 tts_prefetch，朗读设置页可调）。
// 一句一合成时，合成下一句的耗时必须小于**上一句**的音频长度；短句（五六个字，音频
// 约 1.6 秒）撑不住本地端点一次约 1.9 秒的合成，只预取一句必然在那里卡住。
const NR_MAX_PREFETCH = 8;          // 与后端 TTS_PREFETCH_MAX 一致，两侧各夹一次
const NR_PREFETCH_FALLBACK = 1;     // 读不到设置时的保守值：只预取下一句（改动前的行为）

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
        // 正在请求中的句子（序号 → 那次请求的 promise）。深窗口下"这一句可能已经在路上"，
        // 这时要等它而不是再发一次：端点跟不上时，重发等于让同一句在端点上排两次队。
        this._pending = new Map();
        this._prefetchDepth = NR_PREFETCH_FALLBACK;
        // 音频元素当前挂的是哪一句。`ended` 事件不带身份，只靠序号推进会让迟到的
        // ended 多跳一句（听感就是跳读或重念），所以挂 src 时把身份记下来。
        this._audioIndex = undefined;
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
            this.setPrefetchDepth(this.status.prefetch);
            if (this.app && this.app._updateStatus) this.app._updateStatus();
        } catch (e) {
            this.status = null;
            // 状态拿不到 = 设置也不可信：退回逐句现取，宁可偶尔卡一下也不要并发几路
            this.setPrefetchDepth(NR_PREFETCH_FALLBACK);
        }
        return this.status;
    }

    /**
     * 设置预取深度（句）。朗读设置页改完直接调它，不必再走一趟 `tts_status`。
     *
     * 非法值、越界值一律退回 `NR_PREFETCH_FALLBACK`：深度决定"同时在端点上排几路合成"，
     * 配置坏了宁可按最保守的来，也不要让一个错值把端点压垮。
     */
    setPrefetchDepth(value) {
        const depth = Number(value);
        const usable = (Number.isInteger(depth) && depth >= 1 && depth <= NR_MAX_PREFETCH)
            ? depth : NR_PREFETCH_FALLBACK;
        if (usable !== this._prefetchDepth) {
            this._trace(`预取深度 ${this._prefetchDepth} → ${usable}`);
            this._prefetchDepth = usable;
        }
        return this._prefetchDepth;
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
        this._pending.clear();
        this._audioIndex = undefined;
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

    /**
     * 取一句的音频：**指纹随请求一起返回**。
     *
     * 返回指纹而不是只返回结果，是为了让调用方核对"拿到的音频是不是这一句的"：
     * 合成要几秒（实测慢端点单句 7 秒），这段时间里序号会前进，只按序号认结果
     * 会把上一句的音频当成下一句的（用户听到的就是"这一段念了上一段"）。
     */
    /**
     * 取一句的音频：**指纹随请求一起返回**。
     *
     * `segmentIndex` 由调用方给（不是读 `this.index`）：预取下一句时 `this.index`
     * 指向的还是上一句，读它会把回执参数写错一位。
     */
    async _requestSegment(piece, segmentIndex) {
        const cacheKey = await this._cacheKey(piece.text);
        const result = await Bridge.call('tts_speak', piece.text, cacheKey,
            segmentIndex === undefined ? this.index : segmentIndex);
        return { cacheKey, result };
    }

    /**
     * 内容指纹，同时充当缓存键：**必须由内容决定**（后端按它命名缓存文件）。
     *
     * 与后端 `_CACHE_KEY_RE` 的契约一致：算不出摘要时退回 `f<hex>`，后端认这种形状。
     * 取用预取结果时也用同一把钥匙比对"这是不是这一句的音频"（见 `_playCurrent`）。
     */
    async _cacheKey(text) {
        if (window.crypto && window.crypto.subtle && window.TextEncoder) {
            try {
                const data = new TextEncoder().encode(text);
                const digest = await window.crypto.subtle.digest('SHA-1', data);
                return Array.from(new Uint8Array(digest))
                    .map((b) => b.toString(16).padStart(2, '0')).join('').slice(0, 32);
            } catch (e) { /* 落到下面的短指纹 */ }
        }
        let hash = 0;
        const source = String(text || '');
        for (let i = 0; i < source.length; i += 1) hash = (hash * 31 + source.charCodeAt(i)) | 0;
        return `f${(hash >>> 0).toString(16)}`;
    }

    async _playCurrent() {
        if (this.index >= this.pieces.length) {
            await this._advanceChapter();
            return;
        }
        const seq = this._seq;
        const index = this.index;
        const piece = this.pieces[index];
        this._state = 'loading';
        this._syncCard();
        const wantKey = await this._cacheKey(piece.text);
        // 优先用预取结果：合成有数秒的固定开销，不等它才能连得上。
        // 取用前用同一把内容指纹核对 —— 预取是在上一句播完之前发出的，那段时间里
        // 序号会前进，只按序号取用就会把别的句子的音频拿来播。指纹一致时不必再请求。
        const taken = this._prefetched.get(index) || null;
        this._prefetched.delete(index);
        let result = (taken && taken.cacheKey === wantKey
            && taken.result && taken.result.url && !taken.result.error) ? taken.result : null;
        if (taken && !result) this._trace(`丢弃不匹配的预取结果（第 ${index + 1} 句）`);
        if (!result) result = await this._resolveSegment(piece, wantKey, index);
        if (seq !== this._seq) return;                 // 用户已经停/跳了
        if (!result || result.error) {
            Toast.error(`朗读失败：${(result && result.error) || '音频与当前句子对不上，已跳过'}`);
            this.stop();
            return;
        }
        if (result.engine) {
            this.lastEngine = result.engine;
            if (this.app && this.app._updateStatus) this.app._updateStatus();
        }
        // 挂音频前记下"现在念的是哪一句"：`ended` 事件不带身份，语句被换掉之后
        // 迟到的 ended 会让播放位置多跳一句（听感就是跳读或重念）。
        this._audioIndex = index;
        this.audio.src = result.url;
        this._trace(`第 ${index + 1} 句 → ${result.url}（${result.engine || '?'}）`);
        this._highlight(piece);
        try {
            await this.audio.play();
            this._state = 'playing';
        } catch (e) {
            // 自动播放被拦（理论上点菜单已算用户手势，但浏览器策略各异）
            this._state = 'paused';
            Toast.info('浏览器拦下了自动播放，点浮动卡的播放键继续');
        }
        // 开播之后再预取：此时这一句的序号已经稳定，预取不会张冠李戴
        this._prefetchNext();
        this._syncCard();
    }

    /**
     * 取当前句的音频：先等"已经在路上的那次预取"，没有才自己发请求。
     *
     * 为什么要等而不是重发：深窗口下这一句的请求可能还在合成（端点慢的时候），重发会让
     * 同一句在端点上排两次队 —— 端点已经跟不上时，这恰好是最不该做的事。预取那次失败或
     * 指纹对不上时仍然自己再发一次，行为与只有一层预取时一致。
     *
     * `index` 由调用方给：取指纹那一步是异步的，期间 `this.index` 可能已经前进
     * （上一句的 `ended` 赶在这之前到了），读 `this.index` 会去等别的句子的请求。
     */
    async _resolveSegment(piece, wantKey, index = this.index) {
        const pending = this._pending.get(index);
        if (pending) {
            try {
                const asked = await pending;
                if (asked && asked.cacheKey === wantKey && asked.result && !asked.result.error) {
                    // 这一格已经播掉了：从预取表里摘掉，免得回翻时错当成"还没播的下一句"
                    this._pending.delete(index);
                    this._prefetched.delete(index);
                    return asked.result;
                }
            } catch (e) {
                /* 预取那次失败：走下面的现取 */
            }
        }
        try {
            const asked = await this._requestSegment(piece);
            if (asked.cacheKey !== wantKey) {
                this._trace(`请求指纹 ${asked.cacheKey} 与本句 ${wantKey} 不符，丢弃`);
                return null;
            }
            return asked.result;
        } catch (e) {
            return { error: String(e) };
        }
    }

    /**
     * 把后面 `_prefetchDepth` 句排进队列。
     *
     * 请求按序号**升序**发出：后端按到达顺序处理时，真正要播的那句总在队伍最前面，
     * 深窗口不会反过来拖慢离得最近的那一句。
     */
    _prefetchNext() {
        const depth = Math.max(1, Math.min(NR_MAX_PREFETCH, this._prefetchDepth | 0));
        for (let offset = 1; offset <= depth; offset += 1) {
            this._prefetchAt(this.index + offset);
        }
    }

    /**
     * 预取第 `index` 句（已经在队列里或已经拿到就跳过）。
     *
     * 用 `fetch` 把音频取进 HTTP 缓存，而不是只建一个 Image 对象 —— 后者根本不请求
     * mp3（image 解不了音频），等于没预热。真正命中缓存的是 `<audio>` 那次请求。
     *
     * 结果**带指纹**存下来：回调跑到时 `this.index` 可能已经变了，只按序号存会把
     * 别的句子顶到这一格上（取用时还会再核对一次，见 `_playCurrent`）。
     */
    _prefetchAt(index) {
        const piece = this.pieces[index];
        if (!piece || this._prefetched.has(index) || this._pending.has(index)) return;
        const seq = this._seq;
        const asked = this._requestSegment(piece, index);
        this._pending.set(index, asked);
        asked.then((entry) => {
            this._pending.delete(index);
            if (seq !== this._seq) return;
            if (!entry || !entry.result || !entry.result.url || entry.result.error) {
                this._degradePrefetch(`第 ${index + 1} 句预取失败`);
                return;
            }
            this._prefetched.set(index, { cacheKey: entry.cacheKey, result: entry.result });
            this._trace(`预取第 ${index + 1} 句 → ${entry.result.url}`);
            try {
                fetch(entry.result.url).catch(() => {});      // 灌进浏览器缓存
            } catch (e) { /* 忽略：预取失败不影响正常播放 */ }
        }, () => {
            this._pending.delete(index);
            if (seq === this._seq) this._degradePrefetch(`第 ${index + 1} 句预取异常`);
        });
    }

    /**
     * 预取失败 → 后续退回逐句现取。
     *
     * 预取失败通常意味着端点已经在排队或拒连；这时继续按深窗口并发，只会让真正要播的
     * 那一句排得更久。退回 1 之后卡顿会回来，但播放至少还在往前走。
     *
     * 这是一次性的降级：回到配置值要等下一次 `refreshStatus()`（重新进阅读器，或在
     * 朗读设置页点"刷新引擎状态"）—— 端点刚抖过就立刻按 5 路重排，多半会再失败一次。
     */
    _degradePrefetch(reason) {
        if (this._prefetchDepth <= NR_PREFETCH_FALLBACK) return;
        this._trace(`预取失败，深度 ${this._prefetchDepth} → ${NR_PREFETCH_FALLBACK}（${reason}）`);
        this._prefetchDepth = NR_PREFETCH_FALLBACK;
    }

    /** 诊断日志：只有控制台开着才有开销，定位"读错句子"这类问题靠它。 */
    _trace(message) {
        try {
            if (this.app && this.app.debug && this.app.debug.status_debug && console.debug) {
                console.debug('[reader-tts]', message);
            }
        } catch (e) { /* 忽略：诊断不影响播放 */ }
    }

    _onEnded() {
        if (this._state !== 'playing' && this._state !== 'paused') return;
        // 迟到的 ended（音频元素已经换到别的句子）不推进位置
        if (this._audioIndex !== undefined && this._audioIndex !== this.index) {
            this._trace(`忽略迟到的 ended（音频是第 ${this._audioIndex + 1} 句，当前第 ${this.index + 1} 句）`);
            return;
        }
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
        this._pending.clear();
        this._audioIndex = undefined;      // 换了章：旧音频的身份不再有效
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
        this._pending.clear();
        this._audioIndex = undefined;
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
