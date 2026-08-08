"""Configuration and admin storage."""

import asyncio
import json
import os
from pathlib import Path

BASE_DIR = Path(__file__).parent

# --- Load .env (no external dependency) ---------------------------------
_env_file = BASE_DIR / ".env"
if _env_file.exists():
    for _line in _env_file.read_text().splitlines():
        _line = _line.strip()
        if not _line or _line.startswith("#") or "=" not in _line:
            continue
        _k, _v = _line.split("=", 1)
        os.environ.setdefault(_k.strip(), _v.strip().strip("'\""))

BOT_TOKEN = os.environ.get("BOT_TOKEN", "")

# The master admin. Only this user can add/remove other admins.
MASTER_ADMIN_ID = int(os.environ.get("MASTER_ADMIN_ID", "0"))

# How many tasks run at the same time.
WORKER_COUNT = int(os.environ.get("WORKER_COUNT", "3"))

ADMINS_FILE = BASE_DIR / "admins.json"


class AdminStore:
    """Admin list persisted to admins.json. Master admin is always included."""

    def __init__(self, path: Path = ADMINS_FILE, master_id: int = MASTER_ADMIN_ID):
        self.path = path
        self.master_id = master_id
        self._lock = asyncio.Lock()
        self._admins: set[int] = self._load()

    def _load(self) -> set[int]:
        if not self.path.exists():
            return set()
        try:
            data = json.loads(self.path.read_text())
            return {int(x) for x in data.get("admins", [])}
        except (ValueError, OSError):
            return set()

    def _save(self) -> None:
        tmp = self.path.with_suffix(".json.tmp")
        tmp.write_text(json.dumps({"admins": sorted(self._admins)}, indent=2))
        tmp.replace(self.path)

    def is_admin(self, user_id: int) -> bool:
        return user_id == self.master_id or user_id in self._admins

    def is_master(self, user_id: int) -> bool:
        return user_id == self.master_id

    def all_admins(self) -> list[int]:
        return sorted(self._admins)

    async def add(self, user_id: int) -> bool:
        """Returns False if the user was already an admin."""
        async with self._lock:
            if user_id == self.master_id or user_id in self._admins:
                return False
            self._admins.add(user_id)
            self._save()
            return True

    async def remove(self, user_id: int) -> bool:
        """Returns False if the user was not an admin. The master cannot be removed."""
        async with self._lock:
            if user_id not in self._admins:
                return False
            self._admins.discard(user_id)
            self._save()
            return True


admin_store = AdminStore()


#PROXIES = ["http://twosPUg5QBK19Lq:EaMpYBwaTX11aAG@thehub.proxy-cheap.com:8080",]
PROXIES = ["http://twosPUg5QBK19Lq:EaMpYBwaTX11aAG@thehub.proxy-cheap.com:8080",]