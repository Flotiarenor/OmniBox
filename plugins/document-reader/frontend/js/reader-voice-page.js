// ============================================================
// 阅读器 — 朗读设置独立页
//
// 为什么不塞进阅读设置弹窗：这里要放引擎、端点、Key、模型、音色、语速与试听，
// 是个"配置一次就不动"的页面；而字号主题是随时要调的。两者混在一个弹窗里，
// 高频项会被低频项淹没（见 docs/document-reader-redesign.md §2）。
//
// 保存一律走插件的 `save_settings`（同一个 settings_schema），
// 因此这里改的就是插件设置面板里那几项，不存在两份配置。
// ============================================================

// edge 端点上可见的中文音色（2026-09 实测；音色池由微软维护，会变）。
// 写成静态表是为了不依赖网络：列表本身不该因为断网就空掉。
const TTS_VOICE_SUGGESTIONS = [
    ['zh-CN-XiaoxiaoNeural', '晓晓 · 女 · 通用最自然'],
    ['zh-CN-XiaoyiNeural', '晓伊 · 女 · 年轻活泼'],
    ['zh-CN-YunxiNeural', '云希 · 男 · 叙述节奏快'],
    ['zh-CN-YunyangNeural', '云扬 · 男 · 播报腔最稳'],
    ['zh-CN-YunjianNeural', '云健 · 男 · 解说腔'],
    ['zh-CN-YunxiaNeural', '云夏 · 男童'],
    ['zh-CN-liaoning-XiaobeiNeural', '晓北 · 东北话'],
    ['zh-CN-shaanxi-XiaoniNeural', '晓妮 · 陕西话'],
];

class ReaderVoicePage {
    constructor(app) {
        this.app = app;
        this._bound = false;
        this._saving = null;
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
            list: document.getElementById('nr-voice-list'),
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
        dom.key.addEventListener('change', () => this._save({ tts_api_key: dom.key.value }, true));
        dom.test.addEventListener('click', () => this._test());
        this._renderVoices();
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
        this._loadVoicesFromEndpoint();
    }

    /** 音色列表问后端（端点上的真实音色），失败才退回内置常用表。 */
    async _loadVoicesFromEndpoint() {
        const dom = this._dom();
        if (dom.list) dom.list.innerHTML = '<div class="nr-voice-hint">正在读取端点音色…</div>';
        let result = null;
        try {
            result = await Bridge.call('tts_voices');
        } catch (e) {
            result = null;
        }
        const voices = (result && result.voices) || [];
        if (voices.length) {
            this._renderVoices(voices.map((item) => ({
                name: item.name,
                note: item.note || `${item.gender} · ${item.locale}`,
            })), result.source);
            return;
        }
        // 端点不可达：用内置的常用音色兜底，并说明这不是完整列表
        this._renderVoices(TTS_VOICE_SUGGESTIONS.map(([name, note]) => ({ name, note })),
            'fallback');
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
        dom.status.textContent = status.engines
            .map((item) => `${item.available ? '✅' : '⛔'} ${item.label}`)
            .join('　');
    }

    _renderVoices(items = null, source = '') {
        const dom = this._dom();
        if (!dom.list) return;
        const list = items || TTS_VOICE_SUGGESTIONS.map(([name, note]) => ({ name, note }));
        const hint = source === 'fallback'
            ? '<div class="nr-voice-hint">⚠️ 读不到 edge 端点，下面是内置的常用音色（非完整列表）</div>'
            : '';
        dom.list.innerHTML = hint + list.map((item) => `
            <button type="button" class="nr-voice-item" data-voice="${item.name}">
                <span class="nr-voice-name">${item.note || item.name}</span>
                <span class="nr-voice-id">${item.name}</span>
            </button>`).join('');
        dom.list.querySelectorAll('.nr-voice-item').forEach((btn) => {
            btn.addEventListener('click', () => {
                dom.voice.value = btn.dataset.voice;
                // 立刻落盘：紧接着的"试听"要读到这个音色
                this._save({ tts_voice: btn.dataset.voice }, true);
                dom.list.querySelectorAll('.nr-voice-item').forEach((other) => {
                    other.classList.toggle('active', other === btn);
                });
                Toast.info(`已选择 ${btn.dataset.voice}`);
            });
        });
        const current = dom.list.querySelector(`.nr-voice-item[data-voice="${dom.voice.value}"]`);
        if (current) current.classList.add('active');
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
