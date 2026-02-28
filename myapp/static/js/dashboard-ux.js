(function () {
    function formatMetricValue(value, format) {
        const rounded = Math.round(value);
        if (format === 'number') {
            return rounded.toLocaleString('en-IN');
        }
        return `Rs. ${rounded.toLocaleString('en-IN')}`;
    }

    function animateMetricValue(element, duration) {
        const targetValue = Number(element.dataset.count || 0);
        const format = element.dataset.format || 'currency';
        if (!Number.isFinite(targetValue)) {
            return;
        }

        const startTime = performance.now();

        function drawFrame(now) {
            const elapsed = now - startTime;
            const progress = Math.min(elapsed / duration, 1);
            const eased = 1 - Math.pow(1 - progress, 3);
            element.textContent = formatMetricValue(targetValue * eased, format);

            if (progress < 1) {
                window.requestAnimationFrame(drawFrame);
            } else {
                element.textContent = formatMetricValue(targetValue, format);
            }
        }

        window.requestAnimationFrame(drawFrame);
    }

    function revealOnScroll() {
        const revealItems = Array.from(document.querySelectorAll('.animate-on-scroll'));
        if (!revealItems.length) {
            return;
        }

        revealItems.forEach((item, index) => {
            item.style.transitionDelay = `${Math.min(index * 70, 320)}ms`;
        });

        if (!('IntersectionObserver' in window)) {
            revealItems.forEach((item) => item.classList.add('is-visible'));
            return;
        }

        const observer = new IntersectionObserver((entries, watcher) => {
            entries.forEach((entry) => {
                if (!entry.isIntersecting) {
                    return;
                }
                entry.target.classList.add('is-visible');
                watcher.unobserve(entry.target);
            });
        }, {
            threshold: 0.12,
            rootMargin: '0px 0px -40px 0px',
        });

        revealItems.forEach((item) => observer.observe(item));
    }

    function bindPasswordToggles() {
        const passwordInputs = document.querySelectorAll('input[type="password"]');
        if (!passwordInputs.length) {
            return;
        }

        passwordInputs.forEach((input) => {
            if (input.dataset.passwordToggleBound === '1') {
                return;
            }
            input.dataset.passwordToggleBound = '1';

            let inputGroup = input.parentElement;
            if (!inputGroup || !inputGroup.classList.contains('input-group')) {
                inputGroup = document.createElement('div');
                inputGroup.className = 'input-group mf-password-group';
                input.parentNode.insertBefore(inputGroup, input);
                inputGroup.appendChild(input);
            }

            if (inputGroup.querySelector('.mf-password-toggle')) {
                return;
            }

            const toggleButton = document.createElement('button');
            toggleButton.type = 'button';
            toggleButton.className = 'btn btn-outline-secondary mf-password-toggle';
            toggleButton.setAttribute('aria-label', 'Show password');
            toggleButton.innerHTML = '<i class="bi bi-eye"></i>';

            toggleButton.addEventListener('click', () => {
                const showing = input.type === 'text';
                input.type = showing ? 'password' : 'text';
                toggleButton.setAttribute('aria-label', showing ? 'Show password' : 'Hide password');
                const icon = toggleButton.querySelector('i');
                if (!icon) {
                    return;
                }
                icon.className = showing ? 'bi bi-eye' : 'bi bi-eye-slash';
            });

            inputGroup.appendChild(toggleButton);
        });
    }

    function bindOnce(element, key) {
        if (!element) {
            return false;
        }
        const flag = `bound${key}`;
        if (element.dataset[flag] === '1') {
            return false;
        }
        element.dataset[flag] = '1';
        return true;
    }

    window.initMateriallyLayout = function initMateriallyLayout() {
        const body = document.body;
        const toggle = document.getElementById('sidebarToggle');
        const overlay = document.getElementById('mfOverlay');

        const isDesktop = () => window.innerWidth >= 992;
        const closeSidebar = () => {
            body.classList.remove('sidebar-open');
            if (!isDesktop()) {
                return;
            }
            body.classList.add('sidebar-collapsed');
        };
        const toggleSidebar = () => {
            if (isDesktop()) {
                body.classList.toggle('sidebar-collapsed');
                return;
            }
            body.classList.toggle('sidebar-open');
        };

        if (bindOnce(toggle, 'SidebarToggle')) {
            toggle.addEventListener('click', toggleSidebar);
        }

        if (bindOnce(overlay, 'SidebarOverlay')) {
            overlay.addEventListener('click', closeSidebar);
        }

        if (!window.__mfResizeBound) {
            window.addEventListener('resize', () => {
                if (isDesktop()) {
                    body.classList.remove('sidebar-open');
                    return;
                }
                body.classList.remove('sidebar-collapsed');
                body.classList.remove('sidebar-open');
            });
            window.__mfResizeBound = true;
        }
    };

    window.initDashboardUX = function initDashboardUX() {
        const prefersReducedMotion = window.matchMedia('(prefers-reduced-motion: reduce)').matches;
        const counters = document.querySelectorAll('.mf-report-value[data-count]');

        bindPasswordToggles();

        counters.forEach((counter) => {
            if (prefersReducedMotion) {
                const value = Number(counter.dataset.count || 0);
                const format = counter.dataset.format || 'currency';
                counter.textContent = formatMetricValue(value, format);
                return;
            }
            animateMetricValue(counter, 900);
        });

        revealOnScroll();
    };
})();
