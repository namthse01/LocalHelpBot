"use strict";
var __createBinding = (this && this.__createBinding) || (Object.create ? (function(o, m, k, k2) {
    if (k2 === undefined) k2 = k;
    var desc = Object.getOwnPropertyDescriptor(m, k);
    if (!desc || ("get" in desc ? !m.__esModule : desc.writable || desc.configurable)) {
      desc = { enumerable: true, get: function() { return m[k]; } };
    }
    Object.defineProperty(o, k2, desc);
}) : (function(o, m, k, k2) {
    if (k2 === undefined) k2 = k;
    o[k2] = m[k];
}));
var __setModuleDefault = (this && this.__setModuleDefault) || (Object.create ? (function(o, v) {
    Object.defineProperty(o, "default", { enumerable: true, value: v });
}) : function(o, v) {
    o["default"] = v;
});
var __importStar = (this && this.__importStar) || (function () {
    var ownKeys = function(o) {
        ownKeys = Object.getOwnPropertyNames || function (o) {
            var ar = [];
            for (var k in o) if (Object.prototype.hasOwnProperty.call(o, k)) ar[ar.length] = k;
            return ar;
        };
        return ownKeys(o);
    };
    return function (mod) {
        if (mod && mod.__esModule) return mod;
        var result = {};
        if (mod != null) for (var k = ownKeys(mod), i = 0; i < k.length; i++) if (k[i] !== "default") __createBinding(result, mod, k[i]);
        __setModuleDefault(result, mod);
        return result;
    };
})();
Object.defineProperty(exports, "__esModule", { value: true });
exports.activate = activate;
exports.deactivate = deactivate;
const vscode = __importStar(require("vscode"));
const cp = __importStar(require("child_process"));
const fs = __importStar(require("fs"));
const path = __importStar(require("path"));
const os = __importStar(require("os"));
const BOT_ROOT = 'D:/Code/yolo/Claude_Code/LocalHelpBot';
const PYTHON_EXE = path.join(BOT_ROOT, 'venv/Scripts/python.exe');
const PROXY_SCRIPT = path.join(BOT_ROOT, 'rag_proxy.py');
const CONTINUE_CONFIG_PATH = path.join(os.homedir(), '.continue', 'config.json');
let proxyProcess;
function activate(context) {
    console.log('Local Help Bot Manager is now active!');
    // Create a status bar item to show proxy status
    const statusBarItem = vscode.window.createStatusBarItem(vscode.StatusBarAlignment.Right, 100);
    statusBarItem.command = 'localhelpbot.startProxy';
    context.subscriptions.push(statusBarItem);
    const updateStatus = () => {
        if (proxyProcess && !proxyProcess.killed) {
            statusBarItem.text = '$(play) LocalHelpBot: Running';
            statusBarItem.tooltip = 'Proxy is running. Click to stop.';
            statusBarItem.command = 'localhelpbot.stopProxy';
        }
        else {
            statusBarItem.text = '$(play) LocalHelpBot: Stopped';
            statusBarItem.tooltip = 'Proxy is stopped. Click to start.';
            statusBarItem.command = 'localhelpbot.startProxy';
        }
        statusBarItem.show();
    };
    const startProxy = async () => {
        if (proxyProcess && !proxyProcess.killed) {
            vscode.window.showInformationMessage('Proxy is already running.');
            return;
        }
        if (!fs.existsSync(PYTHON_EXE)) {
            vscode.window.showErrorMessage(`Python executable not found at ${PYTHON_EXE}. Please ensure venv is created.`);
            return;
        }
        try {
            proxyProcess = cp.spawn(PYTHON_EXE, [PROXY_SCRIPT], {
                cwd: BOT_ROOT,
                detached: false
            });
            proxyProcess.stdout?.on('data', (data) => {
                console.log(`[LocalHelpBot] ${data}`);
            });
            proxyProcess.stderr?.on('data', (data) => {
                console.error(`[LocalHelpBot Error] ${data}`);
            });
            updateStatus();
            vscode.window.showInformationMessage('LocalHelpBot Proxy started successfully!');
        }
        catch (err) {
            vscode.window.showErrorMessage(`Failed to start proxy: ${err.message}`);
        }
    };
    const stopProxy = () => {
        if (proxyProcess) {
            proxyProcess.kill();
            proxyProcess = undefined;
            updateStatus();
            vscode.window.showInformationMessage('LocalHelpBot Proxy stopped.');
        }
        else {
            vscode.window.showInformationMessage('Proxy is not running.');
        }
    };
    const configureContinue = async () => {
        const config = {
            "models": [
                {
                    "title": "Local Chat (qwen3.5)",
                    "provider": "ollama",
                    "model": "qwen3.5",
                    "apiBase": "http://localhost:11435"
                },
                {
                    "title": "Deep Reasoning (GLM 19B)",
                    "provider": "ollama",
                    "model": "deep-agent",
                    "apiBase": "http://localhost:11435"
                },
                {
                    "title": "CAD / AutoCAD Agent",
                    "provider": "ollama",
                    "model": "cad-rag",
                    "apiBase": "http://localhost:11435"
                },
                {
                    "title": "UI / Frontend Agent",
                    "provider": "ollama",
                    "model": "ui-agent",
                    "apiBase": "http://localhost:11435"
                },
                {
                    "title": "Code Agent — tự fix bugs",
                    "provider": "ollama",
                    "model": "code-agent",
                    "apiBase": "http://localhost:11435"
                },
                {
                    "title": "Web Research Agent",
                    "provider": "ollama",
                    "model": "web-creep",
                    "apiBase": "http://localhost:11435"
                },
                {
                    "title": "Browser Agent",
                    "provider": "ollama",
                    "model": "browser-agent",
                    "apiBase": "http://localhost:11435"
                }
            ],
            "tabAutocompleteModel": {
                "title": "Qwen3.5 Autocomplete",
                "provider": "ollama",
                "model": "qwen3.5",
                "apiBase": "http://localhost:11434"
            },
            "embeddingsProvider": {
                "provider": "ollama",
                "model": "mxbai-embed-large",
                "apiBase": "http://localhost:11434"
            }
        };
        try {
            const dir = path.dirname(CONTINUE_CONFIG_PATH);
            if (!fs.existsSync(dir)) {
                fs.mkdirSync(dir, { recursive: true });
            }
            // If config exists, we should merge it. For simplicity, we'll just write it if it doesn't exist
            // or ask the user to overwrite.
            if (fs.existsSync(CONTINUE_CONFIG_PATH)) {
                const choice = await vscode.window.showWarningMessage('Continue config already exists. Overwrite it?', 'Yes', 'No');
                if (choice !== 'Yes')
                    return;
            }
            fs.writeFileSync(CONTINUE_CONFIG_PATH, JSON.stringify(config, null, 2));
            vscode.window.showInformationMessage(`Continue configured at ${CONTINUE_CONFIG_PATH}`);
        }
        catch (err) {
            vscode.window.showErrorMessage(`Failed to configure Continue: ${err.message}`);
        }
    };
    // Register commands
    context.subscriptions.push(vscode.commands.registerCommand('localhelpbot.startProxy', startProxy), vscode.commands.registerCommand('localhelpbot.stopProxy', stopProxy), vscode.commands.registerCommand('localhelpbot.configureContinue', configureContinue));
    // Auto-start proxy on activation
    startProxy();
    updateStatus();
}
function deactivate() {
    if (proxyProcess) {
        proxyProcess.kill();
    }
}
//# sourceMappingURL=extension.js.map