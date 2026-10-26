// ============================================================
// 文档阅读器 — 主控制器
//
// 页面 chrome（品牌块 / 左栏导航 / 顶栏 / 搜索 / 状态栏 / 分隔条）由壳按
// manifest 的 frontend.chrome 渲染；本文件只维护内容：
//   书架态  主区＝封面网格
//   阅读态  主区＝正文（返回、目录、阅读设置入口在壳顶栏/左栏）
//   目录与阅读设置是内容弹窗，朗读设置是覆盖主区的独立页（reader-voice-page.js）
//   朗读控制是右下角浮动小卡（reader-tts.js），不占正文宽度
//
// 与壳的通信：壳发 omnibox:view / omnibox:action / omnibox:query，本文件回
// omnibox:chrome（标题、副标题、视图、动作显隐、导航补丁、搜索显隐、状态）。
//
// 分页 / 测量 / 滚动加载在 reader-engine.js；阅读偏好持久化在 settings.js。
// ============================================================
// 图标名 → 标记。sprite 与 Icons.html 由壳注入的 /shell/icons.generated.js 提供；
// 缺失时（本页脱离壳单独打开）返回空串，不写 emoji 兜底。
const nrAppIcon = (name) => (window.Icons && typeof window.Icons.html === 'function')
    ? window.Icons.html(name)
    : '';

class DocumentReader {
    constructor() {
        this.documents = [];
        this.currentDocument = null;
        this.marks = {};              // {文档 id: [书签]}
        this.view = 'all';            // all | recent | marks
        this._isReaderMode = false;

        this.fontSize = 16;
        this.lineHeight = 1.8;
        this.letterSpacing = 0;
        this.theme = 'auto';
        this.bgColor = '#ffffff';
        this.textColor = '#1a1a1a';
        // 编码全自动、没有开关：后端的解码链（UTF-8 自证 → 中文编码 → gb18030）比让
        // 用户手选任何一个编码都强，手选唯一能做到的额外效果是把能读的书解成乱码。
        this.encoding = 'auto';
        this.mode = 'scroll';
        // 正文底色与主题正交：theme 跟随主题 / show 透出壳背景 / solid 指定色彩。
        // 默认 show —— 壳默认就是玻璃背景图，正文让出底色才看得见它。
        this.bgMode = 'show';
        this.bodyColor = '#ffffff';

        this.engine = new DocumentReaderEngine(this);
        this.tts = new ReaderTts(this);
        this.voicePage = new ReaderVoicePage(this);
        this._dom = {};
        this.settings = null;
        this._shelfSeq = 0;           // 书架渲染序号：丢弃过期的异步封面回填
        // 后端此刻记着的阅读位置（章号 + 章内 0~1）：打开时按它填，保存后按它更新。
        // 关闭时位置没动过就不写 —— 否则"打开看一眼再关掉"会把上次读到的地方
        // 覆盖成刚打开的位置（记录就是这样被一路擦回第 1 章的）。
        this._savedPosition = null;

        // 壳渲染的 chrome 的运行期数据。标题/副标题由内容侧维护，视图、动作、
        // 导航与搜索的显隐由 _pushChrome() 每次状态变化时整份上报。
        this._chromeTitle = '全部文档';
        this._chromeSub = '';
        this._statusText = '正在读取…';
        this._statusState = 'idle';
        this._searchQuery = '';
        // 书架文档列表的读取失败原因（空串 = 没有错误）。真实空分类与读取失败
        // 在界面上必须可区分：前者给"放进文档目录"的提示，后者给错误文案与重试。
        this._loadError = '';
        // 让壳的搜索框跟着内容页清空/回填：一次性载荷，随下一次 chrome 上报发出。
        // 不用每帧都上报关键词 —— 那会把用户正在输入的内容顶回去。
        this._searchSync = null;
        this._searchClearSeq = 0;
    }

    async init() {
        this._cacheDom();
        this.settings = new ReaderSettingsStore(this);
        this._bindEvents();
        this._bindModalDismiss();
        await this._loadDocuments();
        await this._loadMarks();
        // 阅读偏好从后端设置读（跨设备持久化），读到之后再渲染书架与正文
        await this.settings.load();
        this.engine.setMode(this.mode);
        this._renderShelf();
        // 与壳握手：先声明监听，再发 ready；壳收到 ready 后补发当前视图。
        this._bindHost();
        this._pushChrome();
        // 朗读可以关掉（`document-reader-tts=off`）：浏览器用例不需要它，
        // 每次初始化都去打一遍引擎状态既慢又依赖网络。关掉后朗读入口依然在，
        // 点了会提示未启用。
        if (localStorage.getItem('document-reader-tts') !== 'off') {
            await this.tts.init();
            // 引擎名进状态栏：朗读初始化完再上报一次
            this._updateStatus();
        }
    }

    // ===== 与壳的页面骨架通信 =====

    /** 内容页与壳同源：消息只发给父窗口，脱离壳单独打开时不发。 */
    _postHost(payload) {
        if (window.parent === window) return;
        try { window.parent.postMessage(payload, location.origin); } catch (e) { /* 非同源父窗口不发 */ }
    }

    /** 上报 chrome：标题、副标题、视图（'' 表示阅读态、不落导航高亮）、
        动作显隐、导航组/徽标补丁、搜索显隐与状态栏。 */
    _pushChrome() {
        const reading = !!this._isReaderMode;
        const voice = !!this._voicePaneOpen;
        const totalMarks = Object.values(this.marks).reduce((sum, list) => sum + list.length, 0);
        const searchSync = this._searchSync;
        this._searchSync = null;
        this._postHost({
            type: 'omnibox:chrome',
            title: voice ? '朗读设置' : this._chromeTitle,
            subtitle: voice ? '引擎 / 音色 / 缓存' : this._chromeSub,
            view: voice ? 'voice' : (reading ? '' : this.view),
            actions: {
                back: { visible: reading },
                settings: { visible: true },
            },
            nav: {
                'reader-tools': { visible: reading },
                all: { badge: this.documents.length ? String(this.documents.length) : '' },
                marks: { badge: totalMarks ? String(totalMarks) : '' },
            },
            search: { enabled: !reading && !voice, ...(searchSync || {}) },
            status: { text: this._statusText, state: this._statusState },
        });
    }

    /** 清空搜索词，并让壳的输入框回空。切分类、进阅读态、开朗读设置时调用：
        列表已经按新的视图重排，框里还留着旧关键词会与内容对不上。 */
    _clearSearch() {
        this._searchQuery = '';
        this._searchSync = { value: '', clear: ++this._searchClearSeq };
    }

    /** 壳的握手与反向下发：视图、顶栏动作、搜索输入。 */
    _bindHost() {
        window.addEventListener('message', (event) => {
            if (event.origin !== location.origin || event.source !== window.parent) return;
            const data = event.data || {};
            if (data.type === 'omnibox:view' && typeof data.view === 'string') {
                this._applyHostView(data.view);
                return;
            }
            if (data.type === 'omnibox:action') {
                this._applyHostAction(data.action);
                return;
            }
            if (data.type === 'omnibox:query') {
                this._onQuery(data.query);
            }
        });
        this._postHost({ type: 'omnibox:ready' });
    }

    /** 壳左栏的导航项都发 omnibox:view：三个书架分类、朗读设置与「当前文档」组的
        目录/阅读设置。后三个是入口而非视图，按动作处理。 */
    _applyHostView(view) {
        if (view === 'toc') { this.openToc(); this._pushChrome(); return; }
        if (view === 'read-settings') {
            openNrModal('nr-read-settings-modal');
            this._pushChrome();
            return;
        }
        if (view === 'voice') { this._openVoicePage(); return; }
        if (view === 'all' || view === 'recent' || view === 'marks') this.switchView(view);
    }

    /** 壳顶栏动作：返回、统一刷新完成后的重载，以及壳设置弹窗保存后的偏好重读。 */
    _applyHostAction(action) {
        if (action === 'back') this.returnToShelf();
        else if (action === 'settings-saved') this._reloadSettings();
        else if (action === 'refresh') this._reloadShelf();
    }

    /** 朗读设置页：主区内容切换，左栏高亮由壳按 view='voice' 落。 */
    _openVoicePage() {
        this._clearSearch();
        this.voicePage.open();
    }

    /** 壳的设置弹窗保存后自己收尾：壳只把值写进后端，既不重载页面也不通知插件页。
        不重读的话正文停在旧配色；更糟的是页内任何一次 `save()` 都会把**整份**偏好
        写回后端，刚在弹窗里改的那一项会被内存里的旧值静默盖掉 —— 表现为"这一项
        设置永远不生效"。
        文档根目录也在同一个弹窗里，因此书架要在保存后重扫一次；阅读态不重绘书架
        （`_renderShelf` 会用书架标题覆盖壳顶栏的书名）。 */
    async _reloadSettings() {
        if (await this.settings.reload()) this.engine.setMode(this.mode);
        if (!this._isReaderMode) await this._reloadShelf();
    }

    /** 统一刷新（同步/校验）完成后重读书架与书签；组件由壳挂载。 */
    async _reloadShelf() {
        await this._loadDocuments();
        await this._loadMarks();
        // 阅读态不重绘书架：`_renderShelf` 会用书架分类的标题/统计覆盖壳顶栏的书名，
        // 而正文还在阅读器里。数据已经重读，退出阅读态后的下一次渲染自然用新列表；
        // `_reloadSettings` 出于同一原因也要在阅读态跳过这一步。
        if (this._isReaderMode) return;
        this._renderShelf(this._searchQuery);
    }

    // ===== DOM =====

    _cacheDom() {
        this._dom = {
            shelfView: document.getElementById('nr-shelf-view'),
            readerView: document.getElementById('nr-reader-view'),
            grid: document.getElementById('nr-grid'),
            contentArea: document.getElementById('document-content-area'),
            progressBar: document.querySelector('.document-progress-bar'),
            progressFill: document.getElementById('document-progress-fill'),
            tocList: document.getElementById('nr-toc-list'),
            tocSub: document.getElementById('nr-toc-sub'),
            tocFilter: document.getElementById('nr-toc-filter'),
        };
    }

    _bindEvents() {
        if (this._dom.tocFilter) {
            this._dom.tocFilter.addEventListener('input', () => this._renderTocItems());
        }

        this._bindReaderSettings();
        this._bindKeyboard();
        this._bindPageTurn();
        this._bindContextMenu();
        this._bindScroll();
        this._bindLifecycle();
        // 关窗口 / 刷新：位置动过才写（见 `_saveCurrentProgress` 的 force 说明）
        window.addEventListener('beforeunload', () => this._saveCurrentProgress(false));
    }

    /** 弹窗：点遮罩关闭（按按下位置判定，与壳内其它弹窗一致）。 */
    _bindModalDismiss() {
        document.querySelectorAll('.modal').forEach((modal) => {
            modal.addEventListener('pointerdown', (event) => {
                if (event.target === modal) closeNrModal(modal.id);
            });
        });
        document.querySelectorAll('[data-nr-close]').forEach((btn) => {
            btn.addEventListener('click', () => closeNrModal(btn.dataset.nrClose));
        });
    }

    _bindReaderSettings() {
        const dom = this._dom;
        dom.fontSizeSlider = document.getElementById('document-font-size');
        dom.fontSizeValue = document.getElementById('document-font-size-value');
        dom.lineHeightSlider = document.getElementById('document-line-height');
        dom.lineHeightValue = document.getElementById('document-line-height-value');
        dom.letterSpacingSlider = document.getElementById('document-letter-spacing');
        dom.letterSpacingValue = document.getElementById('document-letter-spacing-value');
        dom.themeSelect = document.getElementById('document-theme-select');
        dom.modeSelect = document.getElementById('document-mode-select');
        dom.bgColorInput = document.getElementById('document-bg-color');
        dom.textColorInput = document.getElementById('document-text-color');
        dom.customColorRow = document.getElementById('custom-color-row');
        dom.customTextRow = document.getElementById('custom-text-row');
        dom.bgModeSelect = document.getElementById('document-bg-mode');
        dom.bodyColorInput = document.getElementById('document-body-color');
        dom.bodyColorRow = document.getElementById('body-color-row');

        dom.fontSizeSlider.addEventListener('input', (event) => {
            this.fontSize = parseInt(event.target.value, 10);
            dom.fontSizeValue.textContent = this.fontSize;
            this.settings.apply();
        });
        dom.lineHeightSlider.addEventListener('input', (event) => {
            this.lineHeight = parseFloat(event.target.value);
            dom.lineHeightValue.textContent = this.lineHeight.toFixed(1);
            this.settings.apply();
        });
        dom.letterSpacingSlider.addEventListener('input', (event) => {
            this.letterSpacing = parseFloat(event.target.value);
            dom.letterSpacingValue.textContent = `${this.letterSpacing}px`;
            this.settings.apply();
        });
        dom.themeSelect.addEventListener('change', (event) => {
            this.theme = event.target.value;
            this._syncCustomColorRows();
            this.settings.apply();
        });
        dom.bgColorInput.addEventListener('change', (event) => {
            this.bgColor = event.target.value;
            this.settings.apply();
        });
        dom.textColorInput.addEventListener('change', (event) => {
            this.textColor = event.target.value;
            this.settings.apply();
        });
        dom.bgModeSelect.addEventListener('change', (event) => {
            this.bgMode = event.target.value;
            this._syncBodyColorRow();
            this.settings.apply();
        });
        dom.bodyColorInput.addEventListener('change', (event) => {
            this.bodyColor = event.target.value;
            this.settings.apply();
        });
        dom.modeSelect.addEventListener('change', (event) => {
            this.mode = event.target.value === 'page' ? 'page' : 'scroll';
            this.engine.setMode(this.mode);
            this.settings.save();
            Toast.info(this.mode === 'scroll' ? '已切换到连续滚动模式' : '已切换到翻页模式');
        });
        this._syncCustomColorRows();
    }

    _syncCustomColorRows() {
        const dom = this._dom;
        const custom = this.theme === 'custom';
        if (dom.customColorRow) dom.customColorRow.style.display = custom ? 'flex' : 'none';
        if (dom.customTextRow) dom.customTextRow.style.display = custom ? 'flex' : 'none';
        this._syncBodyColorRow();
    }

    /** 「正文底色 = 指定色彩」时才显示取色器。 */
    _syncBodyColorRow() {
        const dom = this._dom;
        if (dom.bodyColorRow) {
            dom.bodyColorRow.style.display = this.bgMode === 'solid' ? 'flex' : 'none';
        }
    }

    _bindKeyboard() {
        document.addEventListener('keydown', (event) => {
            if (!this._isReaderMode) return;
            const tag = (event.target && event.target.tagName) || '';
            if (tag === 'INPUT' || tag === 'TEXTAREA' || tag === 'SELECT') return;
            if (document.querySelector('.modal.active')) return;
            switch (event.key) {
                case 'ArrowUp':
                case 'PageUp':
                    event.preventDefault();
                    this._scrollArea(-0.8);
                    break;
                case 'ArrowDown':
                case 'PageDown':
                case ' ':
                    event.preventDefault();
                    this._scrollArea(0.8);
                    break;
                case 'Escape':
                    event.preventDefault();
                    this.returnToShelf();
                    break;
                default:
                    break;
            }
        });
    }

    /**
     * 常驻插件被切到后台时的收尾。
     *
     * **刻意不绑 onHide 去暂停朗读**：切插件或最小化窗口时朗读应当继续（与
     * media-player 一致 —— 它压根不绑生命周期钩子，音频元素常驻所以不断）。
     * 之前这里写了 `onHide → tts.pause()`，表现就是"一切走朗读就断"。
     * 只保留 dispose：插件真被卸载/刷新时再收尾。
     *
     * dispose 之前要先存一次进度：iframe 被移除**不会**触发 beforeunload（只有整页
     * 导航才会），而 keepAlive 的插件在设置变更、壳刷新这些路径上会被整个卸掉。
     */
    _bindLifecycle() {
        if (!window.PluginLifecycle) return;
        window.PluginLifecycle.onDispose(() => {
            this._saveCurrentProgress(false);
            this.tts.stop();
        });
    }

    _scrollArea(factor) {
        const el = this._dom.contentArea;
        if (el) el.scrollBy({ top: el.clientHeight * factor, behavior: 'smooth' });
    }

    /**
     * 点击翻页：左 25% 上一页、右 25% 下一页（中间 50% 不动）。
     *
     * 与朗读的分工：朗读只从右键菜单进，点击永远只管翻页 —— 否则"想选中一句话"
     * 会顺带翻一章（选中文字时本函数直接返回，这条判定是从旧实现继承下来的）。
     */
    _bindPageTurn() {
        const el = this._dom.contentArea;
        if (!el) return;
        el.addEventListener('click', async (event) => {
            if (!this._isReaderMode) return;
            if (this.mode === 'scroll') return;      // 连续滚动没有"翻页"这回事
            if (event.target.closest('.document-pdf-frame, [data-no-pageturn]')) return;
            const selection = window.getSelection ? String(window.getSelection()) : '';
            if (selection) return;
            const rect = el.getBoundingClientRect();
            const ratio = (event.clientX - rect.left) / rect.width;
            if (ratio < 0.25) await this._turnPage(-1);
            else if (ratio > 0.75) await this._turnPage(1);
        });
    }

    async _turnPage(direction) {
        const result = direction > 0 ? await this.engine.nextPage() : await this.engine.prevPage();
        if (result === 'end') {
            Toast.info(direction > 0 ? '已经是最后一章了' : '已经是第一章了');
            return;
        }
        if (result === 'moved') this._afterPageRender();
    }

    /**
     * 滚动：节流成"每 60ms 至多一次"，两条线共用这一个通道。
     *
     * 以前用"距上次不足 80ms 就丢弃"的节流，会丢掉最后一次滚动事件 —— 滑到底停住
     * 之后下一章永远不加载。这里改成挂起式节流（末尾必执行）。
     */
    _bindScroll() {
        const el = this._dom.contentArea;
        if (!el) return;
        let timer = null;
        const onScroll = () => {
            if (timer) return;
            timer = setTimeout(async () => {
                timer = null;
                if (!this._isReaderMode) return;
                await this.engine.handleScroll();
                this._afterPageRender();
                // 书签标记是按 Range 的矩形定位的，滚动后位置就变了
                this._flashMarks();
                // 手动滚动换了章：朗读跟着停（朗读位置不持久化）。
                // 不自动续读是有意的 —— 用户可能只是翻回去看一下，突然念起来更烦人。
                // 但朗读自己翻章（_advanceChapter）也会触发滚动事件，那种情况不能停，
                // 否则"念完这章自动续下一章"刚跳过去就被自己停掉。
                if (this.tts.isPlaying() && !this._ttsAdvancing
                    && this.tts.chapter !== this.engine.currentChapterIndex) {
                    this.tts.stop();
                    Toast.info('已翻到别的章节，朗读已停止');
                }
            }, 60);
        };
        el.addEventListener('scroll', onScroll, { passive: true });
        el.addEventListener('wheel', onScroll, { passive: true });
        window.addEventListener('resize', () => {
            if (this._isReaderMode) this._afterPageRender();
        });
    }

    // ===== 统一刷新（同步 / 校验）=====
    //
    // 控件由壳的 PluginChrome 挂载（manifest 的 frontend.chrome.freshness），
    // 校验完成或同步发现变化时壳发 omnibox:action: refresh，本页重读书架与书签
    // （见 `_applyHostAction` / `_reloadShelf`）。

    // ===== 书架 =====

    async _loadDocuments() {
        try {
            const result = await Bridge.call('document_list');
            this.documents = result.documents || [];
            this._loadError = '';
            this._statusState = 'ok';
        } catch (e) {
            console.error('加载文档列表失败:', e);
            this.documents = [];
            this._loadError = (e && e.message) ? String(e.message) : '读取失败';
            this._statusState = 'error';
        }
    }

    async _loadMarks() {
        try {
            const result = await Bridge.call('marks_list', '');
            this.marks = {};
            (result.marks || []).forEach((mark) => {
                const id = mark.document_id;
                if (!id) return;
                (this.marks[id] = this.marks[id] || []).push(mark);
            });
        } catch (e) {
            this.marks = {};
        }
    }

    switchView(view) {
        // 切到书架分类就离开朗读设置面板，并让壳的搜索框回到空。
        // 只有视图真的变了才清：壳在加载与握手时会把当前视图重发一次，
        // 那不代表用户切了分类，不能把它当成"重置搜索"。
        const changed = this.view !== view;
        if (this._voicePaneOpen) this.voicePage.close();
        this.view = view;
        if (changed) this._clearSearch();
        this.returnToShelf({ keepView: true });
    }

    returnToShelf(options = {}) {
        if (this._isReaderMode) this._saveCurrentProgress(true);
        this.tts.stop();
        if (this._voicePaneOpen) {
            this._voicePaneOpen = false;
            const pane = document.getElementById('nr-voice-page');
            if (pane) pane.classList.add('hidden');
        }
        this._isReaderMode = false;
        this.currentDocument = null;
        this._dom.shelfView.classList.remove('hidden');
        this._dom.readerView.classList.add('hidden');
        // 书架态没有可表示的阅读进度：进度轨道只在阅读器里有意义，否则会在
        // 书架底部留一条 4px 的空槽。朗读设置页同理。
        this._setProgressVisible(false);
        if (!options.keepView) this.view = this.view || 'all';
        this._renderShelf(this._searchQuery);
    }

    /** 壳搜索框的输入（omnibox:query；壳侧已去抖 250ms）。 */
    _onQuery(query) {
        this._searchQuery = String(query || '').trim();
        this._renderShelf(this._searchQuery);
    }

    _visibleDocuments(keyword = '') {
        let items = this.documents;
        if (this.view === 'recent') {
            items = items.filter((doc) => doc.last_read_time)
                .sort((a, b) => String(b.last_read_time).localeCompare(String(a.last_read_time)));
        } else if (this.view === 'marks') {
            items = items.filter((doc) => (this.marks[doc.id] || []).length);
        }
        if (keyword) {
            items = items.filter((doc) => (doc.title || '').includes(keyword)
                || (doc.author || '').includes(keyword));
        }
        return items;
    }

    // ===== 书签视图 =====
    //
    // 书签是唯一带字级位置的状态（阅读进度只记到章）。列表按文档分组，
    // 每条能跳转（goToMark：切章 + 按 char 定位，engine 里带 snippet 校验）与删除。

    /** 状态栏文案：书架总本数 + 当前朗读引擎（徽标由 `_pushChrome` 一并上报）。
        读取失败时状态栏只说失败，不把 0 本混成"书是空的"。 */
    _updateStatus() {
        if (this._loadError) {
            this._statusText = '书架加载失败';
            this._statusState = 'error';
            this._pushChrome();
            return;
        }
        // 引擎名取冒号前那一段：状态栏只有一行宽度，"edge-tts（神经音色，联网）"
        // 一定会被截成 "edge-"，看着像坏了
        const engine = this.tts && this.tts.lastEngine
            ? ` · 朗读 ${String(this.tts.lastEngine).split(/[（(:：]/)[0].trim()}`
            : '';
        this._statusText = `${this.documents.length} 本${engine}`;
        if (!this._statusState || this._statusState === 'idle') this._statusState = 'ok';
        this._pushChrome();
    }

    // ===== 打开文档 / 阅读态 =====

    async openDocument(documentId, options = {}) {
        const doc = this.documents.find((item) => item.id === documentId);
        if (!doc) return null;
        this.currentDocument = doc;
        try {
            if (doc.kind === 'pdf' || doc.kind === 'external') {
                this._enterReaderMode(doc);
                this._openNonChapter(doc);
                return doc;
            }
            const result = await Bridge.call('document_get_chapters', documentId, this.encoding);
            const chapters = result.chapters || [];
            doc.chapter_count = chapters.length;
            this.engine.setMode(this.mode);
            this.engine.reset(documentId, chapters, this.encoding);
            this._enterReaderMode(doc);
            // 必须在 engine.reset 之后挂加载态：reset 会清空正文容器，先挂的提示
            // 会被一起删掉（旧实现因此从来没有真正显示过 spinner）。
            this._showLoading(true);
            // 调用方指定了章号（目录跳转 / 书签 / 重读）就走它，章内位置同理：只给了
            // 章号的（书签跳转）落到章首，之后由 `scrollToChar` 按字符精确定位。
            const hasTarget = options.chapter !== undefined && options.chapter !== null;
            const chapter = hasTarget
                ? options.chapter
                : Math.max(0, Math.min(Number(doc.last_read_chapter) || 0, Math.max(0, chapters.length - 1)));
            const fraction = hasTarget
                ? this._clampFraction(options.fraction)
                : this._clampFraction(doc.scroll_position);
            await this.engine.goToChapter(chapter, fraction);
            this._afterPageRender();
            // 还原到位之后才记"后端现在记的是这里"：这之后的保存以它为基准判断有没有
            // 动过。`goToChapter` 里还有一帧 rAF 会再落一次位置，落定之后再校准基准。
            this._savedPosition = { chapter, fraction, scrollTop: this._contentScrollTop() };
            requestAnimationFrame(() => {
                if (this.currentDocument === doc) {
                    this._savedPosition = { chapter, fraction, scrollTop: this._contentScrollTop() };
                }
            });
            this._showLoading(false);
            if (options.startTts) {
                // 从"左上角第一个字所在句子的开头"念起
                this.tts.startFromViewport();
            }
            return doc;
        } catch (e) {
            console.error('打开文档失败:', e);
            Toast.error('打开文档失败');
            this._showLoading(false);
            return null;
        }
    }

    _enterReaderMode(doc) {
        this._isReaderMode = true;
        this._dom.shelfView.classList.add('hidden');
        this._dom.readerView.classList.remove('hidden');
        // 正文渲染完成前不显示上一本的进度轨道；`_updateProgressBar` 在章节
        // 落位后按实际章节数决定是否显示
        this._setProgressVisible(false);
        this._chromeTitle = doc.title || doc.id;
        // 副标题留空：壳的工具栏空间有限，"阅读" + 书名挤在一起会把书名截成单字
        // （实测显示成"书"），不如让书名自己占满
        this._chromeSub = '';
        // 阅读态由壳按 view='' 清掉左栏导航高亮，并显示返回动作、隐藏搜索；
        // 搜索框同时复位，离开阅读态后不会带着上一轮的关键词。
        this._clearSearch();
        this._pushChrome();
    }

    _openNonChapter(doc) {
        this.engine.reset(doc.id, [], 'auto');
        const area = this._dom.contentArea;
        if (!area) return;
        this._dom.progressFill.style.width = '0%';
        // PDF / 外部程序打开没有章节进度，底部进度轨道不显示
        this._setProgressVisible(false);
        if (doc.kind === 'pdf') {
            // WebView 自带 PDF 阅览器：同源 /file 路由直接嵌，零依赖
            const src = Bridge.originalUrl(doc.file_path);
            area.innerHTML = `
                <iframe class="document-pdf-frame" src="${Utils.escapeHtml(src)}"
                        title="${Utils.escapeHtml(doc.title || doc.id)}"></iframe>
                <button type="button" class="btn document-pdf-open" id="document-open-external">${nrAppIcon('icon:external-link')} 系统程序打开</button>
            `;
        } else {
            area.innerHTML = `
                <div class="empty-state">
                    <div class="empty-state-icon"><svg class="obx-icon"><use href="#file-text"></use></svg></div>
                    <div class="empty-state-text">${Utils.escapeHtml(doc.title || doc.id)}</div>
                    <div class="empty-state-hint">该格式不在阅读器内渲染，可交给系统默认程序打开</div>
                    <button type="button" class="btn" id="document-open-external">${nrAppIcon('icon:external-link')} 用系统程序打开</button>
                </div>`;
        }
        const btn = document.getElementById('document-open-external');
        if (btn) btn.addEventListener('click', () => this._openExternal(doc));
    }

    async _openExternal(doc) {
        try {
            const result = await Bridge.call('document_open_external', doc.id);
            if (result && result.error) Toast.error(result.error);
            else Toast.success('已交给系统程序打开');
        } catch (e) {
            Toast.error('调用系统程序失败');
        }
    }

    _afterPageRender() {
        this._updateProgressBar();
        this._updateTocSub();
        this._flashMarks();
    }

    _updateProgressBar() {
        const fill = this._dom.progressFill;
        const chapters = this.engine.chapters;
        if (!fill || !chapters.length) {
            this._setProgressVisible(false);
            return;
        }
        this._setProgressVisible(true);
        const progress = (this.engine.currentChapterIndex + this.engine.chapterFraction()) / chapters.length;
        fill.style.width = `${Math.max(0, Math.min(100, progress * 100)).toFixed(2)}%`;
    }

    /** 底部进度轨道只在"阅读器里有章节进度"时出现（书架、PDF、外部打开都隐藏）。 */
    _setProgressVisible(visible) {
        if (!this._dom.progressBar) return;
        this._dom.progressBar.classList.toggle('hidden', !visible);
    }

    // ===== 目录弹窗 =====

    // ===== 正文右键菜单 =====

    // ===== 书签 =====

    /** 当前视口顶部那一句的起点（朗读与书签共用的定位法则）。 */
    // ===== 进度 =====

    /** 章内位置统一夹到 0~1：后端存的是这个区间，越界值只可能是脏数据。 */
    _clampFraction(value) {
        const number = Number(value);
        if (!Number.isFinite(number)) return 0;
        return Math.max(0, Math.min(1, number));
    }

    /** 正文容器的滚动量（判断位置有没有动过的基准，见 `_saveCurrentProgress`）。 */
    _contentScrollTop() {
        const el = this._dom.contentArea;
        return el ? el.scrollTop : 0;
    }

    /**
     * 保存阅读进度（章号 + 章内位置）。
     *
     * `force=false` 只在"位置真的动过"时才写：关闭窗口那条路（beforeunload /
     * dispose）用它 —— 打开一本书看两眼就关掉，不该把上次读到的地方覆盖成刚打开
     * 的位置。
     *
     * 朗读时也要保存：朗读会带着滚动走，"上次读到哪"正是用户关掉应用后最想接着
     * 看的地方。朗读自己没有状态文件，与阅读进度不会互相污染。
     */
    async _saveCurrentProgress(force = false) {
        if (!this._isReaderMode || !this.currentDocument) return;
        if (!this.engine.chapters.length) return;
        const doc = this.currentDocument;
        const chapter = this.engine.currentChapterIndex;
        const fraction = this._clampFraction(this.engine.chapterFraction());
        const scrollTop = this._contentScrollTop();
        const saved = this._savedPosition;
        // 位置有没有动过按**像素**比，不按章内比例：比例是"当前这章的正文高度"算出来的，
        // 滚动条出现、字体度量这些重排会让同一次测量差上百分之几，比例判定会把"没动"
        // 误判成"动过"，于是照旧覆盖记录。
        if (!force && saved && saved.chapter === chapter
            && Math.abs(scrollTop - (Number(saved.scrollTop) || 0)) < 8) {
            return;
        }
        try {
            await Bridge.call('document_update_progress', doc.id, chapter, fraction, this.encoding);
        } catch (e) {
            console.error('保存进度失败:', e);
            return;
        }
        this._savedPosition = { chapter, fraction, scrollTop };
        // 就地回写这份书架条目：`openDocument` 与封面进度条读的都是 `this.documents`
        // 里的对象，不回写就会拿"进书架那一刻"的旧位置重开（表现成读完再进来又回到
        // 开头），而且此时保存的正是那个旧位置 —— 记录会被它一路覆盖。
        // **按 id 取当前那一份**，不能只写 `this.currentDocument`：打开之后书架只要
        // 重新拉过一次列表（刷新引擎的自动同步就会触发），内存里就是另一批对象，
        // 写回旧对象等于没写。
        const target = this.documents.find((item) => item.id === doc.id) || doc;
        for (const entry of target === doc ? [doc] : [target, doc]) {
            entry.last_read_chapter = chapter;
            entry.scroll_position = fraction;
            entry.last_read_time = this._nowStamp();
            const total = this.engine.chapters.length || Number(entry.chapter_count) || 0;
            if (total > 0) entry.progress = (chapter + fraction) / total;
        }
        if (!this._isReaderMode) {
            this._renderShelf(this._searchQuery);
        }
    }

    /** 与后端 `time.strftime('%Y-%m-%d %H:%M:%S')` 同形：最近阅读按字符串排序。 */
    _nowStamp() {
        const pad = (value) => String(value).padStart(2, '0');
        const now = new Date();
        return `${now.getFullYear()}-${pad(now.getMonth() + 1)}-${pad(now.getDate())}`
            + ` ${pad(now.getHours())}:${pad(now.getMinutes())}:${pad(now.getSeconds())}`;
    }

    async reloadDocument() {
        if (!this.currentDocument) return;
        const chapter = this.engine.currentChapterIndex;
        const fraction = this.engine.chapterFraction();
        await this._saveCurrentProgress(true);
        await this.openDocument(this.currentDocument.id, { chapter, fraction });
    }

    _showLoading(show) {
        let el = document.getElementById('document-loading');
        const host = this._dom.readerView || this._dom.contentArea;
        if (show) {
            if (!el) {
                el = document.createElement('div');
                el.id = 'document-loading';
                el.className = 'document-loading';
                // 旋转复用壳 effects.css 的 obx-anim-spin（含 reduced-motion 降级），
                // 本插件 CSS 只负责尺寸、描边与颜色。
                // 挂在阅读器容器（不是正文容器）：engine 换章时会清空正文 innerHTML，
                // 挂在正文里的提示会被一起删掉。
                el.innerHTML = '<div class="spinner obx-anim-spin"></div><span>加载中...</span>';
                if (host) host.appendChild(el);
            }
            el.style.display = 'flex';
        } else if (el) {
            el.style.display = 'none';
        }
    }

    /** 给朗读用：当前章正文文本 + 它的 DOM 容器。 */
    getChapterContext() {
        return this.engine.getChapterContext();
    }

    destroy() {
        this.tts.stop();
        this.currentDocument = null;
        this._isReaderMode = false;
    }
}

// ===== 轻量弹窗开关（壳的模态样式 + .active 类） =====
//
// 名字必须带 `Nr` 前缀：插件脚本与壳注入的 /shell/base.js 共享同一个全局对象，
// 顶层 `function openModal` 会把壳的 `openModal(options)`（弹窗工厂，内嵌页经宿主
// 通道渲染设置表单时也用它）整个替换掉 —— 那条通路点下去只有一条 TypeError。
// `closeModal` 同理（壳后续如需同名入口也会被顶掉）。

function openNrModal(id) {
    const el = document.getElementById(id);
    if (el) el.classList.add('active');
}

function closeNrModal(id) {
    const el = document.getElementById(id);
    if (el) el.classList.remove('active');
}
