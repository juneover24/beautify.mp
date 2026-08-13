import assert from 'node:assert/strict';
import { readFile } from 'node:fs/promises';
import path from 'node:path';
import test from 'node:test';
import vm from 'node:vm';
import { TextEncoder } from 'node:util';
import { webcrypto } from 'node:crypto';
import { fileURLToPath } from 'node:url';

const root = path.resolve(path.dirname(fileURLToPath(import.meta.url)), '..');
const pagePath = path.join(root, 'pages', 'card-link.html');

function extractInlineScript(html) {
  const match = html.match(/<script>([\s\S]*)<\/script>/);
  assert.ok(match, 'card-link.html should keep its logic inline for static hosting');
  return match[1];
}

function createDomStub() {
  const elements = new Map();
  return {
    getElementById(id) {
      if (!elements.has(id)) {
        elements.set(id, {
          id,
          value: '',
          textContent: '',
          className: '',
          innerHTML: '',
          disabled: false,
          classList: { add() {}, remove() {} },
          addEventListener() {},
          focus() {},
          select() {},
        });
      }
      return elements.get(id);
    },
    execCommand() { return true; },
  };
}

test('short card link page source stays static and public-only', async () => {
  const html = await readFile(pagePath, 'utf8');

  assert.match(html, /const FIXED_URL_LINK = 'https:\/\/wxmpurl\.cn\/[A-Za-z0-9._~%-]+'/);
  assert.match(html, /const SCE1_PUBLIC_KEY_RAW_B64URL = '[A-Za-z0-9_-]+'/);
  assert.match(html, /crypto\.subtle\.generateKey/);
  assert.match(html, /crypto\.subtle\.deriveKey/);
  assert.match(html, /AES-GCM/);
  assert.match(html, /HKDF/);
  assert.ok(!html.includes('BEGIN PRIVATE KEY'));
  assert.ok(!html.includes('CARD_SHORT_LINK_ENVELOPE_PRIVATE_KEYS'));
  assert.ok(!/\bfetch\s*\(/.test(html));
  assert.ok(!html.includes('XMLHttpRequest'));
  assert.ok(!html.includes('sendBeacon'));
});

test('short card link page creates compact SCE1 cq and QR svg locally', async () => {
  const html = await readFile(pagePath, 'utf8');
  const script = extractInlineScript(html);
  const context = {
    TextEncoder,
    crypto: webcrypto,
    atob: value => Buffer.from(value, 'base64').toString('binary'),
    btoa: value => Buffer.from(value, 'binary').toString('base64'),
    document: createDomStub(),
    navigator: { clipboard: { writeText: async () => {} } },
    window: { clearInterval() {}, setInterval() { return 1; } },
  };
  vm.runInNewContext(`${script}\n;globalThis.__cardLinkTest = {\n  SHORT_CARD_PATTERN,\n  isUrlLinkConfigured,\n  buildLink,\n  encryptShortCard,\n  makeQrCode,\n  renderQrSvg,\n};`, context);

  const api = context.__cardLinkTest;
  assert.equal(api.SHORT_CARD_PATTERN.test('SC_Abc12345'), true);
  assert.equal(api.SHORT_CARD_PATTERN.test('GCV2_Abc12345'), false);
  assert.equal(api.isUrlLinkConfigured(), true);

  const envelope = await api.encryptShortCard('SC_Abc12345');
  assert.match(envelope.cq, /^SCE1\.sce1-k1\.[0-9a-z]+\.[A-Za-z0-9_-]+$/);
  assert.ok(envelope.cq.length <= 240);

  const link = api.buildLink(envelope.cq);
  assert.ok(link.includes('?cq=SCE1.'));
  const qr = api.makeQrCode(link);
  const svg = api.renderQrSvg(qr);
  assert.match(svg, /^<svg /);
  assert.match(svg, /<rect /);
  assert.ok(svg.length > 1000);
});
