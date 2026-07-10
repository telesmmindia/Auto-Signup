"""SQLite storage for generated test accounts, so credentials can be retrieved
later for repeat testing (e.g. logging back in with an already-verified test
account) instead of only living in terminal scrollback."""
import csv
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
    url TEXT,
    referral_code TEXT
);
"""

# Columns added after the initial release; migrated in on every connect so
# older accounts.db files keep working.
_MIGRATED_COLUMNS = ("proxy TEXT", "url TEXT", "referral_code TEXT")


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
        "INSERT INTO accounts (username, email, password, phone, proxy, url, referral_code) "
        "VALUES (?, ?, ?, ?, ?, ?, ?)",
        (acct["username"], acct["email"], acct["password"], acct.get("phone", ""),
         acct.get("proxy"), acct.get("url"), acct.get("referral_code")),
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
           "status", "proxy", "url", "referral_code", "screenshot", "notes")


def list_accounts(conn, limit=20, status=None, url=None):
    """`limit=None` returns every stored account (used for CSV export).
    `url` filters to signups made against that exact site URL (the `url`
    column, NULL for signups that used the default SITE_URL rather than an
    explicit override)."""
    cols = ", ".join(COLUMNS)
    query = f"SELECT {cols} FROM accounts"
    conditions = []
    params = []
    if status:
        conditions.append("status = ?")
        params.append(status)
    if url:
        conditions.append("url = ?")
        params.append(url)
    if conditions:
        query += " WHERE " + " AND ".join(conditions)
    query += " ORDER BY id DESC"
    if limit is not None:
        query += " LIMIT ?"
        params.append(limit)
    cur = conn.execute(query, params)
    return cur.fetchall()


def get_account(conn, row_id):
    cur = conn.execute(f"SELECT {', '.join(COLUMNS)} FROM accounts WHERE id = ?", (row_id,))
    return cur.fetchone()


def export_csv(conn, path, limit=None, status=None, url=None, row_id=None):
    """Write stored accounts to a CSV file at `path`. Returns the row count.
    If `row_id` is given, exports just that one account (ignores the other
    filters) -- used to hand back a single signup's details as a file rather
    than text."""
    rows = ([get_account(conn, row_id)] if row_id is not None
            else list_accounts(conn, limit=limit, status=status, url=url))
    rows = [r for r in rows if r is not None]
    with open(path, "w", newline="") as f:
        writer = csv.writer(f)
        writer.writerow(COLUMNS)
        writer.writerows(rows)
    return len(rows)


def print_accounts(conn, limit=20, status=None, url=None):
    rows = list_accounts(conn, limit=limit, status=status, url=url)
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
