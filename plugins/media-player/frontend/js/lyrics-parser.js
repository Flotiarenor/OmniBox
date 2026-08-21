// ===== LRC 歌词解析（支持同一行多时间标签） =====
class LyricsParser {
    static parse(lrcText) {
        if (!lrcText || !lrcText.trim()) return [];

        const result = [];
        for (const rawLine of lrcText.split(/\r?\n/)) {
            const tags = [];
            const re = /\[(\d{1,3}):(\d{2})(?:\.(\d{1,3}))?\]/g;
            let match;
            let lastIndex = 0;

            while ((match = re.exec(rawLine)) !== null) {
                const minutes = parseInt(match[1], 10);
                const seconds = parseInt(match[2], 10);
                const frac = match[3] ? parseInt(match[3].padEnd(3, '0'), 10) / 1000 : 0;
                tags.push(minutes * 60 + seconds + frac);
                lastIndex = match.index + match[0].length;
            }

            if (!tags.length) continue;
            const text = rawLine.slice(lastIndex).trim();
            if (!text) continue;

            for (const time of tags) {
                result.push({ time, text });
            }
        }

        result.sort((a, b) => a.time - b.time);
        return result;
    }
}

// ===== 沉浸式歌词页 =====
class MediaLyrics {
    constructor(app) {
        this.app = app;
        this._lines = [];
        this._currentIndex = -1;
        this._visible = false;
        this._userScrolling = false;
        this._scrollTimer = null;
        this._raf = null;

        this._page = document.getElementById('lyrics-page');
        this._bg = document.getElementById('lyrics-page-bg');
        this._aurora = document.getElementById('lyrics-page-aurora');
        this._viz = document.getElementById('lyrics-viz-canvas');
        this._content = document.getElementById('lyrics-page-content');
        this._title = document.getElementById('lyrics-page-title');
        this._progressBar = document.getElementById('lyrics-progress-bar');
        this._timeCur = document.getElementById('lyrics-time-current');
        this._timeDur = document.getElementById('lyrics-time-duration');

        this._bindInteractions();
    }

    get settings() {
        return this.app.settings || {};
    }

    get mediaElement() {
        return this.app.core ? this.app.core.mediaElement : null;
    }

    get lines() {
        return this._lines;
    }

    get currentIndex() {
        return this._currentIndex;
    }

    // ===== 样式 =====
    applyStyleVars() {
        if (!this._page) return;
        const s = this.settings;
        const fc = s.lyrics_font_color || '#ffffff';
        this._page.style.setProperty('--lyrics-font-color', fc + '3d');
        this._page.style.setProperty('--lyrics-font-color-hover', fc + '8c');
        this._page.style.setProperty('--lyrics-font-color-active', fc);
        this._page.style.setProperty('--lyrics-glow-color', fc + '80');
        this._page.style.setProperty('--lyrics-blur', `${s.lyrics_bg_blur ?? 34}px`);
        this._page.style.setProperty('--lyrics-brightness', String(s.lyrics_bg_brightness ?? 0.3));
    }

    applyBg(item) {
        if (!this._bg) return;
        const s = this.settings;
        if (s.lyrics_bg_image) {
            const url = /^(https?:|\/files)/.test(s.lyrics_bg_image)
                ? s.lyrics_bg_image
                : Bridge.originalUrl(s.lyrics_bg_image);
            this._bg.style.background = `url("${url}") center / cover no-repeat`;
        } else if (item && item.cover_path) {
            this._bg.style.background = `url("${MPUtils.coverUrl(item)}") center / cover no-repeat`;
        } else {
            this._bg.style.background = s.lyrics_bg_color || 'linear-gradient(145deg, #10101c, #1a1030)';
        }
    }

    // ===== 数据 =====
    async loadForItem(item) {
        this._lines = [];
        this._currentIndex = -1;
        if (!item) {
            this.app.updateStageLyrics(-1);
            return;
        }
        try {
            const result = await Bridge.call('media_get_lyrics', item.id);
            if (result && result.lyrics) {
                this._lines = LyricsParser.parse(result.lyrics);
            }
        } catch (e) {
            this._lines = [];
        }
        if (this._visible) this._render();
        this.app.updateStageLyrics(this._currentIndex);
    }

    // ===== 播放更新 =====
    update(currentTime) {
        if (this._visible) this._syncProgressBar(currentTime);
        if (!this._lines.length) {
            this.app.updateStageLyrics(-1);
            return;
        }
        const idx = this._findLineIndex(currentTime);
        if (idx !== this._currentIndex) {
            this._currentIndex = idx;
            if (this._visible) this._highlightLine(idx);
            this.app.updateStageLyrics(idx);
        }
    }

    _findLineIndex(time) {
        let low = 0;
        let high = this._lines.length - 1;
        while (low <= high) {
            const mid = (low + high) >> 1;
            if (this._lines[mid].time <= time) low = mid + 1;
            else high = mid - 1;
        }
        return Math.max(0, high);
    }

    _syncProgressBar(currentTime) {
        if (!this._progressBar) return;
        const el = this.mediaElement;
        if (!el || !el.duration || !isFinite(el.duration)) return;
        this._progressBar.max = el.duration;
        this._progressBar.value = currentTime;
        MPUtils.setRangePercent(this._progressBar, (currentTime / el.duration) * 100);
        if (this._timeCur) this._timeCur.textContent = MPUtils.formatTime(currentTime);
        if (this._timeDur) this._timeDur.textContent = MPUtils.formatTime(el.duration);
    }

    // ===== 渲染 =====
    _render() {
        if (!this._content) return;
        const s = this.settings;

        if (!this._lines.length) {
            this._content.innerHTML = '<div class="mp-lyrics-empty">暂无歌词<br>可在同目录放置同名 .lrc 文件</div>';
            return;
        }

        const baseSize = s.lyrics_font_size || 16;
        const lineHeight = s.lyrics_line_height || 1.6;
        const align = s.lyrics_align === 'left' ? 'text-align:left;' : 'text-align:center;';

        this._content.innerHTML = this._lines.map((line, i) => `
            <div class="mp-lyric-line" data-lyric-idx="${i}" data-lyric-time="${line.time}"
                 style="font-size:${baseSize}px;line-height:${lineHeight};${align}">${MPUtils.escapeHtml(line.text)}</div>
        `).join('');
        this._currentIndex = -1;
    }

    _bindInteractions() {
        if (!this._content) return;

        this._content.addEventListener('click', (e) => {
            const line = e.target.closest('.mp-lyric-line');
            if (!line) return;
            const time = parseFloat(line.dataset.lyricTime);
            if (isNaN(time) || !this.mediaElement) return;
            this.mediaElement.currentTime = time;
            this.mediaElement.play().catch(() => { });
        });

        const onScroll = () => {
            this._userScrolling = true;
            clearTimeout(this._scrollTimer);
            this._scrollTimer = setTimeout(() => { this._userScrolling = false; }, 2600);
        };
        this._content.addEventListener('scroll', onScroll, { passive: true });
        this._content.addEventListener('touchstart', onScroll, { passive: true });
    }

    _highlightLine(idx) {
        if (!this._content) return;
        const s = this.settings;

        const prev = this._content.querySelector('.mp-lyric-line.active');
        if (prev) {
            prev.classList.remove('active', 'glow');
            prev.style.fontSize = `${s.lyrics_font_size || 16}px`;
        }

        const current = this._content.querySelector(`[data-lyric-idx="${idx}"]`);
        if (!current) return;

        current.classList.add('active');
        if (s.lyrics_glow !== false) current.classList.add('glow');
        current.style.fontSize = `${s.lyrics_active_size || 24}px`;

        if (!this._userScrolling) {
            const target = current.offsetTop - this._content.offsetHeight / 3;
            this._content.scrollTo({ top: Math.max(0, target), behavior: 'smooth' });
        }
    }

    // ===== 频谱可视化 =====
    _startViz() {
        this._stopViz();
        const draw = () => {
            this._raf = requestAnimationFrame(draw);
            this._drawViz();
        };
        this._raf = requestAnimationFrame(draw);
    }

    _stopViz() {
        if (this._raf) {
            cancelAnimationFrame(this._raf);
            this._raf = null;
        }
        if (this._viz) {
            const ctx = this._viz.getContext('2d');
            if (ctx) ctx.clearRect(0, 0, this._viz.width, this._viz.height);
        }
    }

    _drawViz() {
        const canvas = this._viz;
        if (!canvas || !this._visible) return;
        const analyser = this.app.core.getAnalyser();
        if (!analyser) return;

        const width = canvas.width = canvas.offsetWidth || canvas.clientWidth || 800;
        const height = canvas.height = canvas.offsetHeight || canvas.clientHeight || 600;
        if (!width || !height) return;

        const ctx = canvas.getContext('2d');
        const data = new Uint8Array(analyser.frequencyBinCount);
        analyser.getByteFrequencyData(data);

        ctx.clearRect(0, 0, width, height);
        const bars = 72;
        const step = Math.floor(data.length / bars);
        for (let i = 0; i < bars; i++) {
            let value = 0;
            for (let j = 0; j < step; j++) {
                value = Math.max(value, data[i * step + j]);
            }
            const norm = value / 255;
            const barHeight = Math.max(3, norm * height * 0.55);
            const x = (i + 0.5) * (width / bars);
            const hue = 215 + (i / bars) * 75;
            ctx.fillStyle = `hsla(${hue}, 82%, 64%, ${0.18 + norm * 0.5})`;
            const radius = width / bars * 0.28;
            ctx.beginPath();
            ctx.roundRect(x - width / bars / 2 + 2, height - barHeight, width / bars - 4, barHeight, radius);
            ctx.fill();
        }
    }

    // ===== 显隐 =====
    show(item, titleText) {
        this._visible = true;
        this._userScrolling = false;
        this.applyStyleVars();
        this.applyBg(item);
        if (this._title) this._title.textContent = titleText || item?.title || '未在播放';
        this._render();
        this._page.classList.add('active');
        if (this.app.core) this.app.core.ensureAudioGraph();
        this._startViz();
        this.update(this.mediaElement ? this.mediaElement.currentTime || 0 : 0);
    }

    hide() {
        this._visible = false;
        clearTimeout(this._scrollTimer);
        this._page.classList.remove('active');
        this._stopViz();
        this.app.updateStageLyrics(this._currentIndex);
    }

    toggle(item) {
        if (this._visible) this.hide();
        else this.show(item);
    }

    isVisible() {
        return this._visible;
    }
}
