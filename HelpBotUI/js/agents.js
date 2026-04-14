// Agents tab: dropdown, grid cards, edit modal.

function renderAgentSelect() {
    const select = $id('chat-agent-select');
    if (!select || !window.configState.agents) return;
    select.innerHTML = '';
    Object.keys(window.configState.agents).forEach(agentId => {
        const opt = document.createElement('option');
        opt.value = agentId;
        opt.innerText = agentId.charAt(0).toUpperCase() + agentId.slice(1);
        if (agentId === window.currentModel) opt.selected = true;
        select.appendChild(opt);
    });
}

function renderAgentsGrid() {
    const grid = $id('agents-grid');
    if (!grid || !window.configState.agents) return;
    grid.innerHTML = '';
    Object.entries(window.configState.agents).forEach(([id, profile]) => {
        const card = document.createElement('div');
        card.className = 'bg-slate-900 border border-slate-800 p-5 rounded-2xl hover:border-indigo-500 transition-all group';
        card.innerHTML = `
            <div class="flex justify-between items-start mb-4">
                <h4 class="font-bold text-slate-200">${escapeHtml(id.toUpperCase())}</h4>
                <button onclick="openAgentModal('${escapeHtml(id)}')" class="text-slate-500 hover:text-indigo-400 opacity-0 group-hover:opacity-100 transition-all">
                    <i class="fas fa-edit"></i>
                </button>
            </div>
            <p class="text-xs text-slate-400 line-clamp-3 mb-4">${escapeHtml(profile.system_prompt || '')}</p>
            <div class="flex items-center justify-between">
                <span class="text-[10px] font-mono text-slate-500">${escapeHtml(profile.model || 'unknown')}</span>
                <span class="text-[10px] bg-slate-800 text-slate-400 px-2 py-0.5 rounded border border-slate-700">${(profile.tools || []).length} tools</span>
            </div>
        `;
        grid.appendChild(card);
    });
}

let activeEditingAgent = null;
function openAgentModal(id) {
    activeEditingAgent = id;
    const profile = window.configState.agents[id];
    if (!profile) return;
    if ($id('modal-agent-title')) $id('modal-agent-title').innerText = `Edit Specialist: ${id}`;
    if ($id('edit-agent-name'))   $id('edit-agent-name').value = id;
    if ($id('edit-agent-prompt')) $id('edit-agent-prompt').value = profile.system_prompt || '';
    if ($id('edit-agent-model'))  $id('edit-agent-model').value = profile.model || '';
    if ($id('agent-modal'))       $id('agent-modal').classList.remove('hidden');
}

function closeAgentModal() {
    const modal = $id('agent-modal');
    if (modal) modal.classList.add('hidden');
}

async function saveAgentSettings() {
    const prompt = $id('edit-agent-prompt')?.value || '';
    const model  = $id('edit-agent-model')?.value  || '';
    const updatedAgents = { ...window.configState.agents };
    updatedAgents[activeEditingAgent] = {
        ...updatedAgents[activeEditingAgent],
        system_prompt: prompt,
        model
    };
    if (await updateConfig({ agents: updatedAgents })) closeAgentModal();
}
