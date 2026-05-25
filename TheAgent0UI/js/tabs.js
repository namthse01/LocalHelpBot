// Top-nav tab switching.
function switchTab(tabId) {
    document.querySelectorAll('nav button').forEach(btn => {
        btn.classList.remove('bg-indigo-600', 'text-white');
        btn.classList.add('text-slate-400', 'hover:text-slate-200');
    });
    const activeBtn = $id(`tab-${tabId}`);
    if (activeBtn) {
        activeBtn.classList.add('bg-indigo-600', 'text-white');
        activeBtn.classList.remove('text-slate-400', 'hover:text-slate-200');
    }
    document.querySelectorAll('.tab-content').forEach(c => c.classList.remove('active'));
    const activeContent = $id(`content-${tabId}`);
    if (activeContent) {
        activeContent.classList.add('active');
        if (tabId === 'chat') activeContent.classList.add('flex-layout');
    }
}
