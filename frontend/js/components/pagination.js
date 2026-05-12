/**
 * 分页组件
 * @param {HTMLElement} container - 分页容器
 * @param {Object} options
 *   - onPageChange: (page) => void
 */
function createPagination(container, options = {}) {
    function render(currentPage, totalPages) {
        container.innerHTML = '';
        if (totalPages <= 1) return;

        const delta = 2;
        const pages = [];

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
                ellipsis.className = 'ellipsis';
                ellipsis.textContent = '...';
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
            el.addEventListener('click', () => {
                if (options.onPageChange) options.onPageChange(page);
            });
        }
        return el;
    }

    return { render };
}