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
        // 播放起点：'restart'=从头播放（默认） 'resume'=保留播放进度（剩余不足 5s 从头）
        this.resumeMode = 'restart';

        this._volume = 1;            // 线性位置（0~1，存储/传输语义）
        this._volumeMapper = new VolumeMapper(2.5);   // 指数映射：低音量区更精细
        this._muted = false;
        this._progressSaver = null;
        this._pendingResume = 0;
        this._lastBackendSave = 0;
        this._failedIds = new Set();
        this._retryCounts = new Map();   // 每曲加载失败重试计数（成功后清除）
        this._pendingSkipTimer = null;   // 失败后自动跳下一曲的挂起定时器
        this._skipArmed = false;         // 定时器是否仍有效（用户操作即解除）
        this._loadSeq = 0;
        // 当前加载对应的导航方向：+1 = 向后（下一首 / 直接选曲），-1 = 向前（上一首）。
        // 加载失败后按此方向继续找相邻可播放条目，避免「上一首」落到失败条目后反向跳到后面。
        this._loadNavDir = 1;

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
        this._invalidateShuffleOrder();
        this.saveQueueState();
        if (this.queue.length === 0) {
            this.stop();
            return;
        }
        const idx = Math.max(0, Math.min(this.queue.length - 1, startIndex));
        this.playIndex(idx, autoplay);
    }

    playIndex(index, autoplay = true, navDir = 1) {
        if (!this.queue.length) return;
        if (index < 0 || index >= this.queue.length) index = 0;
        // 用户（或自动跳转）选择了曲目：解除任何挂起的自动跳转，并允许重试此前失败的曲目
        this._clearPendingSkip();
        this._loadNavDir = navDir < 0 ? -1 : 1;
        // 直接选曲（含随机模式下点行）：只把随机排列的游标移到该条目，排列本身不变，
        // 因此之后前进/后退仍沿同一份顺序，就是"怎么走顺序都一样"。
        this._syncShuffleCursor(index);
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
            this._invalidateShuffleOrder();
            this.saveQueueState();
            this.playIndex(this.queue.length - 1, autoplay);
        }
    }

    // ===== 随机播放顺序 =====
    // 随机模式下一份排列覆盖整条队列，游标指向当前条目：next/prev 沿同一份排列进退，
    // 因此「上一首」按原路返回，而不是重新随机取下标（历史行为：进退互不相关，
    // 上一首可能跳到队列任意位置，表现为与列表顺序无关、无规律）。
    // 排列在下列时机重建并锚定到当前条目：队列内容变化、切换播放模式、恢复播放、走完一轮。
    _invalidateShuffleOrder() {
        this._shuffleOrder = null;
        this._shuffleCursor = 0;
    }

    // anchorIndex：把该条目放到排列首位（首次进入随机 / 恢复播放时先播当前条目）；
    // avoidFirst：排列首位避开该条目（走完一轮重排时避免立刻重播刚播完的条目）。
    _buildShuffleOrder(anchorIndex = this.currentIndex, avoidFirst = null) {
        const order = this.queue.map((_, i) => i);
        for (let i = order.length - 1; i > 0; i--) {
            const j = Math.floor(Math.random() * (i + 1));
            [order[i], order[j]] = [order[j], order[i]];
        }
        const anchor = order.indexOf(anchorIndex);
        if (anchor > 0) {
            [order[0], order[anchor]] = [order[anchor], order[0]];
        }
        if (order.length > 1 && order[0] === avoidFirst) {
            [order[0], order[1]] = [order[1], order[0]];
        }
        this._shuffleOrder = order;
        this._shuffleCursor = 0;
    }

    _ensureShuffleOrder() {
        if (!Array.isArray(this._shuffleOrder) || this._shuffleOrder.length !== this.queue.length) {
            this._buildShuffleOrder();
        }
    }

    _syncShuffleCursor(index) {
        if (this.playMode !== 1) return;
        if (!Array.isArray(this._shuffleOrder) || this._shuffleOrder.length !== this.queue.length) {
            this._buildShuffleOrder(index);
            return;
        }
        const at = this._shuffleOrder.indexOf(index);
        if (at >= 0) this._shuffleCursor = at;
    }

    // 沿排列走一步：dir=+1 前进，dir=-1 后退。走到排列末尾重新生成一份并从头开始；
    // 已在排列开头再后退则停在第 0 位（不反向重排，也不跳到任意位置）。
    _stepShuffle(dir) {
        this._ensureShuffleOrder();
        const size = this._shuffleOrder.length;
        if (!size) return -1;
        if (dir < 0 && this._shuffleCursor <= 0) {
            this._shuffleCursor = 0;
            return this._shuffleOrder[0];
        }
        let cursor = this._shuffleCursor + dir;
        if (cursor >= size) {
            this._buildShuffleOrder(-1, this._shuffleOrder[size - 1]);
            cursor = 0;
        }
        cursor = Math.max(0, Math.min(size - 1, cursor));
        this._shuffleCursor = cursor;
        return this._shuffleOrder[cursor];
    }

    // 当前导航顺序（队列下标序列）：顺序模式即队列自身，随机模式即随机排列
    _navOrder() {
        if (this.playMode === 1) {
            this._ensureShuffleOrder();
            return this._shuffleOrder;
        }
        return this.queue.map((_, i) => i);
    }

    async _loadItem(item, autoplay = true) {
        if (!item) return;
        const loadId = ++this._loadSeq;
        this._saveProgress();
        this.currentItem = item;

        // 切换前记住当前进度，避免视频/音频元素切换丢进度。
        // 同一曲目重载（重试/重新点击）不续播；续播位置统一交给 _resumeTarget 判定
        const keepTime = item.id !== (this._previousItem && this._previousItem.id)
            ? MediaProgressStore.get(item)
            : 0;
        this._previousItem = item;
        // 此刻还不知道时长，先记录候选位置，真正的"剩余不足 5s 从头"在 loadedmetadata 里判定
        this._pendingResume = this._resumeTarget(keepTime, 0);

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
        this._applyVolume();
        el.muted = this._muted;
        el.load();

        if (autoplay) {
            el.play().catch((e) => console.log('自动播放被阻止:', e));
        }
        this._savePlaybackState();
        Bridge.call('media_update_recent', item.id).catch(() => { });
    }

    // ===== 播放起点（设置项 resume_mode）=====
    // 'restart'：每次点击曲目都从 0 开始；'resume'：续播上次位置，
    // 但剩余不足 RESUME_TAIL_SECONDS 时一律从头开始。
    setResumeMode(mode) {
        this.resumeMode = mode === 'resume' ? 'resume' : 'restart';
    }

    // 把候选续播位置换算为真正的起点：返回 0 即从头播放。
    // duration 未知（0/NaN/Infinity，如网络流）时不做剩余时长判定。
    _resumeTarget(pos, duration) {
        if (this.resumeMode !== 'resume') return 0;
        const target = Number(pos) || 0;
        if (!(target > 0)) return 0;
        if (isFinite(duration) && duration > 0) {
            if (target >= duration) return 0;
            if (duration - target < this.RESUME_TAIL_SECONDS) return 0;
        }
        return target;
    }

    // 元素停在末尾时 play() 不会重新出声（本 bug 的"无法播放"表现）：先回到 0
    _rewindIfAtEnd(el) {
        if (!el) return;
        const nearEnd = isFinite(el.duration) && el.duration > 0
            && el.duration - (el.currentTime || 0) <= 0.25;
        if (el.ended || nearEnd) {
            try { el.currentTime = 0; } catch (e) { }
        }
    }

    togglePlay() {
        // 用户手动播放/暂停：解除任何挂起的自动跳歌定时器（失败重试 600ms 窗口内
        // 用户点播放应继续当前曲目，而不是被定时器覆盖跳走）
        this._clearPendingSkip();
        const el = this.mediaElement;
        if (this.currentItem && el && el.src) {
            if (el.paused) {
                this._rewindIfAtEnd(el);
                el.play().catch(() => { });
            } else {
                el.pause();
            }
        } else if (this.queue.length) {
            this.playIndex(Math.max(0, this.currentIndex), true);
        }
    }

    next(auto = true) {
        if (!this.queue.length) return;
        this._clearPendingSkip();
        let idx;
        if (this.playMode === 1) {
            idx = this._stepShuffle(1);
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
        this.playIndex(idx, true, 1);
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
            idx = this._stepShuffle(-1);
        } else {
            idx = (this.currentIndex - 1 + this.queue.length) % this.queue.length;
        }
        this.playIndex(idx, true, -1);
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
        this._invalidateShuffleOrder();
        this.app.onTrackChange(null);
        this._savePlaybackState();
    }

    seekTo(value) {
        this._clearPendingSkip();
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
        // 进入/离开随机模式都重建排列：再次开启随机应按当前条目重新排一份
        this._invalidateShuffleOrder();
        this.app.updatePlayModeUI();
        this._savePlaybackState();
        const names = ['顺序播放', '随机播放', '单曲循环'];
        Toast.info(names[this.playMode]);
        return this.playMode;
    }

    // ===== 音量 =====
    get volume() { return this._volume; }

    // 统一应用点：仅此处把线性位置映射为实际音量系数（指数映射）
    _applyVolume() {
        const actual = this._volumeMapper.linearToActual(this._volume);
        this.audio.volume = actual;
        this.video.volume = actual;
    }

    set volume(v) {
        this._volume = Math.max(0, Math.min(1, v));
        this._applyVolume();
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
        // 画面/仅声音切换是同一曲目的连续播放，不走续播逻辑；清掉挂起的续播位置，
        // 避免 loadedmetadata 时被旧值覆盖。停在末尾则从 0 开始，否则停在末尾 play() 无声
        this._pendingResume = 0;
        el.src = MPUtils.mediaUrl(this.currentItem.path);
        this._applyVolume();
        el.muted = this._muted;
        el.load();
        const resume = () => {
            const atEnd = isFinite(el.duration) && el.duration > 0 && position >= el.duration - 0.25;
            if (position > 0 && !atEnd) el.currentTime = position;
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

    // 进度记忆写入口：只在"离结尾还有足够距离"时落盘。
    // 历史 bug：曲目播完后 currentTime 停在 duration，切歌瞬间 _loadItem 会先
    // _saveProgress()，把这个"已播完"位置写回存储，覆盖掉 ended 里的 clear()；
    // 下次点击该曲目就续播到末尾，元素停在末尾 play() 无声——表现为"无法播放"。
    _saveProgress() {
        const el = this.mediaElement;
        if (!this.currentItem || !el || !el.src) return;
        const position = el.currentTime || 0;
        if (!(position > 2)) return;
        if (el.ended) return;   // 已播完：进度由 ended 清除，不能再写回
        if (isFinite(el.duration) && el.duration > 0
            && el.duration - position < this.RESUME_TAIL_SECONDS) {
            // 剩余不足 5s：按设置语义等同"从头开始"，直接清掉记录
            MediaProgressStore.clear(this.currentItem.id);
            return;
        }
        MediaProgressStore.save(this.currentItem.id, position);
    }

    _savePlaybackState() {
        const loopMap = { 0: 'all', 1: 'all', 2: 'one' };
        Bridge.call('media_save_playback',
            this.currentItem ? this.currentItem.id : '',
            loopMap[this.playMode] || 'none',
            this.playMode === 1,
            this._volume,
            this.videoMode ? 'video' : 'audio',
            Math.max(0, this.currentIndex)
        ).catch(() => { });
    }

    // 队列变化时单独落盘一次 id 列表（换曲只更新下标，不必每次重传整条队列）
    saveQueueState() {
        Bridge.call('media_save_queue', this.queue.map(i => (i ? i.id : '')))
            .catch(() => { });
    }

    _debouncedSavePlayback() {
        clearTimeout(this._saveTimer);
        this._saveTimer = setTimeout(() => this._savePlaybackState(), 600);
    }

    async restorePlayback(item, pb) {
        if (!item || !item.id) return false;
        this._clearPendingSkip();
        const savedPos = MediaProgressStore.get(item);
        // 恢复上次的整条队列：后端保存的是 id 列表，按 id 批量取回条目。
        // 只恢复当前条目时（历史行为）next/prev 会退化成单条队列，与可见列表不符。
        let queue = [];
        const ids = Array.isArray(pb.queue_ids) ? pb.queue_ids.filter(Boolean) : [];
        if (ids.length) {
            try {
                const items = await Bridge.call('media_get_items', ids);
                queue = (items || []).filter(x => x && x.id);
            } catch (e) {
                queue = [];
            }
        }
        let index = queue.findIndex(x => x.id === item.id);
        if (index < 0) {
            queue = [item];
            index = 0;
        } else {
            queue[index] = item;
        }
        this.queue = queue;
        this.currentIndex = index;
        this.currentItem = item;

        if (pb.loop_mode === 'one') this.playMode = 2;
        else if (pb.shuffle) this.playMode = 1;
        else this.playMode = 0;
        this._invalidateShuffleOrder();

        if (pb.volume !== undefined && pb.volume !== null) {
            // pb.volume 是线性位置（保存时即线性语义），直接赋值，映射在 _applyVolume
            this._volume = Math.max(0, Math.min(1, Number(pb.volume) || 1));
            this._applyVolume();
        }

        this._previousItem = item;
        // 恢复上次曲目同样受播放起点设置约束（时长未知，loadedmetadata 里补判剩余时长）
        this._pendingResume = this._resumeTarget(savedPos, 0);
        // 视频画面/仅声音：优先用上次持久化的模式，其次回落到设置项
        const savedVideoMode = pb.video_mode
            || (this.app.settings && this.app.settings.default_video_mode)
            || 'video';
        this.videoMode = item.kind !== 'video' || savedVideoMode !== 'audio';
        const el = this.mediaElement;
        el.src = item.stream_url || item.url || MPUtils.mediaUrl(item.path);
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
    RESUME_TAIL_SECONDS = 5;   // 剩余不足该秒数时续播一律改为从头播放

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

            // 两个媒体元素共同汇入同一条滤波链；单个元素接管失败（如已连接过）
            // 不应毒化整条 EQ 链：各自容错，至少保留能用的那一路
            [this.audio, this.video].forEach(el => {
                try {
                    this._sources[el === this.audio ? 'audio' : 'video'] = this._audioCtx.createMediaElementSource(el);
                } catch (e) {
                    console.warn('EQ 接管媒体元素失败（该元素将直通扬声器）:', e);
                }
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
        // 定时器内跳转必须沿用本次加载的导航方向：失败条目可能连续多个，
        // 若让 playIndex 取默认方向，链条中的下一跳就会反向（上一首最终跳到后面）。
        const dir = this._loadNavDir;
        this._pendingSkipTimer = setTimeout(() => {
            this._pendingSkipTimer = null;
            if (!this._skipArmed) return;
            this._skipArmed = false;
            // 执行前再校验：期间用户已切走则放弃
            if (!this.currentItem || this.currentItem.id !== itemId) return;
            this.playIndex(nextIndex, true, dir);
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
                // 时长此时才可知：剩余不足 5s（或位置已越界）则不续播，直接从头
                const target = this._resumeTarget(this._pendingResume, el.duration);
                this._pendingResume = 0;
                if (target > 0) {
                    try { el.currentTime = target; } catch (e) { }
                } else if (this.currentItem) {
                    MediaProgressStore.clear(this.currentItem.id);
                }
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

            // 4. 重试仍失败：按本次加载的导航方向继续找相邻可播放条目并跳转。
            //    向后（下一首 / 直接选曲）与历史行为一致；向前（上一首）只在当前条目
            //    之前查找，绝不向队列后面跳 —— 历史缺陷：上一首落到失败条目后固定向后
            //    跳，表现为「上一首」停在原曲目（表观无效）或反向前进，跳转距离取决于
            //    连续失败条目数，现象无规律。队列尽头（本方向已无可播条目）则停止。
            this._failedIds.add(failedId);
            const curIdx = this.queue.findIndex(x => x && x.id === failedId);
            const dir = this._loadNavDir;
            // 候选按当前导航顺序取（顺序模式=队列顺序，随机模式=随机排列），只取本方向第一个
            // 未失败条目；该方向已无可播放条目则停止，绝不向反方向跳。
            const order = this._navOrder();
            const at = order.indexOf(curIdx);
            let nextIndex = -1;
            if (at >= 0) {
                for (let step = 1; step < order.length; step++) {
                    const pos = at + dir * step;
                    if (pos < 0 || pos >= order.length) break;
                    const candidate = this.queue[order[pos]];
                    if (candidate && !this._failedIds.has(candidate.id)) {
                        nextIndex = order[pos];
                        break;
                    }
                }
            }
            if (nextIndex < 0) {
                Toast.error('媒体加载失败，请检查文件是否仍然存在');
                this.app.onPlayStateChange(false);
                return;
            }
            Toast.error(dir < 0 ? '媒体加载失败，尝试上一曲' : '媒体加载失败，尝试下一曲');
            this._armSkip(failedId, nextIndex);
        });
    }
}
