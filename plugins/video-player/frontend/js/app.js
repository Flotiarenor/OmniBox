class VideoPlayer {
    constructor() {
        this.currentPath = '';
        this.directories = [];
        this.playlist = [];
        this.filteredDirs = [];
        this.filteredFiles = [];
        this.currentIndex = -1;
        this.mediaElement = null;
        this.isVideo = true;
        this.playMode = 0;
        this.progressSaveInterval = null;
        this.volume = 1;
        this.isFullscreen = false;
        this.hideTimer = null;
        this.controlsVisible = true;
        this.fsChangeHandler = null;
    }

    async init() {
        this.bindUIEvents();
        this.bindKeyboardShortcuts();
        this.loadVolume();
        await this.navigateTo('');
        document.addEventListener('fullscreenchange', () => {
            if (!document.fullscreenElement && this.isFullscreen) {
                this.exitFullscreen();
            }
        });
    }

    // ---------- 导航与播放列表 ----------
    async navigateTo(path) {
        this.currentPath = path;
        document.getElementById('current-dir-label').textContent = path || '根目录';

        const mediaData = await Bridge.call('list_media', path);
        this.directories = mediaData.dirs || [];
        this.playlist = mediaData.files || [];
        this.filteredDirs = [...this.directories];
        this.filteredFiles = [...this.playlist];
        this.currentIndex = -1;
        this.stopPlayback();
        this.renderPlaylist();
        document.getElementById('playlist-filter').value = '';
    }

    goToParentDir() {
        if (this.currentPath === '') return;
        const parentPath = this.currentPath.split('/').slice(0, -1).join('/') || '';
        this.navigateTo(parentPath);
    }

    renderPlaylist() {
        const container = document.getElementById('playlist-container');
        container.innerHTML = '';
        const dirs = this.filteredDirs || [];
        const files = this.filteredFiles || [];

        if (dirs.length === 0 && files.length === 0) {
            container.innerHTML = '<div class="playlist-item" style="color:var(--text-secondary);">暂无媒体文件</div>';
            return;
        }

        dirs.forEach((dir) => {
            const item = document.createElement('div');
            item.className = 'playlist-item';
            item.innerHTML = `<span class="file-icon">📁</span>${dir.name}`;
            item.title = dir.name;
            item.addEventListener('click', () => {
                this.navigateTo(dir.path);
            });
            container.appendChild(item);
        });

        files.forEach((file) => {
            const item = document.createElement('div');
            item.className = 'playlist-item';
            const realIndex = this.playlist.indexOf(file);
            if (realIndex === this.currentIndex) {
                item.classList.add('active');
            }
            const icon = VideoUtils.isVideoName(file.name) ? '🎬' : '🎵';
            item.innerHTML = `<span class="file-icon">${icon}</span>${file.name}`;
            item.title = file.name;
            item.addEventListener('click', () => {
                this.playFile(realIndex);
            });
            container.appendChild(item);
        });
    }

    filterPlaylist(keyword) {
        if (!keyword) {
            this.filteredDirs = [...this.directories];
            this.filteredFiles = [...this.playlist];
        } else {
            const kw = keyword.toLowerCase();
            this.filteredDirs = this.directories.filter(d => d.name.toLowerCase().includes(kw));
            this.filteredFiles = this.playlist.filter(f => f.name.toLowerCase().includes(kw));
        }
        this.renderPlaylist();
    }

    // ---------- 播放控制 ----------
    playFile(index) {
        if (index < 0 || index >= this.playlist.length) return;

        this.currentIndex = index;
        const file = this.playlist[index];
        const url = Bridge.originalUrl(file.path);
        this.isVideo = VideoUtils.isVideoName(file.name);

        document.getElementById('player-placeholder').classList.add('hidden');
        document.getElementById('player-wrapper').classList.remove('hidden');

        const videoPlayer = document.getElementById('video-player');
        const audioPlayer = document.getElementById('audio-player');

        videoPlayer.style.display = this.isVideo ? 'block' : 'none';
        audioPlayer.style.display = this.isVideo ? 'none' : 'block';

        this.mediaElement = this.isVideo ? videoPlayer : audioPlayer;
        this.mediaElement.src = url;
        this.mediaElement.volume = this.volume;
        this.mediaElement.load();

        const savedTime = VideoProgressStore.get(file.path);
        if (savedTime > 0) {
            this.mediaElement.currentTime = savedTime;
        }

        this.mediaElement.play().catch(e => console.log('自动播放被阻止:', e));

        this.renderPlaylist();
        this.startProgressSave();
    }

    playPrev() {
        if (this.playlist.length === 0) return;
        let newIndex;
        if (this.playMode === 1) {
            newIndex = Math.floor(Math.random() * this.playlist.length);
        } else {
            newIndex = (this.currentIndex - 1 + this.playlist.length) % this.playlist.length;
        }
        this.playFile(newIndex);
    }

    playNext() {
        if (this.playlist.length === 0) return;
        let newIndex;
        if (this.playMode === 2) {
            newIndex = this.currentIndex;
        } else if (this.playMode === 1) {
            newIndex = Math.floor(Math.random() * this.playlist.length);
        } else {
            newIndex = (this.currentIndex + 1) % this.playlist.length;
        }
        this.playFile(newIndex);
    }

    stopPlayback() {
        this.saveProgress();
        this.stopProgressSave();
        if (this.mediaElement) {
            this.mediaElement.pause();
            this.mediaElement.currentTime = 0;
        }
        document.getElementById('player-placeholder').classList.remove('hidden');
        document.getElementById('player-wrapper').classList.add('hidden');
        this.currentIndex = -1;
        this.renderPlaylist();
    }

    togglePlay() {
        if (!this.mediaElement) return;
        if (this.mediaElement.paused) {
            this.mediaElement.play();
        } else {
            this.mediaElement.pause();
        }
    }

    seek(seconds) {
        if (this.mediaElement && this.mediaElement.duration) {
            this.mediaElement.currentTime = Math.min(
                Math.max(this.mediaElement.currentTime + seconds, 0),
                this.mediaElement.duration
            );
        }
    }

    adjustVolume(delta) {
        this.volume = Math.min(Math.max(this.volume + delta, 0), 1);
        if (this.mediaElement) {
            this.mediaElement.volume = this.volume;
        }
        document.getElementById('volume-bar').value = this.volume;
        localStorage.setItem('videoPlayerVolume', this.volume);
    }

    loadVolume() {
        const saved = localStorage.getItem('videoPlayerVolume');
        if (saved !== null) {
            const vol = parseFloat(saved);
            if (isFinite(vol) && vol >= 0 && vol <= 1) {
                this.volume = vol;
                document.getElementById('volume-bar').value = vol;
            }
        }
    }

    cyclePlayMode() {
        this.playMode = (this.playMode + 1) % 3;
        const btn = document.getElementById('btn-play-mode');
        const icons = ['🔁', '🔀', '🔂'];
        const titles = ['顺序播放', '随机播放', '单曲循环'];
        btn.textContent = icons[this.playMode];
        btn.title = titles[this.playMode];
    }

    // ---------- 全屏 ----------
    toggleFullscreen() {
        if (this.isFullscreen) {
            this.exitFullscreen();
        } else {
            this.enterFullscreen();
        }
    }

    enterFullscreen() {
        this.isFullscreen = true;
        document.getElementById('app').classList.add('fs-mode');
        this.startAutoHide();

        try {
            parent.document.documentElement.setAttribute('data-video-fullscreen', 'true');
        } catch (e) {}

        try {
            const api = parent.pywebview && parent.pywebview.api;
            if (api && api.system_toggle_fullscreen) {
                api.system_toggle_fullscreen();
            }
        } catch (e) {
            try {
                document.documentElement.requestFullscreen();
            } catch (e2) {}
        }
    }

    exitFullscreen() {
        this.isFullscreen = false;
        document.getElementById('app').classList.remove('fs-mode');
        document.getElementById('app').classList.remove('custom-hidden');
        this.stopAutoHide();
        this.showControls();

        try {
            parent.document.documentElement.removeAttribute('data-video-fullscreen');
        } catch (e) {}

        try {
            const api = parent.pywebview && parent.pywebview.api;
            if (api && api.system_toggle_fullscreen) {
                api.system_toggle_fullscreen();
            }
        } catch (e) {
            try {
                if (document.fullscreenElement) {
                    document.exitFullscreen();
                }
            } catch (e2) {}
        }
    }

    // ---------- 控件自动隐藏 ----------
    startAutoHide() {
        this.controlsVisible = true;
        this.showControls();
        this.scheduleHide();

        this.fsChangeHandler = (e) => {
            this.showControls();
            this.scheduleHide();
        };
        document.addEventListener('mousemove', this.fsChangeHandler);
    }

    stopAutoHide() {
        if (this.fsChangeHandler) {
            document.removeEventListener('mousemove', this.fsChangeHandler);
            this.fsChangeHandler = null;
        }
        if (this.hideTimer) {
            clearTimeout(this.hideTimer);
            this.hideTimer = null;
        }
    }

    scheduleHide() {
        if (this.hideTimer) {
            clearTimeout(this.hideTimer);
        }
        this.hideTimer = setTimeout(() => {
            if (this.isFullscreen && this.mediaElement && !this.mediaElement.paused) {
                this.hideControls();
            }
        }, 3000);
    }

    showControls() {
        this.controlsVisible = true;
        const controls = document.getElementById('custom-controls');
        if (controls) controls.classList.remove('controls-hidden');
        document.getElementById('app').classList.remove('custom-hidden');
    }

    hideControls() {
        this.controlsVisible = false;
        const controls = document.getElementById('custom-controls');
        if (controls) controls.classList.add('controls-hidden');
        document.getElementById('app').classList.add('custom-hidden');
    }

    // ---------- 记忆播放进度 ----------
    getSavedProgress(filePath) {
        return VideoProgressStore.get(filePath);
    }

    saveProgress() {
        if (!this.mediaElement || !this.mediaElement.src || this.currentIndex < 0) return;
        const file = this.playlist[this.currentIndex];
        if (!file) return;
        VideoProgressStore.save(file.path, this.mediaElement.currentTime);
    }

    startProgressSave() {
        this.stopProgressSave();
        this.progressSaveInterval = setInterval(() => this.saveProgress(), 2000);
    }

    stopProgressSave() {
        if (this.progressSaveInterval) {
            clearInterval(this.progressSaveInterval);
            this.progressSaveInterval = null;
        this.volume = 1;
        }
    }

    // ---------- UI 事件绑定 ----------
    bindUIEvents() {
        document.getElementById('btn-parent-dir').addEventListener('click', () => {
            this.goToParentDir();
        });

        document.getElementById('playlist-filter').addEventListener('input', (e) => {
            this.filterPlaylist(e.target.value);
        });

        const video = document.getElementById('video-player');
        const audio = document.getElementById('audio-player');
        const progressBar = document.getElementById('progress-bar');
        const volumeBar = document.getElementById('volume-bar');
        const playPauseBtn = document.getElementById('btn-play-pause');
        const stopBtn = document.getElementById('btn-stop');
        const prevBtn = document.getElementById('btn-prev');
        const nextBtn = document.getElementById('btn-next');
        const fullscreenBtn = document.getElementById('btn-fullscreen');
        const playModeBtn = document.getElementById('btn-play-mode');
        const timeDisplay = document.getElementById('time-display');

        const updateUI = () => {
            if (!this.mediaElement) return;
            playPauseBtn.textContent = this.mediaElement.paused ? '▶' : '⏸';
            if (this.mediaElement.duration && !isNaN(this.mediaElement.duration)) {
                progressBar.max = this.mediaElement.duration;
                progressBar.value = this.mediaElement.currentTime;
                timeDisplay.textContent =
                    `${VideoUtils.formatTime(this.mediaElement.currentTime)} / ${VideoUtils.formatTime(this.mediaElement.duration)}`;
            }
        };

        [video, audio].forEach(el => {
            el.addEventListener('timeupdate', updateUI);
            el.addEventListener('loadedmetadata', updateUI);
            el.addEventListener('play', () => {
                updateUI();
                if (this.isFullscreen) this.scheduleHide();
            });
            el.addEventListener('pause', () => {
                updateUI();
                if (this.isFullscreen) this.showControls();
            });
            el.addEventListener('ended', () => {
                this.playNext();
            });
            el.addEventListener('volumechange', () => {
                volumeBar.value = el.volume;
            });
        });

        playPauseBtn.addEventListener('click', () => this.togglePlay());

        stopBtn.addEventListener('click', () => this.stopPlayback());

        prevBtn.addEventListener('click', () => this.playPrev());
        nextBtn.addEventListener('click', () => this.playNext());

        progressBar.addEventListener('input', () => {
            if (this.mediaElement && this.mediaElement.duration) {
                this.mediaElement.currentTime = progressBar.value;
            }
        });

        volumeBar.addEventListener('input', () => {
            this.volume = parseFloat(volumeBar.value);
            if (this.mediaElement) {
                this.mediaElement.volume = this.volume;
            }
            localStorage.setItem('videoPlayerVolume', this.volume);
        });
        if (this.mediaElement) {
            volumeBar.value = this.mediaElement.volume;
        } else {
            const savedVol = localStorage.getItem('videoPlayerVolume');
            if (savedVol) volumeBar.value = parseFloat(savedVol);
        }

        fullscreenBtn.addEventListener('click', () => this.toggleFullscreen());

        playModeBtn.addEventListener('click', () => this.cyclePlayMode());

        document.getElementById('btn-settings').addEventListener('click', () => {
            openSettingsModal({
                title: '媒体库设置',
                successMessage: '媒体库设置已保存',
                onSave: async (values) => {
                    const result = await Bridge.call('save_settings', values);
                    if (result.success) {
                        await this.navigateTo(this.currentPath);
                        return { success: true };
                    }
                    return result;
                }
            });
        });

    }

    // ---------- 键盘快捷键 ----------
    bindKeyboardShortcuts() {
        document.addEventListener('keydown', (e) => {
            if (e.target.tagName === 'INPUT' || e.target.tagName === 'TEXTAREA') return;
            switch (e.key) {
                case ' ':
                    e.preventDefault();
                    this.togglePlay();
                    break;
                case 'ArrowLeft':
                    e.preventDefault();
                    this.seek(-5);
                    break;
                case 'ArrowRight':
                    e.preventDefault();
                    this.seek(5);
                    break;
                case 'ArrowUp':
                    e.preventDefault();
                    this.adjustVolume(0.05);
                    break;
                case 'ArrowDown':
                    e.preventDefault();
                    this.adjustVolume(-0.05);
                    break;
                case 'f':
                case 'F':
                    e.preventDefault();
                    this.toggleFullscreen();
                    break;
                case 'Escape':
                    if (this.isFullscreen) {
                        this.exitFullscreen();
                    }
                    break;
                case 'n':
                case 'N':
                    e.preventDefault();
                    this.playNext();
                    break;
                case 'p':
                case 'P':
                    e.preventDefault();
                    this.playPrev();
                    break;
                case 'm':
                case 'M':
                    e.preventDefault();
                    if (this.mediaElement) {
                        this.mediaElement.muted = !this.mediaElement.muted;
                    }
                    break;
            }
        });
    }

    // ---------- 工具函数 ----------
    formatTime(seconds) {
        return VideoUtils.formatTime(seconds);
    }
}