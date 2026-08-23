/**
 * This product includes software developed by flotiarenor.Copyright 2026 flotiarenor
 * Shell 注入的基础运行时
 * 提供：Bridge（通信）、Utils（工具）、通用 UI 组件函数
 */

// ===== 集中设置变更通知：接收 shell 的 postMessage 后自动刷新 =====
window.addEventListener('message', function(event) {
  if (event.data && event.data.type === 'omnibox:settings-changed') {
    window.location.href = window.location.href.split('?')[0] + '?_t=' + Date.now();
  }
});

// ==================== Bridge ====================
window.Bridge = (function() {
  let API_PREFIX = '';

  // 兼容宿主再内嵌 iframe 的场景：从当前 frame 一直向上找拥有 pywebview.api 的窗口。
  function resolveApi() {
    let current = parent;
    while (current) {
      if (current.pywebview && current.pywebview.api) {
        return current.pywebview.api;
      }
      if (current === current.parent) break;
      current = current.parent;
    }
    return null;
  }

  async function call(method, ...args) {
    const api = resolveApi();
    if (!api) throw new Error('PyWebView API 不可用');
    const fullMethod = API_PREFIX ? `${API_PREFIX}__${method}` : method;
    return await api[fullMethod](...args);
  }

  async function callSystem(method, ...args) {
    const api = resolveApi();
    if (!api) throw new Error('PyWebView API 不可用');
    return await api[method](...args);
  }

  async function callPlugin(plugin, method, ...args) {
    const api = resolveApi();
    if (!api) throw new Error('PyWebView API 不可用');
    return await api[`${plugin}__${method}`](...args);
  }

  function originalUrl(path) {
    const plugin = API_PREFIX; // 如 'image-viewer'
    // 使用 query 参数传递 path，避免绝对路径中的 / 被 Flask 路由吞掉
    return `/file?path=${encodeURIComponent(path)}&plugin=${plugin}`;
  }

  function thumbUrl(path) {
    const plugin = API_PREFIX;
    return `/thumbs/${path}?plugin=${plugin}`;
  }

  function setPrefix(prefix) {
    API_PREFIX = prefix;
  }

  return { call, callSystem, callPlugin, originalUrl, thumbUrl, setPrefix };
})();

// 兼容旧代码的全局 bridge 别名
window.bridge = window.Bridge;

// ==================== 通用扩展入口渲染 ====================
// 宿主插件只需提供一个容器，并声明 host + placement：
//   renderExtensions(document.getElementById('extensions'), 'image-viewer', 'sidebar');
// 扩展插件通过 get_extensions() 注册到 Shell 后，会自动渲染到该容器。
function renderExtensions(container, host, placement, options = {}) {
  if (!container) return Promise.resolve();
  container.innerHTML = '';

  return Bridge.callSystem('system_get_plugin_extensions', host, placement)
    .then(list => {
      if (!Array.isArray(list) || list.length === 0) return;

      const section = document.createElement('div');
      section.className = 'obx-extensions' + (options.className ? ' ' + options.className : '');

      if (options.title) {
        const title = document.createElement('div');
        title.className = 'obx-extensions-title';
        title.textContent = options.title;
        section.appendChild(title);
      }

      list.forEach(ext => {
        const btn = document.createElement('button');
        btn.type = 'button';
        btn.className = 'obx-extension' + (options.itemClass ? ' ' + options.itemClass : '');
        btn.title = ext.description || ext.label || ext.id || '';
        btn.innerHTML =
          `<span class="obx-extension-icon">${Utils.escapeHtml(ext.icon || '🧩')}</span>` +
          `<span class="obx-extension-label">${Utils.escapeHtml(ext.label || ext.id || '扩展')}</span>`;

        btn.addEventListener('click', () => {
          // 1. 内嵌型扩展：在宿主内部打开 iframe 面板
          if (ext.embedUrl) {
            if (typeof options.onEmbed === 'function') {
              options.onEmbed(ext, btn);
            } else if (options.embedContainer) {
              options.embedContainer.innerHTML = '';
              const frame = document.createElement('iframe');
              frame.src = ext.embedUrl;
              frame.className = 'obx-embed-frame';
              options.embedContainer.appendChild(frame);
            }
            return;
          }
          // 2. 独立路由型扩展：跳转到插件自身页面
          if (ext.route) {
            const nav = parent && parent.__omniboxNavigate;
            if (typeof nav === 'function') {
              nav(ext.route);
            } else if (parent) {
              parent.location.href = ext.route;
            }
            return;
          }
          // 3. 纯后端方法型扩展：跨插件调用
          if (ext.method && ext.plugin) {
            Bridge.callPlugin(ext.plugin, ext.method).catch(err => {
              console.error('扩展调用失败:', err);
              if (window.Toast) Toast.error('扩展调用失败');
            });
          }
        });

        section.appendChild(btn);
      });

      container.appendChild(section);
    })
    .catch(err => {
      console.error('加载扩展失败:', err);
    });
}

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

// ==================== Toast 通知 ====================
window.Toast = (function() {
  let container = null;
  function ensureContainer() {
    if (!container) {
      container = document.createElement('div');
      container.className = 'toast-container';
      document.body.appendChild(container);
    }
    return container;
  }
  function show(message, type = 'info', duration = 2600) {
    const el = document.createElement('div');
    el.className = `toast toast-${type}`;
    el.textContent = message;
    ensureContainer().appendChild(el);
    requestAnimationFrame(() => el.classList.add('show'));
    setTimeout(() => {
      el.classList.remove('show');
      setTimeout(() => el.remove(), 250);
    }, duration);
    return el;
  }
  return {
    show,
    info: (msg) => show(msg, 'info'),
    success: (msg) => show(msg, 'success'),
    warning: (msg) => show(msg, 'warning'),
    error: (msg) => show(msg, 'error')
  };
})();

// ==================== 确认对话框（替代原生 confirm） ====================
function confirmDialog(message, options = {}) {
  return new Promise((resolve) => {
    const overlay = document.createElement('div');
    overlay.className = 'modal active';
    overlay.innerHTML = `
      <div class="modal-box modal-confirm">
        <div class="modal-body">
          <div class="confirm-message">${Utils.escapeHtml(message)}</div>
        </div>
        <div class="modal-footer">
          <button class="btn" data-act="cancel">取消</button>
          <button class="btn ${options.danger ? 'btn-danger-solid' : 'btn-primary'}" data-act="ok">${options.okText || '确定'}</button>
        </div>
      </div>`;
    document.body.appendChild(overlay);
    const close = (val) => { overlay.remove(); resolve(val); };
    overlay.addEventListener('click', (e) => { if (e.target === overlay) close(false); });
    overlay.querySelector('[data-act="cancel"]').addEventListener('click', () => close(false));
    overlay.querySelector('[data-act="ok"]').addEventListener('click', () => close(true));
  });
}

// ==================== 设置表单（按 schema 渲染） ====================
function createSettingsForm(container, schema, values = {}) {
  const fieldEls = {};
  const baseId = 'cfg-' + Math.random().toString(36).slice(2, 8);

  (schema || []).forEach((field) => {
    const wrap = document.createElement('div');
    wrap.className = 'field' + (field.type === 'checkbox' ? ' field-checkbox' : '');
    wrap.dataset.key = field.key;

    const label = document.createElement('label');
    label.className = 'field-label';
    label.htmlFor = `${baseId}-${field.key}`;
    label.textContent = field.label || field.key;
    if (field.required) label.classList.add('required');

    const current = values[field.key] !== undefined ? values[field.key] : field.default;
    let input;

    if (field.type === 'checkbox') {
      input = document.createElement('input');
      input.type = 'checkbox';
      input.id = `${baseId}-${field.key}`;
      input.checked = !!current;
      wrap.append(input, label);
    } else if (field.type === 'select') {
      input = document.createElement('select');
      input.id = `${baseId}-${field.key}`;
      (field.options || []).forEach(opt => {
        const o = document.createElement('option');
        o.value = (typeof opt === 'object') ? opt.value : opt;
        o.textContent = (typeof opt === 'object') ? opt.label : opt;
        input.appendChild(o);
      });
      if (current !== undefined) input.value = String(current);
    } else if (field.type === 'textarea') {
      input = document.createElement('textarea');
      input.id = `${baseId}-${field.key}`;
      input.value = current !== undefined ? current : '';
      if (field.placeholder) input.placeholder = field.placeholder;
    } else {
      const isRange = field.type === 'range';
      const isNumber = field.type === 'number';
      input = document.createElement('input');
      input.type = isRange ? 'range' : (isNumber ? 'number' : 'text');
      input.id = `${baseId}-${field.key}`;
      if (isRange || isNumber) {
        if (field.min !== undefined) input.min = field.min;
        if (field.max !== undefined) input.max = field.max;
        if (field.step !== undefined) input.step = field.step;
        input.value = current !== undefined ? current : (field.default !== undefined ? field.default : 0);
      } else {
        input.value = current !== undefined ? current : (field.default !== undefined ? field.default : '');
        if (field.placeholder) input.placeholder = field.placeholder;
      }
    }

    if (field.type === 'range') {
      const valueSpan = document.createElement('span');
      valueSpan.className = 'field-range-value';
      valueSpan.textContent = input.value;
      input.addEventListener('input', () => { valueSpan.textContent = input.value; });
      const row = document.createElement('div');
      row.className = 'field-range';
      row.append(input, valueSpan);
      wrap.append(label, row);
      fieldEls[field.key] = input;
    } else if (field.type === 'checkbox') {
      fieldEls[field.key] = input;
    } else {
      wrap.append(label, input);
      fieldEls[field.key] = input;
    }

    if (field.help) {
      const help = document.createElement('p');
      help.className = 'field-help';
      help.textContent = field.help;
      wrap.appendChild(help);
    }
    container.appendChild(wrap);
  });

  function getValues() {
    const out = {};
    (schema || []).forEach((field) => {
      const el = fieldEls[field.key];
      if (!el) return;
      if (field.type === 'checkbox') out[field.key] = el.checked;
      else if (field.type === 'number' || field.type === 'range') out[field.key] = Number(el.value);
      else out[field.key] = el.value;
    });
    return out;
  }

  function setValues(newValues) {
    (schema || []).forEach((field) => {
      const el = fieldEls[field.key];
      if (!el || newValues[field.key] === undefined) return;
      if (field.type === 'checkbox') el.checked = !!newValues[field.key];
      else if (field.type === 'range') {
        el.value = newValues[field.key];
        const span = el.parentElement.querySelector('.field-range-value');
        if (span) span.textContent = el.value;
      } else el.value = newValues[field.key];
    });
  }

  return { getValues, setValues, element: container };
}

// ==================== 统一设置弹窗（插件按需调用） ====================
async function openSettingsModal(options = {}) {
  const title = options.title || '设置';
  let schema = options.schema;
  let values = options.values;
  const onSave = options.onSave;

  if (!schema) {
    try { schema = await Bridge.call('get_settings_schema'); } catch (e) { schema = []; }
  }
  if (values === undefined) {
    try { values = await Bridge.call('get_settings'); } catch (e) { values = {}; }
  }

  const overlay = document.createElement('div');
  overlay.className = 'modal active';
  overlay.innerHTML = `
    <div class="modal-box">
      <h3>${Utils.escapeHtml(title)}</h3>
      <div class="modal-body settings-form"></div>
      <div class="modal-footer">
        <button class="btn" data-act="cancel">取消</button>
        <button class="btn btn-primary" data-act="save">保存</button>
      </div>
    </div>`;
  document.body.appendChild(overlay);

  const body = overlay.querySelector('.modal-body');
  let form = null;
  if (!schema || schema.length === 0) {
    body.innerHTML = '<div class="empty-state" style="min-height:120px;">该插件暂无设置项</div>';
  } else {
    form = createSettingsForm(body, schema, values || {});
  }

  const close = () => overlay.remove();
  overlay.addEventListener('click', (e) => { if (e.target === overlay) close(); });
  overlay.querySelector('[data-act="cancel"]').addEventListener('click', close);

  overlay.querySelector('[data-act="save"]').addEventListener('click', async () => {
    const saveBtn = overlay.querySelector('[data-act="save"]');
    const cancelBtn = overlay.querySelector('[data-act="cancel"]');
    saveBtn.disabled = true;
    try {
      const newValues = form ? form.getValues() : {};
      let result = newValues;
      if (onSave) {
        result = await onSave(newValues);
      } else {
        result = await Bridge.call('save_settings', newValues);
      }
      if (result && result.success === false) {
        throw new Error(result.error || '保存失败');
      }
      close();
      Toast.success((options.successMessage) || (result && result.message) || '设置已保存');
      setTimeout(() => { window.location.href = window.location.href.split('?')[0] + '?_t=' + Date.now(); }, 400);
    } catch (e) {
      saveBtn.disabled = false;
      Toast.error(e.message || '保存失败');
    }
  });
}

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
