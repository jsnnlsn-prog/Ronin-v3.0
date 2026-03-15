"""
RONIN Vault — Encrypted API Key Storage
==========================================
Fernet symmetric encryption for secrets.
Master key from env var or auto-generated and persisted to ~/.ronin/vault.key.

Keys are stored as: name → Fernet(value) in the vault SQLite table.
At runtime, consuming code calls vault.get("ANTHROPIC_API_KEY") instead of os.getenv().
"""

import os
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import List, Optional

from cryptography.fernet import Fernet

# ─── CONFIGURATION ───────────────────────────────────────────────────────────

RONIN_HOME = Path(os.environ.get("RONIN_HOME", Path.home() / ".ronin"))
VAULT_KEY_FILE = RONIN_HOME / "vault.key"


def _get_or_create_master_key() -> bytes:
    """Load master key from env, key file, or generate a new one."""
    # 1. Env var takes priority
    env_key = os.environ.get("RONIN_VAULT_KEY")
    if env_key:
        return env_key.encode() if isinstance(env_key, str) else env_key

    # 2. Key file
    if VAULT_KEY_FILE.exists():
        return VAULT_KEY_FILE.read_bytes().strip()

    # 3. Generate new key and persist
    RONIN_HOME.mkdir(parents=True, exist_ok=True)
    key = Fernet.generate_key()
    VAULT_KEY_FILE.write_bytes(key)
    VAULT_KEY_FILE.chmod(0o600)
    print(f"🔐 Generated new vault key: {VAULT_KEY_FILE}")
    return key


# ─── DATABASE SETUP ──────────────────────────────────────────────────────────

def init_vault_table(conn: sqlite3.Connection) -> None:
    conn.executescript("""
        CREATE TABLE IF NOT EXISTS vault (
            key_name TEXT PRIMARY KEY,
            encrypted_value TEXT NOT NULL,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL
        );
    """)
    conn.commit()


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


# ─── VAULT STORE ─────────────────────────────────────────────────────────────

class VaultStore:
    """Encrypted key-value store for secrets."""

    def __init__(self, conn: sqlite3.Connection, master_key: Optional[bytes] = None):
        self.conn = conn
        key = master_key or _get_or_create_master_key()
        self._fernet = Fernet(key)

    def set(self, name: str, value: str) -> None:
        """Store a secret, encrypted."""
        encrypted = self._fernet.encrypt(value.encode()).decode()
        now = _now_iso()
        self.conn.execute(
            """INSERT INTO vault (key_name, encrypted_value, created_at, updated_at)
               VALUES (?,?,?,?)
               ON CONFLICT(key_name) DO UPDATE SET encrypted_value=excluded.encrypted_value, updated_at=excluded.updated_at""",
            (name, encrypted, now, now),
        )
        self.conn.commit()

    def get(self, name: str) -> Optional[str]:
        """Retrieve and decrypt a secret. Returns None if not found."""
        row = self.conn.execute(
            "SELECT encrypted_value FROM vault WHERE key_name=?", (name,)
        ).fetchone()
        if not row:
            return None
        return self._fernet.decrypt(row[0].encode()).decode()

    def delete(self, name: str) -> bool:
        """Delete a secret. Returns True if it existed."""
        cur = self.conn.execute("DELETE FROM vault WHERE key_name=?", (name,))
        self.conn.commit()
        return cur.rowcount > 0

    def list_keys(self) -> List[dict]:
        """List all stored key names (no values)."""
        rows = self.conn.execute(
            "SELECT key_name, created_at, updated_at FROM vault ORDER BY key_name"
        ).fetchall()
        return [{"name": r[0], "created_at": r[1], "updated_at": r[2]} for r in rows]

    def get_or_env(self, name: str) -> Optional[str]:
        """Try vault first, fall back to env var."""
        v = self.get(name)
        if v:
            return v
        return os.environ.get(name)


# ─── IMPORT FROM ENV ─────────────────────────────────────────────────────────

_KNOWN_API_KEYS = [
    "ANTHROPIC_API_KEY",
    "VENICE_API_KEY",
    "GEMINI_API_KEY",
    "SAM_API_KEY",
    "OPENAI_API_KEY",
]


def import_env_to_vault(vault: VaultStore) -> List[str]:
    """
    On startup, import known API keys from env → vault if not already present.
    Returns list of key names that were imported.
    """
    imported = []
    for key_name in _KNOWN_API_KEYS:
        env_val = os.environ.get(key_name)
        if env_val and vault.get(key_name) is None:
            vault.set(key_name, env_val)
            imported.append(key_name)
    return imported


# ─── GLOBAL ACCESSOR ─────────────────────────────────────────────────────────
# api.py sets this up after creating the VaultStore
_vault_instance: Optional[VaultStore] = None


def get_vault() -> Optional[VaultStore]:
    return _vault_instance


def set_vault(v: VaultStore) -> None:
    global _vault_instance
    _vault_instance = v
