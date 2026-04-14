// Discord connect tab.
let discordConnected = false;

function renderDiscordSettings() {
    const d = window.configState.discord;
    if (!d) return;
    if ($id('cfg-discord-token'))    $id('cfg-discord-token').value = d.token || '';
    if ($id('cfg-discord-server'))   $id('cfg-discord-server').value = d.server_id || '';
    if ($id('cfg-discord-channels')) $id('cfg-discord-channels').value = (d.allowed_channels || []).join(', ');
    checkDiscordStatus();
}

async function checkDiscordStatus() {
    try {
        const res = await fetch('/api/discord/status');
        const data = await res.json();
        updateDiscordUI(data.connected);
    } catch (e) {
        updateDiscordUI(false);
    }
}

function updateDiscordUI(connected) {
    discordConnected = connected;
    const dot  = $id('discord-status-dot');
    const text = $id('discord-status-text');
    const btn  = $id('discord-connect-btn');
    if (connected) {
        if (dot)  dot.className = 'w-2.5 h-2.5 bg-green-500 rounded-full animate-pulse';
        if (text) { text.innerText = 'Connected'; text.className = 'text-green-400'; }
        if (btn)  {
            btn.className = 'bg-red-600 hover:bg-red-500 text-white px-6 py-2 rounded-lg font-medium transition-all';
            btn.innerHTML = '<i class="fab fa-discord mr-2"></i> Disconnect';
        }
    } else {
        if (dot)  dot.className = 'w-2.5 h-2.5 bg-slate-600 rounded-full';
        if (text) { text.innerText = 'Disconnected'; text.className = 'text-slate-500'; }
        if (btn)  {
            btn.className = 'bg-green-600 hover:bg-green-500 text-white px-6 py-2 rounded-lg font-medium transition-all';
            btn.innerHTML = '<i class="fab fa-discord mr-2"></i> Connect';
        }
    }
}

async function toggleDiscordConnection() {
    const endpoint = discordConnected ? '/api/discord/disconnect' : '/api/discord/connect';
    try {
        const res = await fetch(endpoint, { method: 'POST' });
        const data = await res.json();
        updateDiscordUI(data.connected);
    } catch (e) {
        console.error('Discord toggle error:', e);
    }
}

async function saveDiscordSettings() {
    const token    = $id('cfg-discord-token')?.value || '';
    const server   = $id('cfg-discord-server')?.value || '';
    const channels = ($id('cfg-discord-channels')?.value || '').split(',').map(c => c.trim());
    const updatedDiscord = {
        token,
        server_id: parseInt(server) || 0,
        allowed_channels: channels.filter(c => c).map(c => parseInt(c))
    };
    if (await updateConfig({ discord: updatedDiscord })) alert('Discord settings saved!');
}
