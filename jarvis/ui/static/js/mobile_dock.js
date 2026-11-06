/**
 * HM Algo 2.0 — MOBILE DOCK BOOTSTRAP
 *
 * The market pages render their bottom dock server-side, so its buttons are
 * tappable the moment the HTML paints. The handler that drives them, however,
 * lives in the page controller (stocks.js / india.js / india_options.js), which
 * is a blocking <script> near the END of the document. An inline
 * onclick="switchMobileStocksView('screener')" resolves that identifier at
 * CLICK time in global scope, so any tap landing in that window threw
 * "switchMobileStocksView is not defined" and the dock did nothing at all.
 *
 * That window is not theoretical: the page controllers are 1000+ lines and sit
 * ~500 lines of markup after the dock, and stocks.html / india.html additionally
 * pull lightweight-charts with a document.write CDN fallback in <head>, which
 * can stall parsing and widen the gap further.
 *
 * This file installs the three dock entry points up front so the dock is
 * functional from first paint. Each one applies the selected state immediately
 * and hands the view to the real controller as soon as it registers - so a tap
 * that arrives before the controller loads is honoured rather than dropped.
 *
 * Load it BEFORE the dock markup, without defer:
 *     <script src="/static/js/mobile_dock.js"></script>
 *     ...dock buttons with onclick="switchMobileStocksView('x')"...
 *
 * The controller then registers its real behaviour instead of assigning the
 * global directly:
 *     window.registerMobileView('stocks', applyMobileStocksView);
 *
 * Controllers still work if this file is absent - they fall back to assigning
 * the global themselves.
 */
(function () {
    'use strict';

    /* pageKey -> the controller's real implementation (registered later). */
    var REG = {};
    /* pageKey -> the last view the user asked for before the controller loaded. */
    var PENDING = {};

    /**
     * Called by a page controller once its real switcher is defined.
     * Replays an early tap so it is not silently lost.
     */
    window.registerMobileView = function (key, fn) {
        if (typeof fn !== 'function') return;
        REG[key] = fn;
        if (Object.prototype.hasOwnProperty.call(PENDING, key)) {
            fn(PENDING[key]);
        }
    };

    /**
     * Install one dock entry point.
     *
     * @param {string} pageKey  key the controller registers under
     * @param {string} fnName   global the inline onclick calls
     * @param {string[]} ids    dock button suffixes, i.e. 'screener' -> #mob-btn-screener
     */
    function install(pageKey, fnName, ids) {
        window[fnName] = function (view) {
            PENDING[pageKey] = view;

            /* Reflect the selection now: the dock must feel alive even before
               the controller arrives, and this is also what makes the tap
               observable if the controller never loads. */
            for (var i = 0; i < ids.length; i++) {
                var btn = document.getElementById('mob-btn-' + ids[i]);
                if (!btn) continue;
                var on = ids[i] === view;
                btn.classList.toggle('active', on);
                btn.setAttribute('aria-selected', on ? 'true' : 'false');
            }

            if (REG[pageKey]) REG[pageKey](view);
        };
    }

    /* The three docks that use inline onclick. Keyed exactly as the page
       controllers register below, and the id lists mirror each dock's markup. */
    install('stocks', 'switchMobileStocksView', ['setups', 'screener', 'heatmap', 'all']);
    install('india', 'switchMobileIndiaView', ['buys', 'screener', 'heatmap', 'all']);
    install('options', 'switchMobileOptionsView', ['advisor', 'singles', 'spreads', 'payoff', 'chain', 'all']);
})();
