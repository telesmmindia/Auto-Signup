"""Admin-only Telegram bot.

Admins submit jobs as `<link> <platform> <count>`, one per line.
Run with: python bot.py
"""

import asyncio
import contextlib
import html
import logging
from typing import Any, Awaitable, Callable

from aiogram import BaseMiddleware, Bot, Dispatcher, Router, F
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from aiogram.filters import Command, CommandObject, CommandStart
from aiogram.types import Message, TelegramObject, User

import handlers  # noqa: F401  -- registers the platform handlers
from config import BOT_TOKEN, MASTER_ADMIN_ID, WORKER_COUNT, admin_store
from tasks import HANDLERS, PLATFORMS, Task, parse_batch, queue, worker

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)-8s %(name)s: %(message)s",
)
log = logging.getLogger("bot")

router = Router()


class AdminOnlyMiddleware(BaseMiddleware):
    """Drops every update from a user who is not an admin."""

    async def __call__(
        self,
        handler: Callable[[TelegramObject, dict[str, Any]], Awaitable[Any]],
        event: TelegramObject,
        data: dict[str, Any],
    ) -> Any:
        user: User | None = data.get("event_from_user")
        if user is None or not admin_store.is_admin(user.id):
            log.warning("blocked non-admin %s (%s)", user and user.id, user and user.username)
            if isinstance(event, Message):
                await event.answer("You are not authorized to use this bot.")
            return None
        return await handler(event, data)


def _parse_user_id(command: CommandObject, message: Message) -> int | None:
    """User id from the command argument, or from the replied-to message."""
    if command.args:
        arg = command.args.split()[0]
        if arg.lstrip("-").isdigit():
            return int(arg)
        return None
    if message.reply_to_message and message.reply_to_message.from_user:
        return message.reply_to_message.from_user.id
    return None


# --- Commands ------------------------------------------------------------

HELP = (
    "<b>Submit a job</b>\n"
    "Send one or more lines:\n"
    "<code>&lt;link&gt; &lt;platform&gt; &lt;count&gt;</code>\n\n"
    "Example:\n"
    "<code>https://t.me/example telegram 500</code>\n\n"
    "<b>Platforms:</b> {platforms}\n\n"
    "<b>Commands</b>\n"
    "/id — show your user id\n"
    "/status — queue status\n"
    "/help — this message\n\n"
    "<b>Master only</b>\n"
    "/addadmin &lt;id&gt; (or reply to a message)\n"
    "/deladmin &lt;id&gt;\n"
    "/admins — list admins"
)


@router.message(CommandStart())
@router.message(Command("help"))
async def cmd_help(message: Message) -> None:
    platforms = ", ".join(sorted(set(PLATFORMS.values())))
    await message.answer(HELP.format(platforms=platforms))


@router.message(Command("id"))
async def cmd_id(message: Message) -> None:
    user = message.from_user
    role = "master admin" if admin_store.is_master(user.id) else "admin"
    await message.answer(f"Your id: <code>{user.id}</code>\nRole: {role}")


@router.message(Command("status"))
async def cmd_status(message: Message) -> None:
    await message.answer(
        f"Queued: <b>{queue.qsize()}</b>\nWorkers: <b>{WORKER_COUNT}</b>"
    )


@router.message(Command("admins"))
async def cmd_admins(message: Message) -> None:
    if not admin_store.is_master(message.from_user.id):
        await message.answer("Only the master admin can do that.")
        return
    lines = [f"Master: <code>{MASTER_ADMIN_ID}</code>"]
    admins = admin_store.all_admins()
    if admins:
        lines += [f"• <code>{a}</code>" for a in admins]
    else:
        lines.append("<i>No other admins.</i>")
    await message.answer("\n".join(lines))


@router.message(Command("addadmin"))
async def cmd_addadmin(message: Message, command: CommandObject) -> None:
    if not admin_store.is_master(message.from_user.id):
        await message.answer("Only the master admin can do that.")
        return
    user_id = _parse_user_id(command, message)
    if user_id is None:
        await message.answer("Usage: <code>/addadmin &lt;user_id&gt;</code> or reply to their message.")
        return
    if await admin_store.add(user_id):
        await message.answer(f"Added <code>{user_id}</code> as admin.")
    else:
        await message.answer(f"<code>{user_id}</code> is already an admin.")


@router.message(Command("deladmin"))
async def cmd_deladmin(message: Message, command: CommandObject) -> None:
    if not admin_store.is_master(message.from_user.id):
        await message.answer("Only the master admin can do that.")
        return
    user_id = _parse_user_id(command, message)
    if user_id is None:
        await message.answer("Usage: <code>/deladmin &lt;user_id&gt;</code> or reply to their message.")
        return
    if user_id == MASTER_ADMIN_ID:
        await message.answer("The master admin cannot be removed.")
        return
    if await admin_store.remove(user_id):
        await message.answer(f"Removed <code>{user_id}</code>.")
    else:
        await message.answer(f"<code>{user_id}</code> is not an admin.")


# --- Job submission ------------------------------------------------------

@router.message(F.text & ~F.text.startswith("/"))
async def submit_jobs(message: Message) -> None:
    tasks, errors = parse_batch(message.text, message.from_user.id)

    if errors:
        lines = ["<b>Skipped lines:</b>"]
        lines += [f"• <code>{html.escape(line)}</code> — {html.escape(err)}" for line, err in errors]
        await message.answer("\n".join(lines))

    if not tasks:
        if not errors:
            await message.answer("Send: <code>&lt;link&gt; &lt;platform&gt; &lt;count&gt;</code>")
        return

    for task in tasks:
        await enqueue(task, message)


async def enqueue(task: Task, message: Message) -> None:
    """Queue a task and keep one status message updated as it runs."""
    if task.platform not in HANDLERS:
        await message.answer(f"No handler implemented for <b>{task.platform}</b> yet.")
        return

    header = f"<code>#{task.id}</code> {task.platform} ×{task.count}\n{html.escape(task.link)}"
    status_msg = await message.answer(f"{header}\n\n⏳ Queued…")

    async def progress(text: str) -> None:
        with contextlib.suppress(Exception):  # ignore "message is not modified" / rate limits
            await status_msg.edit_text(f"{header}\n\n🔄 {html.escape(text)}")

    async def done(finished: Task, result: str | None, error: str | None) -> None:
        body = f"✅ {html.escape(result or 'done')}" if error is None else f"❌ {html.escape(error)}"
        with contextlib.suppress(Exception):
            await status_msg.edit_text(f"{header}\n\n{body}")

    await queue.put((task, progress, done))


# --- Entrypoint ----------------------------------------------------------

async def main() -> None:
    if not BOT_TOKEN:
        raise SystemExit("BOT_TOKEN is not set — copy .env.example to .env and fill it in.")
    if not MASTER_ADMIN_ID:
        raise SystemExit("MASTER_ADMIN_ID is not set — put your Telegram user id in .env.")

    bot = Bot(BOT_TOKEN, default=DefaultBotProperties(parse_mode=ParseMode.HTML))
    dp = Dispatcher()
    dp.message.outer_middleware(AdminOnlyMiddleware())
    dp.callback_query.outer_middleware(AdminOnlyMiddleware())
    dp.include_router(router)

    workers = [asyncio.create_task(worker(f"w{i}")) for i in range(WORKER_COUNT)]
    log.info("started with %d workers, master admin %s", WORKER_COUNT, MASTER_ADMIN_ID)
    try:
        await dp.start_polling(bot)
    finally:
        for w in workers:
            w.cancel()
        await asyncio.gather(*workers, return_exceptions=True)
        await bot.session.close()


if __name__ == "__main__":
    with contextlib.suppress(KeyboardInterrupt):
        asyncio.run(main())
