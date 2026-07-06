#!/usr/bin/env node
// FOLLOW an end-to-end-encrypted shellshare broadcast live over the
// viewer WebSocket, decoding to plain text as records stream in.
//
//   node follow.mjs '<share-url-with-#key>' [--seconds N] [--idle N] [--until REGEX]
//
// Needs Node >=22 (built-in global WebSocket). Snapshot-only runtimes
// (no WebSocket) can poll `node decrypt.mjs URL` instead.
//
// Stop conditions (so an agent's call always returns):
//   --seconds N   stop after N seconds   (N=0 -> run until killed)
//   --idle N      stop after N seconds with no new output
//   --until RE    stop once decoded output matches the regex RE
//   (no flag)     defaults to --seconds 30
//
// Two usage patterns:
//   * Bounded window:  node follow.mjs URL --seconds 15   (blocks, returns the window)
//   * Background tail: nohup node follow.mjs URL --seconds 0 > out.log 2>&1 &
//                      ...then `tail -f out.log` / re-read out.log across turns.
//
// Wire format on the WS: binary frames carry ciphertext records
//   [u32 BE N][12-byte nonce][ciphertext || 16-byte tag], AES-256-GCM,
//   N = 12 + len(ct||tag); the first binary frame is the catch-up
//   history. Text frames are control JSON (size/broadcasting/usersCount)
//   and are ignored. Key = 64 hex chars after '#' in the link.

import crypto from 'node:crypto';

const args = process.argv.slice(2);
const url = args.find((a) => !a.startsWith('--'));
const has = (name) => args.includes(`--${name}`);
const val = (name, def) => {
  const i = args.indexOf(`--${name}`);
  return i >= 0 && args[i + 1] !== undefined && !args[i + 1].startsWith('--') ? args[i + 1] : def;
};

if (!url) {
  console.error("usage: node follow.mjs '<url-with-#key>' [--seconds N] [--idle N] [--until REGEX]");
  process.exit(1);
}

const u = new URL(url);
const keyHex = u.hash.replace(/^#/, '');
if (!/^[0-9a-fA-F]{64}$/.test(keyHex)) {
  console.error('share URL has no 32-byte hex key in its #fragment (or the broadcast is plaintext)');
  process.exit(1);
}
const key = Buffer.from(keyHex, 'hex');
const room = u.pathname.replace(/^\/r\//, '');
const wsUrl = `${u.protocol === 'https:' ? 'wss:' : 'ws:'}//${u.host}/ws/v/r/${room}`;

const timeBound = has('seconds') ? Number(val('seconds', 0)) : has('idle') || has('until') ? 0 : 30;
const idleSecs = has('idle') ? Number(val('idle', 0)) : 0;
const until = has('until') ? new RegExp(val('until', '')) : null;

// Clean the terminal stream for a log: strip ANSI escapes and fold the
// terminal's CRLF line endings to plain LF.
const cleanLog = (s) => s.replace(/\x1b\[[0-9;?]*[A-Za-z]/g, '').replace(/\x1b\][^\x07]*\x07/g, '').replace(/\r\n/g, '\n');

let done = false;
const finish = (why, code = 0) => {
  if (done) return;
  done = true;
  process.stderr.write(`\n[follow stopped: ${why}]\n`);
  try { ws.close(); } catch {}
  process.exit(code);
};

if (timeBound > 0) setTimeout(() => finish(`${timeBound}s elapsed`), timeBound * 1000).unref();
let idleTimer;
const bumpIdle = () => {
  if (idleSecs > 0) {
    clearTimeout(idleTimer);
    idleTimer = setTimeout(() => finish(`${idleSecs}s idle`), idleSecs * 1000).unref();
  }
};

const ws = new WebSocket(wsUrl);
ws.binaryType = 'arraybuffer';

let buf = Buffer.alloc(0);
let acc = '';

ws.onopen = () => bumpIdle();
ws.onerror = (e) => finish(`ws error: ${e?.message ?? 'connection failed'}`, 1);
ws.onclose = () => finish('connection closed');
ws.onmessage = (ev) => {
  if (typeof ev.data === 'string') return; // control frame (size/broadcasting/usersCount)
  buf = Buffer.concat([buf, Buffer.from(ev.data)]);
  bumpIdle();
  let o = 0;
  while (o + 4 <= buf.length) {
    const n = buf.readUInt32BE(o);
    if (o + 4 + n > buf.length) break; // wait for the rest of this record
    const iv = buf.subarray(o + 4, o + 4 + 12);
    const body = buf.subarray(o + 4 + 12, o + 4 + n); // ciphertext || tag
    o += 4 + n;
    const d = crypto.createDecipheriv('aes-256-gcm', key, iv);
    d.setAuthTag(body.subarray(body.length - 16));
    let txt;
    try {
      txt = Buffer.concat([d.update(body.subarray(0, body.length - 16)), d.final()]).toString('utf8');
    } catch {
      finish('decryption failed - wrong key?', 1);
      return;
    }
    const clean = cleanLog(txt);
    process.stdout.write(clean);
    acc += clean;
    if (until && until.test(acc)) {
      finish(`matched /${until.source}/`);
      return;
    }
  }
  buf = buf.subarray(o);
};
