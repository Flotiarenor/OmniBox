/**
 * 共享内容卡片 / 行基座（注入 content iframe，不替代插件视觉语言）。
 *
 * 只做结构、可访问性与状态：图片懒加载、title/sub/meta/badge、操作插槽、
 * selected/焦点/激活；具体密度与变体由 `content-ui.css` 的 `cui-*` 以及插件
 * 自己的内容根节点覆盖。调用方不需要拼 HTML，也不依赖宿主 DOM。
 *
 * 一张卡片同时承载图片/文字/类型提示/状态/操作；差异走 options：
 *   layout、density、mediaOverlay、actionsPlacement、actions、image。
 * 旧调用（card/row/grid/list 及 create* 别名）保持兼容。
 *
 * 已废弃：`variant`。它曾经只生成 `cui-card--*` / `cui-row--*` / `cui-media--*`
 * 类名，而统一 160/16 硬指标之后这些类名没有任何样式消费方（插件仍会传，静默忽略）。
 *
 * 用法：
 *   const card = ContentUI.card({ image: {src, alt}, title, actions });
 *   grid.appendChild(card);
 *   const grid = ContentUI.grid({ min: '180px', visibleCount: 12, expandable: true });
 *   grid.appendChild(card);          // 超过 visibleCount 的卡片只隐藏，不重建
 *
 * 可激活卡片用 `role="button"` + `tabindex="0"`（内部还要放操作按钮，不能用
 * `<button>` 嵌套 `<button>`）；Enter / Space 激活，操作按钮的点击/键盘事件
 * 不会误触卡片激活。
 */
(function () {
  'use strict';

  var DENSITIES = { compact: 1, normal: 1, spacious: 1 };
  var PLACEMENTS = { bottom: 1, 'top-right': 1, overlay: 1 };
  // 状态色调白名单：值直接拼进 className，非法字符（空格等）会让 classList.add 抛错。
  var STATUS_TONES = { ok: 1, warn: 1, error: 1 };
  // aria 属性名白名单：调用方给的 key 会被拼成 `aria-<key>`，非法字符同理。
  var ARIA_KEY_RE = /^aria-[a-z][a-z0-9-]*$/;
  var collectionSeq = 0;

  function el(tag, className) {
    var node = document.createElement(tag);
    if (className) node.className = className;
    return node;
  }

  function text(tag, className, value) {
    var node = el(tag, className);
    node.textContent = value == null ? '' : String(value);
    return node;
  }

  function addClasses(node, value) {
    if (!node || !node.classList || !value) return;
    String(value).split(/\s+/).forEach(function (token) {
      if (token) node.classList.add(token);
    });
  }

  function normalizeImage(image) {
    if (!image) return null;
    if (typeof image === 'string') return { src: image, alt: '' };
    if (typeof image === 'object' && image.src) {
      return {
        src: String(image.src),
        alt: image.alt == null ? '' : String(image.alt),
        data: image.data && typeof image.data === 'object' ? image.data : null,
        onError: typeof image.onError === 'function' ? image.onError : null,
        className: image.className || image.class || '',
      };
    }
    return null;
  }

  /** 只接受 icon:* 名称；图标标记来自壳的生成器，不接受调用方 HTML。 */
  function iconMarkup(name) {
    if (typeof name !== 'string' || !/^icon:[a-z0-9-]+$/i.test(name)) return '';
    var utils = window.Utils;
    if (utils && typeof utils.iconHtml === 'function') return utils.iconHtml(name);
    var icons = window.Icons;
    if (icons && typeof icons.html === 'function') return icons.html(name);
    return '';
  }

  function inActions(target) {
    return !!(target && typeof target.closest === 'function' && target.closest('.cui-actions'));
  }

  function setSelected(root, selected) {
    if (!root || !root.classList) return;
    var on = !!selected;
    root.classList.toggle('is-selected', on);
    if (root.dataset) root.dataset.selected = on ? 'true' : 'false';
    root.setAttribute('aria-pressed', on ? 'true' : 'false');
  }

  function applyActionAria(button, aria) {
    if (!aria || typeof aria !== 'object') return;
    Object.keys(aria).forEach(function (key) {
      var value = aria[key];
      if (value === null || value === undefined || value === false) return;
      var attr = key.indexOf('aria-') === 0 ? key : 'aria-' + key;
      // 非法属性名在真实 DOM 里抛 InvalidCharacterError，会让整张卡片的渲染中断。
      if (!ARIA_KEY_RE.test(attr)) return;
      button.setAttribute(attr, String(value));
    });
  }

  function appendActions(root, actions, actionSlot, placement, media) {
    if ((!actions || !actions.length) && !actionSlot) return null;
    var place = PLACEMENTS[placement] ? placement : 'bottom';
    var box = el('div', 'cui-actions');
    if (place !== 'bottom') box.classList.add('cui-actions--' + place);
    (actions || []).forEach(function (action) {
      if (!action) return;
      var button = el('button', 'btn btn-sm cui-action');
      button.type = 'button';
      addClasses(button, action.className || action.class);
      var label = action.label == null ? '' : String(action.label);
      var icon = iconMarkup(action.icon);
      if (icon) {
        button.innerHTML = icon;
        button.appendChild(text('span', 'cui-action-label', label));
      } else {
        button.textContent = label;
      }
      if (action.title) button.title = String(action.title);
      if (action.ariaLabel) button.setAttribute('aria-label', String(action.ariaLabel));
      applyActionAria(button, action.aria);
      if (action.data && typeof action.data === 'object') {
        Object.keys(action.data).forEach(function (key) {
          if (button.dataset) button.dataset[key] = String(action.data[key]);
        });
      }
      if (action.disabled) button.disabled = true;
      button.addEventListener('click', function (event) {
        if (event && event.stopPropagation) event.stopPropagation();
        if (typeof action.onClick === 'function') action.onClick(event);
      });
      box.appendChild(button);
    });
    if (actionSlot && actionSlot.nodeType) box.appendChild(actionSlot);
    if ((place === 'top-right' || place === 'overlay') && media) media.appendChild(box);
    else root.appendChild(box);
    return box;
  }

  /** 封面上的计数 / 时间 / 徽标 / 进度；只读文本走 textContent。 */
  function appendMediaOverlay(media, overlay) {
    if (!overlay) return;
    if (typeof overlay !== 'object' || Array.isArray(overlay)) overlay = { badge: overlay };
    function add(className, value) {
      if (value === null || value === undefined || value === '') return;
      media.appendChild(text('span', 'cui-media-' + className, value));
    }
    add('badge', overlay.badge);
    add('count', overlay.count);
    add('time', overlay.time);
    add('text', overlay.text);
    var progress = overlay.progress;
    if (progress !== null && progress !== undefined) {
      var percent = 0;
      if (typeof progress === 'number') {
        percent = progress;
      } else if (typeof progress === 'object') {
        if (Number.isFinite(Number(progress.percent))) percent = Number(progress.percent);
        else if (Number(progress.total) > 0) {
          percent = (Number(progress.value) || 0) / Number(progress.total) * 100;
        }
      }
      percent = Math.max(0, Math.min(100, Math.round((Number(percent) || 0) * 100) / 100));
      var track = el('div', 'cui-media-progress');
      track.setAttribute('role', 'progressbar');
      track.setAttribute('aria-valuemin', '0');
      track.setAttribute('aria-valuemax', '100');
      track.setAttribute('aria-valuenow', String(percent));
      if (overlay.progressLabel) track.setAttribute('aria-label', String(overlay.progressLabel));
      var bar = el('span', 'cui-media-progress-bar');
      if (bar.style) bar.style.width = percent + '%';
      track.appendChild(bar);
      media.appendChild(track);
    }
  }

  function appendMedia(root, image, overlay) {
    var media = el('div', 'cui-media');
    var normalized = normalizeImage(image);
    if (!normalized) {
      media.classList.add('cui-media--empty');
      media.setAttribute('aria-hidden', 'true');
      appendMediaOverlay(media, overlay);
      root.appendChild(media);
      return media;
    }
    var img = el('img', 'cui-image');
    img.loading = 'lazy';
    img.decoding = 'async';
    img.src = normalized.src;
    img.alt = normalized.alt;
    addClasses(img, normalized.className);
    if (normalized.data) {
      Object.keys(normalized.data).forEach(function (key) {
        if (img.dataset) img.dataset[key] = String(normalized.data[key]);
      });
    }
    img.addEventListener('error', function (event) {
      media.classList.add('cui-media--broken');
      if (normalized.onError) normalized.onError(event, img);
    });
    media.appendChild(img);
    appendMediaOverlay(media, overlay);
    root.appendChild(media);
    return media;
  }

  function appendBody(root, options) {
    var body = el('div', 'cui-body');
    if (options.type != null) body.appendChild(text('div', 'cui-type', options.type));
    if (options.title != null) body.appendChild(text('div', 'cui-title', options.title));
    if (options.sub != null) body.appendChild(text('div', 'cui-sub', options.sub));
    if (options.status != null) {
      var status = typeof options.status === 'object' ? options.status : { text: options.status };
      var statusNode = text('div', 'cui-status', status.text);
      // 只接受白名单色调：非法值拼进 className 会让 classList.add 抛
      // InvalidCharacterError（空格）或生成无样式类名。
      if (status.tone && STATUS_TONES[status.tone]) {
        statusNode.classList.add('cui-status--' + status.tone);
      }
      body.appendChild(statusNode);
    }
    var metaRow = el('div', 'cui-meta-row');
    if (options.meta != null) metaRow.appendChild(text('span', 'cui-meta', options.meta));
    if (options.badge != null) metaRow.appendChild(text('span', 'cui-badge', options.badge));
    if (metaRow.children.length) body.appendChild(metaRow);
    root.appendChild(body);
  }

  function wireActivation(root, onActivate, ariaLabel) {
    if (typeof onActivate !== 'function') return;
    root.setAttribute('role', 'button');
    root.tabIndex = 0;
    if (ariaLabel) root.setAttribute('aria-label', String(ariaLabel));

    root.addEventListener('click', function (event) {
      if (inActions(event && event.target)) return;
      onActivate(event);
    });

    root.addEventListener('keydown', function (event) {
      if (event.key !== 'Enter' && event.key !== ' ' && event.key !== 'Spacebar') return;
      if (inActions(event.target)) return;
      if (event.preventDefault) event.preventDefault();
      onActivate(event);
    });
  }

  function applyCommon(root, options, baseClass) {
    root.className = baseClass;
    addClasses(root, options.className);
    if (options.density && DENSITIES[options.density]) {
      root.classList.add('cui-density-' + options.density);
    }
    if (options.data && typeof options.data === 'object') {
      Object.keys(options.data).forEach(function (key) {
        if (root.dataset) root.dataset[key] = String(options.data[key]);
      });
    }
    if (options.ariaLabel) root.setAttribute('aria-label', String(options.ariaLabel));
  }

  function build(options, layout) {
    options = options || {};
    var baseClass = layout === 'row' ? 'cui-row' : 'cui-card';
    var root = el('div', baseClass);
    applyCommon(root, options, baseClass);
    var media = appendMedia(root, options.image, options.mediaOverlay);
    appendBody(root, options);
    appendActions(root, options.actions, options.actionSlot, options.actionsPlacement, media);
    wireActivation(root, options.onActivate, options.ariaLabel);
    if (options.selected !== undefined) setSelected(root, options.selected);
    return root;
  }

  function card(options) {
    options = options || {};
    return build(options, options.layout === 'row' ? 'row' : 'card');
  }

  function row(options) {
    return build(options, 'row');
  }

  function gridBase(options) {
    options = options || {};
    var root = el('div', 'cui-grid');
    addClasses(root, options.className);
    if (options.min) root.style.setProperty('--cui-min', String(options.min));
    if (options.ariaLabel) root.setAttribute('aria-label', String(options.ariaLabel));
    return root;
  }

  function listBase(options) {
    options = options || {};
    var root = el('div', 'cui-list');
    addClasses(root, options.className);
    if (options.ariaLabel) root.setAttribute('aria-label', String(options.ariaLabel));
    return root;
  }

  function formatCollectionLabel(value, hidden, total, fallback) {
    var raw = typeof value === 'function'
      ? String(value(hidden, total))
      : (typeof value === 'string' && value ? value : fallback);
    return raw.replace(/\{hidden\}/g, String(hidden)).replace(/\{total\}/g, String(total));
  }

  /** 集合：卡片只在 DOM 里隐藏/显示，展开收起不重建卡片。 */
  function createCollection(kind, options) {
    options = options || {};
    var wrapper = el('div', 'cui-collection cui-collection--' + kind);
    var inner = kind === 'list' ? listBase(options) : gridBase(options);
    var innerId = options.id || ('cui-collection-' + (++collectionSeq));
    inner.id = innerId;
    wrapper.appendChild(inner);

    var toggle = el('button', 'btn btn-sm cui-collection-toggle');
    toggle.type = 'button';
    toggle.hidden = true;
    toggle.setAttribute('aria-controls', innerId);
    toggle.setAttribute('aria-expanded', 'false');
    wrapper.appendChild(toggle);

    var items = [];
    var expanded = options.expanded === true;
    var limit = options.visibleCount === undefined
      ? 8 : Math.max(0, Math.floor(Number(options.visibleCount)));
    if (!Number.isFinite(limit)) limit = 8;
    var expandable = options.expandable !== false;

    function hiddenCount() {
      return Math.max(0, items.length - limit);
    }

    function applyVisibility() {
      var collapsed = expandable && !expanded;
      items.forEach(function (item, index) {
        var hide = collapsed && index >= limit;
        item.classList.toggle('cui-item--hidden', hide);
        if (hide) item.setAttribute('aria-hidden', 'true');
        else item.removeAttribute('aria-hidden');
      });
      var canToggle = expandable && items.length > limit;
      toggle.hidden = !canToggle;
      toggle.setAttribute('aria-expanded', expanded ? 'true' : 'false');
      var hidden = hiddenCount();
      var label = expanded
        ? formatCollectionLabel(options.collapsedLabel, hidden, items.length, '收起')
        : formatCollectionLabel(options.expandLabel, hidden, items.length, '展开全部（' + hidden + '）');
      toggle.textContent = label;
      toggle.setAttribute('aria-label', label);
    }

    function append(node) {
      if (!node) return node;
      items.push(node);
      inner.appendChild(node);
      applyVisibility();
      return node;
    }

    function setItems(nodes) {
      inner.innerHTML = '';
      items = [];
      (nodes || []).forEach(function (node) {
        if (node) {
          items.push(node);
          inner.appendChild(node);
        }
      });
      applyVisibility();
      return wrapper;
    }

    function notify() {
      if (typeof options.onExpandChange === 'function') {
        options.onExpandChange(expanded, items.slice());
      }
    }

    function toggleExpanded() {
      if (!expandable) return;
      expanded = !expanded;
      applyVisibility();
      notify();
    }

    toggle.addEventListener('click', toggleExpanded);
    // 覆盖 appendChild，让旧式 `collection.appendChild(card)` 也进入集合逻辑；
    // 旧 grid/list 在没有集合选项时仍返回原来的元素，不受影响。
    wrapper.appendChild = append;
    wrapper.append = function () {
      Array.prototype.forEach.call(arguments, append);
      return wrapper;
    };
    wrapper.setItems = setItems;
    wrapper.expand = function () {
      if (!expandable) return;
      expanded = true;
      applyVisibility();
      notify();
    };
    wrapper.collapse = function () {
      if (!expandable) return;
      expanded = false;
      applyVisibility();
      notify();
    };
    wrapper.toggleExpanded = toggleExpanded;
    wrapper.itemsEl = inner;
    wrapper.toggleEl = toggle;
    wrapper.isExpanded = function () { return expanded; };
    applyVisibility();
    return wrapper;
  }

  function needsCollection(options) {
    return !!options && (options.visibleCount !== undefined || options.expandable === true);
  }

  function grid(options) {
    options = options || {};
    if (needsCollection(options)) return createCollection('grid', options);
    return gridBase(options);
  }

  function list(options) {
    options = options || {};
    if (needsCollection(options)) return createCollection('list', options);
    return listBase(options);
  }

  function collection(options) {
    options = options || {};
    return createCollection(options.layout === 'list' ? 'list' : 'grid', options);
  }

  window.ContentUI = {
    card: card,
    row: row,
    grid: grid,
    list: list,
    collection: collection,
    setSelected: setSelected,
    // 别名：旧代码/文档可以按 create* 渐进接入。
    createCard: card,
    createRow: row,
    createGrid: grid,
    createList: list,
    createCollection: collection,
  };
})();
