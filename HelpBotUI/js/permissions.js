// permissions.js — polls /api/permissions/pending and renders a per-tool
// approval modal. Slice 2 of the upgrade plan: the server now ships a
// structured `preview` payload with `kind`, and a `risk` level. We pick
// the renderer based on `preview.kind`.

const _handledPerms = new Set();

const RISK_STYLE = {
    low:    { color: '#22c55e', icon: 'fa-shield-check', label: 'Low risk'    },
    medium: { color: '#f59e0b', icon: 'fa-shield-halved', label: 'Medium risk' },
    high:   { color: '#ef4444', icon: 'fa-triangle-exclamation', label: 'HIGH risk' },
};

async function pollPermissions() {
    try {
        const r = await fetch('/api/permissions/pending');
        if (!r.ok) return;
        const { pending } = await r.json();
        for (const p of pending) {
            if (_handledPerms.has(p.id)) continue;
            _handledPerms.add(p.id);
            showPermissionModal(p);
        }
    } catch (e) { /* silent */ }
}

function showPermissionModal(p) {
    if (document.getElementById('perm-modal-' + p.id)) return;
    const wrap = document.createElement('div');
    wrap.id = 'perm-modal-' + p.id;
    wrap.style.cssText =
        'position:fixed;inset:0;background:rgba(0,0,0,0.6);z-index:9999;' +
        'display:flex;align-items:center;justify-content:center;';

    const risk = RISK_STYLE[p.risk] || RISK_STYLE.medium;
    const previewHtml = renderPreview(p.preview || { kind: 'generic', text: '' }, p);

    wrap.innerHTML = `
        <div style="background:#1e293b;border:1px solid #334155;border-left:4px solid ${risk.color};border-radius:12px;padding:24px;max-width:680px;width:92%;color:#e2e8f0;max-height:88vh;overflow:auto">
            <div style="display:flex;align-items:center;gap:12px;margin-bottom:6px">
                <i class="fas ${risk.icon}" style="color:${risk.color};font-size:20px"></i>
                <h3 style="font-size:18px;font-weight:600">Agent requests permission: <span style="color:#fbbf24">${escapeHtml(p.tool)}</span></h3>
            </div>
            <div style="font-size:11px;color:${risk.color};margin-bottom:14px;text-transform:uppercase;letter-spacing:.05em;font-weight:600">${risk.label}</div>
            <div style="font-size:13px;color:#cbd5e1;line-height:1.6">${previewHtml}</div>
            <div style="display:flex;gap:8px;justify-content:flex-end;margin-top:20px;flex-wrap:wrap">
                <button data-act="deny"    class="px-4 py-2 rounded-lg bg-slate-700  hover:bg-slate-600  text-sm">Deny</button>
                <button data-act="once"    class="px-4 py-2 rounded-lg bg-indigo-600 hover:bg-indigo-500 text-sm">Allow once</button>
                <button data-act="session" class="px-4 py-2 rounded-lg bg-indigo-600 hover:bg-indigo-500 text-sm">Allow for session</button>
                <button data-act="always"  class="px-4 py-2 rounded-lg bg-emerald-600 hover:bg-emerald-500 text-sm">Always (this tool)</button>
            </div>
        </div>`;

    wrap.querySelectorAll('button').forEach(btn => {
        btn.onclick = async () => {
            const act = btn.dataset.act;
            const approved = act !== 'deny';
            const scope = act === 'session' ? 'session'
                        : act === 'always'  ? 'always-this-tool'
                        : 'once';
            await fetch('/api/permissions/resolve', {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({ id: p.id, approved, scope })
            });
            wrap.remove();
        };
    });
    document.body.appendChild(wrap);
}

function renderPreview(preview, p) {
    const k = preview.kind;
    if (k === 'diff')    return previewDiff(preview);
    if (k === 'command') return previewCommand(preview);
    if (k === 'exec')    return previewExec(preview);
    if (k === 'write')   return previewWrite(preview);
    if (k === 'delete')  return previewDelete(preview);
    if (k === 'move')    return previewMove(preview);
    if (k === 'install') return previewInstall(preview);
    if (k === 'kill')    return previewKill(preview);
    if (k === 'url')     return previewUrl(preview);
    return previewGeneric(p.details, preview);
}

function previewDiff(p) {
    const diff = (p.diff || '').split('\n').map(line => {
        let color = '#cbd5e1';
        if (line.startsWith('+')) color = '#22c55e';
        else if (line.startsWith('-')) color = '#ef4444';
        else if (line.startsWith('@@')) color = '#60a5fa';
        return `<span style="color:${color}">${escapeHtml(line)}</span>`;
    }).join('\n');
    return `
        <div><strong>Path:</strong> <code>${escapeHtml(p.path || '')}</code>
             ${p.replace_all ? '<span style="color:#f59e0b;font-size:11px"> · replace_all</span>' : ''}</div>
        <div style="margin-top:10px"><strong>Diff:</strong>
          <pre style="max-height:280px;overflow:auto;background:#0f172a;padding:10px;border-radius:6px;font-family:'Cascadia Code',ui-monospace,monospace;font-size:12px;white-space:pre;line-height:1.5">${diff || '(no preview)'}</pre>
        </div>`;
}

function previewCommand(p) {
    return `
        <div><strong>Command:</strong></div>
        <pre style="background:#0a0a0a;color:#a3e635;padding:10px;border-radius:6px;font-family:'Cascadia Code',ui-monospace,monospace;font-size:12px;white-space:pre-wrap;margin-top:6px">$ ${escapeHtml(p.command || '')}</pre>
        ${p.cwd ? `<div style="margin-top:6px"><strong>CWD:</strong> <code>${escapeHtml(p.cwd)}</code></div>` : ''}`;
}

function previewExec(p) {
    return `
        <div><strong>Python snippet</strong> · ${p.lines || '?'} lines · timeout ${p.timeout || '?'}s</div>
        <pre style="max-height:260px;overflow:auto;background:#0f172a;padding:10px;border-radius:6px;font-family:'Cascadia Code',ui-monospace,monospace;font-size:12px;white-space:pre-wrap;margin-top:6px;color:#c7d2fe">${escapeHtml(p.code || '')}</pre>`;
}

function previewWrite(p) {
    return `
        <div><strong>Path:</strong> <code>${escapeHtml(p.path || '')}</code></div>
        <div><strong>Size:</strong> ${p.bytes ?? '?'} chars</div>
        <div style="margin-top:8px"><strong>Preview:</strong>
          <pre style="max-height:200px;overflow:auto;background:#0f172a;padding:8px;border-radius:4px;font-size:11px;color:#94a3b8;white-space:pre-wrap">${escapeHtml(p.preview || '')}</pre>
        </div>`;
}

function previewDelete(p) {
    return `
        <div style="color:#fca5a5"><strong>About to delete ${p.is_dir ? 'directory' : 'file'}:</strong></div>
        <code style="display:block;background:#0f172a;padding:8px;border-radius:4px;margin-top:6px">${escapeHtml(p.path || '')}</code>
        ${p.size != null ? `<div style="margin-top:6px;font-size:11px;color:#94a3b8">${p.size} bytes</div>` : ''}`;
}

function previewMove(p) {
    return `
        <div><strong>From:</strong> <code>${escapeHtml(p.src || '')}</code></div>
        <div style="margin-top:4px"><strong>To:</strong> <code>${escapeHtml(p.dst || '')}</code></div>
        ${p.overwrite ? '<div style="color:#fbbf24;margin-top:6px">⚠ Will overwrite existing destination</div>' : ''}`;
}

function previewInstall(p) {
    return `
        <div><strong>Package:</strong> <code>${escapeHtml(p.package || '')}</code></div>
        <div style="margin-top:4px"><strong>Reason:</strong> ${escapeHtml(p.reason || '(none)')}</div>
        ${p.command ? `<pre style="background:#0a0a0a;color:#a3e635;padding:8px;border-radius:6px;font-size:11px;margin-top:8px">$ ${escapeHtml(p.command)}</pre>` : ''}`;
}

function previewKill(p) {
    return `
        <div style="color:#fca5a5"><strong>Terminate process:</strong></div>
        <code style="display:block;background:#0f172a;padding:8px;border-radius:4px;margin-top:6px">${escapeHtml(p.label || ('pid=' + p.pid))}</code>`;
}

function previewUrl(p) {
    return `
        <div><strong>Host:</strong> <code style="color:#fbbf24">${escapeHtml(p.host || '(unknown)')}</code></div>
        <div style="margin-top:6px"><strong>URL:</strong> <code style="word-break:break-all">${escapeHtml(p.url || '')}</code></div>`;
}

function previewGeneric(details, p) {
    if (p.text) return `<div>${escapeHtml(p.text)}</div>`;
    return `<pre style="background:#0f172a;padding:8px;border-radius:4px;font-size:11px;color:#94a3b8;white-space:pre-wrap">${escapeHtml(JSON.stringify(details, null, 2))}</pre>`;
}
