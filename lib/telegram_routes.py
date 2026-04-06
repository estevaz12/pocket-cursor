"""Telegram route keys: (chat_id, message_thread_id) for forum topic routing."""

from __future__ import annotations

import json
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping

ROUTES_FILE_VERSION = 1


@dataclass(frozen=True, order=True)
class RouteKey:
    """Telegram destination: private/supergroup chat + optional forum topic thread."""

    chat_id: int
    message_thread_id: int | None

    def to_storage_key(self) -> str:
        tid = 'null' if self.message_thread_id is None else str(self.message_thread_id)
        return f'{self.chat_id}:{tid}'

    @staticmethod
    def from_storage_key(s: str) -> RouteKey:
        cid_str, tid_str = s.split(':', 1)
        cid = int(cid_str)
        if tid_str == 'null':
            return RouteKey(cid, None)
        return RouteKey(cid, int(tid_str))


def route_key_from_message(msg: Mapping[str, Any]) -> RouteKey:
    """Build RouteKey from a Telegram Message or callback_query.message object."""
    chat = msg.get('chat')
    if not isinstance(chat, Mapping):
        raise ValueError('message has no chat')
    cid = chat.get('id')
    if cid is None:
        raise ValueError('chat has no id')
    tid = msg.get('message_thread_id')
    if tid is not None:
        tid = int(tid)
    return RouteKey(int(cid), tid)


def route_binding_to_json(binding: Mapping[str, Any]) -> dict[str, Any]:
    """Normalize a persisted route value to JSON-serializable dict."""
    out: dict[str, Any] = {
        'workspace': binding.get('workspace'),
        'pc_id': binding.get('pc_id'),
        'chat_name': binding.get('chat_name'),
    }
    mid = binding.get('msg_id')
    if mid:
        out['msg_id'] = mid
    return out


def load_routes_json(path: Path) -> dict[RouteKey, dict[str, Any]]:
    """Load `.telegram_routes.json`. Returns route -> {workspace, pc_id, chat_name}."""
    if not path.exists():
        return {}
    raw = json.loads(path.read_text(encoding='utf-8'))
    if not isinstance(raw, dict):
        return {}
    out: dict[RouteKey, dict[str, Any]] = {}
    for k, v in raw.get('routes', {}).items():
        if not isinstance(k, str) or not isinstance(v, dict):
            continue
        try:
            rk = RouteKey.from_storage_key(k)
        except ValueError:
            continue
        row: dict[str, Any] = {
            'workspace': v.get('workspace'),
            'pc_id': v.get('pc_id'),
            'chat_name': v.get('chat_name'),
        }
        if v.get('msg_id'):
            row['msg_id'] = v['msg_id']
        out[rk] = row
    return out


def save_routes_json(path: Path, routes: Mapping[RouteKey, Mapping[str, Any]]) -> None:
    """Write routes file (atomic replace via same path)."""
    payload = {
        'version': ROUTES_FILE_VERSION,
        'routes': {rk.to_storage_key(): dict(route_binding_to_json(v)) for rk, v in routes.items()},
    }
    path.write_text(json.dumps(payload, indent=2, ensure_ascii=False) + '\n', encoding='utf-8')


def canonical_outbound_route(
    candidates: list[RouteKey],
    *,
    forum_chat_id: int | None,
    last_sender: RouteKey | None,
) -> RouteKey:
    """When several Telegram routes share the same Cursor mirror, pick one for outbound sends.

    Prefer ``last_sender`` if it is among the candidates (matches the topic the user used).
    Otherwise prefer a real forum thread over the General row (``message_thread_id is None``).
    """
    if not candidates:
        raise ValueError('candidates must be non-empty')
    if last_sender is not None and last_sender in candidates:
        return last_sender
    if forum_chat_id is not None:
        threaded = [c for c in candidates if c.message_thread_id is not None]
        if threaded:
            return min(threaded, key=lambda r: r.message_thread_id or 0)
    return candidates[0]


def group_routes_by_mirror(
    items: Sequence[tuple[RouteKey, Any]],
    *,
    forum_chat_id: int | None,
    last_sender: RouteKey | None,
) -> list[tuple[RouteKey, Any, list[RouteKey]]]:
    """One row per Cursor composer (same ``instance_id`` + ``pc_id``).

    RouteKeys that share ``(iid, pc_id)`` but differ in stored ``chat_name`` (e.g. one
    row still says \"New Agent\" after a rename) must stay one group — otherwise the
    monitor forwards the same turn to multiple Telegram topics.
    """
    by_comp: dict[tuple[str, str], list[tuple[RouteKey, Any]]] = {}
    for rk, mc in items:
        if not isinstance(mc, (tuple, list)) or len(mc) < 3:
            continue
        iid, pc_id, _nm = mc[0], mc[1], mc[2]
        by_comp.setdefault((iid, pc_id), []).append((rk, mc))
    out: list[tuple[RouteKey, Any, list[RouteKey]]] = []
    for _key, pairs in by_comp.items():
        rks = [p[0] for p in pairs]
        mc = max(pairs, key=lambda p: len(p[1][2]))[1]
        canon = canonical_outbound_route(rks, forum_chat_id=forum_chat_id, last_sender=last_sender)
        out.append((canon, mc, rks))
    return out


def routes_for_global_bot_notify(
    route_keys: list[RouteKey],
    *,
    forum_chat_id: int | None,
    last_sender: RouteKey | None,
) -> list[RouteKey]:
    """Where to send workspace / scan system lines (not per-agent Cursor output).

    Use **one** forum topic plus any non-forum routes (e.g. DM). Broadcasting the same
    line to every forum topic duplicates it in Telegram's \"All topics\" view.
    """
    if forum_chat_id is None:
        return route_keys
    non_forum = [rk for rk in route_keys if rk.chat_id != forum_chat_id]
    forum_threaded = [
        rk for rk in route_keys if rk.chat_id == forum_chat_id and rk.message_thread_id is not None
    ]
    if forum_threaded:
        if last_sender is not None and last_sender in forum_threaded:
            return non_forum + [last_sender]
        return non_forum + [min(forum_threaded, key=lambda r: r.message_thread_id or 0)]
    forum_general = [
        rk for rk in route_keys if rk.chat_id == forum_chat_id and rk.message_thread_id is None
    ]
    return non_forum + (forum_general[:1] if forum_general else [])


def normalize_mirror_chat_name(name: str) -> str:
    """Normalize chat title for comparing mirror rows (whitespace + case-insensitive)."""
    return ' '.join((name or '').split()).strip().casefold()


def forum_topic_title(chat_name: str, pc_id: str) -> str:
    """Build a Telegram forum topic name (max 128 chars, single line)."""
    base = ' '.join((chat_name or '').split()).strip() or 'Chat'
    suffix = ''
    if pc_id and pc_id.startswith('cid-') and len(pc_id) > 8:
        suffix = f' ({pc_id[4:12]})'
    max_base = max(1, 128 - len(suffix))
    base = base[:max_base]
    out = (base + suffix)[:128]
    return out if out else 'Chat'


def migrate_legacy_route_files(
    chat_id_file: Path,
    active_chat_file: Path,
) -> dict[RouteKey, dict[str, Any]]:
    """Build route map from `.chat_id` + `.active_chat` (pre–multi-route)."""
    if not chat_id_file.exists():
        return {}
    try:
        cid = int(chat_id_file.read_text(encoding='utf-8').strip())
    except (ValueError, OSError):
        return {}
    rk = RouteKey(cid, None)
    if not active_chat_file.exists():
        return {rk: {'workspace': None, 'pc_id': None, 'chat_name': None}}
    try:
        saved = json.loads(active_chat_file.read_text(encoding='utf-8'))
    except (json.JSONDecodeError, OSError):
        return {rk: {'workspace': None, 'pc_id': None, 'chat_name': None}}
    return {
        rk: {
            'workspace': saved.get('workspace'),
            'pc_id': saved.get('pc_id'),
            'chat_name': saved.get('chat_name'),
        }
    }
