// ===== 播放核心：统一管理音频 / 视频元素、队列、模式、EQ 与进度记忆 =====
class MediaPlayerCore {
    constructor(app) {
        this.app = app;

        this.audio = new Audio();
        this.audio.preload = 'metadata';
        this.video = document.getElementById('video-player');
        this.video.preload = 'metadata';

        this.queue = [];
        this.currentIndex = -1;
        this.currentItem = null;
        this.playMode = 0; // 0=顺序 1=随机 2=单曲循环
        this.videoMode = true; // true=画面 false=仅声音

        this._volume = 1;
        this._muted = false;
        this._progressSaver = null;
        this._pendingResume = 0;
        this._lastBackendSave = 0;
        this._failedIds = new Set();
        this._retryCounts = new Map();   // 每曲加载失败重试计数（成功后清除）
        this._pendingSkipTimer = null;   // 失败后自动跳下一曲的挂起定时器
        this._skipArmed = false;         // 定时器是否仍有效（用户操作即解除）
        this._loadSeq = 0;

        this._audioCtx = null;
        this._sources = {};
        this._eqNodes = null;
        this._analyser = null;

        this._bindMediaEvents(this.audio);
        this._bindMediaEvents(this.video);
    }

    get mediaElement() {
        if (this.currentItem && this.currentItem.kind === 'video' && this.videoMode) {
            return this.video;
        }
        return this.audio;
    }

    get paused() {
        const el = this.mediaElement;
        return !el || el.paused;
    }

    // ===== 队列与播放 =====
    setQueue(items, startIndex = 0, autoplay = true) {
        this.queue = items || [];
        if (this.queue.length === 0) {
            this.stop();
            return;
        }
        const idx = Math.max(0, Math.min(this.queue.length - 1, startIndex));
        this.playIndex(idx, autoplay);
    }

    playIndex(index, autoplay = true) {
        if (!this.queue.length) return;
        if (index < 0 || index >= this.queue.length) index = 0;
        // 用户（或自动跳转）选择了曲目：解除任何挂起的自动跳转，并允许重试此前失败的曲目
        this._clearPendingSkip();
        const item = this.queue[index];
        if (item) {
            this._failedIds.delete(item.id);
            this._retryCounts.delete(item.id);
        }
        this.currentIndex = index;
        this._loadItem(item, autoplay);
    }

    playItem(item, autoplay = true) {
        if (!item) return;
        this._clearPendingSkip();
        const idx = this.queue.findIndex(x => x && x.id === item.id);
        if (idx >= 0) {
            this.playIndex(idx, autoplay);
        } else {
            this.queue.push(item);
            this.playIndex(this.queue.length - 1, autoplay);
        }
    }

    async _loadItem(item, autoplay = true) {
        if (!item) return;
        const loadId = ++this._loadSeq;
        this._saveProgress();
        this.currentItem = item;

        // 切换前记住当前进度，避免视频/音频元素切换丢进度
        const keepTime = item.id !== (this._previousItem && this._previousItem.id)
            ? MediaProgressStore.get(item)
            : 0;
        this._previousItem = item;
        this._pendingResume = keepTime;

        this.audio.pause();
        this.video.pause();

        this._startProgressSaver();
        this.app.onTrackChange(item);
        this.app.onPlayStateChange(false);

        let src = item.stream_url || item.url || MPUtils.mediaUrl(item.path);

        // 网易云网络流：如果还没有 URL，先临时解析，不跳歌、不报错
        if (!src && item.ncm_encrypted_id) {
            try {
                const data = await Bridge.callPlugin('netease-music', 'get_song_url', item.ncm_encrypted_id, item.original_id);
                if (data && data.url) {
                    item.stream_url = data.url;
                    src = data.url;
                }
            } catch (e) {
                console.warn('获取网易云播放地址失败:', e);
            }
        }

        if (loadId !== this._loadSeq) return;
        if (!src) {
            Toast.error('无法获取播放地址');
            return;
        }

        const el = this.mediaElement;
        el.src = src;
        el.volume = this._volume;
        el.muted = this._muted;
        el.load();

        if (autoplay) {
            el.play().catch((e) => console.log('自动播放被阻止:', e));
        }
        this._savePlaybackState();
        Bridge.call('media_update_recent', item.id).catch(() => { });
    }

    togglePlay() {
        const el = this.mediaElement;
        if (this.currentItem && el && el.src) {
            if (el.paused) el.play().catch(() => { });
            else el.pause();
        } else if (this.queue.length) {
            this.playIndex(Math.max(0, this.currentIndex), true);
        }
    }

    next(auto = true) {
        if (!this.queue.length) return;
        this._clearPendingSkip();
        let idx;
        if (this.playMode === 1) {
            idx = Math.floor(Math.random() * this.queue.length);
        } else if (this.playMode === 2 && auto) {
            // 单曲循环：ended 时直接重播当前曲目
            const el = this.mediaElement;
            if (el) {
                el.currentTime = 0;
                el.play().catch(() => { });
            }
            return;
        } else if (this.playMode === 2) {
            idx = this.currentIndex;
        } else {
            idx = (this.currentIndex + 1) % this.queue.length;
        }
        this.playIndex(idx, true);
    }

    prev() {
        if (!this.queue.length) return;
        this._clearPendingSkip();
        const el = this.mediaElement;
        if (el && el.currentTime > 3) {
            el.currentTime = 0;
            return;
        }
        let idx;
        if (this.playMode === 1) {
            idx = Math.floor(Math.random() * this.queue.length);
        } else {
            idx = (this.currentIndex - 1 + this.queue.length) % this.queue.length;
        }
        this.playIndex(idx, true);
    }

    stop() {
        this._clearPendingSkip();
        this._saveProgress();
        this._stopProgressSaver();
        this.audio.pause();
        this.video.pause();
        this.audio.removeAttribute('src');
        this.video.removeAttribute('src');
        this.audio.load();
        this.video.load();
        this.currentItem = null;
        this.currentIndex = -1;
        this.app.onTrackChange(null);
        this._savePlaybackState();
    }

    seekTo(value) {
        const el = this.mediaElement;
        if (el && isFinite(value)) {
            el.currentTime = Math.max(0, Math.min(el.duration || 0, value));
        }
    }

    seekDelta(seconds) {
        const el = this.mediaElement;
        if (el && el.duration) {
            this.seekTo(el.currentTime + seconds);
        }
    }

    // ===== 播放模式 =====
    cyclePlayMode() {
        this.playMode = (this.playMode + 1) % 3;
        this.app.updatePlayModeUI();
        this._savePlaybackState();
        const names = ['顺序播放', '随机播放', '单曲循环'];
        Toast.info(names[this.playMode]);
        return this.playMode;
    }

    // ===== 音量 =====
    get volume() { return this._volume; }

    set volume(v) {
        this._volume = Math.max(0, Math.min(1, v));
        this.audio.volume = this._volume;
        this.video.volume = this._volume;
        try { localStorage.setItem('omniboxMediaVolume', String(this._volume)); } catch (e) { }
        this.app.updateVolumeUI();
        this._debouncedSavePlayback();
    }

    toggleMute() {
        this._muted = !this._muted;
        this.audio.muted = this._muted;
        this.video.muted = this._muted;
        this.app.updateVolumeUI();
    }

    // ===== 视频画面 / 仅声音 =====
    setVideoMode(mode) {
        if (!this.currentItem || this.currentItem.kind !== 'video') return;
        this._clearPendingSkip();
        const wasPlaying = !this.mediaElement.paused;
        const position = this.mediaElement.currentTime || 0;

        this.videoMode = mode === true || mode === 'video';

        this.audio.pause();
        this.video.pause();
        const el = this.mediaElement;
        el.src = MPUtils.mediaUrl(this.currentItem.path);
        el.volume = this._volume;
        el.muted = this._muted;
        el.load();
        const resume = () => {
            if (position > 0) el.currentTime = position;
            if (wasPlaying) el.play().catch(() => { });
        };
        el.addEventListener('loadedmetadata', resume, { once: true });
        this.app.onTrackChange(this.currentItem);
        this.app.onPlayStateChange(wasPlaying);
    }

    // ===== 状态保存 / 恢复 =====
    _startProgressSaver() {
        this._stopProgressSaver();
        this._progressSaver = setInterval(() => this._saveProgress(), 2000);
    }

    _stopProgressSaver() {
        if (this._progressSaver) {
            clearInterval(this._progressSaver);
            this._progressSaver = null;
        }
    }

    _saveProgress() {
        const el = this.mediaElement;
        if (!this.currentItem || !el || !el.src) return;
        if (el.currentTime > 2) {
            MediaProgressStore.save(this.currentItem.id, el.currentTime);
        }
    }

    _savePlaybackState() {
        const loopMap = { 0: 'all', 1: 'all', 2: 'one' };
        Bridge.call('media_save_playback',
            this.currentItem ? this.currentItem.id : '',
            loopMap[this.playMode] || 'none',
            this.playMode === 1,
            this._volume
        ).catch(() => { });
    }

    _debouncedSavePlayback() {
        clearTimeout(this._saveTimer);
        this._saveTimer = setTimeout(() => this._savePlaybackState(), 600);
    }

    restorePlayback(item, pb) {
        if (!item || !item.id) return false;
        this._clearPendingSkip();
        const savedPos = MediaProgressStore.get(item);
        this.queue = [item];
        this.currentIndex = 0;
        this.currentItem = item;
        this.videoMode = item.kind !== 'video' || (pb.video_mode || 'video') !== 'audio';

        if (pb.loop_mode === 'one') this.playMode = 2;
        else if (pb.shuffle) this.playMode = 1;
        else this.playMode = 0;

        if (pb.volume !== undefined && pb.volume !== null) {
            this._volume = Math.max(0, Math.min(1, Number(pb.volume) || 1));
            this.audio.volume = this._volume;
            this.video.volume = this._volume;
        }

        this._previousItem = item;
        this._pendingResume = savedPos;
        const el = this.mediaElement;
        el.src = item.stream_url || item.url || MPUtils.mediaUrl(item.path);
        el.volume = this._volume;
        el.load();
        this._startProgressSaver();
        this.app.onTrackChange(item);
        this.app.onPlayStateChange(false);
        this.app.updatePlayModeUI();
        this.app.updateVolumeUI();
        return true;
    }

    // ===== EQ =====
    EQ_FREQS = [32, 64, 125, 250, 500, 1000, 2000, 4000, 8000, 16000];

    ensureAudioGraph() {
        if (this._eqNodes) {
            if (this._audioCtx && this._audioCtx.state === 'suspended') {
                this._audioCtx.resume().catch(() => { });
            }
            return;
        }

        try {
            const Ctx = window.AudioContext || window.webkitAudioContext;
            if (!Ctx) return;
            this._audioCtx = new Ctx();
            this._eqNodes = [];
            this._analyser = this._audioCtx.createAnalyser();
            this._analyser.fftSize = 512;
            this._analyser.smoothingTimeConstant = 0.82;

            // 两个媒体元素共同汇入同一条滤波链
            [this.audio, this.video].forEach(el => {
                try {
                    this._sources[el === this.audio ? 'audio' : 'video'] = this._audioCtx.createMediaElementSource(el);
                } catch (e) { }
            });

            let filters = this.EQ_FREQS.map(freq => {
                const filter = this._audioCtx.createBiquadFilter();
                filter.type = 'peaking';
                filter.frequency.value = freq;
                filter.Q.value = 1.0;
                filter.gain.value = 0;
                return filter;
            });
            filters.forEach((filter, i) => {
                if (i > 0) filters[i - 1].connect(filter);
            });
            Object.values(this._sources).forEach(source => {
                source.connect(filters[0]);
            });
            filters[filters.length - 1].connect(this._analyser);
            this._analyser.connect(this._audioCtx.destination);

            this._eqNodes = filters;
            try {
                const saved = JSON.parse(localStorage.getItem('omniboxMediaEQ') || 'null');
                if (Array.isArray(saved)) this.applyEqBands(saved);
            } catch (e) { }

            if (this._audioCtx.state === 'suspended') this._audioCtx.resume().catch(() => { });
        } catch (e) {
            console.warn('EQ 初始化失败:', e);
        }
    }

    getAnalyser() {
        this.ensureAudioGraph();
        return this._analyser;
    }

    setEqBand(index, gain) {
        this.ensureAudioGraph();
        if (this._eqNodes && this._eqNodes[index]) {
            this._eqNodes[index].gain.value = Math.max(-12, Math.min(12, gain));
        }
    }

    getEqBands() {
        if (!this._eqNodes) return this.EQ_FREQS.map(() => 0);
        return this._eqNodes.map(n => Math.round(n.gain.value));
    }

    resetEq() {
        this.applyEqBands(this.EQ_FREQS.map(() => 0));
    }

    applyEqBands(bands) {
        this.ensureAudioGraph();
        bands.forEach((gain, i) => this.setEqBand(i, gain));
        try { localStorage.setItem('omniboxMediaEQ', JSON.stringify(this.getEqBands())); } catch (e) { }
    }

    // 失败自动跳转：只在「重试后仍失败」时武装一次；任何用户操作都会解除，
    // 避免 600ms 延迟定时器覆盖用户刚做的选择（历史乱跳 bug 根因之一）。
    _clearPendingSkip() {
        this._skipArmed = false;
        if (this._pendingSkipTimer) {
            clearTimeout(this._pendingSkipTimer);
            this._pendingSkipTimer = null;
        }
    }

    _armSkip(itemId, nextIndex) {
        this._clearPendingSkip();
        this._skipArmed = true;
        this._pendingSkipTimer = setTimeout(() => {
            this._pendingSkipTimer = null;
            if (!this._skipArmed) return;
            this._skipArmed = false;
            // 执行前再校验：期间用户已切走则放弃
            if (!this.currentItem || this.currentItem.id !== itemId) return;
            this.playIndex(nextIndex, true);
        }, 600);
    }

    // ===== 媒体事件 =====
    _bindMediaEvents(el) {
        el.addEventListener('loadedmetadata', () => {
            if (el === this.mediaElement && this.currentItem) {
                this._failedIds.delete(this.currentItem.id);
                this._retryCounts.delete(this.currentItem.id);
                // 视频「读取即取封面」：播放加载成功时立刻在后台抽帧，
                // 供舞台/列表显示（MediaFrameExtractor 内部去重，未缓存才生成）
                const cur = this.currentItem;
                if (cur && cur.kind === 'video' && window.MediaFrameExtractor) {
                    const stageImg = document.getElementById('mp-stage-cover-img');
                    if (stageImg && stageImg.isConnected) {
                        MediaFrameExtractor.request(cur.id, stageImg, () => { }, true);
                    }
                }
            }
            if (el === this.mediaElement && this._pendingResume > 0) {
                try { el.currentTime = this._pendingResume; } catch (e) { }
                this._pendingResume = 0;
            }
            this.app.onTimeUpdate();
        });

        el.addEventListener('timeupdate', () => {
            if (el === this.mediaElement) this.app.onTimeUpdate();
        });

        el.addEventListener('play', () => {
            if (el === this.mediaElement) this.app.onPlayStateChange(true);
        });

        el.addEventListener('pause', () => {
            if (el === this.mediaElement) this.app.onPlayStateChange(false);
        });

        el.addEventListener('ended', () => {
            if (el !== this.mediaElement) return;
            MediaProgressStore.clear(this.currentItem ? this.currentItem.id : '');
            this.next(true);
        });

        el.addEventListener('error', () => {
            if (!el.src || el !== this.mediaElement) return;
            const code = el.error ? el.error.code : 0;

            // 1. MEDIA_ERR_ABORTED：换源中止（快速切歌/连点导致上一资源被中断）属正常流程
            if (code === 1) return;

            const item = this.currentItem;
            const failedId = item ? item.id : '';
            console.warn('媒体加载失败:', code, el.error, failedId);

            // 2. 播放中解码错误（硬解/编码问题）：不自动跳歌，停下提示，避免整队列连环跳
            if (code === 3 && el.currentTime > 0) {
                Toast.error('解码错误，播放已停止（可点击下一曲继续）');
                this.app.onPlayStateChange(false);
                return;
            }
            if (!failedId) return;

            // 3. 网络/格式不支持错误：同曲重试一次，防瞬时网络抖动
            const attempts = this._retryCounts.get(failedId) || 0;
            if (attempts < 1) {
                this._retryCounts.set(failedId, attempts + 1);
                Toast.info('媒体加载失败，正在重试…');
                setTimeout(() => {
                    if (this.currentItem && this.currentItem.id === failedId) {
                        this._loadItem(this.currentItem, true);
                    }
                }, 800);
                return;
            }

            // 4. 重试仍失败：标记后只向前跳下一首（绝不回跳）；队列尽头则停止
            this._failedIds.add(failedId);
            const curIdx = this.queue.findIndex(x => x && x.id === failedId);
            const start = curIdx >= 0 ? curIdx + 1 : this.queue.length;
            const candidates = this.queue
                .map((x, index) => ({ item: x, index }))
                .filter(x => x.index >= start && x.item && !this._failedIds.has(x.item.id));
            if (!candidates.length) {
                Toast.error('媒体加载失败，请检查文件是否仍然存在');
                this.app.onPlayStateChange(false);
                return;
            }
            Toast.error('媒体加载失败，尝试下一曲');
            this._armSkip(failedId, candidates[0].index);
        });
    }
}
