// ============================================================
// OmniBox 媒体播放器 — 均衡器、歌词 / 视频模式、全屏与快捷键
//
// 本文件是 MediaPlayerApp.prototype 的一个分片：类骨架与构造函数在 app.js，
// 这里用 Object.assign 把方法扩回同一个原型。**加载顺序是硬约束** —— 必须在
// app.js 之后、实例化之前（见 index.html），契约由 tests/js/media_player_app_split.mjs 把关。
// ============================================================

Object.assign(MediaPlayerApp.prototype, {

    // ============================================================
    // 均衡器
    // ============================================================
    _toggleEQ() {
        const panel = document.getElementById('eq-panel');
        const willOpen = panel.classList.contains('hidden');
        if (willOpen) this._positionPops();
        panel.classList.toggle('hidden', !willOpen);
        if (willOpen) this._buildEQBands();
    },

    // 弹出面板贴播放栏上沿定位。
    // 面板是 fixed：横向按视口（与播放栏右侧 16px 内边距对齐），纵向偏移必须实测 ——
    // 播放栏在舞台底部，内容区在舞台下方，且舞台高度随视频模式变化，CSS 里算不出
    // 「播放栏顶边到视口底边」的距离。原实现写死 calc(--mp-pb-height + 28px)，等于假定
    // 播放栏贴着视口底边：窗口变矮、舞台被压扁时面板会压到播放栏上，把播放栏按钮盖住
    // （实测 1168×598 视口下压住 8 个控件，点按钮实际点在面板上）。
    // 变量写在 #app 上：面板是 #app 的子元素（与 .mp-main 平级），写在内层 .mp-main 上
    // 不会继承到面板，var() 会静默退回兜底值。
    _positionPops() {
        const bar = document.getElementById('player-bar');
        const host = document.getElementById('app');
        if (!bar || !host) return;
        const gap = 28;
        const offset = Math.round(window.innerHeight - bar.getBoundingClientRect().top + gap);
        host.style.setProperty('--mp-pop-bottom', `${Math.max(gap, offset)}px`);
    },

    _hideEQ() {
        document.getElementById('eq-panel').classList.add('hidden');
    },

    _buildEQBands() {
        const container = document.getElementById('eq-bands');
        if (!container) return;
        this.core.ensureAudioGraph();
        const bands = this.core.getEqBands();

        container.innerHTML = this.core.EQ_FREQS.map((freq, i) => {
            const label = freq >= 1000 ? `${(freq / 1000).toFixed(0)}K` : String(freq);
            const gain = bands[i] || 0;
            const percent = ((gain + 12) / 24) * 100;
            return `
                <div class="mp-eq-band">
                    <span class="mp-eq-value" data-eq-val="${i}">${gain > 0 ? '+' + gain : gain}dB</span>
                    <input type="range" class="mp-eq-slider" min="-12" max="12" step="1" value="${gain}"
                           data-eq-idx="${i}" style="--range-val:${percent}%">
                    <span class="mp-eq-label">${label}</span>
                </div>`;
        }).join('');

        container.querySelectorAll('.mp-eq-slider').forEach(slider => {
            slider.addEventListener('input', () => {
                const idx = parseInt(slider.dataset.eqIdx, 10);
                const gain = parseFloat(slider.value);
                this.core.setEqBand(idx, gain);
                const percent = ((gain + 12) / 24) * 100;
                MPUtils.setRangePercent(slider, percent);
                const valueEl = container.querySelector(`[data-eq-val="${idx}"]`);
                if (valueEl) valueEl.textContent = (gain > 0 ? '+' + gain : gain) + 'dB';
                try {
                    localStorage.setItem('omniboxMediaEQ', JSON.stringify(this.core.getEqBands()));
                } catch (e) { }
            });
        });
    },

    _resetEQ() {
        this.core.resetEq();
        this._buildEQBands();
        document.getElementById('eq-preset-select').value = '';
    },

    async _loadEQPresets() {
        const select = document.getElementById('eq-preset-select');
        if (!select) return;
        try {
            const presets = await Bridge.call('media_list_eq_presets') || [];
            select.innerHTML = '<option value="">内置预设</option>';
            presets.forEach(p => {
                const opt = document.createElement('option');
                opt.value = p.id;
                opt.textContent = p.name;
                select.appendChild(opt);
            });
        } catch (e) { }
    },

    async _applyEQPreset(id) {
        if (!id) {
            this._resetEQ();
            return;
        }
        try {
            const presets = await Bridge.call('media_list_eq_presets') || [];
            const preset = presets.find(p => p.id === id);
            if (preset && Array.isArray(preset.bands)) {
                this.core.applyEqBands(preset.bands);
                this._buildEQBands();
            }
        } catch (e) {
            Toast.error('加载预设失败');
        }
    },

    async _confirmEQName() {
        const name = document.getElementById('input-eq-name').value.trim();
        if (!name) {
            Toast.error('请输入预设名称');
            return;
        }
        MPUtils.closeModal('modal-eq-name');
        document.getElementById('input-eq-name').value = '';
        try {
            const result = await Bridge.call('media_save_eq_preset', name, this.core.getEqBands());
            if (result.success) {
                Toast.success('预设已保存');
                await this._loadEQPresets();
            } else {
                Toast.error(result.error || '保存失败');
            }
        } catch (e) {
            Toast.error('保存失败');
        }
    },

    // ============================================================
    // 歌词 / 视频模式
    // ============================================================
    // 未全屏时的迷你歌词：显示当前句前后各两句
    updateStageLyrics(index) {
        const el = document.getElementById('mp-stage-lyrics');
        if (!el) return;
        const item = this.core.currentItem;
        const canShow = item && (item.kind === 'audio' || !this.core.videoMode)
            && this.settings.lyrics_enabled !== false
            && !this.lyrics.isVisible();

        if (!canShow) {
            el.classList.remove('show');
            el.innerHTML = '';
            return;
        }

        const lines = this.lyrics.lines || [];
        if (!lines.length) {
            el.classList.add('show');
            el.innerHTML = '<div class="mp-stage-lyric-empty">暂无歌词 · 点击打开歌词页</div>';
            return;
        }

        let idx = Number.isInteger(index) && index >= 0 ? index : this.lyrics.currentIndex;
        if (idx < 0) idx = 0;
        const start = Math.max(0, idx - 2);
        const end = Math.min(lines.length - 1, idx + 2);
        let html = '';
        for (let i = start; i <= end; i++) {
            const cls = i === idx ? 'current'
                : (i < idx ? (i === idx - 1 ? 'prev1' : 'prev2')
                    : (i === idx + 1 ? 'next1' : 'next2'));
            html += `<div class="mp-stage-lyric ${cls}">${MPUtils.escapeHtml(lines[i].text)}</div>`;
        }
        el.innerHTML = html;
        el.classList.add('show');
    },

    // 宽屏模式：隐藏下方媒体列表，舞台铺满主区域（左侧导航保留）
    _toggleWideMode() {
        if (!this.core.currentItem) {
            Toast.info('请先播放一个媒体');
            return;
        }
        const appEl = document.getElementById('app');
        const btn = document.getElementById('btn-wide');
        const active = appEl.classList.toggle('wide-mode');
        btn.classList.toggle('wide-active', active);
        btn.title = active ? '恢复普通模式' : '宽屏模式';
        Toast.info(active ? '宽屏模式已开启' : '已恢复普通模式');
    },

    _toggleLyrics() {
        const item = this.core.currentItem;
        if (!item) return;
        if (this.settings.lyrics_enabled === false) {
            Toast.info('歌词显示已在设置中关闭');
            return;
        }
        if (item.kind !== 'audio' && this.core.videoMode) {
            Toast.info('仅声音模式下可查看歌词');
            return;
        }
        this.lyrics.toggle(item, item.title);
    },

    _toggleVideoMode() {
        const item = this.core.currentItem;
        if (!item || item.kind !== 'video') return;
        this.core.setVideoMode(!this.core.videoMode);
        const btn = document.getElementById('btn-video-mode');
        btn.innerHTML = MPUtils.icon(this.core.videoMode ? 'icon:clapperboard' : 'icon:music');
        btn.title = this.core.videoMode ? '切换到仅声音' : '切换到画面';
        Bridge.call('media_set_config', 'default_video_mode', this.core.videoMode ? 'video' : 'audio').catch(() => { });
    },

    _isVideoShowing() {
        return document.getElementById('mp-stage').classList.contains('video-on');
    },

    // ============================================================
    // 全屏
    // ============================================================
    toggleFullscreen() {
        const item = this.core.currentItem;
        if (!item) {
            Toast.info('请先播放一个媒体');
            return;
        }
        // 音乐（或视频的“仅声音”模式）的全屏就是沉浸式歌词页
        if (!this._isVideoShowing()) {
            this._toggleLyrics();
            return;
        }
        if (this.isFullscreen) this._exitFullscreen();
        else this._enterFullscreen();
    },

    _enterFullscreen() {
        this.isFullscreen = true;
        const stage = document.getElementById('mp-stage');
        stage.classList.add('fs-on');
        this._showControls();

        try {
            parent.document.documentElement.setAttribute('data-video-fullscreen', 'true');
        } catch (e) { }
        try {
            if (stage.requestFullscreen) stage.requestFullscreen().catch(() => { });
        } catch (e) { }
        try {
            const api = parent.pywebview && parent.pywebview.api;
            if (api && api.system_toggle_fullscreen) api.system_toggle_fullscreen();
        } catch (e) { }

        this._bindFsMove();
        if (this.settings.auto_hide_enabled !== false) this._scheduleAutoHide();
    },

    _exitFullscreen() {
        this.isFullscreen = false;
        const stage = document.getElementById('mp-stage');
        stage.classList.remove('fs-on', 'controls-hidden');
        this._showControls();
        this._stopAutoHide();

        try {
            parent.document.documentElement.removeAttribute('data-video-fullscreen');
        } catch (e) { }
        try {
            if (document.fullscreenElement && document.exitFullscreen) {
                document.exitFullscreen().catch(() => { });
            }
        } catch (e) { }
        try {
            const api = parent.pywebview && parent.pywebview.api;
            if (api && api.system_toggle_fullscreen) api.system_toggle_fullscreen();
        } catch (e) { }
    },

    _bindFsMove() {
        if (this.fsMoveHandler) document.removeEventListener('mousemove', this.fsMoveHandler);
        this.fsMoveHandler = () => {
            this._showControls();
            if (this.settings.auto_hide_enabled !== false) this._scheduleAutoHide();
        };
        document.addEventListener('mousemove', this.fsMoveHandler);
    },

    _stopAutoHide() {
        if (this.fsMoveHandler) {
            document.removeEventListener('mousemove', this.fsMoveHandler);
            this.fsMoveHandler = null;
        }
        if (this.hideTimer) {
            clearTimeout(this.hideTimer);
            this.hideTimer = null;
        }
    },

    _scheduleAutoHide() {
        if (this.hideTimer) clearTimeout(this.hideTimer);
        const delay = Math.max(1, (this.settings.auto_hide_delay ?? 3) || 1) * 1000;
        this.hideTimer = setTimeout(() => {
            const el = this.core.mediaElement;
            if (this.isFullscreen && el && !el.paused) this._hideControls();
        }, delay);
    },

    _showControls() {
        this.controlsVisible = true;
        document.getElementById('mp-stage').classList.remove('controls-hidden');
    },

    _hideControls() {
        this.controlsVisible = false;
        document.getElementById('mp-stage').classList.add('controls-hidden');
    },

    // ============================================================
    // 键盘快捷键
    // ============================================================
    _bindKeyboard() {
        document.addEventListener('keydown', (e) => {
            if (e.target.tagName === 'INPUT' || e.target.tagName === 'TEXTAREA' || e.target.tagName === 'SELECT') return;

            switch (e.key) {
                case ' ':
                    e.preventDefault();
                    this.core.togglePlay();
                    break;
                case 'ArrowLeft':
                    e.preventDefault();
                    this.core.seekDelta(-5);
                    break;
                case 'ArrowRight':
                    e.preventDefault();
                    this.core.seekDelta(5);
                    break;
                case 'ArrowUp':
                    e.preventDefault();
                    this.core.volume = Math.min(1, this.core.volume + 0.05);
                    break;
                case 'ArrowDown':
                    e.preventDefault();
                    this.core.volume = Math.max(0, this.core.volume - 0.05);
                    break;
                case 'n':
                case 'N':
                    e.preventDefault();
                    this.core.next(false);
                    break;
                case 'p':
                case 'P':
                    e.preventDefault();
                    this.core.prev();
                    break;
                case 'm':
                case 'M':
                    e.preventDefault();
                    this.core.toggleMute();
                    break;
                case 'l':
                case 'L':
                    e.preventDefault();
                    this._toggleLyrics();
                    break;
                case 'f':
                case 'F':
                    e.preventDefault();
                    this.toggleFullscreen();
                    break;
                case 'Escape':
                    if (this.isFullscreen) {
                        this._exitFullscreen();
                        return;
                    }
                    if (this.lyrics.isVisible()) this.lyrics.hide();
                    document.getElementById('queue-popup').classList.add('hidden');
                    document.getElementById('eq-panel').classList.add('hidden');
                    document.querySelectorAll('.mp-modal.active').forEach(m => m.classList.remove('active'));
                    this._closePlaylistMenu();
                    break;
            }
        });
    },
});
