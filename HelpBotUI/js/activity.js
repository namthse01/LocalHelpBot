// Streaming "Agent activity" panel: compact collapsible rows for tool_call events.

function createActivityPanel() {
    const chatWindow = $id('chat-window');
    const wrap = document.createElement('div');
    wrap.className = 'flex justify-start mb-4';
    const panel = document.createElement('div');
    panel.className = 'activity-panel max-w-[90%] w-full';
    panel.innerHTML = `
        <div class="activity-header">
            <span><i class="fas fa-cogs mr-1"></i>Agent activity</span>
            <span class="activity-meta"><span class="spinner"></span> <span class="activity-status">working…</span></span>
        </div>
        <div class="activity-rows"></div>`;
    wrap.appendChild(panel);
    if (chatWindow) {
        chatWindow.appendChild(wrap);
        chatWindow.scrollTop = chatWindow.scrollHeight;
    }
    return {
        rowsEl:   panel.querySelector('.activity-rows'),
        statusEl: panel.querySelector('.activity-status'),
        metaEl:   panel.querySelector('.activity-meta'),
        wrap,
    };
}

function shortArg(args) {
    if (!args || typeof args !== 'object') return '';
    const keys = ['path', 'file_path', 'file', 'filename', 'url', 'query', 'pattern', 'command', 'name'];
    for (const k of keys) {
        if (args[k]) {
            const v = String(args[k]);
            const base = v.split(/[\\/]/).pop();
            return base.length > 60 ? base.slice(0, 57) + '…' : base;
        }
    }
    return '';
}

function addCompactRow(panel, iconHtml, labelHtml, detailText) {
    const row = document.createElement('div');
    row.className = 'activity-row compact';
    const hasDetail = detailText && String(detailText).trim().length > 0;
    row.innerHTML = `
        <div class="activity-icon">${iconHtml}</div>
        <div class="activity-body">
            ${hasDetail ? '<span class="activity-chev">▶</span>' : '<span style="display:inline-block;width:14px"></span>'}
            ${labelHtml}
        </div>`;
    panel.rowsEl.appendChild(row);
    const detail = document.createElement('div');
    detail.className = 'activity-detail';
    detail.textContent = detailText || '';
    panel.rowsEl.appendChild(detail);
    if (hasDetail) row.addEventListener('click', () => row.classList.toggle('open'));
    const chatWindow = $id('chat-window');
    if (chatWindow) chatWindow.scrollTop = chatWindow.scrollHeight;
    return { row, detail };
}

function renderAgentEvent(panel, ev, ctx) {
    if (!ev || !ev.type) return;

    if (ev.type === 'status') {
        panel.statusEl.textContent = ev.text || 'working…';
        return;
    }

    if (ev.type === 'agent_start') {
        addCompactRow(panel,
            '<i class="fas fa-play text-indigo-400"></i>',
            `<span class="muted">agent</span> <span class="tool-name">${escapeHtml(ev.agent)}</span> <span class="muted">started</span>`,
            '');
        return;
    }

    if (ev.type === 'thought') {
        if (!ctx.thoughtPair) {
            ctx.thoughtStart = Date.now();
            ctx.thoughtPair = addCompactRow(panel,
                '<i class="fas fa-lightbulb text-amber-400"></i>',
                `<span class="tool-name">Thinking…</span>`,
                ev.text || '');
        } else {
            ctx.thoughtPair.detail.textContent += (ev.text || '');
        }
        return;
    }

    if (ev.type === 'tool_call') {
        if (ctx.thoughtPair) {
            const secs = Math.max(1, Math.round((Date.now() - ctx.thoughtStart) / 1000));
            const name = ctx.thoughtPair.row.querySelector('.activity-body .tool-name');
            if (name) name.textContent = `Thought for ${secs}s`;
            ctx.thoughtPair = null;
        }
        let argsJson = '';
        try { argsJson = JSON.stringify(ev.args || {}, null, 2); } catch (e) { argsJson = String(ev.args); }
        const chip = shortArg(ev.args);
        const pair = addCompactRow(panel,
            '<i class="fas fa-bolt text-sky-400"></i>',
            `<span class="tool-name">${escapeHtml(ev.tool)}</span>` +
            (chip ? `<span class="activity-arg-chip">${escapeHtml(chip)}</span>` : '') +
            ` <span class="muted running"><span class="spinner"></span> running…</span>`,
            argsJson);
        ctx.pendingTools[ev.id] = pair;
        return;
    }

    if (ev.type === 'tool_result') {
        const pair = ctx.pendingTools[ev.id];
        const preview = ev.preview || '';
        const cls = ev.ok ? 'ok' : 'err';
        const icon = ev.ok ? '✓' : '✗';
        if (pair) {
            const running = pair.row.querySelector('.activity-body .running');
            if (running) running.outerHTML = `<span class="${cls}">${icon}</span>`;
            if (preview) pair.detail.textContent = preview;
        } else {
            addCompactRow(panel,
                `<span class="${cls}">${icon}</span>`,
                `<span class="tool-name">${escapeHtml(ev.tool || 'tool')}</span>`,
                preview);
        }
        return;
    }

    if (ev.type === 'done') {
        panel.metaEl.innerHTML = `<i class="fas fa-check text-emerald-400"></i> done ${ev.total_ms || 0}ms · ${escapeHtml(ev.active || '')}`;
        return;
    }
}
