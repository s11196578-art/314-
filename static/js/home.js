/**
 * Fiji Ferry Booking - Complete Homepage JavaScript
 * Unified implementation for home.html
 * Version: 2.5.0
 *
 * Fixed:
 * • Weather fetched from DATABASE (no SSE stream)
 * • Map shows ONLY ports with active schedules
 * • Robust fallbacks, error handling, no 404s
 */
(function () {
    'use strict';

    // GLOBAL CONFIGURATION
    const FijiFerry = window.FijiFerry || {};
    FijiFerry.config = {
        animationDuration: 600,
        slideshowInterval: 5000,
        testimonialInterval: 7000,
        pollingInterval: 60000,
        weatherUpdateInterval: 120000, // Poll DB every 2 min
        debug: true
    };

    // LOGGER
    const logger = {
        log: (...args) => FijiFerry.config.debug && console.log('[FijiFerry]', ...args),
        warn: (...args) => FijiFerry.config.debug && console.warn('[FijiFerry]', ...args),
        error: (...args) => console.error('[FijiFerry]', ...args)
    };

    // UTILITY FUNCTIONS
    const Utils = {
        safeParseJSON(elementId, defaultValue = {}) {
            try {
                const script = document.getElementById(elementId);
                if (!script) return defaultValue;
                const jsonStr = script.textContent.trim()
                    .replace(/^\s*<\!\[CDATA\[/, '').replace(/\]\]>\s*$/, '');
                return JSON.parse(jsonStr) || defaultValue;
            } catch (error) {
                logger.warn(`Failed to parse ${elementId}:`, error);
                return defaultValue;
            }
        },
        sanitizeHTML(str) {
            const div = document.createElement('div');
            div.textContent = str;
            return div.innerHTML;
        },
        formatDuration(minutes) {
            if (!minutes || isNaN(minutes)) return 'N/A';
            const hours = Math.floor(minutes / 60);
            const mins = minutes % 60;
            return hours > 0 ? `${hours}h ${mins}m` : `${mins}m`;
        },
        formatPrice(price) {
            return price ? `FJD ${parseFloat(price).toFixed(0)}` : 'Price TBD';
        },
        debounce(func, wait) {
            let timeout;
            return function executedFunction(...args) {
                const later = () => {
                    clearTimeout(timeout);
                    func(...args);
                };
                clearTimeout(timeout);
                timeout = setTimeout(later, wait);
            };
        },
        throttle(func, limit) {
            let inThrottle;
            return function () {
                const args = arguments;
                const context = this;
                if (!inThrottle) {
                    func.apply(context, args);
                    inThrottle = true;
                    setTimeout(() => inThrottle = false, limit);
                }
            };
        },
        getWeatherIcon(condition) {
            const icons = {
                'sunny': '☀︎',
                'clear': '☀︎',
                'partly cloudy': '☁︎⸝⸝',
                'partly_cloudy': '☁︎⸝⸝',
                'cloudy': '☁︎',
                'overcast': '☁︎',
                'cloud': '☁︎',
                'rain': '☂︎',
                'light rain': '☂︎',
                'heavy rain': '☔',
                'shower': '☂︎',
                'thunderstorm': '⚡',
                'thunder': '⚡',
                'drizzle': '☂︎',
                'fog': '〰',
                'mist': '〰',
                'haze': '〰',
                'windy': '〽',
                'snow': '❄︎',
                'sleet': '❄︎',
                'hail': '❄︎'
            };

            const key = condition?.toLowerCase()?.replace(/\s+/g, '_');
            return icons[key] || '☁︎';
        },
        getRouteColor(routeId) {
            const colors = ['#10b981', '#3b82f6', '#f59e0b', '#ef4444', '#8b5cf6', '#06b6d4', '#84cc16'];
            return colors[routeId % colors.length];
        },
        preloadImages(sources) {
            sources.forEach(src => {
                if (src) {
                    const img = new Image();
                    img.src = src;
                }
            });
        },
        getThemeColor(color, opacity = 1) {
            const root = document.documentElement;
            const theme = root.getAttribute('data-theme') || 'light';
            const colors = {
                primary: theme === 'dark' ? '#34D399' : '#10B981',
                secondary: theme === 'dark' ? '#60A5FA' : '#3B82F6',
                background: theme === 'dark' ? '#0F172A' : '#FFFFFF',
                surface: theme === 'dark' ? '#1E293B' : '#F8FAFC',
                text: theme === 'dark' ? '#F1F5F9' : '#1E293B',
                border: theme === 'dark' ? '#334155' : '#E2E8F0'
            };
            return colors[color] || (theme === 'dark' ? '#94A3B8' : '#6B7280');
        },
        applyTailwindOverrides() {
            const theme = document.documentElement.getAttribute('data-theme') || 'light';
            if (theme === 'dark') {
                const style = document.createElement('style');
                style.textContent = `
                    [data-theme="dark"] .text-gray-800 { color: #F1F5F9 !important; }
                    [data-theme="dark"] .text-gray-700 { color: #E2E8F0 !important; }
                    [data-theme="dark"] .text-gray-600 { color: #CBD5E1 !important; }
                    [data-theme="dark"] .text-gray-500 { color: #94A3B8 !important; }
                    [data-theme="dark"] .text-gray-400 { color: #64748B !important; }
                    [data-theme="dark"] .text-gray-300 { color: #475569 !important; }
                    [data-theme="dark"] .text-gray-200 { color: #334155 !important; }
                    [data-theme="dark"] .text-gray-100 { color: #1E293B !important; }
                    [data-theme="dark"] .bg-white { background-color: #0F172A !important; }
                    [data-theme="dark"] .bg-gray-50 { background-color: #1E293B !important; }
                    [data-theme="dark"] .bg-gray-100 { background-color: #334155 !important; }
                    [data-theme="dark"] .bg-gray-200 { background-color: #475569 !important; }
                    [data-theme="dark"] .border-gray-200 { border-color: #475569 !important; }
                    [data-theme="dark"] .border-gray-100 { border-color: #334155 !important; }
                `;
                document.head.appendChild(style);
            }
        }
    };

    // NOTIFICATION SYSTEM
    class NotificationManager {
        constructor() {
            this.container = null;
            this.notifications = new Map();
            this.init();
        }
        init() {
            this.createContainer();
            this.setupThemeListener();
        }
        setupThemeListener() {
            const observer = new MutationObserver(() => this.updateNotificationsTheme());
            observer.observe(document.documentElement, { attributes: true, attributeFilter: ['data-theme'] });
        }
        createContainer() {
            if (this.container) this.container.remove();
            this.container = document.createElement('div');
            this.container.className = 'notification-container fixed top-4 right-4 z-50 space-y-2 max-w-sm w-full sm:w-80';
            document.body.appendChild(this.container);
            this.updateContainerTheme();
        }
        updateContainerTheme() {
            if (!this.container) return;
            const theme = document.documentElement.getAttribute('data-theme') || 'light';
            this.container.style.background = theme === 'dark' ? 'rgba(15, 23, 42, 0.95)' : 'rgba(255, 255, 255, 0.95)';
            this.container.style.backdropFilter = 'blur(20px)';
        }
        updateNotificationsTheme() {
            this.notifications.forEach(({ element }) => {
                if (element && element.parentNode) this.updateNotificationTheme(element);
            });
        }
        updateNotificationTheme(notification) {
            const theme = document.documentElement.getAttribute('data-theme') || 'light';
            const classes = notification.className.split(' ');
            const bgClass = classes.find(cls => cls.includes('bg-'));
            if (bgClass) {
                const color = bgClass.replace('bg-', '');
                const opacity = theme === 'dark' ? 0.15 : 1;
                const rgbaColor = this.hexToRgb(Utils.getThemeColor(color), opacity);
                notification.style.background = `rgba(${rgbaColor})`;
                if (theme === 'dark') {
                    notification.style.color = '#F1F5F9';
                    const closeBtn = notification.querySelector('.notification-close');
                    if (closeBtn) closeBtn.style.color = '#F1F5F9';
                }
            }
        }
        hexToRgb(hex, alpha = 1) {
            const result = /^#?([a-f\d]{2})([a-f\d]{2})([a-f\d]{2})$/i.exec(hex);
            return result ? `${parseInt(result[1], 16)}, ${parseInt(result[2], 16)}, ${parseInt(result[3], 16)}` : '0, 0, 0';
        }
        show(message, type = 'info', duration = 4000, options = {}) {
            const id = `notification-${Date.now()}-${Math.random().toString(36).substr(2, 9)}`;
            const config = this.getNotificationConfig(type);
            const notification = document.createElement('div');
            notification.id = id;
            notification.className = `
                notification transform translate-x-full transition-transform duration-300 p-4 rounded-xl shadow-2xl max-w-sm
                ${config.bg} ${config.text} border-l-4 ${config.border}
            `;
            notification.setAttribute('role', 'alert');
            notification.setAttribute('aria-live', options.live || 'polite');
            notification.setAttribute('aria-atomic', 'true');
            notification.innerHTML = `
                <div class="notification-content flex items-start gap-3">
                    <span class="notification-icon flex-shrink-0 mt-0.5 text-lg">${config.icon}</span>
                    <div class="notification-message flex-1 min-w-0">
                        <div class="notification-title font-semibold text-sm leading-tight capitalize">${type}</div>
                        <div class="notification-text text-sm leading-relaxed break-words" title="${Utils.sanitizeHTML(message)}">${Utils.sanitizeHTML(message)}</div>
                    </div>
                    <button class="notification-close ml-2 text-current opacity-70 hover:opacity-100 p-1 rounded-full hover:bg-white/20 transition-colors flex-shrink-0"
                            onclick="FijiFerry.notificationManager?.removeNotification('${id}')"
                            aria-label="Dismiss ${type} notification">
                        <i class="fas fa-times text-sm"></i>
                    </button>
                </div>
            `;
            this.container.appendChild(notification);
            this.notifications.set(id, { element: notification, duration, options });
            requestAnimationFrame(() => notification.classList.remove('translate-x-full'));
            if (duration > 0) setTimeout(() => this.removeNotification(id), duration);
            this.updateNotificationTheme(notification);
            return notification;
        }
        getNotificationConfig(type) {
            const configs = {
                success: { icon: 'Check', bg: 'bg-emerald-500', border: 'border-emerald-400', text: 'text-white' },
                error: { icon: 'Cross', bg: 'bg-red-500', border: 'border-red-400', text: 'text-white' },
                warning: { icon: 'Warning', bg: 'bg-amber-500', border: 'border-amber-400', text: 'text-white' },
                info: { icon: 'Info', bg: 'bg-blue-500', border: 'border-blue-400', text: 'text-white' }
            };
            return configs[type] || configs.info;
        }
        removeNotification(id) {
            const notification = document.getElementById(id);
            if (!notification) return;
            notification.classList.add('translate-x-full');
            setTimeout(() => {
                if (notification.parentNode) notification.remove();
                this.notifications.delete(id);
            }, 300);
        }
        closeAll() {
            this.notifications.forEach((_, id) => this.removeNotification(id));
        }
        destroy() {
            this.closeAll();
            if (this.container) this.container.remove();
            this.container = null;
        }
    };

    // HERO MANAGER
    class HeroManager {
        constructor() {
            this.slides = document.querySelectorAll('.hero-slide');
            this.dots = document.querySelectorAll('.hero-nav-dots .dot');
            this.form = document.getElementById('search-form');
            this.currentSlide = 0;
            this.slideInterval = null;
            this.isInitialized = false;
            this.init();
        }
        init() {
            if (this.isInitialized) return;
            this.isInitialized = true;
            logger.log('Initializing HeroManager');
            this.setupSlideshow();
            if (this.form) this.setupForm();
            this.setupThemeListener();
            this.setupQuickSearch();
            if (this.form) this.form.addEventListener('input', () => this.updateState());
        }
        setupThemeListener() {
            const observer = new MutationObserver(() => this.updateTheme());
            observer.observe(document.documentElement, { attributes: true, attributeFilter: ['data-theme'] });
        }
        updateTheme() {
            const theme = document.documentElement.getAttribute('data-theme') || 'light';
            logger.log(`Hero theme changed to: ${theme}`);
            this.slides.forEach(slide => {
                const imgSrc = slide.dataset.srcLight;
                if (imgSrc && slide.style.backgroundImage !== `url('${imgSrc}')`) {
                    const img = new Image();
                    img.onload = () => {
                        slide.style.backgroundImage = `url('${imgSrc}')`;
                        logger.log(`Updated slide image to: ${imgSrc}`);
                    };
                    img.src = imgSrc;
                }
            });
        }
        setupSlideshow() {
            if (!this.slides.length) {
                logger.warn('No slides found for hero slideshow');
                return;
            }
            const activeSlide = document.querySelector('.hero-slide.active');
            if (activeSlide) this.currentSlide = Array.from(this.slides).indexOf(activeSlide);
            else if (this.slides[0]) {
                this.slides[0].classList.add('active');
                this.currentSlide = 0;
            }
            if (this.dots[this.currentSlide]) this.dots[this.currentSlide].classList.add('active');
            this.showSlide(this.currentSlide);
            this.startSlideshow();
            const hero = document.querySelector('.hero');
            if (hero) {
                ['mouseenter', 'focusin'].forEach(event => hero.addEventListener(event, () => this.pauseSlideshow()));
                ['mouseleave', 'focusout'].forEach(event => hero.addEventListener(event, () => this.resumeSlideshow()));
            }
            this.updateTheme();
        }
        showSlide(index) {
            if (index < 0 || index >= this.slides.length) return;
            this.slides.forEach((slide, i) => {
                if (i === index) {
                    slide.classList.add('active');
                    slide.style.opacity = '1';
                    slide.style.zIndex = '2';
                } else {
                    slide.classList.remove('active');
                    slide.style.opacity = '0';
                    slide.style.zIndex = '1';
                }
            });
            this.dots.forEach((dot, i) => {
                dot.classList.toggle('active', i === index);
                dot.setAttribute('aria-pressed', (i === index).toString());
                dot.setAttribute('tabindex', i === index ? '0' : '-1');
            });
            this.currentSlide = index;
        }
        startSlideshow() {
            if (this.slideInterval) clearInterval(this.slideInterval);
            if (this.slides.length <= 1) return;
            this.slideInterval = setInterval(() => {
                this.currentSlide = (this.currentSlide + 1) % this.slides.length;
                this.showSlide(this.currentSlide);
            }, FijiFerry.config.slideshowInterval);
        }
        pauseSlideshow() {
            if (this.slideInterval) {
                clearInterval(this.slideInterval);
                this.slideInterval = null;
            }
        }
        resumeSlideshow() {
            if (!this.slideInterval && this.slides.length > 1) this.startSlideshow();
        }
        setupForm() {
            this.form.addEventListener('submit', (e) => {
                if (!this.validate()) {
                    e.preventDefault();
                    FijiFerry.notificationManager?.show('Please fill in all required fields', 'warning');
                }
            });
            const dateInput = document.getElementById('departure-date');
            if (dateInput) {
                dateInput.addEventListener('change', (e) => {
                    const today = new Date().toISOString().split('T')[0];
                    if (e.target.value < today) {
                        e.target.value = today;
                        FijiFerry.notificationManager?.show('Please select a future date', 'warning');
                    }
                });
                if (!dateInput.min) dateInput.min = new Date().toISOString().split('T')[0];
            }
            this.populateForm();
            this.setupRouteSuggestions();
        }
        setupRouteSuggestions() {
            const routeInput = document.getElementById('route');
            const suggestions = document.getElementById('route-suggestions');
            if (!routeInput || !suggestions) return;
            routeInput.addEventListener('input', Utils.debounce(async (e) => {
                const query = e.target.value.toLowerCase().trim();
                if (query.length < 2) {
                    suggestions.classList.add('hidden');
                    return;
                }
                try {
                    const response = await fetch(`/bookings/api/routes/?q=${encodeURIComponent(query)}`);
                    if (!response.ok) throw new Error('Failed to fetch routes');
                    const { routes } = await response.json();
                    if (routes.length > 0) {
                        suggestions.innerHTML = routes.slice(0, 5).map(route => `
                            <div class="route-suggestion p-3 border-b border-gray-100 hover:bg-gray-50 cursor-pointer transition-colors dark:border-gray-700 dark:hover:bg-gray-800"
                                 onclick="document.getElementById('route').value='${route.departure_port.name} to ${route.destination_port.name}'; document.getElementById('route-suggestions').classList.add('hidden');"
                                 role="option" tabindex="0">
                                ${route.departure_port.name} to ${route.destination_port.name}
                            </div>
                        `).join('');
                        suggestions.classList.remove('hidden');
                        suggestions.querySelector('.route-suggestion')?.focus();
                    } else {
                        suggestions.classList.add('hidden');
                    }
                } catch (error) {
                    console.warn('Route suggestions failed:', error);
                    suggestions.classList.add('hidden');
                }
            }, 300));
            document.addEventListener('click', (e) => {
                if (!e.target.closest('#route') && !e.target.closest('#route-suggestions')) {
                    suggestions.classList.add('hidden');
                }
            });
            routeInput.addEventListener('keydown', (e) => {
                if (e.key === 'Escape') {
                    suggestions.classList.add('hidden');
                    routeInput.focus();
                }
                if (e.key === 'ArrowDown' && !suggestions.classList.contains('hidden')) {
                    e.preventDefault();
                    suggestions.querySelector('.route-suggestion')?.focus();
                }
            });
            suggestions.addEventListener('keydown', (e) => {
                const suggestionsList = suggestions.querySelectorAll('.route-suggestion');
                const current = document.activeElement;
                const currentIndex = Array.from(suggestionsList).indexOf(current);
                if (e.key === 'ArrowDown') {
                    e.preventDefault();
                    const nextIndex = (currentIndex + 1) % suggestionsList.length;
                    suggestionsList[nextIndex].focus();
                } else if (e.key === 'ArrowUp') {
                    e.preventDefault();
                    const prevIndex = (currentIndex - 1 + suggestionsList.length) % suggestionsList.length;
                    suggestionsList[prevIndex].focus();
                } else if (e.key === 'Enter' && current && current.classList.contains('route-suggestion')) {
                    e.preventDefault();
                    current.click();
                } else if (e.key === 'Escape') {
                    suggestions.classList.add('hidden');
                    routeInput.focus();
                }
            });
        }
        validate() {
            const route = document.getElementById('route')?.value.trim();
            const date = document.getElementById('departure-date')?.value;
            const passengers = document.getElementById('passengers')?.value;
            const today = new Date().toISOString().split('T')[0];
            return route && date && date >= today && passengers && passengers !== '0';
        }
        updateState() {
            const isValid = this.validate();
            const submitBtn = this.form.querySelector('button[type="submit"]');
            const feedback = document.getElementById('form-feedback');
            if (submitBtn) {
                submitBtn.disabled = !isValid;
                submitBtn.classList.toggle('opacity-50', !isValid);
                submitBtn.classList.toggle('cursor-not-allowed', !isValid);
            }
            if (feedback) {
                const theme = document.documentElement.getAttribute('data-theme') || 'light';
                feedback.classList.toggle('hidden', isValid);
                if (isValid) {
                    feedback.innerHTML = '<i class="fas fa-check-circle mr-1"></i>Ready to search!';
                    feedback.classList.add(theme === 'dark' ? 'text-emerald-400' : 'text-emerald-300');
                    feedback.classList.remove('text-red-300', 'text-red-400');
                } else {
                    feedback.innerHTML = '<i class="fas fa-exclamation-triangle mr-1"></i>Please complete all fields';
                    feedback.classList.add(theme === 'dark' ? 'text-red-400' : 'text-red-300');
                    feedback.classList.remove('text-emerald-300', 'text-emerald-400');
                }
            }
        }
        populateForm() {
            try {
                const formData = Utils.safeParseJSON('form-data', {});
                const urlParams = new URLSearchParams(window.location.search);
                const routeInput = document.getElementById('route');
                const routeValue = formData.route || urlParams.get('route') || '';
                if (routeInput && routeValue) {
                    routeInput.value = routeValue;
                    routeInput.dispatchEvent(new Event('input', { bubbles: true }));
                }
                const dateInput = document.getElementById('departure-date');
                const today = new Date().toISOString().split('T')[0];
                const dateValue = formData.date || urlParams.get('date') || today;
                if (dateInput && dateValue >= today) dateInput.value = dateValue;
                const passengerSelect = document.getElementById('passengers');
                const passengerValue = formData.passengers || urlParams.get('passengers') || '1';
                if (passengerSelect && passengerValue) {
                    const option = passengerSelect.querySelector(`[value="${passengerValue}"]`);
                    if (option) passengerSelect.value = passengerValue;
                }
                this.updateState();
            } catch (error) {
                logger.warn('Form population failed:', error);
            }
        }
        setupQuickSearch() {
            document.querySelectorAll('.quick-route, .destination-cta, [data-quick-search]').forEach(link => {
                link.addEventListener('click', (e) => {
                    e.preventDefault();
                    e.stopPropagation();
                    const portName = link.dataset.toPort || link.dataset.quickSearch || link.textContent.toLowerCase().replace(/[^a-z0-9]/g, '-').split('-')[0];
                    this.quickSearch(portName);
                });
            });
        }
        quickSearch(portName) {
            if (!portName) return false;
            const routeInput = document.getElementById('route');
            const form = document.getElementById('search-form');
            if (!routeInput || !form) return false;
            routeInput.value = `${portName}-to-destination`;
            routeInput.dispatchEvent(new Event('input', { bubbles: true }));
            form.scrollIntoView({ behavior: 'smooth', block: 'center' });
            setTimeout(() => { routeInput.focus(); routeInput.select(); }, 300);
            FijiFerry.notificationManager?.show(
                `Searching routes from ${portName.replace(/-/g, ' ').replace(/\b\w/g, l => l.toUpperCase())}`,
                'info', 2500
            );
            return true;
        }
        destroy() {
            this.isInitialized = false;
        }
    };

    // FILTER MANAGER
    class FilterManager {
        constructor() {
            this.isInitialized = false;
            this.init();
        }
        init() {
            if (this.isInitialized) return;
            this.isInitialized = true;
            this.setupControls();
            this.setupThemeListener();
        }
        setupThemeListener() {
            const observer = new MutationObserver(() => this.updateTheme());
            observer.observe(document.documentElement, { attributes: true, attributeFilter: ['data-theme'] });
        }
        updateTheme() {
            const theme = document.documentElement.getAttribute('data-theme') || 'light';
            const filters = document.querySelector('.schedule-filters');
            if (filters) {
                filters.style.background = theme === 'dark' ? 'rgba(30, 41, 59, 0.8)' : 'rgba(255, 255, 255, 0.8)';
                filters.style.backdropFilter = 'blur(10px)';
                filters.style.borderColor = theme === 'dark' ? '#475569' : '#E5E7EB';
            }
            const sortSelect = document.getElementById('sort-by');
            if (sortSelect) {
                sortSelect.style.background = theme === 'dark' ? 'rgb(30, 41, 59)' : 'white';
                sortSelect.style.borderColor = theme === 'dark' ? '#475569' : '#D1D5DB';
                sortSelect.style.color = Utils.getThemeColor('text');
            }
        }
        setupControls() {
            const viewAllBtn = document.getElementById('view-all-btn');
            if (viewAllBtn) {
                viewAllBtn.addEventListener('click', (e) => {
                    e.preventDefault();
                    const url = new URL(window.location);
                    ['route', 'date', 'passengers', 'sort'].forEach(param => url.searchParams.delete(param));
                    window.history.replaceState({}, '', url);
                    window.resetSearch();
                    const sortSelect = document.getElementById('sort-by');
                    if (sortSelect) sortSelect.value = 'time';
                    FijiFerry.notificationManager?.show('Showing all available schedules', 'success', 2000);
                });
            }
            window.resetSearch = () => {
                const form = document.getElementById('search-form');
                if (form) {
                    form.reset();
                    const dateInput = document.getElementById('departure-date');
                    const today = new Date().toISOString().split('T')[0];
                    if (dateInput) {
                        dateInput.value = today;
                        dateInput.min = today;
                    }
                    const url = new URL(window.location);
                    ['route', 'date', 'passengers', 'sort'].forEach(param => url.searchParams.delete(param));
                    window.history.replaceState({}, '', url);
                    form.dispatchEvent(new Event('reset', { bubbles: true }));
                    form.dispatchEvent(new Event('input', { bubbles: true }));
                }
                FijiFerry.notificationManager?.show('Search reset! Ready to discover new routes.', 'info', 3000);
            };
            document.addEventListener('keydown', (e) => {
                if ((e.ctrlKey || e.metaKey) && e.key === 'r') {
                    e.preventDefault();
                    window.resetSearch();
                }
            });
        }
        destroy() {
            this.isInitialized = false;
        }
    };

    // MAP MANAGER – ONLY ACTIVE PORTS
    class MapManager {
        constructor() {
            this.map = null;
            this.init();
        }
        async init() {
            const mapContainer = document.getElementById('fiji-map');
            if (!mapContainer) return;
            this.map = L.map('fiji-map').setView([-17.7134, 178.0650], 8);
            L.tileLayer('https://tile.openstreetmap.org/{z}/{x}/{y}.png', {
                maxZoom: 19,
                attribution: '&copy; OpenStreetMap'
            }).addTo(this.map);
            await this.loadPorts();
        }
        async loadPorts() {
            try {
                const response = await fetch('/bookings/api/routes/');
                if (!response.ok) throw new Error('Ports fetch failed');
                const { routes } = await response.json();

                // Determine active ports from schedule cards
                const activePorts = new Set();
                document.querySelectorAll('.schedule-card h3').forEach(h3 => {
                    const [depart, dest] = h3.textContent.split(' to ');
                    if (depart) activePorts.add(depart.trim());
                    if (dest) activePorts.add(dest.trim());
                });

                // Add markers
                routes.forEach(route => {
                    const ports = [route.departure_port, route.destination_port];
                    ports.forEach(p => {
                        const lat = parseFloat(p.lat);
                        const lng = parseFloat(p.lng);
                        if (isNaN(lat) || isNaN(lng)) return;

                        const isActive = activePorts.has(p.name);
                        if (isActive) {
                            L.circleMarker([lat, lng], {
                                radius: 10,
                                color: '#10b981',
                                fillColor: '#10b981',
                                fillOpacity: 0.7
                            }).addTo(this.map)
                              .bindPopup(`<b>${p.name}</b><br>Active schedule`);
                            L.circle([lat, lng], {
                                radius: 25000,
                                color: '#10b981',
                                fillColor: '#10b981',
                                fillOpacity: 0.15,
                                className: 'pulse-circle'
                            }).addTo(this.map);
                        } else {
                            L.marker([lat, lng]).addTo(this.map).bindPopup(p.name);
                        }
                    });
                });
                logger.log(`Map loaded – ${activePorts.size} active ports`);
            } catch (error) {
                logger.warn('Failed to load ports from API:', error);
                this.addHardcodedPorts();
            }
        }
        addHardcodedPorts() {
            const ports = [
                { name: 'Nadi', lat: -17.7728, lng: 177.3809 },
                { name: 'Suva', lat: -18.1248, lng: 178.3967 },
                { name: 'Denarau', lat: -17.7725, lng: 177.3805 },
                { name: 'Yasawa Islands', lat: -16.9, lng: 177.3 }
            ];
            ports.forEach(port => {
                L.marker([port.lat, port.lng]).addTo(this.map).bindPopup(port.name);
            });
        }
    };

// === SCHEDULE MANAGER – PER-SCHEDULE DB POLLING ===
class ScheduleManager {
    constructor() {
        this.scheduleList = null;
        this.nextDepartureBanner = document.querySelector('.next-departure-banner');
        this.viewAllButton = document.getElementById('view-all-btn');
        this.weatherTimer = null;
        this.scheduleTimer = null;
        this.latestHash = null;
        this.maxSchedules = 12;
        this.visibleScheduleIds = new Set();
        this.init();
    }

    init() {
        this.scheduleList = this.ensureScheduleList();
        if (this.scheduleList) {
            this.maxSchedules = parseInt(this.scheduleList.dataset.limit || '12', 10);
            this.visibleScheduleIds = new Set(
                Array.from(this.scheduleList.querySelectorAll('.schedule-card'))
                    .map(card => parseInt(card.dataset.scheduleId, 10))
                    .filter(id => !Number.isNaN(id))
            );
        }

        this.updateWeatherDisplay();
        this.weatherTimer = setInterval(() => this.updateWeatherDisplay(), FijiFerry.config.weatherUpdateInterval);
        this.startSchedulePolling();
    }

    destroy() {
        if (this.weatherTimer) clearInterval(this.weatherTimer);
        if (this.scheduleTimer) clearInterval(this.scheduleTimer);
    }

    ensureScheduleList() {
        if (this.scheduleList) return this.scheduleList;
        const existing = document.getElementById('schedule-list');
        if (existing) {
            return existing;
        }

        const container = document.querySelector('#schedules-section .container');
        if (!container) return null;

        const list = document.createElement('div');
        list.id = 'schedule-list';
        list.className = 'schedule-list grid grid-cols-1 md:grid-cols-2 lg:grid-cols-3 gap-6';
        list.setAttribute('role', 'list');
        list.setAttribute('aria-label', 'Available ferry schedules');
        list.dataset.limit = '12';

        const loadMore = container.querySelector('.load-more');
        if (loadMore) {
            container.insertBefore(list, loadMore);
        } else {
            container.appendChild(list);
        }

        return list;
    }

    startSchedulePolling() {
        this.fetchAndRenderSchedules(true);
        this.scheduleTimer = setInterval(() => this.fetchAndRenderSchedules(), FijiFerry.config.pollingInterval);
    }

    async fetchAndRenderSchedules(force = false) {
        const endpoint = window.urls?.homeSchedules || '/bookings/api/homepage-schedules/';
        try {
            const url = new URL(endpoint, window.location.origin);
            url.searchParams.set('limit', (this.maxSchedules || 12).toString());
            url.searchParams.set('_', Date.now().toString());

            const response = await fetch(url.toString(), {
                headers: {
                    'X-Requested-With': 'XMLHttpRequest',
                    'Accept': 'application/json'
                },
                credentials: 'same-origin'
            });

            if (!response.ok) {
                throw new Error(`Schedule fetch failed with status ${response.status}`);
            }

            const data = await response.json();
            this.renderNextDeparture(data.next_departure);
            this.updateCounts(data.total_schedules, data.remaining_schedules);

            const hasChanged = force || (data.schedules_hash && data.schedules_hash !== this.latestHash);
            if (!hasChanged) {
                logger.log('Schedules unchanged; skipping re-render');
                return;
            }

            this.latestHash = data.schedules_hash || null;
            this.renderScheduleList(data.schedules || [], force);
            await this.updateWeatherDisplay();
        } catch (error) {
            logger.warn('Schedule polling failed:', error);
        }
    }

    renderNextDeparture(nextDeparture) {
        if (!this.nextDepartureBanner) {
            this.nextDepartureBanner = document.querySelector('.next-departure-banner');
        }
        if (!this.nextDepartureBanner) return;

        const timeEl = this.nextDepartureBanner.querySelector('#next-departure-time');
        if (timeEl) {
            if (nextDeparture) {
                timeEl.textContent = `${nextDeparture.time_display} - ${nextDeparture.route_display}`;
                timeEl.setAttribute('datetime', nextDeparture.departure_time);
            } else {
                timeEl.textContent = 'No upcoming departures available';
                timeEl.removeAttribute('datetime');
            }
        }

        const cta = this.nextDepartureBanner.querySelector('.banner-cta');
        if (cta) {
            if (nextDeparture?.book_url) {
                cta.href = nextDeparture.book_url;
                cta.setAttribute('aria-label', `Book the next departure from ${nextDeparture.route_display}`);
                cta.classList.remove('pointer-events-none', 'opacity-50');
                cta.removeAttribute('aria-disabled');
            } else {
                cta.removeAttribute('href');
                cta.setAttribute('aria-disabled', 'true');
                cta.classList.add('pointer-events-none', 'opacity-50');
            }
        }
    }

    updateCounts(total, remaining) {
        const totalSchedules = Number.isFinite(total) ? total : 0;
        const remainingSchedules = Number.isFinite(remaining) ? remaining : 0;

        const viewAllBtn = this.viewAllButton || document.getElementById('view-all-btn');
        if (viewAllBtn) {
            const labelSpan = viewAllBtn.querySelector('span');
            if (labelSpan) {
                labelSpan.textContent = `View All (${totalSchedules})`;
            }
            viewAllBtn.setAttribute('aria-label', `View all ${totalSchedules} available schedules`);
        }

        const loadMoreBtn = document.getElementById('load-more-schedules');
        const loadMoreWrapper = loadMoreBtn ? loadMoreBtn.closest('.load-more') : null;
        if (loadMoreBtn && loadMoreWrapper) {
            if (remainingSchedules > 0) {
                loadMoreWrapper.classList.remove('hidden');
                const textSpan = loadMoreBtn.querySelector('span');
                if (textSpan) {
                    textSpan.textContent = `Load More Schedules (${remainingSchedules} remaining)`;
                }
                loadMoreBtn.setAttribute('aria-label', `Load ${remainingSchedules} more schedules`);
            } else {
                loadMoreWrapper.classList.add('hidden');
            }
        }
    }

    renderScheduleList(schedules, suppressHighlight = false) {
        this.scheduleList = this.ensureScheduleList();
        const list = this.scheduleList;
        if (!list) return;

        const previousIds = new Set(this.visibleScheduleIds);
        list.innerHTML = '';

        if (!schedules || schedules.length === 0) {
            list.appendChild(this.buildEmptyState());
            this.visibleScheduleIds.clear();
            return;
        }

        const fragment = document.createDocumentFragment();
        const newIdSet = new Set();

        for (const schedule of schedules) {
            fragment.appendChild(this.createScheduleCard(schedule));
            newIdSet.add(schedule.id);
        }

        list.appendChild(fragment);
        this.visibleScheduleIds = newIdSet;

        if (!suppressHighlight) {
            const addedIds = [...newIdSet].filter(id => !previousIds.has(id));
            if (addedIds.length) {
                this.highlightNewSchedules(addedIds);
            }
        }
    }

    buildEmptyState() {
        const wrapper = document.createElement('div');
        wrapper.className = 'col-span-full text-center py-16';

        const container = document.createElement('div');
        container.className = 'empty-state';

        const icon = document.createElement('i');
        icon.className = 'fas fa-ship text-6xl text-gray-300 mb-6';
        icon.setAttribute('aria-hidden', 'true');

        const heading = document.createElement('h3');
        heading.className = 'text-2xl font-semibold text-gray-700 mb-2 font-poppins';
        heading.textContent = 'No Departures Available';

        const description = document.createElement('p');
        description.className = 'text-gray-600 mb-6 max-w-md mx-auto leading-relaxed';
        description.textContent = 'No current departures available. Please check back soon or try a different route.';

        const actions = document.createElement('div');
        actions.className = 'empty-state-actions flex flex-wrap justify-center gap-4';

        const resetButton = document.createElement('button');
        resetButton.type = 'button';
        resetButton.className = 'reset-btn bg-emerald-100 hover:bg-emerald-200 text-emerald-700 px-6 py-3 rounded-lg font-medium transition-all flex items-center gap-2 shadow-sm focus:outline-none focus:ring-2 focus:ring-emerald-500/50';
        resetButton.setAttribute('aria-label', 'Reset search filters');
        resetButton.addEventListener('click', () => window.resetSearch?.());

        const resetIcon = document.createElement('i');
        resetIcon.className = 'fas fa-sync-alt';
        resetIcon.setAttribute('aria-hidden', 'true');

        const resetText = document.createElement('span');
        resetText.textContent = 'Reset Search';

        resetButton.append(resetIcon, resetText);

        const browseLink = document.createElement('a');
        browseLink.className = 'browse-btn bg-gradient-to-r from-emerald-500 to-cyan-500 hover:from-emerald-600 hover:to-cyan-600 text-white px-6 py-3 rounded-lg font-medium shadow-lg hover:shadow-xl transition-all flex items-center gap-2 focus:outline-none focus:ring-2 focus:ring-emerald-500/50';
        browseLink.href = this.getBookingUrl({ id: '', book_url: window.urls?.bookTicket || '/bookings/book/' });
        browseLink.setAttribute('aria-label', 'Browse all available routes');

        const browseIcon = document.createElement('i');
        browseIcon.className = 'fas fa-compass';
        browseIcon.setAttribute('aria-hidden', 'true');

        const browseText = document.createElement('span');
        browseText.textContent = 'Browse All Routes';

        browseLink.append(browseIcon, browseText);

        actions.append(resetButton, browseLink);
        container.append(icon, heading, description, actions);
        wrapper.appendChild(container);

        return wrapper;
    }

    createScheduleCard(schedule) {
        const card = document.createElement('article');
        card.className = 'schedule-card transition-all duration-300 hover:shadow-lg hover:-translate-y-1 border border-gray-200 rounded-xl overflow-hidden flex flex-col h-full';
        card.dataset.scheduleId = schedule.id;
        if (schedule.route?.id) card.dataset.routeId = schedule.route.id;
        if (schedule.departure_hour) card.dataset.timeSlot = schedule.departure_hour;
        if (typeof schedule.route?.base_fare !== 'undefined') {
            card.dataset.price = schedule.route.base_fare ?? 0;
        }
        card.setAttribute('role', 'article');
        card.setAttribute('aria-labelledby', `schedule-${schedule.id}-title`);

        const routeInfo = document.createElement('div');
        routeInfo.className = 'route-info p-6 flex-grow';

        const header = document.createElement('header');
        header.className = 'mb-4';

        const title = document.createElement('h3');
        title.id = `schedule-${schedule.id}-title`;
        title.className = 'text-xl font-bold text-gray-800 font-poppins mb-2';
        title.appendChild(document.createTextNode(`${schedule.route?.departure || ''} `));
        const arrow = document.createElement('span');
        arrow.className = 'text-sm text-gray-400';
        arrow.setAttribute('aria-hidden', 'true');
        arrow.textContent = '→';
        title.appendChild(arrow);
        title.appendChild(document.createTextNode(` ${schedule.route?.destination || ''}`));

        const timeEl = document.createElement('time');
        timeEl.className = 'departure-time text-sm text-gray-600 mb-1';
        if (schedule.departure_time) timeEl.setAttribute('datetime', schedule.departure_time);
        timeEl.setAttribute('aria-label', 'Departure time');
        timeEl.textContent = schedule.departure_display || '';

        const ferryName = document.createElement('p');
        ferryName.className = 'ferry-name text-sm text-gray-500';
        ferryName.textContent = schedule.ferry_name || '';

        header.append(title, timeEl, ferryName);

        const weatherInfo = document.createElement('div');
        weatherInfo.className = 'weather-info flex items-center gap-3 p-3 bg-gray-50 rounded-lg mb-4 border border-gray-100';
        weatherInfo.setAttribute('aria-label', 'Weather forecast for departure');

        const weatherIcon = document.createElement('div');
        weatherIcon.className = 'weather-icon text-2xl flex-shrink-0';
        weatherIcon.id = `weather-icon-${schedule.id}`;
        weatherIcon.textContent = '🌤️';

        const weatherDetails = document.createElement('div');
        weatherDetails.className = 'weather-details flex-1 min-w-0';

        const weatherCondition = document.createElement('div');
        weatherCondition.className = 'weather-condition font-semibold text-sm text-gray-800 truncate';
        weatherCondition.id = `weather-condition-${schedule.id}`;
        weatherCondition.setAttribute('role', 'status');
        weatherCondition.setAttribute('aria-live', 'polite');
        weatherCondition.textContent = 'Loading weather...';

        const weatherMeta = document.createElement('div');
        weatherMeta.className = 'weather-meta flex gap-4 text-xs text-gray-500 mt-1 flex-wrap';

        weatherMeta.append(
            this.createWeatherMetricElement('temp', schedule.id, 'fas fa-thermometer-half text-emerald-500', '28°C', 'Temperature'),
            this.createWeatherMetricElement('wind', schedule.id, 'fas fa-wind text-blue-500', '12 kph', 'Wind speed'),
            this.createWeatherMetricElement('precip', schedule.id, 'fas fa-cloud-rain text-gray-500', '5%', 'Precipitation chance')
        );

        weatherDetails.append(weatherCondition, weatherMeta);
        weatherInfo.append(weatherIcon, weatherDetails);

        const scheduleMeta = document.createElement('dl');
        scheduleMeta.className = 'schedule-meta grid grid-cols-2 gap-3 mb-4 text-sm text-gray-600';

        const seats = document.createElement('div');
        seats.className = 'seats flex items-center gap-2 dt';
        const seatsIconWrap = document.createElement('dt');
        seatsIconWrap.className = 'flex-shrink-0';
        const seatsIcon = document.createElement('i');
        seatsIcon.className = 'fas fa-chair text-gray-400';
        seatsIcon.setAttribute('aria-hidden', 'true');
        seatsIconWrap.appendChild(seatsIcon);
        const seatsCount = document.createElement('dd');
        seatsCount.className = 'seats-count font-semibold text-gray-800';
        seatsCount.textContent = typeof schedule.available_seats === 'number' ? schedule.available_seats : '0';
        const seatsSr = document.createElement('span');
        seatsSr.className = 'sr-only';
        seatsSr.textContent = 'seats available';
        seats.append(seatsIconWrap, seatsCount, seatsSr);

        scheduleMeta.appendChild(seats);

        if (schedule.route?.estimated_duration_minutes) {
            const duration = document.createElement('div');
            duration.className = 'duration flex items-center gap-2 justify-end dt';
            const durationIconWrap = document.createElement('dt');
            durationIconWrap.className = 'flex-shrink-0';
            const durationIcon = document.createElement('i');
            durationIcon.className = 'fas fa-clock text-gray-400';
            durationIcon.setAttribute('aria-hidden', 'true');
            durationIconWrap.appendChild(durationIcon);
            const durationValue = document.createElement('dd');
            durationValue.textContent = Utils.formatDuration(schedule.route.estimated_duration_minutes);
            const durationSr = document.createElement('span');
            durationSr.className = 'sr-only';
            durationSr.textContent = 'estimated duration';
            duration.append(durationIconWrap, durationValue, durationSr);
            scheduleMeta.appendChild(duration);
        }

        routeInfo.append(header, weatherInfo, scheduleMeta);

        const footer = document.createElement('footer');
        footer.className = 'schedule-footer pt-4 border-t border-gray-200 px-6 pb-6 bg-gray-50 mt-auto';

        const statusPrice = document.createElement('div');
        statusPrice.className = 'status-price flex items-center justify-between mb-4';

        const statusConfig = this.getStatusConfig(schedule);
        const statusBadge = document.createElement('span');
        statusBadge.className = `status-badge inline-flex items-center gap-1 px-3 py-1 rounded-full text-xs font-semibold ${statusConfig.badgeClass}`;

        const statusIconEl = document.createElement('i');
        statusIconEl.className = statusConfig.iconClass;
        statusIconEl.setAttribute('aria-hidden', 'true');

        const statusLabel = document.createElement('span');
        statusLabel.setAttribute('aria-label', `Schedule status: ${statusConfig.label}`);
        statusLabel.textContent = statusConfig.label;

        statusBadge.append(statusIconEl, statusLabel);

        const priceEl = document.createElement('span');
        if (schedule.bookable && schedule.route?.base_fare) {
            priceEl.className = 'price text-lg font-bold text-emerald-600';
            priceEl.setAttribute('aria-label', `Fare: ${Utils.formatPrice(schedule.route.base_fare)}`);
            priceEl.textContent = Utils.formatPrice(schedule.route.base_fare);
        } else {
            priceEl.className = 'price text-sm text-gray-500 font-medium';
            priceEl.setAttribute('aria-label', 'Booking unavailable');
            priceEl.textContent = 'Unavailable';
        }

        statusPrice.append(statusBadge, priceEl);

        const actionButtons = document.createElement('div');
        actionButtons.className = 'action-buttons space-y-3';

        if (schedule.bookable) {
            const bookLink = document.createElement('a');
            bookLink.href = this.getBookingUrl(schedule);
            bookLink.className = 'book-btn w-full bg-gradient-to-r from-emerald-500 to-teal-500 hover:from-emerald-600 hover:to-teal-600 text-white py-3 px-6 rounded-xl font-semibold shadow-lg hover:shadow-xl transition-all transform hover:-translate-y-1 flex items-center justify-center gap-2 text-center focus:outline-none focus:ring-2 focus:ring-emerald-500/50';
            bookLink.setAttribute('aria-label', `Book ferry from ${schedule.route?.departure || ''} to ${schedule.route?.destination || ''} departing ${schedule.departure_display || ''} (${schedule.available_seats} seats available)`);
            const bookIcon = document.createElement('i');
            bookIcon.className = 'fas fa-ticket-alt';
            bookIcon.setAttribute('aria-hidden', 'true');
            const bookText = document.createElement('span');
            bookText.textContent = `Book Now (${schedule.available_seats} seats)`;
            bookLink.append(bookIcon, bookText);
            actionButtons.appendChild(bookLink);
        } else if (schedule.available_seats === 0) {
            const soldOutBtn = document.createElement('button');
            soldOutBtn.type = 'button';
            soldOutBtn.className = 'unavailable-btn w-full bg-gray-300 text-gray-600 py-3 px-6 rounded-xl font-semibold cursor-not-allowed opacity-60 flex items-center justify-center gap-2 disabled';
            soldOutBtn.disabled = true;
            soldOutBtn.setAttribute('aria-label', 'This departure is sold out');
            const soldOutIcon = document.createElement('i');
            soldOutIcon.className = 'fas fa-chair';
            soldOutIcon.setAttribute('aria-hidden', 'true');
            const soldOutText = document.createElement('span');
            soldOutText.textContent = 'Sold Out';
            soldOutBtn.append(soldOutIcon, soldOutText);
            actionButtons.appendChild(soldOutBtn);
        } else {
            const statusBtn = document.createElement('button');
            statusBtn.type = 'button';
            statusBtn.className = 'cancelled-btn w-full bg-gradient-to-r from-red-500 to-rose-500 text-white py-3 px-6 rounded-xl font-semibold flex items-center justify-center gap-2 disabled';
            statusBtn.disabled = true;
            statusBtn.setAttribute('aria-label', `This departure is ${statusConfig.label.toLowerCase()} and cannot be booked`);
            const statusBtnIcon = document.createElement('i');
            statusBtnIcon.className = 'fas fa-times-circle';
            statusBtnIcon.setAttribute('aria-hidden', 'true');
            const statusBtnText = document.createElement('span');
            statusBtnText.textContent = statusConfig.label;
            statusBtn.append(statusBtnIcon, statusBtnText);
            actionButtons.appendChild(statusBtn);
        }

        const quickActions = document.createElement('div');
        quickActions.className = 'quick-actions flex gap-2 justify-center pt-3 bg-white/50 rounded-lg p-2';
        quickActions.setAttribute('role', 'group');
        quickActions.setAttribute('aria-label', 'Quick actions for this schedule');

        const saveBtn = document.createElement('button');
        saveBtn.type = 'button';
        saveBtn.className = 'quick-btn bg-gray-100 hover:bg-gray-200 text-gray-700 px-3 py-1 rounded-lg text-xs transition-all flex items-center gap-1 focus:outline-none focus:ring-2 focus:ring-gray-500/50';
        saveBtn.setAttribute('aria-label', 'Save this schedule to favorites');
        saveBtn.addEventListener('click', () => window.saveSchedule?.(schedule.id));
        const saveIcon = document.createElement('i');
        saveIcon.className = 'far fa-heart';
        saveIcon.setAttribute('aria-hidden', 'true');
        const saveText = document.createElement('span');
        saveText.className = 'sr-only';
        saveText.textContent = 'Save';
        saveBtn.append(saveIcon, saveText);

        const shareBtn = document.createElement('button');
        shareBtn.type = 'button';
        shareBtn.className = 'quick-btn bg-blue-100 hover:bg-blue-200 text-blue-700 px-3 py-1 rounded-lg text-xs transition-all flex items-center gap-1 focus:outline-none focus:ring-2 focus:ring-blue-500/50';
        shareBtn.setAttribute('aria-label', 'Share this schedule');
        shareBtn.addEventListener('click', () => window.shareSchedule?.(schedule.id));
        const shareIcon = document.createElement('i');
        shareIcon.className = 'fas fa-share-alt';
        shareIcon.setAttribute('aria-hidden', 'true');
        const shareText = document.createElement('span');
        shareText.className = 'sr-only';
        shareText.textContent = 'Share';
        shareBtn.append(shareIcon, shareText);

        quickActions.append(saveBtn, shareBtn);
        actionButtons.appendChild(quickActions);

        footer.append(statusPrice, actionButtons);
        card.append(routeInfo, footer);

        return card;
    }

    createWeatherMetricElement(type, scheduleId, iconClass, defaultText, ariaLabel) {
        const span = document.createElement('span');
        span.className = `weather-${type} inline-flex items-center gap-1`;
        span.id = `weather-${type}-${scheduleId}`;
        span.setAttribute('aria-label', ariaLabel);

        const icon = document.createElement('i');
        icon.className = iconClass;
        icon.setAttribute('aria-hidden', 'true');

        const value = document.createElement('span');
        value.textContent = defaultText;

        span.append(icon, value);
        return span;
    }

    getStatusConfig(schedule) {
        const status = (schedule.status || 'scheduled').toLowerCase();
        const label = schedule.status_display || status.charAt(0).toUpperCase() + status.slice(1);
        const configs = {
            scheduled: {
                badgeClass: 'bg-emerald-100 text-emerald-800',
                iconClass: 'fas fa-check-circle text-emerald-500',
                label
            },
            delayed: {
                badgeClass: 'bg-yellow-100 text-yellow-800',
                iconClass: 'fas fa-exclamation-triangle text-yellow-500',
                label
            },
            cancelled: {
                badgeClass: 'bg-red-100 text-red-800',
                iconClass: 'fas fa-times-circle text-red-500',
                label
            }
        };

        return configs[status] || {
            badgeClass: 'bg-gray-100 text-gray-700',
            iconClass: 'fas fa-clock text-gray-500',
            label
        };
    }

    getBookingUrl(schedule) {
        if (schedule.book_url) {
            return schedule.book_url;
        }

        const base = window.urls?.bookTicket || '/bookings/book/';
        const separator = base.includes('?') ? '&' : '?';
        return `${base}${schedule.id ? `${separator}schedule_id=${encodeURIComponent(schedule.id)}` : ''}`;
    }

    highlightNewSchedules(ids) {
        if (!Array.isArray(ids) || ids.length === 0 || !this.scheduleList) {
            return;
        }

        ids.forEach(id => {
            const card = this.scheduleList.querySelector(`[data-schedule-id="${id}"]`);
            if (!card) return;
            card.classList.add('ring-2', 'ring-emerald-400', 'shadow-xl');
            setTimeout(() => {
                card.classList.remove('ring-2', 'ring-emerald-400', 'shadow-xl');
            }, 3000);
        });

        FijiFerry.notificationManager?.show?.('Schedules updated', 'info', 3000);
    }

    async updateWeatherDisplay() {
        try {
            this.scheduleList = this.scheduleList || this.ensureScheduleList();
            const scheduleCards = this.scheduleList ? Array.from(this.scheduleList.querySelectorAll('.schedule-card')) : [];
            if (scheduleCards.length === 0) return;

            for (const card of scheduleCards) {
                const id = card.dataset.scheduleId;
                if (!id) continue;

                const weatherUrl = new URL('/bookings/api/weather/conditions/', window.location.origin);
                weatherUrl.searchParams.set('schedule_id', id);
                weatherUrl.searchParams.set('_', Date.now().toString());

                const response = await fetch(weatherUrl.toString());
                if (!response.ok) throw new Error(`Weather fetch failed for schedule ${id}`);

                const { weather } = await response.json();

                ['nadi', 'suva'].forEach(port => {
                    const data = weather?.ports?.[port] || {};
                    const iconEl = document.getElementById(`${port}-icon`);
                    const tempEl = document.getElementById(`${port}-temp`);
                    if (iconEl) iconEl.textContent = Utils.getWeatherIcon(data.condition);
                    if (tempEl) tempEl.textContent = `${data.temp || 28}°`;
                });

                const data = weather || {};
                ['condition', 'icon', 'temp', 'wind', 'precip'].forEach(field => {
                    const el = document.getElementById(`weather-${field}-${id}`);
                    if (!el) return;

                    const value =
                        field === 'icon' ? Utils.getWeatherIcon(data.condition) :
                        field === 'temp' ? `${Math.round(data.temperature ?? data.temp ?? 28)}°C` :
                        field === 'wind' ? `${Math.round(data.wind_speed ?? data.wind ?? 12)} kph` :
                        field === 'precip' ? `${Math.round(data.precipitation_probability ?? data.precip ?? 5)}%` :
                        data.condition || 'Sunny';

                    el.textContent = value;
                });
            }

            logger.log('✅ Weather updated per schedule from DB');
        } catch (error) {
            logger.warn('❌ Weather update failed:', error);
            this.applyFallbackWeather();
        }
    }

    applyFallbackWeather() {
        const fallback = Utils.safeParseJSON('weather-data-fallback', {});
        if (!fallback || !fallback.ports) return;

        ['nadi', 'suva'].forEach(port => {
            const data = fallback.ports[port] || {};
            const iconEl = document.getElementById(`${port}-icon`);
            const tempEl = document.getElementById(`${port}-temp`);
            if (iconEl) iconEl.textContent = Utils.getWeatherIcon(data.condition || 'Sunny');
            if (tempEl) tempEl.textContent = `${data.temp || 28}°`;
        });

        const list = this.scheduleList || this.ensureScheduleList();
        if (!list) return;

        list.querySelectorAll('.schedule-card').forEach(card => {
            const id = card.dataset.scheduleId;
            const data = fallback.current || {};
            ['condition', 'icon', 'temp', 'wind', 'precip'].forEach(field => {
                const el = document.getElementById(`weather-${field}-${id}`);
                if (!el) return;

                const value =
                    field === 'icon' ? Utils.getWeatherIcon(data.condition) :
                    field === 'temp' ? `${data.temp || 28}°C` :
                    field === 'wind' ? `${data.wind || 12} kph` :
                    field === 'precip' ? `${data.precip || 5}%` :
                    data.condition || 'Sunny';

                el.textContent = value;
            });
        });

        logger.log('Applied fallback weather');
    }
}

    // TESTIMONIAL MANAGER
    class TestimonialManager {
        constructor() {
            this.testimonials = document.querySelectorAll('.testimonial');
            this.current = 0;
            this.interval = null;
            this.init();
        }
        init() {
            this.startRotation();
        }
        startRotation() {
            this.interval = setInterval(() => {
                this.testimonials.forEach(t => t.classList.remove('active'));
                this.current = (this.current + 1) % this.testimonials.length;
                this.testimonials[this.current].classList.add('active');
            }, FijiFerry.config.testimonialInterval);
        }
        pauseAutoRotate() {
            clearInterval(this.interval);
        }
        resumeAutoRotate() {
            this.startRotation();
        }
    };

    // MAIN INITIALIZATION
    class HomepageManager {
        constructor() {
            this.components = new Map();
            this.isInitialized = false;
        }
        init() {
            if (this.isInitialized) return;
            this.isInitialized = true;
            Utils.applyTailwindOverrides();
            this.components.set('notifications', new NotificationManager());
            this.components.set('hero', new HeroManager());
            this.components.set('filters', new FilterManager());
            setTimeout(() => {
                if (document.getElementById('fiji-map')) {
                    this.components.set('map', new MapManager());
                }
            }, 200);
            setTimeout(() => {
                this.components.set('schedules', new ScheduleManager());
                this.components.set('testimonials', new TestimonialManager());
            }, 300);
            this.setupGlobalListeners();
            this.startBackgroundTasks();
            window.FijiFerry = FijiFerry;
            FijiFerry.homepage = this;
            FijiFerry.notificationManager = this.components.get('notifications');
            logger.log('Homepage initialized v2.5.0');
        }
        setupGlobalListeners() {
            document.addEventListener('keydown', (e) => {
                if (['INPUT', 'TEXTAREA', 'SELECT'].includes(document.activeElement.tagName)) return;
                if (e.key === 'Escape') this.components.get('notifications')?.closeAll?.();
                if ((e.ctrlKey || e.metaKey) && e.key === 'k') {
                    e.preventDefault();
                    document.getElementById('route')?.focus();
                }
                if ((e.ctrlKey || e.metaKey) && e.key === 'r') {
                    e.preventDefault();
                    window.resetSearch();
                }
            });
            window.addEventListener('online', () => {
                FijiFerry.notificationManager?.show('Connection restored', 'success', 3000);
            });
            window.addEventListener('offline', () => {
                FijiFerry.notificationManager?.show('Connection lost', 'warning', 5000);
            });
        }
        startBackgroundTasks() {
            const liveFerryCount = document.getElementById('live-ferry-count');
            const onScheduleCount = document.getElementById('on-schedule-count');
            if (liveFerryCount && onScheduleCount) {
                this.animateCounter(liveFerryCount, 5, 1500);
                this.animateCounter(onScheduleCount, 4, 1500);
            }
            const heroSlides = document.querySelectorAll('.hero-slide');
            if (heroSlides.length > 0) {
                const heroImages = Array.from(heroSlides).map(slide => slide.dataset.srcLight).filter(Boolean);
                Utils.preloadImages(heroImages);
            }
        }
        animateCounter(element, target, duration) {
            let start = 0;
            const increment = target / (duration / 16);
            const timer = setInterval(() => {
                start += increment;
                if (start >= target) {
                    start = target;
                    clearInterval(timer);
                }
                element.textContent = Math.floor(start);
            }, 16);
        }
        pauseAll() {
            this.components.get('hero')?.pauseSlideshow?.();
            this.components.get('testimonials')?.pauseAutoRotate?.();
        }
        resumeAll() {
            this.components.get('hero')?.resumeSlideshow?.();
            this.components.get('testimonials')?.resumeAutoRotate?.();
        }
        destroy() {
            this.components.forEach(comp => comp.destroy?.());
            this.components.clear();
            this.isInitialized = false;
        }
    };

    // GLOBAL FUNCTIONS
    window.saveSchedule = (id) => console.log(`Save schedule ${id}`);
    window.shareSchedule = (id) => console.log(`Share schedule ${id}`);
    window.scrollToSchedules = () => {
        document.getElementById('schedules-section')?.scrollIntoView({ behavior: 'smooth' });
    };

    // INITIALIZATION
    let initAttempted = false;
    function initialize() {
        if (initAttempted) return;
        initAttempted = true;
        const initWithDelay = () => {
            Utils.applyTailwindOverrides();
            FijiFerry.homepage = new HomepageManager();
            FijiFerry.homepage.init();
        };
        if (window.requestIdleCallback) {
            requestIdleCallback(initWithDelay, { timeout: 200 });
        } else {
            setTimeout(initWithDelay, 100);
        }
    }
    if (document.readyState === 'loading') {
        document.addEventListener('DOMContentLoaded', initialize);
    } else {
        initialize();
    }
    window.addEventListener('load', () => {
        if (!initAttempted) initialize();
    });
    window.addEventListener('beforeunload', () => {
        if (FijiFerry.homepage?.destroy) FijiFerry.homepage.destroy();
    });
    window.FijiFerry = FijiFerry;
    window.Utils = Utils;
})();