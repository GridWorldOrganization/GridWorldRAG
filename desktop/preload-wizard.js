// preload-wizard.js — IPC bridge for the Setup Wizard renderer.
// Narrow surface; everything the renderer can touch is whitelisted here.
const { contextBridge, ipcRenderer } = require("electron");

contextBridge.exposeInMainWorld("wizard", {
  // Read state
  readConfig:        () => ipcRenderer.invoke("wizard:read-config"),
  configPath:        () => ipcRenderer.invoke("wizard:config-path"),
  prereqCheck:       () => ipcRenderer.invoke("wizard:prereq-check"),

  // PG connection test
  testPgConnection:  (params) => ipcRenderer.invoke("wizard:pg-test", params),

  // Google OAuth: file picker for credentials.json
  pickCredentials:   () => ipcRenderer.invoke("wizard:pick-credentials"),
  copyCredentials:   (srcPath) => ipcRenderer.invoke("wizard:copy-credentials", srcPath),

  // Apply: write config.v2.env atomically
  writeConfig:       (values) => ipcRenderer.invoke("wizard:write-config", values),

  // db_init — spawn winserverrag-dbinit.exe and stream output.
  // v1.3.2 fix B3: onDbInitLog returns an `off()` disposer so the
  // wizard renderer can detach the listener when the user navigates
  // away from step 6 (or re-runs db_init). Without this, every
  // re-entry into step 6 attaches another listener and log lines
  // duplicate N times after N re-runs.
  runDbInit:         () => ipcRenderer.invoke("wizard:db-init"),
  onDbInitLog:       (cb) => {
    const handler = (_e, line) => cb(line);
    ipcRenderer.on("wizard:db-init-log", handler);
    return () => ipcRenderer.removeListener("wizard:db-init-log", handler);
  },

  // Service start
  startServices:     () => ipcRenderer.invoke("wizard:start-services"),
  serviceStatus:     () => ipcRenderer.invoke("wizard:service-status"),

  // Done step — open web console / launch Mini Monitor
  openWebConsole:    () => ipcRenderer.invoke("wizard:open-web-console"),
  launchMini:        () => ipcRenderer.invoke("wizard:launch-mini"),

  // Quit
  closeWizard:       () => ipcRenderer.invoke("wizard:close"),
});
