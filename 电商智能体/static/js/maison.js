/* Maison Dorée 模板交互的中文化改写：所有选择器都做空值保护，页面上没有对应块时直接跳过。 */

(() => {
	const header = document.getElementById('header');
	if (header) {
		window.addEventListener('scroll', () => {
			header.classList.toggle('scrolled', window.scrollY > 60);
		}, { passive: true });
		header.classList.toggle('scrolled', window.scrollY > 60);
	}

	const menuToggle = document.getElementById('menuToggle');
	const mobileNav = document.getElementById('mobileNav');
	const mobileOverlay = document.getElementById('mobileOverlay');
	const mobileNavClose = document.getElementById('mobileNavClose');

	if (menuToggle && mobileNav && mobileOverlay) {
		const setOpen = (open) => {
			mobileNav.classList.toggle('active', open);
			mobileOverlay.classList.toggle('active', open);
			document.body.style.overflow = open ? 'hidden' : '';
		};
		menuToggle.addEventListener('click', () => setOpen(true));
		mobileOverlay.addEventListener('click', () => setOpen(false));
		if (mobileNavClose) mobileNavClose.addEventListener('click', () => setOpen(false));
		mobileNav.querySelectorAll('a').forEach((link) => link.addEventListener('click', () => setOpen(false)));
	}

	const heroOffset = () => (header ? Math.min(header.offsetHeight, 120) : 0);
	document.querySelectorAll('a[href^="#"]:not(.mobile-nav-links a)').forEach((anchor) => {
		anchor.addEventListener('click', (event) => {
			const href = anchor.getAttribute('href');
			if (!href || href === '#') return;
			const target = document.querySelector(href);
			if (!target) return;
			event.preventDefault();
			window.scrollTo({ top: target.getBoundingClientRect().top + window.scrollY - heroOffset(), behavior: 'smooth' });
		});
	});

	const slides = document.querySelectorAll('.hero-slide');
	const heroTitle = document.getElementById('heroTitle');
	const heroPrice = document.getElementById('heroPrice');
	if (slides.length > 1 && heroTitle && heroPrice) {
		let current = 0;
		setInterval(() => {
			slides[current].classList.remove('active');
			current = (current + 1) % slides.length;
			heroTitle.style.opacity = '0';
			heroPrice.style.opacity = '0';
			slides[current].classList.add('active');
			setTimeout(() => {
				heroTitle.textContent = slides[current].dataset.title || '';
				heroPrice.textContent = slides[current].dataset.price || '';
				heroTitle.style.opacity = '1';
				heroPrice.style.opacity = '1';
			}, 450);
		}, 5000);
	}

	const reduceMotion = window.matchMedia('(prefers-reduced-motion: reduce)').matches;
	// 悬浮客服窗是固定层，不参与进场动画，否则它的初始位移会让拖动/存档几何失准
	const sections = document.querySelectorAll('section:not(.hero):not(.xiaoma-panel)');
	if (!reduceMotion && 'IntersectionObserver' in window && sections.length) {
		const observer = new IntersectionObserver((entries) => {
			entries.forEach((entry) => {
				if (!entry.isIntersecting) return;
				entry.target.style.opacity = '1';
				entry.target.style.transform = 'translateY(0)';
				observer.unobserve(entry.target);
			});
		}, { threshold: 0.08, rootMargin: '0px 0px -40px 0px' });
		sections.forEach((section) => {
			if (section.querySelector('.hero-slide')) return;
			section.style.opacity = '0';
			section.style.transform = 'translateY(26px)';
			section.style.transition = 'opacity .7s ease, transform .7s ease';
			observer.observe(section);
		});
	}
})();
