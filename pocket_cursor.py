"""
PocketCursor — Your Cursor IDE, in your pocket.

Mirrors conversations between Cursor and Telegram in both directions:
  Telegram → Cursor:  messages from your phone are typed into Cursor
  Cursor → Telegram:  AI responses stream back to your phone in real time

Connects to Cursor via Chrome DevTools Protocol (CDP).

Usage: python -X utf8 pocket_cursor.py
"""

import io
import sys

if sys.platform == 'win32' and hasattr(sys.stdout, 'buffer'):
    sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding='utf-8', errors='replace')
    sys.stderr = io.TextIOWrapper(sys.stderr.buffer, encoding='utf-8', errors='replace')

# Standard library
import atexit
import base64
import hashlib
import json
import os
import re
import subprocess as sp
import threading
import time
from datetime import datetime
from pathlib import Path
from typing import Any
from urllib.parse import unquote, urlparse

# Third-party
import requests
import websocket
from openai import OpenAI
from PIL import Image

from chat_detection import install_chat_listener, list_chats, start_chat_listener, ts_print
from lib import command_rules
from lib.observability import init_sentry
from lib.telegram_routes import (
    RouteKey,
    canonical_outbound_route,
    forum_topic_title,
    group_routes_by_mirror,
    load_routes_json,
    migrate_legacy_route_files,
    route_key_from_message,
    routes_for_global_bot_notify,
    save_routes_json,
)

# Sibling modules
from start_cursor import discover_cdp_ports

print = ts_print


# ── Config ───────────────────────────────────────────────────────────────────

env_path = Path(__file__).parent / '.env'
if env_path.exists():
    for line in env_path.read_text().strip().splitlines():
        if '=' in line and not line.startswith('#'):
            key, val = line.split('=', 1)
            os.environ[key.strip()] = val.strip()

TOKEN = os.environ.get('TELEGRAM_BOT_TOKEN')
if not TOKEN:
    print('ERROR: TELEGRAM_BOT_TOKEN not set.')
    sys.exit(1)

init_sentry()

TG_API = f'https://api.telegram.org/bot{TOKEN}'

# OpenAI API for voice transcription (optional)
OPENAI_API_KEY = os.environ.get('OPENAI_API_KEY')
openai_client = OpenAI(api_key=OPENAI_API_KEY) if OPENAI_API_KEY else None
if not OPENAI_API_KEY:
    print("WARNING: OPENAI_API_KEY not set. Voice messages won't be transcribed.")

# Context journal: prefill annotation into chat input when context window is filling up
CONTEXT_MONITOR = os.environ.get('CONTEXT_MONITOR', '').lower() in ('true', '1', 'yes')

# Command rules: auto-accept/deny Cursor tool confirmations based on allow/deny patterns
COMMAND_RULES = os.environ.get('COMMAND_RULES', '').lower() in ('true', '1', 'yes')

# Owner lock: only respond to this Telegram user ID
# Set in .env or auto-captured on first /start command
_owner_raw = os.environ.get('TELEGRAM_OWNER_ID')
OWNER_ID: int | None = int(_owner_raw) if _owner_raw else None
_raw_forum = os.environ.get('TELEGRAM_FORUM_CHAT_ID', '').strip()
FORUM_CHAT_ID: int | None = None
if _raw_forum:
    try:
        FORUM_CHAT_ID = int(_raw_forum)
    except ValueError:
        print(
            'WARNING: TELEGRAM_FORUM_CHAT_ID must be the numeric forum supergroup id (e.g. -100…)'
        )
_unpin_raw = os.environ.get('TELEGRAM_UNPIN_OUTBOUND', 'true').strip().lower()
TELEGRAM_UNPIN_OUTBOUND = _unpin_raw not in ('false', '0', 'no', 'off')
owner_file = Path(__file__).parent / '.owner_id'
chat_id_file = Path(__file__).parent / '.chat_id'
active_chat_file = Path(__file__).parent / '.active_chat'
routes_file = Path(__file__).parent / '.telegram_routes.json'

# Shared state
cdp_lock = threading.Lock()
ws = None  # Active instance's WebSocket (all cdp_* functions use this)
_browser_ws_url = None  # Browser-level WebSocket URL (cached at connect time)
instance_registry: dict[Any, Any] = {}  # {target_id: {workspace, ws, ws_url, title}}
active_instance_id = None  # Which instance ws points to
# Per Telegram route → (instance_id, pc_id, chat_name, msg_fingerprint?) mirrored to Cursor.
# msg_fingerprint = last human message id from DOM (stable across pc_id retags).
MirrorRow = tuple[str, str, str, str | None]
mirrored_chats: dict[RouteKey, MirrorRow] = {}
mirrored_chats_lock = threading.Lock()


def _norm_msg_fp(fp: str | None) -> str | None:
    if fp is None:
        return None
    s = str(fp).strip()
    return s or None


def _normalize_mirror_row(mc: MirrorRow | tuple[str, str, str]) -> MirrorRow:
    """Upgrade legacy 3-tuples to 4-tuples."""
    if len(mc) >= 4:
        return (mc[0], mc[1], mc[2], _norm_msg_fp(mc[3]))
    return (mc[0], mc[1], mc[2], None)


def _mirror_row(iid: str, pc_id: str, name: str, msg_fp: str | None = None) -> MirrorRow:
    return (iid, pc_id, name, _norm_msg_fp(msg_fp))


def _forum_conversation_id_known(msg_fingerprint: str | None) -> bool:
    """True once DOM gave a stable fingerprint (message id, or cid:+composer UUID for empty threads)."""
    return _norm_msg_fp(msg_fingerprint) is not None


def _forum_general_key() -> RouteKey | None:
    if FORUM_CHAT_ID is None:
        return None
    return RouteKey(FORUM_CHAT_ID, None)


def _purge_all_forum_thread_routes_for_cursor_chat(iid: str, pc_id: str) -> int:
    """Remove every forum *thread* mirror for this Cursor composer (not General).

    Used when a topic id is invalid so we do not retry other stale duplicate rows for the
    same ``(instance_id, pc_id)``.
    """
    global last_sender_route
    if FORUM_CHAT_ID is None:
        return 0
    n = 0
    with mirrored_chats_lock:
        for rk in list(mirrored_chats.keys()):
            if rk.chat_id != FORUM_CHAT_ID or rk.message_thread_id is None:
                continue
            mc = mirrored_chats[rk]
            if mc[0] == iid and mc[1] == pc_id:
                del mirrored_chats[rk]
                n += 1
                if last_sender_route == rk:
                    last_sender_route = None
    return n


def _dedupe_forum_thread_mirrors_by_cursor_chat() -> None:
    """Keep one forum thread RouteKey per (instance_id, pc_id); drop duplicate restores."""
    global last_sender_route
    if FORUM_CHAT_ID is None:
        return
    culled = 0
    with mirrored_chats_lock:
        by_comp: dict[tuple[str, str], list[RouteKey]] = {}
        for rk, mc in list(mirrored_chats.items()):
            if rk.chat_id != FORUM_CHAT_ID or rk.message_thread_id is None:
                continue
            if len(mc) < 2:
                continue
            by_comp.setdefault((mc[0], mc[1]), []).append(rk)
        for _key, rks in by_comp.items():
            if len(rks) <= 1:
                continue
            keep = max(rks, key=lambda x: x.message_thread_id or 0)
            for rk in rks:
                if rk != keep:
                    del mirrored_chats[rk]
                    culled += 1
                    if last_sender_route == rk:
                        last_sender_route = None
    if culled:
        print(f'[forum] Deduped {culled} duplicate forum mirror(s) (same instance + pc_id)')
        _persist_mirrored_routes()


def _strip_general_mirror_for_chat_unsafe(iid: str, pc_id: str) -> None:
    """Remove General row if it points at this Cursor chat. Caller must hold ``mirrored_chats_lock``."""
    g = _forum_general_key()
    if g is None:
        return
    gm = mirrored_chats.get(g)
    if gm and gm[0] == iid and gm[1] == pc_id:
        del mirrored_chats[g]


# When a forum topic is deleted, map (forum_chat_id, old_thread_id) → new_thread_id for the same tick.
_forum_thread_remap_lock = threading.Lock()
_forum_thread_remap: dict[tuple[int, int], int] = {}
# Last Telegram route that received a message or callback (DOM chat switch updates this route)
last_sender_route: RouteKey | None = None
# Load chat_id from disk so PC messages work after restart without a Telegram message first
chat_id = int(chat_id_file.read_text().strip()) if chat_id_file.exists() else None
# Persisted route bindings (workspace/pc_id/name) loaded at import; resolved in cdp_connect
if routes_file.exists():
    _route_bindings_initial: dict[RouteKey, dict[str, Any]] = load_routes_json(routes_file)
else:
    _route_bindings_initial = migrate_legacy_route_files(chat_id_file, active_chat_file)
    if _route_bindings_initial:
        try:
            save_routes_json(routes_file, _route_bindings_initial)
        except OSError:
            pass
chat_id_lock = threading.Lock()
muted_file = Path(__file__).parent / '.muted'
muted = muted_file.exists()  # Persisted across restarts
context_pcts_file = Path(__file__).parent / '.context_pcts'
phone_outbox = Path(__file__).parent / '_phone_outbox'
# Note: no reinit_monitor — monitor tracks continuously even while muted,
# just skips Telegram sends. This keeps forwarded_ids in sync at all times.
# Per Telegram route (chat + optional forum topic): last outbound text / message id
last_sent_by_route: dict[RouteKey, str] = {}
last_tg_message_id_by_route: dict[RouteKey, int] = {}
last_sent_lock = threading.Lock()
last_tg_message_id = None  # legacy: last route’s message id (reactions)
pending_confirms: dict[Any, Any] = {}  # {short_cb_key: {...}} for inline keyboards
pending_confirms_lock = threading.Lock()


def _confirm_callback_key(tool_id: str) -> str:
    """Stable short key for Telegram callback_data (max 64 bytes total per button)."""
    h = hashlib.sha256(str(tool_id).encode('utf-8')).hexdigest()[:16]
    return h


def _is_forum_general_route(rk: RouteKey | None) -> bool:
    """Telegram forum \"General\" is ``message_thread_id is None`` for the forum supergroup."""
    if rk is None or FORUM_CHAT_ID is None:
        return False
    return rk.chat_id == FORUM_CHAT_ID and rk.message_thread_id is None


def _primary_route() -> RouteKey | None:
    """Default route without a forum thread (DM or plain supergroup).

    When ``TELEGRAM_FORUM_CHAT_ID`` equals the stored ``.chat_id``, that row would be
    forum General — we return None so callers use real topic routes instead.
    """
    if chat_id is None:
        return None
    if FORUM_CHAT_ID is not None and chat_id == FORUM_CHAT_ID:
        return None
    return RouteKey(chat_id, None)


def _preferred_telegram_route() -> RouteKey | None:
    """Where to send Telegram lines not tied to a specific inbound message.

    Prefers ``last_sender_route`` / canonical forum topic; avoids forum General whenever
    a threaded forum route exists in ``mirrored_chats``.
    """
    if FORUM_CHAT_ID is None:
        return last_sender_route or _primary_route()
    if last_sender_route and _is_forum_general_route(last_sender_route):
        return last_sender_route

    with mirrored_chats_lock:
        forum_routes = [rk for rk in mirrored_chats if rk.chat_id == FORUM_CHAT_ID]
    if forum_routes:
        ls = last_sender_route if last_sender_route in forum_routes else None
        return canonical_outbound_route(
            forum_routes,
            forum_chat_id=FORUM_CHAT_ID,
            last_sender=ls,
        )

    if last_sender_route and not _is_forum_general_route(last_sender_route):
        return last_sender_route
    return _primary_route()


def _prune_forum_general_route_if_redundant() -> None:
    """Keep ``RouteKey(forum, None)`` — aggressive routing uses General until a fingerprint exists."""
    return


def _mirror_for_inbound(route: RouteKey) -> MirrorRow | None:
    """Resolve Cursor mirror for a Telegram route (exact RouteKey only).

    We intentionally do **not** fall back to ``RouteKey(chat_id, None)`` when the message has
    a forum ``message_thread_id``. That fallback would map every topic to the same Cursor chat
    as the General / legacy row and break per-topic routing.
    """
    with mirrored_chats_lock:
        return mirrored_chats.get(route)


_FORUM_TOPIC_UNBOUND = (
    'This forum topic is not linked to a Cursor chat yet. '
    'Focus that agent tab in Cursor so the bridge can bind it, then send again.'
)


def _forum_topic_requires_mirror(route: RouteKey) -> bool:
    """True if we must have a per-topic binding (no silent fallback to the active Cursor tab)."""
    if FORUM_CHAT_ID is None or route.message_thread_id is None:
        return False
    if route.chat_id != FORUM_CHAT_ID:
        return False
    with mirrored_chats_lock:
        return route not in mirrored_chats


def _persist_mirrored_routes() -> None:
    """Persist mirrored_chats to `.telegram_routes.json`."""
    try:
        bind: dict[RouteKey, dict[str, Any]] = {}
        with mirrored_chats_lock:
            for rk, mc in mirrored_chats.items():
                iid, pc_id, name, msg_fp = _normalize_mirror_row(mc)
                info = instance_registry.get(iid, {})
                row: dict[str, Any] = {
                    'workspace': info.get('workspace'),
                    'pc_id': pc_id,
                    'chat_name': name,
                }
                if msg_fp:
                    row['msg_id'] = msg_fp
                bind[rk] = row
        save_routes_json(routes_file, bind)
    except OSError:
        pass


def _find_forum_route_for_pc(
    forum_cid: int,
    iid: str,
    pc_id: str,
    msg_fingerprint: str | None = None,
) -> RouteKey | None:
    """Return the Telegram route for this Cursor chat in the forum, if any.

    Matching is **id-only** (no chat titles): ``(instance_id, pc_id)``, cross-window same
    ``pc_id``, or unique stored conversation fingerprint. Never title match.
    """
    fp = _norm_msg_fp(msg_fingerprint)
    with mirrored_chats_lock:
        fallback: RouteKey | None = None
        for rk, mc in mirrored_chats.items():
            if rk.chat_id == forum_cid and rk.message_thread_id is not None and mc[1] == pc_id:
                if mc[0] == iid:
                    return rk
                fallback = rk
        if fallback is not None:
            return fallback
        if fp:
            msg_hits: list[RouteKey] = []
            for rk, mc in mirrored_chats.items():
                m = _normalize_mirror_row(mc)
                if (
                    rk.chat_id == forum_cid
                    and rk.message_thread_id is not None
                    and m[0] == iid
                    and m[3] == fp
                ):
                    msg_hits.append(rk)
            if len(msg_hits) == 1:
                return msg_hits[0]
        return None


def _migrate_mirrored_pc_id(iid: str, old_pc: str, new_pc: str, name: str) -> None:
    """When Cursor replaces a conversation id (overview fingerprint merge), keep one mirror row.

    Without this, ``_ensure_forum_topic_for_cursor_chat`` sees a new ``pc_id``, does not find the
    existing forum route, and creates a second Telegram topic for the same chat.
    """
    if old_pc == new_pc:
        return
    changed = False
    nm_out = ''
    with mirrored_chats_lock:
        for rk, mc in list(mirrored_chats.items()):
            if mc[0] == iid and mc[1] == old_pc:
                nm_out = name.strip() if name else mc[2]
                m = _normalize_mirror_row(mc)
                mirrored_chats[rk] = _mirror_row(iid, new_pc, nm_out, m[3])
                changed = True
    if changed:
        _persist_mirrored_routes()
        print(f'[forum] Migrated mirror pc_id {old_pc[:20]}… → {new_pc[:20]}…  ({nm_out!r})')


def tg_create_forum_topic(forum_chat_id: int, name: str) -> int | None:
    """Create a forum topic; returns message_thread_id or None."""
    result = tg_call('createForumTopic', chat_id=forum_chat_id, name=name[:128])
    if not result.get('ok'):
        return None
    r = result.get('result') or {}
    tid = r.get('message_thread_id')
    return int(tid) if tid is not None else None


def _ensure_forum_topic_for_cursor_chat(
    iid: str, pc_id: str, name: str, msg_fingerprint: str | None = None
) -> RouteKey | None:
    """Bind this Cursor chat to a forum topic, General, or an existing thread (id-based only).

    Skips provisional tabs (pc-*). Without a conversation fingerprint we only route to
    **General**; once ``list_chats`` / the DOM provides a fingerprint we create (or match)
    a dedicated topic and rebind. Never uses chat title for matching.
    """
    global last_sender_route
    if FORUM_CHAT_ID is None:
        return None
    if pc_id.startswith('pc-'):
        return None
    title = forum_topic_title(name, pc_id)
    existing = _find_forum_route_for_pc(FORUM_CHAT_ID, iid, pc_id, msg_fingerprint)
    fp = _norm_msg_fp(msg_fingerprint)
    if existing:
        with mirrored_chats_lock:
            prev = mirrored_chats.get(existing)
            pm = _normalize_mirror_row(prev) if prev else None
            merged_fp = fp or (pm[3] if pm else None)
            row = _mirror_row(iid, pc_id, name, merged_fp)
            if pm != row:
                mirrored_chats[existing] = row
            last_sender_route = existing
            _strip_general_mirror_for_chat_unsafe(iid, pc_id)
        _persist_mirrored_routes()
        if pm and pm[2] != name:
            tg_call(
                'editForumTopic',
                chat_id=FORUM_CHAT_ID,
                message_thread_id=existing.message_thread_id,
                name=title,
            )
        last_sender_route = existing
        return existing

    if not _forum_conversation_id_known(msg_fingerprint):
        g = _forum_general_key()
        if g is None:
            return None
        with mirrored_chats_lock:
            mirrored_chats[g] = _mirror_row(iid, pc_id, name, None)
        last_sender_route = g
        _persist_mirrored_routes()
        print(
            f'[forum] No conversation fingerprint yet for {name!r} ({pc_id[:12]}…) — using General'
        )
        return g

    tid = tg_create_forum_topic(FORUM_CHAT_ID, title)
    if tid is None:
        print(
            '[forum] createForumTopic failed — is the bot an admin with "Manage topics" in the forum?'
        )
        g = _forum_general_key()
        if g is not None:
            with mirrored_chats_lock:
                mirrored_chats[g] = _mirror_row(iid, pc_id, name, fp)
            last_sender_route = g
            _persist_mirrored_routes()
        return g
    rk = RouteKey(FORUM_CHAT_ID, tid)
    with mirrored_chats_lock:
        mirrored_chats[rk] = _mirror_row(iid, pc_id, name, fp)
        _strip_general_mirror_for_chat_unsafe(iid, pc_id)
    last_sender_route = rk
    _persist_mirrored_routes()
    print(f'[forum] Topic for {name!r} → thread_id={tid}')
    return rk


# ── Telegram helpers ─────────────────────────────────────────────────────────


def _forum_resolve_thread_id(cid: int | None, message_thread_id: int | None) -> int | None:
    """Apply remap after a deleted topic was replaced in-process (same monitor tick)."""
    if cid is None or message_thread_id is None or FORUM_CHAT_ID is None:
        return message_thread_id
    if cid != FORUM_CHAT_ID:
        return message_thread_id
    with _forum_thread_remap_lock:
        return _forum_thread_remap.get((cid, message_thread_id), message_thread_id)


def _recover_stale_forum_topic_after_failed_send(
    result: dict[str, Any],
    cid: int,
    tid_used: int | None,
) -> int | None:
    """Topic was deleted in Telegram: drop route, recreate topic, return new thread_id if any."""
    if result.get('ok') or tid_used is None:
        return None
    desc = str(result.get('description', '')).lower()
    if 'message thread not found' not in desc and 'thread not found' not in desc:
        return None
    if FORUM_CHAT_ID is None or cid != FORUM_CHAT_ID:
        return None

    rk = RouteKey(cid, tid_used)
    with mirrored_chats_lock:
        mc = mirrored_chats.pop(rk, None)
    if not mc:
        with _forum_thread_remap_lock:
            mapped = _forum_thread_remap.get((cid, tid_used))
        return mapped

    m = _normalize_mirror_row(mc)
    iid, pc_id, name, msg_fp = m
    extra = _purge_all_forum_thread_routes_for_cursor_chat(iid, pc_id)
    if extra:
        print(f'[forum] Cleared {extra} other stale forum row(s) for same Cursor chat')

    _persist_mirrored_routes()
    global last_sender_route
    if last_sender_route == rk:
        last_sender_route = None

    print(f'[forum] Topic deleted (thread {tid_used}); recreating for {name!r}  ({pc_id[:16]}…)')
    new_rk = _ensure_forum_topic_for_cursor_chat(iid, pc_id, name, msg_fp)
    new_tid = new_rk.message_thread_id if new_rk else None
    if new_tid is not None:
        with _forum_thread_remap_lock:
            _forum_thread_remap[(cid, tid_used)] = new_tid
            if len(_forum_thread_remap) > 128:
                for k in list(_forum_thread_remap)[:32]:
                    _forum_thread_remap.pop(k, None)
    return new_tid


def tg_call(method, **params):
    resp = requests.post(f'{TG_API}/{method}', json=params, timeout=60)
    result = resp.json()
    if not result.get('ok'):
        desc = result.get('description', '?')
        code = result.get('error_code', '?')
        print(f'[telegram] API error: {method} -> {code} {desc}')
    return result


def _tg_maybe_unpin_outbound(cid: int | None, send_result: dict[str, Any] | None) -> None:
    """Call unpinChatMessage for messages we just sent (forums may auto-pin bot posts)."""
    if not TELEGRAM_UNPIN_OUTBOUND or cid is None or not send_result or not send_result.get('ok'):
        return
    msg = send_result.get('result')
    if not isinstance(msg, dict):
        return
    mid = msg.get('message_id')
    if mid is None:
        return
    # If the message was not pinned, Telegram returns an error — do not use tg_call (avoids log spam).
    try:
        requests.post(
            f'{TG_API}/unpinChatMessage',
            json={'chat_id': cid, 'message_id': mid},
            timeout=60,
        )
    except OSError:
        pass


def tg_typing(cid, message_thread_id=None):
    """Show 'typing...' indicator."""
    tid = _forum_resolve_thread_id(cid, message_thread_id)
    params: dict[str, Any] = {'chat_id': cid, 'action': 'typing'}
    if tid is not None:
        params['message_thread_id'] = tid
    r = tg_call('sendChatAction', **params)
    if not r.get('ok'):
        n = _recover_stale_forum_topic_after_failed_send(r, cid, tid)
        if n is not None:
            params['message_thread_id'] = n
            r = tg_call('sendChatAction', **params)
    return r


def tg_send(cid, text, message_thread_id=None):
    if not cid:
        return
    tid = _forum_resolve_thread_id(cid, message_thread_id)
    base: dict[str, Any] = {'chat_id': cid}
    if tid is not None:
        base['message_thread_id'] = tid
    if len(text) <= 4000:
        r = tg_call('sendMessage', text=text, **base)
        if not r.get('ok'):
            n = _recover_stale_forum_topic_after_failed_send(r, cid, tid)
            if n is not None:
                base['message_thread_id'] = n
                r = tg_call('sendMessage', text=text, **base)
        _tg_maybe_unpin_outbound(cid, r)
        return r
    # Split long messages at line breaks
    chunks = []
    while len(text) > 4000:
        split_at = text.rfind('\n', 0, 4000)
        if split_at < 1000:
            split_at = 4000
        chunks.append(text[:split_at])
        text = text[split_at:].lstrip('\n')
    if text:
        chunks.append(text)
    for chunk in chunks:
        tid = _forum_resolve_thread_id(cid, message_thread_id)
        base = {'chat_id': cid}
        if tid is not None:
            base['message_thread_id'] = tid
        r = tg_call('sendMessage', text=chunk, **base)
        if not r.get('ok'):
            n = _recover_stale_forum_topic_after_failed_send(r, cid, tid)
            if n is not None:
                base['message_thread_id'] = n
                r = tg_call('sendMessage', text=chunk, **base)
        _tg_maybe_unpin_outbound(cid, r)
        time.sleep(0.3)


def _tg_send_message_forum_aware(payload: dict[str, Any]) -> dict[str, Any]:
    """sendMessage with resolve + recreate when the forum topic was deleted."""
    cid = payload['chat_id']
    raw_tid = payload.get('message_thread_id')
    tid = _forum_resolve_thread_id(cid, raw_tid)
    pl = dict(payload)
    if tid is not None:
        pl['message_thread_id'] = tid
    elif 'message_thread_id' in pl:
        pl.pop('message_thread_id')
    r = tg_call('sendMessage', **pl)
    if not r.get('ok'):
        n = _recover_stale_forum_topic_after_failed_send(r, cid, tid)
        if n is not None:
            pl = dict(payload)
            pl['message_thread_id'] = n
            r = tg_call('sendMessage', **pl)
    return r


def tg_escape_markdown_v2(text):
    """Escape special characters for Telegram MarkdownV2 parse mode."""
    special = r'_*[]()~`>#+-=|{}.!'
    return ''.join('\\' + ch if ch in special else ch for ch in text)


def tg_send_thinking(cid, text, message_thread_id=None):
    """Send thinking text to Telegram in italic with 💭 prefix.
    Tries MarkdownV2 italic first, falls back to plain text if formatting fails.
    """
    if not cid or not text:
        return
    tid = _forum_resolve_thread_id(cid, message_thread_id)
    base: dict[str, Any] = {'chat_id': cid}
    if tid is not None:
        base['message_thread_id'] = tid
    # Truncate if very long (thinking can be verbose)
    if len(text) > 3500:
        cut = text[:3500].rfind('\n')
        if cut < 1000:
            cut = 3500
        text = text[:cut] + '...'
    # Try MarkdownV2 italic first
    try:
        escaped = tg_escape_markdown_v2(text)
        msg = f'_💭 {escaped}_'
        result = tg_call('sendMessage', text=msg, parse_mode='MarkdownV2', **base)
        if not result.get('ok'):
            n = _recover_stale_forum_topic_after_failed_send(result, cid, tid)
            if n is not None:
                base['message_thread_id'] = n
                result = tg_call('sendMessage', text=msg, parse_mode='MarkdownV2', **base)
        if result.get('ok'):
            _tg_maybe_unpin_outbound(cid, result)
            return result
        print(
            f'[telegram] MarkdownV2 failed: {result.get("description", "?")}, falling back to plain text'
        )
    except Exception as e:
        print(f'[telegram] MarkdownV2 error: {e}, falling back to plain text')
    # Fallback: plain text with prefix
    r = tg_call('sendMessage', text=f'💭 {text}', **base)
    if not r.get('ok'):
        n = _recover_stale_forum_topic_after_failed_send(r, cid, tid)
        if n is not None:
            base['message_thread_id'] = n
            r = tg_call('sendMessage', text=f'💭 {text}', **base)
    _tg_maybe_unpin_outbound(cid, r)
    return r


def tg_send_photo(cid, photo_path, caption=None, message_thread_id=None):
    """Send a photo to Telegram. photo_path is a local file path."""
    if not cid or not photo_path:
        return
    try:
        tid = _forum_resolve_thread_id(cid, message_thread_id)
        with open(photo_path, 'rb') as f:
            data: dict[str, Any] = {'chat_id': cid}
            if tid is not None:
                data['message_thread_id'] = tid
            if caption:
                data['caption'] = caption[:1024]  # Telegram caption limit
            resp = requests.post(f'{TG_API}/sendPhoto', data=data, files={'photo': f}, timeout=30)
            result = resp.json()
            if not result.get('ok'):
                n = _recover_stale_forum_topic_after_failed_send(result, cid, tid)
                if n is not None:
                    f.seek(0)
                    data = {'chat_id': cid, 'message_thread_id': n}
                    if caption:
                        data['caption'] = caption[:1024]
                    resp = requests.post(
                        f'{TG_API}/sendPhoto', data=data, files={'photo': f}, timeout=30
                    )
                    result = resp.json()
                if not result.get('ok'):
                    desc = result.get('description', '?')
                    code = result.get('error_code', '?')
                    print(f'[telegram] sendPhoto failed: {code} {desc}  ({photo_path})')
            if result.get('ok'):
                _tg_maybe_unpin_outbound(cid, result)
            return result
    except Exception as e:
        print(f'[telegram] sendPhoto error: {e}')
        return None


def tg_send_photo_bytes(
    cid, photo_bytes, filename='screenshot.png', caption=None, message_thread_id=None
):
    """Send photo from bytes (e.g. CDP screenshot)."""
    if not cid or not photo_bytes:
        return
    try:
        tid = _forum_resolve_thread_id(cid, message_thread_id)
        data: dict[str, Any] = {'chat_id': cid}
        if tid is not None:
            data['message_thread_id'] = tid
        if caption:
            data['caption'] = caption[:1024]
        resp = requests.post(
            f'{TG_API}/sendPhoto',
            data=data,
            files={'photo': (filename, photo_bytes, 'image/png')},
            timeout=30,
        )
        result = resp.json()
        if not result.get('ok'):
            n = _recover_stale_forum_topic_after_failed_send(result, cid, tid)
            if n is not None:
                data = {'chat_id': cid, 'message_thread_id': n}
                if caption:
                    data['caption'] = caption[:1024]
                resp = requests.post(
                    f'{TG_API}/sendPhoto',
                    data=data,
                    files={'photo': (filename, photo_bytes, 'image/png')},
                    timeout=30,
                )
                result = resp.json()
            if not result.get('ok'):
                desc = result.get('description', '?')
                code = result.get('error_code', '?')
                print(f'[telegram] sendPhoto failed: {code} {desc}  ({len(photo_bytes)} bytes)')
        if result.get('ok'):
            _tg_maybe_unpin_outbound(cid, result)
        return result
    except Exception as e:
        print(f'[telegram] sendPhoto bytes error: {e}')
        return None


def tg_send_photo_bytes_with_keyboard(
    cid, photo_bytes, keyboard, filename='screenshot.png', caption=None, message_thread_id=None
):
    """Send photo with inline keyboard buttons."""
    if not cid or not photo_bytes:
        return None
    try:
        tid = _forum_resolve_thread_id(cid, message_thread_id)
        data: dict[str, Any] = {'chat_id': cid}
        if tid is not None:
            data['message_thread_id'] = tid
        if caption:
            data['caption'] = caption[:1024]
        data['reply_markup'] = json.dumps({'inline_keyboard': keyboard})
        resp = requests.post(
            f'{TG_API}/sendPhoto',
            data=data,
            files={'photo': (filename, photo_bytes, 'image/png')},
            timeout=30,
        )
        result = resp.json()
        if not result.get('ok'):
            n = _recover_stale_forum_topic_after_failed_send(result, cid, tid)
            if n is not None:
                data = {
                    'chat_id': cid,
                    'message_thread_id': n,
                    'reply_markup': json.dumps({'inline_keyboard': keyboard}),
                }
                if caption:
                    data['caption'] = caption[:1024]
                resp = requests.post(
                    f'{TG_API}/sendPhoto',
                    data=data,
                    files={'photo': (filename, photo_bytes, 'image/png')},
                    timeout=30,
                )
                result = resp.json()
            if not result.get('ok'):
                desc = result.get('description', '?')
                code = result.get('error_code', '?')
                print(
                    f'[telegram] sendPhoto+keyboard failed: {code} {desc}  ({len(photo_bytes)} bytes)'
                )
        if result.get('ok'):
            _tg_maybe_unpin_outbound(cid, result)
        return result
    except Exception as e:
        print(f'[telegram] sendPhoto+keyboard error: {e}')
        return None


POCKET_CURSOR_COMMANDS = [
    {'command': 'newchat', 'description': 'Start a new chat in Cursor'},
    {'command': 'chats', 'description': 'Show all chats across instances'},
    {'command': 'pause', 'description': 'Pause Cursor to Telegram forwarding'},
    {'command': 'play', 'description': 'Resume forwarding'},
    {'command': 'screenshot', 'description': 'Screenshot your Cursor window'},
    {'command': 'unpair', 'description': 'Disconnect this device'},
]


def tg_commands_need_update():
    """Check if bot commands are missing or outdated compared to POCKET_CURSOR_COMMANDS."""
    try:
        existing = tg_call('getMyCommands')
        current = existing.get('result', []) if existing.get('ok') else []
        registered = {c['command']: c['description'] for c in current}
        for cmd in POCKET_CURSOR_COMMANDS:
            if cmd['command'] not in registered:
                return True
            if registered[cmd['command']] != cmd['description']:
                return True
        return False
    except Exception:
        return False


def tg_register_commands():
    """Merge PocketCursor commands into existing bot commands (doesn't overwrite others)."""
    try:
        existing = tg_call('getMyCommands')
        current = existing.get('result', []) if existing.get('ok') else []
        our_names = {c['command'] for c in POCKET_CURSOR_COMMANDS}
        merged = [c for c in current if c['command'] not in our_names]
        merged.extend(POCKET_CURSOR_COMMANDS)
        result = tg_call('setMyCommands', commands=merged)
        ok = result.get('ok', False)
        print(
            f'[telegram] Registered {len(POCKET_CURSOR_COMMANDS)} commands (total {len(merged)}): {"OK" if ok else result}'
        )
        return ok
    except Exception as e:
        print(f'[telegram] Failed to register commands: {e}')
        return False


def tg_ask_command_update(cid):
    """Send an inline keyboard asking the user to update bot commands."""
    r = tg_call(
        'sendMessage',
        chat_id=cid,
        text='New commands available. Want me to update your Telegram bot menu?',
        reply_markup={
            'inline_keyboard': [
                [
                    {'text': '✅ Yes, update', 'callback_data': 'setup_commands:yes'},
                    {'text': 'Skip', 'callback_data': 'setup_commands:no'},
                ]
            ]
        },
    )
    _tg_maybe_unpin_outbound(cid, r)


def vscode_url_to_path(url):
    """Convert vscode-file://vscode-app/c%3A/Users/... to a local file path."""
    if not url or not url.startswith('vscode-file://'):
        return None
    # Strip protocol and host: vscode-file://vscode-app/c%3A/...
    parsed = urlparse(url)
    path = unquote(parsed.path)  # decode %3A -> :
    # Remove leading / on Windows (e.g. /c:/Users -> c:/Users)
    if len(path) > 2 and path[0] == '/' and path[2] == ':':
        path = path[1:]
    # Strip query string (?t=timestamp)
    return path.split('?')[0] if '?' in path else path


def transcribe_voice(audio_bytes, filename='voice.ogg'):
    """Transcribe audio using OpenAI gpt-4o-transcribe. Returns text or None."""
    if not openai_client:
        return None
    try:
        result = openai_client.audio.transcriptions.create(
            model='gpt-4o-transcribe',
            file=(filename, audio_bytes),
        )
        return result.text
    except Exception as e:
        print(f'[transcribe] Error: {e}')
        return None


# ── CDP helpers ──────────────────────────────────────────────────────────────


def detect_cdp_port(exit_on_fail=True):
    """Auto-detect the CDP port from running Cursor processes.

    Uses start_cursor.discover_cdp_ports() (WMIC + PowerShell command lines on
    Windows, then localhost /json scan if needed). Verifies each candidate
    responds. On Windows, merged windows can leave ghost --remote-debugging-port
    entries in the launcher process even though only the original port is bound.

    When exit_on_fail=False (used by background threads), returns None
    instead of calling sys.exit() so the caller can retry next cycle.
    """
    ports, probed = discover_cdp_ports()
    if not ports:
        if exit_on_fail:
            print('ERROR: No Cursor process with CDP detected.')
            print('Start Cursor with CDP first:  python start_cursor.py')
            print('Or check status:              python start_cursor.py --check')
            sys.exit(1)
        return None
    if probed:
        print(
            '[cdp] CDP port from localhost scan (command-line enumeration had no '
            'flags — e.g. WMIC unavailable on newer Windows).'
        )
    for port in ports:
        try:
            resp = requests.get(f'http://localhost:{port}/json', timeout=2)
            if resp.status_code == 200:
                return port
        except Exception:
            pass
    if exit_on_fail:
        print('ERROR: Cursor process found but no CDP port is responding.')
        print(f'Ports in command line: {ports}')
        print('Start Cursor with CDP first:  python start_cursor.py')
        sys.exit(1)
    return None


def parse_instance_title(title):
    """Extract workspace name from a Cursor instance title.

    Title patterns:
        "Cursor"                                              → no workspace
        "file.py - WorkspaceName - Cursor"                    → "WorkspaceName"
        "file.md - Name (Workspace) - Cursor"                 → "Name (Workspace)"
        "Interactive - file.py - WorkspaceName - Cursor"      → "WorkspaceName"

    Workspace is always the second-to-last segment before "- Cursor".
    """
    parts = title.split(' - ')
    if len(parts) >= 3 and parts[-1].strip() == 'Cursor':
        return parts[-2]
    return None


def cdp_list_instances(port=None):
    """List all Cursor instances on the CDP port.

    Returns list of dicts: {id, title, workspace, ws_url}
    Instances without a workspace (e.g. "select workspace" screen) get workspace=None.
    """
    if port is None:
        port = detect_cdp_port()
    targets = requests.get(f'http://localhost:{port}/json').json()
    instances = []
    for t in targets:
        if t['type'] != 'page':
            continue
        if t.get('url', '').startswith('devtools://'):
            continue
        instances.append(
            {
                'id': t['id'],
                'title': t.get('title', ''),
                'workspace': parse_instance_title(t.get('title', '')),
                'ws_url': t['webSocketDebuggerUrl'],
            }
        )
    return instances


# ── Chat listener callbacks ───────────────────────────────────────────────────

_switch_lock = threading.Lock()
_switch_debounce_lock = threading.Lock()
_switch_debounce_timer = None
_switch_debounce_pre_pcid = None
_last_chat_activated_mono = 0.0
_CHAT_ACTIVATED_COOLDOWN_S = 25.0


def _tg_notify_all_routes(text: str) -> None:
    """Send a system line (workspace / scan digest): one forum topic + DMs, not every topic."""
    if muted:
        return
    with mirrored_chats_lock:
        rks = list(mirrored_chats.keys())
    if not rks:
        if chat_id:
            # Never post without thread into a forum supergroup (that is General).
            if FORUM_CHAT_ID is not None and chat_id == FORUM_CHAT_ID:
                pass
            else:
                tg_send(chat_id, text)
        return
    rks = routes_for_global_bot_notify(
        rks, forum_chat_id=FORUM_CHAT_ID, last_sender=last_sender_route
    )
    for rk in rks:
        tg_send(rk.chat_id, text, message_thread_id=rk.message_thread_id)


def _send_chat_activated_telegram(text: str) -> None:
    """At most one 'Chat activated' every COOLDOWN seconds (any text) — DOM listeners can race."""
    global _last_chat_activated_mono
    r = _preferred_telegram_route()
    if not r or muted:
        return
    now = time.monotonic()
    if now - _last_chat_activated_mono < _CHAT_ACTIVATED_COOLDOWN_S:
        return
    _last_chat_activated_mono = now
    try:
        tg_send(r.chat_id, text, message_thread_id=r.message_thread_id)
    except Exception:
        pass


def _handle_chat_switch(iid, data):
    """Called by chat listener thread when user switches to a different chat.

    Debounces Telegram notifications (1.5s) to suppress rapid focus bounces
    that happen when Cursor moves a chat between sidebar and editor views.
    If the focus returns to the original chat (A->B->A), no notification is sent.

    When TELEGRAM_FORUM_CHAT_ID is set, creates or reuses a forum topic per Cursor chat.
    """
    global mirrored_chats, active_instance_id, ws, last_sender_route
    global _switch_debounce_timer, _switch_debounce_pre_pcid
    pc_id = data.get('pc_id', '')
    name = data.get('name', '')
    msg_fp = _norm_msg_fp(data.get('msg_id'))
    if not pc_id:
        return

    prev_rk = last_sender_route
    prev_mc = mirrored_chats.get(prev_rk) if prev_rk else None
    cur_pc_id_prev = prev_mc[1] if prev_mc else None
    cur_iid_prev = prev_mc[0] if prev_mc else None

    if FORUM_CHAT_ID is not None:
        if pc_id.startswith('pc-'):
            return
        rk = _ensure_forum_topic_for_cursor_chat(iid, pc_id, name, msg_fp)
        if rk is None:
            return
        same_chat_new_window = pc_id == cur_pc_id_prev and iid != cur_iid_prev
    else:
        with _switch_lock:
            rk = last_sender_route
            if rk is None and chat_id is not None:
                rk = RouteKey(chat_id, None)
            cur_mc = mirrored_chats.get(rk) if rk else None
            cur_iid = cur_mc[0] if cur_mc else None
            cur_pc_id = cur_mc[1] if cur_mc else None
            if iid == cur_iid and pc_id == cur_pc_id:
                return
            same_chat_new_window = pc_id == cur_pc_id and iid != cur_iid
            if rk is not None:
                curm = mirrored_chats.get(rk)
                mp = _normalize_mirror_row(curm) if curm else None
                merged = _norm_msg_fp(msg_fp) or (mp[3] if mp else None)
                mirrored_chats[rk] = _mirror_row(iid, pc_id, name, merged)
    if iid != active_instance_id:
        with cdp_lock:
            active_instance_id = iid
            if iid in instance_registry:
                ws = instance_registry[iid]['ws']
    info = instance_registry.get(iid, {})
    ws_label = (info.get('workspace') or '?').removesuffix(' (Workspace)')
    is_provisional = pc_id.startswith('pc-')
    print(f'[dom] Active: {name}  in {ws_label}' + (' (provisional)' if is_provisional else ''))
    # Seed the overview's known_convs so it can detect renames.
    # Without this, a new chat created and auto-renamed before the next
    # overview scan would never be seen under its original name ("New Chat").
    if not is_provisional and 'convs' in info:
        if pc_id not in info['convs']:
            info['convs'][pc_id] = {'name': name, 'active': True, 'msg_id': msg_fp}
        else:
            info['convs'][pc_id]['name'] = name
            info['convs'][pc_id]['active'] = True
            if msg_fp:
                info['convs'][pc_id]['msg_id'] = msg_fp
    if chat_id and not muted and not is_provisional and not same_chat_new_window:
        with _switch_debounce_lock:
            if _switch_debounce_timer:
                _switch_debounce_timer.cancel()
            else:
                _switch_debounce_pre_pcid = cur_pc_id_prev

            def _fire(n=name, wsl=ws_label, pid=pc_id):
                global _switch_debounce_timer, _switch_debounce_pre_pcid
                with _switch_debounce_lock:
                    _switch_debounce_timer = None
                    pre = _switch_debounce_pre_pcid
                    _switch_debounce_pre_pcid = None
                if pre == pid:
                    print(f'[dom] Suppressed notification (returned to same chat: {n})')
                    return
                _send_chat_activated_telegram(f'💬 Chat activated: {n}  ({wsl})')

            _switch_debounce_timer = threading.Timer(1.5, _fire)
            _switch_debounce_timer.start()
    if FORUM_CHAT_ID is None:
        _persist_mirrored_routes()
    if CONTEXT_MONITOR:
        try:
            cursor_clear_input()
        except Exception:
            pass


def _handle_chat_rename(iid, data):
    """Called by chat listener thread when active chat's name changes."""
    global mirrored_chats
    pc_id = data.get('pc_id', '')
    name = data.get('name', '')
    if not pc_id or not name:
        return
    with mirrored_chats_lock:
        for rk, mc in list(mirrored_chats.items()):
            if mc[0] == iid and mc[1] == pc_id:
                m = _normalize_mirror_row(mc)
                mirrored_chats[rk] = _mirror_row(iid, pc_id, name, m[3])
    _persist_mirrored_routes()
    if FORUM_CHAT_ID is not None:
        ex = _find_forum_route_for_pc(FORUM_CHAT_ID, iid, pc_id, None)
        if ex and ex.message_thread_id is not None:
            tg_call(
                'editForumTopic',
                chat_id=FORUM_CHAT_ID,
                message_thread_id=ex.message_thread_id,
                name=forum_topic_title(name, pc_id),
            )
    if CONTEXT_MONITOR and pc_id in _context_pct_names:
        _context_pct_names[pc_id] = name
        _save_context_pcts(pc_id=pc_id, chat_name=name)


def _on_listener_dead(label, exc):
    """Called when a chat listener thread dies. Flags the instance for reconnect."""
    for iid, info in instance_registry.items():
        ws_label = info.get('workspace') or '(no workspace)'
        if ws_label == label or label == ws_label:
            info['listener_dead'] = True
            print(f'[overview] Listener dead for {label}, will reconnect on next scan')
            return


def _setup_chat_listener(iid, ws_url, label):
    """Open a dedicated listener WebSocket and start the chat listener thread."""
    listener_conn = websocket.create_connection(ws_url)
    install_chat_listener(listener_conn)
    start_chat_listener(
        listener_conn,
        label,
        on_switch=lambda data: _handle_chat_switch(iid, data),
        on_rename=lambda data: _handle_chat_rename(iid, data),
        on_dead=_on_listener_dead,
    )
    return listener_conn


# ── Context Journal Monitor ──────────────────────────────────────────────────
# Reads the context window fill level from the SVG token ring in Cursor's DOM.
# The monitor thread sends a follow-up annotation message when the threshold
# is crossed or a summary is detected.

_CONTEXT_PCT_JS = """
(function() {
    var c = document.querySelector('.token-ring-progress');
    if (!c) return null;
    var total = parseFloat(c.getAttribute('stroke-dasharray'));
    var off = parseFloat(c.getAttribute('stroke-dashoffset'));
    if (!total || isNaN(off)) return null;
    return Math.round((1 - off / total) * 1000) / 10;
})()
"""


def get_context_pct(conn=None):
    """Read the context window fill % from the active Cursor instance."""
    try:
        result = cdp_eval_on(conn, _CONTEXT_PCT_JS) if conn else cdp_eval(_CONTEXT_PCT_JS)
        return float(result) if result is not None else None
    except (TypeError, ValueError):
        return None


def _build_context_annotation(ctx, pc_id):
    """Build the annotation string, or None if no annotation needed."""
    if ctx is None:
        return None
    prev = _context_pcts.get(pc_id)
    hint = '(see pocket-cursor.mdc § Context monitor)'
    if prev is not None and prev - ctx > 5:
        return (
            f'[ContextMonitor: context was summarized '
            f'({int(prev)}% -> {int(ctx)}%) -- check your journal {hint}]'
        )
    if ctx >= CONTEXT_MONITOR_THRESHOLD:
        return f'[ContextMonitor: {int(ctx)}% context used -- journal reminder {hint}]'
    return None


def cdp_connect():
    """Connect to all Cursor instances. Restores per-route mirrors from `.telegram_routes.json` (or legacy `.active_chat`), or defaults."""
    global ws, instance_registry, active_instance_id, mirrored_chats, _browser_ws_url
    port = detect_cdp_port()
    print(f'[cdp] Using port {port}')
    try:
        binfo = requests.get(f'http://localhost:{port}/json/version', timeout=3).json()
        _browser_ws_url = binfo.get('webSocketDebuggerUrl')
        print(f'[cdp] Browser WS: {_browser_ws_url}')
    except Exception:
        _browser_ws_url = None
    instances = cdp_list_instances(port)

    if not instances:
        print('ERROR: No Cursor instances found on CDP port.')
        sys.exit(1)

    instance_registry.clear()
    for w in instances:
        label = w['workspace'] or '(no workspace)'
        try:
            conn = websocket.create_connection(w['ws_url'])
            listener_conn = _setup_chat_listener(w['id'], w['ws_url'], label)
            instance_registry[w['id']] = {
                'workspace': w['workspace'],
                'title': w['title'],
                'ws': conn,
                'ws_url': w['ws_url'],
                'listener_ws': listener_conn,
                'convs': {},
            }
            print(f'[cdp] Connected: {label}  [{w["id"][:8]}]')
        except Exception as e:
            print(f'[cdp] Failed to connect to {label}: {e}')

    if not instance_registry:
        print('ERROR: Could not connect to any Cursor instance.')
        sys.exit(1)

    for iid, info in instance_registry.items():
        if info['workspace']:
            try:
                convs = list_chats(lambda js, c=info['ws']: cdp_eval_on(c, js))
                info['convs'] = {
                    c['pc_id']: {
                        'name': c['name'],
                        'active': c['active'],
                        'msg_id': c.get('msg_id'),
                    }
                    for c in convs
                }
                names = [c['name'] for c in convs]
                print(f'[cdp] Conversations in {info["workspace"]}: {names}')
            except Exception:
                pass

    # Restore per-route mirrors from persisted bindings; first match sets active window
    active_instance_id = None
    mirrored_chats.clear()
    for rk, binding in _route_bindings_initial.items():
        saved_ws = binding.get('workspace')
        saved_pc_id = binding.get('pc_id')
        saved_name = binding.get('chat_name')
        saved_msg = _norm_msg_fp(binding.get('msg_id'))
        if not saved_pc_id and not saved_name:
            continue
        for wid, info in instance_registry.items():
            if saved_ws and info['workspace'] != saved_ws:
                continue
            for pc_id, conv in info.get('convs', {}).items():
                live_fp0 = _norm_msg_fp(conv.get('msg_id'))
                id_ok = bool(saved_msg and live_fp0 == saved_msg)
                if pc_id == saved_pc_id or id_ok:
                    live_fp = _norm_msg_fp(conv.get('msg_id'))
                    fp = live_fp or saved_msg
                    mirrored_chats[rk] = _mirror_row(wid, pc_id, conv['name'], fp)
                    if active_instance_id is None:
                        active_instance_id = wid
                        print(
                            f'[cdp] Active (restored): {info["workspace"]} -- {conv["name"]}  [{rk.to_storage_key()}]'
                        )
                    else:
                        print(
                            f'[cdp] Route restored: {rk.to_storage_key()} -> {conv["name"]}  ({info["workspace"]})'
                        )
                    break
            if rk in mirrored_chats:
                break

    _dedupe_forum_thread_mirrors_by_cursor_chat()
    _prune_forum_general_route_if_redundant()

    if not active_instance_id:
        active_instance_id = next(
            (wid for wid, info in instance_registry.items() if info['workspace']),
            next(iter(instance_registry)),
        )
        active_name = instance_registry[active_instance_id]['workspace'] or '(no workspace)'
        print(f'[cdp] Active (default): {active_name}')
    ws = instance_registry[active_instance_id]['ws']


msg_id_counter = 0
msg_id_lock = threading.Lock()


def cdp_eval_on(conn, expression):
    """Evaluate JS on a specific WebSocket connection. Thread-safe via cdp_lock."""
    global msg_id_counter
    with msg_id_lock:
        msg_id_counter += 1
        mid = msg_id_counter
    with cdp_lock:
        conn.send(
            json.dumps(
                {
                    'id': mid,
                    'method': 'Runtime.evaluate',
                    'params': {'expression': expression, 'returnByValue': True},
                }
            )
        )
        result = json.loads(conn.recv())
    return result.get('result', {}).get('result', {}).get('value')


def active_conn():
    """Return the WebSocket for the active instance (from registry, not the global ws)."""
    if active_instance_id and active_instance_id in instance_registry:
        return instance_registry[active_instance_id]['ws']
    return ws


def cdp_eval(expression):
    """Evaluate JS on the active instance. Thread-safe via cdp_lock."""
    return cdp_eval_on(active_conn(), expression)


def _cdp_cmd(conn, method, params=None):
    """Send a CDP command and return the result. Thread-safe."""
    global msg_id_counter
    with msg_id_lock:
        msg_id_counter += 1
        mid = msg_id_counter
    msg = {'id': mid, 'method': method}
    if params:
        msg['params'] = params
    with cdp_lock:
        conn.send(json.dumps(msg))
        return json.loads(conn.recv())


def _win32_force_foreground(title):
    """Bypass Windows focus-stealing prevention.

    Primary: SetWindowPos with HWND_TOPMOST (no flicker, z-order trick).
    Fallback: minimize/restore (flickers but always works).
    """
    import ctypes
    import ctypes.wintypes as wt

    user32 = ctypes.windll.user32

    hwnd = user32.FindWindowW(None, title)
    if not hwnd:
        print(f"[cdp] bring_to_front: FindWindowW no match for '{title[:50]}'")
        return False

    # Properly typed HWND values — critical on 64-bit Windows where
    # HWND is a pointer (c_void_p). Without argtypes, ctypes truncates
    # -1 to 32-bit c_int which SetWindowPos silently ignores.
    user32.SetWindowPos.argtypes = [
        wt.HWND,
        wt.HWND,
        ctypes.c_int,
        ctypes.c_int,
        ctypes.c_int,
        ctypes.c_int,
        ctypes.c_uint,
    ]
    user32.SetWindowPos.restype = wt.BOOL

    SWP = 0x0002 | 0x0001 | 0x0040  # NOMOVE | NOSIZE | SHOWWINDOW
    TOPMOST = wt.HWND(-1)
    NOTOPMOST = wt.HWND(-2)

    r1 = user32.SetWindowPos(hwnd, TOPMOST, 0, 0, 0, 0, SWP)
    r2 = user32.SetWindowPos(hwnd, NOTOPMOST, 0, 0, 0, 0, SWP)
    user32.SetForegroundWindow(hwnd)

    if r1 and r2:
        print(f"[cdp] bring_to_front: SetWindowPos OK  hwnd={hwnd}  title='{title[:50]}'")
        return True

    # Fallback: minimize/restore (causes brief flicker but guaranteed)
    print(f'[cdp] bring_to_front: SetWindowPos failed ({r1},{r2}), trying minimize/restore')
    user32.ShowWindow(hwnd, 6)  # SW_MINIMIZE
    time.sleep(0.05)
    user32.ShowWindow(hwnd, 9)  # SW_RESTORE
    user32.SetForegroundWindow(hwnd)
    print(f'[cdp] bring_to_front: minimize/restore  hwnd={hwnd}')
    return True


def cdp_bring_to_front(conn, target_id=None):
    """Bring a Cursor window to the foreground.

    Three-stage strategy:
      1. CDP Page.bringToFront + window.focus() (works when OS allows it)
      2. Target.activateTarget on browser-level WS (activates the tab in Chrome)
      3. OS-specific: Win32 SetWindowPos | macOS osascript | Linux xdotool
    """
    print(f'[cdp] bring_to_front: Page.bringToFront + window.focus()  target={target_id}')
    _cdp_cmd(conn, 'Page.bringToFront')
    cdp_eval_on(conn, 'window.focus()')

    if target_id and _browser_ws_url:
        try:
            browser_conn = websocket.create_connection(_browser_ws_url)
            try:
                browser_conn.send(
                    json.dumps(
                        {
                            'id': 1,
                            'method': 'Target.activateTarget',
                            'params': {'targetId': target_id},
                        }
                    )
                )
                result = json.loads(browser_conn.recv())
                if result.get('error'):
                    print(f'[cdp] bring_to_front: Target.activateTarget FAILED: {result["error"]}')
                else:
                    print(f'[cdp] bring_to_front: Target.activateTarget OK  target={target_id[:8]}')
            finally:
                browser_conn.close()
        except Exception as e:
            print(f'[cdp] bring_to_front: Target.activateTarget exception: {e}')

    try:
        title = cdp_eval_on(conn, 'document.title')
        if not title:
            print('[cdp] bring_to_front: document.title was empty')
        elif sys.platform == 'win32':
            _win32_force_foreground(title)
        elif sys.platform == 'darwin':
            sp.Popen(
                ['osascript', '-e', 'tell application "Cursor" to activate'],
                stdout=sp.DEVNULL,
                stderr=sp.DEVNULL,
            )
            print('[cdp] bring_to_front: osascript activate')
        elif sys.platform.startswith('linux'):
            sp.Popen(
                ['xdotool', 'search', '--name', title, 'windowactivate'],
                stdout=sp.DEVNULL,
                stderr=sp.DEVNULL,
            )
            print('[cdp] bring_to_front: xdotool windowactivate')
    except Exception as e:
        print(f'[cdp] bring_to_front: OS fallback exception: {e}')


def cdp_insert_text(text):
    """Insert text via CDP Input.insertText. Thread-safe."""
    global ws, msg_id_counter
    with msg_id_lock:
        msg_id_counter += 1
        mid = msg_id_counter
    with cdp_lock:
        ws.send(json.dumps({'id': mid, 'method': 'Input.insertText', 'params': {'text': text}}))
        json.loads(ws.recv())


def cdp_screenshot_on(conn):
    """Capture a screenshot via CDP on a specific connection. Returns PNG bytes."""
    global msg_id_counter
    with msg_id_lock:
        msg_id_counter += 1
        mid = msg_id_counter
    with cdp_lock:
        conn.send(
            json.dumps({'id': mid, 'method': 'Page.captureScreenshot', 'params': {'format': 'png'}})
        )
        result = json.loads(conn.recv())
    b64 = result.get('result', {}).get('data')
    return base64.b64decode(b64) if b64 else None


def cdp_screenshot():
    """Capture a screenshot of the active Cursor window. Returns PNG bytes."""
    return cdp_screenshot_on(active_conn())


def cdp_hover_file_path(filename_selector):
    """Hover over a filename element in the chat to read the full path from its tooltip.

    Uses CDP Input.dispatchMouseEvent (synthetic, doesn't move the real cursor).
    Tooltip format: 'workspace • relative\\path\\file.ext'
    Returns the relative path (e.g., 'scripts/food-tracker/journal.md') or None.
    """
    try:
        conn = active_conn()
        pos = cdp_eval_on(
            conn,
            f"""
            (() => {{
                const el = document.querySelector('{filename_selector}');
                if (!el) return null;
                const r = el.getBoundingClientRect();
                return JSON.stringify({{x: r.x + r.width/2, y: r.y + r.height/2}});
            }})();
        """,
        )
        if not pos:
            return None
        box = json.loads(pos)

        # Hover over filename to trigger tooltip
        _cdp_cmd(
            conn,
            'Input.dispatchMouseEvent',
            {'type': 'mouseMoved', 'x': int(box['x']), 'y': int(box['y'])},
        )

        # Poll for tooltip (typically appears within 50-100ms)
        tooltip = None
        deadline = time.time() + 1.0
        while time.time() < deadline:
            tooltip = cdp_eval_on(
                conn,
                """
                (() => {
                    const hover = document.querySelector('.workbench-hover-container .hover-contents');
                    return hover ? hover.textContent.trim() : null;
                })();
            """,
            )
            if tooltip:
                break
            time.sleep(0.05)

        # Move mouse away to dismiss tooltip
        _cdp_cmd(conn, 'Input.dispatchMouseEvent', {'type': 'mouseMoved', 'x': 0, 'y': 0})
        time.sleep(0.1)

        if not tooltip:
            return None
        # "workspace • relative\path\file.ext" → "relative/path/file.ext"
        parts = tooltip.split(' • ', 1)
        if len(parts) == 2:
            return parts[1].replace('\\', '/')
        return tooltip.replace('\\', '/')
    except Exception as e:
        print(f'[monitor] cdp_hover_file_path error: {e}')
        return None


def cdp_try_expand(selector):
    """Expand a collapsed element by clicking its expand chevron (if any).

    Works for file edits, terminal commands, and any tool-call container
    that has a .composer-message-codeblock-expand button.
    Walks up from the selector to the bubble and looks for the button there.
    Returns True if expanded, False otherwise.
    """
    try:
        result = cdp_eval(f"""
            (() => {{
                const el = document.querySelector('{selector}');
                if (!el) return 'no_el';
                const bubble = el.closest('[id^="bubble-"]');
                const btn = (bubble || el).querySelector('.composer-message-codeblock-expand');
                if (!btn) return 'no_btn';
                const icon = btn.querySelector('.codicon');
                if (!icon || !icon.classList.contains('codicon-chevron-down')) return 'not_collapsed';
                btn.click();
                return 'expanded';
            }})();
        """)
        if result == 'expanded':
            time.sleep(0.5)
            return True
        if result not in ('no_btn', 'not_collapsed'):
            print(f'[screenshot] try_expand: {result}')
        return False
    except Exception as e:
        print(f'[screenshot] try_expand error: {e}')
        return False


def cdp_try_collapse(selector):
    """Collapse an expanded element back by clicking its chevron-up button."""
    try:
        cdp_eval(f"""
            (() => {{
                const el = document.querySelector('{selector}');
                if (!el) return 'skip';
                const bubble = el.closest('[id^="bubble-"]');
                const btn = (bubble || el).querySelector('.composer-message-codeblock-expand');
                if (!btn) return 'skip';
                const icon = btn.querySelector('.codicon');
                if (icon && icon.classList.contains('codicon-chevron-up')) btn.click();
                return 'ok';
            }})();
        """)
        time.sleep(0.3)
    except Exception as e:
        print(f'[screenshot] try_collapse error: {e}')


def cdp_screenshot_element(selector):
    """Screenshot a specific DOM element by CSS selector. Returns PNG bytes or None.

    Takes a full screenshot (which works reliably), then crops the element
    region using Pillow. Sidesteps CDP clip coordinate/DPR issues entirely.
    """
    # Step 1: Scroll the element into view
    found = cdp_eval(f"""
        (function() {{
            const el = document.querySelector('{selector}');
            if (!el) return null;
            el.scrollIntoView({{ block: 'center', behavior: 'instant' }});
            return 'ok';
        }})();
    """)
    if not found:
        print(f'[screenshot] Element NOT found: {selector}')
        return None

    # Step 2: Wait for scroll to settle
    time.sleep(0.5)

    # Step 3: Get bounding rect + viewport size
    rect = cdp_eval(f"""
        (function() {{
            const container = document.querySelector('{selector}');
            if (!container) return null;
            const table = container.querySelector('table.markdown-table') || container.querySelector('table') || container;
            const r = table.getBoundingClientRect();
            const pad = 6;
            return JSON.stringify({{
                x: Math.max(0, r.x - pad),
                y: Math.max(0, r.y - pad),
                width: r.width + pad * 2,
                height: r.height + pad * 2,
                viewport_w: window.innerWidth,
                viewport_h: window.innerHeight
            }});
        }})();
    """)
    if not rect:
        return None
    try:
        box = json.loads(rect)
    except (json.JSONDecodeError, TypeError):
        return None

    if box['width'] < 1 or box['height'] < 1:
        return None

    # Step 4: Take full screenshot
    full_png = cdp_screenshot()
    if not full_png:
        print('[screenshot] Full screenshot failed')
        return None

    # Step 5: Crop using Pillow — calculate scale from image size vs viewport
    img = Image.open(io.BytesIO(full_png))
    img_w, img_h = img.size
    scale_x = img_w / box['viewport_w']
    scale_y = img_h / box['viewport_h']

    # Convert CSS pixel coords to image pixel coords
    left = int(box['x'] * scale_x)
    top = int(box['y'] * scale_y)
    right = int((box['x'] + box['width']) * scale_x)
    bottom = int((box['y'] + box['height']) * scale_y)

    # Clamp to image bounds
    left = max(0, left)
    top = max(0, top)
    right = min(img_w, right)
    bottom = min(img_h, bottom)

    print(
        f'[screenshot] Crop: {img_w}x{img_h} @ {scale_x:.1f}x -> ({left},{top})-({right},{bottom})'
    )

    cropped = img.crop((left, top, right, bottom))

    # Telegram rejects photos under ~100px on shortest side (PHOTO_INVALID_DIMENSIONS).
    # Pad small crops with the background color from the bottom-right pixel.
    MIN_DIM = 100
    cw, ch = cropped.size
    if cw < MIN_DIM or ch < MIN_DIM:
        new_w = max(cw, MIN_DIM)
        new_h = max(ch, MIN_DIM)
        bg = cropped.getpixel((cw - 1, ch - 1))
        padded = Image.new(cropped.mode, (new_w, new_h), bg)
        padded.paste(cropped, ((new_w - cw) // 2, (new_h - ch) // 2))
        cropped = padded

    # Export as PNG bytes
    buf = io.BytesIO()
    cropped.save(buf, format='PNG')
    png_bytes = buf.getvalue()
    print(f'[screenshot] Result: {cropped.size[0]}x{cropped.size[1]}, {len(png_bytes)} bytes')
    return png_bytes


def cursor_paste_image(image_bytes, mime='image/png', filename='image.png'):
    """Paste an image into Cursor's editor via simulated ClipboardEvent."""
    b64 = base64.b64encode(image_bytes).decode('ascii')

    # Focus editor first
    focus_result = cdp_eval("""
        (function() {
            let editor = document.querySelector('.aislash-editor-input');
            if (!editor) {
                const all = document.querySelectorAll('[data-lexical-editor="true"]');
                for (const ed of all) {
                    if (ed.contentEditable === 'true') { editor = ed; break; }
                }
            }
            if (!editor) return 'ERROR: no editor';
            editor.focus();
            editor.click();
            return 'OK';
        })();
    """)
    if focus_result != 'OK':
        return focus_result

    time.sleep(0.3)

    # Inject image via paste event
    result = cdp_eval(f"""
        (function() {{
            const b64 = "{b64}";
            const mime = "{mime}";
            const filename = "{filename}";

            // Decode base64 to binary
            const binary = atob(b64);
            const bytes = new Uint8Array(binary.length);
            for (let i = 0; i < binary.length; i++) bytes[i] = binary.charCodeAt(i);
            const blob = new Blob([bytes], {{ type: mime }});
            const file = new File([blob], filename, {{ type: mime }});

            // Build DataTransfer with the image file
            const dt = new DataTransfer();
            dt.items.add(file);

            // Find the editor
            let editor = document.querySelector('.aislash-editor-input');
            if (!editor) {{
                const all = document.querySelectorAll('[data-lexical-editor="true"]');
                for (const ed of all) {{
                    if (ed.contentEditable === 'true') {{ editor = ed; break; }}
                }}
            }}
            if (!editor) return 'ERROR: no editor for paste';

            // Dispatch paste event
            const event = new ClipboardEvent('paste', {{
                bubbles: true,
                cancelable: true,
                clipboardData: dt
            }});
            editor.dispatchEvent(event);
            return 'OK: paste dispatched';
        }})();
    """)
    return result


# ── Cursor helpers ───────────────────────────────────────────────────────────


def cursor_click_send():
    """Click the send button in Cursor's editor. Used after image paste with no text."""
    return cdp_eval("""
        (function() {
            const selectors = [
                '.send-with-mode .anysphere-icon-button',
                'button[aria-label="Send"]',
                '.send-with-mode button',
            ];
            for (const sel of selectors) {
                const btn = document.querySelector(sel);
                if (btn) {
                    setTimeout(() => btn.click(), 0);
                    return 'OK: ' + sel;
                }
            }
            return 'ERROR: no send button';
        })();
    """)


_CONTEXT_PCTS_MAX = 200

_context_pct_names = {}  # {pc_id: str} — chat names from .context_pcts


def _load_context_pcts():
    """Load per-chat context % and names from disk."""
    global _context_pct_names
    if not context_pcts_file.exists():
        return {}
    try:
        data = json.loads(context_pcts_file.read_text())
        _context_pct_names = {
            k: v['name'] for k, v in data.items() if isinstance(v, dict) and 'name' in v
        }
        return {k: v['pct'] for k, v in data.items() if isinstance(v, dict) and 'pct' in v}
    except Exception:
        return {}


def _save_context_pcts(pc_id=None, chat_name=None):
    """Persist per-chat context % to disk, pruning to most recent entries."""
    try:
        existing = {}
        if context_pcts_file.exists():
            existing = json.loads(context_pcts_file.read_text())
        for pid, pct in _context_pcts.items():
            entry = existing.get(pid, {})
            entry['pct'] = pct
            entry['ts'] = datetime.now().isoformat()
            if pid == pc_id and chat_name:
                entry['name'] = chat_name
            existing[pid] = entry
        if len(existing) > _CONTEXT_PCTS_MAX:
            sorted_entries = sorted(
                existing.items(), key=lambda x: x[1].get('ts', ''), reverse=True
            )
            existing = dict(sorted_entries[:_CONTEXT_PCTS_MAX])
        context_pcts_file.write_text(json.dumps(existing, indent=2))
    except Exception:
        pass


if CONTEXT_MONITOR:
    _context_pcts = _load_context_pcts()
    if _context_pcts:
        print(f'[context-monitor] Restored {len(_context_pcts)} chat(s) from .context_pcts')
else:
    _context_pcts = {}


def cursor_prefill_input(text, conn=None):
    """Focus the input editor and insert text WITHOUT sending.
    Used to pre-fill the annotation so it rides with the user's next message.
    """
    global msg_id_counter
    c = conn or active_conn()
    with cdp_lock:
        with msg_id_lock:
            msg_id_counter += 1
            mid = msg_id_counter
        c.send(
            json.dumps(
                {
                    'id': mid,
                    'method': 'Runtime.evaluate',
                    'params': {
                        'expression': """
                (function() {
                    let editor = document.querySelector('.aislash-editor-input');
                    if (!editor) {
                        const all = document.querySelectorAll('[data-lexical-editor="true"]');
                        for (const ed of all) {
                            if (ed.contentEditable === 'true') { editor = ed; break; }
                        }
                    }
                    if (!editor) return 'ERROR: no input editor found';
                    editor.focus();
                    editor.click();
                    return 'OK';
                })();
            """,
                        'returnByValue': True,
                    },
                }
            )
        )
        focus_result = json.loads(c.recv())
        focus_val = focus_result.get('result', {}).get('result', {}).get('value')
        if focus_val != 'OK':
            return focus_val

        with msg_id_lock:
            msg_id_counter += 1
            mid = msg_id_counter
        c.send(
            json.dumps({'id': mid, 'method': 'Input.insertText', 'params': {'text': text + '\n'}})
        )
        json.loads(c.recv())
        return 'OK'


def cursor_clear_input(conn=None):
    """Focus the chat input editor, select all, and delete via execCommand."""
    global msg_id_counter
    c = conn or active_conn()
    with cdp_lock:
        with msg_id_lock:
            msg_id_counter += 1
            mid = msg_id_counter
        c.send(
            json.dumps(
                {
                    'id': mid,
                    'method': 'Runtime.evaluate',
                    'params': {
                        'expression': """
                (function() {
                    let editor = document.querySelector('.aislash-editor-input');
                    if (!editor) {
                        const all = document.querySelectorAll('[data-lexical-editor="true"]');
                        for (const ed of all) {
                            if (ed.contentEditable === 'true') { editor = ed; break; }
                        }
                    }
                    if (!editor) return 'NO_EDITOR';
                    if (!editor.textContent.trim()) return 'EMPTY';
                    editor.focus();
                    const sel = window.getSelection();
                    const range = document.createRange();
                    range.selectNodeContents(editor);
                    sel.removeAllRanges();
                    sel.addRange(range);
                    document.execCommand('delete');
                    return 'CLEARED';
                })();
            """,
                        'returnByValue': True,
                    },
                }
            )
        )
        json.loads(c.recv())


def cdp_activate_agent_tab(conn, resolved_pc_id: str, resolved_name: str) -> str:
    """Activate the agent/composer tab for the given pc_id (same logic as /chats callback)."""
    return cdp_eval_on(
        conn,
        f"""
        (function() {{
            const targetPcId = {json.dumps(resolved_pc_id)};
            const nameHint = {json.dumps(resolved_name)};
            function normChatTitle(s) {{
                return (s || '').replace(/\\s+/g, ' ').trim().toLowerCase();
            }}
            function unifiedCellTitle(cell) {{
                const te = cell.querySelector('.agent-sidebar-cell-text')
                    || cell.querySelector('[class*="sidebar-cell-text"]')
                    || cell.querySelector('[class*="cell-text"]');
                if (te) return te.textContent.trim();
                const al = cell.getAttribute('aria-label');
                if (al) return al.trim();
                return (cell.textContent || '').replace(/\\s+/g, ' ').trim();
            }}
            const candidates = Array.from(document.querySelectorAll('[data-pc-id]')).filter(
                function(n) {{ return n.getAttribute('data-pc-id') === targetPcId; }});
            let el = null;
            for (const c of candidates) {{
                if (c.querySelector('a[aria-id="chat-horizontal-tab"]')) {{ el = c; break; }}
                if (c.querySelector('.composer-tab-label')) {{ el = c; break; }}
                if (c.classList && c.classList.contains('agent-sidebar-cell')) {{ el = c; break; }}
            }}
            if (!el && nameHint) {{
                const w = normChatTitle(nameHint);
                for (const cell of document.querySelectorAll('.unified-agents-sidebar .agent-sidebar-cell')) {{
                    if (cell.getAttribute('data-selected') === null) continue;
                    const g = normChatTitle(unifiedCellTitle(cell));
                    if (g && g === w) {{ el = cell; break; }}
                }}
                if (!el && w.length >= 3) {{
                    for (const cell of document.querySelectorAll('.unified-agents-sidebar .agent-sidebar-cell')) {{
                        if (cell.getAttribute('data-selected') === null) continue;
                        const g = normChatTitle(unifiedCellTitle(cell));
                        if (g && (g.indexOf(w) >= 0 || w.indexOf(g) >= 0)) {{ el = cell; break; }}
                    }}
                }}
            }}
            if (!el && nameHint) {{
                const w = normChatTitle(nameHint);
                for (const a of document.querySelectorAll('[class*="agent-tabs"] li a[aria-id="chat-horizontal-tab"]')) {{
                    const lab = a.getAttribute('aria-label') || a.textContent.trim();
                    const g = normChatTitle(lab);
                    if (g === w) {{ el = a.closest('li'); break; }}
                }}
            }}
            if (!el) return 'ERROR: tab not found (pc_id=' + targetPcId + ', name=' + (nameHint || '?') + ', checked ' + candidates.length + ' id candidates)';
            const a = el.querySelector('a[aria-id="chat-horizontal-tab"]');
            if (a) {{ a.click(); return a.getAttribute('aria-label') || 'OK'; }}
            if (el.classList && el.classList.contains('agent-sidebar-cell')) {{
                el.click();
                const ut = unifiedCellTitle(el);
                return ut || 'OK';
            }}
            el.dispatchEvent(new MouseEvent('mousedown', {{bubbles: true, cancelable: true, button: 0}}));
            const label = el.querySelector('.label-name');
            return label ? label.textContent.trim() || 'OK' : 'OK';
        }})();
    """,
    )


def cursor_switch_to_mirrored(mc: MirrorRow | tuple[str, str, str] | None) -> None:
    """Bring the Cursor window to front and activate the mirrored agent tab."""
    global active_instance_id, ws
    if not mc:
        return
    m = _normalize_mirror_row(mc)
    iid, pc_id, name = m[0], m[1], m[2]
    info = instance_registry.get(iid)
    if not info:
        return
    if iid != active_instance_id:
        with cdp_lock:
            active_instance_id = iid
            ws = info['ws']
        try:
            cdp_bring_to_front(info['ws'], iid)
        except Exception:
            pass
    cdp_activate_agent_tab(info['ws'], pc_id, name)


def cursor_send_message(text, raw=False, mirrored=None):
    """Focus the input editor, insert text, click send.
    Holds the CDP lock for the entire sequence to avoid monitor thread contention.
    Auto-prepends [Phone] [Day YYYY-MM-DD HH:MM] unless raw=True.
    If ``mirrored`` is (instance_id, pc_id, name), switch to that tab first (forum topic routing).
    """
    if not raw:
        timestamp = datetime.now().strftime('%a %Y-%m-%d %H:%M')
        text = f'[{timestamp}] [Phone] {text}'

    global msg_id_counter
    if mirrored:
        cursor_switch_to_mirrored(mirrored)
        iid, _, _ = mirrored
        conn = instance_registry.get(iid, {}).get('ws') or active_conn()
    else:
        conn = active_conn()
    t0 = time.time()

    with cdp_lock:
        # 1. Focus editor
        with msg_id_lock:
            msg_id_counter += 1
            mid = msg_id_counter
        conn.send(
            json.dumps(
                {
                    'id': mid,
                    'method': 'Runtime.evaluate',
                    'params': {
                        'expression': """
                (function() {
                    let editor = document.querySelector('.aislash-editor-input');
                    if (!editor) {
                        const all = document.querySelectorAll('[data-lexical-editor="true"]');
                        for (const ed of all) {
                            if (ed.contentEditable === 'true') { editor = ed; break; }
                        }
                    }
                    if (!editor) return 'ERROR: no input editor found';
                    editor.focus();
                    // Move cursor to end so new text appends after any prefilled annotation
                    const sel = window.getSelection();
                    const range = document.createRange();
                    range.selectNodeContents(editor);
                    range.collapse(false);
                    sel.removeAllRanges();
                    sel.addRange(range);
                    return 'OK';
                })();
            """,
                        'returnByValue': True,
                    },
                }
            )
        )
        focus_result = json.loads(conn.recv())
        focus_val = focus_result.get('result', {}).get('result', {}).get('value')
        if focus_val != 'OK':
            return focus_val
        t1 = time.time()

        # 2. Insert text at end (still holding lock)
        with msg_id_lock:
            msg_id_counter += 1
            mid = msg_id_counter
        conn.send(json.dumps({'id': mid, 'method': 'Input.insertText', 'params': {'text': text}}))
        json.loads(conn.recv())
        t2 = time.time()

        # 3. Verify + click send (still holding lock)
        with msg_id_lock:
            msg_id_counter += 1
            mid = msg_id_counter
        conn.send(
            json.dumps(
                {
                    'id': mid,
                    'method': 'Runtime.evaluate',
                    'params': {
                        'expression': """
                (function() {
                    let editor = document.querySelector('.aislash-editor-input');
                    if (!editor) {
                        const all = document.querySelectorAll('[data-lexical-editor="true"]');
                        for (const ed of all) {
                            if (ed.contentEditable === 'true') { editor = ed; break; }
                        }
                    }
                    if (!editor || !editor.textContent.trim()) return 'ERROR: text not inserted';
                    const selectors = [
                        '.send-with-mode .anysphere-icon-button',
                        'button[aria-label="Send"]',
                        '.send-with-mode button',
                    ];
                    for (const sel of selectors) {
                        const btn = document.querySelector(sel);
                        if (btn) {
                            // Async click — returns immediately, click fires on next microtask
                            setTimeout(() => btn.click(), 0);
                            return 'OK: ' + sel;
                        }
                    }
                    return 'ERROR: no send button';
                })();
            """,
                        'returnByValue': True,
                    },
                }
            )
        )
        send_result = json.loads(conn.recv())
        result = send_result.get('result', {}).get('result', {}).get('value')

    t3 = time.time()
    print(
        f'[sender] Timing: focus={int((t1 - t0) * 1000)}ms insert={int((t2 - t1) * 1000)}ms verify+send={int((t3 - t2) * 1000)}ms total={int((t3 - t0) * 1000)}ms'
    )
    return result


def cursor_new_chat():
    """Click the '+' button to create a new chat tab. Returns 'OK' or error."""
    return cdp_eval("""
        (function() {
            // Primary: the "New Chat" button in the auxiliary bar title
            const btn = document.querySelector('[data-command-id="auxiliaryBar.newAgentMenu"] a.codicon-add-two')
                     || document.querySelector('[data-command-id="composer.createNewComposerTab"] a.codicon-add-two')
                     || document.querySelector('a[aria-label*="New Chat"]');
            if (!btn) return 'ERROR: new-chat button not found';
            btn.click();
            return 'OK';
        })();
    """)


def cursor_get_active_conv():
    """Get the name of the active conversation tab."""
    return (
        cdp_eval("""
        (function() {
            const tab = document.querySelector('[class*="agent-tabs"] li[class*="checked"] a[aria-id="chat-horizontal-tab"]');
            if (tab) return tab.getAttribute('aria-label') || '';
            const unified = document.querySelector('.unified-agents-sidebar .agent-sidebar-cell[data-selected="true"] .agent-sidebar-cell-text');
            return unified ? unified.textContent.trim() : '';
        })();
    """)
        or ''
    )


def cursor_list_convs():
    """List all conversation tabs. Returns [{name, active}]."""
    result = cdp_eval("""
        (function() {
            const agentAnchors = document.querySelectorAll('[class*="agent-tabs"] li[class*="action-item"] a[aria-id="chat-horizontal-tab"]');
            const rows = [];
            if (agentAnchors.length > 0) {
                agentAnchors.forEach(a => {
                    const li = a.closest('li');
                    rows.push({
                        name: a.getAttribute('aria-label') || '',
                        active: li ? li.classList.contains('checked') : false
                    });
                });
            } else {
                document.querySelectorAll('.unified-agents-sidebar .agent-sidebar-cell').forEach(cell => {
                    if (cell.getAttribute('data-selected') === null) return;
                    const textEl = cell.querySelector('.agent-sidebar-cell-text');
                    rows.push({
                        name: textEl ? textEl.textContent.trim() : '',
                        active: cell.getAttribute('data-selected') === 'true'
                    });
                });
            }
            return JSON.stringify(rows);
        })();
    """)
    try:
        return json.loads(result) if result else []
    except json.JSONDecodeError:
        return []


def cursor_switch_conv(index):
    """Switch to conversation tab by 0-based index. Returns the tab name or error."""
    return cdp_eval(f"""
        (function() {{
            const tabs = document.querySelectorAll('[class*="agent-tabs"] li[class*="action-item"] a[aria-id="chat-horizontal-tab"]');
            if (tabs.length > 0) {{
                if ({index} >= tabs.length) return 'ERROR: only ' + tabs.length + ' tabs open';
                const tab = tabs[{index}];
                tab.click();
                return tab.getAttribute('aria-label') || 'OK';
            }}
            const cells = [];
            document.querySelectorAll('.unified-agents-sidebar .agent-sidebar-cell').forEach(cell => {{
                if (cell.getAttribute('data-selected') !== null) cells.push(cell);
            }});
            if ({index} >= cells.length) return 'ERROR: only ' + cells.length + ' chats open';
            const cell = cells[{index}];
            cell.click();
            const textEl = cell.querySelector('.agent-sidebar-cell-text');
            return textEl ? textEl.textContent.trim() : 'OK';
        }})();
    """)


def cursor_get_turn_info(composer_prefix='', conn=None):
    """Get the last turn's user message and all AI response sections.

    Uses composer-human-ai-pair-container which groups one user message
    with all its AI responses as a single turn.
    Returns individual sections (not joined) for real-time streaming.
    'turn_id' = unique DOM id of the human message (detects new turns).
    'user_full' = complete user message for forwarding to Telegram.
    'images' = list of vscode-file:// image URLs attached to the message.

    If composer_prefix is given (e.g. 'b625b741' from pc_id 'cid-b625b741'),
    scopes the search to the content area with that data-composer-id.
    If conn is given, evaluates on that WebSocket instead of active_conn().
    """
    js = """
        (function() {
            // Helper: extract text from a markdown-section element,
            // preserving list numbering from <ol>/<li> elements.
            // textContent/innerText lose CSS-generated counters.
            function getSectionText(section) {
                let result = '';
                for (const node of section.childNodes) {
                    if (node.tagName === 'OL') {
                        node.querySelectorAll(':scope > li').forEach(li => {
                            const val = li.getAttribute('value') || '';
                            result += '\\n' + val + '. ' + li.textContent.trim();
                        });
                    } else if (node.tagName === 'UL') {
                        node.querySelectorAll(':scope > li').forEach(li => {
                            result += '\\n- ' + li.textContent.trim();
                        });
                    } else {
                        // Regular text — append inline (preserves word spacing)
                        result += node.textContent;
                    }
                }
                return result.trim();
            }

            const composerPrefix = '__COMPOSER_PREFIX__';
            let scope = document;
            if (composerPrefix) {
                const scoped = document.querySelector('[data-composer-id^="' + composerPrefix + '"]');
                if (!scoped) return JSON.stringify({ turn_id: '', user_full: '', sections: [], images: [], conv: '' });
                scope = scoped;
            }
            const containers = scope.querySelectorAll('.composer-human-ai-pair-container');
            if (containers.length === 0) return JSON.stringify({ turn_id: '', user_full: '', sections: [], images: [] });

            const last = containers[containers.length - 1];

            // Get the user message text from this turn
            // Use the readonly lexical editor inside the human message to avoid
            // grabbing UI elements like todo widget text
            const humanMsg = last.querySelector('[data-message-role="human"]');
            const turnId = humanMsg ? ('turn:' + (humanMsg.getAttribute('data-message-id') || '')) : '';
            let userFull = '';
            if (humanMsg) {
                const lexical = humanMsg.querySelector('.aislash-editor-input-readonly');
                userFull = lexical ? lexical.textContent.trim() : humanMsg.textContent.trim();
            }

            // Get image attachments from user message
            const images = [];
            const imgPills = last.querySelectorAll('.context-pill-image img');
            imgPills.forEach(img => {
                if (img.src) images.push(img.src);
            });

            // Get ALL content elements from AI messages in this turn, in DOM order.
            // Walks all message bubbles (AI text, tables, code blocks, tool/file-edit blocks)
            // using data-flat-index for correct ordering.
            const sections = [];
            const allBubbles = last.querySelectorAll('[data-message-role="ai"], [data-message-kind="tool"]');
            allBubbles.forEach(msg => {
                const msgId = msg.getAttribute('data-message-id') || '';
                const bubbleSuffix = msgId.split('-').pop();
                const kind = msg.getAttribute('data-message-kind');
                // Counter for generating fallback IDs when the DOM doesn't
                // provide one (tables lack a DOM id; code blocks inherit
                // from their parent markdown-section).
                let subIdx = 0;

                // --- Tool messages (file edits, confirmations, etc.) ---
                if (kind === 'tool') {
                    const toolStatus = msg.getAttribute('data-tool-status');
                    const toolCallId = msg.getAttribute('data-tool-call-id') || '';
                    // Pending confirmation: legacy [data-click-ready] OR new ui-shell (Run/Skip)
                    const bubbleSelector = '#bubble-' + bubbleSuffix;
                    let actionBtns = msg.querySelectorAll('[data-click-ready="true"]');
                    let buttonsSelector = bubbleSelector + ' [data-click-ready="true"]';
                    let selectorForShot = bubbleSelector + ' .composer-tool-former-message > div';
                    const shellPending = msg.querySelector('.ui-shell-tool-call--pending');
                    if (actionBtns.length === 0 && shellPending) {
                        const approvalRow = shellPending.querySelector('.ui-shell-tool-call__approval-row');
                        if (approvalRow) {
                            actionBtns = approvalRow.querySelectorAll('button.ui-button');
                            buttonsSelector = bubbleSelector + ' .ui-shell-tool-call--pending .ui-shell-tool-call__approval-row button.ui-button';
                            selectorForShot = bubbleSelector + ' .ui-shell-tool-call';
                        }
                    }
                    // MCP / GitHub PR / external tools: .composer-mcp-tool-call-block (no data-click-ready)
                    if (actionBtns.length === 0) {
                        const mcpBlock = msg.querySelector('.composer-mcp-tool-call-block');
                        if (mcpBlock) {
                            const mcpBtns = mcpBlock.querySelectorAll('button.ui-button');
                            if (mcpBtns.length > 0) {
                                actionBtns = mcpBtns;
                                buttonsSelector = bubbleSelector + ' .composer-mcp-tool-call-block button.ui-button';
                                selectorForShot = bubbleSelector + ' .composer-mcp-tool-call-block';
                            }
                        }
                    }

                    if (actionBtns.length > 0) {
                        const buttons = Array.from(actionBtns).map((btn, idx) => ({
                            label: btn.innerText.trim().replace(/\\s+/g, ' '),
                            index: idx
                        }));

                        let cleanText = 'Action pending';
                        if (shellPending) {
                            const parts = [];
                            const descLine = shellPending.querySelector('.ui-shell-tool-call__description');
                            const summary = shellPending.querySelector('.ui-shell-tool-call__summary');
                            const cmd = shellPending.querySelector('.ui-shell-tool-call__command');
                            if (descLine) parts.push(descLine.textContent.trim());
                            if (summary) parts.push(summary.textContent.trim());
                            if (cmd) parts.push(cmd.textContent.trim().replace(/\\s+/g, ' '));
                            cleanText = parts.filter(Boolean).join(' — ') || 'Shell command pending';
                        } else if (msg.querySelector('.composer-mcp-tool-call-block')) {
                            const mcpBlock = msg.querySelector('.composer-mcp-tool-call-block');
                            let t = mcpBlock.innerText.trim().replace(/\\s+/g, ' ');
                            if (t.length > 2400) t = t.slice(0, 2400) + '…';
                            cleanText = t || 'MCP / tool action pending';
                        } else {
                        const desc = msg.querySelector('.composer-tool-former-message');
                        // Extract text from specific DOM parts, ignoring control row (buttons)
                        // and Monaco diff editors (whose innerText changes async and breaks stability).
                        if (desc) {
                            const parts = [];
                            // File edit confirmation: filename + line stats + block status
                            const filename = desc.querySelector('.composer-code-block-filename');
                            if (filename) {
                                parts.push(filename.textContent.trim());
                                const fileStat = desc.querySelector('.composer-code-block-status');
                                if (fileStat) parts.push(fileStat.textContent.trim());
                                // Skip block-attribution-pill (Cursor's "Blocked" dropdown — not useful in Telegram)
                            }
                            // Tool call confirmation: headers + body
                            const topHeader = desc.querySelector('.composer-tool-call-top-header');
                            const header = desc.querySelector('.composer-tool-call-header');
                            const body = desc.querySelector('.composer-tool-call-body');
                            if (topHeader) parts.push(topHeader.innerText.trim().replace(/\\s+/g, ' '));
                            if (header) parts.push(header.innerText.trim().replace(/\\s+/g, ' '));
                            if (body && body.innerText.trim()) parts.push(body.innerText.trim());
                            if (!parts.length) {
                                // Fallback: clone desc, strip status row (buttons),
                                // walk text nodes and join with spaces (innerText
                                // doesn't insert spaces between flex items).
                                const clone = desc.cloneNode(true);
                                const sr = clone.querySelector('.composer-tool-call-status-row');
                                if (sr) sr.remove();
                                const walker = document.createTreeWalker(clone, NodeFilter.SHOW_TEXT);
                                let node;
                                while (node = walker.nextNode()) {
                                    const t = node.textContent.trim();
                                    if (t) parts.push(t);
                                }
                            }
                            cleanText = parts.join(' ') || 'Action pending';
                        }
                        }
                        sections.push({
                            text: cleanText,
                            type: 'confirmation',
                            id: toolCallId || ('gen:' + msgId + ':' + subIdx),
                            selector: selectorForShot,
                            buttons_selector: buttonsSelector,
                            buttons: buttons
                        });
                        return;
                    }

                    // File edit (code block with diff).
                    // While the AI is still writing, a loading spinner
                    // (.cursorLoadingBackground) is visible. Don't classify yet —
                    // once writing finishes, either buttons appear (blocked edit →
                    // confirmation) or not (auto-accepted → file_edit).
                    const codeBlock = msg.querySelector('.composer-code-block-container');
                    if (codeBlock) {
                        if (codeBlock.querySelector('.cursorLoadingBackground')) return;
                        const filename = msg.querySelector('.composer-code-block-filename');
                        const status = msg.querySelector('.composer-code-block-status');
                        const fname = filename ? filename.textContent.trim() : 'file';
                        const stat = status ? status.textContent.trim() : '';
                        const selector = '#bubble-' + bubbleSuffix + ' .composer-code-block-container';
                        sections.push({
                            text: fname + (stat ? ' ' + stat : ''),
                            type: 'file_edit',
                            id: toolCallId || ('gen:' + msgId + ':' + subIdx),
                            selector: selector,
                            filename_selector: '#bubble-' + bubbleSuffix + ' .composer-code-block-filename',
                            file_stat: stat
                        });
                    }
                    return;
                }

                // --- Thinking messages ---
                if (kind === 'thinking') {
                    // Cursor removes thinking content from DOM when collapsed.
                    // If collapsed, click the header to expand so we can read
                    // the content on the next tick.
                    let root = msg.querySelector('.anysphere-markdown-container-root') || msg.querySelector('.markdown-root');
                    if (!root) {
                        const header = msg.querySelector('.collapsible-thought > div:first-child');
                        if (header) header.click();
                    }
                    let thinkText = '';
                    if (root) {
                        const childList = root.classList.contains('markdown-root')
                            ? (root.firstElementChild ? Array.from(root.firstElementChild.children) : Array.from(root.children))
                            : Array.from(root.children);
                        const parts = [];
                        for (const child of childList) {
                            if (child.classList.contains('markdown-section') || root.classList.contains('markdown-root')) {
                                const t = getSectionText(child);
                                if (t) parts.push(t);
                            }
                        }
                        thinkText = parts.join('\\n');
                    }
                    // Always push (even if empty) to hold correct index position.
                    sections.push({
                        text: thinkText,
                        type: 'thinking',
                        id: msgId || ('gen:thinking:' + subIdx),
                        selector: null
                    });
                    return;
                }

                // --- New UI: pending shell tool embedded in AI bubble (not always data-message-kind="tool") ---
                if (msg.getAttribute('data-message-role') === 'ai') {
                    const shellPending = msg.querySelector('.ui-shell-tool-call--pending');
                    if (shellPending) {
                        const approvalRow = shellPending.querySelector('.ui-shell-tool-call__approval-row');
                        const shellBtns = approvalRow ? approvalRow.querySelectorAll('button.ui-button') : [];
                        if (shellBtns.length > 0) {
                            const buttons = Array.from(shellBtns).map((btn, idx) => ({
                                label: btn.innerText.trim().replace(/\\s+/g, ' '),
                                index: idx
                            }));
                            const parts = [];
                            const descLine = shellPending.querySelector('.ui-shell-tool-call__description');
                            const summary = shellPending.querySelector('.ui-shell-tool-call__summary');
                            const cmd = shellPending.querySelector('.ui-shell-tool-call__command');
                            if (descLine) parts.push(descLine.textContent.trim());
                            if (summary) parts.push(summary.textContent.trim());
                            if (cmd) parts.push(cmd.textContent.trim().replace(/\\s+/g, ' '));
                            const cleanText = parts.filter(Boolean).join(' — ') || 'Shell command pending';
                            const bubbleSelector = '#bubble-' + bubbleSuffix;
                            const nestedId = shellPending.closest('[data-tool-call-id]');
                            const tid = nestedId ? nestedId.getAttribute('data-tool-call-id') : '';
                            sections.push({
                                text: cleanText,
                                type: 'confirmation',
                                id: tid || ('gen:' + msgId + ':shell'),
                                selector: bubbleSelector + ' .ui-shell-tool-call',
                                buttons_selector: bubbleSelector + ' .ui-shell-tool-call--pending .ui-shell-tool-call__approval-row button.ui-button',
                                buttons: buttons
                            });
                            return;
                        }
                    }
                    // MCP block rendered inside AI markdown bubble (not data-message-kind=tool)
                    const mcpInAi = msg.querySelector('.composer-mcp-tool-call-block');
                    if (mcpInAi) {
                        const mcpAiBtns = mcpInAi.querySelectorAll('button.ui-button');
                        if (mcpAiBtns.length > 0) {
                            const buttons = Array.from(mcpAiBtns).map((btn, idx) => ({
                                label: btn.innerText.trim().replace(/\\s+/g, ' '),
                                index: idx
                            }));
                            let t = mcpInAi.innerText.trim().replace(/\\s+/g, ' ');
                            if (t.length > 2400) t = t.slice(0, 2400) + '…';
                            const cleanMcp = t || 'MCP / tool action pending';
                            const bubbleSel = '#bubble-' + bubbleSuffix;
                            const nestedId = mcpInAi.closest('[data-tool-call-id]');
                            const tidMcp = nestedId ? nestedId.getAttribute('data-tool-call-id') : '';
                            sections.push({
                                text: cleanMcp,
                                type: 'confirmation',
                                id: tidMcp || ('gen:' + msgId + ':mcpai'),
                                selector: bubbleSel + ' .composer-mcp-tool-call-block',
                                buttons_selector: bubbleSel + ' .composer-mcp-tool-call-block button.ui-button',
                                buttons: buttons
                            });
                            return;
                        }
                    }
                }

                // --- AI text messages (markdown sections, code blocks + tables) ---
                const root = msg.querySelector('.anysphere-markdown-container-root') || msg.querySelector('.markdown-root');
                if (!root) return;
                let tableIndex = 0;

                const childList = root.classList.contains('markdown-root')
                    ? (root.firstElementChild ? Array.from(root.firstElementChild.children) : Array.from(root.children))
                    : Array.from(root.children);

                for (const child of childList) {
                    if (child.classList.contains('markdown-section') || root.classList.contains('markdown-root')) {
                        const codeBlock = child.querySelector('.markdown-block-code')
                            || (child.tagName === 'PRE' ? child : child.querySelector('pre'));
                        const latexBlock = child.querySelector('.markdown-block-latex');
                        const isTable = child.classList.contains('markdown-table-container')
                            || child.tagName === 'TABLE' || !!child.querySelector('table');
                        if (codeBlock) {
                            const text = child.innerText.trim();
                            const selector = child.id
                                ? '#' + child.id + ' .markdown-block-code'
                                : '#bubble-' + bubbleSuffix + ' .markdown-block-code';
                            sections.push({
                                text: text,
                                type: 'code_block',
                                id: child.id || ('gen:' + msgId + ':' + subIdx),
                                selector: selector
                            });
                            subIdx++;
                        } else if (latexBlock) {
                            const text = child.innerText.trim();
                            const selector = child.id
                                ? '#' + child.id + ' .markdown-block-latex'
                                : '#bubble-' + bubbleSuffix + ' .markdown-block-latex';
                            sections.push({
                                text: text,
                                type: 'latex',
                                id: child.id || ('gen:' + msgId + ':' + subIdx),
                                selector: selector
                            });
                            subIdx++;
                        } else if (child.querySelector('.markdown-inline-latex')) {
                            const text = child.innerText.trim();
                            const selector = child.id
                                ? '#' + child.id
                                : '#bubble-' + bubbleSuffix;
                            sections.push({
                                text: text,
                                type: 'latex',
                                id: child.id || ('gen:' + msgId + ':' + subIdx),
                                selector: selector
                            });
                            subIdx++;
                        } else if (isTable) {
                            const text = child.innerText.trim();
                            sections.push({
                                text: text,
                                type: 'table',
                                id: child.id || ('gen:' + msgId + ':' + subIdx),
                                selector: child.id ? '#' + child.id : '#bubble-' + bubbleSuffix + ' table'
                            });
                            subIdx++;
                            tableIndex++;
                        } else {
                            const text = getSectionText(child);
                            if (text.length > 0) {
                                sections.push({
                                    text: text,
                                    type: 'text',
                                    id: child.id || ('gen:' + msgId + ':' + subIdx),
                                    selector: null
                                });
                                subIdx++;
                            }
                        }
                    } else if (child.classList.contains('markdown-table-container')) {
                        const text = child.innerText.trim();
                        const selector = '#bubble-' + bubbleSuffix +
                            ' .markdown-table-container' +
                            (tableIndex > 0 ? ':nth-of-type(' + (tableIndex + 1) + ')' : '');
                        sections.push({
                            text: text,
                            type: 'table',
                            id: 'gen:' + msgId + ':' + subIdx,
                            selector: selector
                        });
                        subIdx++;
                        tableIndex++;
                    }
                }
            });

            // Active conversation name: checked agent tab or unified sidebar
            const convTab = document.querySelector('[class*="agent-tabs"] li[class*="checked"] a[aria-id="chat-horizontal-tab"]');
            let convName = convTab ? convTab.getAttribute('aria-label') : '';
            if (!convName) {
                const unified = document.querySelector('.unified-agents-sidebar .agent-sidebar-cell[data-selected="true"] .agent-sidebar-cell-text');
                if (unified) convName = unified.textContent.trim();
            }

            return JSON.stringify({ turn_id: turnId, user_full: userFull, sections: sections, images: images, conv: convName });
        })();
    """.replace('__COMPOSER_PREFIX__', composer_prefix)
    result = cdp_eval_on(conn, js) if conn else cdp_eval(js)
    try:
        return (
            json.loads(result)
            if result
            else {'turn_id': '', 'user_full': '', 'sections': [], 'images': [], 'conv': ''}
        )
    except json.JSONDecodeError:
        return {'turn_id': '', 'user_full': '', 'sections': [], 'images': [], 'conv': ''}


# ── Thread 1: Telegram → Cursor (sender) ────────────────────────────────────


def check_owner(user_id, cid):
    """Check if user_id is the owner. Auto-pair on first /start."""
    global OWNER_ID

    # Load saved owner if not set
    if OWNER_ID is None and owner_file.exists():
        OWNER_ID = int(owner_file.read_text().strip())
        print(f'[owner] Loaded owner ID: {OWNER_ID}')

    # No owner yet - accept first /start
    if OWNER_ID is None:
        return 'needs_pairing'

    return 'ok' if user_id == OWNER_ID else 'rejected'


def sender_thread():
    global \
        chat_id, \
        OWNER_ID, \
        last_sender_route, \
        last_sent_by_route, \
        last_tg_message_id_by_route, \
        last_tg_message_id, \
        muted, \
        active_instance_id, \
        mirrored_chats
    print('[sender] Starting Telegram poller...')

    # Drain any pending updates from before this restart
    # so we don't re-process old messages
    offset = 0
    drain = tg_call('getUpdates', offset=0, timeout=0)
    if drain.get('ok') and drain['result']:
        offset = drain['result'][-1]['update_id'] + 1
        print(f'[sender] Skipped {len(drain["result"])} pending updates')

    while True:
        try:
            updates = tg_call(
                'getUpdates',
                offset=offset,
                timeout=30,
                allowed_updates=['message', 'callback_query'],
            )
            if not updates.get('ok'):
                time.sleep(2)
                continue

            for update in updates['result']:
                offset = update['update_id'] + 1
                # Handle inline keyboard callbacks (Accept/Reject)
                callback = update.get('callback_query')
                if callback:
                    cb_data = callback.get('data', '')
                    cb_id = callback.get('id')
                    cb_user_id = callback.get('from', {}).get('id')

                    print(f'[sender] Callback: data={cb_data!r} user={cb_user_id}')

                    # Only owner can press buttons
                    if OWNER_ID and cb_user_id != OWNER_ID:
                        print(f'[sender] Callback ignored: not owner ({cb_user_id} != {OWNER_ID})')
                        continue

                    action, _, cb_key = cb_data.partition(':')
                    with pending_confirms_lock:
                        selectors = pending_confirms.pop(cb_key, None)
                    print(
                        f'[sender] Callback: action={action!r} cb_key={cb_key[:16]!r} selectors={"found" if selectors else "NONE"}'
                    )

                    if cb_data == 'noop':
                        tg_call('answerCallbackQuery', callback_query_id=cb_id)
                        continue

                    if action == 'setup_commands':
                        if cb_key == 'yes':
                            ok = tg_register_commands()
                            tg_call(
                                'answerCallbackQuery',
                                callback_query_id=cb_id,
                                text='Commands registered!' if ok else 'Failed to register',
                            )
                            # Update the message to remove the buttons
                            cb_msg = callback.get('message', {})
                            if cb_msg:
                                tg_call(
                                    'editMessageText',
                                    chat_id=cb_msg['chat']['id'],
                                    message_id=cb_msg['message_id'],
                                    text='✅ Command menu registered.',
                                )
                        else:
                            tg_call('answerCallbackQuery', callback_query_id=cb_id, text='Skipped')
                            cb_msg = callback.get('message', {})
                            if cb_msg:
                                tg_call(
                                    'editMessageText',
                                    chat_id=cb_msg['chat']['id'],
                                    message_id=cb_msg['message_id'],
                                    text='Command menu skipped. You can always add commands later via /setcommands in @BotFather.',
                                )
                        continue

                    if action in ('agent', 'chat'):
                        # New format: chat:{instance_id}:{pc_id}
                        parts = cb_data.split(':', 2)
                        if len(parts) == 3:
                            cb_msg = callback.get('message') or {}
                            try:
                                cb_route = route_key_from_message(cb_msg)
                            except Exception:
                                cb_route = (
                                    RouteKey(cb_msg.get('chat', {}).get('id', 0), None)
                                    if cb_msg.get('chat')
                                    else _primary_route()
                                )
                            if cb_route is not None:
                                last_sender_route = cb_route
                            _, target_iid, target_pc_id = parts
                            info = instance_registry.get(target_iid)
                            if not info:
                                tg_call(
                                    'answerCallbackQuery',
                                    callback_query_id=cb_id,
                                    text='Instance not found',
                                )
                                continue
                            try:
                                fresh_rows = list_chats(lambda js: cdp_eval_on(info['ws'], js))
                            except Exception as e:
                                print(f'[sender] list_chats before chat switch: {e}')
                                fresh_rows = []
                            convs = info.get('convs') or {}
                            if target_pc_id not in convs:
                                tg_call(
                                    'answerCallbackQuery',
                                    callback_query_id=cb_id,
                                    text='Chat list outdated — tap /chats then pick again',
                                )
                                print(f'[sender] Unknown pc_id in registry: {target_pc_id!r}')
                                continue

                            reg = convs[target_pc_id]
                            reg_name = (reg.get('name') or '').strip()

                            def _norm_chat_title(s):
                                return ' '.join((s or '').split()).lower()

                            row = next(
                                (r for r in fresh_rows if r.get('pc_id') == target_pc_id), None
                            )
                            if not row and reg_name:
                                row = next(
                                    (
                                        r
                                        for r in fresh_rows
                                        if (r.get('name') or '').strip() == reg_name
                                    ),
                                    None,
                                )
                            if not row and reg_name:
                                rn = _norm_chat_title(reg_name)
                                for r in fresh_rows:
                                    if _norm_chat_title(r.get('name')) == rn:
                                        row = r
                                        break

                            if not row:
                                tg_call(
                                    'answerCallbackQuery',
                                    callback_query_id=cb_id,
                                    text='Could not find that chat — send /chats and try again',
                                )
                                print(
                                    f'[sender] No fresh row for pc_id={target_pc_id!r} name={reg_name!r} (got {len(fresh_rows)} rows)'
                                )
                                continue

                            resolved_pc_id = row['pc_id']
                            resolved_name = (row.get('name') or reg_name or '').strip()
                            # Click the tab with matching data-pc-id (works for both agent-tabs and editor-group tabs)
                            # Note: filter [data-pc-id] by value so ids never break CSS quoting.
                            # Uses resolved_pc_id from fresh list_chats (ids can rotate when the sidebar rebuilds).
                            result = cdp_eval_on(
                                info['ws'],
                                f"""
                                (function() {{
                                    const targetPcId = {json.dumps(resolved_pc_id)};
                                    const nameHint = {json.dumps(resolved_name)};
                                    function normChatTitle(s) {{
                                        return (s || '').replace(/\\s+/g, ' ').trim().toLowerCase();
                                    }}
                                    function unifiedCellTitle(cell) {{
                                        const te = cell.querySelector('.agent-sidebar-cell-text')
                                            || cell.querySelector('[class*="sidebar-cell-text"]')
                                            || cell.querySelector('[class*="cell-text"]');
                                        if (te) return te.textContent.trim();
                                        const al = cell.getAttribute('aria-label');
                                        if (al) return al.trim();
                                        return (cell.textContent || '').replace(/\\s+/g, ' ').trim();
                                    }}
                                    const candidates = Array.from(document.querySelectorAll('[data-pc-id]')).filter(
                                        function(n) {{ return n.getAttribute('data-pc-id') === targetPcId; }});
                                    let el = null;
                                    for (const c of candidates) {{
                                        if (c.querySelector('a[aria-id="chat-horizontal-tab"]')) {{ el = c; break; }}
                                        if (c.querySelector('.composer-tab-label')) {{ el = c; break; }}
                                        if (c.classList && c.classList.contains('agent-sidebar-cell')) {{ el = c; break; }}
                                    }}
                                    if (!el && nameHint) {{
                                        const w = normChatTitle(nameHint);
                                        for (const cell of document.querySelectorAll('.unified-agents-sidebar .agent-sidebar-cell')) {{
                                            if (cell.getAttribute('data-selected') === null) continue;
                                            const g = normChatTitle(unifiedCellTitle(cell));
                                            if (g && g === w) {{ el = cell; break; }}
                                        }}
                                        if (!el && w.length >= 3) {{
                                            for (const cell of document.querySelectorAll('.unified-agents-sidebar .agent-sidebar-cell')) {{
                                                if (cell.getAttribute('data-selected') === null) continue;
                                                const g = normChatTitle(unifiedCellTitle(cell));
                                                if (g && (g.indexOf(w) >= 0 || w.indexOf(g) >= 0)) {{ el = cell; break; }}
                                            }}
                                        }}
                                    }}
                                    if (!el && nameHint) {{
                                        const w = normChatTitle(nameHint);
                                        for (const a of document.querySelectorAll('[class*="agent-tabs"] li a[aria-id="chat-horizontal-tab"]')) {{
                                            const lab = a.getAttribute('aria-label') || a.textContent.trim();
                                            const g = normChatTitle(lab);
                                            if (g === w) {{ el = a.closest('li'); break; }}
                                        }}
                                    }}
                                    if (!el) return 'ERROR: tab not found (pc_id=' + targetPcId + ', name=' + (nameHint || '?') + ', checked ' + candidates.length + ' id candidates)';
                                    // Agent-tab: click the <a> inside the <li>
                                    const a = el.querySelector('a[aria-id="chat-horizontal-tab"]');
                                    if (a) {{ a.click(); return a.getAttribute('aria-label') || 'OK'; }}
                                    // Unified sidebar cell
                                    if (el.classList && el.classList.contains('agent-sidebar-cell')) {{
                                        el.click();
                                        const ut = unifiedCellTitle(el);
                                        return ut || 'OK';
                                    }}
                                    // Editor-group tab: use mousedown (VS Code activates tabs on mousedown, not click)
                                    el.dispatchEvent(new MouseEvent('mousedown', {{bubbles: true, cancelable: true, button: 0}}));
                                    const label = el.querySelector('.label-name');
                                    return label ? label.textContent.trim() || 'OK' : 'OK';
                                }})();
                            """,
                            )
                            if result and result.startswith('ERROR'):
                                tg_call('answerCallbackQuery', callback_query_id=cb_id, text=result)
                            else:
                                # Switch active instance if needed
                                if target_iid != active_instance_id:
                                    with cdp_lock:
                                        active_instance_id = target_iid
                                    print(f'[sender] Switched instance to: {info["workspace"]}')
                                    # Bring the target Cursor window to the foreground via CDP
                                    try:
                                        cdp_bring_to_front(info['ws'], target_iid)
                                    except Exception as e:
                                        print(f'[sender] Could not bring window to front: {e}')
                                # Update mirror for this Telegram route immediately
                                chat_label = (
                                    result
                                    if result and result != 'OK'
                                    else (resolved_name or resolved_pc_id)
                                )
                                if cb_route is not None:
                                    convs = info.get('convs') or {}
                                    cv = convs.get(resolved_pc_id) or {}
                                    _fp = _norm_msg_fp(cv.get('msg_id'))
                                    with mirrored_chats_lock:
                                        mirrored_chats[cb_route] = _mirror_row(
                                            target_iid,
                                            resolved_pc_id,
                                            chat_label,
                                            _fp,
                                        )
                                    _persist_mirrored_routes()
                                tg_call(
                                    'answerCallbackQuery', callback_query_id=cb_id, text='Switched'
                                )
                            print(f'[sender] Agent switch: {result}')
                        else:
                            # Legacy format: agent:{index}
                            _legacy = cb_data.split(':', 1)
                            if len(_legacy) != 2:
                                tg_call(
                                    'answerCallbackQuery', callback_query_id=cb_id, text='Invalid'
                                )
                                continue
                            try:
                                idx = int(_legacy[1])
                            except ValueError:
                                tg_call(
                                    'answerCallbackQuery', callback_query_id=cb_id, text='Invalid'
                                )
                                continue
                            result = cursor_switch_conv(idx)
                            if result and result.startswith('ERROR'):
                                tg_call('answerCallbackQuery', callback_query_id=cb_id, text=result)
                            else:
                                tg_call(
                                    'answerCallbackQuery', callback_query_id=cb_id, text='Switched'
                                )
                            print(f'[sender] Agent switch: {result}')
                    elif selectors and action.startswith('btn_'):
                        # Universal button click: action = "btn_INDEX"
                        try:
                            btn_index = int(action.split('_', 1)[1])
                        except (ValueError, IndexError):
                            tg_call(
                                'answerCallbackQuery',
                                callback_query_id=cb_id,
                                text='Invalid button',
                            )
                            continue
                        btns_selector = selectors.get('buttons_selector', '')
                        btn_label = next(
                            (
                                b['label']
                                for b in selectors.get('buttons', [])
                                if b['index'] == btn_index
                            ),
                            f'Button {btn_index}',
                        )
                        print(
                            f"[sender] Callback: click button [{btn_index}] '{btn_label}' (cb_key={cb_key[:16]})"
                        )
                        _iid = selectors.get('instance_id') if selectors else None
                        _conn = (
                            instance_registry.get(_iid, {}).get('ws')
                            if _iid and _iid in instance_registry
                            else None
                        )
                        _js = f"""
                            (function() {{
                                const btns = document.querySelectorAll('{btns_selector}');
                                if (!btns[{btn_index}]) return 'ERROR: button ' + {btn_index} + ' not found (' + btns.length + ' buttons)';
                                btns[{btn_index}].click();
                                return 'OK';
                            }})();
                        """
                        click_result = cdp_eval_on(_conn, _js) if _conn else cdp_eval(_js)
                        print(f'[sender] Click result: {click_result}')
                        tg_call('answerCallbackQuery', callback_query_id=cb_id, text=btn_label)
                    else:
                        tg_call('answerCallbackQuery', callback_query_id=cb_id, text='Expired')
                    continue

                msg = update.get('message')
                if not msg:
                    continue

                text = msg.get('text', '')
                photo = msg.get('photo')  # List of PhotoSize objects
                voice = msg.get('voice')  # Voice message object
                caption = msg.get('caption', '')

                # Skip messages with no actionable content
                if not text and not photo and not voice:
                    continue

                cid = msg['chat']['id']
                mid = msg['message_id']
                user_id = msg['from']['id']
                user = msg['from'].get('first_name', '?')

                try:
                    route = route_key_from_message(msg)
                except Exception:
                    route = RouteKey(cid, None)
                last_sender_route = route
                thr = route.message_thread_id

                # Owner check
                status = check_owner(user_id, cid)

                if status == 'needs_pairing':
                    # First message from anyone -> auto-pair
                    OWNER_ID = user_id
                    owner_file.write_text(str(user_id))
                    with chat_id_lock:
                        chat_id = cid
                    chat_id_file.write_text(str(cid))
                    print(f'[owner] Auto-paired with {user} (ID: {user_id})')
                    tg_send(
                        cid,
                        "🔗 You're in! Messages flow both ways now.\nUse /pause to mute, /play to resume.",
                    )
                    if tg_commands_need_update():
                        tg_ask_command_update(cid)
                    continue

                if status == 'rejected':
                    print(f'[sender] Rejected message from {user} (ID: {user_id})')
                    tg_send(
                        cid,
                        "Already paired with someone else.\nIf that's you on another device, send /unpair there first.\n\nWant your own? github.com/qmHecker/pocket-cursor",
                    )
                    continue

                # Store chat_id for the monitor thread (and persist for restarts)
                with chat_id_lock:
                    chat_id = cid
                chat_id_file.write_text(str(cid))

                # Handle photo messages (from phone gallery, camera, etc.)
                if photo:
                    if _forum_topic_requires_mirror(route):
                        tg_send(cid, _FORUM_TOPIC_UNBOUND, message_thread_id=thr)
                        continue
                    print(f'[sender] {user}: [photo] {caption}')
                    tg_typing(cid, message_thread_id=thr)
                    _mc = _mirror_for_inbound(route)
                    if _mc:
                        cursor_switch_to_mirrored(_mc)
                    # Mark so monitor knows this turn came from Telegram
                    with last_sent_lock:
                        last_sent_by_route[route] = caption if caption else '[photo]'
                        last_tg_message_id_by_route[route] = mid
                        last_tg_message_id = mid
                    # Get the largest resolution (last in the array)
                    file_id = photo[-1]['file_id']
                    # Download from Telegram
                    file_info = tg_call('getFile', file_id=file_id)
                    if file_info.get('ok'):
                        file_path = file_info['result']['file_path']
                        dl_url = f'https://api.telegram.org/file/bot{TOKEN}/{file_path}'
                        img_data = requests.get(dl_url, timeout=30).content
                        print(f'[sender] Downloaded {len(img_data)} bytes')

                        # Determine mime type
                        ext = file_path.rsplit('.', 1)[-1].lower() if '.' in file_path else 'jpg'
                        mime = {
                            'jpg': 'image/jpeg',
                            'jpeg': 'image/jpeg',
                            'png': 'image/png',
                            'gif': 'image/gif',
                            'webp': 'image/webp',
                        }.get(ext, 'image/jpeg')

                        # Paste image into Cursor
                        paste_result = cursor_paste_image(img_data, mime, f'telegram_photo.{ext}')
                        print(f'[sender] Paste result: {paste_result}')

                        # If there's a caption, also insert it as text
                        if caption:
                            time.sleep(0.5)
                            cursor_send_message(caption, mirrored=_mc)
                        else:
                            # Just click send after the image
                            time.sleep(0.5)
                            cursor_click_send()
                    else:
                        tg_send(
                            cid, 'Failed to download photo from Telegram.', message_thread_id=thr
                        )
                    continue

                # Handle voice messages
                if voice:
                    if _forum_topic_requires_mirror(route):
                        tg_send(cid, _FORUM_TOPIC_UNBOUND, message_thread_id=thr)
                        continue
                    print(f'[sender] {user}: [voice] {voice.get("duration", "?")}s')
                    tg_typing(cid, message_thread_id=thr)
                    file_id = voice['file_id']
                    file_info = tg_call('getFile', file_id=file_id)
                    if file_info.get('ok'):
                        file_path = file_info['result']['file_path']
                        dl_url = f'https://api.telegram.org/file/bot{TOKEN}/{file_path}'
                        audio_data = requests.get(dl_url, timeout=30).content
                        print(f'[sender] Downloaded voice: {len(audio_data)} bytes')

                        # Transcribe
                        transcription = transcribe_voice(audio_data)
                        if transcription:
                            print(f'[sender] Transcribed: {transcription[:80]}')
                            # Echo transcription back to Telegram so user sees what was understood
                            tg_send(cid, f'🎤 {transcription}', message_thread_id=thr)
                            # Send to Cursor
                            with last_sent_lock:
                                last_sent_by_route[route] = transcription
                                last_tg_message_id_by_route[route] = mid
                                last_tg_message_id = mid
                            result = cursor_send_message(
                                f'[Voice] {transcription}', mirrored=_mirror_for_inbound(route)
                            )
                            print(f'[sender] -> Cursor: {result}')
                        else:
                            tg_send(
                                cid,
                                'Could not transcribe voice message. Is OPENAI_API_KEY set?',
                                message_thread_id=thr,
                            )
                    else:
                        tg_send(cid, 'Failed to download voice message.', message_thread_id=thr)
                    continue

                print(f'[sender] {user}: {text}')

                # Handle commands
                if text == '/start':
                    conv_name = cursor_get_active_conv()
                    status_line = '⏸ Paused' if muted else '▶ Active'
                    instances = len(instance_registry)
                    lines = [
                        f'PocketCursor is running. {status_line}',
                        f'{instances} workspace{"s" if instances != 1 else ""} connected.',
                    ]
                    if conv_name:
                        lines.append(f'💬 {conv_name}')
                    lines.append('\n/newchat /chats /pause /play /screenshot /unpair')
                    tg_send(cid, '\n'.join(lines), message_thread_id=thr)
                    continue

                if text == '/unpair':
                    OWNER_ID = None
                    if owner_file.exists():
                        owner_file.unlink()
                    tg_send(
                        cid,
                        '👋 Unpaired. Next message from anyone will pair them.',
                        message_thread_id=thr,
                    )
                    print('[owner] Unpaired')
                    continue

                if text == '/pause':
                    muted = True
                    muted_file.touch()
                    tg_send(
                        cid,
                        "⏸ Paused. Nothing will be forwarded.\nSend /play when you're ready.",
                        message_thread_id=thr,
                    )
                    print('[sender] Paused')
                    continue

                if text == '/play':
                    muted = False
                    muted_file.unlink(missing_ok=True)
                    # Include active conversation name in resume message
                    conv_name = cursor_get_active_conv()
                    resume_msg = '▶ Resumed.'
                    if conv_name:
                        resume_msg += f'\n💬 {conv_name}'
                    tg_send(cid, resume_msg, message_thread_id=thr)
                    print('[sender] Resumed')
                    continue

                if text == '/screenshot':
                    print(
                        f'[sender] Taking screenshot of {active_instance_id and active_instance_id[:8]}...'
                    )
                    try:
                        cdp_bring_to_front(active_conn(), active_instance_id)
                    except Exception:
                        pass
                    time.sleep(0.3)
                    png = cdp_screenshot()
                    if png:
                        tg_send_photo_bytes(
                            cid, png, caption='Cursor IDE screenshot', message_thread_id=thr
                        )
                        print(f'[sender] Screenshot sent ({len(png)} bytes)')
                    else:
                        tg_send(cid, 'Failed to capture screenshot.', message_thread_id=thr)
                    continue

                if text == '/newchat':
                    print('[sender] Creating new chat...')
                    result = cursor_new_chat()
                    if not result or not result.startswith('OK'):
                        tg_send(cid, f'Failed: {result}', message_thread_id=thr)
                    print(f'[sender] New chat: {result}')
                    continue

                if text in ('/chats', '/agents', '/agent'):
                    # Single Telegram message — previously we sent one per workspace key (N workspaces => N pings).
                    keyboard_rows = []
                    inst_with = [
                        (iid, inf) for iid, inf in instance_registry.items() if inf.get('convs')
                    ]
                    multi = len(inst_with) > 1
                    for iid, info in inst_with:
                        convs = info['convs']
                        for pc_id, conv in convs.items():
                            _mr = mirrored_chats.get(route)
                            is_mirrored = _mr and _mr[0] == iid and _mr[1] == pc_id
                            star = '▶ ' if is_mirrored else ''
                            name = (
                                conv.get('name') if isinstance(conv, dict) else None
                            ) or '(unnamed)'
                            label = f'{star}[{iid[:8]}] {name}' if multi else f'{star}{name}'
                            if len(label) > 64:
                                label = label[:61] + '…'
                            keyboard_rows.append(
                                [{'text': label, 'callback_data': f'chat:{iid}:{pc_id}'}]
                            )
                    if keyboard_rows:
                        n = len(keyboard_rows)
                        body = f'📂 {n} chat(s) — tap to mirror'
                        if multi:
                            body += '\n[iid] = Cursor window id when multiple instances are open.'
                        _mk: dict[str, Any] = {
                            'chat_id': cid,
                            'text': body[:4096],
                            'reply_markup': {'inline_keyboard': keyboard_rows},
                        }
                        if thr is not None:
                            _mk['message_thread_id'] = thr
                        _r_mk = _tg_send_message_forum_aware(_mk)
                        _tg_maybe_unpin_outbound(cid, _r_mk)
                    else:
                        tg_send(cid, 'No open chats right now.', message_thread_id=thr)
                    continue

                if _forum_topic_requires_mirror(route):
                    tg_send(cid, _FORUM_TOPIC_UNBOUND, message_thread_id=thr)
                    continue

                # Record what we're sending (so monitor knows which turn is ours)
                with last_sent_lock:
                    last_sent_by_route[route] = text
                    last_tg_message_id_by_route[route] = mid
                    last_tg_message_id = mid

                # Send to Cursor with [Phone] prefix + timestamp (day name helps resolve relative dates)
                tg_typing(cid, message_thread_id=thr)
                result = cursor_send_message(text, mirrored=_mirror_for_inbound(route))
                print(f'[sender] -> Cursor: {result}')

                if 'ERROR' in str(result):
                    tg_send(cid, f'Failed: {result}', message_thread_id=thr)

        except Exception as e:
            print(f'[sender] Error: {e}')
            time.sleep(2)


# ── Thread 2: Cursor → Telegram (monitor) ────────────────────────────────────


def short_id(sid):
    """Shorten section IDs for readable logs.
    'markdown-section-be9a6e9f-f29f-4a8a-b1f8-104b63383ec5-4' → '..383ec5-4'
    'gen:be9a6e9f-...:2'  → 'gen:..9f-...:2'
    Short IDs (tool call ids, etc.) are returned as-is.
    """
    if not sid or len(sid) <= 24:
        return sid or '?'
    # Show last 12 chars (captures uuid tail + section index)
    return '..' + sid[-12:]


def _composer_prefix_from_pcid(pc_id):
    """Extract composer-id prefix from a pc_id like 'cid-b625b741' → 'b625b741'."""
    if pc_id and pc_id.startswith('cid-'):
        return pc_id[4:]
    return ''


def _forwarded_id_seed_from_sections(sections):
    """IDs of sections to treat as already mirrored to Telegram.

    Pending tool confirmations are omitted so they can still be delivered after
    you switch away and back (otherwise every section id was seeded on switch
    and confirmations were skipped forever).
    """
    return {
        sec.get('id', '')
        for sec in sections
        if isinstance(sec, dict) and sec.get('id') and sec.get('type') != 'confirmation'
    }


def _reconcile_mirrored_chat_from_dom():
    """If DOM active tab != mirror for last Telegram route, sync (fast tab switches)."""
    global mirrored_chats
    rk = _preferred_telegram_route()
    if rk is None:
        return False
    mc = mirrored_chats.get(rk)
    if not mc:
        return False
    m = _normalize_mirror_row(mc)
    iid, pc_id, name, old_fp = m
    info = instance_registry.get(iid)
    if not info:
        return False
    conn = info.get('ws')
    if not conn:
        return False
    try:
        rows = list_chats(lambda js, c=conn: cdp_eval_on(c, js))
    except Exception as e:
        print(f'[monitor] reconcile list_chats: {e}')
        return False
    active_rows = [r for r in rows if r.get('active')]
    if not active_rows:
        return False
    active = next(
        (r for r in active_rows if str(r.get('pc_id', '')).startswith('cid-')),
        None,
    )
    if not active:
        active = active_rows[0]
    apc = active.get('pc_id')
    if not apc or apc == pc_id:
        return False
    new_name = (active.get('name') or '').strip() or name
    fp = _norm_msg_fp(active.get('msg_id')) or old_fp
    with mirrored_chats_lock:
        for k, row in list(mirrored_chats.items()):
            if row[0] == iid and row[1] == pc_id:
                mirrored_chats[k] = _mirror_row(iid, apc, new_name, fp)
    _persist_mirrored_routes()
    print(f'[monitor] Reconciled active chat from DOM: {new_name!r}')
    return True


def _new_monitor_state() -> dict[str, Any]:
    return {
        'last_turn_id': None,
        'last_conv': None,
        'last_mc_pcid': None,
        'last_iid': None,
        'forwarded_ids': set(),
        'sent_this_turn': False,
        'prev_by_id': {},
        'section_stable': {},
        'initialized': False,
        'marked_done': False,
    }


def _monitor_route_entries() -> list[tuple[RouteKey, Any, list[RouteKey]]]:
    """Deduplicate monitor work: one poll per Cursor chat, one Telegram thread per outbound."""
    with mirrored_chats_lock:
        items = list(mirrored_chats.items())
    return group_routes_by_mirror(
        items,
        forum_chat_id=FORUM_CHAT_ID,
        last_sender=last_sender_route,
    )


def _inbound_sent_matches_siblings(sibling_routes: list[RouteKey], user_full: str) -> bool:
    if not user_full:
        return False
    with last_sent_lock:
        for rk in sibling_routes:
            sent = last_sent_by_route.get(rk)
            if sent and (sent[:30] in user_full or sent == '[photo]'):
                return True
    return False


def monitor_thread():
    print('[monitor] Starting Cursor monitor...')
    route_states: dict[RouteKey, dict[str, Any]] = {}
    STABLE_THRESHOLD = 2  # Forward section after N ticks of no change

    while True:
        try:
            time.sleep(1)

            with chat_id_lock:
                cid = chat_id
            if not cid:
                continue

            _reconcile_mirrored_chat_from_dom()

            entries = _monitor_route_entries()
            if not entries:
                continue

            # Same Cursor composer may be registered under multiple RouteKeys (e.g. DM + forum);
            # only prefill context-monitor text once per tick per (instance, pc_id).
            context_prefill_done: set[tuple[str, str]] = set()

            for route, mc, sibling_routes in entries:
                mirror_key = (mc[0], mc[1])
                st = route_states.setdefault(mirror_key, _new_monitor_state())
                last_turn_id = st['last_turn_id']
                last_conv = st['last_conv']
                last_mc_pcid = st['last_mc_pcid']
                last_iid = st['last_iid']
                forwarded_ids = st['forwarded_ids']
                sent_this_turn = st['sent_this_turn']
                prev_by_id = st['prev_by_id']
                section_stable = st['section_stable']
                initialized = st['initialized']
                marked_done = st['marked_done']
                cid = route.chat_id
                thr = route.message_thread_id
                try:
                    # Get the last turn's info (scoped to this route's mirrored chat composer-id)
                    cp = _composer_prefix_from_pcid(mc[1]) if mc else ''
                    mc_conn = instance_registry.get(mc[0], {}).get('ws') if mc else None
                    turn = cursor_get_turn_info(cp, conn=mc_conn)

                    # Composer not in expected instance — search others (chat may have moved)
                    if cp and not turn['turn_id']:
                        found_iid = None
                        for iid, info in instance_registry.items():
                            try:
                                turn = cursor_get_turn_info(cp, conn=info['ws'])
                                if turn['turn_id']:
                                    found_iid = iid
                                    break
                            except Exception:
                                pass

                        if not found_iid:
                            continue

                        ws_label = (
                            instance_registry[found_iid].get('workspace') or found_iid[:8]
                        ).removesuffix(' (Workspace)')
                        print(
                            f'[monitor] Composer {cp} moved to {ws_label}, skipping {len(turn["sections"])} existing'
                        )
                        with mirrored_chats_lock:
                            for rk in sibling_routes:
                                mm = _normalize_mirror_row(mc)
                                mirrored_chats[rk] = _mirror_row(found_iid, mm[1], mm[2], mm[3])
                        last_iid = found_iid
                        last_mc_pcid = mc[1]
                        last_turn_id = turn['turn_id']
                        last_conv = turn.get('conv', '')
                        forwarded_ids = _forwarded_id_seed_from_sections(turn['sections'])
                        prev_by_id = {
                            sec.get('id', ''): sec.get('text', '')
                            for sec in turn['sections']
                            if isinstance(sec, dict) and sec.get('id')
                        }
                        section_stable = {}
                        sent_this_turn = False
                        marked_done = False
                        continue

                    turn_id = turn['turn_id']  # Unique DOM id per turn
                    user_full = turn['user_full']  # Full text for forwarding
                    sections = turn['sections']
                    images = turn.get('images', [])
                    conv = turn.get('conv', '')

                    # Detect chat switch via mirrored tuple (chat_detection listener updates mirrored_chats).
                    # Uses pc_id which is stable across auto-renames — unlike conv name which
                    # changes when AI renames the chat and caused false switch detections.
                    cur_pcid = mc[1] if mc else None
                    cur_iid = mc[0] if mc else active_instance_id
                    switched = False
                    if last_mc_pcid is not None and cur_pcid and cur_pcid != last_mc_pcid:
                        switched = True
                    if last_iid is not None and cur_iid and cur_iid != last_iid:
                        switched = True

                    if switched:
                        if cur_iid != last_iid:
                            cp = _composer_prefix_from_pcid(cur_pcid) if cur_pcid else ''
                            sw_conn = instance_registry.get(cur_iid, {}).get('ws')
                            turn = (
                                cursor_get_turn_info(cp, conn=sw_conn)
                                if sw_conn
                                else cursor_get_turn_info(cp)
                            )
                            if not turn['turn_id'] and not turn['sections']:
                                for _retry in range(8):
                                    time.sleep(0.5)
                                    turn = (
                                        cursor_get_turn_info(cp, conn=sw_conn)
                                        if sw_conn
                                        else cursor_get_turn_info(cp)
                                    )
                                    if turn['turn_id'] or turn['sections']:
                                        break
                            turn_id = turn['turn_id']
                            sections = turn['sections']
                            conv = turn.get('conv', '')
                        cur_name = mc[2] if mc else conv
                        prev_name = last_conv or f'instance {last_iid[:8] if last_iid else "?"}'
                        print(
                            f"[monitor] Switched: '{prev_name[:40]}' -> '{cur_name[:40]}', skipping {len(sections)} sections"
                        )
                        forwarded_ids = _forwarded_id_seed_from_sections(sections)
                        sent_this_turn = False
                        prev_by_id = {
                            sec.get('id', ''): sec.get('text', '')
                            for sec in sections
                            if isinstance(sec, dict) and sec.get('id')
                        }
                        section_stable = {}
                        marked_done = False
                        if CONTEXT_MONITOR and cur_pcid:
                            ctx = get_context_pct(mc_conn)
                            if ctx is not None:
                                _context_pcts[cur_pcid] = ctx
                                _context_pct_names[cur_pcid] = cur_name
                                _save_context_pcts(pc_id=cur_pcid, chat_name=cur_name)
                                print(f"[context-monitor] Switch: {ctx}% in '{cur_name}'")
                        last_turn_id = turn_id
                        last_conv = conv
                        last_mc_pcid = cur_pcid
                        last_iid = cur_iid
                        continue
                    last_conv = conv
                    last_mc_pcid = cur_pcid
                    last_iid = cur_iid

                    if turn_id != last_turn_id:
                        if not initialized:
                            print(
                                f"[monitor] Init: '{user_full[:50]}', skipping {len(sections)} existing"
                            )
                            # Do not tg_send "Chat activated" here — _handle_chat_switch already notifies on
                            # in-app chat switches, and duplicate monitor + DOM messages spam the user (4+ hits).
                            if conv:
                                ws_label = ''
                                if mc:
                                    info = instance_registry.get(mc[0], {})
                                    ws_label = (info.get('workspace') or '').removesuffix(
                                        ' (Workspace)'
                                    )
                                print(
                                    f'[monitor] Active conv: {conv}'
                                    + (f'  ({ws_label})' if ws_label else '')
                                )
                            forwarded_ids = _forwarded_id_seed_from_sections(sections)
                            initialized = True
                            last_turn_id = turn_id
                            prev_by_id = {
                                sec.get('id', ''): sec.get('text', '')
                                for sec in sections
                                if isinstance(sec, dict) and sec.get('id')
                            }
                            section_stable = {}
                            continue

                        if not user_full:
                            print(
                                f'[monitor] user_full empty, polling (turn_id={short_id(turn_id)})...'
                            )
                            for attempt in range(10):
                                time.sleep(0.2)
                                t = cursor_get_turn_info(cp, conn=mc_conn)
                                t_tid = t['turn_id']
                                t_uf = t['user_full']
                                if t_tid != turn_id:
                                    print(
                                        f'[monitor]   poll {attempt}: turn_id changed -> {short_id(t_tid)}, abort'
                                    )
                                    break
                                if t_uf:
                                    print(f"[monitor]   poll {attempt}: got '{t_uf[:40]}'")
                                    user_full = t_uf
                                    sections = t['sections'] or sections
                                    images = t.get('images') or images
                                    break
                            else:
                                print('[monitor]   poll exhausted, user_full still empty')

                        # Check if this came from Telegram or was typed directly in Cursor
                        from_telegram = _inbound_sent_matches_siblings(sibling_routes, user_full)

                        origin = 'Telegram' if from_telegram else 'Cursor'
                        print(f"[monitor] New turn ({origin}): '{user_full[:50]}'")
                        for idx, sec in enumerate(sections):
                            if isinstance(sec, dict):
                                print(
                                    f'  [{idx}] {sec.get("type", "?"):12s}  id={short_id(sec.get("id"))}'
                                )

                        if CONTEXT_MONITOR and mc:
                            cur_pcid = mc[1]
                            prev_pct = _context_pcts.get(cur_pcid)
                            ctx = get_context_pct(mc_conn)
                            ann = _build_context_annotation(ctx, cur_pcid)
                            if ctx is not None:
                                _context_pcts[cur_pcid] = ctx
                                chat_label = mc[2] if mc else cur_pcid
                                _context_pct_names[cur_pcid] = chat_label
                                _save_context_pcts(pc_id=cur_pcid, chat_name=chat_label)
                                lines = [f"[context-monitor] {ctx}% used in '{chat_label}'"]
                                for pid, pct in _context_pcts.items():
                                    name = _context_pct_names.get(pid, pid)
                                    if pid == cur_pcid:
                                        delta = ctx - prev_pct if prev_pct is not None else 0
                                        trend = ' 📈' if delta > 0 else ' 📉' if delta < 0 else ''
                                        lines.append(f'  {name}: {pct:.1f}%{trend}')
                                    else:
                                        lines.append(f'  {name}: {pct:.1f}%')
                                print('\n'.join(lines))
                            if ann:
                                dedup_key = (mc[0], mc[1])
                                if dedup_key not in context_prefill_done:
                                    context_prefill_done.add(dedup_key)
                                    try:
                                        cursor_prefill_input(ann, conn=mc_conn)
                                        print(f'[context-monitor] Prefilled: {ann}')
                                    except Exception as e:
                                        print(f'[context-monitor] Failed to prefill: {e}')

                        if not from_telegram:
                            if not muted and user_full:
                                tg_send(cid, f'[PC] {user_full}', message_thread_id=thr)

                                for img_url in images:
                                    local_path = vscode_url_to_path(img_url)
                                    if local_path and Path(local_path).exists():
                                        print(
                                            f'[monitor] Forwarding image: {Path(local_path).name}'
                                        )
                                        tg_send_photo(
                                            cid,
                                            local_path,
                                            caption='[PC] attached image',
                                            message_thread_id=thr,
                                        )

                        forwarded_ids = set()
                        sent_this_turn = False
                        prev_by_id = {}
                        section_stable = {}
                        marked_done = False
                        last_turn_id = turn_id
                        continue

                    if not initialized:
                        continue

                    # Keep typing indicator alive while AI is generating
                    _gen_js = """
                        (function() { return !!document.querySelector('[data-stop-button="true"]'); })();
                    """
                    is_generating = cdp_eval_on(mc_conn, _gen_js) if mc_conn else cdp_eval(_gen_js)
                    if is_generating and not muted:
                        tg_typing(cid, message_thread_id=thr)

                    # Log newly appeared bubbles (compare against previous tick)
                    for i, sec in enumerate(sections):
                        if isinstance(sec, dict) and sec.get('id'):
                            sid = sec['id']
                            if sid not in prev_by_id and sid not in forwarded_ids:
                                print(
                                    f'[monitor] + New bubble [{i}] {sec.get("type", "?"):12s}  id={short_id(sid)}'
                                )

                    # [SILENT] scan: if ANY section contains [SILENT], suppress entire response
                    turn_silent = any(
                        '[SILENT]' in (s['text'] if isinstance(s, dict) else s) for s in sections
                    )
                    if turn_silent and not getattr(monitor_thread, '_silent_logged', False):
                        print('[monitor] [SILENT] detected — suppressing entire response')
                        for s in sections:
                            sk = s.get('id', '') if isinstance(s, dict) else ''
                            if sk:
                                forwarded_ids.add(sk)
                        sent_this_turn = True
                        monitor_thread._silent_logged = True
                    if not turn_silent:
                        monitor_thread._silent_logged = False

                    # Walk sections in DOM order. Skip already-forwarded IDs.
                    # Stop at the first un-forwarded section that isn't stable yet
                    # (preserves sequential ordering for Telegram).
                    for i, sec in enumerate(sections):
                        sec_key = sec.get('id', '') if isinstance(sec, dict) else ''
                        text = sec['text'] if isinstance(sec, dict) else sec
                        sec_type = sec.get('type', 'text') if isinstance(sec, dict) else 'text'
                        sec_id = sec.get('id') if isinstance(sec, dict) else None

                        # Already forwarded — skip
                        if sec_key and sec_key in forwarded_ids:
                            continue

                        # Check stability (keyed by ID — survives position shifts)
                        prev_text = prev_by_id.get(sec_key)
                        if text == prev_text:
                            section_stable[sec_key] = section_stable.get(sec_key, 0) + 1
                        else:
                            section_stable[sec_key] = 0

                        # Not stable yet — stop here (sequential ordering)
                        if section_stable.get(sec_key, 0) < STABLE_THRESHOLD:
                            break

                        # Don't forward empty thinking — wait for content to load
                        if sec_type == 'thinking' and not text.strip():
                            break

                        sec_selector = sec.get('selector') if isinstance(sec, dict) else None

                        if sec_type == 'confirmation':
                            # Always track confirmation selectors; send keyboard only when not muted
                            tool_id = sec_id
                            cb_key = _confirm_callback_key(str(tool_id))
                            with pending_confirms_lock:
                                if cb_key in pending_confirms:
                                    # Already tracked this confirmation
                                    if sec_key:
                                        forwarded_ids.add(sec_key)
                                    section_stable.pop(sec_key, None)
                                    continue
                            buttons = sec.get('buttons', [])
                            btns_selector = sec.get('buttons_selector', '')
                            with pending_confirms_lock:
                                pending_confirms[cb_key] = {
                                    'buttons_selector': btns_selector,
                                    'buttons': buttons,
                                    'instance_id': mc[0] if mc else None,
                                    'route': route,
                                }

                            # Auto-accept: check command text against allow/deny rules
                            rule_result = command_rules.match(text) if COMMAND_RULES else None
                            if rule_result == 'accept' and btns_selector:
                                accept_idx, accept_label = command_rules.find_accept_button(buttons)
                                if accept_idx is not None:
                                    # Screenshot BEFORE click (click changes DOM)
                                    png = (
                                        cdp_screenshot_element(sec_selector)
                                        if sec_selector
                                        else None
                                    )
                                    _accept_js = f"""
                                        (function() {{
                                            const btns = document.querySelectorAll('{btns_selector}');
                                            if (!btns[{accept_idx}]) return 'ERROR: button not found';
                                            btns[{accept_idx}].click();
                                            return 'OK';
                                        }})();
                                    """
                                    click_result = (
                                        cdp_eval_on(mc_conn, _accept_js)
                                        if mc_conn
                                        else cdp_eval(_accept_js)
                                    )
                                    if click_result and str(click_result).strip() == 'OK':
                                        print(
                                            f'[command-rules] Auto-accepted: {text} -> {accept_label}'
                                        )
                                        if not muted and cid:
                                            if png:
                                                tg_send_photo_bytes(
                                                    cid,
                                                    png,
                                                    filename='auto_accept.png',
                                                    caption=f'✅ Auto: {text}',
                                                    message_thread_id=thr,
                                                )
                                            else:
                                                tg_send(
                                                    cid, f'✅ Auto: {text}', message_thread_id=thr
                                                )
                                        with pending_confirms_lock:
                                            pending_confirms.pop(cb_key, None)
                                        if sec_key:
                                            forwarded_ids.add(sec_key)
                                        section_stable.pop(sec_key, None)
                                        continue
                                    else:
                                        print(
                                            f'[command-rules] Auto-accept click failed ({click_result}), falling back to keyboard'
                                        )

                            if not muted:
                                tg_typing(cid, message_thread_id=thr)
                                png = None
                                if sec_selector:
                                    png = cdp_screenshot_element(sec_selector)
                                keyboard = []
                                for btn in buttons:
                                    cb_line = f'btn_{btn["index"]}:{cb_key}'
                                    if len(cb_line.encode('utf-8')) > 64:
                                        print(
                                            f'[monitor] ERROR: callback_data too long for Telegram ({len(cb_line)}): {cb_line!r}'
                                        )
                                    keyboard.append(
                                        [
                                            {
                                                'text': btn['label'],
                                                'callback_data': cb_line,
                                            }
                                        ]
                                    )
                                if png:
                                    print(
                                        f'[monitor] Forwarding CONFIRMATION with keyboard: {text}'
                                    )
                                    tg_send_photo_bytes_with_keyboard(
                                        cid,
                                        png,
                                        keyboard,
                                        filename='confirmation.png',
                                        caption=f'⚡ {text}',
                                        message_thread_id=thr,
                                    )
                                else:
                                    print(f'[monitor] Forwarding CONFIRMATION as text: {text}')
                                    _cm: dict[str, Any] = {
                                        'chat_id': cid,
                                        'text': f'⚡ {text}',
                                        'reply_markup': {'inline_keyboard': keyboard},
                                    }
                                    if thr is not None:
                                        _cm['message_thread_id'] = thr
                                    _r_cm = _tg_send_message_forum_aware(_cm)
                                    _tg_maybe_unpin_outbound(cid, _r_cm)

                        elif not muted:
                            # Only send to Telegram when not muted
                            tg_typing(cid, message_thread_id=thr)
                            if sec_type in ('table', 'file_edit', 'code_block', 'latex'):
                                file_path = None
                                if sec_type == 'file_edit':
                                    fn_sel = (
                                        sec.get('filename_selector')
                                        if isinstance(sec, dict)
                                        else None
                                    )
                                    if fn_sel:
                                        file_path = cdp_hover_file_path(fn_sel)
                                        if file_path:
                                            print(f'[monitor] File path: {file_path}')
                                png = None
                                expanded = False
                                if sec_selector:
                                    expanded = cdp_try_expand(sec_selector)
                                    png = cdp_screenshot_element(sec_selector)
                                    if expanded:
                                        cdp_try_collapse(sec_selector)
                                if not png and sec_type == 'table':
                                    png = cdp_screenshot_element(
                                        '.composer-human-ai-pair-container:last-child [data-message-role="ai"] .markdown-table-container'
                                    )
                                label = {
                                    'table': 'TABLE',
                                    'file_edit': 'FILE_EDIT',
                                    'code_block': 'CODE_BLOCK',
                                    'latex': 'LATEX',
                                }[sec_type]
                                if sec_type == 'file_edit':
                                    stat = sec.get('file_stat', '') if isinstance(sec, dict) else ''
                                    display = file_path or text
                                    if file_path and stat:
                                        display = f'{file_path} {stat}'
                                    caption = f'📝 {display}'
                                else:
                                    caption = ''
                                if png:
                                    print(
                                        f'[monitor] Forwarding section {i + 1} as {label} screenshot ({len(png)} bytes)'
                                    )
                                    tg_send_photo_bytes(
                                        cid,
                                        png,
                                        filename=f'{sec_type}.png',
                                        caption=caption,
                                        message_thread_id=thr,
                                    )
                                else:
                                    print(
                                        f'[monitor] {label} screenshot failed, sending as text ({len(text)} chars)'
                                    )
                                    prefix = '📝 ' if sec_type == 'file_edit' else ''
                                    display_text = (
                                        (file_path or text) if sec_type == 'file_edit' else text
                                    )
                                    tg_send(cid, f'{prefix}{display_text}', message_thread_id=thr)
                            elif sec_type == 'thinking':
                                print(f'[monitor] Forwarding THINKING ({len(text)} chars)')
                                tg_send_thinking(cid, text, message_thread_id=thr)
                            else:
                                # Check for [PHONE_OUTBOX:filename] marker
                                outbox_match = OUTBOX_MARKER_RE.search(text)
                                if outbox_match:
                                    outbox_filename = outbox_match.group(1).strip()
                                    caption = OUTBOX_MARKER_RE.sub('', text).strip()
                                    # Wait up to 15s for the file to appear
                                    outbox_file = phone_outbox / outbox_filename
                                    print(f'[monitor] Outbox marker: waiting for {outbox_filename}')
                                    deadline = time.time() + 15
                                    while not outbox_file.exists() and time.time() < deadline:
                                        time.sleep(1)
                                    if outbox_file.exists():
                                        outbox_render_and_send(
                                            outbox_filename,
                                            cid,
                                            caption=caption,
                                            message_thread_id=thr,
                                        )
                                    else:
                                        print(
                                            f'[monitor] Outbox file not found after 15s: {outbox_filename}'
                                        )
                                        if caption:
                                            tg_send(cid, caption, message_thread_id=thr)
                                else:
                                    print(
                                        f'[monitor] Forwarding section {i + 1} ({len(text)} chars)'
                                    )
                                    tg_send(cid, text, message_thread_id=thr)

                        # Always advance tracking — muted sections are "silently consumed"
                        if sec_key:
                            forwarded_ids.add(sec_key)
                        sent_this_turn = True
                        print(
                            f'[monitor]   → [{i}] {sec_type:12s}  id={short_id(sec_key)}  ids={len(forwarded_ids)}'
                        )
                        section_stable.pop(sec_key, None)

                    # Build prev_by_id for next tick's stability comparison
                    prev_by_id = {}
                    for sec in sections:
                        if isinstance(sec, dict) and sec.get('id'):
                            prev_by_id[sec['id']] = sec.get('text', '')

                    # Mark turn as done when AI finishes (for tracking)
                    if sent_this_turn and not marked_done:
                        _done_js = """
                            (function() { return !!document.querySelector('[data-stop-button="true"]'); })();
                        """
                        is_gen = cdp_eval_on(mc_conn, _done_js) if mc_conn else cdp_eval(_done_js)
                        if not is_gen:
                            print(f'[monitor] AI done — {len(forwarded_ids)} sections forwarded')
                            marked_done = True

                finally:
                    st['last_turn_id'] = last_turn_id
                    st['last_conv'] = last_conv
                    st['last_mc_pcid'] = last_mc_pcid
                    st['last_iid'] = last_iid
                    st['forwarded_ids'] = forwarded_ids
                    st['sent_this_turn'] = sent_this_turn
                    st['prev_by_id'] = prev_by_id
                    st['section_stable'] = section_stable
                    st['initialized'] = initialized
                    st['marked_done'] = marked_done

        except Exception as e:
            print(f'[monitor] Error: {e}')
            time.sleep(2)


# ── Overview thread ──────────────────────────────────────────────────────────
#
# Active chat detection is event-driven via chat_detection.py:
# click/focusin events → JS detectChat() → __pc_report binding → Python callback.
# See docs/active-chat-detection-plan.md for full design.
#
# This thread handles instance lifecycle (new/closed/workspace changes)
# and periodic conversation scans (new/closed/renamed chats).

SCAN_INTERVAL = 3  # seconds between full rescans
SCAN_VERBOSE = False  # True = log every chat per scan (fingerprint details)
CONTEXT_MONITOR_THRESHOLD = 85


def overview_thread():
    """Periodically rescan CDP targets. Detect new/closed Cursor instances."""
    global ws, active_instance_id, mirrored_chats
    print('[overview] Starting instance monitor...')
    if not mirrored_chats and active_instance_id and active_instance_id in instance_registry:
        info = instance_registry[active_instance_id]
        for pc_id, conv in info.get('convs', {}).items():
            if conv['active']:
                if FORUM_CHAT_ID is not None and not str(pc_id).startswith('pc-'):
                    _fp = _norm_msg_fp(conv.get('msg_id'))
                    _rk = _ensure_forum_topic_for_cursor_chat(
                        active_instance_id, pc_id, conv['name'], _fp
                    )
                    if _rk is None:
                        pr = _primary_route()
                        if pr:
                            with mirrored_chats_lock:
                                mirrored_chats[pr] = _mirror_row(
                                    active_instance_id,
                                    pc_id,
                                    conv['name'],
                                    _fp,
                                )
                            _persist_mirrored_routes()
                else:
                    pr = _primary_route()
                    if pr:
                        with mirrored_chats_lock:
                            mirrored_chats[pr] = _mirror_row(
                                active_instance_id,
                                pc_id,
                                conv['name'],
                                _norm_msg_fp(conv.get('msg_id')),
                            )
                        _persist_mirrored_routes()
                break
    _overview_start = time.time()
    _scan_cycle = 0
    _cdp_miss_count = 0
    _HEARTBEAT_CYCLES = 85  # ~10 min (3s sleep + ~4s scan ≈ 7s per cycle)
    while True:
        try:
            time.sleep(SCAN_INTERVAL)
            _scan_cycle += 1

            if _scan_cycle % _HEARTBEAT_CYCLES == 0:
                uptime_s = int(time.time() - _overview_start)
                h, m = uptime_s // 3600, (uptime_s % 3600) // 60
                n_inst = len(instance_registry)
                n_chats = sum(len(info.get('convs', {})) for info in instance_registry.values())
                print(
                    f'[overview] heartbeat: {n_inst} instances, {n_chats} chats, uptime {h}h{m:02d}m'
                )

            port = detect_cdp_port(exit_on_fail=False)
            if port is None:
                _cdp_miss_count += 1
                if _cdp_miss_count == 1:
                    print('[overview] CDP port unavailable, will keep retrying...')
                continue
            if _cdp_miss_count > 0:
                print(f'[overview] CDP port recovered after {_cdp_miss_count} missed cycles')
                _cdp_miss_count = 0
            current = cdp_list_instances(port)
            current_ids = {inst['id'] for inst in current}
            known_ids = set(instance_registry.keys())

            for inst in current:
                if inst['id'] not in known_ids:
                    label = inst['workspace'] or '(no workspace)'
                    is_detach = inst['workspace'] and any(
                        info['workspace'] == inst['workspace']
                        for info in instance_registry.values()
                    )
                    try:
                        conn = websocket.create_connection(inst['ws_url'])
                        listener_conn = _setup_chat_listener(inst['id'], inst['ws_url'], label)
                        with cdp_lock:
                            instance_registry[inst['id']] = {
                                'workspace': inst['workspace'],
                                'title': inst['title'],
                                'ws': conn,
                                'ws_url': inst['ws_url'],
                                'listener_ws': listener_conn,
                                'convs': {},
                            }
                        if is_detach:
                            print(f'[overview] Detached window: {label}  [{inst["id"][:8]}]')
                        else:
                            print(f'[overview] Opened: {label}  [{inst["id"][:8]}]')
                            if chat_id and not muted and inst['workspace']:
                                _tg_notify_all_routes(f'📂 Workspace opened: {label}')
                    except Exception as e:
                        print(f'[overview] Failed to connect to {label}: {e}')

            for iid in known_ids - current_ids:
                with cdp_lock:
                    info = instance_registry.pop(iid, None)
                    if info:
                        is_active = iid == active_instance_id
                        if is_active and instance_registry:
                            new_id = next(
                                (k for k, v in instance_registry.items() if v['workspace']),
                                next(iter(instance_registry)),
                            )
                            active_instance_id = new_id
                            ws = instance_registry[new_id]['ws']
                        elif is_active:
                            active_instance_id = None
                            ws = None
                if info:
                    label = info['workspace'] or '(no workspace)'
                    try:
                        info['ws'].close()
                    except Exception:
                        pass
                    try:
                        info.get('listener_ws', None) and info['listener_ws'].close()
                    except Exception:
                        pass
                    is_merge = info['workspace'] and any(
                        v['workspace'] == info['workspace'] for v in instance_registry.values()
                    )
                    if is_merge:
                        print(f'[overview] Window merged: {label}  [{iid[:8]}]')
                    else:
                        print(f'[overview] Closed: {label}  [{iid[:8]}]')
                        if chat_id and not muted:
                            _tg_notify_all_routes(f'📂 Workspace closed: {label}')
                    if is_active and active_instance_id:
                        new_name = (
                            instance_registry[active_instance_id]['workspace'] or '(no workspace)'
                        )
                        print(f'[overview] Active switched to: {new_name}')
                        if chat_id and not muted and not is_merge:
                            _tg_notify_all_routes(f'📂 Workspace activated: {new_name}')

            # Detect workspace changes (e.g. user picked a workspace in empty instance)
            for inst in current:
                if inst['id'] in instance_registry:
                    old = instance_registry[inst['id']]
                    if old['workspace'] != inst['workspace'] and inst['workspace']:
                        with cdp_lock:
                            old['workspace'] = inst['workspace']
                            old['title'] = inst['title']
                        print(
                            f'[overview] Workspace opened: {inst["workspace"]}  [{inst["id"][:8]}]'
                        )
                        if chat_id and not muted:
                            _tg_notify_all_routes(f'📂 Workspace opened: {inst["workspace"]}')

            # Reconnect dead listeners
            for iid, info in list(instance_registry.items()):
                if info.get('listener_dead'):
                    label = info.get('workspace') or '(no workspace)'
                    try:
                        old_ws = info.get('listener_ws')
                        if old_ws:
                            try:
                                old_ws.close()
                            except Exception:
                                pass
                        listener_conn = _setup_chat_listener(iid, info['ws_url'], label)
                        info['listener_ws'] = listener_conn
                        info.pop('listener_dead', None)
                        print(f'[overview] Listener reconnected: {label}')
                    except Exception as e:
                        print(f'[overview] Listener reconnect failed for {label}: {e}')

            # Scan conversations per instance. Each CDP target gets its own scan
            # because detached windows have distinct DOMs despite sharing a workspace name.
            scan_summary = {}
            rename_digest = []
            for iid, info in list(instance_registry.items()):
                ws_name = info['workspace']
                if not ws_name:
                    continue
                try:
                    convs = list_chats(lambda js, c=info['ws']: cdp_eval_on(c, js))
                except Exception:
                    continue

                current_convs = {c['pc_id']: c for c in convs}
                known_convs = info['convs']
                ws_label = ws_name.removesuffix(' (Workspace)')
                if SCAN_VERBOSE:
                    for c in convs:
                        mid = c.get('msg_id', '-')
                        mid_short = mid[:12] if mid and mid != '-' else '-'
                        print(
                            f'[overview] fingerprint: {c["pc_id"]}  msg={mid_short:14s}  "{c["name"]}"  in {ws_label}'
                        )

                # Fingerprint-scoring: match disappeared↔appeared entries
                disappeared = set(known_convs) - set(current_convs)
                appeared = set(current_convs) - set(known_convs)

                if disappeared or appeared:
                    d_names = {pid: known_convs[pid]['name'] for pid in disappeared}
                    a_names = {pid: current_convs[pid]['name'] for pid in appeared}
                    print(
                        f'[overview] diff: disappeared={d_names}  appeared={a_names}  in {ws_label}'
                    )

                if disappeared and appeared:
                    scores = {}  # (appeared_id, disappeared_id) → score
                    for a_id in appeared:
                        a = current_convs[a_id]
                        for d_id in disappeared:
                            d = known_convs[d_id]
                            score = 0
                            a_mid = a.get('msg_id')
                            d_mid = d.get('msg_id')
                            if a_mid and d_mid and a_mid == d_mid:
                                score += 3
                            if a.get('name') == d.get('name'):
                                score += 1
                            if score > 0:
                                scores[(a_id, d_id)] = score

                    if scores:
                        print(f'[overview] scores: {scores}  in {ws_label}')
                    else:
                        print(f'[overview] scores: EMPTY (no matches found)  in {ws_label}')

                    matched_a = set()
                    matched_d = set()
                    for (a_id, d_id), score in sorted(scores.items(), key=lambda x: -x[1]):
                        if a_id in matched_a or d_id in matched_d:
                            continue
                        # Check for ambiguity: is there another pair with the same score for this d_id?
                        rivals = [
                            s
                            for (ai, di), s in scores.items()
                            if di == d_id and ai != a_id and s == score
                        ]
                        if rivals:
                            print(
                                f'[overview] Ambiguous match for "{known_convs[d_id]["name"]}" (score={score}, {len(rivals) + 1} candidates) — skipping  in {ws_label}'
                            )
                            continue
                        mid_info = (
                            f'msg={current_convs[a_id].get("msg_id", "-")[:12]}'
                            if current_convs[a_id].get('msg_id')
                            else 'msg=-'
                        )
                        print(
                            f'[overview] Linked: {d_id} -> {a_id}  score={score}  {mid_info}  "{known_convs[d_id]["name"]}"  in {ws_label}'
                        )
                        known_convs[a_id] = known_convs.pop(d_id)
                        _nm = (current_convs[a_id].get('name') or '').strip()
                        _migrate_mirrored_pc_id(iid, d_id, a_id, _nm)
                        matched_a.add(a_id)
                        matched_d.add(d_id)

                    disappeared -= matched_d
                    appeared -= matched_a

                for pc_id in appeared:
                    print(
                        f'[overview] New conversation: {current_convs[pc_id]["name"]}  in {ws_label}'
                    )
                    if FORUM_CHAT_ID is not None and not pc_id.startswith('pc-'):
                        _nm = (current_convs[pc_id].get('name') or '').strip()
                        _fp = _norm_msg_fp(current_convs[pc_id].get('msg_id'))
                        _ensure_forum_topic_for_cursor_chat(iid, pc_id, _nm, _fp)

                for pc_id in disappeared:
                    print(
                        f'[overview] Conversation closed: {known_convs[pc_id]["name"]}  in {ws_label}'
                    )

                for pc_id, conv in current_convs.items():
                    if pc_id in known_convs and known_convs[pc_id]['name'] != conv['name']:
                        old_name = known_convs[pc_id]['name']
                        print(
                            f'[overview] Conversation renamed: {old_name} → {conv["name"]}  in {ws_label}'
                        )
                        rename_digest.append((old_name, conv['name'], ws_label))

                info['convs'] = {
                    pc_id: {'name': c['name'], 'active': c['active'], 'msg_id': c.get('msg_id')}
                    for pc_id, c in current_convs.items()
                }
                scan_summary[ws_label] = scan_summary.get(ws_label, 0) + len(convs)

            if rename_digest and chat_id and not muted:
                lines = [f'• {o} → {n}  ({w})' for o, n, w in rename_digest[:35]]
                if len(rename_digest) > 35:
                    lines.append(f'… +{len(rename_digest) - 35} more')
                _tg_notify_all_routes('💬 Renamed this scan:\n' + '\n'.join(lines))

            if scan_summary:
                parts = '  '.join(f'{ws} ({n})' for ws, n in scan_summary.items())
                print(f'[overview] chat scan: {parts}')

        except BaseException as e:
            if isinstance(e, (KeyboardInterrupt, SystemExit)):
                print(f'[overview] Caught {type(e).__name__} — overview thread staying alive')
            else:
                print(f'[overview] Error: {e}')
            time.sleep(5)


# ── Phone outbox renderer ─────────────────────────────────────────────────────

_render_local = os.environ.get('RENDER_LOCAL_DIR', '').strip()
if _render_local:
    MD_TO_IMAGE_SCRIPT = Path(_render_local) / 'md_to_image.mjs'
    if MD_TO_IMAGE_SCRIPT.exists():
        print(f'[outbox] Using local render: {MD_TO_IMAGE_SCRIPT}')
    else:
        print(
            f'[outbox] WARNING: RENDER_LOCAL_DIR set but {MD_TO_IMAGE_SCRIPT} not found. Run setup_local_render.py'
        )
        MD_TO_IMAGE_SCRIPT = Path(__file__).parent / 'md_to_image.mjs'
else:
    MD_TO_IMAGE_SCRIPT = Path(__file__).parent / 'md_to_image.mjs'
OUTBOX_MARKER_RE = re.compile(r'\[PHONE_OUTBOX:([^\]]+)\]')
phone_outbox.mkdir(exist_ok=True)


def outbox_render_and_send(filename, cid, caption=None, message_thread_id=None):
    """Render an outbox file and send it to Telegram. Returns True on success.

    Width convention: 'name.w800.md' → render at 800px. Default 450px.
    """
    f = phone_outbox / filename
    if not f.is_file():
        return False

    ext = f.suffix.lower()
    png_bytes = None

    if ext == '.md':
        # Parse optional width from filename: name.w800.md → 800
        width_match = re.search(r'\.w(\d+)\.md$', f.name, re.IGNORECASE)
        width_args = ['--width', width_match.group(1)] if width_match else []

        png_path = f.with_suffix('.png')
        try:
            result = sp.run(
                ['node', str(MD_TO_IMAGE_SCRIPT), str(f), '--out', str(png_path)] + width_args,
                capture_output=True,
                text=True,
                encoding='utf-8',
                errors='replace',
                timeout=60,
            )
            if result.returncode != 0:
                print(f'[outbox] Render failed: {result.stderr.strip()}')
                return False
            png_bytes = png_path.read_bytes()
        except Exception as e:
            print(f'[outbox] Render error: {e}')
            return False
        finally:
            try:
                f.unlink(missing_ok=True)
                png_path.unlink(missing_ok=True)
            except Exception:
                pass

    elif ext in ('.png', '.jpg', '.jpeg', '.gif', '.webp'):
        try:
            png_bytes = f.read_bytes()
            f.unlink()
        except Exception as e:
            print(f'[outbox] Read error: {e}')
            return False

    if png_bytes:
        tg_send_photo_bytes(
            cid,
            png_bytes,
            filename=f'{f.stem}.png',
            caption=caption,
            message_thread_id=message_thread_id,
        )
        print(
            f'[outbox] Sent {filename} ({len(png_bytes)} bytes)'
            + (f' with caption ({len(caption)} chars)' if caption else '')
        )
        return True
    return False


# ── Main ─────────────────────────────────────────────────────────────────────

# Single-instance guard: prevent multiple bridge processes
_lock_file = Path(__file__).parent / '.bridge.lock'


def _is_process_alive(pid):
    """Check if another bridge process is still running (single-instance guard)."""
    if sys.platform != 'win32':
        try:
            os.kill(pid, 0)
        except OSError:
            return False
        return True
    import ctypes

    kernel32 = ctypes.windll.kernel32
    handle = kernel32.OpenProcess(0x0400 | 0x1000, False, pid)
    if not handle:
        # Cannot query — assume alive so we never start a second bridge by mistake
        # (duplicate bridges = every Telegram message repeated N times).
        print(
            f'[bridge] Could not query PID {pid}; if no bridge is running, delete {_lock_file} and retry.'
        )
        return True
    try:
        exit_code = ctypes.c_ulong()
        kernel32.GetExitCodeProcess(handle, ctypes.byref(exit_code))
        return exit_code.value == 259  # STILL_ACTIVE
    finally:
        kernel32.CloseHandle(handle)


def _check_single_instance():
    """Ensure only one bridge process is running. Uses a PID lock file."""
    if _lock_file.exists():
        try:
            old_pid = int(_lock_file.read_text().strip())
            if _is_process_alive(old_pid):
                print(f'ERROR: Bridge is already running (PID {old_pid}).')
                print(f'Kill it first: taskkill /PID {old_pid} /F')
                sys.exit(1)
            # Process is dead, stale lock file — proceed
        except (ValueError, OSError):
            pass  # Corrupt or stale lock file, proceed
    # Write our PID
    _lock_file.write_text(str(os.getpid()))


def _cleanup_lock():
    try:
        if _lock_file.exists() and _lock_file.read_text().strip() == str(os.getpid()):
            _lock_file.unlink()
    except OSError:
        pass


atexit.register(_cleanup_lock)

_check_single_instance()

print('Checking bot identity...')
me = tg_call('getMe')
if not me.get('ok'):
    print('ERROR: Cannot reach Telegram API')
    sys.exit(1)
bot = me['result']
print(f'Bot: @{bot["username"]} ({bot["first_name"]})')

# Set bot description if not already configured (shown to new users above the START button)
_desc = tg_call('getMyDescription')
if not _desc.get('result', {}).get('description'):
    tg_call(
        'setMyDescription',
        description='Your Cursor IDE, in your pocket.\n\nTap START to pair. Your conversations then flow both ways between Cursor and Telegram.',
    )
_short = tg_call('getMyShortDescription')
if not _short.get('result', {}).get('short_description'):
    tg_call('setMyShortDescription', short_description='Cursor IDE ↔ Telegram bridge')

print('Connecting to Cursor via CDP...')
cdp_connect()
print('Connected.')
if FORUM_CHAT_ID is not None:
    print(
        f'Forum auto-topics: TELEGRAM_FORUM_CHAT_ID={FORUM_CHAT_ID} '
        '(bot must be admin with “Manage topics”)'
    )

print('\nPocketCursor Bridge v2 running!')
print(f'Send a message to @{bot["username"]} on Telegram.')
print(
    '[bridge] If every message appears 2–5×, stop duplicate python.exe processes '
    '(run restart_pocket_cursor.py or Task Manager → end tasks running pocket_cursor.py).'
)
if OWNER_ID:
    print(f'Owner: {OWNER_ID}')
if chat_id:
    print(f'Chat ID: {chat_id} (restored from previous session)')
if muted:
    print('Status: PAUSED (restored from previous session)')

# Check if bot commands need updating and ask the user
if chat_id and tg_commands_need_update():
    print('[telegram] Command menu is outdated, asking user to update...')
    tg_ask_command_update(chat_id)

print('Press Ctrl+C to stop.\n')

t1 = threading.Thread(target=sender_thread, daemon=True)
t2 = threading.Thread(target=monitor_thread, daemon=True)
t3 = threading.Thread(target=overview_thread, daemon=True)
t1.start()
t2.start()
t3.start()

try:
    while True:
        time.sleep(1)
except KeyboardInterrupt:
    print('\nStopping...')
    for info in instance_registry.values():
        try:
            info['ws'].close()
        except Exception:
            pass
    print('Done.')
