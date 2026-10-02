"""Telegram UI: commands, inline menus, conversation for creating/editing entries."""

from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass, field

import httpx

from . import formatting as fmt
from .chains import CHAINS, EVM_KEYS
from .config import Settings
from .db import Database
from .models import MAX_TICKERS, Entry, Result, parse_tickers
from .scanner import Scanner
from .telegram_api import TelegramAPI, TelegramError

log = logging.getLogger(__name__)

# Commands handled by the scanner; anything else is left to the host bot.
COMMANDS = {"/start", "/scanner", "/nouveau", "/annuler", "/scanner_aide"}
MAX_NAME_LEN = 40
MAX_TICKER_LEN = 20

CONFLICT_HELP = (
    "⚠️ <b>Menu du scanner désactivé</b>\n"
    "Un autre programme lit déjà les messages de ce bot (ton bot principal ?). "
    "Les notifications du scanner continuent, mais les boutons et /scanner ne répondront pas.\n\n"
    "Solutions :\n"
    "1. Crée un 2e bot avec @BotFather et mets son token dans <code>TELEGRAM_BOT_TOKEN</code> du scanner\n"
    "2. Ou intègre le scanner dans ton bot principal (voir INTEGRATION.md)"
)


@dataclass
class Session:
    state: str
    entry_id: int | None = None
    name: str = ""
    tickers: list[str] = field(default_factory=list)
    selected: set[str] = field(default_factory=set)
    picker_message_id: int | None = None


class TelegramNotifier:
    """Sends the scanner notifications (used by Scanner)."""

    def __init__(self, api: TelegramAPI, chat_id: int, min_interval: float = 0.4):
        self.api = api
        self.chat_id = chat_id
        self.min_interval = min_interval
        self._last = 0.0
        self._lock = asyncio.Lock()

    async def send(self, entry: Entry, result: Result, labels: list[str], note: str | None) -> int | None:
        async with self._lock:
            wait = self._last + self.min_interval - time.monotonic()
            if wait > 0:
                await asyncio.sleep(wait)
            msg = await self.api.send_message(
                self.chat_id, fmt.notification_text(entry, result, labels, note),
                fmt.notification_keyboard(entry, result),
            )
            self._last = time.monotonic()
        return (msg or {}).get("message_id")

    async def edit(self, entry: Entry, result: Result, labels: list[str], note: str | None, message_id: int) -> None:
        await self.api.edit_message(
            self.chat_id, message_id, fmt.notification_text(entry, result, labels, note),
            fmt.notification_keyboard(entry, result),
        )


class BotUI:
    def __init__(self, api: TelegramAPI, db: Database, scanner: Scanner, settings: Settings,
                 handle_start: bool = True):
        self.api = api
        self.db = db
        self.scanner = scanner
        self.settings = settings
        self.sessions: dict[int, Session] = {}
        self._warned_ids: set[int] = set()
        # /start belongs to the host bot when the scanner is embedded in it.
        self.commands = set(COMMANDS) if handle_start else COMMANDS - {"/start"}

    # ---- helpers -------------------------------------------------------
    def realtime(self) -> dict[str, bool]:
        rt = {}
        for key, chain in CHAINS.items():
            if chain.is_evm:
                rpc = self.settings.evm_rpc.get(key)
                rt[key] = bool(rpc and rpc.usable and chain.has_onchain_config)
            else:
                rt[key] = self.settings.pumpportal_enabled or bool(self.settings.solana_ws_url)
        return rt

    def allowed(self, user_id: int) -> bool:
        return user_id in self.settings.allowed_users

    async def _show(self, chat_id: int, message_id: int | None, text: str, markup: dict | None) -> int | None:
        if message_id is not None:
            try:
                await self.api.edit_message(chat_id, message_id, text, markup)
                return message_id
            except TelegramError as exc:
                log.debug("edit impossible (%s), nouveau message", exc)
        msg = await self.api.send_message(chat_id, text, markup)
        return (msg or {}).get("message_id")

    async def show_main(self, chat_id: int, message_id: int | None = None) -> None:
        entries = self.db.list_entries()
        counts = {e.id: self.db.count_results(e.id) for e in entries}
        text, markup = fmt.main_menu(entries, counts, self.scanner.source_names())
        await self._show(chat_id, message_id, text, markup)

    async def show_entry(self, chat_id: int, entry_id: int, message_id: int | None = None,
                         header: str | None = None) -> None:
        entry = self.db.get_entry(entry_id)
        if entry is None:
            await self.show_main(chat_id, message_id)
            return
        total, liquid = self.db.count_results(entry.id)
        text, markup = fmt.entry_card(entry, total, liquid, self.realtime(), self.settings.timezone, header)
        await self._show(chat_id, message_id, text, markup)

    async def show_results(self, chat_id: int, entry_id: int, page: int, message_id: int | None = None,
                           updated_at: float | None = None) -> None:
        entry = self.db.get_entry(entry_id)
        if entry is None:
            await self.show_main(chat_id, message_id)
            return
        text, markup = fmt.results_page(
            entry, self.db.results_for_entry(entry.id), page, self.settings.results_page_size,
            self.settings.timezone, updated_at,
        )
        await self._show(chat_id, message_id, text, markup)

    async def show_picker(self, chat_id: int, session: Session, message_id: int | None = None) -> None:
        title = f"Blockchains pour « {session.name} »"
        text, markup = fmt.chain_picker(title, session.selected, self.realtime())
        session.picker_message_id = await self._show(chat_id, message_id, text, markup)

    # ---- polling -------------------------------------------------------
    async def run_polling(self) -> None:
        offset = None
        log.info("Menu Telegram actif (/scanner)")
        while True:
            try:
                updates = await self.api.get_updates(offset)
            except TelegramError as exc:
                if exc.code == 409:
                    log.error("Conflit getUpdates: %s", exc.description)
                    await self._notify_conflict()
                    return
                if exc.code == 401:
                    log.error("TELEGRAM_BOT_TOKEN invalide")
                    return
                log.warning("getUpdates: %s", exc)
                await asyncio.sleep(3)
                continue
            except httpx.HTTPError as exc:
                log.warning("getUpdates réseau: %s", exc)
                await asyncio.sleep(3)
                continue
            for update in updates or []:
                offset = update["update_id"] + 1
                try:
                    await self.handle_update(update)
                except Exception:  # noqa: BLE001
                    log.exception("update %s", update.get("update_id"))

    async def _notify_conflict(self) -> None:
        chat = self.settings.target_chat_id
        if chat is None:
            return
        try:
            await self.api.send_message(chat, CONFLICT_HELP)
        except TelegramError:
            pass

    async def handle_update(self, update: dict) -> bool:
        """Process a raw Telegram update. Returns False when it is not for the scanner."""
        if "callback_query" in update:
            return await self.on_callback(update["callback_query"])
        if "message" in update:
            return await self.on_message(update["message"])
        return False

    # ---- messages ------------------------------------------------------
    async def on_message(self, message: dict) -> bool:
        user_id = (message.get("from") or {}).get("id")
        chat_id = (message.get("chat") or {}).get("id")
        text = (message.get("text") or "").strip()
        if user_id is None or chat_id is None or not text:
            return False
        command = text.split()[0].split("@")[0].lower() if text.startswith("/") else None
        if command is not None and command not in self.commands:
            return False
        if not self.allowed(user_id):
            if command in ("/start", "/scanner") and not self.settings.allowed_users and user_id not in self._warned_ids:
                self._warned_ids.add(user_id)
                await self.api.send_message(
                    chat_id, f"Ton ID Telegram est <code>{user_id}</code>.\nAjoute-le dans "
                             "<code>TELEGRAM_ALLOWED_USERS</code> (.env) puis relance le scanner.")
            return command is not None
        if command is not None:
            await self.on_command(chat_id, user_id, command)
            return True
        session = self.sessions.get(user_id)
        if session is None:
            return False
        await self.on_input(chat_id, user_id, session, text)
        return True

    async def on_command(self, chat_id: int, user_id: int, command: str) -> None:
        if command in ("/scanner", "/start"):
            self.sessions.pop(user_id, None)
            await self.show_main(chat_id)
        elif command == "/nouveau":
            await self.start_creation(chat_id, user_id)
        elif command == "/annuler":
            if self.sessions.pop(user_id, None):
                await self.api.send_message(chat_id, "❌ Saisie annulée.")
            await self.show_main(chat_id)
        elif command == "/scanner_aide":
            await self.api.send_message(chat_id, fmt.HELP)

    async def start_creation(self, chat_id: int, user_id: int) -> None:
        self.sessions[user_id] = Session(state="new_name")
        await self.api.send_message(chat_id, "🆕 <b>Nouveau projet</b>\nQuel nom pour ce projet ? "
                                             "(/annuler pour arrêter)")

    async def on_input(self, chat_id: int, user_id: int, session: Session, text: str) -> None:
        if session.state in ("new_name", "edit_name"):
            name = " ".join(text.split())
            if not name or len(name) > MAX_NAME_LEN:
                await self.api.send_message(chat_id, f"Le nom doit faire entre 1 et {MAX_NAME_LEN} caractères.")
                return
            if session.state == "edit_name":
                self.db.update_entry(session.entry_id, name=name)
                self.sessions.pop(user_id, None)
                self.scanner.reload()
                await self.show_entry(chat_id, session.entry_id, header="✅ Nom modifié")
                return
            session.name = name
            session.state = "new_tickers"
            await self.api.send_message(
                chat_id, f"Projet <b>{fmt.esc(name)}</b>.\nEnvoie <b>1 à {MAX_TICKERS} tickers</b> "
                         "séparés par un espace ou une virgule (ex : <code>$ABC, ABCD</code>).")
        elif session.state in ("new_tickers", "edit_tickers"):
            tickers = parse_tickers(text)
            if not 1 <= len(tickers) <= MAX_TICKERS or any(len(t) > MAX_TICKER_LEN for t in tickers):
                await self.api.send_message(
                    chat_id, f"Il faut entre 1 et {MAX_TICKERS} tickers (max {MAX_TICKER_LEN} caractères chacun).")
                return
            if session.state == "edit_tickers":
                self.db.update_entry(session.entry_id, tickers=tickers)
                self.sessions.pop(user_id, None)
                self.scanner.reload()
                await self.show_entry(chat_id, session.entry_id, header="✅ Tickers modifiés")
                return
            session.tickers = tickers
            session.state = "pick_chains"
            await self.show_picker(chat_id, session)

    # ---- callbacks -----------------------------------------------------
    async def on_callback(self, query: dict) -> bool:
        user_id = (query.get("from") or {}).get("id")
        message = query.get("message") or {}
        chat_id = (message.get("chat") or {}).get("id")
        message_id = message.get("message_id")
        data = query.get("data") or ""
        if not data.startswith(fmt.CB):
            return False
        data = data[len(fmt.CB):]
        if user_id is None or not self.allowed(user_id):
            await self.api.answer_callback(query["id"])
            return True
        if chat_id is None:
            chat_id = user_id
            message_id = None
        parts = data.split(":")
        action, args = parts[0], parts[1:]
        toast: str | None = None
        alert = False

        def arg_int(i: int, default: int = 0) -> int:
            try:
                return int(args[i])
            except (IndexError, ValueError):
                return default

        if action == "noop":
            pass
        elif action == "m":
            await self.show_main(chat_id, message_id)
        elif action == "n":
            await self.start_creation(chat_id, user_id)
        elif action == "e":
            await self.show_entry(chat_id, arg_int(0), message_id)
        elif action == "p":
            entry = self.db.get_entry(arg_int(0))
            if entry is not None:
                self.db.update_entry(entry.id, paused=not entry.paused)
                self.scanner.reload()
                toast = "▶️ Scan repris" if entry.paused else "⏸ Scan en pause"
            await self.show_entry(chat_id, arg_int(0), message_id)
        elif action == "ed":
            entry = self.db.get_entry(arg_int(0))
            if entry is not None:
                text, markup = fmt.edit_menu(entry)
                await self._show(chat_id, message_id, text, markup)
        elif action == "en":
            self.sessions[user_id] = Session(state="edit_name", entry_id=arg_int(0))
            await self.api.send_message(chat_id, "Envoie le nouveau nom (/annuler pour arrêter).")
        elif action == "et":
            entry = self.db.get_entry(arg_int(0))
            if entry is not None:
                self.sessions[user_id] = Session(state="edit_tickers", entry_id=entry.id)
                await self.api.send_message(
                    chat_id, f"Tickers actuels : <b>{fmt.esc(fmt.tickers_text(entry))}</b>\n"
                             f"Envoie 1 à {MAX_TICKERS} nouveaux tickers (/annuler pour arrêter).")
        elif action == "ec":
            entry = self.db.get_entry(arg_int(0))
            if entry is not None:
                session = Session(state="pick_chains", entry_id=entry.id, name=entry.name,
                                  selected=set(entry.chains))
                self.sessions[user_id] = session
                await self.show_picker(chat_id, session, message_id)
        elif action == "d":
            entry = self.db.get_entry(arg_int(0))
            if entry is not None:
                text, markup = fmt.delete_confirm(entry, self.db.count_results(entry.id)[0])
                await self._show(chat_id, message_id, text, markup)
        elif action == "dy":
            self.db.delete_entry(arg_int(0))
            self.scanner.reload()
            toast = "🗑 Projet supprimé"
            await self.show_main(chat_id, message_id)
        elif action == "r":
            await self.show_results(chat_id, arg_int(0), arg_int(1), message_id)
        elif action == "rn":
            await self.show_results(chat_id, arg_int(0), 0)
        elif action == "rf":
            await self.api.answer_callback(query["id"], "🔄 Mise à jour des market caps…")
            try:
                await self.scanner.refresh_entry(arg_int(0))
            except Exception:  # noqa: BLE001
                log.exception("refresh")
            await self.show_results(chat_id, arg_int(0), arg_int(1), message_id, updated_at=time.time())
            return True
        elif action in ("ct", "ca", "cv", "cx"):
            toast, alert = await self.on_picker(chat_id, user_id, message_id, action, args)
        await self.api.answer_callback(query["id"], toast, alert)
        return True

    async def on_picker(self, chat_id: int, user_id: int, message_id: int | None, action: str,
                        args: list[str]) -> tuple[str | None, bool]:
        session = self.sessions.get(user_id)
        if session is None or session.state != "pick_chains":
            return "Session expirée, recommence depuis /scanner", True
        if action == "ct" and args and args[0] in CHAINS:
            session.selected ^= {args[0]}
        elif action == "ca":
            mode = args[0] if args else ""
            if mode == "evm":
                session.selected |= set(EVM_KEYS)
            elif mode == "all":
                session.selected = set(CHAINS)
            elif mode == "none":
                session.selected = set()
        elif action == "cx":
            self.sessions.pop(user_id, None)
            if session.entry_id is not None:
                await self.show_entry(chat_id, session.entry_id, message_id)
            else:
                await self._show(chat_id, message_id, "❌ Création annulée.", None)
            return None, False
        elif action == "cv":
            if not session.selected:
                return "Choisis au moins une blockchain", True
            chains = [k for k in CHAINS if k in session.selected]
            self.sessions.pop(user_id, None)
            if session.entry_id is not None:
                self.db.update_entry(session.entry_id, chains=chains)
                self.scanner.reload()
                await self.show_entry(chat_id, session.entry_id, message_id, header="✅ Chaînes modifiées")
            else:
                entry = self.db.create_entry(session.name, session.tickers, chains)
                self.scanner.reload()
                await self.show_entry(chat_id, entry.id, message_id, header="✅ Projet créé, scan lancé")
            return None, False
        await self.show_picker(chat_id, session, message_id)
        return None, False
