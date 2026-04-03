"""Telegram route keys: (chat_id, message_thread_id) for forum topic routing."""

from __future__ import annotations

import json
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
    return {
        'workspace': binding.get('workspace'),
        'pc_id': binding.get('pc_id'),
        'chat_name': binding.get('chat_name'),
    }


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
        out[rk] = {
            'workspace': v.get('workspace'),
            'pc_id': v.get('pc_id'),
            'chat_name': v.get('chat_name'),
        }
    return out


def save_routes_json(path: Path, routes: Mapping[RouteKey, Mapping[str, Any]]) -> None:
    """Write routes file (atomic replace via same path)."""
    payload = {
        'version': ROUTES_FILE_VERSION,
        'routes': {rk.to_storage_key(): dict(route_binding_to_json(v)) for rk, v in routes.items()},
    }
    path.write_text(json.dumps(payload, indent=2, ensure_ascii=False) + '\n', encoding='utf-8')


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
