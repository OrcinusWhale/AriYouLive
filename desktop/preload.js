const { contextBridge, ipcRenderer } = require('electron');

contextBridge.exposeInMainWorld('ariyouDesktop', {
  openAlerts: () => ipcRenderer.invoke('ariyou:open-alerts')
});
