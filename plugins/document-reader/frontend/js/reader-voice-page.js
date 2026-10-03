// ============================================================
// 阅读器 — 朗读设置独立页
//
// 为什么不塞进阅读设置弹窗：这里要放引擎、端点、Key、模型、音色、语速与试听，
// 是个"配置一次就不动"的页面；而字号主题是随时要调的。两者混在一个弹窗里，
// 高频项会被低频项淹没（见 docs/document-reader-design.md §2）。
//
// 保存一律走插件的 `save_settings`（同一个 settings_schema），
// 因此这里改的就是插件设置面板里那几项，不存在两份配置。
// ============================================================

// 图标值（`icon:名字`）→ 标记。sprite 与 Icons.html 由壳注入的 /shell/icons.generated.js 提供；
// 缺失时（本页脱离壳单独打开）返回空串，不写 emoji 兜底。
const nrVoiceIcon = (name) => (window.Icons && typeof window.Icons.html === 'function')
    ? window.Icons.html(name)
    : '';

// edge 端点上可见的中文音色（2026-09 实测；音色池由微软维护，会变）。
// 只在"问了后端也问不到 edge 音色"时给个能点的兜底：列表本身不该因为断网就整块空掉。
// 端点那一档没有兜底表 —— 名字由端点自己定，写死一份只会给出不存在的音色。
const EDGE_VOICE_FALLBACK = [
    { name: 'zh-CN-XiaoxiaoNeural', note: '晓晓 · 女 · 通用最自然' },
    { name: 'zh-CN-XiaoyiNeural', note: '晓伊 · 女 · 年轻活泼' },
    { name: 'zh-CN-YunxiNeural', note: '云希 · 男 · 叙述节奏快' },
    { name: 'zh-CN-YunyangNeural', note: '云扬 · 男 · 播报腔最稳' },
    { name: 'zh-CN-YunjianNeural', note: '云健 · 男 · 解说腔' },
    { name: 'zh-CN-YunxiaNeural', note: '云夏 · 男童' },
    { name: 'zh-CN-liaoning-XiaobeiNeural', note: '晓北 · 东北话' },
    { name: 'zh-CN-shaanxi-XiaoniNeural', note: '晓妮 · 陕西话' },
];

class ReaderVoicePage {
    constructor(app) {
        this.app = app;
        this._bound = false;
        this._saving = null;
        // 两张音色表各自的缓存：切引擎时不必再打一次网络（端点那次要几秒）
        this._edgeVoices = null;
        this._endpointVoices = null;
    }

    _dom() {
        return {
            page: document.getElementById('nr-voice-page'),
            engine: document.getElementById('nr-voice-engine'),
            base: document.getElementById('nr-voice-base'),
            key: document.getElementById('nr-voice-key'),
            model: document.getElementById('nr-voice-model'),
            voice: document.getElementById('nr-voice-name'),
            rate: document.getElementById('nr-voice-rate'),
            rateValue: document.getElementById('nr-voice-rate-value'),
            status: document.getElementById('nr-voice-status'),
            // 两档音色各占一张卡片：不出的那一档整张隐藏（见 _renderVoices）
            edgeList: document.getElementById('nr-voice-list-edge'),
            endpointList: document.getElementById('nr-voice-list-endpoint'),
            edgeCard: document.getElementById('nr-voice-card-edge'),
            endpointCard: document.getElementById('nr-voice-card-endpoint'),
            test: document.getElementById('nr-voice-test'),
            testResult: document.getElementById('nr-voice-test-result'),
        };
    }

    open() {
        const dom = this._dom();
        if (!dom.page) return;
        this._bind();
        // 朗读设置是**主区的内容切换**（与书架网格、正文同一个位置），不是遮挡页：
        // 左栏的"朗读设置"按钮保持选中，用户可以随时切回书架或正文
        dom.page.classList.remove('hidden');
        const shelf = document.getElementById('nr-shelf-view');
        const reader = document.getElementById('nr-reader-view');
        if (shelf) shelf.classList.add('hidden');
        if (reader) reader.classList.add('hidden');
        document.querySelectorAll('.nr-nav-item').forEach((btn) => {
            btn.classList.toggle('active', btn.id === 'nr-open-voice');
        });
        this.app._voicePaneOpen = true;
        this._load();
        this.app.tts.refreshStatus().then(() => this._renderStatus());
    }

    close() {
        const dom = this._dom();
        if (dom.page) dom.page.classList.add('hidden');
        this.app._voicePaneOpen = false;
        // 回哪儿去：正在阅读就回正文，否则回书架
        const back = this.app._isReaderMode ? 'nr-reader-view' : 'nr-shelf-view';
        const target = document.getElementById(back);
        if (target) target.classList.remove('hidden');
        this.app._syncNavActive();
    }

    _bind() {
        if (this._bound) return;
        this._bound = true;
        const dom = this._dom();
        // 没有"返回"按钮：朗读设置是左栏的导航项，切回去点书架/书签即可
        // （与其它主区面板一致，少一个语义重复的按钮）
        document.getElementById('nr-voice-refresh').addEventListener('click', async () => {
            await this.app.tts.refreshStatus();
            this._renderStatus();
            Toast.info('已重新检测引擎');
        });
        dom.rate.addEventListener('input', () => {
            dom.rateValue.textContent = `${dom.rate.value}%`;
            // 语速是连续输入，防抖落盘即可
            this._save({ tts_rate: parseInt(dom.rate.value, 10) });
        });
        // 离散控件改完就生效：它们后面往往紧接着"试听"，等防抖会合成到旧值
        [dom.engine, dom.base, dom.model, dom.voice].forEach((input) => {
            input.addEventListener('change', () => this._save(this._collect(), true));
        });
        // 引擎与端点决定"有哪两档音色"，改完立刻按缓存重画（不等网络）
        [dom.engine, dom.base, dom.model].forEach((input) => {
            input.addEventListener('change', () => this._renderVoices(this._buildTables()));
        });
        // 手工改音色名（不点列表）时，选中态跟着走
        dom.voice.addEventListener('change', () => this._markActiveVoice());
        dom.key.addEventListener('change', () => this._save({ tts_api_key: dom.key.value }, true));
        document.getElementById('nr-voice-cache-clear')
            .addEventListener('click', () => this._clearCache());
        document.getElementById('nr-voice-cache-open')
            .addEventListener('click', () => this._openCacheDir());
        dom.test.addEventListener('click', () => this._test());
        this._renderVoices();
        this._renderCache();
    }

    /** 缓存位置与占用：用户不必自己去翻目录找。 */
    async _renderCache() {
        const pathEl = document.getElementById('nr-voice-cache-path');
        const sizeEl = document.getElementById('nr-voice-cache-size');
        if (!pathEl || !sizeEl) return;
        let info = null;
        try {
            info = await Bridge.call('tts_cache_info');
        } catch (e) {
            info = null;
        }
        if (!info || info.error) {
            pathEl.textContent = '读取失败';
            sizeEl.textContent = '';
            return;
        }
        this._cachePath = info.path;
        pathEl.textContent = info.path;
        pathEl.title = info.path;
        const mb = (info.bytes || 0) / 1024 / 1024;
        sizeEl.textContent = `${info.files} 个文件 · ${mb.toFixed(1)} MB（上限 ${(info.limitBytes || 0) / 1024 / 1024 | 0} MB，超出自动清理最旧的）`;
    }

    async _clearCache() {
        const sizeEl = document.getElementById('nr-voice-cache-size');
        const ok = await confirmDialog('清空朗读缓存？\n\n音频会按需重新合成，书签与阅读进度不受影响。',
            { danger: true });
        if (!ok) return;
        try {
            const result = await Bridge.call('tts_clear_cache');
            if (result && result.success) {
                Toast.success(`已清理 ${result.removed} 个文件`);
                if (sizeEl) sizeEl.textContent = '已清空';
            } else {
                Toast.error((result && result.error) || '清理失败');
            }
        } catch (e) {
            Toast.error('清理失败');
        }
    }

    /** 打开缓存所在文件夹：真调系统文件管理器（不是只复制路径）。 */
    async _openCacheDir() {
        try {
            const result = await Bridge.call('tts_open_cache_dir');
            if (!result || !result.success) {
                Toast.error((result && result.error) || '打开目录失败');
                return;
            }
            Toast.success('已打开缓存目录');
        } catch (e) {
            Toast.error('打开目录失败');
        }
    }

    async _load() {
        const dom = this._dom();
        let settings = {};
        try {
            // 不要传参数：本插件没有覆写 get_settings，基类签名是 `get_settings(self)`，
            // 多传一个位置参数就是 500（"takes 1 positional argument but 2 were given"）。
            // image-viewer 那种 `Bridge.call('get_settings', '')` 写法能被它自己的
            // `get_settings(self, rel_path='')` 覆写接住，本插件没有那层覆写。
            settings = await Bridge.call('get_settings') || {};
        } catch (e) {
            settings = {};
        }
        dom.engine.value = settings.tts_engine || 'auto';
        dom.base.value = settings.tts_base_url || '';
        dom.key.value = settings.tts_api_key === '********' ? '' : (settings.tts_api_key || '');
        dom.model.value = settings.tts_model || '';
        dom.voice.value = settings.tts_voice || 'zh-CN-XiaoxiaoNeural';
        dom.rate.value = settings.tts_rate === undefined || settings.tts_rate === null ? 100 : settings.tts_rate;
        dom.rateValue.textContent = `${dom.rate.value}%`;
        this._renderStatus();
        // 先把"按当前引擎该显示哪些表"画出来（不依赖网络，切引擎也走这里），
        // 再拉一次后端的权威清单把名字换成端点上真实的那批。
        this._renderVoices(this._buildTables());
        this._loadVoices();
    }

    /** 后端清单：按当前引擎给表（自动模式给 edge + 端点两张，system 一张都不给）。 */
    async _loadVoices() {
        let result = null;
        try {
            result = await Bridge.call('tts_voices');
        } catch (e) {
            result = null;
        }
        const tables = (result && result.tables) || [];
        // 把两份音色名缓存下来：之后用户切引擎时不必再等网络
        tables.forEach((table) => this._cacheTable(table));
        // 后端回答的是"保存那一刻的引擎"；用户可能已经改了下拉框，那种情况以本地算的为准
        const expected = (result && result.mode) || 'auto';
        if (this._dom().engine.value !== expected) {
            this._renderVoices(this._buildTables());
            return;
        }
        this._renderVoices(this._buildTables());
    }

    _cacheTable(table) {
        if (table.source === 'edge' && table.voices && table.voices.length) {
            this._edgeVoices = table.voices;
        }
        if (table.source === 'endpoint' && table.voices && table.voices.length) {
            // 连端点一起记：换了端点就不能再显示上一个端点的音色（它们是两套名字）
            this._endpointVoices = { base: this._dom().base.value.trim(), voices: table.voices };
        }
    }

    /**
     * 按**当前下拉框里的引擎**算音色表。
     *
     * 用本地缓存的音色名而不是再问一次后端：切一下引擎就等几秒网络（端点那次尤其慢）
     * 是不可接受的；名字的权威来源仍然是后端的 `tts_voices`，这里只是把它换一种组合方式。
     */
    _buildTables() {
        const dom = this._dom();
        const mode = dom.engine.value || 'auto';
        if (mode === 'system') return [];
        const base = dom.base.value.trim();
        const tables = [];
        if (mode === 'edge' || mode === 'auto') tables.push(this._edgeTable());
        if (mode === 'openai' || mode === 'auto') tables.push(this._endpointTable(base));
        return tables;
    }

    _edgeTable() {
        const voices = this._edgeVoices;
        return {
            source: 'edge',
            title: 'edge-tts 音色',
            // 问不到 edge 端点时给一份"能点的"常用表：整块空掉比不完整更糟
            voices: voices && voices.length ? voices : EDGE_VOICE_FALLBACK,
            unavailable: !(voices && voices.length),
        };
    }

    _endpointTable(base) {
        // 缓存按端点分开存：换端点后不能继续显示上一个端点的音色
        const cached = this._endpointVoices;
        const voices = cached && cached.base === base ? cached.voices : null;
        return {
            source: 'endpoint',
            title: 'OpenAI 兼容端点音色',
            voices: voices || [],
            unavailable: !voices,
        };
    }

    _collect() {
        const dom = this._dom();
        return {
            tts_engine: dom.engine.value,
            tts_base_url: dom.base.value.trim(),
            tts_model: dom.model.value.trim(),
            tts_voice: dom.voice.value.trim(),
        };
    }

    /**
     * 保存设置，返回"真正写完"的 promise。
     *
     * `immediate` 用于点选音色这类"选完马上就要用到"的改动：防抖保存会让紧随其后的
     * 试听读到**旧音色**（实测：点云希立刻试听，后端合成出来的还是晓晓）。
     * 只有拖语速滑杆这种连续输入才需要防抖。
     *
     * 必须返回 write() 本身的 promise：返回一个已 resolve 的 promise 会让
     * `await this._save(...)` 立刻通过，试听照样跑在保存之前。
     */
    _save(patch, immediate = false) {
        clearTimeout(this._saving);
        const write = async () => {
            this._saving = null;
            try {
                const result = await Bridge.call('save_settings', patch);
                if (result && result.success === false) Toast.error(result.error || '保存失败');
            } catch (e) {
                Toast.error('保存朗读设置失败');
            }
        };
        if (immediate) return write();
        this._saving = setTimeout(write, 300);
        return Promise.resolve();
    }

    _renderStatus() {
        const dom = this._dom();
        const status = this.app.tts.status;
        if (!status || !status.engines) {
            dom.status.textContent = '引擎状态不可用（后端 tts_status 调用失败）';
            return;
        }
        // 图标用 innerHTML 写（标记是仓库常量），引擎名一律走文本节点：
        // 原来整段走 textContent，改成 innerHTML 时名称必须继续按纯文本插入。
        dom.status.textContent = '';
        status.engines.forEach((item, index) => {
            if (index) dom.status.appendChild(document.createTextNode('　'));
            const icon = document.createElement('span');
            icon.innerHTML = nrVoiceIcon(item.available ? 'icon:circle-check' : 'icon:ban');
            dom.status.append(icon, document.createTextNode(' ' + item.label));
        });
    }

    /**
     * 画音色表。`tables` 每一项是一档引擎的候选音色（见后端 `tts_voices`）。
     *
     * 两档各占一张卡片（edge 一张、端点一张），不出的那一档整张隐藏 ——
     * 自动模式两张都在，选定引擎只留对应的那张，系统离线音色一张都不出。
     * 卡片里的"问不到"要说清原因：端点没实现 `/v1/voices` 这种路由是常态，
     * 那时不该显示一张空表，而要说明照旧可以手填名字。
     */
    _renderVoices(tables) {
        const dom = this._dom();
        const targets = { edge: [dom.edgeCard, dom.edgeList], endpoint: [dom.endpointCard, dom.endpointList] };
        for (const [source, [card, list]] of Object.entries(targets)) {
            if (!card || !list) continue;
            const table = (tables || []).find((item) => item.source === source);
            card.classList.toggle('hidden', !table);
            list.innerHTML = !table ? '' : (table.voices.length
                ? table.voices.map((voice) => `
                    <button type="button" class="nr-voice-item" data-voice="${voice.name}">
                        <span class="nr-voice-name">${voice.note || voice.name}</span>
                        <span class="nr-voice-id">${voice.name}</span>
                    </button>`).join('')
                : '<p class="nr-voice-hint">问不到这一档的候选音色，直接填名字也能用。</p>');
        }
        for (const list of [dom.edgeList, dom.endpointList]) {
            if (!list) continue;
            list.querySelectorAll('.nr-voice-item').forEach((btn) => {
                btn.addEventListener('click', () => {
                    dom.voice.value = btn.dataset.voice;
                    // 立刻落盘：紧接着的"试听"要读到这个音色
                    this._save({ tts_voice: btn.dataset.voice }, true);
                    this._markActiveVoice();
                    Toast.info(`已选择 ${btn.dataset.voice}`);
                });
            });
        }
        this._markActiveVoice();
    }

    /** 标出当前音色在**哪一张卡片**里 —— 也就是自动模式下真正会去念它的那一档。 */
    _markActiveVoice() {
        const dom = this._dom();
        const want = (dom.voice.value || '').trim();
        for (const list of [dom.edgeList, dom.endpointList]) {
            if (!list) continue;
            list.querySelectorAll('.nr-voice-item').forEach((btn) => {
                btn.classList.toggle('active', btn.dataset.voice === want);
            });
        }
    }

    async _test() {
        const dom = this._dom();
        // 先把"刚选的音色"落盘，再合成：否则后端读到的还是上一个音色
        await this._save(this._collect(), true);
        dom.testResult.textContent = '合成中…';
        dom.test.disabled = true;
        try {
            const text = '这一段用来试听音色，语速与音调以当前设置为准。';
            const result = await Bridge.call('tts_speak', text, '', 0);
            if (result && result.error) {
                dom.testResult.textContent = `失败：${result.error}`;
                return;
            }
            dom.testResult.textContent = `引擎：${result.engine || '?'}（缓存${result.cached ? '命中' : '未命中'}）`;
            // 音频对象必须挂在实例上：`new Audio()` 不入 DOM，局部变量在函数返回后
            // 就被回收，Chromium 会中断已经开始播放的音频（表现是只念出开头几个字）。
            if (this._preview) {
                this._preview.pause();
                this._preview.removeAttribute('src');
            }
            this._preview = new Audio(result.url);
            this._preview.play().catch(() => Toast.info('自动播放被拦，请手动点一下播放'));
        } catch (e) {
            dom.testResult.textContent = `失败：${e}`;
        } finally {
            dom.test.disabled = false;
        }
    }
}
