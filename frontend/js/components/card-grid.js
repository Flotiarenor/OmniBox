/**
 * 通用卡片网格组件
 * @param {HTMLElement} container
 * @param {Object} options
 *   - cardRenderer: (item) => { image, title, subtitle, badge, badgeClass }
 *   - onClick: (item, index) => void
 *   - onContextMenu: (item, index, event) => void
 */
function createCardGrid(container, options = {}) {
    function renderCard(item, index) {
        const data = options.cardRenderer
            ? options.cardRenderer(item)
            : { image: '', title: '' };

        const card = document.createElement('div');
        card.className = 'manga-card';

        card.innerHTML = `
            <div class="manga-cover">
                <img src="${data.image}" loading="lazy"
                     onerror="this.style.display='none'">
                ${data.badge ? `<span class="manga-badge ${data.badgeClass || ''}">${data.badge}</span>` : ''}
            </div>
            <div class="manga-info">
                <p class="manga-title" title="${data.title}">${data.title}</p>
                ${data.subtitle ? `<p class="manga-author">${data.subtitle}</p>` : ''}
            </div>
        `;

        card.addEventListener('click', () => options.onClick?.(item, index));
        card.addEventListener('contextmenu', (e) => {
            e.preventDefault();
            options.onContextMenu?.(item, index, e);
        });

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