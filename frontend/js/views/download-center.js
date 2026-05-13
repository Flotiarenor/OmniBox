// frontend/js/views/download-center.js

class DownloadCenter {
    constructor() {
        this.tasks = [];
        this.filter = 'all';
        this.pollTimer = null;
    }

    async init() {
        console.log('DownloadCenter 初始化...');
        this.bindEvents();
        await this.loadTasks();
        this.startPolling();
    }

    bindEvents() {
        // 添加任务按钮
        const addBtn = document.getElementById('dc-add-task');
        if (addBtn) {
            addBtn.addEventListener('click', () => this.showAddModal());
        }

        // 过滤器按钮
        document.querySelectorAll('.dc-filter-btn').forEach(btn => {
            btn.addEventListener('click', () => {
                document.querySelectorAll('.dc-filter-btn').forEach(b => b.classList.remove('active'));
                btn.classList.add('active');
                this.filter = btn.dataset.filter;
                this.render();
            });
        });

        // 添加任务模态框
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
            const result = await bridge.downloadList();
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
        this.pollTimer = setInterval(async () => {
            const hasActive = this.tasks.some(t => t.status === 'downloading');
            if (hasActive) {
                await this.loadTasks();
            }
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
        const speedText = totalSpeed > 0 ? this.formatSpeed(totalSpeed) : '0 B/s';

        document.getElementById('dc-stat-total').textContent = total;
        document.getElementById('dc-stat-active').textContent = active;
        document.getElementById('dc-stat-completed').textContent = completed;
        document.getElementById('dc-stat-speed').textContent = speedText;
        document.getElementById('dc-stat-queue').textContent = queued;
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

        // 缩略图
        const thumb = document.createElement('img');
        thumb.className = 'dc-task-thumb';
        thumb.src = task.thumbUrl || 'data:image/svg+xml,...';
        thumb.onerror = () => { thumb.src = 'data:image/svg+xml,...'; };

        // 信息区
        const info = document.createElement('div');
        info.className = 'dc-task-info';

        const title = document.createElement('div');
        title.className = 'dc-task-title';
        title.textContent = task.title || `漫画 #${task.albumId}`;

        const meta = document.createElement('div');
        meta.className = 'dc-task-meta';
        meta.innerHTML = `
            <span class="dc-status-badge ${task.status}">${this.getStatusText(task.status)}</span>
            <span>${task.completedImages || 0}/${task.totalImages || '?'} 页</span>
            ${task.speed ? `<span>${this.formatSpeed(task.speed)}</span>` : ''}
            ${task.eta ? `<span>剩余 ${this.formatTime(task.eta)}</span>` : ''}
        `;

        info.appendChild(title);
        info.appendChild(meta);

        // 进度条
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

        // 操作按钮（前端只做 UI 展示，不直接调用后端操作）
        const actions = document.createElement('div');
        actions.className = 'dc-task-actions';

        // 点击查看详情
        div.addEventListener('click', () => {
            this.showDetail(task);
        });

        // 组装
        div.appendChild(thumb);
        div.appendChild(info);
        div.appendChild(progressArea);
        div.appendChild(actions);

        return div;
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
            alert('请输入漫画 ID 或 URL');
            return;
        }

        try {
            await bridge.downloadSubmit(albumId);
            this.closeAddModal();
            await this.loadTasks();
        } catch (e) {
            alert('添加任务失败: ' + e.message);
        }
    }

    // ===== 任务详情 =====

    async showDetail(task) {
        try {
            // 从 album_info.json 获取详细信息
            const info = await bridge.downloadGetAlbumInfo(task.albumId);
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
                    <span class="value">${info.title || task.title || '未知'}</span>
                </div>
                <div class="dc-detail-row">
                    <span class="label">作者</span>
                    <span class="value">${info.author || '未知'}</span>
                </div>
                <div class="dc-detail-row">
                    <span class="label">标签</span>
                    <span class="value">${(info.tags || []).join(', ') || '无'}</span>
                </div>
                <div class="dc-detail-row">
                    <span class="label">状态</span>
                    <span class="value"><span class="dc-status-badge ${task.status}">${this.getStatusText(task.status)}</span></span>
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
                    <span class="value">${task.speed ? this.formatSpeed(task.speed) : '0 B/s'}</span>
                </div>
                <div class="dc-detail-row">
                    <span class="label">剩余时间</span>
                    <span class="value">${task.eta ? this.formatTime(task.eta) : '--'}</span>
                </div>
            </div>
            ${info.chapters && info.chapters.length > 0 ? `
            <div class="dc-detail-section">
                <h4>章节列表 (${info.chapters.length})</h4>
                <div class="dc-detail-chapter-list">
                    ${info.chapters.map(ch => `
                        <div class="dc-detail-chapter-item">
                            <span>${ch.title || `第 ${ch.chapter_id} 话`}</span>
                            <span>${ch.page_count || '?'} 页</span>
                        </div>
                    `).join('')}
                </div>
            </div>
            ` : ''}
        `;
    }

    // ===== 工具方法 =====

    getStatusText(status) {
        const map = {
            'downloading': '下载中',
            'paused': '已暂停',
            'completed': '已完成',
            'failed': '失败',
            'queued': '排队中'
        };
        return map[status] || status;
    }

    formatSpeed(bytesPerSecond) {
        if (!bytesPerSecond || bytesPerSecond === 0) return '0 B/s';
        const units = ['B/s', 'KB/s', 'MB/s', 'GB/s'];
        let i = 0;
        let speed = bytesPerSecond;
        while (speed >= 1024 && i < units.length - 1) {
            speed /= 1024;
            i++;
        }
        return `${speed.toFixed(1)} ${units[i]}`;
    }

    formatTime(seconds) {
        if (!seconds || seconds <= 0) return '--';
        if (seconds < 60) return `${Math.round(seconds)}秒`;
        if (seconds < 3600) return `${Math.floor(seconds / 60)}分${Math.round(seconds % 60)}秒`;
        const h = Math.floor(seconds / 3600);
        const m = Math.floor((seconds % 3600) / 60);
        return `${h}时${m}分`;
    }

    destroy() {
        this.stopPolling();
    }
}