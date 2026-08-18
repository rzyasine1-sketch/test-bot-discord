document.addEventListener('DOMContentLoaded', () => {
    const btn = document.getElementById('navAvatarBtn');
    const menu = document.getElementById('navDropdown');
    if (!btn || !menu) return;

    const close = () => {
        menu.classList.remove('open');
        btn.setAttribute('aria-expanded', 'false');
    };

    btn.addEventListener('click', (event) => {
        event.stopPropagation();
        const isOpen = menu.classList.toggle('open');
        btn.setAttribute('aria-expanded', isOpen ? 'true' : 'false');
    });

    document.addEventListener('click', (event) => {
        if (!menu.contains(event.target) && event.target !== btn) close();
    });

    document.addEventListener('keydown', (event) => {
        if (event.key === 'Escape') close();
    });
});
