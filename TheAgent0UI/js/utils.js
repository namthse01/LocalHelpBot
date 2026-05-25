// Shared globals + DOM/string helpers.
window.currentModel = 'auto-agent';
window.configState = { agents: {}, discord: {}, tasks: [], providers: {} };

function $id(id) {
    const el = document.getElementById(id);
    if (!el) console.error(`CRITICAL: Element ${id} missing!`);
    return el;
}

function escapeHtml(s) {
    return String(s ?? '').replace(/[&<>"']/g, c => (
        { '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]
    ));
}
