// Polls /api/permissions/pending and shows an approval modal for risky tools.

const _handledPerms = new Set();

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

    const details = p.tool === 'write_file'
        ? `<div><strong>Path:</strong> <code>${escapeHtml(p.details.path)}</code></div>
           <div><strong>Size:</strong> ${p.details.bytes} bytes</div>
           <div style="margin-top:8px"><strong>Preview:</strong>
             <pre style="max-height:200px;overflow:auto;background:#0f172a;padding:8px;border-radius:4px;font-size:11px;color:#94a3b8;white-space:pre-wrap">${escapeHtml(p.details.preview || '')}</pre>
           </div>`
        : `<div><strong>Command:</strong> <code>${escapeHtml(p.details.command)}</code></div>
           ${p.details.cwd ? `<div><strong>CWD:</strong> <code>${escapeHtml(p.details.cwd)}</code></div>` : ''}`;

    wrap.innerHTML = `
        <div style="background:#1e293b;border:1px solid #334155;border-radius:12px;padding:24px;max-width:560px;width:90%;color:#e2e8f0">
            <div style="display:flex;align-items:center;gap:12px;margin-bottom:12px">
                <i class="fas fa-shield-halved" style="color:#f59e0b;font-size:20px"></i>
                <h3 style="font-size:18px;font-weight:600">Agent requests permission: <span style="color:#fbbf24">${escapeHtml(p.tool)}</span></h3>
            </div>
            <div style="font-size:13px;color:#cbd5e1;line-height:1.6">${details}</div>
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
