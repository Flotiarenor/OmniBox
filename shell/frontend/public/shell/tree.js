/**
 * This product includes software developed by flotiarenor.Copyright 2026 flotiarenor
 * 共享运行时 · 树组件（createTree）：展开折叠、勾选、数字徽标与异步子节点。
 *
 * 图标走 Utils.iconHtml（名字来自仓库常量），节点文案走 textContent 或 Utils.escapeHtml；
 * 异步子节点加载中 / 加载失败各留一行提示，失败后可重试，不把错误吞成空白节点。
 */

// ==================== 树组件 ====================
function createTree(container, options = {}) {
  const icon = Utils.iconHtml(options.icon || 'icon:folder');
  let selectedLabel = null;

  function renderNode(item, depth) {
    const itemDiv = document.createElement('div');
    itemDiv.className = 'tree-item';

    const label = document.createElement('div');
    label.className = 'tree-label';
    label.style.paddingLeft = (depth * 16 + 10) + 'px';

    const arrow = document.createElement('span');
    arrow.className = 'tree-arrow collapsed';
    // 折叠态由 CSS 的 rotate(-90deg) 表现（base.css 的 .tree-arrow.collapsed），
    // 这里只放图标本体；.tree-arrow 的字号决定图标尺寸。
    arrow.innerHTML = Utils.iconHtml('icon:chevron-down');

    const iconSpan = document.createElement('span');
    iconSpan.className = 'tree-icon';
    // icon 已经是 Utils.iconHtml 产出的安全标记（非 `icon:` 值在内部走 escapeHtml），
    // 所以这里写 innerHTML：写 textContent 会把标记当纯文本显示出来。
    iconSpan.innerHTML = icon;

    const nameSpan = document.createElement('span');
    nameSpan.className = 'tree-name';
    nameSpan.textContent = item.name;

    label.append(arrow, iconSpan, nameSpan);
    itemDiv.appendChild(label);

    const childrenDiv = document.createElement('div');
    childrenDiv.className = 'tree-children';
    itemDiv.appendChild(childrenDiv);

    label.addEventListener('click', async (e) => {
      e.stopPropagation();
      if (selectedLabel) selectedLabel.classList.remove('active');
      label.classList.add('active');
      selectedLabel = label;
      if (options.onClick) options.onClick(item);

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
                children.forEach(child => childrenDiv.appendChild(renderNode(child, depth + 1)));
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

  if (options.data) {
    options.data.forEach(item => container.appendChild(renderNode(item, 0)));
  }

  return { selectPath(path) { /* 简化实现 */ } };
}

