/**
 * This product includes software developed by flotiarenor.Copyright 2026 flotiarenor
 * Shell 注入的基础运行时
 * 提供：Bridge（通信）、Utils（工具）、通用 UI 组件函数
 */

// ==================== Bridge ====================
window.Bridge = (function() {
  let API_PREFIX = '';

  async function call(method, ...args) {
    const api = parent.pywebview && parent.pywebview.api;
    if (!api) throw new Error('PyWebView API 不可用');
    const fullMethod = API_PREFIX ? `${API_PREFIX}__${method}` : method;
    return await api[fullMethod](...args);
  }

  function originalUrl(path) {
    const plugin = API_PREFIX; // 如 'image-viewer'
    return `/files/${path}?plugin=${plugin}`;
  }

  function thumbUrl(path) {
    const plugin = API_PREFIX;
    return `/thumbs/${path}?plugin=${plugin}`;
  }

  function setPrefix(prefix) {
    API_PREFIX = prefix;
  }

  return { call, originalUrl, thumbUrl, setPrefix };
})();

// 兼容旧代码的全局 bridge 别名
window.bridge = window.Bridge;

// ==================== Utils ====================
window.Utils = {
  debounce(func, wait) {
    let timeout;
    return function(...args) {
      clearTimeout(timeout);
      timeout = setTimeout(() => func.apply(this, args), wait);
    };
  },

  formatFileSize(bytes) {
    if (bytes < 1024) return bytes + ' B';
    if (bytes < 1048576) return (bytes / 1024).toFixed(1) + ' KB';
    return (bytes / 1048576).toFixed(1) + ' MB';
  },

  escapeHtml(str) {
    const div = document.createElement('div');
    div.textContent = str;
    return div.innerHTML;
  }
};

// ==================== 树组件 ====================
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

// ==================== 灯箱组件 ====================
function createLightbox(options = {}) {
  const getImageUrl = options.getImageUrl || ((item) => item.url);

  const overlay = document.createElement('div');
  overlay.className = 'lightbox';
  overlay.innerHTML = `
    <div class="lightbox-arrow left">❮</div>
    <img id="lightbox-img" src="" alt="原图查看" draggable="false">
    <div class="lightbox-arrow right">❯</div>
    <div class="lightbox-close">✕</div>
  `;
  document.body.appendChild(overlay);

  const img = overlay.querySelector('#lightbox-img');
  const leftArrow = overlay.querySelector('.lightbox-arrow.left');
  const rightArrow = overlay.querySelector('.lightbox-arrow.right');
  const closeBtn = overlay.querySelector('.lightbox-close');

  let scale = 1, translate = { x: 0, y: 0 };
  let isDragging = false, dragStart = { x: 0, y: 0 };
  let currentIndex = -1, items = [];

  function resetTransform() { scale = 1; translate = { x: 0, y: 0 }; applyTransform(); }
  function applyTransform() {
    img.style.transform = `translate(${translate.x}px, ${translate.y}px) scale(${scale})`;
    img.style.cursor = scale > 1 ? 'grab' : 'zoom-out';
  }

  function show(itemList, index) {
    items = itemList; currentIndex = index;
    img.src = Bridge.originalUrl(getImageUrl(items[currentIndex]));
    overlay.classList.add('active');
    resetTransform();
    document.addEventListener('keydown', onKey);
    img.addEventListener('wheel', onWheel, { passive: false });
    img.addEventListener('mousedown', onDragStart);
    document.addEventListener('mousemove', onDragMove);
    document.addEventListener('mouseup', onDragEnd);
  }

  function hide() {
    overlay.classList.remove('active'); img.src = '';
    document.removeEventListener('keydown', onKey);
    img.removeEventListener('wheel', onWheel);
    img.removeEventListener('mousedown', onDragStart);
    document.removeEventListener('mousemove', onDragMove);
    document.removeEventListener('mouseup', onDragEnd);
  }

  function navigate(dir) {
    currentIndex += dir;
    if (currentIndex < 0) currentIndex = items.length - 1;
    if (currentIndex >= items.length) currentIndex = 0;
    img.src = Bridge.originalUrl(getImageUrl(items[currentIndex]));
    resetTransform();
  }

  function onKey(e) {
    if (e.key === 'Escape') hide();
    if (e.key === 'ArrowLeft') navigate(-1);
    if (e.key === 'ArrowRight') navigate(1);
  }
  function onWheel(e) {
    e.preventDefault();
    const delta = e.deltaY > 0 ? -0.1 : 0.1;
    scale = Math.max(0.5, Math.min(5, scale + delta));
    applyTransform();
  }
  function onDragStart(e) {
    if (scale <= 1) return;
    e.preventDefault(); isDragging = true;
    dragStart.x = e.clientX - translate.x;
    dragStart.y = e.clientY - translate.y;
    img.style.cursor = 'grabbing';
  }
  function onDragMove(e) {
    if (!isDragging) return;
    translate.x = e.clientX - dragStart.x;
    translate.y = e.clientY - dragStart.y;
    applyTransform();
  }
  function onDragEnd() {
    if (!isDragging) return;
    isDragging = false;
    img.style.cursor = scale > 1 ? 'grab' : 'zoom-out';
  }

  overlay.addEventListener('click', (e) => { if (e.target === overlay) hide(); });
  closeBtn.addEventListener('click', hide);
  leftArrow.addEventListener('click', (e) => { e.stopPropagation(); navigate(-1); });
  rightArrow.addEventListener('click', (e) => { e.stopPropagation(); navigate(1); });

  return { show, hide, navigate };
}

// ==================== 分页组件 ====================
function createPagination(container, options = {}) {
  function render(currentPage, totalPages) {
    container.innerHTML = '';
    if (totalPages <= 1) return;
    const delta = 2, pages = [];
    for (let i = 1; i <= totalPages; i++) {
      if (i === 1 || i === totalPages || (i >= currentPage - delta && i <= currentPage + delta)) {
        pages.push(i);
      }
    }
    container.appendChild(makeLink('«', 1, currentPage === 1));
    container.appendChild(makeLink('‹', currentPage - 1, currentPage === 1));
    let last = 0;
    pages.forEach(page => {
      if (last + 1 < page) {
        const ellipsis = document.createElement('span');
        ellipsis.className = 'ellipsis'; ellipsis.textContent = '...';
        container.appendChild(ellipsis);
      }
      container.appendChild(makeLink(page, page, false, page === currentPage));
      last = page;
    });
    container.appendChild(makeLink('›', currentPage + 1, currentPage === totalPages));
    container.appendChild(makeLink('»', totalPages, currentPage === totalPages));
  }

  function makeLink(text, page, isDisabled, isCurrent) {
    const el = document.createElement('a');
    el.innerHTML = text;
    if (isCurrent) el.classList.add('current');
    if (isDisabled) el.classList.add('disabled');
    if (!isDisabled && !isCurrent) {
      el.addEventListener('click', () => { if (options.onPageChange) options.onPageChange(page); });
    }
    return el;
  }

  return { render };
}

// ==================== 右键菜单组件 ====================
function createContextMenu(options = {}) {
  const menu = document.createElement('ul');
  menu.className = 'context-menu';
  (options.items || []).forEach(item => {
    const li = document.createElement('li');
    li.textContent = item.label;
    li.dataset.action = item.action;
    if (item.danger) li.classList.add('danger');
    li.addEventListener('click', () => {
      menu.style.display = 'none';
      if (options.onSelect) options.onSelect(item.action);
    });
    menu.appendChild(li);
  });
  document.body.appendChild(menu);
  document.addEventListener('click', () => { menu.style.display = 'none'; });

  function show(x, y, targetData) {
    menu._targetData = targetData;
    menu.style.top = `${y}px`;
    menu.style.left = `${x}px`;
    menu.style.display = 'block';
  }
  function getTargetData() { return menu._targetData; }

  return { show, getTargetData, element: menu };
}

// ==================== 卡片网格组件 ====================
function createCardGrid(container, options = {}) {
  function renderCard(item) {
    const data = options.cardRenderer(item);
    const card = document.createElement('div');
    card.className = 'manga-card';
    card.dataset.folderName = item.folder_name || '';
    card.innerHTML = `
      <div class="manga-cover">
        <img src="${data.image}" loading="lazy" alt="${data.title}">
        ${data.badge ? `<span class="manga-badge">${data.badge}</span>` : ''}
        ${data.extraHtml || ''}
      </div>
      <div class="manga-info">
        <p class="manga-title">${data.title}</p>
        ${data.subtitle ? `<p class="manga-author">${data.subtitle}</p>` : ''}
      </div>
    `;
    card.addEventListener('click', (e) => {
      if (e.target.closest('.card-extra-item')) return;
      if (options.onClick) options.onClick(item, Array.from(container.children).indexOf(card));
    });
    if (options.onContextMenu) {
      card.addEventListener('contextmenu', (e) => {
        e.preventDefault();
        options.onContextMenu(item, Array.from(container.children).indexOf(card), e);
      });
    }
    return card;
  }

  return {
    render(items) {
      container.innerHTML = '';
      items.forEach(item => container.appendChild(renderCard(item)));
    },
    append(items) {
      items.forEach(item => container.appendChild(renderCard(item)));
    }
  };
}
