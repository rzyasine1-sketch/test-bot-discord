/**
 * gallery.js — Discord channel gallery + lightbox crop (GIF-safe)
 */
(function () {
    'use strict';

    const state = {
        category: 'rome1',
        categories: [],
        items: [],
        before: null,
        loading: false,
        hasMore: true,
        cropper: null,
        currentItem: null,
        userCoins: 0,
        hideBalance: false,
    };

    const els = {};

    const SHAPE_CONFIG = {
        circle: {
            aspectRatio: 1,
            outputWidth: 512,
            outputHeight: 512,
            previewW: 180,
            previewH: 180,
            mask: 'circle',
            previewClass: 'preview-frame--circle',
        },
        rectangle: {
            aspectRatio: 16 / 9,
            outputWidth: 1200,
            outputHeight: 675,
            previewW: 240,
            previewH: 135,
            mask: 'square',
            previewClass: 'preview-frame--rectangle',
        },
    };

    function $(id) { return document.getElementById(id); }

    function formatNumber(n) {
        return Number(n).toLocaleString();
    }

    function endpointForCategory(categoryId) {
        return categoryId === 'rome9'
            ? '/api/anime-banners'
            : `/api/gallery/${encodeURIComponent(categoryId)}`;
    }

    function getCategoryMeta(categoryId) {
        return state.categories.find((cat) => cat.id === categoryId) || null;
    }

    function setGalleryStatus(message = '', visible = false) {
        if (!els.galleryStatus) return;
        els.galleryStatus.textContent = message;
        els.galleryStatus.classList.toggle('hidden', !visible || !message);
    }

    function displayUrl(item) {
        if (!item) return '';
        if (item.is_gif) return item.url || item.proxy_url;
        return item.proxy_url || item.url;
    }

    function escapeAttr(value) {
        return String(value || '')
            .replace(/&/g, '&amp;')
            .replace(/"/g, '&quot;')
            .replace(/</g, '&lt;');
    }

    function showToast(message, type = 'info') {
        const colors = {
            success: 'paper-green',
            error: 'paper-pink',
            info: 'paper-blue',
            warning: 'paper-orange',
        };
        const toast = document.createElement('div');
        toast.className = `toast ${colors[type] || colors.info} tilt-${(Math.floor(Math.random() * 3) + 1)}`;
        toast.style.color = '#f0f0f5';
        toast.textContent = message;
        els.toastContainer.appendChild(toast);
        setTimeout(() => {
            toast.style.opacity = '0';
            toast.style.transform = 'translateX(120%)';
            setTimeout(() => toast.remove(), 350);
        }, 3800);
    }

    function setCoinLabel(value) {
        if (!els.navCoinBalance) return;
        els.navCoinBalance.textContent = state.hideBalance ? '••••' : formatNumber(value);
        if (els.modalBalance) els.modalBalance.textContent = formatNumber(value);
    }

    async function loadCategories() {
        const res = await fetch('/api/gallery/categories');
        const data = await res.json();
        if (!data.success) return;
        state.categories = data.categories;
        renderTabs();
    }

    function renderTabs() {
        els.categoryTabs.innerHTML = state.categories.map((cat) => `
            <button type="button"
                class="category-tab${cat.id === state.category ? ' active' : ''}"
                data-category="${cat.id}"
                title="${cat.coming_soon ? cat.label + ' (coming soon)' : cat.label}">
                ${cat.label}${cat.coming_soon ? ' •' : ''}
            </button>
        `).join('');

        els.categoryTabs.querySelectorAll('.category-tab').forEach((btn) => {
            btn.addEventListener('click', () => switchCategory(btn.dataset.category));
        });
    }

    function switchCategory(categoryId) {
        if (state.loading || state.category === categoryId) return;
        state.category = categoryId;
        state.items = [];
        state.before = null;
        state.hasMore = true;
        renderTabs();
        loadGallery(true);
    }

    function renderSkeletons(count = 8) {
        els.masonryGrid.innerHTML = Array(count).fill(0).map(() => `
            <div class="masonry-item masonry-item--avatar" style="opacity:0.6">
                <div class="masonry-item__img-wrap skeleton"></div>
                <div class="masonry-item__meta"><span class="skeleton" style="width:60px;height:14px;display:inline-block"></span></div>
            </div>
        `).join('');
    }

    async function loadGallery(reset = false) {
        if (state.loading || (!state.hasMore && !reset)) return;
        state.loading = true;
        const categoryMeta = getCategoryMeta(state.category);

        if (reset) {
            renderSkeletons();
            els.emptyState.classList.add('hidden');
            els.loadMoreWrap.classList.add('hidden');
            setGalleryStatus(`Loading ${categoryMeta ? categoryMeta.label : 'gallery'}...`, true);
        } else {
            els.loadMoreBtn.disabled = true;
            els.loadMoreBtn.textContent = 'Loading…';
            setGalleryStatus('Loading more images...', true);
        }

        try {
            const params = new URLSearchParams();
            if (state.before) params.set('before', state.before);

            const endpoint = endpointForCategory(state.category);
            const requestUrl = params.toString() ? `${endpoint}?${params}` : endpoint;
            const res = await fetch(requestUrl);
            const data = await res.json();

            if (data.coming_soon) {
                state.items = [];
                els.masonryGrid.innerHTML = '';
                els.emptyTitle.textContent = data.label || (categoryMeta ? categoryMeta.label : 'Coming Soon');
                els.emptyCopy.textContent = data.message || 'This slot is reserved for the anime banner bot. Set DISCORD_CHANNEL_ROME9 when ready.';
                els.emptyState.classList.remove('hidden');
                els.loadMoreWrap.classList.add('hidden');
                state.hasMore = false;
                setGalleryStatus('', false);
                return;
            }

            if (!res.ok || !data.success) {
                if (reset) els.masonryGrid.innerHTML = '';
                showToast(data.message || 'Failed to load gallery.', 'error');
                els.emptyTitle.textContent = categoryMeta ? `${categoryMeta.label} Unavailable` : 'No Images Yet';
                els.emptyCopy.textContent = data.message || 'This category is empty or not configured.';
                els.emptyState.classList.remove('hidden');
                state.hasMore = false;
                setGalleryStatus('Could not load images right now.', true);
                return;
            }

            if (reset) {
                state.items = data.results;
            } else {
                state.items.push(...data.results);
            }

            state.before = data.next_before || null;
            state.hasMore = Boolean(data.has_more && data.next_before);

            renderItems(data.results, !reset);
            els.emptyTitle.textContent = 'No Images Yet';
            els.emptyCopy.textContent = 'This category is empty or not configured.';
            els.emptyState.classList.toggle('hidden', state.items.length > 0);
            els.loadMoreWrap.classList.toggle('hidden', !state.hasMore);
            setGalleryStatus(state.items.length ? '' : `No images found in ${data.label || (categoryMeta ? categoryMeta.label : 'this category')}.`, state.items.length === 0);
        } catch (err) {
            console.error(err);
            showToast('Network error loading gallery.', 'error');
            if (reset) els.masonryGrid.innerHTML = '';
            els.emptyTitle.textContent = categoryMeta ? `${categoryMeta.label} Offline` : 'Gallery Offline';
            els.emptyCopy.textContent = 'Check the Discord bot token, channel permissions, and network connection, then try again.';
            els.emptyState.classList.remove('hidden');
            state.hasMore = false;
            setGalleryStatus('Network error while fetching images.', true);
        } finally {
            state.loading = false;
            els.loadMoreBtn.disabled = false;
            els.loadMoreBtn.textContent = 'Load Older Images';
        }
    }

    function renderItems(items, append = false) {
        if (!append) els.masonryGrid.innerHTML = '';

        items.forEach((item) => {
            const src = displayUrl(item);
            const card = document.createElement('article');
            card.className = `masonry-item masonry-item--${item.layout}`;
            card.dataset.itemId = item.id;
            card.innerHTML = `
                <div class="masonry-item__img-wrap">
                    <img class="masonry-item__img"
                        src="${escapeAttr(src)}"
                        alt="${escapeAttr(item.filename || 'Gallery image')}"
                        loading="lazy"
                        referrerpolicy="no-referrer">
                </div>
                <div class="masonry-item__meta">
                    <span>${item.is_gif ? 'GIF' : 'IMG'}</span>
                    <span class="masonry-item__price">🪙 ${formatNumber(item.price)}</span>
                </div>
            `;
            card.addEventListener('click', () => openLightbox(item));
            els.masonryGrid.appendChild(card);
        });
    }

    function getShapeConfig(item) {
        return SHAPE_CONFIG[item.shape] || SHAPE_CONFIG.circle;
    }

    function openLightbox(item) {
        state.currentItem = item;
        const cfg = getShapeConfig(item);
        const src = displayUrl(item);

        els.lightboxTitle.textContent = item.filename || 'Preview';
        els.lightboxSubtitle.textContent = `${item.layout === 'banner' ? 'Banner' : 'Avatar'} · ${item.is_gif ? 'Animated GIF' : 'Image'} · 🪙 ${formatNumber(item.price)}`;
        els.modalPrice.textContent = formatNumber(item.price);
        els.modalBalance.textContent = formatNumber(state.userCoins);

        const preview = els.previewFrame;
        preview.className = `preview-frame ${cfg.previewClass}`;
        preview.style.width = `${cfg.previewW}px`;
        preview.style.height = `${cfg.previewH}px`;
        els.previewDimensions.textContent = item.is_gif ? 'Original animation' : `${cfg.outputWidth} × ${cfg.outputHeight}`;

        els.lightbox.classList.toggle('is-gif', Boolean(item.is_gif));
        els.lightbox.classList.add('active');
        document.body.style.overflow = 'hidden';

        if (state.cropper) {
            state.cropper.destroy();
            state.cropper = null;
        }

        if (item.is_gif) {
            els.gifLiveImage.src = src;
            els.cropperPreviewInner.innerHTML = `<img src="${escapeAttr(src)}" alt="GIF preview" referrerpolicy="no-referrer" style="width:100%;height:100%;object-fit:cover">`;
            return;
        }

        els.gifLiveImage.removeAttribute('src');
        const img = els.cropperImage;
        img.src = src;
        img.onload = () => {
            state.cropper = new Cropper(img, {
                aspectRatio: cfg.aspectRatio,
                viewMode: 1,
                dragMode: 'move',
                autoCropArea: 0.9,
                restore: false,
                guides: true,
                center: true,
                highlight: false,
                cropBoxMovable: true,
                cropBoxResizable: true,
                toggleDragModeOnDblclick: false,
                preview: '#cropperPreviewInner',
            });
        };
    }

    function closeLightbox() {
        els.lightbox.classList.remove('active', 'is-gif');
        document.body.style.overflow = '';
        if (state.cropper) {
            state.cropper.destroy();
            state.cropper = null;
        }
        els.gifLiveImage.removeAttribute('src');
        els.cropperPreviewInner.innerHTML = '';
        state.currentItem = null;
    }

    function getCroppedCanvas() {
        if (!state.cropper || !state.currentItem) return null;
        const cfg = getShapeConfig(state.currentItem);
        return state.cropper.getCroppedCanvas({
            width: cfg.outputWidth,
            height: cfg.outputHeight,
            fillColor: '#00000000',
            imageSmoothingEnabled: true,
            imageSmoothingQuality: 'high',
        });
    }

    async function purchaseAndSave() {
        const item = state.currentItem;
        if (!item) return;

        if (state.userCoins < item.price) {
            showToast('Insufficient coins!', 'error');
            return;
        }

        els.purchaseBtn.disabled = true;
        els.purchaseBtn.textContent = 'Processing…';

        try {
            const payload = {
                item_type: item.item_type,
                price: item.price,
                source_url: item.url,
                is_gif: Boolean(item.is_gif),
            };

            if (item.is_gif) {
                payload.mask_type = 'square';
            } else {
                if (!state.cropper) {
                    showToast('Could not crop image.', 'error');
                    return;
                }
                const canvas = getCroppedCanvas();
                if (!canvas) {
                    showToast('Could not crop image.', 'error');
                    return;
                }
                payload.image_data = canvas.toDataURL('image/png');
                payload.mask_type = getShapeConfig(item).mask;
            }

            const res = await fetch('/shop/purchase', {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify(payload),
            });
            const data = await res.json();
            if (data.success) {
                state.userCoins = (data.new_balance ?? data.remaining_coins);
                setCoinLabel(state.userCoins);
                showToast(data.message || 'Saved to inventory!', 'success');
                closeLightbox();
            } else {
                showToast(data.message || 'Purchase failed.', 'error');
            }
        } catch (err) {
            showToast('Purchase failed.', 'error');
        } finally {
            els.purchaseBtn.disabled = false;
            els.purchaseBtn.textContent = '🛒 Purchase & Save';
        }
    }

    async function downloadCropped() {
        const item = state.currentItem;
        if (!item) return;

        if (state.userCoins < item.price) {
            showToast('Insufficient coins to download.', 'error');
            return;
        }

        els.downloadBtn.disabled = true;
        try {
            const payload = {
                item_type: item.item_type,
                price: item.price,
                source_url: item.url,
                is_gif: Boolean(item.is_gif),
                filename: item.filename || 'discord-image',
            };

            if (!item.is_gif) {
                const canvas = getCroppedCanvas();
                if (!canvas) {
                    showToast('Could not prepare download.', 'error');
                    return;
                }
                payload.image_data = canvas.toDataURL('image/png');
                payload.mask_type = getShapeConfig(item).mask;
            }

            const res = await fetch('/api/gallery/download', {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify(payload),
            });
            const data = await res.json();
            if (!data.success) {
                showToast(data.message || 'Download failed.', 'error');
                return;
            }

            state.userCoins = (data.new_balance ?? data.remaining_coins);
            setCoinLabel(state.userCoins);

            if (item.is_gif) {
                const link = document.createElement('a');
                link.href = item.url;
                link.target = '_blank';
                link.rel = 'noopener';
                link.download = item.filename || 'image.gif';
                link.click();
            } else {
                const canvas = getCroppedCanvas();
                const link = document.createElement('a');
                link.href = canvas.toDataURL('image/png');
                link.download = `${(item.filename || 'image').replace(/\.[^.]+$/, '')}-cropped.png`;
                link.click();
            }
            showToast('Download started!', 'success');
        } catch (err) {
            showToast('Download failed.', 'error');
        } finally {
            els.downloadBtn.disabled = false;
        }
    }

    async function copyUrl() {
        const item = state.currentItem;
        if (!item) return;
        try {
            await navigator.clipboard.writeText(item.url);
            showToast('Discord URL copied!', 'success');
        } catch (err) {
            showToast('Could not copy URL.', 'error');
        }
    }

    async function claimDaily() {
        const btn = $('claimDailyBtn');
        if (btn) btn.disabled = true;
        try {
            const res = await fetch('/api/user/daily', { method: 'POST' });
            const data = await res.json();
            if (data.success) {
                state.userCoins = data.coins;
                setCoinLabel(state.userCoins);
                showToast(data.message, 'success');
            } else {
                showToast(data.message || 'Already claimed today.', 'warning');
            }
        } catch (err) {
            showToast('Could not claim daily reward.', 'error');
        } finally {
            if (btn) btn.disabled = false;
        }
    }

    function setupInfiniteScroll() {
        const observer = new IntersectionObserver((entries) => {
            if (entries[0].isIntersecting && state.hasMore && !state.loading) {
                loadGallery(false);
            }
        }, { rootMargin: '200px' });
        observer.observe(els.loadSentinel);
    }

    function init(options) {
        state.userCoins = options.userCoins || 0;
        state.hideBalance = Boolean(options.hideBalance);

        els.categoryTabs = $('categoryTabs');
        els.galleryStatus = $('galleryStatus');
        els.masonryGrid = $('masonryGrid');
        els.emptyState = $('emptyState');
        els.emptyTitle = $('emptyTitle');
        els.emptyCopy = $('emptyCopy');
        els.loadMoreWrap = $('loadMoreWrap');
        els.loadMoreBtn = $('loadMoreBtn');
        els.loadSentinel = $('loadSentinel');
        els.lightbox = $('lightbox');
        els.lightboxTitle = $('lightboxTitle');
        els.lightboxSubtitle = $('lightboxSubtitle');
        els.cropperImage = $('cropperImage');
        els.gifLiveImage = $('gifLiveImage');
        els.previewFrame = $('previewFrame');
        els.cropperPreviewInner = $('cropperPreviewInner');
        els.previewDimensions = $('previewDimensions');
        els.modalPrice = $('modalPrice');
        els.modalBalance = $('modalBalance');
        els.navCoinBalance = $('navCoinBalance');
        els.purchaseBtn = $('purchaseBtn');
        els.downloadBtn = $('downloadBtn');
        els.copyUrlBtn = $('copyUrlBtn');
        els.toastContainer = $('toastContainer');

        $('closeLightbox').addEventListener('click', closeLightbox);
        els.lightbox.querySelector('.lightbox__overlay').addEventListener('click', closeLightbox);
        els.purchaseBtn.addEventListener('click', purchaseAndSave);
        els.downloadBtn.addEventListener('click', downloadCropped);
        els.copyUrlBtn.addEventListener('click', copyUrl);
        els.loadMoreBtn.addEventListener('click', () => loadGallery(false));
        const dailyBtn = $('claimDailyBtn');
        if (dailyBtn) dailyBtn.addEventListener('click', claimDaily);

        document.addEventListener('keydown', (e) => {
            if (e.key === 'Escape') closeLightbox();
        });

        loadCategories().then(() => loadGallery(true));
        setupInfiniteScroll();
    }

    window.Gallery = {
        init,
        showToast,
        rotateCrop: (deg) => state.cropper && state.cropper.rotate(deg),
        resetCrop: () => state.cropper && state.cropper.reset(),
    };
})();
