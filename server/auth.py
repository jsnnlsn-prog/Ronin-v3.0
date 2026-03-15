"""
RONIN Auth — JWT Authentication + User Management
====================================================
JWT-based authentication with bcrypt password hashing.
Provides FastAPI dependency `get_current_user` for protected routes.

Users table lives in the same memory.db as all other data.
Default admin is auto-created from env vars on first run.
"""

import os
import sqlite3
import uuid
from datetime import datetime, timedelta, timezone
from typing import Optional

from fastapi import Depends, HTTPException, Request, status
from fastapi.security import OAuth2PasswordBearer, OAuth2PasswordRequestForm
from jose import JWTError, jwt
from passlib.context import CryptContext
from pydantic import BaseModel

# ─── CONFIGURATION ───────────────────────────────────────────────────────────

SECRET_KEY = os.environ.get("RONIN_JWT_SECRET", "ronin-dev-secret-change-in-production")
ALGORITHM = "HS256"
ACCESS_TOKEN_EXPIRE_MINUTES = 60
REFRESH_TOKEN_EXPIRE_DAYS = 7

pwd_context = CryptContext(schemes=["bcrypt"], deprecated="auto")
oauth2_scheme = OAuth2PasswordBearer(tokenUrl="/api/auth/login", auto_error=False)


# ─── MODELS ──────────────────────────────────────────────────────────────────

class User(BaseModel):
    id: str
    username: str
    is_admin: bool
    created_at: str


class UserInDB(User):
    password_hash: str


class TokenResponse(BaseModel):
    access_token: str
    refresh_token: str
    token_type: str = "bearer"
    expires_in: int = ACCESS_TOKEN_EXPIRE_MINUTES * 60


class RegisterRequest(BaseModel):
    username: str
    password: str
    is_admin: bool = False


class RefreshRequest(BaseModel):
    refresh_token: str


# ─── DATABASE HELPERS ────────────────────────────────────────────────────────

def init_user_tables(conn: sqlite3.Connection) -> None:
    """Create users table if not exists."""
    conn.executescript("""
        CREATE TABLE IF NOT EXISTS users (
            id TEXT PRIMARY KEY,
            username TEXT UNIQUE NOT NULL,
            password_hash TEXT NOT NULL,
            is_admin INTEGER DEFAULT 0,
            created_at TEXT NOT NULL
        );
        CREATE INDEX IF NOT EXISTS idx_users_username ON users(username);

        CREATE TABLE IF NOT EXISTS refresh_tokens (
            token_hash TEXT PRIMARY KEY,
            user_id TEXT NOT NULL,
            expires_at TEXT NOT NULL,
            created_at TEXT NOT NULL,
            FOREIGN KEY (user_id) REFERENCES users(id)
        );
    """)
    conn.commit()


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


# ─── PASSWORD + TOKEN UTILS ──────────────────────────────────────────────────

def hash_password(password: str) -> str:
    return pwd_context.hash(password)


def verify_password(plain: str, hashed: str) -> bool:
    return pwd_context.verify(plain, hashed)


def create_access_token(user_id: str, username: str, is_admin: bool) -> str:
    expire = datetime.now(timezone.utc) + timedelta(minutes=ACCESS_TOKEN_EXPIRE_MINUTES)
    payload = {
        "sub": user_id,
        "username": username,
        "is_admin": is_admin,
        "exp": expire,
        "type": "access",
    }
    return jwt.encode(payload, SECRET_KEY, algorithm=ALGORITHM)


def create_refresh_token(user_id: str) -> str:
    expire = datetime.now(timezone.utc) + timedelta(days=REFRESH_TOKEN_EXPIRE_DAYS)
    payload = {
        "sub": user_id,
        "exp": expire,
        "jti": str(uuid.uuid4()),
        "type": "refresh",
    }
    return jwt.encode(payload, SECRET_KEY, algorithm=ALGORITHM)


def decode_token(token: str) -> dict:
    """Decode and validate a JWT. Raises JWTError on failure."""
    return jwt.decode(token, SECRET_KEY, algorithms=[ALGORITHM])


# ─── USER CRUD ───────────────────────────────────────────────────────────────

class UserStore:
    """Manages user records in SQLite."""

    def __init__(self, conn: sqlite3.Connection):
        self.conn = conn

    def create(self, username: str, password: str, is_admin: bool = False) -> User:
        user_id = str(uuid.uuid4())
        created = _now_iso()
        ph = hash_password(password)
        try:
            self.conn.execute(
                "INSERT INTO users (id, username, password_hash, is_admin, created_at) VALUES (?,?,?,?,?)",
                (user_id, username, ph, int(is_admin), created),
            )
            self.conn.commit()
        except sqlite3.IntegrityError:
            raise ValueError(f"Username '{username}' already exists")
        return User(id=user_id, username=username, is_admin=is_admin, created_at=created)

    def get_by_username(self, username: str) -> Optional[UserInDB]:
        row = self.conn.execute(
            "SELECT id, username, password_hash, is_admin, created_at FROM users WHERE username=?",
            (username,),
        ).fetchone()
        if not row:
            return None
        return UserInDB(
            id=row["id"], username=row["username"], password_hash=row["password_hash"],
            is_admin=bool(row["is_admin"]), created_at=row["created_at"],
        )

    def get_by_id(self, user_id: str) -> Optional[User]:
        row = self.conn.execute(
            "SELECT id, username, is_admin, created_at FROM users WHERE id=?",
            (user_id,),
        ).fetchone()
        if not row:
            return None
        return User(
            id=row["id"], username=row["username"],
            is_admin=bool(row["is_admin"]), created_at=row["created_at"],
        )

    def count(self) -> int:
        return self.conn.execute("SELECT COUNT(*) FROM users").fetchone()[0]

    def list_all(self) -> list:
        rows = self.conn.execute(
            "SELECT id, username, is_admin, created_at FROM users ORDER BY created_at"
        ).fetchall()
        return [
            User(id=r["id"], username=r["username"], is_admin=bool(r["is_admin"]), created_at=r["created_at"])
            for r in rows
        ]

    def authenticate(self, username: str, password: str) -> Optional[UserInDB]:
        user = self.get_by_username(username)
        if not user or not verify_password(password, user.password_hash):
            return None
        return user


# ─── DEFAULT ADMIN BOOTSTRAP ─────────────────────────────────────────────────

def ensure_default_admin(conn: sqlite3.Connection) -> None:
    """Create default admin from env vars if no users exist."""
    store = UserStore(conn)
    if store.count() > 0:
        return
    admin_user = os.environ.get("RONIN_ADMIN_USER", "admin")
    admin_pass = os.environ.get("RONIN_ADMIN_PASS", "ronin-admin-change-me")
    store.create(admin_user, admin_pass, is_admin=True)
    print(f"🔑 Created default admin user: '{admin_user}'")


# ─── FASTAPI DEPENDENCIES ────────────────────────────────────────────────────

def set_db_getter(fn):
    """No-op — kept for backwards compatibility. Auth now uses Request.app.state."""
    pass


async def get_current_user(
    request: Request,
    token: str = Depends(oauth2_scheme),
) -> Optional[User]:
    """
    FastAPI dependency — validates JWT and returns the current user.
    Returns None if no token provided (for optional auth).
    Raises 401 if token is invalid.
    """
    if token is None:
        return None
    try:
        payload = decode_token(token)
        if payload.get("type") != "access":
            raise JWTError("Not an access token")
        user_id: str = payload.get("sub")
        if not user_id:
            raise JWTError("Missing sub claim")
    except JWTError:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Not authenticated",
            headers={"WWW-Authenticate": "Bearer"},
        )
    # Use the app's shared DB connection (consistent with write endpoints)
    try:
        db = request.app.state.db
    except AttributeError:
        from ronin_mcp_server import MEMORY_DB, init_database
        db = init_database(MEMORY_DB)
    store = UserStore(db)
    user = store.get_by_id(user_id)
    if not user:
        raise HTTPException(status_code=401, detail="User not found")
    return user


async def require_auth(user: Optional[User] = Depends(get_current_user)) -> User:
    """Require authentication — raises 401 if not authenticated."""
    if user is None:
        raise HTTPException(status_code=401, detail="Not authenticated")
    return user


async def require_admin(user: User = Depends(require_auth)) -> User:
    """Require admin role — raises 403 if not admin."""
    if not user.is_admin:
        raise HTTPException(status_code=403, detail="Insufficient permissions")
    return user
