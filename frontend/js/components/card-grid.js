/**
 * 通用卡片网格组件
 * @param {HTMLElement} container
 * @param {Object} options
 *   - cardRenderer: (item) => { image, title, subtitle, badge, badgeClass, extraHtml }
 *   - onClick: (item, index) => void
 *   - onContextMenu: (item, index, event) => void
 */
function createCardGrid(container, options = {}) {
    function renderCard(item) {
        const data = options.cardRenderer(item);
        const card = document.createElement('div');
        card.className = 'manga-card';
        card.dataset.folderName = item.folder_name || ''; // 存储文件夹名，方便事件委托
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
            // 如果点击的是额外元素(如星星)，不触发卡片点击
            if (e.target.closest('.card-extra-item')) return;
            if (options.onClick) options.onClick(item, Array.from(container.children).indexOf(card));
        });
        if (options.onContextMenu) {
            card.addEventListener('contextmenu', (e) => {
                e.preventDefault();
                options.onContextMenu(item, Array.from(container.children).indexOf(card), e);
            });
        }
        
        // 关键修复：只返回节点，由外层统一挂载
        return card; 
    }

    return {
        render(items) {
            container.innerHTML = '';
            items.forEach((item, i) => container.appendChild(renderCard(item, i)));
        },
        append(items) {
            items.forEach((item, i) => container.appendChild(renderCard(item, i)));
        }
    };
}