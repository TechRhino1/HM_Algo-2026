/**
 * HM Algo 2.0 — Universal Authentication & Session Management Module
 * Handles login modal, user self-registration, admin approvals, token persistence, and remote access gating.
 */
(function () {
    "use strict";

    const AUTH_STORAGE_KEY = "jarvis_auth_token";
    const USER_STORAGE_KEY = "jarvis_user_info";

    window.HM_AUTH = {
        _isLocked: false,
        _currentTab: "login",

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
                if (data.token) { localStorage.setItem(AUTH_STORAGE_KEY, data.token); sessionStorage.setItem(AUTH_STORAGE_KEY, data.token); }
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
            const isLocal = ["localhost", "127.0.0.1", "::1", ""].includes(window.location.hostname);
            try {
                const res = await fetch("/api/auth/verify", {
                    method: "POST",
                    headers: { "Content-Type": "application/json" }
                });
                if (res.status === 401) {
                    this.clearSession();
                    if (isLocal) {
                        const localUser = { username: "admin", role: "ADMIN", full_name: "Local Administrator" };
                        this.updateHeaderUI(localUser);
                        this.closeLoginModal(true);
                        return true;
                    }
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
                    this.closeLoginModal(true);
                    if (user.role === "ADMIN") {
                        this.checkPendingUsers();
                    }
                    return true;
                }
            } catch (e) {
                console.warn("Auth verification transient network error, preserving local session:", e);
                const existingUser = this.getUser();
                if (existingUser) {
                    this.updateHeaderUI(existingUser);
                    if (existingUser.role === "ADMIN") this.checkPendingUsers();
                }
                return true;
            }
            this.updateHeaderUI(null);
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

        register: async function (username, fullName, password) {
            try {
                const res = await fetch("/api/auth/register", {
                    method: "POST",
                    headers: { "Content-Type": "application/json" },
                    body: JSON.stringify({
                        username: (username || "").trim(),
                        full_name: (fullName || "").trim(),
                        password: (password || "").trim()
                    })
                });
                const data = await res.json();
                if (res.ok && data && data.status === "SUCCESS") {
                    return { success: true, message: data.message };
                } else {
                    return { success: false, error: data.error || data.message || "Registration failed" };
                }
            } catch (e) {
                return { success: false, error: "Network error connecting to registration server" };
            }
        },

        logout: async function () {
            try {
                await fetch("/api/auth/logout", { method: "POST", headers: { "Content-Type": "application/json" } });
            } catch (e) {
                console.warn("Logout API notice:", e);
            }
            this.clearSession();
            this.openLoginModal("Session ended. Please log in to continue.", true);
        },

        authMarkup: function (user) {
            const isLocal = ["localhost", "127.0.0.1", "::1", ""].includes(window.location.hostname);
            if (!user) {
                if (isLocal) {
                    return `
                        <div class="auth-user-pill" title="Local System Active">
                            <span>👤</span>
                            <span style="font-family:'JetBrains Mono',monospace;">admin</span>
                            <span class="auth-user-role-tag">ADMIN</span>
                        </div>
                    `;
                }
                return `
                    <div class="auth-user-pill" title="Read-Only Telemetry Active" style="border-color: rgba(255,170,0,0.4);">
                        <span>🔒</span>
                        <span style="font-family:'JetBrains Mono',monospace; color: var(--hm-amber, #fa0);">AUTHENTICATION REQUIRED</span>
                    </div>
                    <button class="auth-login-btn" onclick="window.HM_AUTH.openLoginModal()" title="Sign in or Register">
                        <span>🔑</span> Sign In / Register
                    </button>
                `;
            }
            const username = user.username || 'admin';
            const role = user.role || 'ADMIN';
            const fullName = user.full_name || username;
            
            let adminBtn = "";
            if (role === "ADMIN") {
                adminBtn = `
                    <button class="auth-admin-btn" onclick="window.HM_AUTH.openAdminModal()" title="Manage User Registrations & Approvals">
                        <span>👥</span> Users <span id="admin-pending-badge" class="auth-pending-badge" style="display:none;">0</span>
                    </button>
                `;
            }

            return `
                ${adminBtn}
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
            if (dropUserName) dropUserName.textContent = (user && user.username) ? user.username : "Guest";
            if (dropUserRole) dropUserRole.textContent = (user && user.role) ? `${user.role}` : "LOCKED";

            const html = this.authMarkup(user);
            const targets = document.querySelectorAll("#auth-header-widget, [data-auth-mount]");
            targets.forEach(function (el) {
                el.innerHTML = html;
            });
        },

        switchTab: function (tab) {
            this._currentTab = tab;
            const loginTab = document.getElementById("tab-btn-login");
            const regTab = document.getElementById("tab-btn-register");
            const loginPanel = document.getElementById("auth-panel-login");
            const regPanel = document.getElementById("auth-panel-register");
            const errEl = document.getElementById("auth-modal-error");
            const succEl = document.getElementById("auth-modal-success");

            if (errEl) errEl.style.display = "none";
            if (succEl) succEl.style.display = "none";

            if (tab === "register") {
                if (loginTab) loginTab.classList.remove("active");
                if (regTab) regTab.classList.add("active");
                if (loginPanel) loginPanel.style.display = "none";
                if (regPanel) regPanel.style.display = "block";
                const regUser = document.getElementById("reg-input-user");
                if (regUser) regUser.focus();
            } else {
                if (regTab) regTab.classList.remove("active");
                if (loginTab) loginTab.classList.add("active");
                if (regPanel) regPanel.style.display = "none";
                if (loginPanel) loginPanel.style.display = "block";
                const logUser = document.getElementById("auth-input-user");
                if (logUser) logUser.focus();
            }
        },

        openLoginModal: function (msg, isMandatory = false) {
            this._isLocked = isMandatory;
            let modal = document.getElementById("universal-auth-modal");
            if (!modal) {
                modal = document.createElement("div");
                modal.id = "universal-auth-modal";
                modal.className = "universal-auth-overlay";
                modal.innerHTML = `
                    <div class="universal-auth-card">
                        <button class="auth-close-btn" id="auth-modal-close-btn" onclick="window.HM_AUTH.closeLoginModal()" title="Close">✕</button>
                        <div class="auth-card-top-icon">🔒</div>
                        <div class="auth-card-title">HM Algo 2.0 TERMINAL</div>
                        <div class="auth-card-subtitle" id="auth-modal-subtitle">Institutional Execution Desk</div>
                        
                        <div class="auth-modal-tabs">
                            <button type="button" id="tab-btn-login" class="auth-modal-tab active" onclick="window.HM_AUTH.switchTab('login')">SIGN IN</button>
                            <button type="button" id="tab-btn-register" class="auth-modal-tab" onclick="window.HM_AUTH.switchTab('register')">REGISTER</button>
                        </div>

                        <div id="auth-modal-error" class="auth-error-alert"></div>
                        <div id="auth-modal-success" class="auth-success-alert"></div>

                        <!-- Login Panel -->
                        <div id="auth-panel-login">
                            <form id="universal-auth-form" onsubmit="window.HM_AUTH.handleFormSubmit(event)">
                                <div class="auth-form-group">
                                    <label class="auth-input-lbl">Username</label>
                                    <div class="auth-input-wrapper">
                                        <input type="text" id="auth-input-user" class="auth-input-field" placeholder="Username (e.g. admin)" required autocomplete="username">
                                    </div>
                                </div>
                                <div class="auth-form-group">
                                    <label class="auth-input-lbl">Password</label>
                                    <div class="auth-input-wrapper">
                                        <input type="password" id="auth-input-pass" class="auth-input-field" placeholder="Enter password" required autocomplete="current-password">
                                        <button type="button" class="auth-pwd-toggle" onclick="window.HM_AUTH.togglePasswordVisibility('auth-input-pass')">👁️</button>
                                    </div>
                                </div>
                                <button type="submit" id="auth-submit-btn" class="auth-submit-btn">
                                    UNLOCK TERMINAL ➔
                                </button>
                            </form>
                        </div>

                        <!-- Register Panel -->
                        <div id="auth-panel-register" style="display:none;">
                            <form id="universal-reg-form" onsubmit="window.HM_AUTH.handleRegisterSubmit(event)">
                                <div class="auth-form-group">
                                    <label class="auth-input-lbl">Desired Username</label>
                                    <div class="auth-input-wrapper">
                                        <input type="text" id="reg-input-user" class="auth-input-field" placeholder="3-30 letters or numbers" required autocomplete="username">
                                    </div>
                                </div>
                                <div class="auth-form-group">
                                    <label class="auth-input-lbl">Full Name</label>
                                    <div class="auth-input-wrapper">
                                        <input type="text" id="reg-input-name" class="auth-input-field" placeholder="e.g. John Doe" required>
                                    </div>
                                </div>
                                <div class="auth-form-group">
                                    <label class="auth-input-lbl">Password (min 8 chars)</label>
                                    <div class="auth-input-wrapper">
                                        <input type="password" id="reg-input-pass" class="auth-input-field" placeholder="Create secure password" required autocomplete="new-password">
                                        <button type="button" class="auth-pwd-toggle" onclick="window.HM_AUTH.togglePasswordVisibility('reg-input-pass')">👁️</button>
                                    </div>
                                </div>
                                <div class="auth-form-group">
                                    <label class="auth-input-lbl">Confirm Password</label>
                                    <div class="auth-input-wrapper">
                                        <input type="password" id="reg-input-confirm" class="auth-input-field" placeholder="Re-type password" required autocomplete="new-password">
                                        <button type="button" class="auth-pwd-toggle" onclick="window.HM_AUTH.togglePasswordVisibility('reg-input-confirm')">👁️</button>
                                    </div>
                                </div>
                                <button type="submit" id="reg-submit-btn" class="auth-submit-btn" style="background: linear-gradient(135deg, #00d4ff, #0088cc);">
                                    REQUEST ACCESS ➔
                                </button>
                            </form>
                        </div>
                    </div>
                `;
                document.body.appendChild(modal);
            }

            const closeBtn = document.getElementById("auth-modal-close-btn");
            if (closeBtn) {
                closeBtn.style.display = isMandatory ? "none" : "block";
            }
            if (isMandatory) {
                modal.classList.add("universal-auth-overlay--locked");
            } else {
                modal.classList.remove("universal-auth-overlay--locked");
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

            const succEl = document.getElementById("auth-modal-success");
            if (succEl) succEl.style.display = "none";

            modal.style.display = "flex";
            this.switchTab("login");
        },

        closeLoginModal: function (force = false) {
            if (this._isLocked && !force) {
                return; // Cannot dismiss mandatory modal without valid auth
            }
            const modal = document.getElementById("universal-auth-modal");
            if (modal) modal.style.display = "none";
            this._isLocked = false;
        },

        togglePasswordVisibility: function (fieldId) {
            const passInp = document.getElementById(fieldId || "auth-input-pass");
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
                this.closeLoginModal(true);
                if (typeof window.refreshData === "function") window.refreshData();
                if (typeof window.fetchTelemetry === "function") window.fetchTelemetry();
                if (typeof window.location !== "undefined") {
                    window.location.reload();
                }
            } else {
                if (errEl) {
                    errEl.textContent = res.error || "Invalid username or password";
                    errEl.style.display = "block";
                }
            }
        },

        handleRegisterSubmit: async function (e) {
            if (e) e.preventDefault();
            const userInp = document.getElementById("reg-input-user");
            const nameInp = document.getElementById("reg-input-name");
            const passInp = document.getElementById("reg-input-pass");
            const confirmInp = document.getElementById("reg-input-confirm");
            const errEl = document.getElementById("auth-modal-error");
            const succEl = document.getElementById("auth-modal-success");
            const submitBtn = document.getElementById("reg-submit-btn");

            if (errEl) errEl.style.display = "none";
            if (succEl) succEl.style.display = "none";

            const username = userInp ? userInp.value.trim() : "";
            const fullName = nameInp ? nameInp.value.trim() : "";
            const password = passInp ? passInp.value.trim() : "";
            const confirm = confirmInp ? confirmInp.value.trim() : "";

            if (!username || !password) {
                if (errEl) {
                    errEl.textContent = "Please fill in all required fields";
                    errEl.style.display = "block";
                }
                return;
            }

            if (password.length < 8) {
                if (errEl) {
                    errEl.textContent = "Password must be at least 8 characters long";
                    errEl.style.display = "block";
                }
                return;
            }

            if (password !== confirm) {
                if (errEl) {
                    errEl.textContent = "Passwords do not match";
                    errEl.style.display = "block";
                }
                return;
            }

            if (submitBtn) {
                submitBtn.disabled = true;
                submitBtn.textContent = "SUBMITTING...";
            }

            const res = await this.register(username, fullName, password);

            if (submitBtn) {
                submitBtn.disabled = false;
                submitBtn.textContent = "REQUEST ACCESS ➔";
            }

            if (res.success) {
                if (succEl) {
                    succEl.innerHTML = `✓ ${res.message}<br><small style="color:#94a3b8">An administrator will review your request. You can sign in once approved.</small>`;
                    succEl.style.display = "block";
                }
                if (userInp) userInp.value = "";
                if (nameInp) nameInp.value = "";
                if (passInp) passInp.value = "";
                if (confirmInp) confirmInp.value = "";
            } else {
                if (errEl) {
                    errEl.textContent = res.error || "Registration failed";
                    errEl.style.display = "block";
                }
            }
        },

        /* ==========================================================================
           Admin User Management Desk
           ========================================================================== */
        checkPendingUsers: async function () {
            try {
                const res = await fetch("/api/admin/users");
                if (res.ok) {
                    const data = await res.json();
                    if (data && data.users) {
                        const pendingCount = data.users.filter(u => u.status === "PENDING_APPROVAL").length;
                        const badge = document.getElementById("admin-pending-badge");
                        if (badge) {
                            if (pendingCount > 0) {
                                badge.textContent = `${pendingCount} PENDING`;
                                badge.style.display = "inline-block";
                            } else {
                                badge.style.display = "none";
                            }
                        }
                    }
                }
            } catch (e) {
                console.warn("Could not check pending user count:", e);
            }
        },

        openAdminModal: async function () {
            let modal = document.getElementById("universal-admin-modal");
            if (!modal) {
                modal = document.createElement("div");
                modal.id = "universal-admin-modal";
                modal.className = "universal-auth-overlay";
                modal.innerHTML = `
                    <div class="admin-users-card">
                        <div class="admin-users-header">
                            <div class="admin-users-title">
                                <span>👥</span> HM Algo 2.0 User Administration
                            </div>
                            <button class="auth-close-btn" onclick="window.HM_AUTH.closeAdminModal()" style="position:static;">✕</button>
                        </div>
                        <div id="admin-modal-alert" class="auth-success-alert" style="display:none;"></div>
                        <div class="admin-users-table-container">
                            <table class="admin-users-table">
                                <thead>
                                    <tr>
                                        <th>Username</th>
                                        <th>Full Name</th>
                                        <th>Role</th>
                                        <th>Status</th>
                                        <th>Created</th>
                                        <th>Actions</th>
                                    </tr>
                                </thead>
                                <tbody id="admin-users-tbody">
                                    <tr><td colspan="6" style="text-align:center; padding:20px; color:#94a3b8;">Loading user roster...</td></tr>
                                </tbody>
                            </table>
                        </div>
                    </div>
                `;
                document.body.appendChild(modal);
            }

            modal.style.display = "flex";
            await this.loadAdminUsers();
        },

        closeAdminModal: function () {
            const modal = document.getElementById("universal-admin-modal");
            if (modal) modal.style.display = "none";
        },

        loadAdminUsers: async function () {
            const tbody = document.getElementById("admin-users-tbody");
            if (!tbody) return;

            try {
                const res = await fetch("/api/admin/users");
                if (!res.ok) {
                    tbody.innerHTML = `<tr><td colspan="6" style="text-align:center; color:#ef4444; padding:20px;">Failed to load users: ${res.statusText}</td></tr>`;
                    return;
                }
                const data = await res.json();
                const users = data.users || [];
                if (users.length === 0) {
                    tbody.innerHTML = `<tr><td colspan="6" style="text-align:center; padding:20px; color:#94a3b8;">No registered users found.</td></tr>`;
                    return;
                }

                tbody.innerHTML = users.map(u => {
                    const statusClass = u.status === "ACTIVE" ? "badge-status-active" : (u.status === "PENDING_APPROVAL" ? "badge-status-pending" : "badge-status-rejected");
                    const dateStr = u.created_at ? new Date(u.created_at * 1000).toLocaleDateString() : "—";
                    
                    let actionsHtml = "";
                    if (u.username === "admin") {
                        actionsHtml = `<span style="color:#64748b; font-style:italic;">Primary Admin</span>`;
                    } else if (u.status === "PENDING_APPROVAL") {
                        actionsHtml = `
                            <button class="btn-action-sm btn-approve-trader" onclick="window.HM_AUTH.approveUser('${u.username}', 'TRADER')">✓ Trader</button>
                            <button class="btn-action-sm btn-approve-viewer" onclick="window.HM_AUTH.approveUser('${u.username}', 'VIEWER')">✓ Viewer</button>
                            <button class="btn-action-sm btn-reject" onclick="window.HM_AUTH.rejectUser('${u.username}')">✕ Reject</button>
                        `;
                    } else if (u.status === "ACTIVE") {
                        actionsHtml = `
                            <button class="btn-action-sm btn-reject" onclick="window.HM_AUTH.rejectUser('${u.username}')">Deactivate</button>
                            <button class="btn-action-sm btn-delete" onclick="window.HM_AUTH.deleteUser('${u.username}')">Delete</button>
                        `;
                    } else {
                        actionsHtml = `
                            <button class="btn-action-sm btn-approve-trader" onclick="window.HM_AUTH.approveUser('${u.username}', 'TRADER')">Re-Approve</button>
                            <button class="btn-action-sm btn-delete" onclick="window.HM_AUTH.deleteUser('${u.username}')">Delete</button>
                        `;
                    }

                    return `
                        <tr>
                            <td><strong style="color:#ffffff; font-family:'JetBrains Mono',monospace;">${u.username}</strong></td>
                            <td>${u.full_name || '—'}</td>
                            <td><span style="font-family:'JetBrains Mono',monospace; font-size:10px;">${u.role}</span></td>
                            <td><span class="badge-status ${statusClass}">${u.status}</span></td>
                            <td><small style="color:#94a3b8;">${dateStr}</small></td>
                            <td>${actionsHtml}</td>
                        </tr>
                    `;
                }).join("");

                this.checkPendingUsers();
            } catch (e) {
                tbody.innerHTML = `<tr><td colspan="6" style="text-align:center; color:#ef4444; padding:20px;">Network error loading users</td></tr>`;
            }
        },

        approveUser: async function (username, role) {
            try {
                const res = await fetch("/api/admin/approve_user", {
                    method: "POST",
                    headers: { "Content-Type": "application/json" },
                    body: JSON.stringify({ username: username, role: role })
                });
                const data = await res.json();
                this.showAdminAlert(data.message || `User '${username}' approved!`);
                await this.loadAdminUsers();
            } catch (e) {
                alert("Failed to approve user: " + e);
            }
        },

        rejectUser: async function (username) {
            if (!confirm(`Are you sure you want to decline/reject user '${username}'?`)) return;
            try {
                const res = await fetch("/api/admin/reject_user", {
                    method: "POST",
                    headers: { "Content-Type": "application/json" },
                    body: JSON.stringify({ username: username })
                });
                const data = await res.json();
                this.showAdminAlert(data.message || `User '${username}' rejected.`);
                await this.loadAdminUsers();
            } catch (e) {
                alert("Failed to reject user: " + e);
            }
        },

        deleteUser: async function (username) {
            if (!confirm(`Permanently delete account for '${username}'? This cannot be undone.`)) return;
            try {
                const res = await fetch("/api/admin/delete_user", {
                    method: "POST",
                    headers: { "Content-Type": "application/json" },
                    body: JSON.stringify({ username: username })
                });
                const data = await res.json();
                this.showAdminAlert(data.message || `User '${username}' deleted.`);
                await this.loadAdminUsers();
            } catch (e) {
                alert("Failed to delete user: " + e);
            }
        },

        showAdminAlert: function (msg) {
            const el = document.getElementById("admin-modal-alert");
            if (el) {
                el.textContent = msg;
                el.style.display = "block";
                setTimeout(() => { el.style.display = "none"; }, 3500);
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
