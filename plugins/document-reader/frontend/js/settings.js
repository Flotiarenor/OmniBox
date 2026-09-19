// ===== 阅读器设置持久化与应用 =====
const READER_SETTINGS_KEY = 'document-reader-settings';
// 插件改名前的键：读过一次就搬到新键，用户调好的字号/主题不至于丢
const LEGACY_SETTINGS_KEY = 'novel-reader-settings';
const READER_SETTINGS_VERSION = 2;
const READER_DEFAULT_THEME = 'auto';    // 跟随主题
const READER_DEFAULT_MODE = 'scroll';   // 连续滚动

class ReaderSettingsStore {
    constructor(app) {
        this.app = app;
    }

    load() {
        const app = this.app;
        try {
            let saved = localStorage.getItem(READER_SETTINGS_KEY);
            if (!saved) {
                saved = localStorage.getItem(LEGACY_SETTINGS_KEY);
                if (saved) localStorage.setItem(READER_SETTINGS_KEY, saved);
            }
            if (saved) {
                const settings = JSON.parse(saved);
                // v1 的设置文件里存着"旧默认值"：load() 末尾的 apply() 会自动 save 一遍，
                // 连没动过的设置也落了盘。默认值改成「跟随主题 + 连续滚动」之后，那些记录
                // 并不是用户的选择 —— 没有 version 字段的旧文件按新默认走一次，
                // 字号、行距、自定义配色原样保留。
                const oldDefaults = settings.version !== READER_SETTINGS_VERSION;
                app.fontSize = settings.fontSize || 16;
                app.lineHeight = settings.lineHeight || 1.8;
                app.letterSpacing = settings.letterSpacing || 0;
                app.theme = oldDefaults ? READER_DEFAULT_THEME : (settings.theme || READER_DEFAULT_THEME);
                app.bgColor = settings.bgColor || '#ffffff';
                app.textColor = settings.textColor || '#1a1a1a';
                app.mode = oldDefaults ? READER_DEFAULT_MODE : (settings.mode || READER_DEFAULT_MODE);
            }
        } catch (e) {
            console.error('加载设置失败:', e);
        }

        const dom = app._dom;
        if (dom.fontSizeSlider) dom.fontSizeSlider.value = app.fontSize;
        if (dom.fontSizeValue) dom.fontSizeValue.textContent = app.fontSize;
        if (dom.lineHeightSlider) dom.lineHeightSlider.value = app.lineHeight;
        if (dom.lineHeightValue) dom.lineHeightValue.textContent = app.lineHeight.toFixed(1);
        if (dom.letterSpacingSlider) dom.letterSpacingSlider.value = app.letterSpacing;
        if (dom.letterSpacingValue) dom.letterSpacingValue.textContent = `${app.letterSpacing}px`;
        if (dom.themeSelect) dom.themeSelect.value = app.theme;
        if (dom.modeSelect) dom.modeSelect.value = app.mode || READER_DEFAULT_MODE;
        if (dom.bgColorInput) dom.bgColorInput.value = app.bgColor;
        if (dom.textColorInput) dom.textColorInput.value = app.textColor;

        const isCustom = app.theme === 'custom';
        if (dom.customColorLabel) dom.customColorLabel.style.display = isCustom ? 'inline-flex' : 'none';
        if (dom.customTextLabel) dom.customTextLabel.style.display = isCustom ? 'inline-flex' : 'none';
        this.apply();
    }

    apply() {
        const app = this.app;
        const contentArea = app._dom.contentArea;
        if (!contentArea) return;
        contentArea.style.setProperty('--reader-font-size', `${app.fontSize}px`);
        contentArea.style.setProperty('--reader-line-height', app.lineHeight);
        contentArea.style.setProperty('--reader-letter-spacing', `${app.letterSpacing}px`);

        // 主题设计：默认「跟随主题」，完全使用 Shell 的 CSS 变量，
        // 与设置中的浅色 / 深色 / 自定义配色自动同步。
        if (app.theme === 'auto') {
            contentArea.className = 'document-content-area theme-auto';
            contentArea.style.removeProperty('--reader-bg-color');
            contentArea.style.removeProperty('--reader-text-color');
        } else if (app.theme === 'custom') {
            contentArea.className = 'document-content-area theme-custom';
            contentArea.style.setProperty('--reader-bg-color', app.bgColor);
            contentArea.style.setProperty('--reader-text-color', app.textColor);
        } else {
            contentArea.style.removeProperty('--reader-bg-color');
            contentArea.style.removeProperty('--reader-text-color');
            contentArea.className = `document-content-area theme-${app.theme}`;
        }
        this.save();
    }

    save() {
        const app = this.app;
        const settings = {
            version: READER_SETTINGS_VERSION,
            fontSize: app.fontSize,
            lineHeight: app.lineHeight,
            letterSpacing: app.letterSpacing,
            theme: app.theme,
            bgColor: app.bgColor,
            textColor: app.textColor,
            mode: app.mode,
        };
        localStorage.setItem(READER_SETTINGS_KEY, JSON.stringify(settings));
    }
}
