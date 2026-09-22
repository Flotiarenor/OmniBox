// ============================================================
// OmniBox 媒体播放器 — UI 事件绑定与视频封面预取
//
// 本文件是 MediaPlayerApp.prototype 的一个分片：类骨架与构造函数在 app.js，
// 这里用 Object.assign 把方法扩回同一个原型。**加载顺序是硬约束** —— 必须在
// app.js 之后、实例化之前（见 index.html），契约由 tests/js/media_player_app_split.mjs 把关。
// ============================================================

Object.assign(MediaPlayerApp.prototype, {

    // ============================================================
    // UI 事件绑定
    // ============================================================
    _bindUI() {
        // 左侧导航
        document.querySelectorAll('.mp-nav-item').forEach(btn => {
            btn.addEventListener('click', () => this.switchView(btn.dataset.view));
        });

        // 工具栏
        const scanBtn = document.getElementById('btn-scan');
        const deepScanBtn = document.getElementById('btn-deep-scan');
        scanBtn.addEventListener('click', () => this._doScan(false));    // 增量扫描
        deepScanBtn.addEventListener('click', () => this._doScan(true)); // 深度全量扫描
        document.getElementById('btn-settings').addEventListener('click', () => this._openSettings());
        const search = document.getElementById('media-search');
        this._searchDebounced = MPUtils.debounce(() => this._loadCurrentView(), 300);
        search.addEventListener('input', () => {
            const value = search.value.trim();
            document.getElementById('btn-search-clear').classList.toggle('hidden', !value);
            this._searchDebounced();
        });
        search.addEventListener('keydown', (e) => {
            if (e.key === 'Escape') {
                search.value = '';
                document.getElementById('btn-search-clear').classList.add('hidden');
                this._loadCurrentView();
            }
        });
        document.getElementById('btn-search-clear').addEventListener('click', () => {
            search.value = '';
            document.getElementById('btn-search-clear').classList.add('hidden');
            this._loadCurrentView();
            search.focus();
        });

        // 歌单
        document.getElementById('btn-new-playlist').addEventListener('click', () => this._openPlaylistModal('create'));

        // 播放控制
        document.getElementById('btn-play-pause').addEventListener('click', () => this.core.togglePlay());
        document.getElementById('btn-prev').addEventListener('click', () => this.core.prev());
        document.getElementById('btn-next').addEventListener('click', () => this.core.next(false));
        document.getElementById('btn-stop').addEventListener('click', () => this.core.stop());
        document.getElementById('btn-play-mode').addEventListener('click', () => this.core.cyclePlayMode());
        document.getElementById('btn-toggle-fav').addEventListener('click', () => this._toggleCurrentFavorite());
        document.getElementById('player-cover').addEventListener('click', () => this._toggleLyrics());

        const progress = document.getElementById('progress-bar');
        progress.addEventListener('input', () => this.core.seekTo(parseFloat(progress.value)));
        progress.addEventListener('change', () => this.core.seekTo(parseFloat(progress.value)));

        // 音量
        const volumeBar = document.getElementById('volume-bar');
        volumeBar.addEventListener('input', () => {
            this.core.volume = parseFloat(volumeBar.value);
        });
        document.getElementById('btn-volume-icon').addEventListener('click', () => this.core.toggleMute());

        // 右侧工具
        document.getElementById('btn-queue').addEventListener('click', () => this._toggleQueue());
        document.getElementById('btn-clear-queue').addEventListener('click', () => this._clearQueue());
        document.getElementById('btn-eq').addEventListener('click', () => this._toggleEQ());
        document.getElementById('btn-eq-close').addEventListener('click', () => this._hideEQ());
        document.getElementById('btn-eq-reset').addEventListener('click', () => this._resetEQ());
        document.getElementById('btn-eq-save').addEventListener('click', () => MPUtils.openModal('modal-eq-name'));
        document.getElementById('eq-preset-select').addEventListener('change', (e) => this._applyEQPreset(e.target.value));
        document.getElementById('btn-video-mode').addEventListener('click', () => this._toggleVideoMode());
        document.getElementById('btn-wide').addEventListener('click', () => this._toggleWideMode());
          document.getElementById('btn-fullscreen').addEventListener('click', () => this.toggleFullscreen());

        // 视频舞台：音乐封面 → 歌词页；视频画面 → 全屏
        const stageViewport = document.querySelector('#mp-stage .mp-stage-viewport');
        stageViewport.addEventListener('click', (e) => this._onStageClick(e));
        document.getElementById('mp-stage-cover').addEventListener('click', () => this._toggleLyrics());
        document.getElementById('mp-stage-lyrics').addEventListener('click', () => this._toggleLyrics());
        document.getElementById('btn-stage-play').addEventListener('click', (e) => {
            e.stopPropagation();
            if (this._isVideoShowing()) this.core.togglePlay();
        });

        // 歌词页
        document.getElementById('btn-lyrics-back').addEventListener('click', () => this.lyrics.hide());
        document.getElementById('btn-lyrics-settings').addEventListener('click', () => {
            this.lyrics.hide();
            this._openSettings('歌词与播放设置');
        });
        const lyricsProgress = document.getElementById('lyrics-progress-bar');
        lyricsProgress.addEventListener('input', () => {
            this.core.seekTo(parseFloat(lyricsProgress.value));
        });

        // 歌单弹窗
        document.getElementById('btn-playlist-cancel').addEventListener('click', () => MPUtils.closeModal('modal-playlist'));
        document.getElementById('btn-playlist-cancel-2').addEventListener('click', () => MPUtils.closeModal('modal-playlist'));
        document.getElementById('btn-playlist-confirm').addEventListener('click', () => this._confirmPlaylistModal());
        document.getElementById('input-playlist-name').addEventListener('keydown', (e) => {
            if (e.key === 'Enter') this._confirmPlaylistModal();
        });

        // 加入歌单弹窗
        document.getElementById('btn-add-pl-close').addEventListener('click', () => MPUtils.closeModal('modal-add-to-playlist'));
        document.getElementById('btn-add-pl-new').addEventListener('click', () => {
            MPUtils.closeModal('modal-add-to-playlist');
            this._openPlaylistModal('create', null, true);
        });

        // EQ 预设命名弹窗
        document.getElementById('btn-eq-name-cancel').addEventListener('click', () => MPUtils.closeModal('modal-eq-name'));
        document.getElementById('btn-eq-name-cancel-2').addEventListener('click', () => MPUtils.closeModal('modal-eq-name'));
        document.getElementById('btn-eq-name-confirm').addEventListener('click', () => this._confirmEQName());
        document.getElementById('input-eq-name').addEventListener('keydown', (e) => {
            if (e.key === 'Enter') this._confirmEQName();
        });

        // 按在遮罩上关闭弹窗 / 面板（pointerdown：只看按下位置，拖到遮罩上松开不算）
        document.querySelectorAll('.mp-modal').forEach(modal => {
            modal.addEventListener('pointerdown', (e) => {
                if (e.target === modal) modal.classList.remove('active');
            });
        });

        document.addEventListener('click', (e) => this._onDocumentClick(e));
        document.addEventListener('fullscreenchange', () => {
            if (!document.fullscreenElement && this.isFullscreen) {
                this._exitFullscreen();
            }
        });

        this.updatePlayModeUI();
        this.updateVolumeUI();
        this._loadEQPresets();
        // 窗口尺寸变化会改变「播放栏顶边到视口底边」的距离，面板打开期间要跟着重算
        this._positionPops();
        window.addEventListener('resize', () => this._positionPops());
    },

    _onStageClick(e) {
        if (e.target.closest('button, input, select, .mp-playerbar')) return;
        if (this._isVideoShowing()) {
            // 未全屏：点击视频画面进入全屏；全屏中：点击切换播放/暂停
            if (this.isFullscreen) this.core.togglePlay();
            else this._enterFullscreen();
        }
    },

    _onDocumentClick(e) {
        const queue = document.getElementById('queue-popup');
        const queueBtn = document.getElementById('btn-queue');
        if (queue && !queue.classList.contains('hidden') && !this._isClickInside(e.target, queue, queueBtn)) {
            queue.classList.add('hidden');
        }
        const eq = document.getElementById('eq-panel');
        const eqBtn = document.getElementById('btn-eq');
        if (eq && !eq.classList.contains('hidden') && !this._isClickInside(e.target, eq, eqBtn)) {
            eq.classList.add('hidden');
        }
        this._closePlaylistMenu();
    },

    // 点击是否落在该面板内、或该面板的开关按钮内。
    // 判定按钮必须用 contains 而不是比较 target === 按钮：按钮里的图标是 <svg> 与它内部的
    // <use>，真实鼠标点在字形上时 target 是它们而不是按钮，比较相等会判成「点在面板外」，
    // 刚打开的面板会被同一轮事件立即关掉 —— 表现为「点图标打不开、点按钮边缘能开」的
    // 时好时坏（图标只占按钮中心约 15×15px，其余是内边距）。
    // 判据必须锚在「这一个面板」上：用 target.closest('.mp-pop') 会让另一个面板的点击
    // 也算「点在面板内」，点「播放队列」时就关不掉已打开的均衡器面板。
    _isClickInside(target, panel, toggleBtn) {
        return !!target && (panel.contains(target) || !!toggleBtn && toggleBtn.contains(target));
    },

    // ============================================================
    // 视频封面预取：按浏览位置自动在后台生成封面并写入 DB
    // ============================================================
    _bindThumbPrefetch() {
        if (!('IntersectionObserver' in window)) return;
        this._thumbPrefetchTimer = null;
        this._thumbPrefetchQueue = new Set();
        this._thumbObserver = new IntersectionObserver((entries) => {
            for (const entry of entries) {
                if (!entry.isIntersecting) continue;
                const img = entry.target;
                const id = img && img.dataset.mpThumbId;
                if (id && !this._thumbPrefetchSeen.has(id)) {
                    // 防止大媒体库长期浏览导致 Set 无限膨胀（仅影响去重，清空无害）
                    if (this._thumbPrefetchSeen.size > 3000) this._thumbPrefetchSeen.clear();
                    this._thumbPrefetchSeen.add(id);
                    this._thumbPrefetchQueue.add(id);
                }
            }
            // debounce 合并批量查询（避免滚动时高频单条请求）
            if (this._thumbPrefetchQueue.size) {
                clearTimeout(this._thumbPrefetchTimer);
                this._thumbPrefetchTimer = setTimeout(() => {
                    const ids = Array.from(this._thumbPrefetchQueue);
                    this._thumbPrefetchQueue.clear();
                    this._prefetchThumbs(ids);
                }, 250);
            }
        }, { root: null, rootMargin: '500px' });   // 视口 + 500px 预读（IO 自动计算裁剪链）
    },

    // 只对「未缓存」的封面抽帧（后端批量判断），避免重复生成
    async _prefetchThumbs(ids) {
        let missing = ids;
        try {
            const res = await Bridge.call('media_thumb_missing', ids);
            missing = (res && res.missing) || [];
        } catch (e) {
            missing = ids;   // 查询失败：直接全部尝试（request 有 pending 去重，不会重复抽）
        }
        missing.forEach((id) => {
            document.querySelectorAll(`img[data-mp-thumb-id="${id}"]`).forEach((img) => {
                if (img.isConnected) {
                    MediaFrameExtractor.request(id, img, () => {
                        // 抽帧失败：允许下次滚动/重进视口时重试（不永久放弃）
                        this._thumbPrefetchSeen.delete(id);
                        MPUtils.fallbackCover(img, 'icon:clapperboard');
                    });
                }
            });
        });
    },

    // 渲染后注册封面观察：IO 的初始回调会立即处理视口内元素（含 500px 预读边距）
    _observeThumbImgs(root) {
        if (!this._thumbObserver || !root) return;
        root.querySelectorAll('img[data-mp-thumb-id]').forEach(img => this._thumbObserver.observe(img));
    },

});
