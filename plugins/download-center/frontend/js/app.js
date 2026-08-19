class DownloadCenter {
    constructor() {
        this.tasks = [];
        this.filter = 'all';
        this.pollTimer = null;
    }

    async init() {
        this.bindEvents();
        await this.loadTasks();
        this.startPolling();
    }

    bindEvents() {
        const addBtn = document.getElementById('dc-add-task');
        if (addBtn) {
            addBtn.addEventListener('click', () => this.showAddModal());
        }

        const settingsBtn = document.getElementById('btn-settings');
        if (settingsBtn) {
            settingsBtn.addEventListener('click', () => {
                openSettingsModal({ title: '下载中心设置' });
            });
        }

        const startAllBtn = document.getElementById('dc-start-all');
        if (startAllBtn) {
            startAllBtn.addEventListener('click', async () => {
                try {
                    await Bridge.call('download_start_all');
                    await this.loadTasks();
                } catch (e) {
                    console.error('全部开始失败:', e);
                }
            });
        }

        const pauseAllBtn = document.getElementById('dc-pause-all');
        if (pauseAllBtn) {
            pauseAllBtn.addEventListener('click', async () => {
                try {
                    await Bridge.call('download_pause_all');
                    await this.loadTasks();
                } catch (e) {
                    console.error('全部暂停失败:', e);
                }
            });
        }

        const clearCompletedBtn = document.getElementById('dc-clear-completed');
        if (clearCompletedBtn) {
            clearCompletedBtn.addEventListener('click', async () => {
                try {
                    await Bridge.call('download_clear_completed');
                    await this.loadTasks();
                } catch (e) {
                    console.error('清除已完成失败:', e);
                }
            });
        }

        document.querySelectorAll('.dc-filter-btn').forEach(btn => {
            btn.addEventListener('click', () => {
                document.querySelectorAll('.dc-filter-btn').forEach(b => b.classList.remove('active'));
                btn.classList.add('active');
                this.filter = btn.dataset.filter;
                this.render();
            });
        });

        const confirmBtn = document.getElementById('dc-confirm-add');
        if (confirmBtn) {
            confirmBtn.addEventListener('click', () => this.confirmAdd());
        }

        const cancelBtn = document.getElementById('dc-cancel-add');
        if (cancelBtn) {
            cancelBtn.addEventListener('click', () => this.closeAddModal());
        }

        const closeDetailBtn = document.getElementById('dc-close-detail');
        if (closeDetailBtn) {
            closeDetailBtn.addEventListener('click', () => this.closeDetailModal());
        }
    }

    async loadTasks() {
        try {
            const result = await Bridge.call('download_list');
            this.tasks = result.tasks || [];
            this.updateStats();
            this.render();
        } catch (e) {
            console.error('加载任务列表失败:', e);
        }
    }

    startPolling() {
        if (this.pollTimer) {
            clearInterval(this.pollTimer);
        }
        this.pollTimer = setInterval(() => {
            // 始终刷新，避免最后一个活动任务完成后界面停在“下载中”。
            this.loadTasks();
        }, 2000);
    }

    stopPolling() {
        if (this.pollTimer) {
            clearInterval(this.pollTimer);
            this.pollTimer = null;
        }
    }

    updateStats() {
        const total = this.tasks.length;
        const active = this.tasks.filter(t => t.status === 'downloading').length;
        const completed = this.tasks.filter(t => t.status === 'completed').length;
        const queued = this.tasks.filter(t => t.status === 'queued').length;

        let totalSpeed = 0;
        this.tasks.forEach(t => {
            if (t.speed) totalSpeed += t.speed;
        });
        const speedText = totalSpeed > 0 ? DownloadUtils.formatSpeed(totalSpeed) : '0 B/s';

        const statsEls = {
            total: document.getElementById('dc-stat-total'),
            active: document.getElementById('dc-stat-active'),
            completed: document.getElementById('dc-stat-completed'),
            speed: document.getElementById('dc-stat-speed'),
            queue: document.getElementById('dc-stat-queue')
        };
        if (statsEls.total) statsEls.total.textContent = total;
        if (statsEls.active) statsEls.active.textContent = active;
        if (statsEls.completed) statsEls.completed.textContent = completed;
        if (statsEls.speed) statsEls.speed.textContent = speedText;
        if (statsEls.queue) statsEls.queue.textContent = queued;

        const sidebarStats = document.getElementById('dc-sidebar-stats');
        if (sidebarStats) {
            sidebarStats.textContent = `全部 ${total} · 下载中 ${active} · 已完成 ${completed} · 排队 ${queued}`;
        }
    }

    render() {
        const container = document.getElementById('dc-task-list');
        if (!container) return;

        const emptyState = document.getElementById('dc-empty-state');
        if (!emptyState) return;

        let filteredTasks = this.tasks;
        if (this.filter !== 'all') {
            filteredTasks = this.tasks.filter(t => t.status === this.filter);
        }

        if (filteredTasks.length === 0) {
            emptyState.style.display = 'flex';
            container.querySelectorAll('.dc-task-item').forEach(el => el.remove());
            return;
        }
        emptyState.style.display = 'none';

        container.querySelectorAll('.dc-task-item').forEach(el => el.remove());

        filteredTasks.forEach(task => {
            const item = this.createTaskItem(task);
            container.appendChild(item);
        });
    }

    createTaskItem(task) {
        const div = document.createElement('div');
        div.className = 'dc-task-item';
        div.dataset.taskId = task.id;

        const thumb = document.createElement('img');
        thumb.className = 'dc-task-thumb';
        thumb.src = task.thumbUrl || 'data:image/svg+xml,<svg xmlns="http://www.w3.org/2000/svg" width="48" height="64"/>';
        thumb.onerror = () => { thumb.src = 'data:image/svg+xml,<svg xmlns="http://www.w3.org/2000/svg" width="48" height="64"/>'; };

        const info = document.createElement('div');
        info.className = 'dc-task-info';

        const title = document.createElement('div');
        title.className = 'dc-task-title';
        title.textContent = task.title || `漫画 #${task.albumId}`;

        const meta = document.createElement('div');
        meta.className = 'dc-task-meta';
        meta.innerHTML = `
            <span class="dc-status-badge ${task.status}">${DownloadUtils.getStatusText(task.status)}</span>
            <span>${task.completedImages || 0}/${task.totalImages || '?'} 页</span>
            ${task.speed ? `<span>${DownloadUtils.formatSpeed(task.speed)}</span>` : ''}
            ${task.eta ? `<span>剩余 ${DownloadUtils.formatTime(task.eta)}</span>` : ''}
        `;

        info.appendChild(title);
        info.appendChild(meta);

        const progressArea = document.createElement('div');
        progressArea.className = 'dc-task-progress-area';

        const progressBar = document.createElement('div');
        progressBar.className = 'dc-task-progress-bar';

        const progressFill = document.createElement('div');
        progressFill.className = `dc-task-progress-fill ${task.status}`;
        const percent = task.totalImages > 0 ? Math.round((task.completedImages / task.totalImages) * 100) : 0;
        progressFill.style.width = `${percent}%`;
        progressBar.appendChild(progressFill);

        const progressText = document.createElement('div');
        progressText.className = 'dc-task-progress-text';
        progressText.textContent = `${percent}%`;

        progressArea.appendChild(progressBar);
        progressArea.appendChild(progressText);

        const actions = document.createElement('div');
        actions.className = 'dc-task-actions';
        actions.appendChild(this.createActionButtons(task));

        div.appendChild(thumb);
        div.appendChild(info);
        div.appendChild(progressArea);
        div.appendChild(actions);

        return div;
    }

    createActionButtons(task) {
        const wrapper = document.createElement('div');
        wrapper.style.display = 'flex';
        wrapper.style.gap = '4px';

        if (task.status === 'downloading' || task.status === 'queued') {
            const pauseBtn = this._iconButton('⏸', '暂停', () => this._pause(task));
            wrapper.appendChild(pauseBtn);
        } else if (task.status === 'paused') {
            const resumeBtn = this._iconButton('▶', '继续', () => this._resume(task));
            wrapper.appendChild(resumeBtn);
        } else if (task.status === 'failed') {
            const retryBtn = this._iconButton('↻', '重试', () => this._retry(task));
            wrapper.appendChild(retryBtn);
        }

        const detailBtn = this._iconButton('ℹ', '详情', () => this.showDetail(task));
        wrapper.appendChild(detailBtn);

        const deleteBtn = this._iconButton('🗑', '删除', () => this._delete(task), true);
        wrapper.appendChild(deleteBtn);

        return wrapper;
    }

    _iconButton(icon, title, onClick, danger = false) {
        const btn = document.createElement('button');
        btn.className = `btn-icon${danger ? ' danger' : ''}`;
        btn.title = title;
        btn.textContent = icon;
        btn.addEventListener('click', (e) => {
            e.stopPropagation();
            onClick();
        });
        return btn;
    }

    async _pause(task) {
        try {
            await Bridge.call('download_pause', task.id);
            await this.loadTasks();
        } catch (e) {
            console.error('暂停任务失败:', e);
        }
    }

    async _resume(task) {
        try {
            await Bridge.call('download_resume', task.id);
            await this.loadTasks();
        } catch (e) {
            console.error('恢复任务失败:', e);
        }
    }

    async _retry(task) {
        try {
            await Bridge.call('download_retry', task.id);
            await this.loadTasks();
        } catch (e) {
            console.error('重试任务失败:', e);
        }
    }

    async _delete(task) {
        const confirmed = await confirmDialog(`确定删除任务「${task.title || `漫画 #${task.albumId}`}」吗？`, { danger: true });
        if (!confirmed) return;
        try {
            await Bridge.call('download_delete', task.id);
            await this.loadTasks();
        } catch (e) {
            console.error('删除任务失败:', e);
        }
    }

    // ===== 添加任务 =====

    showAddModal() {
        const modal = document.getElementById('dc-add-modal');
        if (modal) {
            modal.classList.add('active');
            const input = document.getElementById('dc-input-id');
            if (input) {
                input.value = '';
                input.focus();
            }
        }
    }

    closeAddModal() {
        const modal = document.getElementById('dc-add-modal');
        if (modal) {
            modal.classList.remove('active');
        }
    }

    async confirmAdd() {
        const albumId = document.getElementById('dc-input-id')?.value.trim();
        if (!albumId) {
            Toast.warning('请输入漫画 ID 或 URL');
            return;
        }

        const concurrency = parseInt(document.getElementById('dc-input-concurrency')?.value || '3') || 3;
        const priority = document.getElementById('dc-input-priority')?.value || 'normal';
        const autoStart = document.getElementById('dc-input-auto-start')?.checked ?? true;

        try {
            await Bridge.call('download_submit', albumId, concurrency, priority, autoStart);
            this.closeAddModal();
            await this.loadTasks();
        } catch (e) {
            Toast.error('添加任务失败: ' + e.message);
        }
    }

    // ===== 任务详情 =====

    async showDetail(task) {
        try {
            const info = await Bridge.call('download_get_album_info', task.albumId);
            this.renderDetail(task, info);
            const modal = document.getElementById('dc-detail-modal');
            if (modal) {
                modal.classList.add('active');
            }
        } catch (e) {
            console.error('获取任务详情失败:', e);
        }
    }

    closeDetailModal() {
        const modal = document.getElementById('dc-detail-modal');
        if (modal) {
            modal.classList.remove('active');
        }
    }

    renderDetail(task, info) {
        const container = document.getElementById('dc-detail-content');
        if (!container) return;

        container.innerHTML = `
            <div class="dc-detail-section">
                <h4>基本信息</h4>
                <div class="dc-detail-row">
                    <span class="label">漫画 ID</span>
                    <span class="value">${task.albumId}</span>
                </div>
                <div class="dc-detail-row">
                    <span class="label">标题</span>
                    <span class="value">${Utils.escapeHtml(info.title || task.title || '未知')}</span>
                </div>
                <div class="dc-detail-row">
                    <span class="label">作者</span>
                    <span class="value">${Utils.escapeHtml(info.author || '未知')}</span>
                </div>
                <div class="dc-detail-row">
                    <span class="label">标签</span>
                    <span class="value">${Utils.escapeHtml((info.tags || []).join(', ')) || '无'}</span>
                </div>
                <div class="dc-detail-row">
                    <span class="label">状态</span>
                    <span class="value"><span class="dc-status-badge ${task.status}">${DownloadUtils.getStatusText(task.status)}</span></span>
                </div>
            </div>
            <div class="dc-detail-section">
                <h4>进度</h4>
                <div class="dc-detail-row">
                    <span class="label">总页数</span>
                    <span class="value">${task.completedImages || 0} / ${task.totalImages || info.total_page_count || '?'}</span>
                </div>
                <div class="dc-detail-row">
                    <span class="label">下载速度</span>
                    <span class="value">${task.speed ? DownloadUtils.formatSpeed(task.speed) : '0 B/s'}</span>
                </div>
                <div class="dc-detail-row">
                    <span class="label">剩余时间</span>
                    <span class="value">${task.eta ? DownloadUtils.formatTime(task.eta) : '--'}</span>
                </div>
            </div>
            ${info.chapters && info.chapters.length > 0 ? `
            <div class="dc-detail-section">
                <h4>章节列表 (${info.chapters.length})</h4>
                <div class="dc-detail-chapter-list">
                    ${info.chapters.map(ch => `
                        <div class="dc-detail-chapter-item">
                            <span>${Utils.escapeHtml(ch.title || `第 ${ch.chapter_id} 话`)}</span>
                            <span>${ch.page_count || '?'} 页</span>
                        </div>
                    `).join('')}
                </div>
            </div>
            ` : ''}
        `;
    }

    destroy() {
        this.stopPolling();
    }
}
