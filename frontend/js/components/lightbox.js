/**
 * 灯箱组件
 * @param {Object} options
 *   - onNavigate: (direction) => {url, ...}
 *   - getImageUrl: (item) => string
 */
function createLightbox(options = {}) {
    const getImageUrl = options.getImageUrl || ((item) => item.url);

    // 创建DOM
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
    let currentIndex = -1;
    let items = [];

    function resetTransform() {
        scale = 1;
        translate = { x: 0, y: 0 };
        applyTransform();
    }

    function applyTransform() {
        img.style.transform = `translate(${translate.x}px, ${translate.y}px) scale(${scale})`;
        img.style.cursor = scale > 1 ? 'grab' : 'zoom-out';
    }

    function show(itemList, index) {
        items = itemList;
        currentIndex = index;
        img.src = bridge.originalUrl(getImageUrl(items[currentIndex]));
        overlay.classList.add('active');
        resetTransform();
        document.addEventListener('keydown', onKey);
        img.addEventListener('wheel', onWheel, { passive: false });
        img.addEventListener('mousedown', onDragStart);
        document.addEventListener('mousemove', onDragMove);
        document.addEventListener('mouseup', onDragEnd);
    }

    function hide() {
        overlay.classList.remove('active');
        img.src = '';
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
        img.src = bridge.originalUrl(getImageUrl(items[currentIndex]));
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
        e.preventDefault();
        isDragging = true;
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

    // 事件绑定
    overlay.addEventListener('click', (e) => {
        if (e.target === overlay) hide();
    });
    closeBtn.addEventListener('click', hide);
    leftArrow.addEventListener('click', (e) => { e.stopPropagation(); navigate(-1); });
    rightArrow.addEventListener('click', (e) => { e.stopPropagation(); navigate(1); });

    return { show, hide, navigate };
}