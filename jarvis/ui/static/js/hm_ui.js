/* ==========================================================================
   HM Algo 2.0 — SHARED UX / ACCESSIBILITY LAYER
   --------------------------------------------------------------------------
   Loaded last, after every page script. Responsibilities:

     1. Toast system + a non-blocking `window.alert` shim
     2. Modal focus management (trap / Escape / restore / scroll lock)
     3. Tab-strip semantics + arrow-key navigation
     4. Live region for screen-reader announcements
     5. Active-page navigation marking
     6. Skip-link target focus handling

   Design constraint: this file must work with ZERO edits to the four page
   scripts and ZERO required edits to the templates. Everything is feature-
   detected and degrades to a no-op. Every mutation is additive - no existing
   element is removed, re-parented or renamed.
   ========================================================================== */

(function () {
    'use strict';

    var doc = document;
    var root = doc.documentElement;

    /* ======================================================================
       0. Utilities
       ====================================================================== */

    function ready(fn) {
        if (doc.readyState === 'loading') {
            doc.addEventListener('DOMContentLoaded', fn, { once: true });
        } else {
            fn();
        }
    }

    function el(tag, attrs, children) {
        var node = doc.createElement(tag);
        if (attrs) {
            Object.keys(attrs).forEach(function (k) {
                if (k === 'class') node.className = attrs[k];
                else if (k === 'text') node.textContent = attrs[k];
                else node.setAttribute(k, attrs[k]);
            });
        }
        (children || []).forEach(function (c) { node.appendChild(c); });
        return node;
    }

    // setAttribute queues a MutationObserver record even when the value is
    // already what we are writing. A callback that both observes an attribute
    // and writes it therefore re-queues itself on every pass, and because
    // microtasks drain before the browser may paint or dispatch input, that
    // spins the main thread forever with no error to show for it. Writing only
    // on a real change is what makes such a callback settle: once the group
    // matches its markup, a pass produces no records and the loop stops.
    function setIfChanged(node, name, value) {
        if (node.getAttribute(name) !== value) node.setAttribute(name, value);
    }

    var FOCUSABLE = [
        'a[href]', 'area[href]', 'button:not([disabled])',
        'input:not([disabled]):not([type="hidden"])',
        'select:not([disabled])', 'textarea:not([disabled])',
        'iframe', 'audio[controls]', 'video[controls]',
        '[contenteditable]:not([contenteditable="false"])',
        '[tabindex]:not([tabindex="-1"])'
    ].join(',');

    function focusableWithin(container) {
        var nodes = Array.prototype.slice.call(container.querySelectorAll(FOCUSABLE));
        return nodes.filter(function (n) {
            if (n.hasAttribute('hidden')) return false;
            if (n.getAttribute('aria-hidden') === 'true') return false;
            // offsetParent is null for display:none subtrees (but also for
            // position:fixed elements, so only use it as a soft filter).
            if (n.offsetParent === null && getComputedStyle(n).position !== 'fixed') return false;
            return true;
        });
    }


    /* ======================================================================
       1. TOAST SYSTEM
       ====================================================================== */

    var toastStack = null;

    function ensureToastStack() {
        if (toastStack && doc.body.contains(toastStack)) return toastStack;
        toastStack = el('div', {
            'class': 'hm-toast-stack',
            id: 'hm-toast-stack',
            role: 'region',
            'aria-label': 'Notifications'
        });
        (doc.body || root).appendChild(toastStack);
        return toastStack;
    }

    var ICONS = {
        success: '\u2705',   // check mark
        error: '\u26A0\uFE0F', // warning
        warning: '\u26A0\uFE0F',
        info: '\u2139\uFE0F'   // information
    };

    /**
     * Show a non-blocking toast.
     * @param {string} message
     * @param {{type?:string, duration?:number, id?:string}} [opts]
     * @returns {HTMLElement|null} the toast node
     */
    function toast(message, opts) {
        if (message === undefined || message === null) return null;
        opts = opts || {};
        var type = opts.type || 'info';
        var duration = opts.duration === undefined ? 5200 : opts.duration;

        var stack = ensureToastStack();

        var closeBtn = el('button', {
            'class': 'hm-toast__close',
            type: 'button',
            'aria-label': 'Dismiss notification',
            text: '\u00D7'
        });

        var node = el('div', {
            'class': 'hm-toast hm-toast--' + type,
            role: 'status',
            'aria-live': 'polite'
        }, [
            el('span', { 'class': 'hm-toast__icon', 'aria-hidden': 'true', text: ICONS[type] || ICONS.info }),
            el('div', { 'class': 'hm-toast__body', text: String(message) }),
            closeBtn
        ]);

        function dismiss() {
            if (!node.parentNode) return;
            node.classList.add('hm-toast--leaving');
            var done = function () {
                if (node.parentNode) node.parentNode.removeChild(node);
            };
            node.addEventListener('animationend', done, { once: true });
            // Fallback in case the animation is suppressed (reduced motion).
            setTimeout(done, 400);
        }

        closeBtn.addEventListener('click', dismiss);

        // Newest first, so the most recent message is closest to the top.
        if (stack.firstChild) stack.insertBefore(node, stack.firstChild);
        else stack.appendChild(node);

        if (duration > 0) setTimeout(dismiss, duration);

        return node;
    }

    /**
     * Infer a severity from an existing alert() string so the ten call sites
     * keep their current wording but gain colour-coded feedback. Deliberately
     * conservative: anything not clearly a failure reads as informational.
     */
    function inferType(msg) {
        var s = String(msg).toLowerCase();
        if (/(fail|failed|reject|rejected|error|could not|cannot|unable|invalid|unauthorized|not authorized|denied)/.test(s)) {
            return 'error';
        }
        if (/(warn|caution|careful|expiring|low margin)/.test(s)) return 'warning';
        if (/(closed|copied|cancelled|canceled|success|placed|complete|done|saved)/.test(s)) {
            return 'success';
        }
        return 'info';
    }

    /**
     * Replace the native blocking alert with the toast. Call sites are left
     * byte-identical: they still call `alert(...)` and still get `undefined`
     * back, but the UI no longer freezes and the message is announced to
     * assistive technology via the toast's `role="status"`.
     *
     * Long messages (the multi-line basket confirmations) get a longer dwell
     * time so they stay readable.
     */
    function installAlertShim() {
        if (window.__hmAlertShimInstalled) return;
        window.__hmAlertShimInstalled = true;

        var nativeAlert = window.alert ? window.alert.bind(window) : null;

        window.alert = function (message) {
            var text = message === undefined || message === null ? '' : String(message);
            // Guard against an empty alert producing an invisible toast.
            if (!text.trim()) text = '(no message)';

            var type = inferType(text);
            var duration = text.length > 120 ? 9000 : 5200;

            try {
                toast(text, { type: type, duration: duration });
            } catch (err) {
                // If anything in the toast path throws we must not swallow the
                // message - fall back to the original behaviour.
                if (nativeAlert) nativeAlert(message);
            }
        };

        // Keep a handle so future code (or a debugger) can reach the original.
        window.__hmNativeAlert = nativeAlert;
    }


    /* ======================================================================
       2. LIVE REGION (screen-reader announcements)
       ====================================================================== */

    var liveRegion = null;

    function ensureLiveRegion() {
        if (liveRegion && doc.body.contains(liveRegion)) return liveRegion;
        liveRegion = el('div', {
            'class': 'sr-only',
            id: 'hm-live-region',
            role: 'status',
            'aria-live': 'polite',
            'aria-atomic': 'true'
        });
        (doc.body || root).appendChild(liveRegion);
        return liveRegion;
    }

    function announce(message) {
        var region = ensureLiveRegion();
        region.textContent = '';
        // A microtask gap guarantees the change is observed even when the same
        // string is announced twice in a row.
        setTimeout(function () { region.textContent = String(message); }, 30);
    }


    /* ======================================================================
       3. MODAL FOCUS MANAGEMENT
       ----------------------------------------------------------------------
       The templates show and hide modals by writing inline `style.display`.
       Rather than rewrite that logic, we observe the `style` attribute and
       apply focus management when a dialog opens or closes.
       ====================================================================== */

    var MODAL_SELECTORS = [
        '#command-palette-overlay',
        '#macro-news-modal',
        '#remote-login-modal',
        '.macro-news-modal-overlay',
        '.command-palette-overlay'
    ];

    var openModal = null;
    var lastFocused = null;

    function isVisible(node) {
        if (!node) return false;
        if (node.hasAttribute('hidden')) return false;
        var inline = node.style && node.style.display;
        if (inline === 'none') return false;
        return getComputedStyle(node).display !== 'none';
    }

    function onKeydownModal(ev) {
        if (!openModal) return;

        if (ev.key === 'Escape' || ev.key === 'Esc') {
            ev.stopPropagation();
            // Click the backdrop if the page wired one up (all three modals
            // close on backdrop click), otherwise hide directly.
            var backdropHandler = openModal.getAttribute('data-hm-close') || null;
            if (backdropHandler === 'click') {
                openModal.click();
            } else {
                openModal.style.display = 'none';
            }
            return;
        }

        if (ev.key !== 'Tab') return;

        var items = focusableWithin(openModal);
        if (!items.length) {
            ev.preventDefault();
            openModal.setAttribute('tabindex', '-1');
            openModal.focus();
            return;
        }
        var first = items[0];
        var last = items[items.length - 1];
        var active = doc.activeElement;

        if (ev.shiftKey) {
            if (active === first || !openModal.contains(active)) {
                ev.preventDefault();
                last.focus();
            }
        } else {
            if (active === last || !openModal.contains(active)) {
                ev.preventDefault();
                first.focus();
            }
        }
    }

    function activateModal(node) {
        if (openModal === node) return;
        if (openModal) deactivateModal(openModal);

        openModal = node;
        lastFocused = doc.activeElement;

        // Dialog semantics, only if the template did not already provide them.
        if (!node.hasAttribute('role')) node.setAttribute('role', 'dialog');
        if (!node.hasAttribute('aria-modal')) node.setAttribute('aria-modal', 'true');
        if (!node.hasAttribute('aria-label') && !node.hasAttribute('aria-labelledby')) {
            var heading = node.querySelector('h1, h2, h3, [class*="title"]');
            if (heading) {
                if (!heading.id) heading.id = 'hm-modal-title-' + Math.random().toString(36).slice(2, 8);
                node.setAttribute('aria-labelledby', heading.id);
            } else {
                node.setAttribute('aria-label', 'Dialog');
            }
        }

        doc.body.classList.add('hm-modal-open');
        doc.addEventListener('keydown', onKeydownModal, true);

        // Move focus into the dialog. A short delay lets the open animation
        // settle so the browser does not scroll to a mid-transition position.
        setTimeout(function () {
            var items = focusableWithin(node);
            if (items.length) {
                items[0].focus();
            } else {
                node.setAttribute('tabindex', '-1');
                node.focus();
            }
        }, 40);
    }

    function deactivateModal(node) {
        if (!node) return;
        doc.removeEventListener('keydown', onKeydownModal, true);
        doc.body.classList.remove('hm-modal-open');

        // Restore focus to whatever the user was on before the dialog opened -
        // otherwise keyboard focus is dumped back to <body> and the next Tab
        // restarts from the top of the page.
        var target = lastFocused;
        openModal = null;
        lastFocused = null;
        if (target && doc.contains(target) && typeof target.focus === 'function') {
            try { target.focus(); } catch (e) { /* element went away */ }
        }
    }

    function watchModals() {
        var seen = [];

        function collect() {
            MODAL_SELECTORS.forEach(function (sel) {
                Array.prototype.forEach.call(doc.querySelectorAll(sel), function (node) {
                    if (seen.indexOf(node) !== -1) return;
                    seen.push(node);

                    // Record how the page closes this modal so Escape can
                    // mirror it. The templates attach an inline onclick to the
                    // backdrop element itself.
                    if (node.getAttribute('onclick')) node.setAttribute('data-hm-close', 'click');

                    new MutationObserver(function () {
                        if (isVisible(node)) activateModal(node);
                        else if (openModal === node) deactivateModal(node);
                    }).observe(node, { attributes: true, attributeFilter: ['style', 'class', 'hidden'] });

                    if (isVisible(node)) activateModal(node);
                });
            });
        }

        collect();
        // Templates are static, but late-injected dialogs (e.g. an auth gate
        // rendered by a page script) should still be picked up.
        new MutationObserver(collect).observe(doc.body || root, { childList: true, subtree: true });
    }


    /* ======================================================================
       4. TAB-STRIP SEMANTICS
       ----------------------------------------------------------------------
       The mobile tab bars and in-page tab strips are plain <button> rows with
       an `active` class. We add the ARIA tab pattern and arrow-key support
       without touching the existing click handlers, so the page's own
       `switchMobileView(...)` etc. keep working exactly as before.
       ====================================================================== */

    var TAB_GROUPS = [
        '#mobile-nav-bar',
        '.mobile-nav-bar',
        '.tab-strip',
        '.tabs',
        '[role="tablist"]'
    ];

    function enhanceTabGroup(group) {
        if (group.getAttribute('data-hm-tabs') === 'done') return;

        var tabs = Array.prototype.slice.call(group.querySelectorAll('button, [role="tab"]'));
        if (tabs.length < 2) return;

        group.setAttribute('data-hm-tabs', 'done');
        if (!group.hasAttribute('role')) group.setAttribute('role', 'tablist');
        if (!group.hasAttribute('aria-label')) {
            group.setAttribute('aria-label', group.id === 'mobile-nav-bar'
                ? 'View selector'
                : 'Section tabs');
        }

        // `sync` writes `aria-selected`, which the observer below is watching,
        // so it must be idempotent or it feeds itself: every pass would queue a
        // record for the next, and the microtask queue would never drain - a
        // hard main-thread freeze with no error and no recovery. The guard flag
        // covers re-entry, and setIfChanged keeps a settled group silent.
        var syncing = false;

        function sync() {
            if (syncing) return;
            syncing = true;
            try {
                tabs.forEach(function (t) {
                    var isActive = t.classList.contains('active') ||
                                   t.getAttribute('aria-selected') === 'true';
                    setIfChanged(t, 'role', 'tab');
                    setIfChanged(t, 'aria-selected', isActive ? 'true' : 'false');
                    // Roving tabindex: only the selected tab is in the tab order.
                    setIfChanged(t, 'tabindex', isActive ? '0' : '-1');
                    if (!t.id) t.id = 'hm-tab-' + Math.random().toString(36).slice(2, 8);
                });
            } finally {
                syncing = false;
            }
        }

        sync();

        // The page scripts toggle `.active` directly, so watch for it and keep
        // ARIA in sync rather than fighting over who owns the state.
        new MutationObserver(sync).observe(group, {
            subtree: true,
            attributes: true,
            attributeFilter: ['class', 'aria-selected']
        });

        group.addEventListener('keydown', function (ev) {
            var idx = tabs.indexOf(doc.activeElement);
            if (idx === -1) return;

            var next = null;
            if (ev.key === 'ArrowRight' || ev.key === 'ArrowDown') {
                next = tabs[(idx + 1) % tabs.length];
            } else if (ev.key === 'ArrowLeft' || ev.key === 'ArrowUp') {
                next = tabs[(idx - 1 + tabs.length) % tabs.length];
            } else if (ev.key === 'Home') {
                next = tabs[0];
            } else if (ev.key === 'End') {
                next = tabs[tabs.length - 1];
            }

            if (next) {
                ev.preventDefault();
                // Activate on arrow, which is the recommended pattern when the
                // panel swap is cheap (it is here - it is a class toggle).
                next.focus();
                next.click();
            }
        });
    }

    function enhanceTabs() {
        TAB_GROUPS.forEach(function (sel) {
            Array.prototype.forEach.call(doc.querySelectorAll(sel), enhanceTabGroup);
        });
    }


    /* ======================================================================
       5. ACTIVE-PAGE NAVIGATION
       ----------------------------------------------------------------------
       Removes the `btn-nav-active` (stocks) vs `active` (india/options)
       divergence: the current page is derived from the URL and marked with
       BOTH class names plus the canonical `aria-current="page"`, so whichever
       convention a page sheet styles will light up correctly.
       ====================================================================== */

    function normalisePath(p) {
        if (!p) return '/';
        p = p.replace(/\/+$/, '');
        if (p === '' || p === '/index.html') return '/';
        return p;
    }

    function markActiveNav() {
        var here = normalisePath(window.location.pathname);

        // Map the alias routes the server also accepts onto one canonical path.
        var ALIASES = {
            '/index.html': '/',
            '/stocks.html': '/stocks',
            '/screener': '/stocks',
            '/india.html': '/india',
            '/india/stocks': '/india',
            '/nse': '/india',
            '/bse': '/india',
            '/options.html': '/options',
            '/india/options': '/options',
            '/india-options': '/options',
            '/fno': '/options'
        };
        here = ALIASES[here] || here;

        var links = doc.querySelectorAll(
            '.btn-nav-switch, .market-nav-item, .nav-links-wrapper a[href], .hm-nav a[href], .bugatti-desk-link, .cx-desk'
        );

        Array.prototype.forEach.call(links, function (a) {
            var href = a.getAttribute('href');
            if (!href || href.charAt(0) === '#' || href.indexOf('http') === 0) return;

            var target = normalisePath(href.split('?')[0]);
            target = ALIASES[target] || target;

            if (target === here) {
                a.classList.add('active', 'btn-nav-active');
                a.setAttribute('aria-current', 'page');
            } else {
                a.classList.remove('active', 'btn-nav-active');
                a.removeAttribute('aria-current');
            }
        });
    }


    /* ======================================================================
       6. SKIP LINK + LANDMARKS
       ====================================================================== */

    function ensureSkipLink() {
        // Only one page currently has a <main> landmark, so create one where
        // it is missing rather than leaving the skip link with no target.
        var main = doc.querySelector('main');
        if (!main) {
            var body = doc.body;
            if (!body) return;
            var candidates = body.querySelectorAll(':scope > div, :scope > section');
            var best = null;
            var bestArea = 0;
            Array.prototype.forEach.call(candidates, function (c) {
                var r = c.getBoundingClientRect();
                var area = r.width * r.height;
                if (area > bestArea) { bestArea = area; best = c; }
            });
            if (best && bestArea > 0) {
                // Wrap-in-place would re-parent nodes and could break the page
                // scripts' element references, so instead we mark the largest
                // region as the main landmark.
                best.setAttribute('role', 'main');
                main = best;
            }
        }

        if (main) {
            if (!main.id) main.id = 'hm-main';
            if (!main.hasAttribute('tabindex')) main.setAttribute('tabindex', '-1');
        }

        if (doc.querySelector('.hm-skip-link')) return;

        var target = main ? '#' + main.id : '#hm-main';
        var link = el('a', {
            'class': 'hm-skip-link',
            href: target,
            text: 'Skip to main content'
        });

        link.addEventListener('click', function (ev) {
            var dest = doc.querySelector(target);
            if (!dest) return;
            ev.preventDefault();
            dest.focus();
            dest.scrollIntoView({ block: 'start', behavior: 'smooth' });
        });

        var body = doc.body;
        if (body) body.insertBefore(link, body.firstChild);
    }


    /* ======================================================================
       7. ICON-ONLY BUTTON LABELLING
       ----------------------------------------------------------------------
       Several controls are emoji-only or glyph-only with no accessible name.
       Where we can derive an intent from the inline handler or a title, we
       promote that to an aria-label. Purely additive.
       ====================================================================== */

    function labelIconButtons() {
        var nodes = doc.querySelectorAll('button:not([aria-label])');
        Array.prototype.forEach.call(nodes, function (b) {
            if (b.textContent && b.textContent.trim().length > 1) return; // has a label
            var title = b.getAttribute('title');
            if (title) {
                b.setAttribute('aria-label', title);
                return;
            }
            var handler = b.getAttribute('onclick') || '';
            var m = handler.match(/(?:window\.)?([A-Za-z_$][\w$]*)\s*\(/);
            if (m) {
                // Turn camelCase / snake_case into words: closeAllPositions -> "close all positions"
                var words = m[1]
                    .replace(/([a-z0-9])([A-Z])/g, '$1 $2')
                    .replace(/_/g, ' ')
                    .toLowerCase()
                    .trim();
                if (words) b.setAttribute('aria-label', words.charAt(0).toUpperCase() + words.slice(1));
            }
        });
    }


    /* ======================================================================
       8. PUBLIC API
       ====================================================================== */

    window.HMUI = {
        toast: toast,
        announce: announce,
        markActiveNav: markActiveNav,
        version: '1.0.0'
    };


    /* ======================================================================
       9. BOOTSTRAP
       ====================================================================== */

    // Install the alert shim synchronously so it is in place before any page
    // script can capture a reference to the native function.
    installAlertShim();

    function wireBugattiDropdowns() {
        if (doc.querySelector('.tt-app')) return; // Dashboard handles its own dropdowns
        doc.addEventListener('click', function (ev) {
            var trigger = ev.target.closest('[data-dropdown-trigger]');
            if (trigger) {
                var panelId = trigger.getAttribute('aria-controls');
                var panel = panelId ? doc.getElementById(panelId) : (trigger.parentElement ? trigger.parentElement.querySelector('[data-dropdown-panel]') : null);
                if (panel) {
                    ev.preventDefault();
                    var willOpen = panel.hasAttribute('hidden');
                    doc.querySelectorAll('.bugatti-dropdown__menu:not([hidden])').forEach(function (m) {
                        m.setAttribute('hidden', '');
                    });
                    if (willOpen) {
                        panel.removeAttribute('hidden');
                        trigger.setAttribute('aria-expanded', 'true');
                    } else {
                        panel.setAttribute('hidden', '');
                        trigger.setAttribute('aria-expanded', 'false');
                    }
                    return;
                }
            }
            if (!ev.target.closest('[data-dropdown-panel]')) {
                doc.querySelectorAll('.bugatti-dropdown__menu:not([hidden])').forEach(function (m) {
                    m.setAttribute('hidden', '');
                    var parent = m.closest('.bugatti-dropdown');
                    if (parent) {
                        var btn = parent.querySelector('[data-dropdown-trigger]');
                        if (btn) btn.setAttribute('aria-expanded', 'false');
                    }
                });
            }
        });
    }

    ready(function () {
        ensureLiveRegion();
        ensureSkipLink();
        markActiveNav();
        enhanceTabs();
        watchModals();
        labelIconButtons();
        wireBugattiDropdowns();

        // The page scripts mutate the DOM after their own async work resolves,
        // so re-run the cheap passes once the first paint settles.
        setTimeout(function () {
            enhanceTabs();
            labelIconButtons();
            markActiveNav();
        }, 1200);
    });

    // Expose the internals for testing without polluting the global namespace
    // with anything the page scripts could accidentally collide with.
    window.__hmUIInternals = {
        focusableWithin: focusableWithin,
        isVisible: isVisible,
        inferType: inferType
    };
})();
