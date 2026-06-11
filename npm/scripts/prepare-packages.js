#!/usr/bin/env node
// Stage the npm packages for a release.
//
// Usage: node npm/scripts/prepare-packages.js <version> <binaries-dir> <out-dir>
//
// <binaries-dir> is the actions/download-artifact@v4 output: one directory
// per artifact (e.g. shellshare-linux-x86_64/shellshare). Produces in
// <out-dir> one publishable directory per platform package plus the meta
// package, all stamped with <version> and the meta package's
// optionalDependencies pinned exactly to it.
'use strict';

const fs = require('fs');
const path = require('path');

const [version, binariesDir, outDir] = process.argv.slice(2);
if (!version || !binariesDir || !outDir) {
  console.error('usage: prepare-packages.js <version> <binaries-dir> <out-dir>');
  process.exit(1);
}

const npmDir = path.join(__dirname, '..');
const platforms = JSON.parse(fs.readFileSync(path.join(npmDir, 'platform-packages.json'), 'utf8'));

// Sanity floor: a real shellshare binary is several MB; anything under 1 MB
// means we staged the wrong file and must not publish.
const MIN_BINARY_BYTES = 1024 * 1024;

for (const [name, info] of Object.entries(platforms)) {
  const src = path.join(binariesDir, info.artifact, info.binary);
  const size = fs.statSync(src).size;
  if (size < MIN_BINARY_BYTES) {
    console.error(`${src} is only ${size} bytes - refusing to package it`);
    process.exit(1);
  }

  const pkgDir = path.join(outDir, name);
  fs.mkdirSync(path.join(pkgDir, 'bin'), { recursive: true });
  fs.copyFileSync(src, path.join(pkgDir, 'bin', info.binary));
  fs.chmodSync(path.join(pkgDir, 'bin', info.binary), 0o755);
  fs.writeFileSync(
    path.join(pkgDir, 'package.json'),
    JSON.stringify(
      {
        name,
        version,
        description: `shellshare binary for ${info.os} ${info.cpu}`,
        os: [info.os],
        cpu: [info.cpu],
        files: [`bin/${info.binary}`],
        license: 'Apache-2.0',
        repository: { type: 'git', url: 'git+https://github.com/vitorbaptista/shellshare.git' },
        homepage: 'https://shellshare.net',
      },
      null,
      2,
    ) + '\n',
  );
  console.log(`staged ${name}@${version} (${(size / 1024 / 1024).toFixed(1)} MB)`);
}

const metaSrc = path.join(npmDir, 'shellshare');
const metaDir = path.join(outDir, 'shellshare');
fs.mkdirSync(path.join(metaDir, 'bin'), { recursive: true });
fs.copyFileSync(path.join(metaSrc, 'bin', 'shellshare.js'), path.join(metaDir, 'bin', 'shellshare.js'));
fs.chmodSync(path.join(metaDir, 'bin', 'shellshare.js'), 0o755);
fs.copyFileSync(path.join(metaSrc, 'README.md'), path.join(metaDir, 'README.md'));

const meta = JSON.parse(fs.readFileSync(path.join(metaSrc, 'package.json'), 'utf8'));
meta.version = version;
meta.optionalDependencies = Object.fromEntries(Object.keys(platforms).map((name) => [name, version]));
fs.writeFileSync(path.join(metaDir, 'package.json'), JSON.stringify(meta, null, 2) + '\n');
console.log(`staged shellshare@${version} (meta package)`);
