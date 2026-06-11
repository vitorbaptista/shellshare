#!/usr/bin/env node
// Thin launcher: resolves the platform-specific package (installed via
// optionalDependencies) and execs the real shellshare binary with the
// terminal's stdio inherited, so raw mode, Ctrl+C and resize pass through.
'use strict';

const { spawnSync } = require('child_process');

const PLATFORMS = {
  'linux x64': 'shellshare-linux-x64',
  'darwin x64': 'shellshare-darwin-x64',
  'darwin arm64': 'shellshare-darwin-arm64',
  // "windows" not "win32": npm's spam filter rejects new win32-* names
  'win32 x64': 'shellshare-windows-x64',
};

const key = process.platform + ' ' + process.arch;
const pkg = PLATFORMS[key];
if (!pkg) {
  console.error('shellshare: unsupported platform: ' + key);
  console.error('Supported platforms: ' + Object.keys(PLATFORMS).join(', '));
  console.error('Prebuilt binaries for other setups: https://github.com/vitorbaptista/shellshare/releases');
  process.exit(1);
}

const exe = process.platform === 'win32' ? 'shellshare.exe' : 'shellshare';
let binPath;
try {
  binPath = require.resolve(pkg + '/bin/' + exe);
} catch (err) {
  console.error('shellshare: the platform package "' + pkg + '" is not installed.');
  console.error('This usually means optional dependencies were skipped. Reinstall with:');
  console.error('  npm install -g shellshare --force --include=optional');
  process.exit(1);
}

const result = spawnSync(binPath, process.argv.slice(2), { stdio: 'inherit' });
if (result.error) {
  console.error('shellshare: failed to start ' + binPath + ': ' + result.error.message);
  process.exit(1);
}
if (result.signal) {
  // Re-raise so the parent sees the same termination signal.
  process.kill(process.pid, result.signal);
} else {
  process.exit(result.status === null ? 1 : result.status);
}
