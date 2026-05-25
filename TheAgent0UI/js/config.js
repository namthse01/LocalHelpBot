// /api/config GET + POST, and the render fan-out.
async function loadConfig() {
    try {
        const response = await fetch('/api/config');
        if (!response.ok) throw new Error(`HTTP ${response.status}`);
        window.configState = await response.json();
        renderAgentSelect();
        renderAgentsGrid();
        renderDiscordSettings();
        renderTasksList();
        renderModeSettings();
    } catch (error) {
        console.error('Config load error:', error);
    }
}

async function updateConfig(payload) {
    try {
        const response = await fetch('/api/config', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify(payload)
        });
        if (!response.ok) throw new Error(`HTTP ${response.status}`);
        await loadConfig();
        return true;
    } catch (error) {
        console.error('Config save error:', error);
        return false;
    }
}
