// ===== 歌词解析与全屏显示 =====
class LyricsParser {
    static parse(lrcText) {
        if (!lrcText || !lrcText.trim()) return [];

        const lines = lrcText.split('\n');
        const result = [];

        for (const line of lines) {
            const match = line.match(/^\[(\d{2}):(\d{2})\.(\d{2,3})\](.*)/);
            if (!match) continue;

            const minutes = parseInt(match[1], 10);
            const seconds = parseInt(match[2], 10);
            const millis = parseInt(match[3], 10);
            const time = minutes * 60 + seconds + millis / (match[3].length === 2 ? 100 : 1000);
            const text = match[4].trim();

            if (text) {
                result.push({ time, text });
            }
        }

        result.sort((a, b) => a.time - b.time);
        return result;
    }
}

class LyricsDisplay {
    constructor(app) {
        this._app = app;

        this._lines = [];
        this._currentIndex = -1;
        this._visible = false;
        this._userScrolling = false;
        this._scrollTimer = null;
        this._scrollBound = false;

        this._page = document.getElementById('lyrics-page');
        this._bg = document.getElementById('lyrics-page-bg');
        this._blur = document.getElementById('lyrics-page-blur');
        this._content = document.getElementById('lyrics-page-content');
        this._title = document.getElementById('lyrics-page-title');
        this._progressBar = document.getElementById('lyrics-progress-bar');
        this._timeCur = document.getElementById('lyrics-time-current');
        this._timeDur = document.getElementById('lyrics-time-duration');

        this._settings = {};
        this._loadSettings();
    }

    _audioEl() {
        return this._app && this._app.player ? this._app.player.audio : null;
    }

    // ===== 设置加载 =====
    _loadSettings() {
        const defaults = {
            lyrics_font_size: 16,
            lyrics_active_size: 24,
            lyrics_line_height: 1.6,
            lyrics_glow: true,
            lyrics_align: 'center',
            lyrics_bg_color: '',
            lyrics_bg_image: '',
            lyrics_font_color: '#ffffff',
            lyrics_bg_blur: 8,
            lyrics_bg_brightness: 0.25,
        };
        try {
            const saved = JSON.parse(localStorage.getItem('musicLyricsSettings') || '{}');
            this._settings = { ...defaults, ...saved };
        } catch (e) {
            this._settings = { ...defaults };
        }
    }

    async refreshSettings() {
        try {
            const settings = await Bridge.call('get_settings');
            if (!settings) return;
            const keys = ['lyrics_font_size', 'lyrics_active_size', 'lyrics_line_height',
                          'lyrics_glow', 'lyrics_align', 'lyrics_bg_color', 'lyrics_bg_image',
                          'lyrics_font_color', 'lyrics_bg_blur', 'lyrics_bg_brightness'];
            let changed = false;
            keys.forEach(k => {
                if (settings[k] !== undefined && settings[k] !== this._settings[k]) {
                    this._settings[k] = settings[k];
                    changed = true;
                }
            });
            if (changed) {
                localStorage.setItem('musicLyricsSettings', JSON.stringify(this._settings));
                this._applyStyleVars();
                this._render();
            }
        } catch (e) {}
    }

    // ===== 样式应用 =====
    _applyStyleVars() {
        if (!this._page) return;
        const { lyrics_font_color, lyrics_bg_blur, lyrics_bg_brightness } = this._settings;

        const fc = lyrics_font_color || '#ffffff';
        this._page.style.setProperty('--lyrics-font-color', fc + '38');
        this._page.style.setProperty('--lyrics-font-color-active', fc);
        this._page.style.setProperty('--lyrics-glow-color', fc + '80');

        const blur = lyrics_bg_blur !== undefined ? lyrics_bg_blur : 8;
        const brightness = lyrics_bg_brightness !== undefined ? lyrics_bg_brightness : 0.25;
        this._page.style.setProperty('--lyrics-blur', blur + 'px');
        this._page.style.setProperty('--lyrics-brightness', String(brightness));
    }

    _applyBg() {
        if (!this._bg) return;
        const { lyrics_bg_color, lyrics_bg_image } = this._settings;
        if (lyrics_bg_image) {
            const url = lyrics_bg_image.startsWith('/files') || lyrics_bg_image.startsWith('http')
                ? lyrics_bg_image
                : Bridge.originalUrl(lyrics_bg_image);
            this._bg.style.background = `url(${url}) center/cover no-repeat`;
        } else {
            this._bg.style.background = lyrics_bg_color || '#0d0d1a';
        }
    }

    // ===== 歌词加载 =====
    async loadForSong(songId) {
        this.reset();
        try {
            const result = await Bridge.call('music_get_lyrics', songId);
            if (result.lyrics && result.lyrics.trim()) {
                this._lines = LyricsParser.parse(result.lyrics);
            }
        } catch (e) {
            this._lines = [];
        }
        if (this._visible) {
            this._currentIndex = -1;
            this._render();
        }
    }

    // ===== 播放更新 =====
    update(currentTime) {
        if (!this._visible || this._lines.length === 0) return;
        this._syncProgressBar(currentTime);
        const idx = this._findLineIndex(currentTime);
        if (idx !== this._currentIndex) {
            this._currentIndex = idx;
            this._highlightLine(idx);
        }
    }

    _findLineIndex(time) {
        let low = 0, high = this._lines.length - 1;
        while (low <= high) {
            const mid = (low + high) >> 1;
            if (this._lines[mid].time <= time) low = mid + 1;
            else high = mid - 1;
        }
        return Math.max(0, high);
    }

    _syncProgressBar(currentTime) {
        if (!this._progressBar) return;
        const audio = this._audioEl();
        if (!audio || !audio.duration || isNaN(audio.duration)) return;
        this._progressBar.max = audio.duration;
        this._progressBar.value = currentTime;
        if (this._timeCur) this._timeCur.textContent = MusicUtils.formatTime(currentTime);
        if (this._timeDur) this._timeDur.textContent = MusicUtils.formatTime(audio.duration);
    }

    // ===== 渲染 =====
    _render() {
        if (!this._content) return;
        if (this._lines.length === 0) {
            this._content.innerHTML = '<div class="lyrics-empty">暂无歌词</div>';
            return;
        }

        const { lyrics_font_size, lyrics_active_size, lyrics_line_height, lyrics_align } = this._settings;
        const baseSize = lyrics_font_size || 16;
        const activeSize = lyrics_active_size || 24;
        const lh = lyrics_line_height || 1.6;
        const align = lyrics_align === 'left' ? 'text-align:left;' : 'text-align:center;';

        this._content.innerHTML = this._lines.map((line, i) =>
            `<div class="lyrics-line" data-lyric-idx="${i}" data-lyric-time="${line.time}"
                  style="font-size:${baseSize}px;line-height:${lh};${align}">${MusicUtils.escapeHtml(line.text)}</div>`
        ).join('');

        this._currentIndex = -1;
        this._bindInteractions();
    }

    _bindInteractions() {
        if (!this._content) return;

        // 点击歌词行跳转（innerHTML 重建后重新绑定）
        this._content.querySelectorAll('.lyrics-line').forEach(el => {
            el.addEventListener('click', () => {
                const time = parseFloat(el.dataset.lyricTime);
                if (isNaN(time)) return;
                const audio = this._audioEl();
                if (audio) {
                    audio.currentTime = time;
                    audio.play().catch(() => {});
                }
            });
        });

        // 滚动/触摸监听只绑定一次（_content 元素不重建）
        if (this._scrollBound) return;
        this._scrollBound = true;

        const onScrollStart = () => {
            this._userScrolling = true;
            clearTimeout(this._scrollTimer);
        };
        const onScrollEnd = () => {
            clearTimeout(this._scrollTimer);
            this._scrollTimer = setTimeout(() => { this._userScrolling = false; }, 3000);
        };

        this._content.addEventListener('scroll', () => { onScrollStart(); onScrollEnd(); }, { passive: true });
        this._content.addEventListener('touchstart', onScrollStart, { passive: true });
        this._content.addEventListener('touchend', onScrollEnd, { passive: true });
    }

    _highlightLine(idx) {
        if (!this._content) return;

        const prev = this._content.querySelector('.lyrics-line.active');
        if (prev) {
            prev.classList.remove('active', 'glow');
            prev.style.fontSize = this._settings.lyrics_font_size + 'px';
        }

        const current = this._content.querySelector(`[data-lyric-idx="${idx}"]`);
        if (!current) return;

        current.classList.add('active');
        if (this._settings.lyrics_glow !== false) {
            current.classList.add('glow');
        }
        current.style.fontSize = this._settings.lyrics_active_size + 'px';

        if (!this._userScrolling) {
            const target = current.offsetTop - this._content.offsetHeight / 3;
            this._content.scrollTo({ top: Math.max(0, target), behavior: 'smooth' });
        }
    }

    // ===== 显隐 =====
    show(songTitle) {
        this._visible = true;
        this._currentIndex = -1;
        this._userScrolling = false;
        clearTimeout(this._scrollTimer);
        if (this._title) this._title.textContent = songTitle || '未在播放';
        this._applyBg();
        this._applyStyleVars();
        this._render();
        if (this._page) this._page.classList.add('active');
    }

    hide() {
        this._visible = false;
        clearTimeout(this._scrollTimer);
        if (this._page) this._page.classList.remove('active');
    }

    toggle() {
        if (this._visible) {
            this.hide();
        } else {
            this.refreshSettings().then(() => {
                const titleEl = document.getElementById('player-title');
                this.show(titleEl ? titleEl.textContent : '');
            });
        }
    }

    isVisible() {
        return this._visible;
    }

    reset() {
        this._lines = [];
        this._currentIndex = -1;
        if (this._content) {
            this._content.innerHTML = '<div class="lyrics-empty">暂无歌词</div>';
        }
    }
}
