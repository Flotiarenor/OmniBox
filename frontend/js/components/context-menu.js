/**
 * 右键菜单组件
 * @param {Object} options
 *   - items: [{label, action, danger?}]
 *   - onSelect: (action, target) => void
 */
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

    // 点击其他地方关闭
    document.addEventListener('click', () => { menu.style.display = 'none'; });

    function show(x, y, targetData) {
        menu._targetData = targetData;
        menu.style.top = `${y}px`;
        menu.style.left = `${x}px`;
        menu.style.display = 'block';
    }

    function getTargetData() {
        return menu._targetData;
    }

    return { show, getTargetData, element: menu };
}