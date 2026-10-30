"""
HM Algo 2.0 — Remote Access Authentication & Session Management Engine.
Provides secure password verification with salted hashing, HMAC-SHA256 session token generation,
token validation, server-side token revocation (logout), role-based access control, and user profile management.
"""
import os
import re
import time
import hmac
import hashlib
import secrets
import logging
import sqlite3
from jarvis.config.paths import resolve_db_path, ensure_data_dir
try:
    import bcrypt
    _HAVE_BCRYPT = True
except Exception:  # pragma: no cover
    _HAVE_BCRYPT = False
from typing import Dict, Any, Optional, Tuple, List, ClassVar

logger = logging.getLogger("JARVIS_RemoteAuth")

def _get_user_db():
    """Returns a WAL-mode SQLite connection to the persistent user database."""
    ensure_data_dir()
    db_path = resolve_db_path("jarvis_users.db")
    conn = sqlite3.connect(db_path, timeout=10.0)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL;")
    return conn

# Persistent Secret key for HMAC token signing (auto-generated & stored on disk or loaded from env)
def _get_or_create_secret_key() -> str:
    env_key = os.environ.get("JARVIS_SECRET_KEY")
    if env_key and len(env_key.strip()) >= 16:
        return env_key.strip()

    base_dir = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    key_file = os.path.join(base_dir, ".jarvis_secret_key")
    if os.path.exists(key_file):
        try:
            with open(key_file, "r", encoding="utf-8") as f:
                k = f.read().strip()
                if len(k) >= 32:
                    return k
        except Exception as e:
            logger.warning(f"Could not read persistent secret key file: {e}")

    new_key = secrets.token_hex(32)
    try:
        with open(key_file, "w", encoding="utf-8") as f:
            f.write(new_key)
    except Exception as e:
        logger.warning(f"Could not write persistent secret key file: {e}")
    return new_key

SECRET_KEY = _get_or_create_secret_key()

# Default Remote Access Admin Credentials (Override via environment variables)
ADMIN_USERNAME = os.environ.get("JARVIS_ADMIN_USER", "admin")

def _resolve_admin_password() -> str:
    """Resolve the admin password from env, else check .jarvis_admin_pass in root/package dir, else generate + persist."""
    env_pass = os.environ.get("JARVIS_ADMIN_PASS")
    if env_pass and len(env_pass.strip()) >= 8:
        return env_pass.strip()
    base_dir = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    root_dir = os.path.dirname(base_dir)
    candidate_paths = [
        os.path.join(root_dir, ".jarvis_admin_pass"),
        os.path.join(base_dir, ".jarvis_admin_pass"),
        os.path.join(os.getcwd(), ".jarvis_admin_pass")
    ]
    for pass_file in candidate_paths:
        try:
            if os.path.exists(pass_file):
                with open(pass_file, "r", encoding="utf-8") as f:
                    existing = f.read().strip()
                    if existing:
                        return existing
        except Exception as e:
            logger.warning(f"Could not read admin password file {pass_file}: {e}")
    new_pass = secrets.token_urlsafe(18)
    pass_file = os.path.join(base_dir, ".jarvis_admin_pass")
    try:
        with open(pass_file, "w", encoding="utf-8") as f:
            f.write(new_pass)
    except Exception as e:
        logger.warning(f"Could not persist generated admin password: {e}")
    return new_pass

DEFAULT_PASS_RAW = _resolve_admin_password()
# Log WHERE the password came from, never the password. It used to be written
# here in clear text, which put the remote-access credential into every log
# file and any log shipped off the box.
logger.info(
    "RemoteAuthEngine initialized with ADMIN_USER='%s' (password resolved from %s)",
    ADMIN_USERNAME,
    "JARVIS_ADMIN_PASS" if os.environ.get("JARVIS_ADMIN_PASS") else "jarvis/.jarvis_admin_pass",
)


class _RevokedTokens(dict):
    """Revoked session tokens, each carrying the moment its revocation may lapse.

    Dict-shaped (``{token: exp}``) so expired revocations can be swept, but it
    also answers the ``add`` / ``discard`` calls the set it replaced did — both
    the call sites and the existing coverage use that interface. A revocation
    only needs to outlive its own session: once the token could no longer
    validate anyway, remembering it forever is unbounded growth, not safety.
    """

    def add(self, token: str) -> None:
        self[token] = time.time() + RemoteAuthEngine._token_ttl

    def discard(self, token: str) -> None:
        self.pop(token, None)


class RemoteAuthEngine:
    """
    Secure Authentication and Session Engine for HM Algo 2.0 Remote Web Terminals.
    Includes rate limiting, temporary lockout against brute-force attacks, and persistent HMAC signing.
    """
    _tokens: ClassVar[Dict[str, float]] = {}       # token -> expiration timestamp
    # token -> the moment the revocation itself may be forgotten (dict-shaped;
    # see _RevokedTokens — it still answers the old set interface).
    _revoked_tokens: ClassVar[Dict[str, float]] = _RevokedTokens()
    _token_ttl: float = 8 * 3600.0       # Short-lived browser session

    # Failed login attempts tracker for brute force protection
    _failed_attempts: ClassVar[Dict[str, List[float]]] = {}
    _lockout_duration_sec: float = 60.0
    _max_failed_attempts: int = 5

    # In-memory user database with salted hashes and roles
    _users: ClassVar[Dict[str, Dict[str, Any]]] = {}

    @classmethod
    def is_local_client(cls, client_ip: str) -> bool:
        """Checks whether the client IP represents a local/private network address.

        An empty or missing address is NOT local. It used to be treated as
        local, and because ``check_rate_limit`` defaults ``client_ip`` to ``""``
        and both of its call sites pass a single argument, every caller was
        classified as local — so the 5-attempt remote lockout was unreachable
        and everyone got the relaxed 50. Unknown must fail closed; the stricter
        limit is the safe default.
        """
        ip = (client_ip or "").strip().lower()
        if not ip:
            return False
        return ip in ("127.0.0.1", "::1", "localhost") or ip.startswith("192.168.") or ip.startswith("10.") or ip.startswith("172.16.")

    @classmethod
    def check_rate_limit(cls, identifier: str, client_ip: str = "") -> Tuple[bool, int]:
        """
        Checks if identifier (IP or username) is currently locked out.
        Returns (is_allowed, seconds_remaining).
        """
        now = time.time()
        key = (identifier or "client").strip().lower()
        attempts = cls._failed_attempts.get(key, [])
        # Retain attempts within last 300 seconds (5 mins)
        attempts = [t for t in attempts if (now - t) < 300.0]
        cls._failed_attempts[key] = attempts

        is_local = cls.is_local_client(key) or cls.is_local_client(client_ip)
        max_allowed = 50 if is_local else cls._max_failed_attempts
        if len(attempts) >= max_allowed:
            last_attempt = max(attempts)
            elapsed = now - last_attempt
            if elapsed < cls._lockout_duration_sec:
                remaining = max(1, int(cls._lockout_duration_sec - elapsed))
                return False, remaining

        return True, 0

    @classmethod
    def record_failed_attempt(cls, identifier: str):
        now = time.time()
        key = (identifier or "global").strip().lower()
        if key not in cls._failed_attempts:
            cls._failed_attempts[key] = []
        cls._failed_attempts[key].append(now)

    @classmethod
    def record_successful_login(cls, identifier: str):
        key = (identifier or "global").strip().lower()
        cls._failed_attempts.pop(key, None)

    @classmethod
    def _init_default_users(cls):
        """Initializes default institutional users with salted hashes and synchronizes with persistent SQLite storage."""
        def _make_user_record(username: str, password_raw: str, role: str, full_name: str, status: str = "ACTIVE") -> Dict[str, Any]:
            salt = secrets.token_hex(16)
            pwd_hash = cls._hash_password(password_raw)
            return {
                "username": username.lower(),
                "role": role,
                "full_name": full_name,
                "salt": salt,
                "password_hash": pwd_hash,
                "status": status,
                "created_at": time.time(),
                "approved_by": "SYSTEM",
                "approved_at": time.time()
            }

        # 1. Primary Admin Account - dynamically sync with current resolved password
        current_admin_pass = _resolve_admin_password()
        admin_user = ADMIN_USERNAME.strip().lower()
        if admin_user not in cls._users or not cls._verify_password(current_admin_pass, cls._users[admin_user].get("password_hash", "")):
            cls._users[admin_user] = _make_user_record(admin_user, current_admin_pass, "ADMIN", "System Administrator", "ACTIVE")

        # Additional accounts are opt-in. Never create predictable credentials.
        for username, password, role, full_name in (
            (os.environ.get("JARVIS_TRADER_USER", "trader"), os.environ.get("JARVIS_TRADER_PASS"), "TRADER", "Senior Algo Trader"),
            (os.environ.get("JARVIS_DEMO_USER", "demo"), os.environ.get("JARVIS_DEMO_PASS"), "VIEWER", "Demo Guest Account"),
        ):
            if password and len(password.strip()) >= 12 and username.strip().lower() not in cls._users:
                cls._users[username.strip().lower()] = _make_user_record(username, password.strip(), role, full_name, "ACTIVE")

        # 2. Synchronize persistent SQLite user database
        try:
            conn = _get_user_db()
            try:
                with conn:
                    conn.execute("""
                        CREATE TABLE IF NOT EXISTS users (
                            username TEXT PRIMARY KEY,
                            password_hash TEXT NOT NULL,
                            full_name TEXT NOT NULL,
                            role TEXT NOT NULL DEFAULT 'TRADER',
                            status TEXT NOT NULL DEFAULT 'ACTIVE',
                            created_at REAL NOT NULL,
                            approved_by TEXT,
                            approved_at REAL
                        );
                    """)
                    # Ensure admin exists in DB
                    admin_rec = cls._users.get(admin_user)
                    if admin_rec:
                        row = conn.execute("SELECT password_hash FROM users WHERE username = ?", (admin_user,)).fetchone()
                        if not row:
                            conn.execute("""
                                INSERT INTO users (username, password_hash, full_name, role, status, created_at, approved_by, approved_at)
                                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                            """, (admin_user, admin_rec["password_hash"], admin_rec["full_name"], "ADMIN", "ACTIVE", admin_rec["created_at"], "SYSTEM", time.time()))
                        elif not cls._verify_password(current_admin_pass, row["password_hash"]):
                            conn.execute("UPDATE users SET password_hash = ?, status = 'ACTIVE' WHERE username = ?", (admin_rec["password_hash"], admin_user))

                    # Load registered users from DB into in-memory store
                    for r in conn.execute("SELECT * FROM users").fetchall():
                        u_name = str(r["username"]).lower()
                        if u_name not in cls._users:
                            cls._users[u_name] = {
                                "username": u_name,
                                "role": r["role"],
                                "full_name": r["full_name"],
                                "password_hash": r["password_hash"],
                                "status": r["status"] if "status" in r.keys() else "ACTIVE",
                                "created_at": float(r["created_at"] or 0.0),
                                "approved_by": r["approved_by"] if "approved_by" in r.keys() else None,
                                "approved_at": float(r["approved_at"] or 0.0) if ("approved_at" in r.keys() and r["approved_at"]) else None,
                            }
                        else:
                            if "status" not in cls._users[u_name]:
                                cls._users[u_name]["status"] = r["status"] if "status" in r.keys() else "ACTIVE"
            finally:
                conn.close()
        except Exception as e:
            logger.warning(f"Could not initialize or sync persistent user DB: {e}")

    @classmethod
    def _hash_password(cls, password: str, salt: str = None) -> str:
        """Returns bcrypt hash if available, else standard library PBKDF2-HMAC-SHA256 hash."""
        pw = password.strip().encode("utf-8")[:72]
        if _HAVE_BCRYPT:
            try:
                return bcrypt.hashpw(pw, bcrypt.gensalt()).decode("utf-8")
            except Exception:
                pass
        salt_bytes = (salt or secrets.token_hex(16)).encode("utf-8")
        derived = hashlib.pbkdf2_hmac("sha256", pw, salt_bytes, 100_000)
        return f"pbkdf2:sha256:100000:{salt_bytes.hex()}:{derived.hex()}"

    @classmethod
    def _verify_password(cls, password: str, stored_hash: str) -> bool:
        """Constant-time verification supporting both bcrypt and PBKDF2 hashes."""
        try:
            pw = password.strip().encode("utf-8")[:72]
            if stored_hash.startswith("$2"):
                if _HAVE_BCRYPT:
                    return bcrypt.checkpw(pw, stored_hash.encode("utf-8"))
                return False
            elif stored_hash.startswith("pbkdf2:sha256:"):
                parts = stored_hash.split(":")
                if len(parts) == 5:
                    rounds = int(parts[2])
                    salt_bytes = bytes.fromhex(parts[3])
                    target_hash = bytes.fromhex(parts[4])
                    derived = hashlib.pbkdf2_hmac("sha256", pw, salt_bytes, rounds)
                    return hmac.compare_digest(derived, target_hash)
            elif len(stored_hash) == 64:
                # Legacy raw SHA-256 fallback
                computed = hashlib.sha256(pw).hexdigest()
                return hmac.compare_digest(computed, stored_hash)
        except Exception:
            return False
        return False

    @classmethod
    def verify_credentials(cls, username: str, password_raw: str, client_ip: str = "") -> Tuple[Optional[Dict[str, Any]], str]:
        """
        Strictly verifies provided username and password with rate limiting and account approval checks.
        Supports configured password, standard administrative aliases, and role-based accounts.
        Valid credentials automatically clear temporary rate-limit lockouts.
        Returns (user_dict, error_msg).
        """
        cls._init_default_users()
        user_key = (username or "").strip().lower()
        pwd = (password_raw or "").strip()

        if not user_key or not pwd:
            return None, "Username and password required"

        user_data = cls._users.get(user_key)
        if not user_data:
            # Check DB if not present in memory
            try:
                conn = _get_user_db()
                try:
                    r = conn.execute("SELECT * FROM users WHERE username = ?", (user_key,)).fetchone()
                    if r:
                        user_data = {
                            "username": user_key,
                            "role": r["role"],
                            "full_name": r["full_name"],
                            "password_hash": r["password_hash"],
                            "status": r["status"] if "status" in r.keys() else "ACTIVE",
                            "created_at": float(r["created_at"] or 0.0),
                            "approved_by": r["approved_by"] if "approved_by" in r.keys() else None,
                            "approved_at": float(r["approved_at"] or 0.0) if ("approved_at" in r.keys() and r["approved_at"]) else None,
                        }
                        cls._users[user_key] = user_data
                finally:
                    conn.close()
            except Exception:
                pass

        if not user_data:
            cls.record_failed_attempt(client_ip or "")
            cls.record_failed_attempt(user_key)
            return None, "Invalid username or password"

        # Check approval status
        status = user_data.get("status", "ACTIVE")
        if status == "PENDING_APPROVAL":
            return None, "Account registration is pending administrator approval."
        if status == "REJECTED":
            return None, "Account registration has been declined."
        if status != "ACTIVE":
            return None, "Account is inactive."

        # Verify password validity first so authentic users with correct password can always log in.
        is_valid = cls._verify_password(pwd, user_data.get("password_hash", ""))

        if is_valid:
            cls.record_successful_login(client_ip or "")
            cls.record_successful_login(user_key)
            return {
                "username": user_key,
                "role": user_data["role"],
                "full_name": user_data.get("full_name", user_key.title()),
                "status": status
            }, ""

        # Enforce rate limiting only on failed password attempts
        allowed_ip, remaining_ip = cls.check_rate_limit(client_ip or "")
        if not allowed_ip:
            return None, f"Too many failed login attempts. Account temporarily locked for {remaining_ip}s."

        allowed_user, remaining_user = cls.check_rate_limit(user_key)
        if not allowed_user:
            return None, f"Too many failed login attempts for user '{user_key}'. Locked for {remaining_user}s."

        cls.record_failed_attempt(client_ip or "")
        cls.record_failed_attempt(user_key)
        return None, "Invalid username or password"

    @classmethod
    def create_session_token(cls, username: str) -> Dict[str, Any]:
        """
        Generates a tamper-proof HMAC-SHA256 signed session token for authenticated user.
        Valid for ``_token_ttl`` (8 hours) and extended on every successful validation.
        """
        cls._init_default_users()
        user_key = (username or "").strip().lower()
        user_data = cls._users.get(user_key)
        if not user_data:
            raise ValueError("Cannot create a session for an unknown user")

        timestamp = str(time.time())
        nonce = secrets.token_hex(16)
        payload = f"{user_key}:{timestamp}:{nonce}"
        signature = hmac.new(SECRET_KEY.encode("utf-8"), payload.encode("utf-8"), hashlib.sha256).hexdigest()
        token = f"{payload}:{signature}"

        expires_at = time.time() + cls._token_ttl
        cls._tokens[token] = expires_at

        # Un-revoke token if previously in revoked set
        cls._revoked_tokens.discard(token)

        return {
            "token": token,
            "username": user_key,
            "role": user_data["role"],
            "full_name": user_data.get("full_name", user_key.title()),
            "expires_at": expires_at,
            "status": "AUTHENTICATED"
        }

    @classmethod
    def validate_token(cls, token: Optional[str]) -> Optional[Dict[str, Any]]:
        """
        Validates token signature, expiration, and ensures token is not revoked.
        Returns user session details if valid, extending rolling expiration.
        """
        if not token:
            return None

        cls._init_default_users()

        # Clean token prefix if passed as 'Bearer <token>'
        if token.startswith("Bearer "):
            token = token[7:].strip()

        now = time.time()

        # Sweep expired revocations BEFORE testing membership. Testing first meant
        # a token whose revocation window had already closed was still refused —
        # it only became usable again once some *other* call happened to trigger
        # the sweep. A revocation is only meant to outlast the token, not to
        # outlive its own expiry.
        revoked = cls._revoked_tokens
        if isinstance(revoked, dict):
            for stale in [t for t, exp in revoked.items() if exp <= now]:
                del revoked[stale]

        if token in cls._revoked_tokens:
            return None

        # Clean up expired tokens
        cls._tokens = {t: exp for t, exp in cls._tokens.items() if exp > now}
        # Sweep bookkeeping whose window has closed. A revoked token only needs to
        # be remembered until it could no longer validate anyway, and a failed-login
        # stamp only matters for an hour — neither is load-bearing forever, and
        # both used to grow without bound.
        for key in list(cls._failed_attempts):
            recent = [t for t in cls._failed_attempts[key] if (now - t) < 3600.0]
            if recent:
                cls._failed_attempts[key] = recent
            else:
                del cls._failed_attempts[key]
        # A valid signature alone is not a session.  Requiring the server-side
        # allow-list makes logout effective and invalidates sessions on restart.
        if token not in cls._tokens:
            return None

        try:
            parts = token.split(":")
            if len(parts) == 4:
                username, ts_str, nonce, signature = parts
                payload = f"{username}:{ts_str}:{nonce}"
                expected_sig = hmac.new(SECRET_KEY.encode("utf-8"), payload.encode("utf-8"), hashlib.sha256).hexdigest()
                if hmac.compare_digest(signature, expected_sig):
                    ts = float(ts_str)
                    if (now - ts) < cls._token_ttl:
                        cls._tokens[token] = now + cls._token_ttl
                        user_data = cls._users.get(username.lower())
                        if not user_data or user_data.get("status", "ACTIVE") != "ACTIVE":
                            return None
                        return {
                            "valid": True,
                            "username": username.lower(),
                            "role": user_data["role"],
                            "full_name": user_data.get("full_name", username.title()),
                            "expires_at": now + cls._token_ttl,
                            "token": token
                        }
        except Exception:
            pass

        return None

    @classmethod
    def revoke_token(cls, token: Optional[str]) -> bool:
        """
        Revokes the session token on logout, immediately invalidating access.
        """
        if not token:
            return False

        if token.startswith("Bearer "):
            token = token[7:].strip()

        cls._revoked_tokens.add(token)
        cls._tokens.pop(token, None)
        return True

    @classmethod
    def change_password(cls, username: str, old_password: str, new_password: str) -> Tuple[bool, str]:
        """
        Allows an authenticated user to update their password.
        """
        cls._init_default_users()
        user_key = (username or "").strip().lower()
        # verify_credentials returns a (user, error) TUPLE, and a failed call
        # returns (None, "Invalid...") — a non-empty tuple, therefore truthy.
        # `if not cls.verify_credentials(...)` was always False, so the current
        # password was never actually checked: any authenticated session could
        # overwrite the password and lock out (or take over) the account. Test
        # the first element, which is None exactly when the login failed.
        user, _err = cls.verify_credentials(user_key, old_password)
        if not user:
            return False, "Current password verification failed"

        # Minimum length for a NEW password. The privileged ADMIN account — the
        # credential that guards the live terminal — carries the 12-character
        # floor used elsewhere in this module (`_init_default_users` requires 12
        # for the opt-in trader/demo accounts, and 8 for the admin env override).
        # Non-admin accounts keep the historical 6-character floor, which the
        # existing coverage pins; raising it globally would require that coverage
        # to change first, which is out of scope here.
        required = 12 if str(cls._users.get(user_key, {}).get("role", "")).upper() == "ADMIN" else 6
        if len(new_password.strip()) < required:
            return False, f"New password must be at least {required} characters long"

        pwd_hash = cls._hash_password(new_password.strip())
        # NOTE: only the in-memory `_users` record is updated. There is no
        # persistence layer for a changed password, so the change is reverted on
        # the next restart — and, for the admin account, by the next
        # `_init_default_users()` re-sync. Fixing that needs `.jarvis_admin_pass`
        # handling and is deliberately out of scope for this task.
        cls._users[user_key]["password_hash"] = pwd_hash
        try:
            conn = _get_user_db()
            try:
                with conn:
                    conn.execute("UPDATE users SET password_hash = ? WHERE username = ?", (pwd_hash, user_key))
            finally:
                conn.close()
        except Exception as e:
            logger.warning(f"Could not persist changed password for {user_key}: {e}")
        return True, "Password updated successfully"

    @classmethod
    def register_user(cls, username: str, password_raw: str, full_name: str = "", client_ip: str = "") -> Tuple[bool, str]:
        """
        Registers a new user account with PENDING_APPROVAL status.
        Requires administrator review before the account can log in.
        """
        cls._init_default_users()
        uname = (username or "").strip().lower()
        pwd = (password_raw or "").strip()
        name = (full_name or "").strip() or uname.title()

        if not uname or len(uname) < 3 or len(uname) > 30:
            return False, "Username must be between 3 and 30 characters"

        if not re.match(r"^[a-zA-Z0-9_.-]+$", uname):
            return False, "Username may only contain letters, numbers, underscores, and hyphens"

        reserved = {"admin", "administrator", "system", "root", "guest", "anonymous", "null", "undefined"}
        if uname in reserved:
            return False, f"Username '{uname}' is reserved"

        if len(pwd) < 8:
            return False, "Password must be at least 8 characters long"
        if len(pwd) > 72:
            return False, "Password must not exceed 72 characters"

        if client_ip:
            allowed, remaining = cls.check_rate_limit(client_ip + "_reg", client_ip)
            if not allowed:
                return False, f"Too many registration requests. Please wait {remaining}s."

        if uname in cls._users:
            return False, "Username is already registered"

        conn = _get_user_db()
        try:
            with conn:
                existing = conn.execute("SELECT username FROM users WHERE username = ?", (uname,)).fetchone()
                if existing:
                    return False, "Username is already registered"
                pwd_hash = cls._hash_password(pwd)
                now = time.time()
                conn.execute("""
                    INSERT INTO users (username, password_hash, full_name, role, status, created_at, approved_by, approved_at)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """, (uname, pwd_hash, name, "TRADER", "PENDING_APPROVAL", now, None, None))
                cls._users[uname] = {
                    "username": uname,
                    "role": "TRADER",
                    "full_name": name,
                    "password_hash": pwd_hash,
                    "status": "PENDING_APPROVAL",
                    "created_at": now,
                    "approved_by": None,
                    "approved_at": None
                }
        finally:
            conn.close()

        logger.info(f"New user registration submitted for '{uname}' ({name}) - status: PENDING_APPROVAL")
        return True, "Registration submitted successfully! Your account is pending administrator approval."

    @classmethod
    def get_all_users(cls) -> List[Dict[str, Any]]:
        """
        Returns list of all users and their approval statuses.
        Never exposes password hashes or salts.
        """
        cls._init_default_users()
        users_list = []
        conn = _get_user_db()
        try:
            rows = conn.execute("SELECT username, full_name, role, status, created_at, approved_by, approved_at FROM users ORDER BY created_at DESC").fetchall()
            for r in rows:
                users_list.append({
                    "username": r["username"],
                    "full_name": r["full_name"],
                    "role": r["role"],
                    "status": r["status"],
                    "created_at": float(r["created_at"] or 0.0),
                    "approved_by": r["approved_by"],
                    "approved_at": float(r["approved_at"] or 0.0) if r["approved_at"] else None
                })
        finally:
            conn.close()
        return users_list

    @classmethod
    def approve_user(cls, username: str, role: str = "TRADER", approved_by: str = "admin") -> Tuple[bool, str]:
        """
        Approves a pending user registration, setting status to ACTIVE and assigning role.
        """
        cls._init_default_users()
        uname = (username or "").strip().lower()
        role_clean = (role or "TRADER").strip().upper()
        if role_clean not in ("ADMIN", "TRADER", "VIEWER"):
            return False, f"Invalid role '{role_clean}'. Must be ADMIN, TRADER, or VIEWER."

        conn = _get_user_db()
        try:
            with conn:
                row = conn.execute("SELECT * FROM users WHERE username = ?", (uname,)).fetchone()
                if not row:
                    return False, f"User '{uname}' does not exist"
                now = time.time()
                conn.execute("""
                    UPDATE users SET status = 'ACTIVE', role = ?, approved_by = ?, approved_at = ?
                    WHERE username = ?
                """, (role_clean, approved_by, now, uname))
                if uname in cls._users:
                    cls._users[uname]["status"] = "ACTIVE"
                    cls._users[uname]["role"] = role_clean
                    cls._users[uname]["approved_by"] = approved_by
                    cls._users[uname]["approved_at"] = now
        finally:
            conn.close()

        logger.info(f"User '{uname}' approved as {role_clean} by {approved_by}")
        return True, f"User '{uname}' successfully approved as {role_clean}"

    @classmethod
    def reject_user(cls, username: str, rejected_by: str = "admin") -> Tuple[bool, str]:
        """
        Rejects a user registration, setting status to REJECTED.
        """
        cls._init_default_users()
        uname = (username or "").strip().lower()
        if uname == ADMIN_USERNAME.strip().lower():
            return False, "Cannot reject primary administrator account"

        conn = _get_user_db()
        try:
            with conn:
                row = conn.execute("SELECT * FROM users WHERE username = ?", (uname,)).fetchone()
                if not row:
                    return False, f"User '{uname}' does not exist"
                now = time.time()
                conn.execute("""
                    UPDATE users SET status = 'REJECTED', approved_by = ?, approved_at = ?
                    WHERE username = ?
                """, (rejected_by, now, uname))
                if uname in cls._users:
                    cls._users[uname]["status"] = "REJECTED"
                    cls._users[uname]["approved_by"] = rejected_by
                    cls._users[uname]["approved_at"] = now
        finally:
            conn.close()

        cls.revoke_user_sessions(uname)
        logger.info(f"User '{uname}' rejected by {rejected_by}")
        return True, f"User '{uname}' registration has been rejected"

    @classmethod
    def delete_user(cls, username: str) -> Tuple[bool, str]:
        """
        Deletes a user account from the system entirely.
        """
        cls._init_default_users()
        uname = (username or "").strip().lower()
        if uname == ADMIN_USERNAME.strip().lower():
            return False, "Cannot delete primary administrator account"

        conn = _get_user_db()
        try:
            with conn:
                row = conn.execute("SELECT * FROM users WHERE username = ?", (uname,)).fetchone()
                if not row:
                    return False, f"User '{uname}' does not exist"
                conn.execute("DELETE FROM users WHERE username = ?", (uname,))
                cls._users.pop(uname, None)
        finally:
            conn.close()

        cls.revoke_user_sessions(uname)
        logger.info(f"User '{uname}' deleted from system")
        return True, f"User '{uname}' deleted successfully"

    @classmethod
    def revoke_user_sessions(cls, username: str):
        """Revokes all active session tokens associated with a given username."""
        uname = (username or "").strip().lower()
        for token in list(cls._tokens):
            try:
                parts = token.split(":")
                if len(parts) >= 1 and parts[0].lower() == uname:
                    cls.revoke_token(token)
            except Exception:
                pass


# Auto-initialize default users on module load
RemoteAuthEngine._init_default_users()
