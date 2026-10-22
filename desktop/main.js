const { app, BrowserWindow, Menu, Tray, Notification, nativeImage, ipcMain } = require('electron');
const { spawn } = require('node:child_process');
const path = require('node:path');
const fs = require('node:fs');
const os = require('node:os');
const http = require('node:http');
const net = require('node:net');

let win, alertsWin, tray, service, quitting = false, poller, serviceWaiter, serviceWaitRunning = false, notificationCheckRunning = false, lastSeen = new Set();
let port = Number(process.env.ARIYOULIVE_PORT || 8765);

function appIconPath() {
  return app.isPackaged
    ? path.join(process.resourcesPath, 'assets', 'shield-logo.png')
    : path.join(__dirname, '..', 'assets', 'shield-logo.png');
}

function claimPort(preferred) {
  return new Promise((resolve, reject) => {
    const probe = net.createServer();
    probe.once('error', error => {
      if (preferred === 0) reject(error);
      else claimPort(0).then(resolve, reject);
    });
    probe.once('listening', () => {
      const selectedPort = probe.address().port;
      probe.close(() => resolve(selectedPort));
    });
    probe.listen(preferred, '127.0.0.1');
  });
}

function requestState() {
  return new Promise((resolve, reject) => {
    const req = http.get({ hostname: '127.0.0.1', port, path: '/api/state', timeout: 1500 }, res => {
      let data = ''; res.setEncoding('utf8'); res.on('data', x => data += x);
      res.on('end', () => { try { resolve(JSON.parse(data)); } catch (e) { reject(e); } });
    });
    req.on('error', reject); req.on('timeout', () => req.destroy(new Error('timeout')));
  });
}
function startService() {
  const packagedPath = path.join(process.resourcesPath, 'service', process.platform === 'win32' ? 'ariyoulive-service.exe' : 'ariyoulive-service');
  const devPython = process.env.ARIYOULIVE_PYTHON || (process.platform === 'win32' ? 'python' : 'python3');
  const packaged = app.isPackaged;
  const audioDataDir = process.env.ARIYOULIVE_AUDIO_DIR || path.join(app.getPath('userData'), 'audio');
  const bundledAudioDir = packaged ? path.join(process.resourcesPath, 'audio') : path.join(__dirname, '..', 'assets', 'sounds');
  let command, args;
  if (process.platform === 'win32') {
    // Windows packages carry the PyInstaller executable in resources/service.
    // In development, or if that executable is unavailable, use local Python.
    if (packaged && fs.existsSync(packagedPath)) {
      command = packagedPath; args = [];
    } else {
      command = devPython;
      args = [packaged ? path.join(process.resourcesPath, 'service-source', 'server.py') : path.join(__dirname, '..', 'server.py')];
    }
  } else {
    // Preserve the existing macOS/Linux launch behavior exactly.
    command = packaged ? packagedPath : devPython;
    args = packaged ? [] : [path.join(__dirname, '..', 'server.py')];
  }
  service = spawn(command, args, { env: { ...process.env, ARIYOULIVE_HOST: '127.0.0.1', ARIYOULIVE_PORT: String(port), ARIYOULIVE_DB: process.env.ARIYOULIVE_DB || path.join(app.getPath('userData'), 'ariyou_live.db'), ARIYOULIVE_AUDIO_DIR: audioDataDir, ARIYOULIVE_BUNDLED_AUDIO_DIR: bundledAudioDir, ARIYOULIVE_UI_DIR: packaged ? path.join(process.resourcesPath, 'ui') : path.join(__dirname, '..') }, stdio: 'ignore', windowsHide: true });
  service.on('error', err => { console.error('Could not start local monitoring service:', err.message); });
}
function createWindow() {
  win = new BrowserWindow({ width: 1440, height: 920, minWidth: 940, minHeight: 650, show: false, backgroundColor: '#111827', icon: appIconPath(), title: 'AriYouLive — Edge Monitor', autoHideMenuBar: true, webPreferences: { preload: path.join(__dirname, 'preload.js'), contextIsolation: true, sandbox: true } });
  win.loadURL(`http://127.0.0.1:${port}`);
  win.once('ready-to-show', () => win.show());
  win.on('close', event => { if (!quitting) { event.preventDefault(); win.hide(); } });
  win.on('closed', () => { win = null; });
}
function createAlertsWindow() {
  if (alertsWin && !alertsWin.isDestroyed()) { alertsWin.show(); alertsWin.focus(); return alertsWin; }
  alertsWin = new BrowserWindow({ width: 1120, height: 780, minWidth: 760, minHeight: 520, show: false, backgroundColor: '#111827', icon: appIconPath(), title: 'AriYouLive — Alert Center', autoHideMenuBar: true, webPreferences: { preload: path.join(__dirname, 'preload.js'), contextIsolation: true, sandbox: true } });
  alertsWin.loadURL(`http://127.0.0.1:${port}/alerts.html`);
  alertsWin.once('ready-to-show', () => alertsWin?.show());
  alertsWin.on('closed', () => { alertsWin = null; });
  return alertsWin;
}
ipcMain.handle('ariyou:open-alerts', () => { createAlertsWindow(); return true; });
function createTray() {
  // A compact generated icon avoids external image dependencies in the starter package.
  const icon = nativeImage.createFromPath(appIconPath());
  tray = new Tray(icon); tray.setToolTip('AriYouLive Edge Monitor');
  tray.setContextMenu(Menu.buildFromTemplate([
    { label: 'Open AriYouLive', click: () => { if (!win) createWindow(); else { win.show(); win.focus(); } } },
    { type: 'separator' },
    { label: 'Quit and stop monitoring', click: () => { quitting = true; app.quit(); } }
  ]));
  tray.on('double-click', () => { if (!win) createWindow(); else win.show(); });
}
async function monitorNotifications() {
  if (notificationCheckRunning || quitting) return;
  notificationCheckRunning = true;
  try {
    const state = await requestState();
    const liveIds = new Set(state.alerts.filter(a => !a.acknowledged).map(a => a.id));
    for (const a of state.alerts) {
      if (lastSeen.has(a.id) || a.acknowledged || Number(a.muted_at_creation) || a.snooze_until > Date.now() / 1000 || (a.state === 'offline' && a.current_ip_state === 'online') || a.muted_until > Date.now() / 1000) continue;
      if (Notification.isSupported()) new Notification({ title: a.state === 'offline' ? 'Device offline' : 'Device recovered', body: a.message, silent: true }).show();
    }
    lastSeen = liveIds;
  } catch (_) { /* The service may still be starting. */ }
  finally { notificationCheckRunning = false; }
}
app.whenReady().then(async () => {
  port = await claimPort(port);
  startService(); createTray();
  serviceWaiter = setInterval(async () => {
    if (serviceWaitRunning || quitting) return;
    serviceWaitRunning = true;
    try {
      await requestState();
      clearInterval(serviceWaiter); serviceWaiter = null;
      createWindow();
      serviceWaitRunning = false;
      void monitorNotifications();
      poller = setInterval(monitorNotifications, 1500);
    }
    catch (_) { serviceWaitRunning = false; /* wait until local SQLite service is ready */ }
  }, 300);
});
app.on('before-quit', () => { quitting = true; if (poller) clearInterval(poller); if (serviceWaiter) clearInterval(serviceWaiter); if (service && !service.killed) service.kill(); });
app.on('window-all-closed', event => { /* The tray keeps the desktop application and monitor alive. */ });
app.on('activate', () => { if (win) { win.show(); win.focus(); } else createWindow(); });
