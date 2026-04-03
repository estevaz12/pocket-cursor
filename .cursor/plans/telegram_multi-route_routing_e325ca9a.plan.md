---
name: Telegram multi-route routing
overview: Add optional multi-tenant Telegram routing so each allowed user (private DM) or each forum topic (supergroup) can bind to its own Cursor workspace chat (`instance_id` + `pc_id`), with outbound mirroring and inbound sends scoped to that route.
todos:
  - id: tdd-baseline
    content: "TDD for all slices: write failing pytest first, confirm RED, minimal GREEN, refactor; no production code without a failing test first (see TDD section)"
    status: pending
  - id: route-model
    content: Introduce RouteKey, mirrored_chats dict, persistence file + migration from .chat_id/.active_chat (tests for serialization + migration before impl)
    status: pending
  - id: auth
    content: Implement TELEGRAM_ALLOWED_USER_IDS (and optional chat allowlist) + update check_owner / pairing flows (tests for allowlist/pairing matrix before impl)
    status: pending
  - id: cdp-ensure-tab
    content: Extract shared CDP tab-switch helper; use from callbacks and cursor_send_message(route) (test via mocked CDP or extracted JS contract where feasible)
    status: pending
  - id: sender
    content: Resolve route from each update (incl. message_thread_id); scope last_sent_* and /chats callbacks per route (tests with fake update dicts before wiring)
    status: pending
  - id: monitor
    content: Refactor monitor_thread to per-route state machines; tg_send/tg_typing with message_thread_id (prefer injectable deps + tests for routing/state isolation)
    status: pending
  - id: confirms-broadcast
    content: Audit pending_confirms, workspace notifications, context monitor for correct route targeting (add/adjust tests when behavior is specified)
    status: pending
  - id: docs-env
    content: Document README + .env.example for multi-user and forum topics
    status: pending
isProject: false
---

# Telegram multi-route → Cursor agent mapping

## Current behavior (baseline)

- **Auth:** Single “owner” ([`TELEGRAM_OWNER_ID`](.env.example) or auto-pair on first message); everyone else is rejected ([`check_owner`](pocket_cursor.py), ~1939).
- **Telegram destination:** One global [`chat_id`](pocket_cursor.py) (`.chat_id` file) used by the monitor for all [`tg_send`](pocket_cursor.py) calls.
- **Cursor side:** One global [`mirrored_chat`](pocket_cursor.py) `(instance_id, pc_id, name)` — the monitor’s [`monitor_thread`](pocket_cursor.py) polls **only** that composer via [`cursor_get_turn_info`](pocket_cursor.py) + [`_composer_prefix_from_pcid`](pocket_cursor.py).
- **Switching:** [`/chats`](pocket_cursor.py) builds inline buttons with `callback_data` `chat:{iid}:{pc_id}`; the handler updates `mirrored_chat` and persists [`.active_chat`](pocket_cursor.py).

Incoming DMs already use per-message `cid`; the gap is persistence, authorization, per-route mirror state, monitor fan-out, and ensuring sends target the correct Cursor tab.

## Test-driven development (mandatory)

Follow **red → green → refactor** for every behavior change: one small failing test, watch it fail for the right reason, then minimal implementation to pass, then cleanup. No production code without a failing test first.

**Extract for testability:** Prefer pure helpers in a small module (e.g. [`lib/`](lib/) or new `lib/telegram_routes.py`) for **route key** equality/serialization, **allowlist** parsing and membership, **migration** from `.chat_id` / `.active_chat` to the new JSON shape, and **Telegram API params** (`message_thread_id` omitted vs set). Cover these with fast unit tests **before** wiring `pocket_cursor.py`.

**Integration-heavy pieces** (monitor loop, CDP): still TDD by **injecting dependencies** (e.g. `get_turn_info`, `tg_send`) or testing extracted state-transition helpers with fake dicts so the suite does not require a live Cursor session. Use [`tests/`](tests/) pytest patterns consistent with [`AGENTS.md`](AGENTS.md) (`npm run validate`).

**Suggested order of test slices (each RED then GREEN):**

1. Route key + JSON persistence + migration from legacy files.
2. Allowlist env parsing + `check_owner` / pairing outcomes for allowed vs denied users (table-driven tests).
3. “Resolve route” from Telegram `message` + optional `message_thread_id`.
4. Per-route monitor state / which `chat_id` + `message_thread_id` get passed to send helpers (mocked).
5. Narrow integration or regression tests for any refactored `cursor_get_turn_info` scoping if logic moves.

## Target behavior

- **Route key:** `(telegram_chat_id, message_thread_id | None)` — `message_thread_id` is required to distinguish [forum topics](https://core.telegram.org/bots/api#message) in a supergroup; private chats omit it (`None`).
- **Mapping:** Each route stores its own `mirrored_chat` tuple (same shape as today).
- **Inbound:** Text/photo/voice from a route uses that route’s mapping; before typing into Cursor, **ensure** the right window + tab is active (reuse the same CDP tab-click logic already used in the [`chat:` callback handler](pocket_cursor.py) ~2106–2196 — ideally extracted to a shared helper).
- **Outbound:** The monitor maintains **independent state per route** (at minimum: `last_turn_id`, `forwarded_ids`, `prev_by_id`, `section_stable`, flags for init/switch) and, when forwarding, calls Telegram APIs with that route’s `chat_id` and, for forums, `message_thread_id` on [`sendMessage`](https://core.telegram.org/bots/api#sendmessage) / `sendChatAction`.
- **Callbacks:** When handling `chat:{iid}:{pc_id}`, derive the route from `callback_query.message` (`chat.id` + optional `message_thread_id`) and update **that** route’s mapping only.

## Configuration and security

- Add something like **`TELEGRAM_ALLOWED_USER_IDS`** (comma-separated Telegram **user** ids). Semantics:
  - **Unset:** Preserve today’s single-owner behavior (auto-pair first user, or fixed `TELEGRAM_OWNER_ID`).
  - **Set:** Only those `user_id`s may use the bot; each establishes/updates their own private route on first `/start` or message (no “one global owner” lock). Reject others with a clear message.
- **Groups / forums:** Document that routing multiple agents in a **group** requires a **forum** supergroup so each topic has a distinct `message_thread_id`. Plain groups without topics cannot separate routes without extra hacks.
- Optional: **`TELEGRAM_ALLOWED_CHAT_IDS`** allowlist for group chat ids if you want to restrict which supergroups may use the bot (bot still only sees messages where [privacy mode](https://core.telegram.org/bots/features#privacy-mode) allows).

## Persistence

- Replace single `.chat_id` / single `.active_chat` with one JSON file (e.g. `.telegram_routes.json`) storing:
  - For each serialized route key: `{ "mirrored": [iid, pc_id, name], ... }` and anything needed for resume.
- Migration: on first run after upgrade, if old `.chat_id` / `.active_chat` exist, import them into a default route (private chat id + `thread_id: null`).

## Code touchpoints (high signal)

| Area                      | File                                   | Change                                                                                                                                                                                                                                                                                          |
| ------------------------- | -------------------------------------- | ----------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| Route key + allowlist     | [`pocket_cursor.py`](pocket_cursor.py) | New helpers; extend env parsing near lines 81–86                                                                                                                                                                                                                                                |
| Global state              | [`pocket_cursor.py`](pocket_cursor.py) | `mirrored_chat` → `mirrored_chats: dict[RouteKey, ...]`; `last_sent_*` per route                                                                                                                                                                                                                |
| Sender thread             | [`pocket_cursor.py`](pocket_cursor.py) | Resolve route from `msg`; pass route into send path; update pairing logic ~2288–2317                                                                                                                                                                                                            |
| Monitor                   | [`pocket_cursor.py`](pocket_cursor.py) | Loop over routes with isolated state dicts; [`tg_call`](pocket_cursor.py) gains optional `message_thread_id`; [`tg_typing`](pocket_cursor.py)/[`tg_send`](pocket_cursor.py) take route or `(cid, thread_id)`                                                                                    |
| Chat detection / overview | [`pocket_cursor.py`](pocket_cursor.py) | Any `tg_send(chat_id, ...)` that notifies workspace events must target all relevant routes or only routes tied to that workspace — **product choice:** simplest is “broadcast system events to every route that has ever mirrored,” or “only the active route(s)”; document the chosen behavior |
| CDP                       | [`pocket_cursor.py`](pocket_cursor.py) | Extract `_ensure_mirrored_tab(iid, pc_id)` from existing callback JS; call from `cursor_send_message` when a route is specified                                                                                                                                                                 |
| Docs                      | [`README.md`](README.md)               | Multi-user + forum topic setup, env vars, privacy mode note                                                                                                                                                                                                                                     |
| Example env               | [`.env.example`](.env.example)         | Document new variables                                                                                                                                                                                                                                                                          |

## Risks and mitigations

- **CDP load:** N routes ⇒ up to N× `cursor_get_turn_info` polls per tick. Start with sequential polling under `cdp_lock` and a conservative sleep; optimize later if needed.
- **Race between users:** Per-route `last_sent_text` avoids cross-talk for Telegram-origin detection.
- **Callback_data 64-byte limit:** Existing `chat:{uuid}:{pc_id}` already fits; no change if route is inferred from the callback **message**, not embedded in `callback_data`.
- **Confirmation buttons / pending_confirms:** If tool confirmations are sent to Telegram, ensure pending state keys include route so replies go to the correct chat/thread (audit [`pending_confirms`](pocket_cursor.py) usage).

## Verification

- **Automated:** New/changed behavior covered by pytest; every commit-ready change passes [`npm run validate`](AGENTS.md) (Ruff, Mypy, pytest with coverage).
- **TDD gate:** For each feature slice, confirm the new test fails before implementation and passes after (no skipping the RED step).
- **Manual (after green suite):** Two Telegram accounts in `TELEGRAM_ALLOWED_USER_IDS`, each `/chats` → pick different Cursor tabs, verify replies stay separated.
- **Manual:** Forum supergroup — two topics, bind each to a different agent, verify `message_thread_id` on send.
