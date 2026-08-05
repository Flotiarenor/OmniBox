// ===== 音频播放核心 =====
class PlayerCore {
    constructor(app) {
        this.app = app;
        this.audio = new Audio();
        this.audio.preload = 'auto';
        this.audio.crossOrigin = 'anonymous';

        this.queue = [];
        this.currentIndex = -1;
        this.playMode = 0; // 0=顺序 1=随机 2=单曲循环
        this.isPlaying = false;
        this._progressInterval = null;
        this._volumeSaveTimer = null;
        this._lastBackendSave = 0;

        this._bindAudioEvents();
    }

    _bindAudioEvents() {
        this.audio.addEventListener('timeupdate', () => {
            this._updateProgress();
            this.app._onTimeUpdate();
        });

        this.audio.addEventListener('loadedmetadata', () => {
            this._updateProgress();
        });

        this.audio.addEventListener('play', () => {
            this.isPlaying = true;
            this._updatePlayButton();
            this._startProgressSave();
        });

        this.audio.addEventListener('pause', () => {
            this.isPlaying = false;
            this._updatePlayButton();
            this._stopProgressSave();
        });

        this.audio.addEventListener('ended', () => {
            this._onEnded();
        });

        this.audio.addEventListener('error', () => {
            console.warn('音频加载失败:', this.audio.error);
            Toast.error('音频加载失败，尝试下一曲');
            setTimeout(() => this.next(), 500);
        });

        this.audio.addEventListener('volumechange', () => {
            const vb = document.getElementById('volume-bar');
            if (vb) vb.value = this.audio.volume;
            this._updateVolumeIcon();
        });
    }

    setQueue(songs, startIndex = 0) {
        this.queue = songs || [];
        if (this.queue.length > 0 && startIndex >= 0 && startIndex < this.queue.length) {
            this.currentIndex = startIndex;
            this._loadAndPlay();
        } else if (this.queue.length > 0) {
            this.currentIndex = 0;
            this._loadAndPlay();
        } else {
            this.currentIndex = -1;
            this.stop();
        }
    }

    addToQueue(songs) {
        this.queue.push(...songs);
        if (this.currentIndex < 0 && this.queue.length > 0) {
            this.currentIndex = 0;
            this._loadAndPlay();
        }
    }

    playSong(song) {
        const idx = this.queue.findIndex(s => s.id === song.id);
        if (idx >= 0) {
            this.currentIndex = idx;
            this._loadAndPlay();
        } else {
            this.queue.push(song);
            this.currentIndex = this.queue.length - 1;
            this._loadAndPlay();
        }
    }

    _loadAndPlay() {
        if (this.currentIndex < 0 || this.currentIndex >= this.queue.length) return;
        const song = this.queue[this.currentIndex];
        const url = Bridge.originalUrl(song.file_path || song.id);
        this.audio.src = url;
        this.audio.play().catch(e => console.log('自动播放被阻止:', e));
        this._updateNowPlaying(song);
        this.app._onSongChange(song);
        this._savePlaybackState();
    }

    play() {
        if (this.audio.src) {
            this.audio.play().catch(() => {});
        } else if (this.queue.length > 0 && this.currentIndex < 0) {
            this.currentIndex = 0;
            this._loadAndPlay();
        }
    }

    pause() {
        this.audio.pause();
    }

    togglePlay() {
        if (this.isPlaying) {
            this.pause();
        } else {
            this.play();
        }
    }

    stop() {
        this.audio.pause();
        this.audio.currentTime = 0;
        this.audio.src = '';
        this.isPlaying = false;
        this.currentIndex = -1;
        this._updateNowPlaying(null);
        this._updatePlayButton();
        this._stopProgressSave();
        this._updateProgress();
    }

    next() {
        if (this.queue.length === 0) return;
        let nextIdx;
        if (this.playMode === 1) {
            nextIdx = Math.floor(Math.random() * this.queue.length);
        } else if (this.playMode === 2) {
            nextIdx = this.currentIndex;
        } else {
            nextIdx = (this.currentIndex + 1) % this.queue.length;
        }
        this.currentIndex = nextIdx;
        this._loadAndPlay();
    }

    prev() {
        if (this.queue.length === 0) return;
        if (this.audio.currentTime > 3) {
            this.audio.currentTime = 0;
            return;
        }
        let prevIdx;
        if (this.playMode === 1) {
            prevIdx = Math.floor(Math.random() * this.queue.length);
        } else {
            prevIdx = (this.currentIndex - 1 + this.queue.length) % this.queue.length;
        }
        this.currentIndex = prevIdx;
        this._loadAndPlay();
    }

    _onEnded() {
        if (this.playMode === 2) {
            this.audio.currentTime = 0;
            this.audio.play().catch(() => {});
        } else {
            this.next();
        }
    }

    seekTo(percent) {
        if (this.audio.duration) {
            this.audio.currentTime = percent * this.audio.duration;
        }
    }

    seekDelta(seconds) {
        if (this.audio.duration) {
            this.audio.currentTime = Math.min(
                Math.max(this.audio.currentTime + seconds, 0),
                this.audio.duration
            );
        }
    }

    // ===== 音量 =====
    get volume() { return this.audio.volume; }

    set volume(v) {
        this.audio.volume = Math.max(0, Math.min(1, v));
        localStorage.setItem('musicPlayerVolume', v);
        clearTimeout(this._volumeSaveTimer);
        this._volumeSaveTimer = setTimeout(() => this._savePlaybackState(), 500);
    }

    toggleMute() {
        this.audio.muted = !this.audio.muted;
        this._updateVolumeIcon();
    }

    _updateVolumeIcon() {
        const icon = document.getElementById('btn-volume-icon');
        if (!icon) return;
        if (this.audio.muted || this.audio.volume === 0) {
            icon.textContent = '🔇';
        } else if (this.audio.volume < 0.3) {
            icon.textContent = '🔈';
        } else if (this.audio.volume < 0.6) {
            icon.textContent = '🔉';
        } else {
            icon.textContent = '🔊';
        }
    }

    // ===== 播放模式 =====
    cyclePlayMode() {
        this.playMode = (this.playMode + 1) % 3;
        const btn = document.getElementById('btn-play-mode');
        if (!btn) return;
        const icons = ['🔁', '🔀', '🔂'];
        const titles = ['顺序播放', '随机播放', '单曲循环'];
        btn.textContent = icons[this.playMode];
        btn.title = titles[this.playMode];
        this._savePlaybackState();
    }

    // ===== UI 更新 =====
    _updateNowPlaying(song) {
        const titleEl = document.getElementById('player-title');
        const artistEl = document.getElementById('player-artist');
        const coverEl = document.getElementById('player-cover');

        if (!song) {
            if (titleEl) titleEl.textContent = '未在播放';
            if (artistEl) artistEl.textContent = '';
            if (coverEl) coverEl.innerHTML = '<div class="cover-placeholder">🎵</div>';
            return;
        }

        if (titleEl) titleEl.textContent = song.title || '未知歌曲';
        if (artistEl) artistEl.textContent = song.artist || '';

        if (coverEl && song.cover_path) {
            coverEl.innerHTML = `<img src="${Bridge.originalUrl(song.cover_path)}" alt="cover">`;
        } else if (coverEl && song.has_cover) {
            coverEl.innerHTML = `<img src="${Bridge.originalUrl(song.cover_path || '.cache/covers/_default.jpg')}" alt="cover">`;
        } else if (coverEl) {
            coverEl.innerHTML = '<div class="cover-placeholder">🎵</div>';
        }
    }

    _updatePlayButton() {
        const btn = document.getElementById('btn-play-pause');
        if (btn) btn.textContent = this.isPlaying ? '⏸' : '▶';
    }

    _updateProgress() {
        const bar = document.getElementById('progress-bar');
        const cur = document.getElementById('time-current');
        const dur = document.getElementById('time-duration');
        if (!bar) return;

        if (this.audio.duration && !isNaN(this.audio.duration)) {
            bar.max = this.audio.duration;
            bar.value = this.audio.currentTime;
            if (cur) cur.textContent = MusicUtils.formatTime(this.audio.currentTime);
            if (dur) dur.textContent = MusicUtils.formatTime(this.audio.duration);
        } else {
            bar.value = 0;
            if (cur) cur.textContent = '00:00';
            if (dur) dur.textContent = '00:00';
        }
    }

    _startProgressSave() {
        this._stopProgressSave();
        this._progressInterval = setInterval(() => this._saveProgress(), 2000);
    }

    _stopProgressSave() {
        if (this._progressInterval) {
            clearInterval(this._progressInterval);
            this._progressInterval = null;
        }
    }

    _saveProgress() {
        if (!this.audio.src || this.currentIndex < 0) return;
        const song = this.queue[this.currentIndex];
        if (!song) return;
        try {
            const data = JSON.parse(localStorage.getItem('musicProgress') || '{}');
            data[song.id] = this.audio.currentTime;
            localStorage.setItem('musicProgress', JSON.stringify(data));
        } catch (e) {}
        const now = Date.now();
        if (now - this._lastBackendSave > 10000) {
            this._lastBackendSave = now;
            this._savePlaybackState();
        }
    }

    _savePlaybackState() {
        const song = this.queue.length > 0 && this.currentIndex >= 0 ? this.queue[this.currentIndex] : null;
        const loopMap = { 0: 'all', 1: 'all', 2: 'one' };
        Bridge.call('music_save_playback',
            song ? song.id : '',
            loopMap[this.playMode] || 'none',
            this.playMode === 1,
            this.volume
        ).catch(() => {});
    }

    getSavedProgress(songId) {
        try {
            const data = JSON.parse(localStorage.getItem('musicProgress') || '{}');
            return data[songId] || 0;
        } catch {
            return 0;
        }
    }

    // ===== 均衡器 =====
    EQ_FREQS = [32, 64, 125, 250, 500, 1000, 2000, 4000, 8000, 16000];

    _ensureEQ() {
        if (this._eqInitialized) return;
        this._eqInitialized = true;

        const ctx = new (window.AudioContext || window.webkitAudioContext)();
        this._source = ctx.createMediaElementSource(this.audio);
        this._eqNodes = [];
        this._analyser = ctx.createAnalyser();
        this._analyser.fftSize = 512;

        let prev = this._source;
        this.EQ_FREQS.forEach((freq) => {
            const filter = ctx.createBiquadFilter();
            filter.type = 'peaking';
            filter.frequency.value = freq;
            filter.Q.value = 1.0;
            filter.gain.value = 0;
            prev.connect(filter);
            prev = filter;
            this._eqNodes.push(filter);
        });
        prev.connect(this._analyser);
        this._analyser.connect(ctx.destination);

        const saved = localStorage.getItem('musicPlayerEQ');
        if (saved) {
            try {
                const bands = JSON.parse(saved);
                this._eqNodes.forEach((n, i) => { if (bands[i] !== undefined) n.gain.value = bands[i]; });
            } catch (e) {}
        }
    }

    setEqBand(index, gain) {
        this._ensureEQ();
        if (index >= 0 && index < this._eqNodes.length) {
            this._eqNodes[index].gain.value = gain;
        }
    }

    setEqBands(bands) {
        this._ensureEQ();
        bands.forEach((g, i) => this.setEqBand(i, g));
        try { localStorage.setItem('musicPlayerEQ', JSON.stringify(bands)); } catch (e) {}
    }

    getEqBands() {
        if (!this._eqNodes) return this.EQ_FREQS.map(() => 0);
        return this._eqNodes.map(n => Math.round(n.gain.value));
    }

    getAnalyser() {
        this._ensureEQ();
        return this._analyser;
    }

    getAudioContext() {
        this._ensureEQ();
        return this._analyser.context;
    }

    restorePlaybackState(pb, allSongs) {
        if (!pb || !pb.song_id) return false;
        const song = allSongs.find(s => s.id === pb.song_id);
        if (!song) return false;
        this.queue = allSongs;
        this.currentIndex = allSongs.indexOf(song);
        this.audio.src = Bridge.originalUrl(song.file_path || song.id);
        const savedPos = this.getSavedProgress(song.id);
        if (savedPos > 0) {
            this.audio.addEventListener('loadedmetadata', () => {
                this.audio.currentTime = savedPos;
            }, { once: true });
        }
        if (pb.loop_mode === 'one') this.playMode = 2;
        else if (pb.shuffle) this.playMode = 1;
        else this.playMode = 0;
        const btn = document.getElementById('btn-play-mode');
        if (btn) {
            const icons = ['🔁', '🔀', '🔂'];
            const titles = ['顺序播放', '随机播放', '单曲循环'];
            btn.textContent = icons[this.playMode];
            btn.title = titles[this.playMode];
        }
        if (pb.volume !== undefined && pb.volume !== null) {
            this.volume = pb.volume;
            const vb = document.getElementById('volume-bar');
            if (vb) vb.value = pb.volume;
        }
        this._updateNowPlaying(song);
        this._updatePlayButton();
        this.app._onSongChange(song);
        return true;
    }
}
