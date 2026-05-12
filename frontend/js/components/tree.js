/**
 * 通用树组件
 * @param {HTMLElement} container - 树的容器
 * @param {Object} options
 *   - data: 初始数据 [{name, path}]
 *   - onLoadChildren: async (path) => [{name, path}]
 *   - onClick: (item) => void
 *   - icon: 节点图标，默认 '📁'
 */
function createTree(container, options = {}) {
    const icon = options.icon || '📁';
    let selectedLabel = null;

    function renderNode(item, depth) {
        const itemDiv = document.createElement('div');
        itemDiv.className = 'tree-item';

        const label = document.createElement('div');
        label.className = 'tree-label';
        label.style.paddingLeft = (depth * 16 + 10) + 'px';

        const arrow = document.createElement('span');
        arrow.className = 'tree-arrow collapsed';
        arrow.textContent = '▼';

        const iconSpan = document.createElement('span');
        iconSpan.className = 'tree-icon';
        iconSpan.textContent = icon;

        const nameSpan = document.createElement('span');
        nameSpan.className = 'tree-name';
        nameSpan.textContent = item.name;

        label.append(arrow, iconSpan, nameSpan);
        itemDiv.appendChild(label);

        const childrenDiv = document.createElement('div');
        childrenDiv.className = 'tree-children';
        itemDiv.appendChild(childrenDiv);

        // 点击整行：展开/折叠
        label.addEventListener('click', async (e) => {
            e.stopPropagation();

            // 高亮选中
            if (selectedLabel) selectedLabel.classList.remove('active');
            label.classList.add('active');
            selectedLabel = label;

            // 通知外部
            if (options.onClick) options.onClick(item);

            // 展开/折叠
            if (arrow.classList.contains('collapsed')) {
                arrow.classList.remove('collapsed');
                if (!childrenDiv.dataset.loaded) {
                    if (options.onLoadChildren) {
                        childrenDiv.innerHTML = '<div class="loading">加载中...</div>';
                        try {
                            const children = await options.onLoadChildren(item.path);
                            childrenDiv.innerHTML = '';
                            if (children.length === 0) {
                                arrow.style.visibility = 'hidden';
                            } else {
                                children.forEach(child =>
                                    childrenDiv.appendChild(renderNode(child, depth + 1))
                                );
                            }
                            childrenDiv.dataset.loaded = 'true';
                        } catch (err) {
                            childrenDiv.innerHTML = '<div class="loading">加载失败</div>';
                            arrow.classList.add('collapsed');
                            return;
                        }
                    }
                }
                childrenDiv.classList.add('expanded');
            } else {
                arrow.classList.add('collapsed');
                childrenDiv.classList.remove('expanded');
            }
        });

        return itemDiv;
    }

    // 渲染初始数据
    if (options.data) {
        options.data.forEach(item => container.appendChild(renderNode(item, 0)));
    }

    return {
        // 程序化展开并选中某个节点
        selectPath(path) {
            // 简化实现：外部自行管理选中状态
        }
    };
}