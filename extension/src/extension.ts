import * as vscode from 'vscode';
import * as cp from 'child_process';
import * as fs from 'fs';
import * as path from 'path';
import * as os from 'os';

const BOT_ROOT = 'D:/Code/yolo/Claude_Code/LocalHelpBot';
const PYTHON_EXE = path.join(BOT_ROOT, 'venv/Scripts/python.exe');
const PROXY_SCRIPT = path.join(BOT_ROOT, 'core/proxy.py');
const DISCORD_SCRIPT = path.join(BOT_ROOT, 'core/discord_gateway.py');
const CONTINUE_CONFIG_PATH = path.join(os.homedir(), '.continue', 'config.json');

let proxyProcess: cp.ChildProcess | undefined;
let discordProcess: cp.ChildProcess | undefined;

export function activate(context: vscode.ExtensionContext) {
    console.log('Local Help Bot Manager is now active!');

    // Status Bar Items
    const proxyStatusBar = vscode.window.createStatusBarItem(vscode.StatusBarAlignment.Right, 100);
    proxyStatusBar.command = 'localhelpbot.startProxy';
    context.subscriptions.push(proxyStatusBar);

    const discordStatusBar = vscode.window.createStatusBarItem(vscode.StatusBarAlignment.Right, 101);
    discordStatusBar.command = 'localhelpbot.startDiscord';
    context.subscriptions.push(discordStatusBar);

    const updateStatus = () => {
        // Proxy Status
        if (proxyProcess && !proxyProcess.killed) {
            proxyStatusBar.text = '$(play) Proxy: Running';
            proxyStatusBar.tooltip = 'Proxy is running. Click to stop.';
            proxyStatusBar.command = 'localhelpbot.stopProxy';
        } else {
            proxyStatusBar.text = '$(play) Proxy: Stopped';
            proxyStatusBar.tooltip = 'Proxy is stopped. Click to start.';
            proxyStatusBar.command = 'localhelpbot.startProxy';
        }
        proxyStatusBar.show();

        // Discord Status
        if (discordProcess && !discordProcess.killed) {
            discordStatusBar.text = '$(robot) Discord: Online';
            discordStatusBar.tooltip = 'Discord bot is running. Click to stop.';
            discordStatusBar.command = 'localhelpbot.stopDiscord';
        } else {
            discordStatusBar.text = '$(robot) Discord: Offline';
            discordStatusBar.tooltip = 'Discord bot is stopped. Click to start.';
            discordStatusBar.command = 'localhelpbot.startDiscord';
        }
        discordStatusBar.show();
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
                console.log(`[LocalHelpBot Proxy] ${data}`);
            });

            proxyProcess.stderr?.on('data', (data) => {
                console.error(`[LocalHelpBot Proxy Error] ${data}`);
            });

            updateStatus();
            vscode.window.showInformationMessage('LocalHelpBot Proxy started successfully!');
        } catch (err: any) {
            vscode.window.showErrorMessage(`Failed to start proxy: ${err.message}`);
        }
    };

    const stopProxy = () => {
        if (proxyProcess) {
            proxyProcess.kill();
            proxyProcess = undefined;
            updateStatus();
            vscode.window.showInformationMessage('LocalHelpBot Proxy stopped.');
        } else {
            vscode.window.showInformationMessage('Proxy is not running.');
        }
    };

    const startDiscordBot = async () => {
        if (discordProcess && !discordProcess.killed) {
            vscode.window.showInformationMessage('Discord bot is already running.');
            return;
        }

        // Discord bot requires the proxy to be running first
        if (!proxyProcess || proxyProcess.killed) {
            vscode.window.showWarningMessage('The LocalHelpBot Proxy must be running first. Starting proxy now...');
            await startProxy();
            // Small delay to allow proxy to boot
            await new Promise(resolve => setTimeout(resolve, 2000));
        }

        try {
            discordProcess = cp.spawn(PYTHON_EXE, [DISCORD_SCRIPT], {
                cwd: BOT_ROOT,
                detached: false
            });

            discordProcess.stdout?.on('data', (data) => {
                console.log(`[Discord Bot] ${data}`);
            });

            discordProcess.stderr?.on('data', (data) => {
                console.error(`[Discord Bot Error] ${data}`);
            });

            updateStatus();
            vscode.window.showInformationMessage('Discord Bot started successfully!');
        } catch (err: any) {
            vscode.window.showErrorMessage(`Failed to start Discord bot: ${err.message}`);
        }
    };

    const stopDiscordBot = () => {
        if (discordProcess) {
            discordProcess.kill();
            discordProcess = undefined;
            updateStatus();
            vscode.window.showInformationMessage('Discord Bot stopped.');
        } else {
            vscode.window.showInformationMessage('Discord bot is not running.');
        }
    };

    const configureContinue = async () => {

        const config = {
            "models": [
                {
                    "title": "Auto Agent (Smart Switch)",
                    "provider": "ollama",
                    "model": "auto-agent",
                    "apiBase": "http://localhost:11435"
                },
                {
                    "title": "Auto Agent (Smart Switch)",
                    "provider": "ollama",
                    "model": "auto-agent",
                    "apiBase": "http://localhost:11435"
                },
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
                const choice = await vscode.window.showWarningMessage(
                    'Continue config already exists. Overwrite it?',
                    'Yes', 'No'
                );
                if (choice !== 'Yes') return;
            }

            fs.writeFileSync(CONTINUE_CONFIG_PATH, JSON.stringify(config, null, 2));
            vscode.window.showInformationMessage(`Continue configured at ${CONTINUE_CONFIG_PATH}`);
        } catch (err: any) {
            vscode.window.showErrorMessage(`Failed to configure Continue: ${err.message}`);
        }
    };

    // Register commands
    context.subscriptions.push(
        vscode.commands.registerCommand('localhelpbot.startProxy', startProxy),
        vscode.commands.registerCommand('localhelpbot.stopProxy', stopProxy),
        vscode.commands.registerCommand('localhelpbot.startDiscord', startDiscordBot),
        vscode.commands.registerCommand('localhelpbot.stopDiscord', stopDiscordBot),
        vscode.commands.registerCommand('localhelpbot.configureContinue', configureContinue)
    );

    // Auto-start everything on activation for simplicity
    startProxy().then(() => {
        startDiscordBot();
    });
    updateStatus();
}

export function deactivate() {
    if (proxyProcess) {
        proxyProcess.kill();
    }
    if (discordProcess) {
        discordProcess.kill();
    }
}
