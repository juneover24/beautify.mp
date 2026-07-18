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

test('public integrity inventory rejects raw bundles, source maps, pyc and private PEM files', async () => {
  const integrity = JSON.parse(await readFile(path.join(site, 'integrity.json'), 'utf8'));
  assert.equal(integrity.schemaVersion, 2);
  const paths = Object.keys(integrity.files);
  assert.ok(paths.every(file => !/\.map(?:\.json)?$/i.test(file)));
  assert.ok(paths.every(file => !/(?:^|\/)__pycache__\/|\.pyc$/i.test(file)));
  assert.ok(paths.every(file => !/\.raw\.js$|private\.pem$/i.test(file)));
});
