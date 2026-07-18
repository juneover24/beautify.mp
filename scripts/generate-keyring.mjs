import { webcrypto } from 'node:crypto';
import { mkdir, writeFile } from 'node:fs/promises';
import path from 'node:path';

const cryptoApi = globalThis.crypto || webcrypto;
const output = path.resolve(process.argv[2] || '');
if (!process.argv[2]) throw new Error('usage: node scripts/generate-keyring.mjs <private-output-directory>');
await mkdir(output, { recursive: true });

function pem(label, bytes) {
  const body = Buffer.from(bytes).toString('base64').match(/.{1,64}/g)?.join('\n') || '';
  return `-----BEGIN ${label}-----\n${body}\n-----END ${label}-----\n`;
}

const rsa = await cryptoApi.subtle.generateKey(
  { name: 'RSA-OAEP', modulusLength: 3072, publicExponent: new Uint8Array([1, 0, 1]), hash: 'SHA-256' },
  true,
  ['encrypt', 'decrypt'],
);
const signing = await cryptoApi.subtle.generateKey(
  { name: 'ECDSA', namedCurve: 'P-256' },
  true,
  ['sign', 'verify'],
);

await writeFile(path.join(output, 'asset-rsa.private.pem'), pem('PRIVATE KEY', await cryptoApi.subtle.exportKey('pkcs8', rsa.privateKey)), { mode: 0o600 });
await writeFile(path.join(output, 'asset-rsa.public.pem'), pem('PUBLIC KEY', await cryptoApi.subtle.exportKey('spki', rsa.publicKey)));
await writeFile(path.join(output, 'manifest-signing.private.pem'), pem('PRIVATE KEY', await cryptoApi.subtle.exportKey('pkcs8', signing.privateKey)), { mode: 0o600 });
await writeFile(path.join(output, 'manifest-signing.public.pem'), pem('PUBLIC KEY', await cryptoApi.subtle.exportKey('spki', signing.publicKey)));
await writeFile(path.join(output, 'recipients.json'), `${JSON.stringify([
  { kid: 'cloudflare-primary', publicKeyPemFile: 'asset-rsa.public.pem' },
], null, 2)}\n`);
console.log(`generated RSA-OAEP and independent ECDSA keyring in ${output}`);
