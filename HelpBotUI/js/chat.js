// Chat bubble rendering + /api/chat streaming loop.
//
// ── Session memory ───────────────────────────────────────────────
// Full conversation is kept in `window.chatHistory` for the lifetime
// of the tab. Every /api/chat request resends a sliding window of the
// most recent turns so the model sees prior context. Cleared by
// clearChat(). No persistence — reload = fresh session.
//
//   MEMORY_MAX_MESSAGES : hard cap on turns resent (sliding window)
//   MEMORY_MAX_CHARS    : soft cap on total payload chars (cheap token proxy)
//
// Summary system messages are wrapped with SUMMARY_MARKER so the server
// (core/memory.py) recognises and preserves them across pipeline/
// orchestrator rewrites that would otherwise drop the system prompt.
// ─────────────────────────────────────────────────────────────────
const MEMORY_MAX_MESSAGES      = 30;
const MEMORY_MAX_CHARS         = 24000;
// Tier-1 compression: once history gets long, fold the oldest slice into a
// single `system` summary so context is preserved (not dropped) when the
// sliding window clips. Cost: one extra LLM call, amortized across many turns.
const MEMORY_COMPRESS_TRIGGER  = 24;  // turns before we compress
const MEMORY_KEEP_RECENT       = 12;  // most recent turns always kept verbatim

const SUMMARY_MARKER     = '=== CONVERSATION SUMMARY ===';
const SUMMARY_MARKER_END = '=== END SUMMARY ===';

window.chatHistory = window.chatHistory || [];

function pushHistory(role, content) {
    if (!content) return;
    window.chatHistory.push({ role, content, ts: Date.now() });
}

function _extractPriorSummary() {
    // Find existing summary in history (wrapped or legacy prefix).
    for (const m of window.chatHistory) {
        if (m.role !== 'system') continue;
        const c = m.content || '';
        if (c.includes(SUMMARY_MARKER)) {
            const start = c.indexOf(SUMMARY_MARKER) + SUMMARY_MARKER.length;
            const end = c.indexOf(SUMMARY_MARKER_END, start);
            return (end >= 0 ? c.slice(start, end) : c.slice(start)).trim();
        }
        if (c.startsWith('Prior conversation summary')) {
            return c.replace(/^Prior conversation summary[^:]*:\s*/i, '').trim();
        }
    }
    return '';
}

async function maybeCompressHistory() {
    // Fold oldest turns into a summary system-message when the log grows long.
    // Safe to fail: on error we leave history intact and let the sliding
    // window handle overflow.
    if (window.chatHistory.length <= MEMORY_COMPRESS_TRIGGER) return;
    const cut = window.chatHistory.length - MEMORY_KEEP_RECENT;
    const toSummarize = window.chatHistory.slice(0, cut);
    const keep        = window.chatHistory.slice(cut);
    const priorSummary = _extractPriorSummary();
    // Drop the prior summary from the slice being re-summarized — we pass it
    // as a separate hint so the server extends instead of re-summarizing it.
    const slice = toSummarize.filter(m =>
        !(m.role === 'system' && ((m.content || '').includes(SUMMARY_MARKER)
            || /^Prior conversation summary/i.test(m.content || '')))
    );
    try {
        const resp = await fetch('/api/memory/summarize', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({
                messages: slice.map(m => ({ role: m.role, content: m.content })),
                prior_summary: priorSummary || undefined,
            })
        });
        if (!resp.ok) return;
        const data = await resp.json();
        const summary = (data && data.summary) ? data.summary.trim() : '';
        if (!summary) return;
        // Wrap with marker so server-side helpers recognise it.
        const wrapped = `${SUMMARY_MARKER}\n${summary}\n${SUMMARY_MARKER_END}`;
        window.chatHistory = [
            { role: 'system', content: wrapped, ts: Date.now() },
            ...keep.filter(m =>
                // Drop any stale summary in the kept slice (shouldn't happen,
                // but belt-and-braces).
                !(m.role === 'system' && (m.content || '').includes(SUMMARY_MARKER))
            )
        ];
    } catch (e) { /* keep original history; sliding window still protects */ }
}

// Per-message hard cap — a single giant assistant reply (e.g. a full
// summarized document) must NOT blow the budget and cause the whole turn
// to be dropped. We truncate in place with a visible marker so the model
// still sees the turn happened.
const MEMORY_PER_MSG_MAX_CHARS = 4000;

function _truncateMsg(content) {
    if (!content || content.length <= MEMORY_PER_MSG_MAX_CHARS) return content;
    const head = content.slice(0, MEMORY_PER_MSG_MAX_CHARS);
    const dropped = content.length - MEMORY_PER_MSG_MAX_CHARS;
    return `${head}\n\n…[truncated ${dropped} chars for context window; full reply was shown in chat]`;
}

function buildContextWindow() {
    // Sliding window: most recent N messages, trimmed by char budget.
    // Oversized individual messages are truncated (not dropped), so context
    // is preserved even when one turn is huge.
    const hist = window.chatHistory.slice(-MEMORY_MAX_MESSAGES);
    let budget = MEMORY_MAX_CHARS;
    const out = [];
    for (let i = hist.length - 1; i >= 0; i--) {
        const m = hist[i];
        const content = _truncateMsg(m.content || '');
        const len = content.length;
        if (budget - len < 0 && out.length > 0) break;
        budget -= len;
        out.unshift({ role: m.role, content });
    }
    return out;
}

function setModel(model) {
    window.currentModel = model;
    const select = $id('chat-agent-select');
    if (select) select.value = model;
}

function appendMessage(role, text) {
    const chatWindow = $id('chat-window');
    if (!chatWindow) return;
    const div = document.createElement('div');
    div.className = `flex ${role === 'user' ? 'justify-end' : 'justify-start'} mb-4`;
    const inner = document.createElement('div');
    inner.className = `max-w-[80%] px-4 py-2 ${role === 'user' ? 'message-user' : 'message-bot'} text-sm leading-relaxed`;
    if (text && text.includes('```')) {
        inner.innerHTML = text.replace(/```([\s\S]*?)```/g,
            '<pre class="bg-slate-900 p-2 rounded-md my-2 overflow-x-auto text-xs font-mono text-indigo-300">$1</pre>');
    } else {
        inner.innerText = text || '';
    }
    div.appendChild(inner);
    chatWindow.appendChild(div);
    chatWindow.scrollTop = chatWindow.scrollHeight;
}

function clearChat() {
    const chatWindow = $id('chat-window');
    if (!chatWindow) return;
    chatWindow.innerHTML = '';
    window.chatHistory = [];
    appendMessage('bot', 'Chat cleared. How can I help you today?');
}

async function sendMessage() {
    const input = $id('user-input');
    if (!input) return;
    const text = input.value.trim();
    if (!text) return;

    appendMessage('user', text);
    pushHistory('user', text);
    input.value = '';
    input.style.height = 'auto';

    await maybeCompressHistory();

    const panel = createActivityPanel();
    const ctx = { pendingTools: {} };
    let finalText = '';

    try {
        const response = await fetch('/api/chat', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json', 'Accept': 'application/x-ndjson' },
            body: JSON.stringify({
                model: window.currentModel,
                messages: buildContextWindow(),
                stream: true
            })
        });
        if (!response.ok) throw new Error(`Server Error: ${response.status}`);

        const ct = (response.headers.get('Content-Type') || '').toLowerCase();
        if (!response.body || !ct.includes('ndjson')) {
            const data = await response.json();
            panel.metaEl.innerHTML = `<i class="fas fa-check text-emerald-400"></i> done`;
            appendMessage('bot', (data.message && data.message.content) || JSON.stringify(data));
            return;
        }

        const reader = response.body.getReader();
        const decoder = new TextDecoder('utf-8');
        let buf = '';
        while (true) {
            const { value, done } = await reader.read();
            if (done) break;
            buf += decoder.decode(value, { stream: true });
            let idx;
            while ((idx = buf.indexOf('\n')) >= 0) {
                const line = buf.slice(0, idx).trim();
                buf = buf.slice(idx + 1);
                if (!line) continue;
                let frame;
                try { frame = JSON.parse(line); } catch (e) { continue; }
                if (frame.agent_event) renderAgentEvent(panel, frame.agent_event, ctx);
                if (frame.done && frame.message && frame.message.content) {
                    finalText = frame.message.content;
                }
            }
        }
        if (finalText) {
            appendMessage('bot', finalText);
            pushHistory('assistant', finalText);
        } else {
            panel.metaEl.innerHTML = `<i class="fas fa-check text-emerald-400"></i> done`;
        }
    } catch (error) {
        panel.metaEl.innerHTML = `<i class="fas fa-times text-red-400"></i> error`;
        appendMessage('bot', `❌ Error: ${error.message}`);
    }
}
