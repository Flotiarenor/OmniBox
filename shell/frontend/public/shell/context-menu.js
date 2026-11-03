/**
 * This product includes software developed by flotiarenor.Copyright 2026 flotiarenor
 * 共享运行时 · 右键菜单（createContextMenu）：按条目渲染菜单，选中后回传动作，
 * 并用 getTargetData() 交出这次右键指向的数据。
 *
 * 菜单节点挂到 document.body（相对视口定位，不被滚动容器裁剪），关闭时机由这里的全局
 * pointerdown 监听与 Esc 处理；焦点归还复用弹窗层的工具函数。
 */

// ==================== 右键菜单组件 ====================
function createContextMenu(options = {}) {
  const menu = document.createElement('ul');
  menu.className = 'context-menu';
  menu.setAttribute('role', 'menu');
  menu.setAttribute('aria-label', options.label || '操作菜单');
  menu.tabIndex = -1;
  let previousFocus = null;

  function decorate() {
    Array.from(menu.children || []).forEach((li) => {
      if (!li || !li.tagName) return;
      const separator = String(li.className || '').includes('nr-menu-sep')
        || !(li.dataset && li.dataset.action);
      if (separator) {
        li.setAttribute('role', 'separator');
        li.setAttribute('aria-hidden', 'true');
        li.tabIndex = -1;
      } else {
        li.setAttribute('role', 'menuitem');
        li.tabIndex = -1;
      }
    });
  }

  (options.items || []).forEach(item => {
    const li = document.createElement('li');
    li.textContent = item.label;
    li.dataset.action = item.action;
    li.setAttribute('role', 'menuitem');
    li.tabIndex = -1;
    if (item.danger) li.classList.add('danger');
    li.addEventListener('click', () => {
      hide(false);
      if (options.onSelect) options.onSelect(item.action);
    });
    menu.appendChild(li);
  });
  document.body.appendChild(menu);

  function hide(restoreFocus) {
    if (menu.style.display === 'none') return;
    menu.style.display = 'none';
    if (restoreFocus) {
      _restoreFocus(previousFocus);
      previousFocus = null;
    }
  }

  function show(x, y, targetData) {
    previousFocus = _rememberFocus();
    decorate();
    menu._targetData = targetData;
    menu.style.display = 'block';
    menu.style.left = '0px';
    menu.style.top = '0px';
    // 视口夹取：先量实际尺寸再定位，右/下边缘溢出时翻到指针的左侧/上方。
    const rect = typeof menu.getBoundingClientRect === 'function'
      ? menu.getBoundingClientRect() : { width: 0, height: 0 };
    const margin = 8;
    const vw = Number(window.innerWidth) || 0;
    const vh = Number(window.innerHeight) || 0;
    let left = Number(x) || 0;
    let top = Number(y) || 0;
    if (vw > 0 && rect.width > 0) {
      left = Math.min(left, Math.max(margin, vw - rect.width - margin));
      left = Math.max(margin, left);
    }
    if (vh > 0 && rect.height > 0) {
      if (top + rect.height + margin > vh) top = top - rect.height;
      top = Math.min(top, Math.max(margin, vh - rect.height - margin));
      top = Math.max(margin, top);
    }
    menu.style.left = `${Math.round(left)}px`;
    menu.style.top = `${Math.round(top)}px`;
    const first = menu.querySelector('li[role="menuitem"]');
    if (first && typeof first.focus === 'function') {
      try { first.focus({ preventScroll: true }); } catch (e) { /* 旧浏览器 */ }
    } else if (typeof menu.focus === 'function') {
      try { menu.focus({ preventScroll: true }); } catch (e) { /* 旧浏览器 */ }
    }
  }

  menu.addEventListener('keydown', (event) => {
    const items = Array.from(menu.querySelectorAll('li[role="menuitem"]'));
    const active = document.activeElement;
    const index = items.indexOf(active);
    if (event.key === 'Escape') {
      event.preventDefault();
      event.stopPropagation();
      hide(true);
      return;
    }
    if (event.key === 'ArrowDown' || event.key === 'ArrowUp') {
      event.preventDefault();
      if (!items.length) return;
      const step = event.key === 'ArrowDown' ? 1 : -1;
      const next = index < 0
        ? (step > 0 ? 0 : items.length - 1)
        : (index + step + items.length) % items.length;
      try { items[next].focus(); } catch (e) { /* 元素已失效 */ }
      return;
    }
    if (event.key === 'Home' || event.key === 'End') {
      event.preventDefault();
      if (!items.length) return;
      const target = event.key === 'Home' ? items[0] : items[items.length - 1];
      try { target.focus(); } catch (e) { /* 元素已失效 */ }
      return;
    }
    if (event.key === 'Enter' || event.key === ' ') {
      if (active && active.getAttribute && active.getAttribute('role') === 'menuitem') {
        event.preventDefault();
        active.click();
      }
    }
  });

  // 点外部关闭；点菜单项由上面的 li/插件处理器自行执行动作。
  document.addEventListener('pointerdown', (event) => {
    if (menu.style.display !== 'block') return;
    if (event.target && menu.contains && menu.contains(event.target)) return;
    hide(true);
  });
  menu.addEventListener('click', (event) => {
    if (event.target && event.target.closest && event.target.closest('li')) hide(false);
  });

  function getTargetData() { return menu._targetData; }

  return { show, hide, getTargetData, element: menu };
}

// 注：这里曾有一个 `createCardGrid()`，实际采用数为 0，且它渲染的
// `.manga-card / .manga-cover / .manga-info …` 在壳与任何插件样式里都没有定义 ——
// 它是某个插件旧实现的视觉词汇被搬进"共享"组件，谁用谁拿到无样式 DOM，已删除。
// 卡片网格属各插件的内容区布局（image-viewer 用瀑布流、manga-library / media-player
// 用自适应栅格），由插件自建；见 docs/plugin-ui-guide.md §5。
