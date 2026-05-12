// frontend/js/views/download-center.js

class DownloadCenter {
    constructor() {
        this.tasks = [];
        this.filter = 'all';
        this.selectedIds = new Set();
        this.pollTimer = null;
        this.isMultiSelect = false;
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

        // 全部开始
        const startAllBtn = document.getElementById('dc-start-all');
        if (startAllBtn) {
            startAllBtn.addEventListener('click', () => this.startAll());
        }

        // 全部暂停
        const pauseAllBtn = document.getElementById('dc-pause-all');
        if (pauseAllBtn) {
            pauseAllBtn.addEventListener('click', () => this.pauseAll());
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

        // 添加任务模态框的确认按钮
        const confirmBtn = document.getElementById('dc-confirm-add');
        if (confirmBtn) {
            confirmBtn.addEventListener('click', () => this.confirmAdd());
        }

        // 添加任务模态框的取消按钮
        const cancelBtn = document.getElementById('dc-cancel-add');
        if (cancelBtn) {
            cancelBtn.addEventListener('click', () => this.closeAddModal());
        }

        // 浏览目录按钮
        const browseBtn = document.getElementById('dc-browse-dir');
        if (browseBtn) {
            browseBtn.addEventListener('click', () => this.browseDir());
        }

        // 关闭详情模态框
        const closeDetailBtn = document.getElementById('dc-close-detail');
        if (closeDetailBtn) {
            closeDetailBtn.addEventListener('click', () => this.closeDetailModal());
        }

        // 清除已完成任务
        const clearBtn = document.getElementById('dc-clear-completed');
        if (clearBtn) {
            clearBtn.addEventListener('click', () => this.clearCompleted());
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
        // 每2秒轮询一次活跃任务的状态
        if (this.pollTimer) {
            clearInterval(this.pollTimer);
        }
        this.pollTimer = setInterval(async () => {
            const hasActive = this.tasks.some(t => 
                t.status === 'downloading' || t.status === 'queued'
            );
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

        // 计算总速度
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

        // 过滤任务
        let filteredTasks = this.tasks;
        if (this.filter !== 'all') {
            filteredTasks = this.tasks.filter(t => t.status === this.filter);
        }

        // 显示空状态
        if (filteredTasks.length === 0) {
            emptyState.style.display = 'flex';
            // 移除所有任务项
            container.querySelectorAll('.dc-task-item').forEach(el => el.remove());
            return;
        }
        emptyState.style.display = 'none';

        // 移除旧的任务项
        container.querySelectorAll('.dc-task-item').forEach(el => el.remove());

        // 渲染任务项
        filteredTasks.forEach(task => {
            const item = this.createTaskItem(task);
            container.appendChild(item);
        });
    }

    createTaskItem(task) {
        const div = document.createElement('div');
        div.className = 'dc-task-item';
        div.dataset.taskId = task.id;
        if (this.selectedIds.has(task.id)) {
            div.classList.add('selected');
        }

        // 复选框
        const checkbox = document.createElement('input');
        checkbox.type = 'checkbox';
        checkbox.className = 'dc-task-checkbox';
        checkbox.checked = this.selectedIds.has(task.id);
        checkbox.addEventListener('change', (e) => {
            e.stopPropagation();
            if (checkbox.checked) {
                this.selectedIds.add(task.id);
            } else {
                this.selectedIds.delete(task.id);
            }
            div.classList.toggle('selected', checkbox.checked);
        });

        // 缩略图
        const thumb = document.createElement('img');
        thumb.className = 'dc-task-thumb';
        thumb.src = task.thumbUrl || 'data:image/svg+xml,<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 48 64"><rect fill="%23eee" width="48" height="64"/><text x="24" y="32" text-anchor="middle" fill="%23999" font-size="20">📚</text></svg>';
        thumb.onerror = () => { 
            thumb.src = 'data:image/svg+xml,<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 48 64"><rect fill="%23eee" width="48" height="64"/><text x="24" y="32" text-anchor="middle" fill="%23999" font-size="20">📚</text></svg>'; 
        };

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

        // 进度条区域
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

        // 操作按钮
        const actions = document.createElement('div');
        actions.className = 'dc-task-actions';

        if (task.status === 'downloading' || task.status === 'queued') {
            const pauseBtn = document.createElement('button');
            pauseBtn.className = 'btn-icon';
            pauseBtn.textContent = '⏸';
            pauseBtn.title = '暂停';
            pauseBtn.addEventListener('click', (e) => {
                e.stopPropagation();
                this.pauseTask(task.id);
            });
            actions.appendChild(pauseBtn);
        } else if (task.status === 'paused') {
            const resumeBtn = document.createElement('button');
            resumeBtn.className = 'btn-icon';
            resumeBtn.textContent = '▶';
            resumeBtn.title = '继续';
            resumeBtn.addEventListener('click', (e) => {
                e.stopPropagation();
                this.resumeTask(task.id);
            });
            actions.appendChild(resumeBtn);
        } else if (task.status === 'failed') {
            const retryBtn = document.createElement('button');
            retryBtn.className = 'btn-icon';
            retryBtn.textContent = '🔄';
            retryBtn.title = '重试';
            retryBtn.addEventListener('click', (e) => {
                e.stopPropagation();
                this.retryTask(task.id);
            });
            actions.appendChild(retryBtn);
        }

        const deleteBtn = document.createElement('button');
        deleteBtn.className = 'btn-icon danger';
        deleteBtn.textContent = '🗑';
        deleteBtn.title = '删除';
        deleteBtn.addEventListener('click', (e) => {
            e.stopPropagation();
            this.deleteTask(task.id);
        });
        actions.appendChild(deleteBtn);

        // 组装
        div.appendChild(checkbox);
        div.appendChild(thumb);
        div.appendChild(info);
        div.appendChild(progressArea);
        div.appendChild(actions);

        // 点击查看详情
        div.addEventListener('click', () => {
            if (!this.isMultiSelect) {
                this.showDetail(task.id);
            } else {
                checkbox.checked = !checkbox.checked;
                checkbox.dispatchEvent(new Event('change'));
            }
        });

        return div;
    }

    // ===== 任务操作 =====

    async startAll() {
        try {
            await bridge.downloadStartAll();
            await this.loadTasks();
        } catch (e) {
            console.error('全部开始失败:', e);
        }
    }

    async pauseAll() {
        try {
            await bridge.downloadPauseAll();
            await this.loadTasks();
        } catch (e) {
            console.error('全部暂停失败:', e);
        }
    }

    async pauseTask(taskId) {
        try {
            await bridge.downloadPause(taskId);
            await this.loadTasks();
        } catch (e) {
            console.error('暂停任务失败:', e);
        }
    }

    async resumeTask(taskId) {
        try {
            await bridge.downloadResume(taskId);
            await this.loadTasks();
        } catch (e) {
            console.error('恢复任务失败:', e);
        }
    }

    async retryTask(taskId) {
        try {
            await bridge.downloadRetry(taskId);
            await this.loadTasks();
        } catch (e) {
            console.error('重试任务失败:', e);
        }
    }

    async deleteTask(taskId) {
        if (!confirm('确定要删除这个下载任务吗？')) return;
        try {
            await bridge.downloadDelete(taskId);
            this.selectedIds.delete(taskId);
            await this.loadTasks();
        } catch (e) {
            console.error('删除任务失败:', e);
        }
    }

    async clearCompleted() {
        if (!confirm('确定要清除所有已完成的任务吗？')) return;
        try {
            await bridge.downloadClearCompleted();
            await this.loadTasks();
        } catch (e) {
            console.error('清除已完成任务失败:', e);
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
        const downloadDir = document.getElementById('dc-input-dir')?.value.trim();
        const concurrency = parseInt(document.getElementById('dc-input-concurrency')?.value) || 3;
        const priority = document.getElementById('dc-input-priority')?.value || 'normal';
        const autoStart = document.getElementById('dc-input-auto-start')?.checked ?? true;

        if (!albumId) {
            alert('请输入漫画 ID 或 URL');
            return;
        }

        try {
            await bridge.downloadAdd(albumId, downloadDir, concurrency, priority, autoStart);
            this.closeAddModal();
            await this.loadTasks();
        } catch (e) {
            alert('添加任务失败: ' + e.message);
        }
    }

    async browseDir() {
        try {
            const dir = await bridge.dialogSelectDirectory();
            if (dir) {
                const input = document.getElementById('dc-input-dir');
                if (input) {
                    input.value = dir;
                }
            }
        } catch (e) {
            console.error('选择目录失败:', e);
        }
    }

    // ===== 任务详情 =====

    async showDetail(taskId) {
        try {
            const detail = await bridge.downloadDetail(taskId);
            this.renderDetail(detail);
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

    renderDetail(detail) {
        const container = document.getElementById('dc-detail-content');
        if (!container) return;

        let chaptersHtml = '';
        if (detail.chapters && detail.chapters.length > 0) {
            chaptersHtml = detail.chapters.map(ch => `
                <div class="dc-detail-chapter-item">
                    <span>${ch.title || `第 ${ch.index} 话`}</span>
                    <span class="chapter-status ${ch.status}">
                        ${ch.completedImages || 0}/${ch.totalImages || '?'} 页
                        ${ch.status === 'completed' ? '✅' : ch.status === 'downloading' ? '⏳' : '⏸'}
                    </span>
                </div>
            `).join('');
        }

        container.innerHTML = `
            <div class="dc-detail-section">
                <h4>基本信息</h4>
                <div class="dc-detail-row">
                    <span class="label">漫画 ID</span>
                    <span class="value">${detail.albumId || '未知'}</span>
                </div>
                <div class="dc-detail-row">
                    <span class="label">标题</span>
                    <span class="value">${detail.title || '未知'}</span>
                </div>
                <div class="dc-detail-row">
                    <span class="label">状态</span>
                    <span class="value"><span class="dc-status-badge ${detail.status}">${this.getStatusText(detail.status)}</span></span>
                </div>
                <div class="dc-detail-row">
                    <span class="label">下载目录</span>
                    <span class="value" style="font-size:12px;">${detail.downloadDir || '默认'}</span>
                </div>
            </div>
            <div class="dc-detail-section">
                <h4>进度</h4>
                <div class="dc-detail-row">
                    <span class="label">总页数</span>
                    <span class="value">${detail.completedImages || 0} / ${detail.totalImages || '?'}</span>
                </div>
                <div class="dc-detail-row">
                    <span class="label">下载速度</span>
                    <span class="value">${detail.speed ? this.formatSpeed(detail.speed) : '0 B/s'}</span>
                </div>
                <div class="dc-detail-row">
                    <span class="label">剩余时间</span>
                    <span class="value">${detail.eta ? this.formatTime(detail.eta) : '--'}</span>
                </div>
                <div class="dc-detail-row">
                    <span class="label">开始时间</span>
                    <span class="value">${detail.startTime || '--'}</span>
                </div>
                ${detail.completeTime ? `
                <div class="dc-detail-row">
                    <span class="label">完成时间</span>
                    <span class="value">${detail.completeTime}</span>
                </div>
                ` : ''}
            </div>
            ${chaptersHtml ? `
            <div class="dc-detail-section">
                <h4>章节列表 (${detail.chapters.length})</h4>
                <div class="dc-detail-chapter-list">${chaptersHtml}</div>
            </div>
            ` : ''}
            ${detail.error ? `
            <div class="dc-detail-section">
                <h4>错误信息</h4>
                <div style="color:var(--danger);font-size:13px;padding:8px;background:rgba(220,53,69,0.05);border-radius:var(--radius-sm);">
                    ${detail.error}
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
            'queued': '排队中',
            'verifying': '校验中'
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