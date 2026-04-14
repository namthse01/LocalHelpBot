// Chat bubble rendering + /api/chat streaming loop.

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
    appendMessage('bot', 'Chat cleared. How can I help you today?');
}

async function sendMessage() {
    const input = $id('user-input');
    if (!input) return;
    const text = input.value.trim();
    if (!text) return;

    appendMessage('user', text);
    input.value = '';
    input.style.height = 'auto';

    const panel = createActivityPanel();
    const ctx = { pendingTools: {} };
    let finalText = '';

    try {
        const response = await fetch('/api/chat', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json', 'Accept': 'application/x-ndjson' },
            body: JSON.stringify({
                model: window.currentModel,
                messages: [{ role: 'user', content: text }],
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
        if (finalText) appendMessage('bot', finalText);
        else panel.metaEl.innerHTML = `<i class="fas fa-check text-emerald-400"></i> done`;
    } catch (error) {
        panel.metaEl.innerHTML = `<i class="fas fa-times text-red-400"></i> error`;
        appendMessage('bot', `❌ Error: ${error.message}`);
    }
}
