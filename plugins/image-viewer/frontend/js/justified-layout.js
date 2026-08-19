// ===== Justified 图片瀑布布局计算 =====
class JustifiedLayout {
    /**
     * @param {Array<{url:string,width?:number,height?:number}>} images
     * @param {number} containerWidth
     * @param {number} targetHeight
     * @param {number} gap
     * @returns {{cards: Array<{url:string,x:number,y:number,w:number,h:number}>, totalHeight:number}}
     */
    static compute(images, containerWidth, targetHeight, gap = 5) {
        const rows = [];
        let currentRow = { items: [], width: 0 };

        images.forEach(img => {
            const ratio = (img.width || 1) / (img.height || 1);
            const itemWidth = ratio * targetHeight;
            currentRow.items.push({ ...img, ratio, itemWidth });
            currentRow.width += itemWidth + gap;
            if (currentRow.width - gap >= containerWidth) {
                rows.push(currentRow);
                currentRow = { items: [], width: 0 };
            }
        });
        if (currentRow.items.length > 0) rows.push(currentRow);

        let currentTop = 0;
        const cards = [];
        rows.forEach(row => {
            const isLastRow = row === rows[rows.length - 1] && row.width - gap < containerWidth;
            const itemWidthSum = row.width - gap - (row.items.length - 1) * gap;
            const actualRowHeight = isLastRow
                ? targetHeight
                : ((containerWidth - (row.items.length - 1) * gap) / itemWidthSum) * targetHeight;

            let currentLeft = 0;
            row.items.forEach(item => {
                const w = item.ratio * actualRowHeight;
                const h = actualRowHeight;
                cards.push({ url: item.url, x: currentLeft, y: currentTop, w, h });
                currentLeft += w + gap;
            });
            currentTop += actualRowHeight + gap;
        });

        return { cards, totalHeight: Math.max(0, currentTop - gap) };
    }
}
