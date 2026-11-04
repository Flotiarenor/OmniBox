/**
 * This product includes software developed by flotiarenor.Copyright 2026 flotiarenor
 * 共享运行时 · 灯箱（createLightbox）：全屏看图、看图信息、左右切换、缩放拖动。
 *
 * 文件里还有解码位图的内存缓存：客户端持有文件字节不等于显示时不卡 —— 同一张原图重复
 * 打开时，渲染进程可能已经丢掉解码后的位图，于是重新解码并从上到下一行行刷。位图的寿命
 * 跟着 <img> 元素走，所以这里额外养一小池离屏 <img> 把最近看过的位图留住；池子的上限与
 * 淘汰和灯箱的打开 / 关闭生命周期耦合，因此与灯箱放在同一个文件里。
 */

// ==================== 灯箱组件 ====================
function createLightbox(options = {}) {
  const getImageUrl = options.getImageUrl || ((item) => item.url);
  const labels = Object.assign({
    dialog: '图片查看器',
    image: '原图查看',
    previous: '上一张',
    next: '下一张',
    close: '关闭',
    counter: null,
  }, options.labels || {});
  const counterEnabled = options.counter === true;
  const advanceOnClick = options.advanceOnClick === true || options.advance === true;
  const infoProvider = typeof options.infoProvider === 'function'
    ? options.infoProvider : null;
  const labelText = (value, fallback) => {
    const text = typeof value === 'string' && value ? value : fallback;
    if (typeof Utils.escapeHtml === 'function') return Utils.escapeHtml(text);
    return String(text).replace(/&/g, '&amp;').replace(/</g, '&lt;')
      .replace(/>/g, '&gt;').replace(/"/g, '&quot;').replace(/'/g, '&#39;');
  };

  const overlay = document.createElement('div');
  overlay.className = 'lightbox';
  overlay.tabIndex = -1;
  overlay.innerHTML = `
    <div class="lightbox-arrow left" role="button" tabindex="0" aria-label="${labelText(labels.previous, '上一张')}">${Utils.iconHtml('icon:chevron-left')}</div>
    <img id="lightbox-img" src="" alt="${labelText(labels.image, '原图查看')}" draggable="false">
    <div class="lightbox-arrow right" role="button" tabindex="0" aria-label="${labelText(labels.next, '下一张')}">${Utils.iconHtml('icon:chevron-right')}</div>
    <div class="lightbox-close" role="button" tabindex="0" aria-label="${labelText(labels.close, '关闭')}">${Utils.iconHtml('icon:x')}</div>
    <div class="lightbox-info" aria-live="polite"></div>
  `;
  if (typeof overlay.setAttribute === 'function') {
    overlay.setAttribute('role', 'dialog');
    overlay.setAttribute('aria-modal', 'true');
    overlay.setAttribute('aria-label', options.label
      || (typeof labels.dialog === 'string' && labels.dialog) || '图片查看器');
  }
  document.body.appendChild(overlay);

  const img = overlay.querySelector('#lightbox-img');
  const leftArrow = overlay.querySelector('.lightbox-arrow.left');
  const rightArrow = overlay.querySelector('.lightbox-arrow.right');
  const closeBtn = overlay.querySelector('.lightbox-close');
  const infoEl = overlay.querySelector('.lightbox-info');
  let counterEl = null;
  if (counterEnabled) {
    counterEl = document.createElement('div');
    counterEl.className = 'lightbox-counter';
    counterEl.setAttribute('aria-live', 'polite');
    overlay.appendChild(counterEl);
  }
  let previousFocus = null;

  let scale = 1, translate = { x: 0, y: 0 };
  let isDragging = false, dragStart = { x: 0, y: 0 }, dragDistance = 0;
  let suppressClick = false;
  let currentIndex = -1, items = [];
  //: 灯箱是否处于打开态。`isOpen()` 与 `onClose` 让插件能在壳返回/导航时先关灯箱，
  //: 而不是靠猜 class 或重复调用 hide()。
  let opened = false;
  let infoToken = 0;

  // ===== 解码位图内存缓存（"看过的图再打开瞬间整张出现"）=====
  //
  // 为什么需要它：客户端持有文件字节 ≠ 显示时不卡。实测同一张 10 MB / 数千像素的
  // 原图，重复打开会出现两种表现 —— 有时整张瞬出，有时**从上到下一行行刷**。后者是
  // 渲染进程把**解码后的位图**丢掉了，于是重新解码并渐进绘制。文件字节那层（HTTP
  // 缓存 + 系统页缓存）是无辜的：请求可能只回一个 304，但位图仍要重建。
  //
  // 保住位图的唯一现实做法是**让持有它的 <img> 元素活着**：位图的寿命跟着元素走，
  // 元素被回收位图就没了。灯箱本身只用一个 <img>，换 src 就等于丢掉上一张的位图，
  // 所以这里额外养一小池离屏 <img>，只负责把最近看过的位图留在渲染进程里。
  //
  // 代价按**像素**算而不是文件大小：单张 ≈ 宽 × 高 × 4 字节（4000×4000 ≈ 64 MB，
  // 6000×6000 ≈ 144 MB），且渲染进程往往还留一份 GPU 副本。因此上限必须可调，
  // 0 = 关闭。
  const decodedPool = new Map();     // url → { el, at }
  const KEEP_RECENT_MS = 20000;      // 只留"刚看过"的，不按张数硬顶
  let decodedMax = 8;

  function itemUrl(item) {
    return item ? Bridge.originalUrl(getImageUrl(item)) : '';
  }

  function trimDecodedPool() {
    const now = Date.now();
    decodedPool.forEach((entry, url) => {
      if (decodedPool.size <= decodedMax && (now - entry.at) < KEEP_RECENT_MS) return;
      decodedPool.delete(url);       // 丢掉引用 → 元素与其位图一起被回收
    });
  }

  function setDecodedLimit(value) {
    const parsed = Math.floor(Number(value));
    decodedMax = Number.isFinite(parsed) && parsed > 0 ? parsed : 0;
    if (decodedMax === 0) decodedPool.clear();
    else trimDecodedPool();
  }

  /** 把当前这张的位图留在渲染进程里（池满时先淘汰最久未用的）。 */
  function retainDecoded() {
    if (decodedMax <= 0) return;
    const url = img.currentSrc || img.src;
    if (!url) return;
    const hit = decodedPool.get(url);
    if (hit) hit.at = Date.now();
    else decodedPool.set(url, { el: img.cloneNode(false), at: Date.now() });
    while (decodedPool.size > decodedMax) {
      const oldest = decodedPool.keys().next().value;
      decodedPool.delete(oldest);
    }
  }

  /**
   * 缩放后的平移夹取：放大态不允许把图拖到视口外，避免"超大图溢出看不到边"。
   * 小图在 scale=1 时保持原尺寸居中，不主动放大（CSS 只限制 max，不设 width）。
   */
  function clampTranslate() {
    if (scale <= 1) {
      translate = { x: 0, y: 0 };
      return;
    }
    const width = Number(img.offsetWidth) || 0;
    const height = Number(img.offsetHeight) || 0;
    const viewW = Number(window.innerWidth) || 0;
    const viewH = Number(window.innerHeight) || 0;
    if (!width || !height || !viewW || !viewH) return;
    const maxX = Math.max(0, (width * scale - viewW) / 2);
    const maxY = Math.max(0, (height * scale - viewH) / 2);
    translate.x = Math.min(maxX, Math.max(-maxX, translate.x));
    translate.y = Math.min(maxY, Math.max(-maxY, translate.y));
  }

  function resetTransform() {
    scale = 1; translate = { x: 0, y: 0 };
    // 复位时一并恢复过渡：拖动期间它被置为 none，若在窗口外松手导致 mouseup 未触发，
    // 残留的 none 会让之后滚轮缩放的平滑消失。
    img.style.transition = '';
    applyTransform();
  }
  function applyTransform() {
    clampTranslate();
    img.style.transform = `translate(${translate.x}px, ${translate.y}px) scale(${scale})`;
    img.style.cursor = scale > 1 ? 'grab' : 'zoom-out';
  }

  function counterText() {
    const current = currentIndex + 1;
    const total = items.length;
    if (typeof labels.counter === 'function') return String(labels.counter(current, total));
    if (typeof labels.counter === 'string' && labels.counter) {
      return labels.counter
        .replace(/\{current\}/g, String(current))
        .replace(/\{total\}/g, String(total));
    }
    return `${current} / ${total}`;
  }

  function updateCounter() {
    if (!counterEl) return;
    counterEl.textContent = items.length ? counterText() : '';
  }

  /** `infoProvider` 的返回值：字符串/数组/{items}/{text}；只用 textContent，不接收 HTML。 */
  function paintInfo(value) {
    if (!infoEl) return;
    infoEl.textContent = '';
    if (value === null || value === undefined || value === '') return;
    let entries;
    if (Array.isArray(value)) entries = value;
    else if (typeof value === 'object' && Array.isArray(value.items)) entries = value.items;
    else if (typeof value === 'object' && value.text !== undefined) entries = [value.text];
    else entries = [value];
    entries.forEach((entry) => {
      if (entry === null || entry === undefined || entry === '') return;
      const row = document.createElement('div');
      row.className = 'lightbox-info-item';
      row.textContent = String(entry);
      infoEl.appendChild(row);
    });
  }

  function updateInfo() {
    const item = items[currentIndex];
    const token = ++infoToken;
    if (!item) {
      infoEl.innerHTML = '';
      return;
    }
    // 新选项：信息由调用方给（漫画单页阅读、任意查看器），不耦合 image-viewer 的
    // get_image_info；支持同步返回或 Promise。
    if (infoProvider) {
      let result;
      try {
        result = infoProvider(item, currentIndex, items);
      } catch (e) {
        infoEl.textContent = '';
        return;
      }
      Promise.resolve(result).then((value) => {
        if (token !== infoToken) return;
        paintInfo(value);
      }).catch(() => {
        if (token === infoToken) infoEl.textContent = '';
      });
      return;
    }
    // 旧路径保持不变：item 自带尺寸直接显示，否则走桥的 get_image_info。
    const path = getImageUrl(item);
    if (item.size || item.width || item.height) {
      const sizeText = item.size ? Utils.formatFileSize(item.size) : '';
      const resolutionText = (item.width && item.height) ? `${item.width} × ${item.height}` : '';
      // size/width/height 来自插件或后端数据，不是本组件的输入：只走 textContent
      // （paintInfo），拼进 innerHTML 时值里的一个引号就能在这里插标记。
      paintInfo([sizeText, resolutionText].filter(Boolean));
      return;
    }

    infoEl.innerHTML = '<div class="lightbox-info-item">读取中…</div>';
    if (typeof Bridge.call !== 'function') return;
    Bridge.call('get_image_info', path).then(data => {
      if (token !== infoToken) return;
      if (currentIndex < 0 || !items[currentIndex]) return;
      if (getImageUrl(items[currentIndex]) !== path) return;
      if (data && data.success) {
        item.size = data.size;
        item.width = data.width;
        item.height = data.height;
        paintInfo([
          Utils.formatFileSize(data.size),
          (data.width != null && data.height != null) ? `${data.width} × ${data.height}` : '',
        ].filter(Boolean));
      } else {
        infoEl.innerHTML = '<div class="lightbox-info-item">无信息</div>';
      }
    }).catch(() => {
      if (token === infoToken) infoEl.innerHTML = '<div class="lightbox-info-item">无信息</div>';
    });
  }

  function show(itemList, index) {
    items = itemList; currentIndex = index;
    opened = true;
    previousFocus = _rememberFocus();
    img.src = Bridge.originalUrl(getImageUrl(items[currentIndex]));
    overlay.classList.add('active');
    updateInfo();
    updateCounter();
    resetTransform();
    retainDecoded();
    document.addEventListener('keydown', onKey);
    img.addEventListener('wheel', onWheel, { passive: false });
    img.addEventListener('mousedown', onDragStart);
    img.addEventListener('click', onImageClick);
    document.addEventListener('mousemove', onDragMove);
    document.addEventListener('mouseup', onDragEnd);
    // 旧行为：初始焦点给关闭按钮。漫画单页阅读（advanceOnClick）把焦点给灯箱本身，
    // 这样 Enter 是“下一页”，Esc 仍是关闭；Tab 再进入左右/关闭控件。
    const initial = advanceOnClick ? overlay : closeBtn;
    if (initial && typeof initial.focus === 'function') {
      try { initial.focus({ preventScroll: true }); } catch (e) { /* 桩/旧浏览器无 focus */ }
    }
  }

  function hide() {
    const wasActive = overlay.classList.contains('active');
    const wasOpen = opened;
    opened = false;
    overlay.classList.remove('active'); img.src = '';
    infoEl.innerHTML = '';
    infoToken += 1;
    decodedPool.clear();
    document.removeEventListener('keydown', onKey);
    img.removeEventListener('wheel', onWheel);
    img.removeEventListener('mousedown', onDragStart);
    img.removeEventListener('click', onImageClick);
    document.removeEventListener('mousemove', onDragMove);
    document.removeEventListener('mouseup', onDragEnd);
    if (wasActive) {
      _restoreFocus(previousFocus);
      previousFocus = null;
    }
    // 只有"真的从打开态关闭"才回调：重复 hide()（插件清理时常见）不该重复触发。
    if (wasOpen && typeof options.onClose === 'function') {
      try {
        options.onClose();
      } catch (e) {
        console.error('[OmniBox] 灯箱 onClose 回调抛异常:', e);
      }
    }
  }

  function navigate(dir) {
    if (!items.length) return;
    currentIndex += dir;
    if (currentIndex < 0) currentIndex = items.length - 1;
    if (currentIndex >= items.length) currentIndex = 0;
    img.src = Bridge.originalUrl(getImageUrl(items[currentIndex]));
    updateInfo();
    updateCounter();
    resetTransform();
    retainDecoded();
  }

  function onImageClick() {
    if (!advanceOnClick) return;
    if (suppressClick) {
      suppressClick = false;
      return;
    }
    // 放大态下的点击用于拖拽/查看局部，不翻页，避免误触。
    if (isDragging || scale > 1) return;
    navigate(1);
  }

  function onKey(e) {
    if (!overlay.classList.contains('active')) return;
    if (e.key === 'Tab') {
      _trapTab(overlay, e);
      return;
    }
    if (e.key === 'Escape') hide();
    if (e.key === 'ArrowLeft') navigate(-1);
    if (e.key === 'ArrowRight') navigate(1);
    if (e.key === 'Enter' && advanceOnClick) {
      e.preventDefault();
      navigate(1);
    }
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
    dragDistance = 0;
    dragStart.x = e.clientX - translate.x;
    dragStart.y = e.clientY - translate.y;
    img.style.cursor = 'grabbing';
    // 拖动期间必须去掉 `transition: transform`（base.css 的 .lightbox img）。
    // 拖动是每次 pointermove 直接写 transform，元素挂着过渡时每次写入都会重新起一次
    // 0.1s 缓动，位置只能追在指针后面，表现为图像落后于鼠标且一顿一顿。
    // 松手恢复，滚轮缩放仍需那一段平滑。
    img.style.transition = 'none';
  }
  function onDragMove(e) {
    if (!isDragging) return;
    const nextX = e.clientX - dragStart.x;
    const nextY = e.clientY - dragStart.y;
    dragDistance += Math.abs(nextX - translate.x) + Math.abs(nextY - translate.y);
    translate.x = nextX;
    translate.y = nextY;
    applyTransform();
  }
  function onDragEnd() {
    if (!isDragging) return;
    isDragging = false;
    if (dragDistance > 3) suppressClick = true;
    img.style.cursor = scale > 1 ? 'grab' : 'zoom-out';
    // 恢复 base.css 里的过渡（拖动期间被置为 none）
    img.style.transition = '';
  }

  overlay.addEventListener('pointerdown', (e) => { if (e.target === overlay) hide(); });
  closeBtn.addEventListener('click', hide);
  leftArrow.addEventListener('click', (e) => { e.stopPropagation(); navigate(-1); });
  rightArrow.addEventListener('click', (e) => { e.stopPropagation(); navigate(1); });
  // role=button 的 div 没有原生键盘激活，补 Enter/Space。
  [closeBtn, leftArrow, rightArrow].forEach((el) => {
    el.addEventListener('keydown', (e) => {
      if (e.key !== 'Enter' && e.key !== ' ') return;
      e.preventDefault();
      e.stopPropagation();
      el.click();
    });
  });

  /**
   * 就地替换浏览序列（列表随后异步补齐了更完整的序列时用）。
   *
   * `items` 与 `currentIndex` 是本组件的闭包状态，外部改不了，所以必须由这里换：
   * 插件拿到完整序列后调它，并给出 `focusUrl`（当前正在看的那张）—— 新序列里
   * 找到同一张就停在原地，找不到才退回 `fallbackIndex`。这样"补齐序列"对用户
   * 是透明的：图不闪、下标不乱跳，只是左右翻能翻到更多张。
   */
  function setItems(nextItems, focusUrl, fallbackIndex) {
    items = Array.isArray(nextItems) ? nextItems : [];
    let index = -1;
    if (focusUrl) index = items.findIndex(item => getImageUrl(item) === focusUrl);
    if (index < 0) index = Math.min(Math.max(Number(fallbackIndex) || 0, 0), items.length - 1);
    currentIndex = Math.max(index, 0);
    const item = items[currentIndex];
    if (!overlay.classList.contains('active') || !item) return;
    img.src = Bridge.originalUrl(getImageUrl(item));
    resetTransform();
    updateInfo();
    updateCounter();
    retainDecoded();
  }

  return {
    show, hide, navigate, setItems, setDecodedLimit,
    // 打开态查询：插件在壳返回/导航时先判断"要不要关灯箱"，不必自己记状态。
    isOpen: () => opened,
    // 只读：当前保留了几个解码位图。给用例一个可观测点 —— 池子是闭包状态，
    // 没有它就只能靠"数 cloneNode 调用"这类脆弱手法去间接推断。
    getDecodedCount: () => decodedPool.size,
    getIndex: () => currentIndex,
    getCount: () => items.length,
  };
}

