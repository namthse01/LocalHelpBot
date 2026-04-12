console.log("LocalHelpBot UI Loaded Successfully!");

let currentModel = 'auto-agent';
let configState = {
    agents: {},
    discord: {},
    tasks: [],
    providers: {}
};

function $id(id) {
    const el = document.getElementById(id);
    if (!el) console.warn(`Element with id ${id} not found.`);
    return el;
}

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
    document.querySelectorAll('.tab-content').forEach(content => {
        content.classList.remove('active');
    });
    const activeContent = $id(`content-${tabId}`);
    if (activeContent) activeContent.classList.add('active');
}

async function loadConfig() {
    try {
        const response = await fetch('/api/config');
        if (!response.ok) throw new Error('Failed to load config');
        configState = await response.json();
        try { renderAgentSelect(); } catch(e) { console.error(e); }
        try { renderAgentsGrid(); } catch(e) { console.error(e); }
        try { renderDiscordSettings(); } catch(e) { console.error(e); }
        try { renderTasksList(); } catch(e) { console.error(e); }
        try { renderModeSettings(); } catch(e) { console.error(e); }
    } catch (error) {
        console.error('Critical config load error:', error);
    }
}

async function updateConfig(payload) {
    try {
        const response = await fetch('/api/config', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify(payload)
        });
        if (!response.ok) throw new Error('Failed to save config');
        await loadConfig();
        return true;
    } catch (error) {
        console.error('Config save error:', error);
        return false;
    }
}

function setModel(model) {
    currentModel =, model;
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
        inner.innerHTML = text.replace(/```([\s\S]*?)```/g, '<pre class="bg-slate-900 p-2 rounded-md my-2 overflow-x-auto text-xs font-mono text-indigo-300">$1</pre>');
    } else {
        inner.innerText = text || '';
    }
    div.appendChild(inner);
    chatWindow.appendChild(div);
    chatWindow.scrollTop = chatWindow.scrollHeight;
}

async function sendMessage() {
    const input = $id('user-input');
    if (!input) return;
    const text = input.value.trim();
    if (!text) return;
    appendMessage('user', text);
    input.value = '';
    input.style.height = 'auto';
    const chatWindow = $id('chat-window');
    const loadingDiv = document.createElement('div');
    loadingDiv.className = 'flex justify-start mb-4';
    loadingDiv.innerHTML = `<div class="message-bot px-4 py-2 text-sm text-slate-400 italic">Bot is thinking...</div>`;
    if (chatWindow) chatWindow.appendChild(loadingDiv);
    try {
        const response = await fetch('/api/chat', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({
                model: currentModel,
                messages: [{ role: 'user', content: text }]
            })
        });
        if (!response.ok) throw new Error(`Server Error: ${response.status}`);
        const data = await response.json();
        if (chatWindow && chatWindow.contains(loadingDiv)) chatWindow.removeChild(loadingDiv);
        appendMessage('bot', data.message.content);
    } catch (error) {
        if (chatWindow && chatWindow.contains(loadingDiv)) chatWindow.removeChild(loadingDiv);
        appendMessage('bot', `❌ Error: ${error.message}`);
    }
}

function clearChat() {
    const chatWindow = $id('chat-window');
    if (!chatWindow) return;
    chatWindow.innerHTML = '';
    appendMessage('bot', 'Chat cleared. How can I help you today?');
}

function renderAgentSelect() {
    const select = $id('chat-agent-select');
    if (!select || !configState.agents) return;
    select.innerHTML = '';
    Object.keys(configState.agents).forEach(agentId => {
        const opt = document.createElement('option');
        opt.value = agentId;
        opt.innerText = agentId.charAt(0).toUpperCase() + agentId.slice(1);
        if (agentId === currentModel) opt.selected = true;
        select.appendChild(opt);
    });
}

function renderAgentsGrid() {
    const grid = $id('agents-grid');
    if (!grid || !configState.agents) return;
    grid.innerHTML = '';
    Object.entries(configState.agents).forEach(([id, profile]) => {
        const card = document.createElement('div');
        card.className = 'bg-slate-900 border border-slate-800 p-5 rounded-2xl hover:border-indigo-500 transition-all group';
        card.innerHTML = `
            <div class="flex justify-between items-start mb-4">
                <h4 class="font-bold text-slate-200">${id.toUpperCase()}</h4>
                <button onclick="openAgentModal('${id}')" class="text-slate-500 hover:text-indigo-400 opacity-0 group-hover:opacity-100 transition-all">
                    <i class="fas fa-edit"></i>
                </button>
            </div>
            <p class="text-xs text-slate-400 line-clamp-3 mb-4">${profile.system_prompt || ''}</p>
            <div class="flex items-center justify-between">
                <span class="text-[10px] font-mono text-slate-500">${profile.model || 'unknown'}</span>
                <span class="text-[10px] bg-slate-800 text-slate-400 px-2 py-0.5 rounded border border-slate-700">${(profile.tools || []).length} tools</span>
            </div>
        `;
        grid.appendChild(card);
    });
}

let activeEditingAgent = null;
function openAgentModal(id) {
    activeEditingAgent = id;
    const profile = configState.agents[id];
    if (!profile) return;
    if ($id('modal-agent-title')) $id('modal-agent-title').innerText = `Edit Specialist: ${id}`;
    if ($id('edit-agent-name')) $id('edit-agent-name').value = id;
    if ($id('edit-agent-prompt')) $id('edit-agent-prompt').value = profile.system_prompt || '';
    if ($id('edit-agent-model')) $id('edit-agent-model').value = profile.model || '';
    if ($id('agent-modal')) $id('agent-modal').classList.remove('hidden');
}

function closeAgentModal() {
    const modal = $id('agent-modal');
    if (modal) modal.classList.add('hidden');
}

async function saveAgentSettings() {
    const prompt = $id('edit-agent-prompt')?.value || '';
    const model = $id('edit-agent-model')?.value || '';
    const updatedAgents = { ...configState.agents };
    updatedAgents[activeEditingAgent] = { ...updatedAgents[activeEditingAgent], system_prompt: prompt, model: model };
    if (await updateConfig({ agents: updatedAgents })) closeAgentModal();
}

function renderDiscordSettings() {
    const d = configState.discord;
    if (!d) return;
    if ($id('cfg-discord-token')) $id('cfg-discord-token').value = d.token || '';
    if ($id('cfg-discord-server')) $id('cfg-discord-server').value = d.server_id || '';
    if ($id('cfg-discord-channels')) $id('cfg-discord-channels').value = (d.allowed_channels || []).join(', ');
}

async function saveDiscordSettings() {
    const token = $id('cfg-discord-token')?.value || '';
    const server = $id('cfg-discord-server')?.value || '';
    const channels = ($id('cfg-discord-channels')?.value || '').split(',').map(c => c.trim());
    const updatedDiscord = { token: token, server_id: parseInt(server) || 0, allowed_channels: channels.filter(c => c).map(c => parseInt(c)) };
    if (await updateConfig({ discord: updatedDiscord })) alert('Discord settings saved!');
}

function renderTasksList() {
    const list = $id('tasks-list');
    if (!list || !configState.tasks) return;
    list.innerHTML = '';
    configState.tasks.forEach((task, index) => {
        const item = document.createElement('div');
        item.className = 'bg-slate-900 border border-slate-800 p-4 rounded-xl flex justify-between items-center';
        item.innerHTML = `
            <div class="flex items-center gap-4">
                <div class="w-10 h-10 bg-indigo-600/20 text-indigo-500 rounded-lg flex items-center justify-center">
                    <i class="fas fa-clock"></i>
                </div>
                <div>
                    <h4 class="text-sm font-bold text-slate-200">${task.id || 'Unnamed Task'}</h4>
                    <p class="text-xs text-slate-500">${task.schedule || 'Not set'} • Target: ${task.recipient || 'None'}</p>
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

function renderModeSettings() {
    const p = configState.providers?.primary || {};
    const f = configState.providers?.fallback || {};
    const providerSelect = $id('mode-primary-provider');
    if (providerSelect) providerSelect.value = p.provider || 'anthropic';
    const modelInput = $id('mode-primary-model');
    if (modelInput) modelInput.value = p.model || '';
    const keyInput = $id('mode-primary-key');
    if (keyInput) keyInput.value = p.api_key || '';
    const fallbackInput = $id('mode-fallback-model');
    if (fallbackInput) fallbackInput.value = f.model || '';
}

async function saveModeSettings() {
    const provider = $id('mode-primary-provider')?.value || 'anthropic';
    const model = $id('mode-primary-model')?.value || '';
    const key = $id('mode-primary-key')?.value || '';
    const fallbackModel = $id('mode-fallback-model')?.value || '';
    const primary = { type: provider === 'local' ? 'local' : 'api', provider: provider !== 'local' ? provider : undefined, model: model, api_key: provider !== 'local' ? key : undefined };
    const fallback = { type: 'local', provider: 'ollama', model: fallbackModel };
    if (await updateConfig({ providers: { primary, fallback } })) alert('AI Mode updated successfully!');
}

function init() {
    const sendBtn = $id('send-btn');
    if (sendBtn) sendBtn.addEventListener('click', sendMessage);
    const userInput = $id('user-input');
    if (userInput) {
        userInput.addEventListener('keydown', (e) => {
            if (e.key === 'Enter' && !e.shiftKey) {
                e.preventDefault();
                sendMessage();
            }
        });
        userInput.addEventListener('input', function() {
            this.style.height = 'auto';
            this.style.height = (this.scrollHeight) + 'px';
        });
    }
    loadConfig();
    switchTab('chat');
}

init();
