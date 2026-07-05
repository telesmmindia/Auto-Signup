"""SQLite storage for generated test accounts, so credentials can be retrieved
later for repeat testing (e.g. logging back in with an already-verified test
account) instead of only living in terminal scrollback."""
import sqlite3
from pathlib import Path

DB_PATH = Path("accounts.db")

SCHEMA = """
CREATE TABLE IF NOT EXISTS accounts (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    created_at TEXT NOT NULL DEFAULT (datetime('now', 'localtime')),
    username TEXT NOT NULL,
    email TEXT NOT NULL,
    password TEXT NOT NULL,
    phone TEXT,
    status TEXT NOT NULL DEFAULT 'generated',
    notes TEXT,
    screenshot TEXT,
    proxy TEXT,
    url TEXT
);
"""

# Columns added after the initial release; migrated in on every connect so
# older accounts.db files keep working.
_MIGRATED_COLUMNS = ("proxy TEXT", "url TEXT")


def get_connection(db_path=DB_PATH):
    conn = sqlite3.connect(db_path)
    conn.execute(SCHEMA)
    for col_def in _MIGRATED_COLUMNS:
        try:
            conn.execute(f"ALTER TABLE accounts ADD COLUMN {col_def}")
        except sqlite3.OperationalError:
            pass  # column already exists
    conn.commit()
    return conn


def insert_account(conn, acct):
    """Store a freshly generated account, return its row id."""
    cur = conn.execute(
        "INSERT INTO accounts (username, email, password, phone, proxy, url) VALUES (?, ?, ?, ?, ?, ?)",
        (acct["username"], acct["email"], acct["password"], acct.get("phone", ""),
         acct.get("proxy"), acct.get("url")),
    )
    conn.commit()
    return cur.lastrowid


def update_status(conn, row_id, status, notes=None, screenshot=None):
    conn.execute(
        "UPDATE accounts SET status = ?, notes = ?, screenshot = ? WHERE id = ?",
        (status, notes, screenshot, row_id),
    )
    conn.commit()


COLUMNS = ("id", "created_at", "username", "email", "password", "phone",
           "status", "proxy", "url", "screenshot", "notes")


def list_accounts(conn, limit=20, status=None):
    cols = ", ".join(COLUMNS)
    if status:
        cur = conn.execute(
            f"SELECT {cols} FROM accounts WHERE status = ? ORDER BY id DESC LIMIT ?",
            (status, limit),
        )
    else:
        cur = conn.execute(
            f"SELECT {cols} FROM accounts ORDER BY id DESC LIMIT ?",
            (limit,),
        )
    return cur.fetchall()


def print_accounts(conn, limit=20, status=None):
    rows = list_accounts(conn, limit=limit, status=status)
    if not rows:
        print("No accounts stored yet.")
        return
    widths = [max(len(str(h)), *(len(str(r[i])) for r in rows)) for i, h in enumerate(COLUMNS)]
    widths = [min(w, 40) for w in widths]

    def fmt_row(vals):
        return " | ".join(str(v)[:w].ljust(w) for v, w in zip(vals, widths))

    print(fmt_row(COLUMNS))
    print("-+-".join("-" * w for w in widths))
    for r in rows:
        print(fmt_row(r))
