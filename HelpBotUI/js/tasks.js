// Daily Tasks tab.
function renderTasksList() {
    const list = $id('tasks-list');
    if (!list || !window.configState.tasks) return;
    list.innerHTML = '';
    window.configState.tasks.forEach(task => {
        const item = document.createElement('div');
        item.className = 'bg-slate-900 border border-slate-800 p-4 rounded-xl flex justify-between items-center';
        item.innerHTML = `
            <div class="flex items-center gap-4">
                <div class="w-10 h-10 bg-indigo-600/20 text-indigo-500 rounded-lg flex items-center justify-center">
                    <i class="fas fa-clock"></i>
                </div>
                <div>
                    <h4 class="text-sm font-bold text-slate-200">${escapeHtml(task.id || 'Unnamed Task')}</h4>
                    <p class="text-xs text-slate-500">${escapeHtml(task.schedule || 'Not set')} • Target: ${escapeHtml(task.recipient || 'None')}</p>
                </div>
            </div>
            <div class="flex gap-2">
                <button class="p-2 text-slate-500 hover:text-indigo-400 transition-all"><i class="fas fa-edit"></i></button>
                <button class="p-2 text-slate-500 hover:text-red-400 transition-all"><i class="fas fa-trash"></i></button>
            </div>
        `;
        list.appendChild(item);
    });
}
