/**
 * This product includes software developed by flotiarenor.Copyright 2026 flotiarenor
 * 共享运行时 · 分页组件（createPagination）：首末页 + 当前页附近的页码、省略号与当前页标记。
 *
 * 渲染前清空容器（插件常在 render 之前自己清一次），并按「容器内原本是否有焦点」决定
 * 重绘后要不要还焦点 —— 否则键盘用户每翻一页就被甩回 body。
 */

// ==================== 分页组件 ====================
function createPagination(container, options = {}) {
  if (container && typeof container.setAttribute === 'function') {
    if (!container.getAttribute('role')) container.setAttribute('role', 'navigation');
    if (!container.getAttribute('aria-label')) container.setAttribute('aria-label', options.label || '分页');
  }
  // 插件常在 render 之前自己 `container.innerHTML = ''`（image-viewer 就是这样），
  // 焦点会先掉到 body。记住容器内最后聚焦的链接，用它判断“这次重绘该不该还焦点”。
  let lastFocusedLink = null;
  if (container && typeof container.addEventListener === 'function') {
    container.addEventListener('focusin', (event) => {
      if (event.target && container.contains && container.contains(event.target)) {
        lastFocusedLink = event.target;
      }
    });
  }

  function render(currentPage, totalPages) {
    const active = document.activeElement;
    const hadFocus = !!(container && typeof container.contains === 'function' && (
      container.contains(active)
      || (lastFocusedLink && lastFocusedLink.isConnected === false
          && (!active || active === document.body))
    ));
    container.innerHTML = '';
    if (totalPages <= 1) return;
    const delta = 2, pages = [];
    for (let i = 1; i <= totalPages; i++) {
      if (i === 1 || i === totalPages || (i >= currentPage - delta && i <= currentPage + delta)) {
        pages.push(i);
      }
    }
    container.appendChild(makeLink('«', 1, currentPage === 1, false, '第一页'));
    container.appendChild(makeLink('‹', currentPage - 1, currentPage === 1, false, '上一页'));
    let last = 0;
    pages.forEach(page => {
      if (last + 1 < page) {
        const ellipsis = document.createElement('span');
        ellipsis.className = 'ellipsis'; ellipsis.textContent = '...';
        ellipsis.setAttribute('aria-hidden', 'true');
        container.appendChild(ellipsis);
      }
      container.appendChild(makeLink(page, page, false, page === currentPage, `第 ${page} 页`));
      last = page;
    });
    container.appendChild(makeLink('›', currentPage + 1, currentPage === totalPages, false, '下一页'));
    container.appendChild(makeLink('»', totalPages, currentPage === totalPages, false, '最后一页'));
    if (hadFocus) {
      const current = container.querySelector('[aria-current="page"]')
        || container.querySelector('a:not(.disabled)');
      if (current && typeof current.focus === 'function') {
        try { current.focus({ preventScroll: true }); } catch (e) { /* 旧浏览器 */ }
        lastFocusedLink = current;
      }
    }
  }

  function makeLink(text, page, isDisabled, isCurrent, label) {
    const el = document.createElement('a');
    el.textContent = text;
    el.setAttribute('aria-label', label || text);
    if (isCurrent) {
      el.classList.add('current');
      el.setAttribute('aria-current', 'page');
      el.tabIndex = -1;
      el.setAttribute('href', '#');
    } else if (isDisabled) {
      el.classList.add('disabled');
      el.setAttribute('aria-disabled', 'true');
      el.tabIndex = -1;
    } else {
      el.setAttribute('href', '#');
      el.addEventListener('click', (event) => {
        if (event && typeof event.preventDefault === 'function') event.preventDefault();
        if (options.onPageChange) options.onPageChange(page);
      });
      // 分页链接是 a[href]：Enter 原生可用；补 Space 与 button 一致。
      el.addEventListener('keydown', (event) => {
        if (event.key !== ' ' && event.key !== 'Spacebar') return;
        event.preventDefault();
        el.click();
      });
    }
    return el;
  }

  return { render };
}

