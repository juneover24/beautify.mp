import { createHash } from 'node:crypto';
import { cp, mkdir, readFile, readdir, rm, stat, writeFile } from 'node:fs/promises';
import path from 'node:path';
import { fileURLToPath } from 'node:url';
import {
  assertSafeLogicalPath,
  createEncryptedRelease,
  importSigningPrivateKey,
  loadRecipients,
} from './release-lib.mjs';

const packageRoot = path.resolve(path.dirname(fileURLToPath(import.meta.url)), '..');
const repoRoot = path.resolve(packageRoot, '..');
const siteRoot = path.join(packageRoot, 'site');
const policyPath = path.join(packageRoot, 'release-assets.json');

async function exists(target) {
  try { await stat(target); return true; } catch (error) {
    if (error?.code === 'ENOENT') return false;
    throw error;
  }
}

async function walk(directory) {
  const result = [];
  for (const entry of await readdir(directory, { withFileTypes: true })) {
    const absolute = path.join(directory, entry.name);
    if (entry.isDirectory()) result.push(...await walk(absolute));
    else if (entry.isFile()) result.push(absolute);
  }
  return result;
}

async function validatePublishedSnapshot() {
  const current = JSON.parse(await readFile(path.join(siteRoot, 'current.json'), 'utf8'));
  if (!/^[A-Za-z0-9][A-Za-z0-9._-]{0,79}$/.test(String(current.releaseId || ''))) {
    throw new Error('site/current.json releaseId invalid');
  }
  const releaseRoot = path.join(siteRoot, 'releases', current.releaseId);
  const manifest = JSON.parse(await readFile(path.join(releaseRoot, 'manifest.json'), 'utf8'));
  if (manifest.releaseId !== current.releaseId || manifest.schemaVersion !== 1 || !manifest.signature?.value) {
    throw new Error('published release manifest invalid');
  }
  const publicFiles = await walk(siteRoot);
  const forbidden = publicFiles.filter(file => /(?:\.map(?:\.json)?|\.pyc|\.raw\.js|private\.pem)$/i.test(file));
  if (forbidden.length) throw new Error(`forbidden public files: ${forbidden.join(', ')}`);
  return manifest;
}

if (process.env.BEAUTIFY_MP_VALIDATE_SNAPSHOT === '1') {
  const manifest = await validatePublishedSnapshot();
  console.log(`validated encrypted beautify.mp release ${manifest.releaseId}`);
  process.exit(0);
}

const releaseId = String(process.env.BEAUTIFY_MP_RELEASE_ID || '').trim();
const signingKid = String(process.env.BEAUTIFY_MP_SIGNING_KEY_ID || '').trim();
const recipientsFile = path.resolve(String(process.env.BEAUTIFY_MP_RSA_RECIPIENTS_FILE || ''));
const signingPrivateFile = path.resolve(String(process.env.BEAUTIFY_MP_SIGNING_PRIVATE_KEY_FILE || ''));
if (!releaseId || !signingKid || !process.env.BEAUTIFY_MP_RSA_RECIPIENTS_FILE
  || !process.env.BEAUTIFY_MP_SIGNING_PRIVATE_KEY_FILE) {
  throw new Error('encrypted build requires release id, RSA recipients file, signing kid and signing private key file');
}

const policy = JSON.parse(await readFile(policyPath, 'utf8'));
if (policy.schemaVersion !== 1 || !Array.isArray(policy.assets)) throw new Error('release-assets.json invalid');
const seen = new Set();
const assets = [];
for (const entry of policy.assets) {
  const logicalPath = assertSafeLogicalPath(entry.logicalPath);
  if (seen.has(logicalPath)) throw new Error(`duplicate policy path: ${logicalPath}`);
  seen.add(logicalPath);
  if (!['owned', 'owned-composite', 'vendor'].includes(entry.classification)) {
    throw new Error(`unclassified release asset: ${logicalPath}`);
  }
  // 混合 bundle 只有在显式临时开关下才能发布。正式方案必须先把第三方 vendor
  // chunk 拆为明文，再将纯自研 chunk 标成 owned；这样不会悄悄退回“整包混加密”。
  if (entry.classification === 'owned-composite' && process.env.BEAUTIFY_MP_ALLOW_COMPOSITE_OWNED !== '1') {
    throw new Error(`mixed owned/vendor bundle must be split before release: ${logicalPath}`);
  }
  const source = path.resolve(repoRoot, String(entry.source || ''));
  if (!source.startsWith(`${repoRoot}${path.sep}`) || !await exists(source)) {
    throw new Error(`release source missing or outside private repository: ${entry.source}`);
  }
  assets.push({
    logicalPath,
    classification: entry.classification === 'vendor' ? 'vendor' : 'owned',
    mime: String(entry.mime || 'application/octet-stream'),
    bytes: await readFile(source),
  });
}

const recipients = await loadRecipients(recipientsFile);
const signingPrivateKey = await importSigningPrivateKey(await readFile(signingPrivateFile, 'utf8'));
const release = await createEncryptedRelease({ releaseId, signingKid, signingPrivateKey, recipients, assets });

// 所有源、策略、密钥都验证成功后才替换 site，避免配置错误先删除当前可用快照。
await rm(siteRoot, { recursive: true, force: true });
const releaseRoot = path.join(siteRoot, 'releases', releaseId);
await mkdir(releaseRoot, { recursive: true });
for (const [relative, bytes] of release.outputFiles) {
  const destination = path.join(releaseRoot, relative);
  await mkdir(path.dirname(destination), { recursive: true });
  await writeFile(destination, bytes);
}
await writeFile(path.join(releaseRoot, 'manifest.json'), `${JSON.stringify(release.manifest, null, 2)}\n`, 'utf8');
await writeFile(path.join(siteRoot, 'current.json'), `${JSON.stringify({ schemaVersion: 1, releaseId }, null, 2)}\n`, 'utf8');
await cp(path.join(packageRoot, 'pages'), siteRoot, { recursive: true, force: true });

const integrity = {};
for (const file of (await walk(siteRoot)).sort()) {
  const relative = path.relative(siteRoot, file).replaceAll('\\', '/');
  if (relative === 'integrity.json') continue;
  const bytes = await readFile(file);
  integrity[relative] = { bytes: bytes.length, sha256: createHash('sha256').update(bytes).digest('hex') };
}
await writeFile(path.join(siteRoot, 'integrity.json'), `${JSON.stringify({ schemaVersion: 2, releaseId, files: integrity }, null, 2)}\n`);
console.log(`encrypted beautify.mp release ${releaseId}: ${assets.length} classified assets`);
