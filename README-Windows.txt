BUILD KIT ONLY — this folder does not contain a ready-to-run .exe. Build it on a Windows computer first.

AriYouLive Windows Pinger build folder

Transfer this entire folder to your Windows computer. Do not transfer node_modules or the Mac database files.

On Windows, install Node.js and Python, open a terminal in this folder, then run:
  npm install
  pip install pyinstaller
  npm run build:win

The portable Windows application will be created in:
  dist/windows/portable/

To bring your existing devices and settings, export a JSON backup from the running Mac app and import it after starting the Windows build.
