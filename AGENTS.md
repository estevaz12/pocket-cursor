# Agent notes (PocketCursor)

This repo is the **PocketCursor** Telegram bridge for Cursor IDE. It is mostly Python (`pocket_cursor.py`, `chat_detection.py`, `lib/`) plus Node for markdown-to-image (Puppeteer).

## Quick validation (from repo root)

After `pip install -r requirements.txt -r requirements-dev.txt` and `npm install`:

- `npm run validate` — Ruff lint, Ruff format check, Mypy, pytest with coverage
- `npm run test` — pytest only
- `npm run lint` / `npm run typecheck` — individual steps

Install test fixtures: `cd tests && npm ci` (jsdom). Without them, some tests skip.

## Running the bridge

Requires `.env` with `TELEGRAM_BOT_TOKEN`, Cursor with CDP (`python start_cursor.py` or documented flags), then `python -X utf8 pocket_cursor.py`. After changing files here, restart the bridge with `python restart_pocket_cursor.py` when testing runtime behavior.

## Layout

- `pocket_cursor.py` — main bridge
- `chat_detection.py` — CDP chat UI integration
- `lib/command_rules.json` — hot-reloaded command auto-accept rules
- `tests/` — pytest + jsdom DOM tests

## Pre-commit

Optional: `pip install pre-commit && pre-commit install` to run Ruff before each commit.
