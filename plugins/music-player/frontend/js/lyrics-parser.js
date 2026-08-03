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
    constructor() {
        this._lines = [];
        this._currentIndex = -1;
        this._visible = false;

        this._page = document.getElementById('lyrics-page');
        this._bg = document.getElementById('lyrics-page-bg');
        this._content = document.getElementById('lyrics-page-content');
        this._title = document.getElementById('lyrics-page-title');

        this._settings = {};
        this._loadSettings();
    }

    _loadSettings() {
        const defaults = {
            lyrics_font_size: 16,
            lyrics_active_size: 24,
            lyrics_line_height: 1.6,
            lyrics_glow: true,
            lyrics_align: 'center',
            lyrics_bg_color: '',
            lyrics_bg_image: '',
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
            if (settings) {
                const keys = ['lyrics_font_size', 'lyrics_active_size', 'lyrics_line_height',
                              'lyrics_glow', 'lyrics_align', 'lyrics_bg_color', 'lyrics_bg_image'];
                let changed = false;
                keys.forEach(k => {
                    if (settings[k] !== undefined && settings[k] !== this._settings[k]) {
                        this._settings[k] = settings[k];
                        changed = true;
                    }
                });
                if (changed) {
                    localStorage.setItem('musicLyricsSettings', JSON.stringify(this._settings));
                    this._applyBg();
                    this._render();
                }
            }
        } catch (e) {}
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
            const color = lyrics_bg_color || '#0d0d1a';
            this._bg.style.background = color;
        }
    }

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

    update(currentTime) {
        if (!this._visible || this._lines.length === 0) return;
        const idx = this._findLineIndex(currentTime);
        if (idx !== this._currentIndex) {
            this._currentIndex = idx;
            this._highlightLine(idx);
        }
    }

    _findLineIndex(time) {
        let low = 0;
        let high = this._lines.length - 1;
        while (low <= high) {
            const mid = (low + high) >> 1;
            if (this._lines[mid].time <= time) {
                low = mid + 1;
            } else {
                high = mid - 1;
            }
        }
        return Math.max(0, high);
    }

    _render() {
        if (!this._content) return;
        if (this._lines.length === 0) {
            this._content.innerHTML = '<div class="lyrics-empty">暂无歌词</div>';
            return;
        }

        const { lyrics_font_size, lyrics_active_size, lyrics_line_height,
                lyrics_glow, lyrics_align } = this._settings;

        const baseSize = lyrics_font_size || 16;
        const activeSize = lyrics_active_size || 24;
        const lh = lyrics_line_height || 1.6;
        const glow = lyrics_glow !== false ? 'text-shadow: 0 0 24px rgba(255,255,255,0.5);' : '';
        const align = lyrics_align === 'left' ? 'text-align:left;' : 'text-align:center;';

        this._content.style.setProperty('--lyrics-font-size', baseSize + 'px');
        this._content.style.setProperty('--lyrics-active-size', activeSize + 'px');
        this._content.style.setProperty('--lyrics-line-height', String(lh));

        this._content.innerHTML = this._lines.map((line, i) =>
            `<div class="lyrics-line" data-lyric-idx="${i}"
                  style="font-size:${baseSize}px;line-height:${lh};${align}">${MusicUtils.escapeHtml(line.text)}</div>`
        ).join('');
        this._currentIndex = -1;
    }

    _highlightLine(idx) {
        if (!this._content) return;
        const prev = this._content.querySelector('.lyrics-line.active');
        if (prev) {
            prev.classList.remove('active');
            prev.style.fontSize = this._settings.lyrics_font_size + 'px';
            prev.style.textShadow = '';
        }

        const current = this._content.querySelector(`[data-lyric-idx="${idx}"]`);
        if (current) {
            current.classList.add('active');
            current.style.fontSize = this._settings.lyrics_active_size + 'px';
            if (this._settings.lyrics_glow !== false) {
                current.style.textShadow = '0 0 24px rgba(255,255,255,0.5)';
            }
            this._content.scrollTo({
                top: current.offsetTop - this._content.offsetHeight / 3,
                behavior: 'smooth'
            });
        }
    }

    show(songTitle) {
        this._visible = true;
        this._currentIndex = -1;
        if (this._title) this._title.textContent = songTitle || '未在播放';
        this._applyBg();
        this._render();
        if (this._page) this._page.classList.add('active');
    }

    hide() {
        this._visible = false;
        if (this._page) this._page.classList.remove('active');
    }

    toggle() {
        if (this._visible) {
            this.hide();
        } else {
            this.refreshSettings().then(() => {
                const title = document.getElementById('player-title');
                this.show(title ? title.textContent : '');
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
