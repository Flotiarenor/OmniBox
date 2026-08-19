// ===== 阅读器设置持久化与应用 =====
class ReaderSettingsStore {
    constructor(app) {
        this.app = app;
    }

    load() {
        const app = this.app;
        try {
            const saved = localStorage.getItem('novel-reader-settings');
            if (saved) {
                const settings = JSON.parse(saved);
                app.fontSize = settings.fontSize || 16;
                app.lineHeight = settings.lineHeight || 1.8;
                app.letterSpacing = settings.letterSpacing || 0;
                app.theme = settings.theme || 'light';
                app.bgColor = settings.bgColor || '#ffffff';
                app.textColor = settings.textColor || '#1a1a1a';
                app.encoding = settings.encoding || 'auto';
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
        if (dom.bgColorInput) dom.bgColorInput.value = app.bgColor;
        if (dom.textColorInput) dom.textColorInput.value = app.textColor;
        if (dom.encodingSelect) dom.encodingSelect.value = app.encoding;

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
        if (app.theme === 'custom') {
            contentArea.style.setProperty('--reader-bg-color', app.bgColor);
            contentArea.style.setProperty('--reader-text-color', app.textColor);
            contentArea.className = 'novel-content-area';
        } else {
            contentArea.className = `novel-content-area theme-${app.theme}`;
        }
        this.save();
    }

    save() {
        const app = this.app;
        const settings = {
            fontSize: app.fontSize,
            lineHeight: app.lineHeight,
            letterSpacing: app.letterSpacing,
            theme: app.theme,
            bgColor: app.bgColor,
            textColor: app.textColor,
            encoding: app.encoding,
        };
        localStorage.setItem('novel-reader-settings', JSON.stringify(settings));
    }
}
