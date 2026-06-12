class VideoPlayer {
    constructor() {
        this.currentPath = '';
        this.playlist = [];            // 当前目录下的媒体文件列表
        this.filteredPlaylist = [];    // 过滤后的列表
        this.currentIndex = -1;        // 当前播放文件在 playlist 中的索引
        this.mediaElement = null;      // 当前活动的 <video> 或 <audio>
        this.isVideo = true;
        this.sidebarVisible = true;
        this.playMode = 0;             // 0:顺序 1:随机 2:单曲循环
        this.controlsTimer = null;     // 全屏控件自动隐藏定时器
        this.progressSaveInterval = null;
    }

    async init() {
        this.bindUIEvents();
        this.bindKeyboardShortcuts();
        this.loadVolume();
        await this.navigateTo('');     // 加载根目录
    }

    // ---------- 导航与播放列表 ----------
    async navigateTo(path) {
        this.currentPath = path;
        document.getElementById('current-dir-label').textContent = path || '根目录';

        const mediaData = await Bridge.call('list_media', path);
        this.playlist = mediaData.files;
        this.filteredPlaylist = [...this.playlist];
        this.currentIndex = -1;
        this.stopPlayback();
        this.renderPlaylist();
        // 清空搜索框
        document.getElementById('playlist-filter').value = '';
    }

    goToParentDir() {
        if (this.currentPath === '') return;
        const parentPath = this.currentPath.split('/').slice(0, -1).join('/') || '';
        this.navigateTo(parentPath);
    }

    renderPlaylist(filteredList = null) {
        const container = document.getElementById('playlist-container');
        container.innerHTML = '';
        const list = filteredList || this.filteredPlaylist;

        if (list.length === 0) {
            container.innerHTML = '<div class="playlist-item" style="color:var(--text-secondary);">暂无媒体文件</div>';
            return;
        }

        list.forEach((file, index) => {
            const item = document.createElement('div');
            item.className = 'playlist-item';
            // 使用原始 playlist 中的索引来确定是否高亮
            const realIndex = this.playlist.indexOf(file);
            if (realIndex === this.currentIndex) {
                item.classList.add('active');
            }

            const ext = file.name.split('.').pop().toLowerCase();
            const videoExts = ['mp4', 'mkv', 'webm', 'avi', 'mov', 'flv', 'wmv'];
            const icon = videoExts.includes(ext) ? '🎬' : '🎵';
            item.innerHTML = `<span class="file-icon">${icon}</span>${file.name}`;
            item.title = file.name;

            item.addEventListener('click', () => {
                this.playFile(realIndex);
            });

            container.appendChild(item);
        });
    }

    // 搜索过滤
    filterPlaylist(keyword) {
        if (!keyword) {
            this.filteredPlaylist = [...this.playlist];
        } else {
            const kw = keyword.toLowerCase();
            this.filteredPlaylist = this.playlist.filter(f => f.name.toLowerCase().includes(kw));
        }
        this.renderPlaylist();
    }

    // ---------- 播放控制 ----------
    playFile(index) {
        if (index < 0 || index >= this.playlist.length) return;

        this.currentIndex = index;
        const file = this.playlist[index];
        const url = Bridge.originalUrl(file.path);
        const ext = file.name.split('.').pop().toLowerCase();
        const videoExts = ['mp4', 'mkv', 'webm', 'avi', 'mov', 'flv', 'wmv'];
        this.isVideo = videoExts.includes(ext);

        document.getElementById('player-placeholder').classList.add('hidden');
        document.getElementById('player-wrapper').classList.remove('hidden');

        const videoPlayer = document.getElementById('video-player');
        const audioPlayer = document.getElementById('audio-player');

        videoPlayer.style.display = this.isVideo ? 'block' : 'none';
        audioPlayer.style.display = this.isVideo ? 'none' : 'block';

        this.mediaElement = this.isVideo ? videoPlayer : audioPlayer;
        this.mediaElement.src = url;
        this.mediaElement.load();

        // 恢复记忆的播放进度
        const savedTime = this.getSavedProgress(file.path);
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
        if (this.playMode === 1) { // 随机
            newIndex = Math.floor(Math.random() * this.playlist.length);
        } else {
            newIndex = (this.currentIndex - 1 + this.playlist.length) % this.playlist.length;
        }
        this.playFile(newIndex);
    }

    playNext() {
        if (this.playlist.length === 0) return;
        let newIndex;
        if (this.playMode === 2) { // 单曲循环
            newIndex = this.currentIndex;
        } else if (this.playMode === 1) { // 随机
            newIndex = Math.floor(Math.random() * this.playlist.length);
        } else {
            newIndex = (this.currentIndex + 1) % this.playlist.length;
        }
        this.playFile(newIndex);
    }

    stopPlayback() {
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
        if (this.mediaElement) {
            let newVol = Math.min(Math.max(this.mediaElement.volume + delta, 0), 1);
            this.mediaElement.volume = newVol;
            document.getElementById('volume-bar').value = newVol;
            localStorage.setItem('videoPlayerVolume', newVol);
        }
    }

    loadVolume() {
        const saved = localStorage.getItem('videoPlayerVolume');
        if (saved !== null) {
            const vol = parseFloat(saved);
            document.getElementById('volume-bar').value = vol;
            // 媒体元素尚未创建，后续在 playFile 时设置
        }
    }

    // 播放模式切换
    cyclePlayMode() {
        this.playMode = (this.playMode + 1) % 3;
        const btn = document.getElementById('btn-play-mode');
        const icons = ['🔁', '🔀', '🔂']; // 顺序、随机、单曲循环
        const titles = ['顺序播放', '随机播放', '单曲循环'];
        btn.textContent = icons[this.playMode];
        btn.title = titles[this.playMode];
    }

    // 全屏切换
    toggleFullscreen() {
        const main = document.querySelector('.player-main');
        if (!document.fullscreenElement) {
            main.requestFullscreen().then(() => {
                main.classList.add('fullscreen');
                this.startFullscreenControlTimer();
            });
        } else {
            document.exitFullscreen().then(() => {
                main.classList.remove('fullscreen');
                this.stopFullscreenControlTimer();
            });
        }
    }

    // 全屏控件自动隐藏/显示
    startFullscreenControlTimer() {
        const main = document.querySelector('.player-main');
        const showControls = () => {
            main.classList.add('show-controls');
            clearTimeout(this.controlsTimer);
            this.controlsTimer = setTimeout(() => {
                main.classList.remove('show-controls');
            }, 3000);
        };
        // 鼠标移动显示控件
        main.addEventListener('mousemove', showControls);
        main.addEventListener('mouseleave', () => {
            main.classList.remove('show-controls');
            clearTimeout(this.controlsTimer);
        });
        // 初始显示
        showControls();
    }

    stopFullscreenControlTimer() {
        clearTimeout(this.controlsTimer);
        const main = document.querySelector('.player-main');
        main.classList.remove('show-controls');
        // 移除事件监听（简单处理：移除所有，实际可优化）
        main.removeEventListener('mousemove', () => {});
    }

    // 记忆播放进度
    getSavedProgress(filePath) {
        try {
            const data = JSON.parse(localStorage.getItem('videoProgress') || '{}');
            return data[filePath] || 0;
        } catch {
            return 0;
        }
    }

    saveProgress() {
        if (!this.mediaElement || !this.mediaElement.src || this.currentIndex < 0) return;
        const file = this.playlist[this.currentIndex];
        if (!file) return;
        try {
            const data = JSON.parse(localStorage.getItem('videoProgress') || '{}');
            data[file.path] = this.mediaElement.currentTime;
            localStorage.setItem('videoProgress', JSON.stringify(data));
        } catch (e) {
            console.warn('保存进度失败', e);
        }
    }

    startProgressSave() {
        this.stopProgressSave();
        this.progressSaveInterval = setInterval(() => this.saveProgress(), 2000);
    }

    stopProgressSave() {
        if (this.progressSaveInterval) {
            clearInterval(this.progressSaveInterval);
            this.progressSaveInterval = null;
        }
    }

    // ---------- UI 事件绑定 ----------
    bindUIEvents() {
        // 侧边栏折叠（侧边栏头部按钮）
        document.getElementById('btn-toggle-sidebar').addEventListener('click', () => {
            this.sidebarVisible = !this.sidebarVisible;
            this.updateSidebarUI();
        });

        // 控件栏内的展开按钮
        document.getElementById('btn-expand-sidebar').addEventListener('click', () => {
            this.sidebarVisible = true;
            this.updateSidebarUI();
        });

        // 上级目录
        document.getElementById('btn-parent-dir').addEventListener('click', () => {
            this.goToParentDir();
        });

        // 搜索过滤
        document.getElementById('playlist-filter').addEventListener('input', (e) => {
            this.filterPlaylist(e.target.value);
        });

        // 播放器控件
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

        // 统一更新 UI 的函数
        const updateUI = () => {
            if (!this.mediaElement) return;
            playPauseBtn.textContent = this.mediaElement.paused ? '▶' : '⏸';
            if (this.mediaElement.duration && !isNaN(this.mediaElement.duration)) {
                progressBar.max = this.mediaElement.duration;
                progressBar.value = this.mediaElement.currentTime;
                timeDisplay.textContent =
                    `${this.formatTime(this.mediaElement.currentTime)} / ${this.formatTime(this.mediaElement.duration)}`;
            }
        };

        // 绑定事件到两个媒体元素
        [video, audio].forEach(el => {
            el.addEventListener('timeupdate', updateUI);
            el.addEventListener('loadedmetadata', updateUI);
            el.addEventListener('play', updateUI);
            el.addEventListener('pause', updateUI);
            el.addEventListener('ended', () => {
                this.playNext();
            });
            // 音量同步
            el.addEventListener('volumechange', () => {
                volumeBar.value = el.volume;
            });
        });

        // 播放/暂停
        playPauseBtn.addEventListener('click', () => this.togglePlay());

        // 停止
        stopBtn.addEventListener('click', () => this.stopPlayback());

        // 上一个/下一个
        prevBtn.addEventListener('click', () => this.playPrev());
        nextBtn.addEventListener('click', () => this.playNext());

        // 进度条拖动
        progressBar.addEventListener('input', () => {
            if (this.mediaElement && this.mediaElement.duration) {
                this.mediaElement.currentTime = progressBar.value;
            }
        });

        // 音量调节
        volumeBar.addEventListener('input', () => {
            if (this.mediaElement) {
                this.mediaElement.volume = volumeBar.value;
                localStorage.setItem('videoPlayerVolume', volumeBar.value);
            }
        });
        // 初始化音量（如果已有媒体元素）
        if (this.mediaElement) {
            volumeBar.value = this.mediaElement.volume;
        } else {
            // 从存储加载
            const savedVol = localStorage.getItem('videoPlayerVolume');
            if (savedVol) volumeBar.value = parseFloat(savedVol);
        }

        // 全屏
        fullscreenBtn.addEventListener('click', () => this.toggleFullscreen());

        // 播放模式
        playModeBtn.addEventListener('click', () => this.cyclePlayMode());

        // 设置按钮
        document.getElementById('btn-settings').addEventListener('click', async () => {
            const modal = document.getElementById('settings-modal');
            modal.classList.add('active');
            const rootDir = await Bridge.call('get_root_dir');
            document.getElementById('setting-root-dir').value = rootDir;
        });

        // 监听全屏变化（按 ESC 退出时同步状态）
        document.addEventListener('fullscreenchange', () => {
            const main = document.querySelector('.player-main');
            if (!document.fullscreenElement) {
                main.classList.remove('fullscreen');
                this.stopFullscreenControlTimer();
            }
        });
    }

    updateSidebarUI() {
        const sidebar = document.getElementById('sidebar');
        const toggleBtn = document.getElementById('btn-toggle-sidebar');
        const expandBtn = document.getElementById('btn-expand-sidebar');
        if (this.sidebarVisible) {
            sidebar.classList.remove('collapsed');
            toggleBtn.textContent = '◀';
            expandBtn.style.display = 'none';
        } else {
            sidebar.classList.add('collapsed');
            toggleBtn.textContent = '▶';
            expandBtn.style.display = 'inline-block';
        }
    }

    // ---------- 键盘快捷键 ----------
    bindKeyboardShortcuts() {
        document.addEventListener('keydown', (e) => {
            // 避免在输入框中触发
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

    // ---------- 设置 ----------
    async saveSettings() {
        const newRoot = document.getElementById('setting-root-dir').value.trim();
        const result = await Bridge.call('save_settings', { root_dir: newRoot });
        if (result.success) {
            alert('根目录已更新，正在刷新...');
            closeSettingsModal();
            await this.navigateTo(this.currentPath);
        } else {
            alert('保存失败：' + (result.error || '路径无效'));
        }
    }

    // ---------- 工具函数 ----------
    formatTime(seconds) {
        if (isNaN(seconds)) return '00:00';
        const m = Math.floor(seconds / 60);
        const s = Math.floor(seconds % 60);
        return `${m.toString().padStart(2, '0')}:${s.toString().padStart(2, '0')}`;
    }
}

// 全局关闭模态框函数
function closeSettingsModal() {
    document.getElementById('settings-modal').classList.remove('active');
}
