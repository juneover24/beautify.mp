import assert from 'node:assert/strict';
import { readFile, stat } from 'node:fs/promises';
import path from 'node:path';
import test from 'node:test';
import { fileURLToPath } from 'node:url';

const root = path.resolve(path.dirname(fileURLToPath(import.meta.url)), '..');
const site = path.join(root, 'site');

async function currentRelease() {
  const current = JSON.parse(await readFile(path.join(site, 'current.json'), 'utf8'));
  const releaseRoot = path.join(site, 'releases', current.releaseId);
  const manifest = JSON.parse(await readFile(path.join(releaseRoot, 'manifest.json'), 'utf8'));
  return { current, releaseRoot, manifest };
}

test('public snapshot contains a signed encrypted release', async () => {
  const { current, manifest } = await currentRelease();
  assert.equal(manifest.schemaVersion, 1);
  assert.equal(manifest.releaseId, current.releaseId);
  assert.equal(manifest.enc, 'A256GCM');
  assert.equal(manifest.signature.alg, 'ES256');
  assert.ok(manifest.recipients.every(recipient => recipient.alg === 'RSA-OAEP-256'));
});

test('every first-party route is ciphertext and every public part is explicitly vendor-classified', async () => {
  const { releaseRoot, manifest } = await currentRelease();
  const requiredOwned = [
    'index.html',
    'card-link.html',
    'card-link',
    'card-link.js',
    'fetch-522-retry.js',
    'tool-h5-launch-gate.js',
    'font-adjustment-local/app.js',
    'font-adjustment-local/font-adjustment.worker.js',
    'font-adjustment-local/font_processor.py',
    'font-marker-local/marker.worker.js',
    'font-glyph-editor/editor.js',
    'psd-layer-split/app.js',
    'psd-layer-split/psd-worker.js',
  ];
  requiredOwned.forEach(logicalPath => {
    assert.ok(manifest.routes[logicalPath], logicalPath);
    assert.ok(manifest.routes[logicalPath].parts.every(part => part.kind === 'owned'), logicalPath);
  });
  for (const route of Object.values(manifest.routes)) {
    for (const part of route.parts) {
      const relative = part.kind === 'owned' ? part.cipherPath : part.publicPath;
      assert.equal((await stat(path.join(releaseRoot, relative))).isFile(), true, relative);
    }
  }
});

test('open-source runtime remains plaintext with notices while mixed bundles stay split', async () => {
  const { manifest } = await currentRelease();
  for (const logicalPath of [
    'font-adjustment-local/vendor/pyodide/pyodide.js',
    'font-adjustment-local/vendor/pyodide/fonttools-4.56.0-py3-none-any.whl',
    'font-marker-local/font-marker.vendor.js',
    'font-glyph-editor/font-glyph-editor.vendor.js',
    'psd-layer-split/psd-worker.vendor.js',
    'licenses/pako-LICENSE',
    'licenses/fonteditor-core-LICENSE',
    'licenses/ag-psd-LICENSE',
  ]) {
    assert.ok(manifest.routes[logicalPath]?.parts.every(part => part.kind === 'vendor'), logicalPath);
  }
});

test('GitHub Pages root is only a gateway bootstrap and exposes no tool source links', async () => {
  const html = await readFile(path.join(site, 'index.html'), 'utf8');
  assert.match(html, /tools\.beautify\.mp\.juneover24\.cn/);
  assert.ok(!html.includes('./font-adjustment-local/index.html'));
  assert.ok(!html.includes('font_processor.py'));
});

test('tools root and card-link routes share the encrypted card-link source', async () => {
  const policy = JSON.parse(await readFile(path.join(root, 'release-assets.json'), 'utf8'));
  const expected = new Map([
    ['index.html', ['beautify.mp/pages/card-link.html', 'text/html; charset=utf-8']],
    ['card-link.html', ['beautify.mp/pages/card-link.html', 'text/html; charset=utf-8']],
    ['card-link', ['beautify.mp/pages/card-link.html', 'text/html; charset=utf-8']],
    ['card-link.js', ['beautify.mp/pages/card-link.js', 'text/javascript; charset=utf-8']],
  ]);
  for (const [logicalPath, [source, mime]] of expected.entries()) {
    const entry = policy.assets.find(asset => asset.logicalPath === logicalPath);
    assert.ok(entry, logicalPath);
    assert.equal(entry.source, source);
    assert.equal(entry.classification, 'owned');
    assert.equal(entry.mime, mime);
  }

  const { manifest } = await currentRelease();
  const rootPart = manifest.routes['index.html']?.parts?.[0];
  const cardPart = manifest.routes['card-link.html']?.parts?.[0];
  const scriptPart = manifest.routes['card-link.js']?.parts?.[0];
  assert.equal(rootPart?.kind, 'owned');
  assert.equal(cardPart?.kind, 'owned');
  assert.equal(scriptPart?.kind, 'owned');
  // index.html、card-link.html 和 card-link 都是同一个卡密页源文件；密文文件仍按
  // logicalPath 独立生成，因为 AAD 会绑定路由名，避免跨路由替换密文也能解密。
  assert.equal(rootPart.sha256, cardPart.sha256);
  assert.equal(rootPart.bytes, cardPart.bytes);
  assert.notEqual(rootPart.cipherPath, cardPart.cipherPath);
});

test('public integrity inventory rejects raw bundles, source maps, pyc and private PEM files', async () => {
  const integrity = JSON.parse(await readFile(path.join(site, 'integrity.json'), 'utf8'));
  assert.equal(integrity.schemaVersion, 2);
  const paths = Object.keys(integrity.files);
  assert.ok(!paths.includes('card-link.html'));
  assert.ok(!paths.includes('card-link/index.html'));
  assert.ok(!paths.includes('card-link.js'));
  assert.ok(paths.every(file => !/\.map(?:\.json)?$/i.test(file)));
  assert.ok(paths.every(file => !/(?:^|\/)__pycache__\/|\.pyc$/i.test(file)));
  assert.ok(paths.every(file => !/\.raw\.js$|private\.pem$/i.test(file)));
});
