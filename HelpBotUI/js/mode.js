// Change-mode tab: provider picker, model input/datalist, save handler.

const _API_MODEL_SUGGESTIONS = {
    anthropic: ['claude-3-5-sonnet-20240620', 'claude-sonnet-4-5', 'claude-opus-4-20250514', 'claude-3-5-haiku-20241022'],
    openai:    ['gpt-4o-mini', 'gpt-4o', 'gpt-4-turbo', 'o1-mini'],
    google:    ['gemma-3-27b-it', 'gemma-3-12b-it', 'gemini-2.5-flash', 'gemini-2.0-flash', 'gemini-2.5-pro'],
};
let _localModels = [];

async function fetchLocalModels() {
    try {
        const r = await fetch('/api/tags');
        if (!r.ok) return [];
        const data = await r.json();
        return (data.models || [])
            .filter(m => (m.size || 0) > 0)
            .map(m => m.name || m.model)
            .filter(Boolean);
    } catch (e) { return []; }
}

function updateModelDatalist(provider) {
    const dl = $id('primary-model-list');
    if (!dl) return;
    dl.innerHTML = '';
    const list = provider === 'local' ? _localModels : (_API_MODEL_SUGGESTIONS[provider] || []);
    list.forEach(n => {
        const opt = document.createElement('option');
        opt.value = n;
        dl.appendChild(opt);
    });
    const flist = $id('fallback-model-list');
    if (flist) {
        flist.innerHTML = '';
        _localModels.forEach(n => {
            const o = document.createElement('option');
            o.value = n;
            flist.appendChild(o);
        });
    }
}

function toggleLocalUI(provider) {
    const keyWrap     = $id('mode-primary-key-wrap');
    const localPicker = $id('mode-primary-local-picker');
    const modelInput  = $id('mode-primary-model');
    if (provider === 'local') {
        if (keyWrap)     keyWrap.classList.add('hidden');
        if (localPicker) localPicker.classList.remove('hidden');
        if (modelInput)  modelInput.placeholder = 'e.g. qwen3.5:latest';
        const sel = $id('mode-primary-local-select');
        if (sel) {
            sel.innerHTML = '<option value="">— pick an installed model —</option>';
            _localModels.forEach(n => {
                const o = document.createElement('option');
                o.value = n; o.textContent = n;
                sel.appendChild(o);
            });
        }
    } else {
        if (keyWrap)     keyWrap.classList.remove('hidden');
        if (localPicker) localPicker.classList.add('hidden');
        if (modelInput)  modelInput.placeholder = provider === 'google'
            ? 'e.g. gemma-3-27b-it' : 'e.g. claude-3-5-sonnet...';
    }
    updateModelDatalist(provider);
}

async function renderModeSettings() {
    if (_localModels.length === 0) _localModels = await fetchLocalModels();
    const p = window.configState.providers?.primary  || {};
    const f = window.configState.providers?.fallback || {};
    const providerSelect = $id('mode-primary-provider');
    const currentProvider = (p.type === 'local') ? 'local' : (p.provider || 'anthropic');
    if (providerSelect) {
        providerSelect.value = currentProvider;
        providerSelect.onchange = () => toggleLocalUI(providerSelect.value);
    }
    const modelInput    = $id('mode-primary-model');
    if (modelInput)    modelInput.value    = p.model   || '';
    const keyInput      = $id('mode-primary-key');
    if (keyInput)      keyInput.value      = p.api_key || '';
    const fallbackInput = $id('mode-fallback-model');
    if (fallbackInput) fallbackInput.value = f.model   || '';
    toggleLocalUI(currentProvider);
}

async function saveModeSettings() {
    const provider      = $id('mode-primary-provider')?.value || 'anthropic';
    const model         = ($id('mode-primary-model')?.value  || '').trim();
    const key           = $id('mode-primary-key')?.value     || '';
    const fallbackModel = ($id('mode-fallback-model')?.value || '').trim() || 'qwen3.5:latest';

    if (!model) { alert('Please enter a model name.'); return; }

    const primary = provider === 'local'
        ? { type: 'local', provider: 'ollama', model }
        : { type: 'api', provider, model, api_key: key };
    const fallback = { type: 'local', provider: 'ollama', model: fallbackModel };

    if (await updateConfig({ providers: { primary, fallback } })) {
        // Clear the in-DOM plaintext key immediately on success — the next
        // renderModeSettings() will repaint it with the masked value from
        // /api/config. Keeps raw keys out of browser inspector / autofill.
        const keyInput = $id('mode-primary-key');
        if (keyInput) keyInput.value = '';
        alert(`AI Mode updated!\nPrimary: ${provider} / ${model}\nFallback: ${fallbackModel}`);
    }
}
