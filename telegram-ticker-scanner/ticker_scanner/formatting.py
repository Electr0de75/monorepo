"""Telegram HTML rendering: notifications, menus, results list."""

from __future__ import annotations

import html
import re
import time
from datetime import datetime
from zoneinfo import ZoneInfo

from .chains import CHAINS, chain_label
from .health import SourceStats
from .models import (
    LABEL_LAUNCH, LABEL_LIQ, LABEL_PAIR, STATUS_LAUNCH, STATUS_LIQ, STATUS_PAIR, Entry, Result,
)

LABEL_TEXT = {
    LABEL_LAUNCH: "🟣 TOKEN CREATED",
    LABEL_PAIR: "🟡 PAIR CREATED",
    LABEL_LIQ: "🟢 LIQ ADDED",
}
STATUS_EMOJI = {STATUS_LAUNCH: "🟣", STATUS_PAIR: "🟡", STATUS_LIQ: "🟢"}
RULE = "━" * 22
TELEGRAM_LIMIT = 4096
# Prefix of every callback_data, so the scanner can share a bot with other handlers.
CB = "sc:"


def esc(text: str | None) -> str:
    return html.escape(text or "", quote=False)


def fmt_usd(value: float | None) -> str:
    if value is None:
        return "—"
    v = float(value)
    for div, suffix in ((1e9, "B"), (1e6, "M"), (1e3, "K")):
        if abs(v) >= div:
            return f"${v / div:.2f}".rstrip("0").rstrip(".") + suffix
    return f"${v:,.0f}"


def fmt_age(ts: float, now: float | None = None) -> str:
    delta = max(0, int((now or time.time()) - ts))
    if delta < 60:
        return f"il y a {delta} s"
    if delta < 3600:
        return f"il y a {delta // 60} min"
    if delta < 86400:
        return f"il y a {delta // 3600} h"
    return f"il y a {delta // 86400} j"


def fmt_time(ts: float, tz: str, fmt: str = "%d/%m %H:%M") -> str:
    try:
        zone = ZoneInfo(tz)
    except Exception:  # noqa: BLE001
        zone = None
    return datetime.fromtimestamp(ts, zone).strftime(fmt)


# ---- links -------------------------------------------------------------
def _is_plain_address(chain_key: str, value: str | None) -> bool:
    if not value:
        return False
    chain = CHAINS.get(chain_key)
    if chain is not None and chain.is_evm:
        return len(value) == 42  # v4 pool ids are 32-byte hashes
    return True


def links(res: Result) -> list[tuple[str, str]]:
    chain = CHAINS.get(res.chain)
    if chain is None:
        return []
    use_pair = res.status != STATUS_LAUNCH and res.pair_address
    out = [("DexScreener", f"https://dexscreener.com/{chain.dexscreener}/"
                           f"{res.pair_address if use_pair else res.token_address}")]
    if chain.defined:
        target = res.pair_address if use_pair and _is_plain_address(res.chain, res.pair_address) else res.token_address
        out.append(("Defined", f"https://www.defined.fi/{chain.defined}/{target}"))
    if chain.gmgn:
        out.append(("GMGN", f"https://gmgn.ai/{chain.gmgn}/token/{res.token_address}"))
    return out


def links_keyboard(res: Result) -> list[list[dict]]:
    return [[{"text": name, "url": url} for name, url in links(res)]]


# ---- notifications ------------------------------------------------------
def notification_text(entry: Entry, res: Result, labels: list[str], note: str | None = None,
                      test: bool = False) -> str:
    title = " + ".join(LABEL_TEXT[lbl] for lbl in labels)
    if test:
        title = "🧪 NOTIF DE TEST\n" + title
    where = res.dex or "—"
    if res.quote_symbol and res.status != STATUS_LAUNCH:
        where += f" · /{res.quote_symbol}"
    def row(label: str, value: str) -> str:
        return f"{label:<6} : {value}"

    box = [title, RULE, row("Projet", entry.name), row("Ticker", f"${res.symbol}")]
    if res.name:
        box.append(row("Nom", res.name))
    box.append(row("Chaîne", chain_label(res.chain)))
    box.append(row("Launch" if res.status == STATUS_LAUNCH else "DEX", where))
    if res.status == STATUS_PAIR:
        box.append(row("Liq", "pas encore ajoutée"))
    else:
        if res.liquidity_usd is not None:
            box.append(row("Liq", fmt_usd(res.liquidity_usd)))
        if res.market_cap is not None:
            box.append(row("MC", fmt_usd(res.market_cap)))
        else:
            box.append(row("MC", note or "en attente…"))
    box.append(RULE)
    lines = [f"<pre>{esc(chr(10).join(box))}</pre>", f"📄 CA : <code>{esc(res.token_address)}</code>"]
    if res.pair_address and res.status != STATUS_LAUNCH:
        lines.append(f"🔗 Pair : <code>{esc(res.pair_address)}</code>")
    return "\n".join(lines)


def notification_keyboard(entry: Entry, res: Result) -> dict:
    rows = links_keyboard(res)
    rows.append([{"text": f"📋 Résultats · {entry.name}"[:60], "callback_data": f"{CB}rn:{entry.id}"}])
    return {"inline_keyboard": rows}


# ---- menus --------------------------------------------------------------
def tickers_text(entry: Entry) -> str:
    return " ".join(f"${t}" for t in entry.tickers)


def chains_text(chains: list[str]) -> str:
    return ", ".join(chain_label(c) for c in chains) or "—"


MENU_PAGE_SIZE = 10


def main_menu(entries: list[Entry], counts: dict[int, tuple[int, int]], sources: list[str],
              page: int = 0) -> tuple[str, dict]:
    active = sum(1 for e in entries if e.active)
    text = ["🛰 <b>Scanner de tickers</b>", f"{len(entries)} projet(s) · {active} actif(s)"]
    if sources:
        text.append(f"⚡ Temps réel : {esc(', '.join(sources))}")
    if not entries:
        text.append("\nAucun projet pour l'instant. Ajoute-en un 👇")
    pages = max(1, (len(entries) + MENU_PAGE_SIZE - 1) // MENU_PAGE_SIZE)
    page = min(max(0, page), pages - 1)
    rows = []
    for e in entries[page * MENU_PAGE_SIZE:(page + 1) * MENU_PAGE_SIZE]:
        total, _ = counts.get(e.id, (0, 0))
        icon = "▶️" if e.active else "⏸"
        rows.append([{"text": f"{icon} {e.name} · {tickers_text(e)} ({total})"[:64], "callback_data": f"{CB}e:{e.id}"}])
    if pages > 1:
        rows.append([
            {"text": "◀️", "callback_data": f"{CB}m:{max(0, page - 1)}"},
            {"text": f"{page + 1}/{pages}", "callback_data": f"{CB}noop"},
            {"text": "▶️", "callback_data": f"{CB}m:{min(pages - 1, page + 1)}"},
        ])
    rows.append([{"text": "➕ Nouveau projet", "callback_data": f"{CB}n"}])
    rows.append([{"text": "🔄 Actualiser", "callback_data": f"{CB}m:{page}"},
                 {"text": "🩺 État", "callback_data": f"{CB}st"}])
    return "\n".join(text), {"inline_keyboard": rows}


def entry_card(entry: Entry, total: int, liquid: int, realtime: dict[str, bool], tz: str,
               header: str | None = None) -> tuple[str, dict]:
    status = "▶️ Actif" if entry.active else "⏸ En pause"
    rt = " · ".join(f"{chain_label(c)} {'⚡' if realtime.get(c) else '🐢'}" for c in entry.chains)
    lines = []
    if header:
        lines.append(header)
    lines += [
        f"📁 <b>{esc(entry.name)}</b> — {status}",
        f"Tickers : <b>{esc(tickers_text(entry))}</b>",
        f"Chaînes : {esc(chains_text(entry.chains))}",
        f"Scan : {esc(rt)}",
        f"Créé le {fmt_time(entry.created_at, tz)}",
        f"Résultats : {total} (dont {liquid} avec liquidité / launch)",
        "",
        "<i>⚡ temps réel on-chain · 🐢 DexScreener uniquement</i>",
    ]
    rows = [
        [{"text": f"📋 Résultats ({total})", "callback_data": f"{CB}r:{entry.id}:0"}],
        [
            {"text": "▶️ Reprendre" if entry.paused else "⏸ Pause", "callback_data": f"{CB}p:{entry.id}"},
            {"text": "✏️ Modifier", "callback_data": f"{CB}ed:{entry.id}"},
        ],
        [{"text": "🗑 Supprimer", "callback_data": f"{CB}d:{entry.id}"}],
        [{"text": "⬅️ Liste des projets", "callback_data": f"{CB}m"}],
    ]
    return "\n".join(lines), {"inline_keyboard": rows}


def edit_menu(entry: Entry) -> tuple[str, dict]:
    text = f"✏️ Modifier <b>{esc(entry.name)}</b>"
    rows = [
        [{"text": "Nom", "callback_data": f"{CB}en:{entry.id}"},
         {"text": "Tickers", "callback_data": f"{CB}et:{entry.id}"},
         {"text": "Chaînes", "callback_data": f"{CB}ec:{entry.id}"}],
        [{"text": "⬅️ Retour", "callback_data": f"{CB}e:{entry.id}"}],
    ]
    return text, {"inline_keyboard": rows}


def delete_confirm(entry: Entry, total: int) -> tuple[str, dict]:
    text = f"🗑 Supprimer <b>{esc(entry.name)}</b> et ses {total} résultat(s) ?"
    rows = [[
        {"text": "✅ Oui, supprimer", "callback_data": f"{CB}dy:{entry.id}"},
        {"text": "❌ Annuler", "callback_data": f"{CB}e:{entry.id}"},
    ]]
    return text, {"inline_keyboard": rows}


def chain_picker(title: str, selected: set[str], realtime: dict[str, bool]) -> tuple[str, dict]:
    text = (f"⛓ <b>{esc(title)}</b>\nChoisis une ou plusieurs blockchains puis Valider.\n"
            "<i>⚡ = détection on-chain temps réel configurée</i>")
    rows, row = [], []
    for key, chain in CHAINS.items():
        mark = "✅" if key in selected else "▫️"
        bolt = " ⚡" if realtime.get(key) else ""
        row.append({"text": f"{mark} {chain.label}{bolt}", "callback_data": f"{CB}ct:{key}"})
        if len(row) == 3:
            rows.append(row)
            row = []
    if row:
        rows.append(row)
    rows.append([
        {"text": "Toutes EVM", "callback_data": f"{CB}ca:evm"},
        {"text": "Tout", "callback_data": f"{CB}ca:all"},
        {"text": "Aucune", "callback_data": f"{CB}ca:none"},
    ])
    rows.append([
        {"text": f"✅ Valider ({len(selected)})", "callback_data": f"{CB}cv"},
        {"text": "❌ Annuler", "callback_data": f"{CB}cx"},
    ])
    return text, {"inline_keyboard": rows}


def results_page(entry: Entry, results: list[Result], page: int, page_size: int, tz: str,
                 updated_at: float | None = None) -> tuple[str, dict]:
    pages = max(1, (len(results) + page_size - 1) // page_size)
    page = min(max(0, page), pages - 1)
    head = [
        f"📋 <b>{esc(entry.name)}</b> — {len(results)} résultat(s)",
        f"{esc(tickers_text(entry))} · {esc(chains_text(entry.chains))}",
    ]
    if updated_at:
        head.append(f"🕒 Mis à jour à {fmt_time(updated_at, tz, '%H:%M:%S')}")
    if not results:
        head.append("\nRien trouvé pour l'instant. Tu seras notifié dès qu'une pair, une liquidité "
                    "ou un token apparaît.")
    blocks = ["\n".join(head)]
    now = time.time()
    for i, res in enumerate(results[page * page_size:(page + 1) * page_size], start=page * page_size + 1):
        where = res.dex or "—"
        if res.quote_symbol and res.status != STATUS_LAUNCH:
            where += f" · /{res.quote_symbol}"
        if res.status == STATUS_PAIR:
            numbers = "💧 Liq : pas encore ajoutée"
        else:
            numbers = f"💧 Liq {fmt_usd(res.liquidity_usd)} · 📊 MC {fmt_usd(res.market_cap)}"
        link_line = " | ".join(f'<a href="{esc(url)}">{name}</a>' for name, url in links(res))
        title = f"<b>{i}.</b> {STATUS_EMOJI.get(res.status, '•')} <b>${esc(res.symbol)}</b>"
        if res.name:
            title += f" · {esc(res.name)}"
        blocks.append("\n".join([
            title,
            f"   {esc(chain_label(res.chain))} · {esc(where)} · {fmt_age(res.found_at, now)}",
            f"   {numbers}",
            f"   <code>{esc(res.token_address)}</code>",
            f"   {link_line}",
        ]))
    rows = []
    if pages > 1:
        rows.append([
            {"text": "◀️", "callback_data": f"{CB}r:{entry.id}:{max(0, page - 1)}"},
            {"text": f"{page + 1}/{pages}", "callback_data": f"{CB}noop"},
            {"text": "▶️", "callback_data": f"{CB}r:{entry.id}:{min(pages - 1, page + 1)}"},
        ])
    rows.append([{"text": "🔄 Refresh (MC & liq)", "callback_data": f"{CB}rf:{entry.id}:{page}"}])
    rows.append([{"text": "⬅️ Retour au projet", "callback_data": f"{CB}e:{entry.id}"}])
    legend = "\n<i>🟣 token créé · 🟡 pair créée · 🟢 liquidité ajoutée — tape un contrat pour le copier</i>"
    text = "\n\n".join(blocks) + "\n" + legend
    while len(text) > TELEGRAM_LIMIT and len(blocks) > 2:
        blocks.pop()  # never exceed Telegram's limit: drop the last items of the page
        text = "\n\n".join(blocks) + "\n\n<i>… (page tronquée)</i>"
    return text, {"inline_keyboard": rows}


HELP = (
    "🛰 <b>Scanner de tickers</b>\n\n"
    "/scanner — liste des projets surveillés\n"
    "/scanner_etat — santé des sources + notif de test\n"
    "/nouveau — ajouter un projet (nom, 1 à 3 tickers, blockchains)\n"
    "/annuler — annuler la saisie en cours\n\n"
    "Notifications :\n"
    "🟣 <b>TOKEN CREATED</b> — token créé (launchpad ou déploiement)\n"
    "🟡 <b>PAIR CREATED</b> — pair / pool créée sur un DEX\n"
    "🟢 <b>LIQ ADDED</b> — liquidité ajoutée à la pair"
)


def html_to_text(text: str) -> str:
    """Plain-text fallback of an HTML message."""
    return html.unescape(re.sub(r"<[^>]+>", "", text))


def sample_notification() -> tuple[Entry, Result]:
    """A realistic fake result for the test notification (WBNB on BSC, so links work)."""
    now = time.time()
    entry = Entry(id=0, name="Test", tickers=["TEST"], chains=["bsc"], paused=False,
                  created_at=now, scan_since=now)
    result = Result(
        id=0, entry_id=0, chain="bsc", result_key="test", kind="pair", status=STATUS_LIQ,
        token_address="0xbb4cdb9cbd36b01bd1cbaebf2de08d9173bc095c", symbol="TEST", name="Test Token",
        pair_address="0x16b9a82891338f9ba80e2d6970fdda79d1eb0dae", pool_kind="v2", dex="PancakeSwap v2",
        quote_symbol="WBNB", quote_address=None, source="test", tx_hash=None, found_at=now, liq_at=now,
        liquidity_usd=42000.0, market_cap=250000.0, updated_at=now,
    )
    return entry, result


def fmt_duration(seconds: float) -> str:
    seconds = int(max(0, seconds))
    days, rest = divmod(seconds, 86400)
    hours, rest = divmod(rest, 3600)
    minutes = rest // 60
    if days:
        return f"{days} j {hours} h"
    if hours:
        return f"{hours} h {minutes:02d} min"
    return f"{minutes} min"


STATE_ICON = {"ok": "✅", "stale": "⚠️", "down": "❌"}


def status_page(stats: list[SourceStats], *, started_at: float, sent: int, failed: int,
                notify_chat: int | None, warnings: list[str], has_entries: bool) -> tuple[str, dict]:
    now = time.time()
    lines = [
        "🩺 <b>État du scanner</b>",
        f"En ligne depuis {fmt_duration(now - started_at)}",
        f"Notifications : {sent} envoyée(s) · {failed} perdue(s)",
        "Chat des notifs : " + (f"<code>{notify_chat}</code> ✅" if notify_chat is not None
                                 else "❌ non configuré (TELEGRAM_NOTIFY_CHAT_ID)"),
        "",
        "<b>Sources</b>",
    ]
    if not has_entries:
        lines.append("Aucune source active : ajoute ou réactive un projet.")
    for st in stats:
        state = st.state(now)
        parts = [f"{STATE_ICON[state]} <b>{esc(st.name)}</b> — {esc(st.mode)}"]
        if st.events:
            parts.append(f"{st.events:,} évén.".replace(",", " "))
        if st.last_event:
            parts.append(f"dernier {fmt_age(st.last_event, now)}")
        elif state != "down":
            parts.append("en attente du 1er événement")
        if st.detections:
            parts.append(f"{st.detections} détection(s)")
        lines.append(" · ".join(parts))
        if state != "ok" and st.last_error:
            lines.append(f"   ↳ {esc(st.last_error)} ({fmt_age(st.last_error_at or now, now)})")
    if warnings:
        lines += ["", "<b>Avertissements</b>"] + [f"⚠️ {esc(w)}" for w in warnings]
    lines += ["", "<i>✅ ok · ⚠️ rien reçu depuis 3 min · ❌ déconnecté</i>"]
    rows = [
        [{"text": "🔔 Envoyer une notif de test", "callback_data": f"{CB}tn"}],
        [{"text": "🔄 Actualiser", "callback_data": f"{CB}st"},
         {"text": "⬅️ Projets", "callback_data": f"{CB}m"}],
    ]
    return "\n".join(lines), {"inline_keyboard": rows}
