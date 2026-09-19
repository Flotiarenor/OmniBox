// ===== 阅读偏好持久化 =====
//
// 存在**插件的统一设置**里（后端 settings_store），不再是 localStorage：
// 换设备、换浏览器后字号/主题/阅读模式都还在，也不受浏览器隐私模式影响
// （隐私模式下 localStorage 会直接抛，偏好根本存不上）。
//
// 写入是异步的且带防抖：拖动字号滑杆会连续触发 input，等手停下来再落盘一次。

const READER_PREF_KEYS = {
    fontSize: 'reader_font_size',
    lineHeight: 'reader_line_height',
    letterSpacing: 'reader_letter_spacing',
    theme: 'reader_theme',
    bgColor: 'reader_bg_color',
    textColor: 'reader_text_color',
    mode: 'reader_mode',
};

// 旧版本存在 localStorage 的偏好：读一次搬进后端设置，搬完丢掉。
const LEGACY_SETTINGS_KEY = 'document-reader-settings';
const LEGACY_SETTINGS_KEY_OLD = 'novel-reader-settings';
// v1 的文件里存着"旧默认值"（老版本 load() 末尾会 save 一遍，连没动过的设置也落盘）。
// 默认值改成「跟随主题 + 连续滚动」之后，那些记录并不是用户的选择：没有 version
// 字段的旧文件按新默认走一次，字号、配色这些真偏好原样保留。
const LEGACY_SETTINGS_VERSION = 2;
const READER_DEFAULT_THEME = 'auto';
const READER_DEFAULT_MODE = 'scroll';

class ReaderSettingsStore {
    constructor(app) {
        this.app = app;
        this._saveTimer = null;
    }

    /** 从后端设置读偏好并应用到界面。 */
    async load() {
        const app = this.app;
        let settings = {};
        try {
            settings = await Bridge.call('get_settings') || {};
        } catch (e) {
            settings = {};
        }
        // 后端还没有这些键（首次运行 / 刚从 localStorage 迁移）时给默认值
        app.fontSize = Number(settings.reader_font_size) || 16;
        app.lineHeight = Number(settings.reader_line_height) || 1.8;
        app.letterSpacing = Number(settings.reader_letter_spacing) || 0;
        app.theme = settings.reader_theme || 'auto';
        app.bgColor = settings.reader_bg_color || '#ffffff';
        app.textColor = settings.reader_text_color || '#1a1a1a';
        app.mode = settings.reader_mode === 'page' ? 'page' : 'scroll';
        this._migrateLegacy(settings);
        this.apply(true);
    }

    /** 老版本把偏好写在 localStorage，搬一次到后端设置。 */
    _migrateLegacy(backendSettings) {
        const app = this.app;
        let saved = null;
        try {
            saved = localStorage.getItem(LEGACY_SETTINGS_KEY)
                || localStorage.getItem(LEGACY_SETTINGS_KEY_OLD);
        } catch (e) {
            return;      // 隐私模式：没有旧数据可搬
        }
        if (!saved) return;
        try {
            const legacy = JSON.parse(saved);
            // 没有 version 的旧文件：主题/模式是当年自动落盘的旧默认值，按新默认走；
            // 字号、行距、配色是用户真调过的，原样搬过来。
            const oldDefaults = legacy.version !== LEGACY_SETTINGS_VERSION;
            if (legacy.fontSize) app.fontSize = legacy.fontSize;
            if (legacy.lineHeight) app.lineHeight = legacy.lineHeight;
            if (legacy.letterSpacing !== undefined) app.letterSpacing = legacy.letterSpacing;
            app.theme = oldDefaults ? READER_DEFAULT_THEME : (legacy.theme || READER_DEFAULT_THEME);
            if (legacy.bgColor) app.bgColor = legacy.bgColor;
            if (legacy.textColor) app.textColor = legacy.textColor;
            app.mode = oldDefaults ? READER_DEFAULT_MODE : (legacy.mode || READER_DEFAULT_MODE);
            // 后端已经有值时以它为准（说明之前迁移过），避免每次启动都覆盖用户新选择
            this.save(!backendSettings.reader_theme);
        } catch (e) {
            return;
        }
        try {
            localStorage.removeItem(LEGACY_SETTINGS_KEY);
            localStorage.removeItem(LEGACY_SETTINGS_KEY_OLD);
        } catch (e) {
            /* 清不掉也无所谓：下次读到后端已有值就不会再搬 */
        }
    }

    /** 应用当前偏好：只改两个变量与档位类，排版规则全部读变量。 */
    apply(skipSave = false) {
        const app = this.app;
        const contentArea = app._dom.contentArea;
        if (contentArea) {
            contentArea.style.setProperty('--reader-font-size', `${app.fontSize}px`);
            contentArea.style.setProperty('--reader-line-height', app.lineHeight);
            contentArea.style.setProperty('--reader-letter-spacing', `${app.letterSpacing}px`);
            contentArea.classList.remove('theme-auto', 'theme-sepia', 'theme-dark',
                'theme-green', 'theme-blue', 'theme-custom');
            contentArea.classList.add(`theme-${app.theme}`);
            if (app.theme === 'custom') {
                contentArea.style.setProperty('--reader-bg-color', app.bgColor);
                contentArea.style.setProperty('--reader-text-color', app.textColor);
            } else {
                contentArea.style.removeProperty('--reader-bg-color');
                contentArea.style.removeProperty('--reader-text-color');
            }
        }
        const dom = app._dom;
        if (dom.fontSizeSlider) dom.fontSizeSlider.value = app.fontSize;
        if (dom.fontSizeValue) dom.fontSizeValue.textContent = app.fontSize;
        if (dom.lineHeightSlider) dom.lineHeightSlider.value = app.lineHeight;
        if (dom.lineHeightValue) dom.lineHeightValue.textContent = app.lineHeight.toFixed(1);
        if (dom.letterSpacingSlider) dom.letterSpacingSlider.value = app.letterSpacing;
        if (dom.letterSpacingValue) dom.letterSpacingValue.textContent = `${app.letterSpacing}px`;
        if (dom.themeSelect) dom.themeSelect.value = app.theme;
        if (dom.modeSelect) dom.modeSelect.value = app.mode;
        if (dom.bgColorInput) dom.bgColorInput.value = app.bgColor;
        if (dom.textColorInput) dom.textColorInput.value = app.textColor;
        if (app._syncCustomColorRows) app._syncCustomColorRows();
        if (!skipSave) this.save();
    }

    /** 落盘（防抖）。`immediate` 用于迁移这种"必须马上写"的场景。 */
    save(immediate = false) {
        clearTimeout(this._saveTimer);
        const write = async () => {
            const app = this.app;
            const payload = {};
            payload[READER_PREF_KEYS.fontSize] = app.fontSize;
            payload[READER_PREF_KEYS.lineHeight] = app.lineHeight;
            payload[READER_PREF_KEYS.letterSpacing] = app.letterSpacing;
            payload[READER_PREF_KEYS.theme] = app.theme;
            payload[READER_PREF_KEYS.bgColor] = app.bgColor;
            payload[READER_PREF_KEYS.textColor] = app.textColor;
            payload[READER_PREF_KEYS.mode] = app.mode;
            try {
                await Bridge.call('save_settings', payload);
            } catch (e) {
                console.error('保存阅读偏好失败:', e);
            }
        };
        if (immediate) {
            write();
            return;
        }
        this._saveTimer = setTimeout(write, 400);
    }
}
