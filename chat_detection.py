"""Chat detection for Cursor IDE via Chrome DevTools Protocol.

Provides event-driven active chat detection (click/focusin -> callback)
and chat enumeration. All DOM knowledge about Cursor's chat UI lives here.

Exports:
    install_chat_listener(ws_conn) -- inject JS listener + __pc_report binding
    start_chat_listener(ws_conn, label, on_switch, on_rename) -- daemon thread
    list_chats(ws_conn) -- enumerate all open chats [{pc_id, name, active, agents_group?}]
        Glass Agents window rows may include agents_group (sidebar section label).

See _active_chat_detection_plan.md for design rationale and DOM analysis.
"""

import builtins
import json
import threading
from datetime import datetime
from typing import Any


def ts_print(*args, **kwargs):
    ts = datetime.now().strftime('%H:%M:%S.%f')[:-3]
    kwargs.setdefault('flush', True)
    builtins.print(f'[{ts}]', *args, **kwargs)


print = ts_print

# ── CDP helpers (per-connection, no global state) ─────────────────────────────

_lock = threading.Lock()
_msg_counters: dict[Any, int] = {}  # ws_conn -> int


def _next_id(ws_conn):
    with _lock:
        _msg_counters[ws_conn] = _msg_counters.get(ws_conn, 0) + 1
        return _msg_counters[ws_conn]


def _cdp_send(ws_conn, method, params=None):
    mid = _next_id(ws_conn)
    ws_conn.send(json.dumps({'id': mid, 'method': method, 'params': params or {}}))
    return mid


def _cdp_call(ws_conn, method, params=None):
    mid = _cdp_send(ws_conn, method, params)
    while True:
        r = json.loads(ws_conn.recv())
        if r.get('id') == mid:
            return r


def _cdp_eval(ws_conn, js):
    r = _cdp_call(ws_conn, 'Runtime.evaluate', {'expression': js, 'returnByValue': True})
    return r.get('result', {}).get('result', {}).get('value')


# ── Listener JS (injected into each Cursor window) ───────────────────────────
#
# Reports ALL click/focusin events (not just switches) for debugging.
# Python side filters for switches/renames and logs everything.

_LISTENER_JS = r"""
(function() {
    if (window.__pc_handler) {
        document.removeEventListener('click', window.__pc_handler, true);
        document.removeEventListener('focusin', window.__pc_handler, true);
    }
    document.querySelectorAll('[data-pc-id]').forEach(el => el.removeAttribute('data-pc-id'));
    let lastPcId = null;

    function isGlassAgentsUi() {
        if (document.body && document.body.getAttribute('data-cursor-glass-mode') === 'true') return true;
        return !!document.querySelector('nav[data-component="workspace-sidebar"] .glass-sidebar-agent-menu-btn');
    }

    function getComposerId() {
        if (isGlassAgentsUi()) {
            const bar = document.querySelector('[data-component="agent-panel"] .composer-bar[data-composer-id]');
            const cid = bar && bar.getAttribute('data-composer-id');
            return (cid && /^[0-9a-f]{8}-/.test(cid)) ? cid : '';
        }
        const el = document.querySelector('.composite.auxiliarybar[data-composer-id]')
                || document.querySelector('.composer-bar[data-composer-id]');
        const cid = el && el.getAttribute('data-composer-id');
        return (cid && /^[0-9a-f]{8}-/.test(cid)) ? cid : '';
    }

    function cidFromUuid(uuid) {
        return 'cid-' + uuid.substring(0, 8);
    }

    function tagWithCid(el, cid) {
        const pcId = cidFromUuid(cid);
        el.setAttribute('data-pc-id', pcId);
        return pcId;
    }

    function ensurePcId(el) {
        if (el.getAttribute('data-pc-id')) return el.getAttribute('data-pc-id');
        const resName = el.getAttribute('data-resource-name') || '';
        if (/^[0-9a-f]{8}-/.test(resName)) return tagWithCid(el, resName);
        const cid = getComposerId();
        if (cid) return tagWithCid(el, cid);
        const pcId = 'pc-' + Math.random().toString(36).slice(2, 10);
        el.setAttribute('data-pc-id', pcId);
        return pcId;
    }

    function findNearestChat(el) {
        if (isGlassAgentsUi()) {
            const tiptap = el.closest('.ui-prompt-input-editor__input');
            if (tiptap && tiptap.getAttribute('contenteditable') === 'true') {
                const ap = document.querySelector('[data-component="agent-panel"]');
                if (ap && ap.contains(tiptap)) return tiptap;
            }
        }
        const inputs = [...document.querySelectorAll('[data-lexical-editor="true"][contenteditable="true"]')];
        if (!inputs.length) return null;
        const container = el.closest('.editor-group-container') || el.closest('[class*="auxiliarybar"]');
        if (!container) return null;
        const contained = inputs.filter(inp => container.contains(inp));
        if (contained.length === 1) return contained[0];
        return null;
    }

    function extractChatInfo(input) {
        if (isGlassAgentsUi()) {
            const ap = document.querySelector('[data-component="agent-panel"]');
            if (ap && ap.contains(input) && input.classList && input.classList.contains('ProseMirror')) {
                const activeBtn = document.querySelector(
                    'nav[data-component="workspace-sidebar"] .glass-sidebar-agent-menu-btn[data-active="true"]');
                if (activeBtn) {
                    const nameEl = activeBtn.querySelector('.glass-sidebar-agent-menu-label__name');
                    const name = nameEl ? nameEl.textContent.trim() : '';
                    if (name) {
                        const cid = getComposerId();
                        const pcId = cid ? tagWithCid(activeBtn, cid) : ensurePcId(activeBtn);
                        return { name: name, pc_id: pcId };
                    }
                }
            }
        }
        const composerEl = input.closest('[data-composer-id]');
        const cid = composerEl ? composerEl.getAttribute('data-composer-id') : '';
        const hasCid = cid && /^[0-9a-f]{8}-/.test(cid);

        const egc = input.closest('.editor-group-container');
        if (egc) {
            const tab = egc.querySelector('.tab.selected .composer-tab-label')
                       || egc.querySelector('.tab.active .composer-tab-label')
                       || egc.querySelector('.tab .composer-tab-label');
            const tabEl = tab && tab.closest('.tab');
            if (tab && tabEl) {
                const resName = tabEl.getAttribute('data-resource-name') || '';
                const pcId = /^[0-9a-f]{8}-/.test(resName) ? tagWithCid(tabEl, resName) : (hasCid ? tagWithCid(tabEl, cid) : ensurePcId(tabEl));
                return { name: tab.textContent.trim(), pc_id: pcId };
            }
        }
        const li = document.querySelector('[class*="agent-tabs"] li.checked');
        if (li) {
            const a = li.querySelector('a[aria-id="chat-horizontal-tab"]');
            if (a) {
                const pcId = hasCid ? tagWithCid(li, cid) : ensurePcId(li);
                return { name: a.getAttribute('aria-label') || a.textContent.trim(), pc_id: pcId };
            }
        }
        const unifiedCell = document.querySelector('.unified-agents-sidebar .agent-sidebar-cell[data-selected="true"]');
        if (unifiedCell) {
            const textEl = unifiedCell.querySelector('.agent-sidebar-cell-text');
            const name = textEl ? textEl.textContent.trim() : '';
            if (name) {
                const pcId = hasCid ? tagWithCid(unifiedCell, cid) : ensurePcId(unifiedCell);
                return { name: name, pc_id: pcId };
            }
        }
        return null;
    }

    function findTabByName(name) {
        for (const a of document.querySelectorAll('[class*="agent-tabs"] li a[aria-id="chat-horizontal-tab"]')) {
            const tabName = a.getAttribute('aria-label') || a.textContent.trim();
            if (tabName === name) {
                const li = a.closest('li');
                return li ? ensurePcId(li) : '';
            }
        }
        for (const cell of document.querySelectorAll('.unified-agents-sidebar .agent-sidebar-cell')) {
            if (cell.getAttribute('data-selected') === null) continue;
            const textEl = cell.querySelector('.agent-sidebar-cell-text');
            const tabName = textEl ? textEl.textContent.trim() : '';
            if (tabName === name) {
                return ensurePcId(cell);
            }
        }
        for (const label of document.querySelectorAll('.tab .composer-tab-label')) {
            if (label.textContent.trim() === name) {
                const tabEl = label.closest('.tab');
                return tabEl ? ensurePcId(tabEl) : '';
            }
        }
        if (isGlassAgentsUi()) {
            for (const btn of document.querySelectorAll(
                'nav[data-component="workspace-sidebar"] .glass-sidebar-agent-menu-btn')) {
                const nameEl = btn.querySelector('.glass-sidebar-agent-menu-label__name');
                const tabName = nameEl ? nameEl.textContent.trim() : '';
                if (tabName === name) return ensurePcId(btn);
            }
        }
        return '';
    }

    function detectChat(el) {
        const pcEl = el.closest('[data-pc-id]');
        if (pcEl) {
            const label = pcEl.querySelector('.composer-tab-label')
                       || pcEl.querySelector('a[aria-id="chat-horizontal-tab"]')
                       || pcEl.querySelector('.agent-sidebar-cell-text')
                       || pcEl.querySelector('.glass-sidebar-agent-menu-label__name');
            if (label) return { name: label.getAttribute('aria-label') || label.textContent.trim(), pc_id: pcEl.getAttribute('data-pc-id') };
        }
        const glassBtn = el.closest('.glass-sidebar-agent-menu-btn');
        if (glassBtn && isGlassAgentsUi()) {
            const nameEl = glassBtn.querySelector('.glass-sidebar-agent-menu-label__name');
            const name = nameEl ? nameEl.textContent.trim() : '';
            if (name) {
                let pcId = glassBtn.getAttribute('data-pc-id');
                if (!pcId) {
                    const cid = getComposerId();
                    pcId = (glassBtn.getAttribute('data-active') === 'true' && cid)
                        ? tagWithCid(glassBtn, cid) : ensurePcId(glassBtn);
                }
                return { name: name, pc_id: pcId };
            }
        }
        if (isGlassAgentsUi()) {
            const tiptap = el.closest('.ui-prompt-input-editor__input');
            if (tiptap && tiptap.getAttribute('contenteditable') === 'true') {
                const ap = document.querySelector('[data-component="agent-panel"]');
                if (ap && ap.contains(tiptap)) {
                    const chat = extractChatInfo(tiptap);
                    if (chat) return chat;
                }
            }
        }
        const chatIcon = el.closest('.codicon-chat');
        if (chatIcon) {
            const name = chatIcon.getAttribute('aria-label') || (chatIcon.querySelector('.label-name') || {}).textContent;
            if (name) {
                const pcId = findTabByName(name.trim());
                if (pcId) return { name: name.trim(), pc_id: pcId };
                let hash = 0;
                for (let i = 0; i < name.length; i++) hash = ((hash << 5) - hash + name.charCodeAt(i)) | 0;
                return { name: name.trim(), pc_id: 'ext-' + Math.abs(hash).toString(36) };
            }
        }
        const input = findNearestChat(el);
        if (input) return extractChatInfo(input);
        return null;
    }

    function conversationFingerprintFromComposer() {
        let composerPanel = null;
        if (isGlassAgentsUi()) {
            composerPanel = document.querySelector('[data-component="agent-panel"] .composer-messages-container');
        }
        if (!composerPanel) {
            composerPanel = document.querySelector('.composite.auxiliarybar .composer-messages-container')
                || document.querySelector('.auxiliarybar .composer-messages-container');
        }
        if (composerPanel) {
            const humans = composerPanel.querySelectorAll('[data-message-kind="human"][data-message-id]');
            if (humans.length) {
                return humans[humans.length - 1].getAttribute('data-message-id');
            }
            const anyMsg = composerPanel.querySelectorAll('[data-message-id]');
            if (anyMsg.length) {
                return anyMsg[anyMsg.length - 1].getAttribute('data-message-id');
            }
        }
        const cid = getComposerId();
        return (cid && /^[0-9a-f]{8}-/.test(cid)) ? ('cid:' + cid) : null;
    }

    function attachMsgFingerprint(chat) {
        const fp = conversationFingerprintFromComposer();
        if (fp) chat.msg_id = fp;
    }

    let lastChatName = null;

    function report(evType, el, chat, sw, rn) {
        const cls = (el.className && typeof el.className === 'string') ? el.className.substring(0, 120) : '';
        try { __pc_report(JSON.stringify({
            type: evType,
            tag: el.tagName || '?',
            cls: cls,
            text: (el.textContent || '').substring(0, 60).trim(),
            chat: chat,
            sw: sw,
            rn: rn || false
        })); } catch(err) {}
    }

    function handler(e) {
        const el = e.target;
        const chat = detectChat(el);

        if (chat && chat.pc_id && chat.pc_id !== lastPcId) {
            lastPcId = chat.pc_id;
            lastChatName = chat.name;
            attachMsgFingerprint(chat);
            report(e.type, el, chat, true);
            return;
        }

        if (chat && chat.pc_id && chat.name && chat.name !== lastChatName) {
            lastChatName = chat.name;
            report(e.type, el, chat, false, true);
            return;
        }

        if (chat && !chat.pc_id) {
            requestAnimationFrame(() => {
                const cid = getComposerId();
                if (cid) {
                    chat.pc_id = cidFromUuid(cid);
                    const tabEl = el.closest('li.composite-bar-action-tab') || el.closest('.tab');
                    if (tabEl) tabEl.setAttribute('data-pc-id', chat.pc_id);
                }
                if (chat.pc_id && chat.pc_id !== lastPcId) {
                    lastPcId = chat.pc_id;
                    lastChatName = chat.name;
                    attachMsgFingerprint(chat);
                    report(e.type, el, chat, true);
                } else {
                    report(e.type, el, chat, false);
                }
            });
            return;
        }

        report(e.type, el, chat, false);
    }

    window.__pc_handler = handler;
    document.addEventListener('click', handler, true);
    document.addEventListener('focusin', handler, true);

    // When window gains OS focus, reset lastPcId so the NEXT click/focusin
    // on a chat element triggers a switch — even if it's the same chat that
    // was active before. This detects cross-instance switches without
    // guessing which chat the user wants (waits for intent).
    if (window.__pc_focus_handler) window.removeEventListener('focus', window.__pc_focus_handler);
    window.__pc_focus_handler = function() { lastPcId = null; lastChatName = null; };
    window.addEventListener('focus', window.__pc_focus_handler);

    return 'INSTALLED';
})()
"""

# ── List chats JS (unified cid-{uuid[:8]} scheme) ────────────────────────────

_LIST_CHATS_JS = r"""
(function() {
    const results = [];

    function cidFromUuid(uuid) {
        return 'cid-' + uuid.substring(0, 8);
    }

    function tagWithCid(el, uuid) {
        const pcId = cidFromUuid(uuid);
        el.setAttribute('data-pc-id', pcId);
        return pcId;
    }

    function isGlassAgentsUi() {
        if (document.body && document.body.getAttribute('data-cursor-glass-mode') === 'true') return true;
        return !!document.querySelector('nav[data-component="workspace-sidebar"] .glass-sidebar-agent-menu-btn');
    }

    function getComposerId() {
        if (isGlassAgentsUi()) {
            const bar = document.querySelector('[data-component="agent-panel"] .composer-bar[data-composer-id]');
            const cid = bar && bar.getAttribute('data-composer-id');
            return (cid && /^[0-9a-f]{8}-/.test(cid)) ? cid : '';
        }
        const el = document.querySelector('.composite.auxiliarybar[data-composer-id]')
                || document.querySelector('.composer-bar[data-composer-id]');
        const cid = el && el.getAttribute('data-composer-id');
        return (cid && /^[0-9a-f]{8}-/.test(cid)) ? cid : '';
    }

    function conversationFingerprintForPanel(container) {
        if (container) {
            const humans = container.querySelectorAll('[data-message-kind="human"][data-message-id]');
            if (humans.length) {
                return humans[humans.length - 1].getAttribute('data-message-id');
            }
            const anyMsg = container.querySelectorAll('[data-message-id]');
            if (anyMsg.length) {
                return anyMsg[anyMsg.length - 1].getAttribute('data-message-id');
            }
        }
        const cid = getComposerId();
        return (cid && /^[0-9a-f]{8}-/.test(cid)) ? ('cid:' + cid) : null;
    }

    // Deterministic id when Cursor has no stable uuid (replaces Math.random so IDs survive sidebar rebuilds
    // and stay consistent across list_chats runs for the same visible row + title).
    function stablePcId(kind, idx, label) {
        const s = kind + ':' + String(idx) + ':' + (label || '');
        let h = 2166136261;
        for (let i = 0; i < s.length; i++) {
            h ^= s.charCodeAt(i);
            h = Math.imul(h, 16777619);
        }
        return 'pc-' + kind + (h >>> 0).toString(36).slice(0, 11);
    }

    const usedPcIds = new Set();

    // 1. Editor-group tabs first (stable cids from data-resource-name)
    document.querySelectorAll('.editor-group-container').forEach(group => {
        const tabs = group.querySelectorAll('.tab .composer-tab-label');
        tabs.forEach((label, tidx) => {
            const tabEl = label.closest('.tab');
            if (!tabEl) return;
            const name = label.textContent.trim();
            const resName = tabEl.getAttribute('data-resource-name') || '';
            let pcId = tabEl.getAttribute('data-pc-id');
            if (!pcId || !pcId.startsWith('cid-')) {
                if (/^[0-9a-f]{8}-/.test(resName)) {
                    pcId = tagWithCid(tabEl, resName);
                } else if (!pcId) {
                    pcId = stablePcId('e', tidx, name);
                    tabEl.setAttribute('data-pc-id', pcId);
                }
            }
            usedPcIds.add(pcId);
            const entry = {
                pc_id: pcId,
                name: name,
                active: tabEl.classList.contains('active')
            };
            const panel = group.querySelector('.composer-messages-container');
            if (panel && tabEl.classList.contains('active')) {
                const fp = conversationFingerprintForPanel(panel);
                if (fp) entry.msg_id = fp;
            }
            results.push(entry);
        });
    });

    // 2. Agent-tabs: only retag if new cid won't collide with editor-group
    function getActiveComposerMessagesPanel() {
        if (isGlassAgentsUi()) {
            return document.querySelector('[data-component="agent-panel"] .composer-messages-container');
        }
        return document.querySelector('.composite.auxiliarybar .composer-messages-container')
            || document.querySelector('.auxiliarybar .composer-messages-container');
    }
    const composerPanel = getActiveComposerMessagesPanel();
    const activeFingerprint = composerPanel ? conversationFingerprintForPanel(composerPanel) : null;

    const agentTabs = document.querySelectorAll('[class*="agent-tabs"] li[class*="action-item"] a[aria-id="chat-horizontal-tab"]');
    agentTabs.forEach((a, aidx) => {
        const li = a.closest('li');
        if (!li) return;
        let pcId = li.getAttribute('data-pc-id');
        const isChecked = li.classList.contains('checked');
        const tabLabel = a.getAttribute('aria-label') || a.textContent.trim() || '';
        if (isChecked) {
            const cid = getComposerId();
            if (cid) {
                const newPcId = cidFromUuid(cid);
                if (!usedPcIds.has(newPcId)) {
                    pcId = tagWithCid(li, cid);
                }
            }
        }
        if (!pcId || !pcId.startsWith('cid-')) {
            if (!pcId) {
                pcId = stablePcId('t', aidx, tabLabel);
                li.setAttribute('data-pc-id', pcId);
            }
        }
        usedPcIds.add(pcId);
        const entry = {
            pc_id: pcId,
            name: a.getAttribute('aria-label') || a.textContent.trim() || '',
            active: isChecked
        };
        if (isChecked && activeFingerprint) entry.msg_id = activeFingerprint;
        results.push(entry);
    });

    // 3. Unified agents sidebar (new Cursor UI)
    const unifiedCells = document.querySelectorAll('.unified-agents-sidebar .agent-sidebar-cell');
    unifiedCells.forEach((cell, uidx) => {
        if (cell.getAttribute('data-selected') === null) return;
        let pcId = cell.getAttribute('data-pc-id');
        const isChecked = cell.getAttribute('data-selected') === 'true';
        const textEl0 = cell.querySelector('.agent-sidebar-cell-text')
            || cell.querySelector('[class*="sidebar-cell-text"]')
            || cell.querySelector('[class*="cell-text"]');
        let title0 = textEl0 ? textEl0.textContent.trim() : '';
        if (!title0) {
            const al = cell.getAttribute('aria-label');
            if (al) title0 = al.trim();
        }
        if (!title0) {
            title0 = (cell.textContent || '').replace(/\s+/g, ' ').trim().slice(0, 300);
        }

        if (isChecked && activeFingerprint) {
            const cid = getComposerId();
            if (cid) {
                const newPcId = cidFromUuid(cid);
                if (!usedPcIds.has(newPcId)) {
                    pcId = tagWithCid(cell, cid);
                }
            }
        }
        if (!pcId || !pcId.startsWith('cid-')) {
            if (!pcId) {
                pcId = stablePcId('u', uidx, title0);
                cell.setAttribute('data-pc-id', pcId);
            }
        }
        usedPcIds.add(pcId);
        const entry = {
            pc_id: pcId,
            name: title0,
            active: isChecked
        };
        if (isChecked && activeFingerprint) entry.msg_id = activeFingerprint;
        results.push(entry);
    });

    // 4. Agents window (glass): workspace sidebar lists multiple folders / agents
    if (isGlassAgentsUi()) {
        let gidx = 0;
        document.querySelectorAll('nav[data-component="workspace-sidebar"] .ui-sidebar-group').forEach(group => {
            const labelEl = group.querySelector('.ui-sidebar-group-label-text');
            const agentsGroup = labelEl ? labelEl.textContent.trim() : '';
            const container = group.querySelector('.glass-sidebar-agent-list-container');
            if (!container) return;
            container.querySelectorAll('.glass-sidebar-agent-menu-btn').forEach(btn => {
                const nameEl = btn.querySelector('.glass-sidebar-agent-menu-label__name');
                const name = nameEl ? nameEl.textContent.trim() : '';
                if (!name) return;
                const isActive = btn.getAttribute('data-active') === 'true';
                let pcId = btn.getAttribute('data-pc-id');
                if (isActive && activeFingerprint) {
                    const cid = getComposerId();
                    if (cid) {
                        const newPcId = cidFromUuid(cid);
                        if (!usedPcIds.has(newPcId)) {
                            pcId = tagWithCid(btn, cid);
                        }
                    }
                }
                const stableLabel = agentsGroup + '\\0' + name;
                if (!pcId || !pcId.startsWith('cid-')) {
                    if (!pcId) {
                        pcId = stablePcId('g', gidx, stableLabel);
                        btn.setAttribute('data-pc-id', pcId);
                    }
                }
                usedPcIds.add(pcId);
                const entry = {
                    pc_id: pcId,
                    name: name,
                    active: isActive
                };
                if (agentsGroup) entry.agents_group = agentsGroup;
                if (isActive && activeFingerprint) entry.msg_id = activeFingerprint;
                results.push(entry);
                gidx++;
            });
        });
    }

    return JSON.stringify(results);
})()
"""


# ── Public API ────────────────────────────────────────────────────────────────


def install_chat_listener(ws_conn):
    """Install click/focusin listener + __pc_report binding on a CDP connection.

    Must be called before start_chat_listener. Safe to call multiple times
    (JS handler removes old listeners before re-installing).
    """
    _cdp_call(ws_conn, 'Runtime.enable')
    _cdp_call(ws_conn, 'Runtime.addBinding', {'name': '__pc_report'})
    _cdp_call(ws_conn, 'Page.addScriptToEvaluateOnNewDocument', {'source': _LISTENER_JS})
    result = _cdp_eval(ws_conn, _LISTENER_JS)
    return result


def start_chat_listener(ws_conn, label, on_switch, on_rename=None, on_dead=None):
    """Start a daemon thread that listens for chat switch/rename events.

    Logs ALL events for debugging (like _test_composer_focus.py).
    Only triggers callbacks for actual switches and renames.
    on_dead(label, exception) is called when the listener thread exits.
    """

    def _listener():
        try:
            while True:
                raw = ws_conn.recv()
                msg = json.loads(raw)
                if msg.get('method') != 'Runtime.bindingCalled':
                    continue
                if msg.get('params', {}).get('name') != '__pc_report':
                    continue
                try:
                    ev = json.loads(msg['params']['payload'])

                    if ev.get('type') == 'context':
                        pct_val = ev.get('pct', '?')
                        action = ev.get('action', '?')
                        print(f'[context] {pct_val}% -- {action}  [{label}]')
                        continue

                    tag = ev.get('tag', '?')
                    cls = ev.get('cls', '')
                    text = ev.get('text', '')
                    chat = ev.get('chat')
                    is_switch = ev.get('sw', False)
                    is_rename = ev.get('rn', False)
                    ev_type = ev.get('type', '?')

                    cls_short = cls[:80] + '...' if len(cls) > 80 else cls
                    text_short = text[:40] + '...' if len(text) > 40 else text

                    prefix = (
                        '>>> SWITCH' if is_switch else ('>>> RENAME' if is_rename else '          ')
                    )
                    line = f'[dom] {prefix}  {ev_type.upper():8s}  [{label}]  <{tag}> .{cls_short}'
                    if text_short:
                        line += f'\n[dom]               text: "{text_short}"'
                    if chat:
                        line += f'\n[dom]               chat: {chat.get("name", "?")}  (pc_id={chat.get("pc_id", "?")})'
                    print(line)

                    if not chat:
                        continue
                    if is_switch:
                        on_switch(chat)
                    elif is_rename and on_rename:
                        on_rename(chat)
                except (json.JSONDecodeError, KeyError, TypeError):
                    pass
        except Exception as e:
            print(f'[dom] Listener ended: {label} ({e})')
            if on_dead:
                on_dead(label, e)

    t = threading.Thread(target=_listener, name=f'chat-listener-{label}', daemon=True)
    t.start()
    return t


def list_chats(eval_fn):
    """List all open chats on a Cursor instance.

    eval_fn: callable(js_string) -> value, e.g. lambda js: cdp_eval_on(conn, js).
    Returns list of dicts: [{pc_id, name, active, msg_id?}].
    msg_id is a conversation fingerprint: prefer last human message id, else last
    message id in the thread (e.g. AI welcome), else \"cid:\" + full data-composer-id
    when the thread is still empty (no human message required).
    """
    result = eval_fn(_LIST_CHATS_JS)
    try:
        parsed = json.loads(result) if result else []
    except (json.JSONDecodeError, TypeError) as e:
        print(f'[chat_detection] list_chats parse error: {e}')
        raise
    return parsed if isinstance(parsed, list) else []
