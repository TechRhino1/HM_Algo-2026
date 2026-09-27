/**
 * HM Algo 2.0 — Universal Authentication & Session Management Module
 * Handles login modal, token persistence, user profile header widgets, and server-side logout.
 */
(function () {
    "use strict";

    const AUTH_STORAGE_KEY = "jarvis_auth_token";
    const USER_STORAGE_KEY = "jarvis_user_info";

    window.HM_AUTH = {
        getToken: function () {
            let token = "";
            try { token = localStorage.getItem(AUTH_STORAGE_KEY) || ""; } catch (e) {}
            if (!token) {
                try { token = sessionStorage.getItem(AUTH_STORAGE_KEY) || ""; } catch (e) {}
            }
            return token || "";
        },

        getUser: function () {
            try {
                const raw = localStorage.getItem(USER_STORAGE_KEY) || sessionStorage.getItem(USER_STORAGE_KEY);
                return raw ? JSON.parse(raw) : null;
            } catch (e) {
                return null;
            }
        },

        getRememberedUser: function () {
            try {
                return localStorage.getItem("jarvis_remembered_user") || "";
            } catch (e) {
                return "";
            }
        },

        saveSession: function (data) {
            if (!data || !data.status) return;

            const userInfo = {
                username: data.username || "admin",
                role: data.role || "ADMIN",
                full_name: data.full_name || "System Administrator"
            };
            try {
                localStorage.setItem(USER_STORAGE_KEY, JSON.stringify(userInfo));
                sessionStorage.setItem(USER_STORAGE_KEY, JSON.stringify(userInfo));
                localStorage.setItem("jarvis_remembered_user", userInfo.username);
            } catch (e) {}

            this.updateHeaderUI(userInfo);
            window.dispatchEvent(new CustomEvent("jarvis:auth_changed", { detail: { authenticated: true, user: userInfo } }));
        },

        clearSession: function () {
            try { localStorage.removeItem(AUTH_STORAGE_KEY); } catch (e) {}
            try { sessionStorage.removeItem(AUTH_STORAGE_KEY); } catch (e) {}
            try { localStorage.removeItem(USER_STORAGE_KEY); } catch (e) {}
            try { sessionStorage.removeItem(USER_STORAGE_KEY); } catch (e) {}
            try {
                document.cookie = `${AUTH_STORAGE_KEY}=; path=/; max-age=0; expires=Thu, 01 Jan 1970 00:00:00 GMT; SameSite=Lax`;
            } catch (e) {}
            this.updateHeaderUI(null);
            window.dispatchEvent(new CustomEvent("jarvis:auth_changed", { detail: { authenticated: false } }));
        },

        verifyToken: async function () {
            try {
                const res = await fetch("/api/auth/verify", {
                    method: "POST",
                    headers: { "Content-Type": "application/json" }
                });
                if (res.status === 401) {
                    this.clearSession();
                    return false;
                }
                const data = await res.json();
                if (data && data.valid) {
                    const user = data.user || { username: "admin", role: "ADMIN" };
                    try {
                        localStorage.setItem(USER_STORAGE_KEY, JSON.stringify(user));
                        sessionStorage.setItem(USER_STORAGE_KEY, JSON.stringify(user));
                    } catch (e) {}
                    this.updateHeaderUI(user);
                    return true;
                }
            } catch (e) {
                console.warn("Auth verification transient network error, preserving local session:", e);
                const existingUser = this.getUser();
                if (existingUser) {
                    this.updateHeaderUI(existingUser);
                }
                return true;
            }
            return false;
        },

        login: async function (username, password) {
            try {
                const res = await fetch("/api/auth/login", {
                    method: "POST",
                    headers: { "Content-Type": "application/json" },
                    body: JSON.stringify({ username: (username || "").trim(), password: (password || "").trim() })
                });
                const data = await res.json();
                if (res.ok && data && data.status === "AUTHENTICATED") {
                    this.saveSession(data);
                    return { success: true, data: data };
                } else {
                    return { success: false, error: data.error || "Invalid username or password" };
                }
            } catch (e) {
                return { success: false, error: "Network error connecting to authentication server" };
            }
        },

        logout: async function () {
            try {
                await fetch("/api/auth/logout", { method: "POST", headers: { "Content-Type": "application/json" } });
            } catch (e) {
                console.warn("Logout API notice:", e);
            }
            this.clearSession();
            this.openLoginModal("Session ended. Please log in to continue.");
        },

        /* The markup is rendered into #auth-header-widget AND into every
           [data-auth-mount]. Two mounts exist because the dashboard's rail copy
           sits in .tt-rail__group--secondary, which theme_terminal.css hides at
           <=599px; a child cannot escape an ancestor's display:none, so the
           dashboard had no login/logout on a phone at all. Rendering one string
           into both keeps them from ever disagreeing. */
        authMarkup: function (user) {
            const username = (user && user.username) ? user.username : 'admin';
            const role = (user && user.role) ? user.role : 'ADMIN';
            const fullName = (user && user.full_name) ? user.full_name : username;
            return `
                <div class="auth-user-pill" title="Logged in as ${fullName}">
                    <span>👤</span>
                    <span style="font-family:'JetBrains Mono',monospace;">${username}</span>
                    <span class="auth-user-role-tag">${role}</span>
                </div>
                <button class="auth-logout-btn" onclick="window.HM_AUTH.logout()" title="Logout from terminal">
                    <span>🚪</span> Logout
                </button>
            `;
        },

        updateHeaderUI: function (user) {
            const dropUserName = document.getElementById("dropdown-user-name");
            const dropUserRole = document.getElementById("dropdown-user-role");
            if (dropUserName) dropUserName.textContent = (user && user.username) ? user.username : "admin";
            if (dropUserRole) dropUserRole.textContent = (user && user.role) ? `${user.role}` : "ADMIN";

            const html = this.authMarkup(user);
            const targets = document.querySelectorAll("#auth-header-widget, [data-auth-mount]");
            targets.forEach(function (el) {
                el.innerHTML = html;
            });
        },

        openLoginModal: function (msg) {
            let modal = document.getElementById("universal-auth-modal");
            if (!modal) {
                modal = document.createElement("div");
                modal.id = "universal-auth-modal";
                modal.className = "universal-auth-overlay";
                modal.innerHTML = `
                    <div class="universal-auth-card">
                        <button class="auth-close-btn" onclick="window.HM_AUTH.closeLoginModal()" title="Close">✕</button>
                        <div class="auth-card-top-icon">🔒</div>
                        <div class="auth-card-title">HM Algo 2.0 TERMINAL LOGIN</div>
                        <div class="auth-card-subtitle" id="auth-modal-subtitle">Secure Multi-Market Execution Desk</div>
                        <div id="auth-modal-error" class="auth-error-alert"></div>
                        <form id="universal-auth-form" onsubmit="window.HM_AUTH.handleFormSubmit(event)">
                            <div class="auth-form-group">
                                <label class="auth-input-lbl">Username</label>
                                <div class="auth-input-wrapper">
                                    <input type="text" id="auth-input-user" class="auth-input-field" placeholder="Enter username (e.g. admin)" required autocomplete="username" value="">
                                </div>
                            </div>
                            <div class="auth-form-group">
                                <label class="auth-input-lbl">Password</label>
                                <div class="auth-input-wrapper">
                                    <input type="password" id="auth-input-pass" class="auth-input-field" placeholder="Enter password" required autocomplete="current-password" value="">
                                    <button type="button" class="auth-pwd-toggle" onclick="window.HM_AUTH.togglePasswordVisibility()">👁️</button>
                                </div>
                            </div>
                            <button type="submit" id="auth-submit-btn" class="auth-submit-btn">
                                UNLOCK TERMINAL ➔
                            </button>
                        </form>
                    </div>
                `;
                document.body.appendChild(modal);
            }

            const errEl = document.getElementById("auth-modal-error");
            if (errEl) {
                if (msg) {
                    errEl.textContent = msg;
                    errEl.style.display = "block";
                } else {
                    errEl.style.display = "none";
                }
            }

            modal.style.display = "flex";
            const userInp = document.getElementById("auth-input-user");
            if (userInp) userInp.focus();
        },

        closeLoginModal: function () {
            const modal = document.getElementById("universal-auth-modal");
            if (modal) modal.style.display = "none";
        },

        togglePasswordVisibility: function () {
            const passInp = document.getElementById("auth-input-pass");
            if (passInp) {
                passInp.type = passInp.type === "password" ? "text" : "password";
            }
        },

        handleFormSubmit: async function (e) {
            if (e) e.preventDefault();
            const userInp = document.getElementById("auth-input-user");
            const passInp = document.getElementById("auth-input-pass");
            const errEl = document.getElementById("auth-modal-error");
            const submitBtn = document.getElementById("auth-submit-btn");

            if (!userInp || !passInp) return;

            const username = userInp.value.trim();
            const password = passInp.value.trim();

            if (!username || !password) {
                if (errEl) {
                    errEl.textContent = "Please enter both username and password";
                    errEl.style.display = "block";
                }
                return;
            }

            if (submitBtn) {
                submitBtn.disabled = true;
                submitBtn.textContent = "AUTHENTICATING...";
            }

            const res = await this.login(username, password);

            if (submitBtn) {
                submitBtn.disabled = false;
                submitBtn.textContent = "UNLOCK TERMINAL ➔";
            }

            if (res.success) {
                if (errEl) errEl.style.display = "none";
                this.closeLoginModal();
                // Trigger any page-specific refresh handlers if available
                if (typeof window.refreshData === "function") window.refreshData();
                if (typeof window.fetchTelemetry === "function") window.fetchTelemetry();
            } else {
                if (errEl) {
                    errEl.textContent = res.error || "Invalid username or password";
                    errEl.style.display = "block";
                }
            }
        }
    };

    // Global token getter helper
    window.getAuthToken = function () {
        return (window.HM_AUTH && window.HM_AUTH.getToken) ? window.HM_AUTH.getToken() : (localStorage.getItem("jarvis_auth_token") || "");
    };

    // Auto-initialize on DOM ready
    document.addEventListener("DOMContentLoaded", () => {
        if (window.HM_AUTH && typeof window.HM_AUTH.verifyToken === "function") {
            window.HM_AUTH.verifyToken();
        }
    });

})();
