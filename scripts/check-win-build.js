const { spawnSync } = require('node:child_process');

if (process.platform !== 'win32') {
  console.error('Windows packaging requires a native Windows build host. Run npm run build:win on Windows with Python and PyInstaller installed.');
  process.exit(1);
}

const pyinstaller = spawnSync('pyinstaller', ['--version'], { encoding: 'utf8', shell: true });
if (pyinstaller.error || pyinstaller.status !== 0) {
  console.error('PyInstaller was not found. Install it for the Python environment used to build AriYouLive, then retry.');
  process.exit(1);
}
console.log(`Windows build host ready (PyInstaller ${pyinstaller.stdout.trim()}).`);
