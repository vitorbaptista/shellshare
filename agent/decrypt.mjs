#!/usr/bin/env node
// Decode a SNAPSHOT of an end-to-end-encrypted shellshare broadcast.
//
//   node decrypt.mjs '<share-url-including-#key>'
//
// The share link looks like  http://host/r/<room>#<64-hex-key> .
// The key after '#' never reaches the server (browsers/fetch drop the
// fragment), so the server only ever stores ciphertext. We fetch the
// accumulated history from /r/<room>.bin and decrypt it locally with
// that key. Needs only Node's built-in `crypto` (no install).
//
// Wire format (one self-delimiting record per chunk):
//   [u32 BE N][12-byte nonce][ciphertext || 16-byte GCM tag]
//   where N = 12 + len(ciphertext || tag), AES-256-GCM.

import crypto from 'node:crypto';

const shareUrl = process.argv[2];
if (!shareUrl) {
  console.error("usage: node decrypt.mjs '<share-url-with-#key>'");
  process.exit(1);
}

const u = new URL(shareUrl);
const keyHex = u.hash.replace(/^#/, ''); // the part after '#'
if (!/^[0-9a-fA-F]{64}$/.test(keyHex)) {
  console.error('share URL has no 32-byte hex key in its #fragment (or the broadcast is plaintext)');
  process.exit(1);
}
const key = Buffer.from(keyHex, 'hex');

const room = u.pathname.replace(/^\/r\//, '');
const binUrl = `${u.origin}/r/${room}.bin`;

const resp = await fetch(binUrl);
if (!resp.ok) {
  console.error(`GET ${binUrl} -> ${resp.status}`);
  process.exit(1);
}
const buf = Buffer.from(await resp.arrayBuffer());

const parts = [];
let o = 0;
while (o + 4 <= buf.length) {
  const n = buf.readUInt32BE(o);
  o += 4;
  if (o + n > buf.length) break; // partial trailing record
  const iv = buf.subarray(o, o + 12);
  const body = buf.subarray(o + 12, o + n); // ciphertext || tag
  o += n;
  const d = crypto.createDecipheriv('aes-256-gcm', key, iv);
  d.setAuthTag(body.subarray(body.length - 16));
  try {
    parts.push(d.update(body.subarray(0, body.length - 16)), d.final());
  } catch {
    console.error('decryption failed for a record - wrong key?');
    process.exit(1);
  }
}

const plain = Buffer.concat(parts)
  .toString('utf8')
  .replace(/\x1b\[[0-9;?]*[A-Za-z]/g, '') // strip CSI escapes
  .replace(/\x1b\][^\x07]*\x07/g, ''); // strip OSC escapes
process.stdout.write(plain);
