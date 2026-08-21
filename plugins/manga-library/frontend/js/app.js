// ============================================================
// 漫画中心：漫画库 + 下载中心（合并插件）
// ============================================================
class MangaLibraryApp {
    constructor() {
        this.currentView = 'all';
        this.downloadFilter = 'all';
        this.tasks = [];
        this.pollTimer = null;
        this.reader = null;

        this.currentFolderName = '';
        this.currentDetail = null;
        this.isMultiChapter = false;
        this.viewLevel = 'home'; // home / chapters / images
        this._initialized = false;
    }

    async init() {
        if (this._initialized) return;
        this._initialized = true;

        this.reader = new MangaReader();
        this._bindUI();
        await this.loadView();
        await this.loadTasks();
        this.pollTimer = setInterval(() => this.loadTasks(), 2000);
    }

    // ============================================================
    // UI 绑定
    // ============================================================
    _bindUI() {
        document.querySelectorAll('.ml-nav-item').forEach(btn => {
            btn.addEventListener('click', () => {
                this.switchView(btn.dataset.view, btn.dataset.dlFilter || 'all');
            });
        });

        document.getElementById('btn-settings').addEventListener('click', () => {
            openSettingsModal({ title: '漫画中心设置' });
        });

        const search = document.getElementById('manga-search');
        const debounced = Utils.debounce(() => this.loadView(), 300);
        search.addEventListener('input', () => {
            document.getElementById('ml-search-clear').classList.toggle('hidden', !search.value.trim());
            debounced();
        });
        document.getElementById('ml-search-clear').addEventListener('click', () => {
            search.value = '';
            document.getElementById('ml-search-clear').classList.add('hidden');
            this.loadView();
            search.focus();
        });

        // 下载任务工具栏
        document.getElementById('ml-add-task').addEventListener('click', () => this.showAddModal());
        document.getElementById('ml-start-all').addEventListener('click', async () => {
            await Bridge.call('download_start_all');
            await this.loadTasks();
        });
        document.getElementById('ml-pause-all').addEventListener('click', async () => {
            await Bridge.call('download_pause_all');
            await this.loadTasks();
        });
        document.getElementById('ml-clear-completed').addEventListener('click', async () => {
            await Bridge.call('download_clear_completed');
            await this.loadTasks();
        });

        // 添加任务弹窗
        document.getElementById('ml-cancel-add').addEventListener('click', () => this.closeAddModal());
        document.getElementById('ml-confirm-add').addEventListener('click', () => this.confirmAdd());
        document.getElementById('ml-close-detail').addEventListener('click', () => this.closeDetailModal());

        document.querySelectorAll('.modal').forEach(modal => {
            modal.addEventListener('click', (e) => {
                if (e.target === modal) modal.classList.remove('active');
            });
        });
    }

    switchView(view, dlFilter = 'all') {
        this.currentView = view;
        this.downloadFilter = dlFilter;
        this.viewLevel = 'home';
        this.currentDetail = null;
        document.querySelectorAll('.ml-nav-item').forEach(b => {
            const active = b.dataset.view === view && (view !== 'downloads' || (b.dataset.dlFilter || 'all') === dlFilter);
            b.classList.toggle('active', active);
        });
        document.getElementById('manga-search').value = '';
        document.getElementById('ml-search-clear').classList.add('hidden');
        this.loadView();
    }

    // ============================================================
    // 视图渲染
    // ============================================================
    async loadView() {
        const content = document.getElementById('ml-content');
        const title = document.getElementById('ml-view-title');
        const sub = document.getElementById('ml-view-sub');
        const isDownloads = this.currentView === 'downloads';
        const keyword = (document.getElementById('manga-search').value || '').trim();

        // 工具栏按视图切换
        document.getElementById('ml-search-wrap').classList.toggle('hidden', isDownloads);
        document.getElementById('ml-add-task').classList.toggle('hidden', !isDownloads);
        document.getElementById('ml-start-all').classList.toggle('hidden', !isDownloads);
        document.getElementById('ml-pause-all').classList.toggle('hidden', !isDownloads);
        document.getElementById('ml-clear-completed').classList.toggle('hidden', !isDownloads);

        if (isDownloads) {
            title.textContent = '下载中心';
            sub.textContent = '漫画下载任务管理';
            this.renderDownloads();
            return;
        }

        try {
            let items = [];
            if (keyword) {
                items = await Bridge.call('manga_search', keyword);
                title.textContent = `搜索 “${keyword}”`;
                sub.textContent = `${items.length} 个结果`;
                this._renderGrid(content, items, { single: true });
                return;
            }

            if (this.viewLevel === 'chapters') {
                this.renderChapters(content);
                return;
            }
            if (this.viewLevel === 'images') {
                await this.renderImages(content);
                return;
            }

            const state = await Bridge.call('manga_get_state');
            if (this.currentView === 'all') {
                title.textContent = '全部漫画';
                sub.textContent = '按文件夹扫描';
                this._renderGrid(content, await Bridge.call('manga_list'), { single: true });
            } else if (this.currentView === 'favorites') {
                title.textContent = '我的收藏';
                sub.textContent = `${state.favorites.length} 部`;
                this._renderGrid(content, state.favorites, { single: true });
            } else if (this.currentView === 'recent') {
                title.textContent = '最近阅读';
                sub.textContent = `${state.recent.length} 部`;
                this._renderGrid(content, state.recent, { single: true });
            }
        } catch (e) {
            console.error('加载视图失败:', e);
            content.innerHTML = this._emptyHtml('⚠️', '加载失败', String(e && e.message || e));
        }
    }

    _emptyHtml(icon, text, hint) {
        return `<div class="ml-empty">
            <div class="ml-empty-icon">${icon}</div>
            <div class="ml-empty-text">${MangaUtils.escapeHtml(text)}</div>
            ${hint ? `<div class="ml-empty-hint">${MangaUtils.escapeHtml(hint)}</div>` : ''}
        </div>`;
    }

    _renderGrid(container, items, opts = {}) {
        if (!container) return;
        if (!items || !items.length) {
            container.innerHTML = this._emptyHtml('📚', '暂无漫画', opts.single ? '可在设置中调整漫画根目录' : '');
            return;
        }
        // 单视图容器不是网格时，内部包一层 .ml-grid，避免卡片撑满整行
        const target = opts.single ? this._ensureGrid(container) : container;
        target.innerHTML = items.map((manga, i) => {
            const cover = manga.cover_url
                ? MangaUtils.coverImg(Bridge.originalUrl(manga.cover_url))
                : `<div class="ml-cover-fallback">📚</div>`;
            return `
            <div class="ml-card" data-folder="${MangaUtils.escapeHtml(manga.folder_name)}" style="--obx-i:${Math.min(i, 32)}">
                <button class="ml-fav-star ${manga.is_fav ? 'active' : ''}" data-folder="${MangaUtils.escapeHtml(manga.folder_name)}" title="收藏">${manga.is_fav ? '★' : '☆'}</button>
                <div class="ml-cover">${cover}<span class="ml-badge">${manga.page_count}页</span></div>
                <div class="ml-info">
                    <div class="ml-card-title">${MangaUtils.escapeHtml(manga.title)}</div>
                    <div class="ml-card-sub">${MangaUtils.escapeHtml(manga.author || '')}</div>
                </div>
            </div>`;
        }).join('');

        target.querySelectorAll('.ml-card').forEach(card => {
            card.addEventListener('click', (e) => {
                if (e.target.closest('.ml-fav-star')) return;
                this.openMangaDetail(card.dataset.folder);
            });
        });
        target.querySelectorAll('.ml-fav-star').forEach(star => {
            star.addEventListener('click', async (e) => {
                e.stopPropagation();
                await this.toggleFav(star.dataset.folder);
            });
        });
    }

    _ensureGrid(container) {
        let grid = container.querySelector(':scope > .ml-grid');
        if (!grid) {
            container.innerHTML = '';
            grid = document.createElement('div');
            grid.className = 'ml-grid';
            container.appendChild(grid);
        }
        return grid;
    }

    async toggleFav(folderName) {
        try {
            const isFav = await Bridge.call('manga_toggle_favorite', folderName);
            document.querySelectorAll('.ml-fav-star').forEach(el => {
                if (el.dataset.folder === folderName) {
                    el.classList.toggle('active', isFav);
                    el.textContent = isFav ? '★' : '☆';
                    Motion.retrigger(el, 'obx-anim-heart');
                }
            });
            if (this.currentView === 'favorites' || this.currentView === 'recent') this.loadView();
        } catch (e) {
            Toast.error('收藏操作失败');
        }
    }

    // ============================================================
    // 详情 / 章节 / 图片
    // ============================================================
    async openMangaDetail(folderName) {
        try {
            const detail = await Bridge.call('manga_get_detail', folderName);
            if (!detail.folder_name) return;
            this.currentFolderName = folderName;
            this.currentDetail = detail;
            this.isMultiChapter = detail.is_multi_chapter;
            this._chapterPath = '';
            this.viewLevel = this.isMultiChapter ? 'chapters' : 'images';

            document.getElementById('ml-view-title').textContent = detail.title;
            document.getElementById('ml-view-sub').textContent = detail.author || '';
            await this.loadView();
        } catch (e) {
            console.error('打开漫画详情失败', e);
        }
    }

    renderChapters(content) {
        const detail = this.currentDetail;
        const infoHtml = this._detailInfoHtml(detail.info);
        content.innerHTML = `
            <div class="ml-detail-hero" style="--ml-hero-bg:${detail.chapters[0] && detail.chapters[0].cover_url ? `url("${Bridge.originalUrl(detail.chapters[0].cover_url)}")` : 'none'}">
                <button class="btn ml-hero-back" id="ml-detail-back">← 返回</button>
                <div class="ml-detail-cover">${detail.chapters[0] && detail.chapters[0].cover_url ? MangaUtils.coverImg(Bridge.originalUrl(detail.chapters[0].cover_url)) : '📚'}</div>
                <div class="ml-detail-info">
                    <div class="ml-detail-label">漫画详情</div>
                    <div class="ml-detail-title">${MangaUtils.escapeHtml(detail.title)}</div>
                    <div class="ml-detail-author">${MangaUtils.escapeHtml(detail.author)}</div>
                </div>
                <div class="ml-detail-actions">
                    <button class="btn ${detail.is_fav ? 'btn-primary' : ''}" id="ml-detail-fav">${detail.is_fav ? '★ 已收藏' : '☆ 收藏'}</button>
                </div>
            </div>
            ${infoHtml}
            <section class="ml-section">
                <h3 class="ml-section-title">📖 章节</h3>
                <div id="ml-chapters" class="ml-grid"></div>
            </section>`;

        document.getElementById('ml-detail-back').addEventListener('click', () => this.switchView('all'));
        document.getElementById('ml-detail-fav').addEventListener('click', async (e) => {
            const btn = e.target;
            const isFav = await Bridge.call('manga_toggle_favorite', this.currentFolderName);
            btn.classList.toggle('btn-primary', isFav);
            btn.textContent = isFav ? '★ 已收藏' : '☆ 收藏';
        });

        const grid = document.getElementById('ml-chapters');
        grid.innerHTML = detail.chapters.map((ch, i) => `
            <div class="ml-card" data-chapter="${MangaUtils.escapeHtml(ch.path)}" style="--obx-i:${Math.min(i, 32)}">
                <div class="ml-cover">${ch.cover_url ? MangaUtils.coverImg(Bridge.originalUrl(ch.cover_url)) : `<div class="ml-cover-fallback">📖</div>`}</div>
                <div class="ml-info"><div class="ml-card-title">${MangaUtils.escapeHtml(ch.name)}</div></div>
            </div>`).join('');
        grid.querySelectorAll('.ml-card').forEach(card => {
            card.addEventListener('click', async () => {
                this.viewLevel = 'images';
                this._chapterPath = card.dataset.chapter;
                await this.loadView();
            });
        });
    }

    async renderImages(content) {
        const detail = this.currentDetail;
        content.innerHTML = `
            <div class="ml-detail-hero">
                <button class="btn ml-hero-back" id="ml-detail-back">← 返回</button>
                <div class="ml-detail-info">
                    <div class="ml-detail-label">阅读</div>
                    <div class="ml-detail-title">${MangaUtils.escapeHtml(detail.title)}</div>
                    <div class="ml-detail-author">${MangaUtils.escapeHtml(this._chapterPath ? this.currentDetail.chapters.find(c => c.path === this._chapterPath)?.name || '' : '')}</div>
                </div>
            </div>
            <section class="ml-section">
                <h3 class="ml-section-title">🖼 图片</h3>
                <div id="ml-pages" class="ml-grid"></div>
            </section>`;
        document.getElementById('ml-detail-back').addEventListener('click', () => {
            if (this.isMultiChapter) {
                this.viewLevel = 'chapters';
                this.loadView();
            } else {
                this.switchView('all');
            }
        });

        const grid = document.getElementById('ml-pages');
        grid.innerHTML = '<div class="ml-empty"><div class="obx-anim-spin" style="width:32px;height:32px;border-radius:50%;border:3px solid var(--ml-accent-soft);border-top-color:var(--accent);"></div><div>图片加载中…</div></div>';
        try {
            const pages = await Bridge.call('manga_get_pages', this.currentFolderName, this._chapterPath || '');
            if (!pages.length) {
                grid.innerHTML = this._emptyHtml('🖼', '无图片');
                return;
            }
            Bridge.call('manga_update_recent', this.currentFolderName, 0);
            grid.innerHTML = pages.map((url, i) => `
                <div class="ml-card" data-page="${i}" style="--obx-i:${Math.min(i, 32)}">
                    <div class="ml-cover" style="padding-top:140%;">${MangaUtils.coverImg(Bridge.originalUrl(url), '🖼')}</div>
                </div>`).join('');
            grid.querySelectorAll('.ml-card').forEach(card => {
                card.addEventListener('click', () => this.reader.open(pages, parseInt(card.dataset.page, 10)));
            });
        } catch (e) {
            grid.innerHTML = this._emptyHtml('⚠️', '加载失败');
        }
    }

    _detailInfoHtml(info) {
        if (!info || !Object.keys(info).length) return '';
        const rows = [];
        const add = (label, value) => {
            if (value !== undefined && value !== null && value !== '') {
                rows.push(`<span class="ml-info-item">${MangaUtils.escapeHtml(label)}: ${MangaUtils.escapeHtml(value)}</span>`);
            }
        };
        add('ID', info.album_id);
        add('原名', info.oname);
        add('下载时间', info.download_time);
        if (Array.isArray(info.actors) && info.actors.length) add('演员', info.actors.join(', '));
        const tags = (info.tags || []).map(t => `<span class="ml-tag">${MangaUtils.escapeHtml(t)}</span>`).join('');
        return `<div class="ml-info-box">${rows.join('')}${tags ? `<div style="margin-top:8px;">${tags}</div>` : ''}</div>`;
    }

    // ============================================================
    // 下载中心
    // ============================================================
    async loadTasks() {
        try {
            const result = await Bridge.call('download_list');
            this.tasks = result.tasks || [];
            this.updateStats();
            if (this.currentView === 'downloads') this.renderDownloads();
        } catch (e) {
            console.error('加载下载任务失败:', e);
        }
    }

    updateStats() {
        const total = this.tasks.length;
        const active = this.tasks.filter(t => t.status === 'downloading').length;
        const completed = this.tasks.filter(t => t.status === 'completed').length;
        const queued = this.tasks.filter(t => t.status === 'queued').length;
        const stats = document.getElementById('ml-stats');
        if (stats) stats.textContent = `任务 ${total} · 下载中 ${active} · 完成 ${completed} · 排队 ${queued}`;
        const sub = document.getElementById('ml-view-sub');
        if (this.currentView === 'downloads' && sub) sub.textContent = `全部 ${total} · 下载中 ${active} · 已完成 ${completed} · 排队 ${queued}`;
    }

    renderDownloads() {
        const content = document.getElementById('ml-content');
        if (!content) return;
        const filtered = this.downloadFilter === 'all'
            ? this.tasks
            : this.tasks.filter(t => t.status === this.downloadFilter);

        if (!filtered.length) {
            content.innerHTML = this._emptyHtml('⬇️', '暂无下载任务', '点击右上角「添加任务」开始下载漫画');
            return;
        }

        content.innerHTML = filtered.map((task, i) => {
            const percent = task.totalImages > 0 ? Math.round((task.completedImages / task.totalImages) * 100) : 0;
            const thumb = task.thumbUrl || '';
            return `
            <div class="ml-task" data-task-id="${task.id}" style="--obx-i:${Math.min(i, 24)}">
                ${thumb ? `<img class="ml-task-thumb" src="${thumb}" alt="" onerror="this.style.display='none'">` : `<div class="ml-task-thumb" style="display:flex;align-items:center;justify-content:center;font-size:22px;">📚</div>`}
                <div class="ml-task-main">
                    <div class="ml-task-title">${MangaUtils.escapeHtml(task.title || `漫画 #${task.albumId}`)}</div>
                    <div class="ml-task-meta">
                        <span class="ml-status ${task.status}">${DownloadUtils.getStatusText(task.status)}</span>
                        <span>${task.completedImages || 0}/${task.totalImages || '?'} 页</span>
                        ${task.speed ? `<span>${DownloadUtils.formatSpeed(task.speed)}</span>` : ''}
                        ${task.eta ? `<span>剩余 ${DownloadUtils.formatTime(task.eta)}</span>` : ''}
                    </div>
                </div>
                <div class="ml-task-progress">
                    <div class="ml-progress-track"><div class="ml-progress-fill" style="width:${percent}%"></div></div>
                    <div class="ml-task-percent">${percent}%</div>
                </div>
                <div class="ml-task-actions">
                    ${task.status === 'downloading' || task.status === 'queued' ? `<button data-act="pause" title="暂停">⏸</button>` : ''}
                    ${task.status === 'paused' ? `<button data-act="resume" title="继续">▶</button>` : ''}
                    ${task.status === 'failed' ? `<button data-act="retry" title="重试">↻</button>` : ''}
                    <button data-act="detail" title="详情">ℹ</button>
                    <button data-act="delete" class="danger" title="删除">🗑</button>
                </div>
            </div>`;
        }).join('');

        content.querySelectorAll('.ml-task-actions button').forEach(btn => {
            btn.addEventListener('click', async (e) => {
                e.stopPropagation();
                const taskId = btn.closest('.ml-task').dataset.taskId;
                const act = btn.dataset.act;
                if (act === 'pause') await Bridge.call('download_pause', taskId);
                else if (act === 'resume') await Bridge.call('download_resume', taskId);
                else if (act === 'retry') await Bridge.call('download_retry', taskId);
                else if (act === 'detail') { await this.showTaskDetail(taskId); return; }
                else if (act === 'delete') {
                    const task = this.tasks.find(t => t.id === taskId);
                    const ok = await confirmDialog(`确定删除任务「${task ? task.title : taskId}」？`, { danger: true });
                    if (!ok) return;
                    await Bridge.call('download_delete', taskId);
                }
                await this.loadTasks();
            });
        });
    }

    // ===== 添加 / 详情弹窗 =====
    showAddModal() {
        const modal = document.getElementById('ml-add-modal');
        modal.classList.add('active');
        document.getElementById('ml-input-id').value = '';
        setTimeout(() => document.getElementById('ml-input-id').focus(), 40);
    }

    closeAddModal() {
        document.getElementById('ml-add-modal').classList.remove('active');
    }

    async confirmAdd() {
        const albumId = document.getElementById('ml-input-id').value.trim();
        if (!albumId) {
            Toast.warning('请输入漫画 ID 或 URL');
            return;
        }
        const concurrency = parseInt(document.getElementById('ml-input-concurrency').value || '3', 10) || 3;
        const priority = document.getElementById('ml-input-priority').value || 'normal';
        const autoStart = document.getElementById('ml-input-auto-start').checked;
        try {
            await Bridge.call('download_submit', albumId, concurrency, priority, autoStart);
            this.closeAddModal();
            await this.loadTasks();
            Toast.success('任务已添加');
        } catch (e) {
            Toast.error('添加任务失败: ' + e.message);
        }
    }

    async showTaskDetail(taskId) {
        const task = this.tasks.find(t => t.id === taskId);
        if (!task) return;
        try {
            const info = await Bridge.call('download_get_album_info', task.albumId);
            const container = document.getElementById('ml-detail-content');
            container.innerHTML = `
                <div class="ml-field"><label>标题</label><div>${MangaUtils.escapeHtml(info.title || task.title || '未知')}</div></div>
                <div class="ml-field"><label>作者</label><div>${MangaUtils.escapeHtml(info.author || '未知')}</div></div>
                <div class="ml-field"><label>漫画 ID</label><div>${MangaUtils.escapeHtml(task.albumId)}</div></div>
                <div class="ml-field"><label>状态</label><div><span class="ml-status ${task.status}">${DownloadUtils.getStatusText(task.status)}</span></div></div>
                <div class="ml-field"><label>进度</label><div>${task.completedImages || 0} / ${task.totalImages || info.total_page_count || '?'} 页</div></div>
                <div class="ml-field"><label>下载速度</label><div>${task.speed ? DownloadUtils.formatSpeed(task.speed) : '0 B/s'}</div></div>
                <div class="ml-field"><label>剩余时间</label><div>${task.eta ? DownloadUtils.formatTime(task.eta) : '--'}</div></div>
                ${info.error ? `<div class="ml-field"><label>错误</label><div style="color:var(--danger);">${MangaUtils.escapeHtml(info.error)}</div></div>` : ''}`;
            document.getElementById('ml-detail-modal').classList.add('active');
        } catch (e) {
            Toast.error('获取任务详情失败');
        }
    }

    closeDetailModal() {
        document.getElementById('ml-detail-modal').classList.remove('active');
    }
}
