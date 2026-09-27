"""
Automated Multi-Surface Responsive UI & Bugatti Cockpit Navigation Alignment Test Suite.
Validates:
1. Universal Bootstrap 5 & Bugatti Design System integration across all 7 page surfaces.
2. Complete Universal Bugatti Navbar deployment with 6-desk routing parity.
3. Multi-breakpoint responsive layout constraints (320px, 576px, 768px, 992px, 1400px+).
4. Interactive widget integrity (Lifecycle Stepper, Speedometer Gauge, MFE/MAE bars, Failsafe Modal).
"""
import unittest
import os
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
TEMPLATES_DIR = REPO_ROOT / "jarvis" / "ui" / "templates"
CSS_DIR = REPO_ROOT / "jarvis" / "ui" / "static" / "css"
JS_DIR = REPO_ROOT / "jarvis" / "ui" / "static" / "js"


class TestResponsiveBugattiCockpit(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.templates = {
            "dashboard": (TEMPLATES_DIR / "dashboard.html").read_text(encoding="utf-8"),
            "console": (TEMPLATES_DIR / "console.html").read_text(encoding="utf-8"),
            "positions": (TEMPLATES_DIR / "positions.html").read_text(encoding="utf-8"),
            "stocks": (TEMPLATES_DIR / "stocks.html").read_text(encoding="utf-8"),
            "india": (TEMPLATES_DIR / "india.html").read_text(encoding="utf-8"),
            "options": (TEMPLATES_DIR / "india_options.html").read_text(encoding="utf-8"),
            "classic": (TEMPLATES_DIR / "index.html").read_text(encoding="utf-8"),
        }
        cls.theme_bugatti = (CSS_DIR / "theme_bugatti.css").read_text(encoding="utf-8")
        cls.bootstrap_css = (CSS_DIR / "bootstrap.min.css").read_text(encoding="utf-8")
        cls.bootstrap_js = (JS_DIR / "vendor" / "bootstrap.bundle.min.js").read_text(encoding="utf-8")

    def test_bootstrap5_assets_present_locally(self):
        """Verify Bootstrap 5.3.3 CSS and JS bundle are locally cached for zero-latency offline operation."""
        self.assertGreater(len(self.bootstrap_css), 100000)
        self.assertGreater(len(self.bootstrap_js), 50000)

    def test_all_templates_load_bootstrap_and_bugatti_theme(self):
        """Verify all 7 surfaces link Bootstrap 5 and theme_bugatti.css."""
        for name, html in self.templates.items():
            self.assertIn("bootstrap.min.css", html, f"{name}.html missing bootstrap.min.css")
            self.assertIn("theme_bugatti.css", html, f"{name}.html missing theme_bugatti.css")
            self.assertIn("bootstrap.bundle.min.js", html, f"{name}.html missing bootstrap.bundle.min.js")

    def test_universal_bugatti_navbar_desk_parity(self):
        """Verify all 6 active market desks exist in navigation across primary surfaces."""
        desks = ['href="/"', 'href="/console"', 'href="/positions"', 'href="/stocks"', 'href="/india"', 'href="/options"']
        for surface in ["dashboard", "console", "positions", "stocks", "india", "options"]:
            html = self.templates[surface]
            self.assertIn("bugatti-navbar", html, f"{surface} missing bugatti-navbar class")
            for d in desks:
                self.assertIn(d, html, f"{surface} missing desk route link: {d}")

    def test_responsive_layout_breakpoints_in_css(self):
        """Verify theme_bugatti.css implements required responsive breakpoint rules."""
        css = self.theme_bugatti
        # Phone < 576px
        self.assertIn("max-width: 575.98px", css)
        # Tablet 576px - 991px
        self.assertIn("max-width: 991.98px", css)
        # Desktop >= 992px
        self.assertIn("min-width: 992px", css)
        # Wide desktop >= 1400px
        self.assertIn("min-width: 1400px", css)
        # Sticky table first column
        self.assertIn("position: sticky", css)
        # Touch horizontal scroll
        self.assertIn("-webkit-overflow-scrolling: touch", css)
        # Dynamic chart heights
        self.assertIn(".tt-chart__body", css)

    def test_dashboard_cockpit_widgets_intact(self):
        """Verify trading cockpit contains all 7 execution lifecycle steps, gauge, and telemetry."""
        html = self.templates["dashboard"]
        # Stepper nodes
        self.assertIn('id="step-candidate"', html)
        self.assertIn('id="step-regime"', html)
        self.assertIn('id="step-analysts"', html)
        self.assertIn('id="step-gate"', html)
        self.assertIn('id="step-hrp"', html)
        self.assertIn('id="step-execution"', html)
        self.assertIn('id="step-trailing"', html)
        # Speedometer Portfolio Heat Gauge
        self.assertIn('id="cockpit-gauge-meter"', html)
        self.assertIn('id="cockpit-heat-val"', html)
        self.assertIn('id="cockpit-heat-zone"', html)
        # Failsafe Confirmation Modal
        self.assertIn('id="cockpit-confirm-modal"', html)
        self.assertIn('id="confirm-modal-proceed"', html)
        # Dynamic MFE/MAE column in positions table
        self.assertIn("MFE / MAE", html)

    def test_unified_navbar_hierarchy_and_markets_dropdown(self):
        """Verify navigation bar follows HM ALGO 2.0 | Cockpit | Trade | News | Analyst | Markets ▼ | Analytics | Backtest hierarchy."""
        surfaces = ["dashboard", "stocks", "india", "options", "console", "positions"]
        for s in surfaces:
            html = self.templates[s]
            self.assertIn("Markets", html, f"{s} missing Markets dropdown")
            self.assertIn('id="markets-menu"', html, f"{s} missing markets-menu panel")
            # Verify Markets dropdown contains US Market, Indian Market, and Options
            self.assertIn('href="/stocks"', html, f"{s} missing US Stocks link in Markets dropdown")
            self.assertIn('href="/india"', html, f"{s} missing India Stocks link in Markets dropdown")
            self.assertIn('href="/options"', html, f"{s} missing Options link in Markets dropdown")

    def test_trade_history_filters_and_pagination(self):
        """Verify Trade History includes all requested multi-dimensional filters and full pagination controls."""
        html = self.templates["dashboard"]
        # Required multi-dimensional filters
        self.assertIn('id="hist-filter-symbol"', html)
        self.assertIn('id="hist-filter-side"', html)
        self.assertIn('id="hist-filter-strategy"', html)
        self.assertIn('id="hist-filter-regime"', html)
        self.assertIn('id="hist-filter-outcome"', html)
        self.assertIn('id="hist-filter-exit"', html)
        self.assertIn('id="hist-filter-style"', html)
        self.assertIn('id="hist-date-preset"', html)
        self.assertIn('id="hist-date-from"', html)
        self.assertIn('id="hist-date-to"', html)
        self.assertIn('id="hist-filter-days"', html)

        # Pagination controls
        self.assertIn('id="hist-pagination-bar"', html)
        self.assertIn('id="hist-page-info"', html)
        self.assertIn('id="hist-page-size"', html)
        self.assertIn('id="hist-page-first"', html)
        self.assertIn('id="hist-page-prev"', html)
        self.assertIn('id="hist-page-next"', html)
        self.assertIn('id="hist-page-last"', html)
        self.assertIn('id="hist-page-numbers"', html)

    def test_positions_panel_redesign_and_history_pagination(self):
        """Verify Positions panel contains redesigned tabs and history pagination controls."""
        html = self.templates["dashboard"]
        self.assertIn('id="pos-tab-count-open"', html)
        self.assertIn('id="pos-tab-count-history"', html)
        self.assertIn('id="pos-tab-count-pending"', html)
        self.assertIn('id="pos-hist-pagination"', html)
        self.assertIn('id="pos-hist-page-size"', html)
        self.assertIn('id="pos-hist-first"', html)
        self.assertIn('id="pos-hist-prev"', html)
        self.assertIn('id="pos-hist-next"', html)
        self.assertIn('id="pos-hist-last"', html)
        self.assertIn('id="pos-hist-range"', html)
        self.assertIn('id="pos-hist-total"', html)
        self.assertIn('id="pos-hist-page-indicator"', html)

    def test_expanded_sidebar_widths_and_dropdown_hover(self):
        """Verify Bugatti theme contains expanded sidebar widths, dropdown hover rules, and positions tab styles."""
        css = self.theme_bugatti
        self.assertIn(".bugatti-dropdown:hover > .bugatti-dropdown__menu", css)
        self.assertIn(".pos-hist-pagination-bar", css)
        self.assertIn(".tt-pos-tabs", css)
        self.assertIn(".tt-pos-tab", css)
        self.assertIn("minmax(340px, 390px)", css)


if __name__ == "__main__":
    unittest.main()


