"""
Polls a Google Sheet of Telegram channel usernames/links and fills in every
detail it can read about each one via Telethon. Add a new channel to column A
-> on the next poll the script resolves it and writes the title, id, member
count, description, creation date, last post date and so on across that row.

Read-only against Telegram: it never joins, posts, or messages anything. It
only resolves the channel and reads its public info.

Sheet layout (row 1 = header, written automatically if row 1 is empty):

    A: CHANNEL        the input -- @name, t.me/name, https://t.me/name,
                      a private invite link (t.me/+hash, t.me/joinchat/hash),
                      or a numeric id (-1001234567890)
    B: TITLE          C: USERNAME     D: ID           E: TYPE
    F: MEMBERS        G: POSTS        H: DESCRIPTION  I: CREATED
    J: LAST POST      K: FLAGS        L: LINK         M: STATUS

POSTS is the channel's whole message history count, read from the same
request that reads LAST POST (see _history_stats). It's blank for anything
whose history we can't see -- a private invite preview, or a restricted chat.

Queue semantics, same as balance_checker.py / sheet_watcher.py: a row with A
filled and an EMPTY STATUS is picked up, fetched once, and STATUS is then set
to a result -- which also means it won't be touched again on later polls. Add
a row -> it gets filled within one poll interval. To refresh a row (member
counts change), clear its STATUS cell by hand.

A real result (a resolved channel, or a genuine "no such username" / "this is
private") is terminal. A Telegram FLOOD WAIT is NOT: it's written as a
"⏳ flood-wait, auto-retrying" marker and picked back up automatically once
the wait expires, no manual clearing needed -- same distinction
balance_checker.py draws between an application result and an infra block.

Setup
-----
1. Telegram API credentials (these are NOT a bot token -- get them once at
   https://my.telegram.org -> API development tools):

       TELEGRAM_API_ID=1234567
       TELEGRAM_API_HASH=0123456789abcdef0123456789abcdef

2. Google service account -- identical to the hedge/balance sheets, reuse the
   same service_account.json and just share THIS sheet with its email
   (Editor access, since the details/STATUS get written back).

3. Install: .venv/bin/pip install -r requirements.txt

Run:
    CHANNEL_SHEET_SPREADSHEET_ID=<sheet id> \\
        .venv/bin/python channel_info.py --env .env.channels

The FIRST run signs a real Telegram account in interactively (it asks for a
phone number, then the code Telegram sends you, then your 2FA password if you
have one) and saves the session to CHANNEL_SESSION (default
`channel_info.session`, gitignored). Every run after that is unattended.

A **user account is required, not a bot** -- bots can't resolve arbitrary
public usernames they've never interacted with, and can't read a private
invite link's info at all, which is most of what this script does.

Pacing: Telegram rate-limits username resolution, and the punishment is a
FloodWait measured in minutes-to-hours, so rows are processed one at a time
with CHANNEL_SPACING_SECONDS (default 3s) between them. Don't parallelize
this -- unlike balance_checker.py's proxy pool, the limit here is per
ACCOUNT, so extra concurrency buys nothing and risks the account.
"""
import asyncio
import os
import sys
import time
import traceback

# --env <path> selects which env file to load, same convention as
# telegram_bot.py / balance_checker.py / sheet_watcher.py.
_env_file = ".env"
if "--env" in sys.argv:
    _idx = sys.argv.index("--env")
    if _idx + 1 < len(sys.argv):
        _env_file = sys.argv[_idx + 1]
ONCE = "--once" in sys.argv

from dotenv import load_dotenv

import gspread
from google.oauth2.service_account import Credentials

from telethon import TelegramClient
from telethon.errors import FloodWaitError, RPCError
from telethon.tl.functions.channels import GetFullChannelRequest
from telethon.tl.functions.messages import CheckChatInviteRequest, GetFullChatRequest
from telethon.tl.functions.users import GetFullUserRequest
from telethon.tl.types import Channel, Chat, ChatInviteAlready, ChatInvitePeek, User

# Unlike the other sheet scripts this one does NOT import main -- nothing here
# touches Playwright or the signup engine, so there's no bare load_dotenv()
# already having run, and no override ordering gotcha to work around.
load_dotenv(_env_file, override=True)

API_ID = os.environ.get("TELEGRAM_API_ID", "")
API_HASH = os.environ.get("TELEGRAM_API_HASH", "")
SESSION = os.environ.get("CHANNEL_SESSION", "channel_info")

SPREADSHEET_ID = os.environ.get("CHANNEL_SHEET_SPREADSHEET_ID", "")
WORKSHEET_GID = os.environ.get("CHANNEL_SHEET_WORKSHEET_GID", "0")
CREDENTIALS_FILE = os.environ.get(
    "CHANNEL_SHEET_CREDENTIALS_FILE",
    os.environ.get("SHEET_CREDENTIALS_FILE", "service_account.json"))
POLL_SECONDS = int(os.environ.get("CHANNEL_POLL_SECONDS", "20"))
# Gap between two channel lookups. Telegram's limit here is per-ACCOUNT, so
# this is the only lever -- see the module docstring's "Pacing" note.
SPACING_SECONDS = float(os.environ.get("CHANNEL_SPACING_SECONDS", "3"))
# How much of a channel's description to keep. Sheets cells hold far more,
# this is purely so a huge "about" doesn't make the sheet unreadable.
DESCRIPTION_LIMIT = int(os.environ.get("CHANNEL_DESCRIPTION_LIMIT", "1000"))

COL_CHANNEL, COL_TITLE, COL_USERNAME, COL_ID, COL_TYPE, COL_MEMBERS, COL_POSTS = range(1, 8)
COL_DESCRIPTION, COL_CREATED, COL_LAST_POST, COL_FLAGS, COL_LINK, COL_STATUS = range(8, 14)

HEADER = ["CHANNEL", "TITLE", "USERNAME", "ID", "TYPE", "MEMBERS", "POSTS",
          "DESCRIPTION", "CREATED", "LAST POST", "FLAGS", "LINK", "STATUS"]
LAST_COL = "M"  # the STATUS column -- keep in step with HEADER's length

# Set while a FloodWait is in force -- every row is skipped (no Telegram call
# at all, not even a paced one) until it passes, since a flood wait applies to
# the whole account, not to the one channel that happened to trigger it.
_blocked_until = 0.0


def normalize_target(raw):
    """Turns whatever someone pasted in column A into something Telethon can
    resolve. Returns ("invite", hash), ("public", username), or ("id", int).

    Handles: @name, name, t.me/name, https://t.me/name, telegram.me/name,
    t.me/s/name (the web-preview form), t.me/+hash, t.me/joinchat/hash, and
    bare numeric ids like -1001234567890. Query strings are dropped."""
    s = (raw or "").strip()
    if not s:
        return None
    s = s.split("?")[0].split("#")[0].strip()
    for prefix in ("https://", "http://"):
        if s.lower().startswith(prefix):
            s = s[len(prefix):]
    for host in ("t.me/", "telegram.me/", "telegram.dog/", "www.t.me/"):
        if s.lower().startswith(host):
            s = s[len(host):]
            break
    s = s.strip("/")
    if s.lower().startswith("joinchat/"):
        return ("invite", s[len("joinchat/"):])
    if s.startswith("+"):
        return ("invite", s[1:])
    if s.lower().startswith("s/"):  # t.me/s/<channel> preview link
        s = s[2:]
    s = s.lstrip("@").strip("/")
    if not s:
        return None
    # A bare id, with or without the -100 supergroup prefix.
    if s.lstrip("-").isdigit():
        return ("id", int(s))
    return ("public", s)


def _fmt_date(dt):
    return dt.strftime("%Y-%m-%d %H:%M") if dt else ""


def _flags_for(entity):
    """The badges Telegram puts on a chat: verified, scam, fake, restricted,
    plus whether it's private (no public username)."""
    flags = []
    for name in ("verified", "scam", "fake", "restricted"):
        if getattr(entity, name, False):
            flags.append(name)
    if isinstance(entity, Channel) and not getattr(entity, "username", None):
        flags.append("private")
    if getattr(entity, "bot", False):
        flags.append("bot")
    return ", ".join(flags)


async def _history_stats(client, entity):
    """Returns (total_posts, last_post_date) from ONE request: Telethon's
    get_messages() hands back a TotalList, whose .total is the full history
    count regardless of how many messages were actually fetched -- so asking
    for the single latest message answers both questions at once.

    Best-effort: a chat we can't read (private, restricted) leaves both blank
    rather than failing the whole row."""
    try:
        msgs = await client.get_messages(entity, limit=1)
    except FloodWaitError:
        raise
    except Exception:
        return "", ""
    total = getattr(msgs, "total", None)
    last = _fmt_date(msgs[0].date) if msgs else ""
    return ("" if total is None else total), last


async def fetch_details(client, raw):
    """Resolves one column-A value and returns the B..K values for its row.

    Raises on anything that means "this row has no answer" -- the caller turns
    that into a STATUS message."""
    target = normalize_target(raw)
    if target is None:
        raise ValueError("could not read a channel name or link out of this cell")
    kind, value = target

    if kind == "invite":
        return await _fetch_invite(client, value)

    try:
        entity = await client.get_entity(value)
    except (ValueError, TypeError) as e:
        if kind == "id":
            # Telegram won't hand out a chat by bare id unless this account
            # has seen it before -- a username or link always works, an id
            # only sometimes does. Say so instead of leaking the raw error.
            raise ValueError(
                "Telegram won't look this one up by id alone — put its "
                "@username or t.me link in column A instead") from e
        raise

    return await _details_for_entity(client, entity)


async def _details_for_entity(client, entity):
    """The shared "read everything off a resolved chat" half of
    fetch_details(), split out so the invite path can hand in a chat object it
    already has rather than resolving it a second time."""
    if isinstance(entity, Channel):
        full = await client(GetFullChannelRequest(entity))
        fc = full.full_chat
        if entity.broadcast:
            kind_label = "channel"
        elif getattr(entity, "gigagroup", False):
            kind_label = "broadcast group"
        else:
            kind_label = "supergroup"
        link = (f"https://t.me/{entity.username}" if entity.username
                else getattr(getattr(fc, "exported_invite", None), "link", "") or "")
        posts, last_post = await _history_stats(client, entity)
        return {
            "title": entity.title or "",
            "username": f"@{entity.username}" if entity.username else "",
            "id": entity.id,
            "type": kind_label,
            "members": fc.participants_count if fc.participants_count is not None else "",
            "posts": posts,
            "description": (fc.about or "")[:DESCRIPTION_LIMIT],
            "created": _fmt_date(entity.date),
            "last_post": last_post,
            "flags": _flags_for(entity),
            "link": link,
        }

    if isinstance(entity, Chat):
        # A plain old (non-super) group.
        full = await client(GetFullChatRequest(entity.id))
        fc = full.full_chat
        posts, last_post = await _history_stats(client, entity)
        return {
            "title": entity.title or "",
            "username": "",
            "id": entity.id,
            "type": "group",
            "members": getattr(entity, "participants_count", "") or "",
            "posts": posts,
            "description": (getattr(fc, "about", "") or "")[:DESCRIPTION_LIMIT],
            "created": _fmt_date(entity.date),
            "last_post": last_post,
            "flags": _flags_for(entity),
            "link": getattr(getattr(fc, "exported_invite", None), "link", "") or "",
        }

    if isinstance(entity, User):
        full = await client(GetFullUserRequest(entity.id))
        name = " ".join(p for p in (entity.first_name, entity.last_name) if p)
        return {
            "title": name,
            "username": f"@{entity.username}" if entity.username else "",
            "id": entity.id,
            "type": "bot" if entity.bot else "user",
            "members": "",
            "posts": "",
            "description": (getattr(full.full_user, "about", "") or "")[:DESCRIPTION_LIMIT],
            "created": "",
            "last_post": "",
            "flags": _flags_for(entity),
            "link": f"https://t.me/{entity.username}" if entity.username else "",
        }

    raise ValueError(f"unsupported Telegram object: {type(entity).__name__}")


async def _fetch_invite(client, invite_hash):
    """Private invite links. CheckChatInviteRequest reads a link's info
    WITHOUT joining -- which is the point, this script never joins anything.

    What comes back depends on membership: if the account is already in the
    chat Telegram hands back the real chat object (full detail, same as a
    public channel); if not, it's a preview -- title, member count and
    description only, with no id or creation date to report."""
    inv = await client(CheckChatInviteRequest(invite_hash))

    chat = getattr(inv, "chat", None)  # ChatInviteAlready / ChatInvitePeek
    if isinstance(inv, (ChatInviteAlready, ChatInvitePeek)) and chat is not None:
        details = await _details_for_entity(client, chat)
        details["link"] = details["link"] or f"https://t.me/+{invite_hash}"
        return details

    return {
        "title": getattr(inv, "title", "") or "",
        "username": "",
        "id": "",
        "type": "channel (invite preview)" if getattr(inv, "broadcast", False) else "group (invite preview)",
        "members": getattr(inv, "participants_count", "") or "",
        # An invite preview exposes no message history at all -- we're not in
        # the chat, so there's nothing to count.
        "posts": "",
        "description": (getattr(inv, "about", "") or "")[:DESCRIPTION_LIMIT],
        "created": "",
        "last_post": "",
        "flags": ", ".join(f for f in ("verified", "scam", "fake", "request_needed")
                           if getattr(inv, f, False)) or "private",
        "link": f"https://t.me/+{invite_hash}",
    }


def get_worksheet():
    scopes = ["https://www.googleapis.com/auth/spreadsheets"]
    try:
        creds = Credentials.from_service_account_file(CREDENTIALS_FILE, scopes=scopes)
    except FileNotFoundError:
        print(f"\nCan't find the Google service account key file "
              f"'{CREDENTIALS_FILE}'.\nIt's the same one the other sheet "
              f"scripts use -- copy it into this folder, or point "
              f"CHANNEL_SHEET_CREDENTIALS_FILE at wherever it lives.")
        sys.exit(1)
    client = gspread.authorize(creds)
    try:
        sh = client.open_by_key(SPREADSHEET_ID)
    except (PermissionError, gspread.exceptions.APIError) as e:
        # By far the most common first-run stumble: the sheet exists but was
        # never shared with the service account, which surfaces as a bare
        # 403/PermissionError several frames deep in gspread. Say what to do
        # instead of dumping that traceback.
        email = getattr(creds, "service_account_email", "(unknown)")
        print(f"\nGoogle refused access to that spreadsheet ({type(e).__name__}).\n"
              f"\nThe sheet has to be SHARED with the service account, the same "
              f"way you'd share it with a person:\n"
              f"  1. Open https://docs.google.com/spreadsheets/d/{SPREADSHEET_ID}\n"
              f"  2. Share -> paste this address -> give it Editor access:\n\n"
              f"       {email}\n\n"
              f"  3. Run this again.\n"
              f"\nEditor (not Viewer) is needed because the script writes the "
              f"details and STATUS back into the sheet.\n"
              f"If you meant a different sheet, check "
              f"CHANNEL_SHEET_SPREADSHEET_ID in your env file -- it's the long "
              f"code in the sheet's URL, between /d/ and /edit.")
        sys.exit(1)
    for ws in sh.worksheets():
        if str(ws.id) == str(WORKSHEET_GID):
            return ws
    return sh.sheet1


def ensure_header(ws, rows):
    """Writes the header row if the sheet is completely empty, so a brand-new
    sheet doesn't need to be set up by hand. An existing sheet with anything
    at all in row 1 is left exactly as it is."""
    if rows and any(str(c).strip() for c in rows[0]):
        return rows
    ws.update(values=[HEADER], range_name=f"A1:{LAST_COL}1")
    print("wrote the header row into the empty sheet")
    return [HEADER] + rows[1:]


def write_row(ws, row_idx, details, status):
    """One API call per row: B..M in a single update (a dozen separate
    update_cell calls per row would burn Google's per-minute write quota fast
    on a big sheet)."""
    ws.update(values=[[
        details["title"], details["username"], details["id"], details["type"],
        details["members"], details["posts"], details["description"],
        details["created"], details["last_post"], details["flags"],
        details["link"], status,
    ]], range_name=f"B{row_idx}:{LAST_COL}{row_idx}")


def write_status(ws, row_idx, status):
    """Failure path: touch ONLY the status cell, so a row that was filled in
    successfully on an earlier pass doesn't get blanked out by a later error
    -- same reasoning as balance_checker.py leaving BALANCE alone on failure."""
    ws.update(values=[[status]], range_name=f"{LAST_COL}{row_idx}")


async def process_row(client, ws, row_idx, raw):
    global _blocked_until
    ts = time.strftime("%Y-%m-%d %H:%M")
    print(f"[row {row_idx}] fetching {raw!r}...")
    try:
        details = await fetch_details(client, raw)
    except FloodWaitError as e:
        # Telegram-wide cooldown on this account, not a fact about this
        # channel. Pause everything and leave the row retryable.
        _blocked_until = time.time() + e.seconds + 5
        mins = max(1, round(e.seconds / 60))
        print(f"[row {row_idx}] FLOOD WAIT {e.seconds}s -- pausing all lookups")
        write_status(ws, row_idx, f"⏳ {ts} flood-wait ~{mins} min, auto-retrying")
        return
    except (ValueError, TypeError) as e:
        write_status(ws, row_idx, f"❌ {ts} — {str(e)[:200]}")
        print(f"[row {row_idx}] failed: {e}")
        return
    except RPCError as e:
        write_status(ws, row_idx, f"❌ {ts} — {type(e).__name__}: {str(e)[:180]}")
        print(f"[row {row_idx}] failed: {e}")
        return
    except Exception as e:
        traceback.print_exc()
        write_status(ws, row_idx, f"❌ {ts} — {str(e)[:200]}")
        return

    write_row(ws, row_idx, details, f"✅ fetched {ts}")
    print(f"[row {row_idx}] done: {details['title']!r} "
          f"({details['type']}, {details['members']} members)")


async def poll_once(client, ws):
    rows = ws.get_all_values()
    rows = ensure_header(ws, rows)
    for i, row in enumerate(rows[1:], start=2):  # row 1 is the header
        row = list(row) + [""] * (len(HEADER) - len(row))
        raw, status = row[0].strip(), row[COL_STATUS - 1].strip()
        if not raw:
            continue
        # Empty STATUS (never fetched) or a "⏳ flood-wait" marker (not a real
        # result) are both eligible. Anything else is terminal -- clear it by
        # hand to refresh that row.
        if status and not status.startswith("⏳"):
            continue
        if time.time() < _blocked_until:
            wait = int(_blocked_until - time.time())
            print(f"flood-wait active, {wait}s left -- skipping the rest of this poll")
            return
        await process_row(client, ws, i, raw)
        await asyncio.sleep(SPACING_SECONDS)


async def run():
    if not SPREADSHEET_ID:
        print("CHANNEL_SHEET_SPREADSHEET_ID is not set -- nothing to poll. "
              "Set it to the sheet's id (the long code in its URL) and try again.")
        sys.exit(1)
    if not (API_ID and API_HASH):
        print("TELEGRAM_API_ID / TELEGRAM_API_HASH are not set. Get them from "
              "https://my.telegram.org -> API development tools (these are NOT "
              "a bot token) and put them in your env file.")
        sys.exit(1)

    print(f"channel_info: spreadsheet={SPREADSHEET_ID} gid={WORKSHEET_GID} "
          f"poll={POLL_SECONDS}s spacing={SPACING_SECONDS}s session={SESSION}")
    ws = get_worksheet()

    client = TelegramClient(SESSION, int(API_ID), API_HASH)
    # First run only: asks for phone -> code -> 2FA password, then saves the
    # session file so every later run starts silently.
    await client.start()
    me = await client.get_me()
    print(f"signed in as {me.first_name} (@{me.username})" if me else "signed in")

    try:
        while True:
            try:
                await poll_once(client, ws)
            except Exception:
                traceback.print_exc()
            if ONCE:
                break
            await asyncio.sleep(POLL_SECONDS)
    except KeyboardInterrupt:
        print("\nStopping...")
    finally:
        await client.disconnect()


def main():
    try:
        asyncio.run(run())
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
