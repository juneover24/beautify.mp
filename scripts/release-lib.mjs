import { createHash, webcrypto } from 'node:crypto';
import { readFile } from 'node:fs/promises';
import path from 'node:path';

const cryptoApi = globalThis.crypto || webcrypto;
const encoder = new TextEncoder();

export function canonicalize(value) {
  if (value === null || typeof value === 'boolean' || typeof value === 'string') return JSON.stringify(value);
  if (typeof value === 'number') {
    if (!Number.isFinite(value)) throw new Error('manifest contains a non-finite number');
    return JSON.stringify(value);
  }
  if (Array.isArray(value)) return `[${value.map(canonicalize).join(',')}]`;
  if (value && typeof value === 'object') {
    return `{${Object.keys(value).filter(key => value[key] !== undefined).sort()
      .map(key => `${JSON.stringify(key)}:${canonicalize(value[key])}`).join(',')}}`;
  }
  throw new Error('manifest contains an unsupported value');
}

export function sha256Hex(bytes) {
  return createHash('sha256').update(bytes).digest('hex');
}

export function base64(bytes) {
  return Buffer.from(bytes).toString('base64');
}

export function pemDer(pem, label) {
  const expression = new RegExp(`-----BEGIN ${label}-----([\\s\\S]+?)-----END ${label}-----`);
  const match = expression.exec(String(pem || ''));
  if (!match) throw new Error(`${label} PEM is missing`);
  return Buffer.from(match[1].replace(/\s+/g, ''), 'base64');
}

export function assertSafeLogicalPath(value) {
  const normalized = String(value || '').replaceAll('\\', '/');
  if (!/^[A-Za-z0-9][A-Za-z0-9._\/-]{0,399}$/.test(normalized)
    || normalized.includes('//') || normalized.split('/').includes('..')) {
    throw new Error(`unsafe logical path: ${value}`);
  }
  return normalized;
}

export function assetAad(releaseId, logicalPath, part) {
  return encoder.encode(canonicalize({
    v: part.aadVersion,
    releaseId,
    logicalPath,
    partId: part.id,
    mime: part.mime,
    plainBytes: part.bytes,
    plainSha256: part.sha256,
  }));
}

export async function importRsaPublicKey(pem) {
  return cryptoApi.subtle.importKey(
    'spki',
    pemDer(pem, 'PUBLIC KEY'),
    { name: 'RSA-OAEP', hash: 'SHA-256' },
    false,
    ['encrypt'],
  );
}

export async function importSigningPrivateKey(pem) {
  return cryptoApi.subtle.importKey(
    'pkcs8',
    pemDer(pem, 'PRIVATE KEY'),
    { name: 'ECDSA', namedCurve: 'P-256' },
    false,
    ['sign'],
  );
}

export async function loadRecipients(filePath) {
  const raw = JSON.parse(await readFile(filePath, 'utf8'));
  if (!Array.isArray(raw) || !raw.length) throw new Error('at least one RSA recipient is required');
  const baseDirectory = path.dirname(filePath);
  const seen = new Set();
  return Promise.all(raw.map(async entry => {
    const kid = String(entry?.kid || '').trim();
    if (!/^[A-Za-z0-9][A-Za-z0-9._-]{0,79}$/.test(kid) || seen.has(kid)) {
      throw new Error(`invalid or duplicate recipient kid: ${kid}`);
    }
    seen.add(kid);
    const publicKeyPem = entry.publicKeyPem
      ? String(entry.publicKeyPem)
      : await readFile(path.resolve(baseDirectory, String(entry.publicKeyPemFile || '')), 'utf8');
    return { kid, publicKey: await importRsaPublicKey(publicKeyPem) };
  }));
}

export async function createEncryptedRelease({ releaseId, signingKid, signingPrivateKey, recipients, assets }) {
  if (!/^[A-Za-z0-9][A-Za-z0-9._-]{0,79}$/.test(releaseId)) throw new Error('invalid release id');
  if (!/^[A-Za-z0-9][A-Za-z0-9._-]{0,79}$/.test(signingKid)) throw new Error('invalid signing kid');

  const dekBytes = cryptoApi.getRandomValues(new Uint8Array(32));
  const dek = await cryptoApi.subtle.importKey('raw', dekBytes, { name: 'AES-GCM' }, false, ['encrypt']);
  const wrappedRecipients = await Promise.all(recipients.map(async recipient => ({
    kid: recipient.kid,
    alg: 'RSA-OAEP-256',
    wrappedKey: base64(await cryptoApi.subtle.encrypt(
      { name: 'RSA-OAEP', label: encoder.encode(`beautify.mp:${releaseId}`) },
      recipient.publicKey,
      dekBytes,
    )),
  })));

  const routes = {};
  const outputFiles = new Map();
  const usedIvs = new Set();
  for (const asset of assets) {
    const logicalPath = assertSafeLogicalPath(asset.logicalPath);
    if (routes[logicalPath]) throw new Error(`duplicate logical path: ${logicalPath}`);
    const bytes = new Uint8Array(asset.bytes);
    const plainSha256 = sha256Hex(bytes);
    const id = `asset-${plainSha256.slice(0, 20)}`;
    if (asset.classification === 'vendor') {
      const publicPath = `public/${logicalPath}`;
      outputFiles.set(publicPath, bytes);
      routes[logicalPath] = {
        mime: asset.mime,
        cacheMode: 'immutable',
        parts: [{ id, kind: 'vendor', mime: asset.mime, bytes: bytes.byteLength, sha256: plainSha256, publicPath }],
      };
      continue;
    }

    const iv = cryptoApi.getRandomValues(new Uint8Array(12));
    const ivText = base64(iv);
    if (usedIvs.has(ivText)) throw new Error('AES-GCM IV collision');
    usedIvs.add(ivText);
    const part = {
      id,
      kind: 'owned',
      mime: asset.mime,
      bytes: bytes.byteLength,
      sha256: plainSha256,
      cipherPath: '',
      cipherSha256: '',
      iv: ivText,
      aadVersion: 1,
    };
    const cipher = new Uint8Array(await cryptoApi.subtle.encrypt(
      { name: 'AES-GCM', iv, additionalData: assetAad(releaseId, logicalPath, part), tagLength: 128 },
      dek,
      bytes,
    ));
    part.cipherSha256 = sha256Hex(cipher);
    part.cipherPath = `cipher/${part.cipherSha256}.bin`;
    outputFiles.set(part.cipherPath, cipher);
    routes[logicalPath] = { mime: asset.mime, cacheMode: 'no-store', parts: [part] };
  }

  const unsigned = {
    schemaVersion: 1,
    releaseId,
    createdAt: new Date().toISOString(),
    enc: 'A256GCM',
    recipients: wrappedRecipients,
    routes,
  };
  const signature = await cryptoApi.subtle.sign(
    { name: 'ECDSA', hash: 'SHA-256' },
    signingPrivateKey,
    encoder.encode(canonicalize(unsigned)),
  );
  return {
    manifest: {
      ...unsigned,
      signature: { kid: signingKid, alg: 'ES256', value: base64(signature) },
    },
    outputFiles,
  };
}
