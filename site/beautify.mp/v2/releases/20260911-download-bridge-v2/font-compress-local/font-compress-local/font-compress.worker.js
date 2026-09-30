/* global importScripts, loadPyodide */

/**
 * 字体压缩 Worker。
 *
 * 处理核心是本目录下的 font_compress_processor.py；Pyodide / FontTools 运行时
 * 复用字体调整工具已 vendored 的 vendor/pyodide，避免再复制约 13MB 资产。
 * 即使当前 Worker 从历史缓存中的大写目录启动，也只规范化本工具与共享 runtime
 * 的已知路径段，防止 Linux 线上 404。
 */

function canonicalCompressRootUrl() {
  const rootUrl = new URL('./', self.location.href);
  const canonicalSegment = '/static/font-compress-local/';
  const segmentIndex = rootUrl.pathname.toLowerCase().indexOf(canonicalSegment);
  if (segmentIndex >= 0) {
    rootUrl.pathname = `${rootUrl.pathname.slice(0, segmentIndex)}${canonicalSegment}${rootUrl.pathname.slice(segmentIndex + canonicalSegment.length)}`;
  }
  return rootUrl;
}

function sharedAdjustmentVendorRoot(compressRoot) {
  // 共享运行时始终指向字体调整目录的 vendor。Gateway v2 发布时由 release-assets
  // 把同一 logicalPath 的 toolScope 扩到 font_compress，保证密文包里也能解析。
  const vendorUrl = new URL('../font-adjustment-local/vendor/', compressRoot);
  const lower = vendorUrl.pathname.toLowerCase();
  const marker = '/static/font-adjustment-local/';
  const index = lower.indexOf(marker);
  if (index >= 0) {
    vendorUrl.pathname = `${vendorUrl.pathname.slice(0, index)}${marker}${vendorUrl.pathname.slice(index + marker.length)}`;
  }
  return vendorUrl;
}

const ROOT_URL = canonicalCompressRootUrl();
const VENDOR_ROOT = sharedAdjustmentVendorRoot(ROOT_URL);
const LOCAL_PYODIDE_URL = new URL('pyodide/', VENDOR_ROOT).href;
const workerQuery = new URL(self.location.href).searchParams;
const RUNTIME_SOURCE_ORIGINS = Object.freeze({
  jsdelivr: 'https://cdn.jsdelivr.net',
  'jsdelivr-fastly': 'https://fastly.jsdelivr.net',
  'jsdelivr-gcore': 'https://gcore.jsdelivr.net',
});
const requestedRuntimeSource = workerQuery.get('runtimeSource') || '';
const runtimeSource = requestedRuntimeSource === 'local'
  || Object.prototype.hasOwnProperty.call(RUNTIME_SOURCE_ORIGINS, requestedRuntimeSource)
  ? requestedRuntimeSource
  : 'local';
const runtimeAssetVersion = workerQuery.get('v') || '1.0.0';
const REQUIRED_PYODIDE_ASSETS = [
  'pyodide.js',
  'pyodide.asm.js',
  'pyodide.asm.wasm',
  'python_stdlib.zip',
  'pyodide-lock.json',
  'fonttools-4.56.0-py3-none-any.whl',
];

// Web Worker 不继承页面 fetch 包装器；与字体调整一样在 Worker 内安装 522 重试。
importScripts(new URL('../fetch-522-retry.js', ROOT_URL).href);

let pyodidePromise = null;
let analyzeProxy = null;
let processProxy = null;
let extractSvgProxy = null;

function post(type, payload = {}, transfer = []) {
  self.postMessage({ type, ...payload }, transfer);
}

async function resolveCdnPyodideUrl(sourceId) {
  const sourceOrigin = RUNTIME_SOURCE_ORIGINS[sourceId];
  if (!sourceOrigin) throw new Error('字体运行节点标识无效');
  const manifestUrl = new URL('runtime-assets.json', VENDOR_ROOT);
  manifestUrl.searchParams.set('v', runtimeAssetVersion);
  const response = await fetch(manifestUrl.href, { cache: 'no-cache' });
  if (!response.ok) throw new Error(`字体运行组件清单加载失败（${response.status}）`);
  const manifest = await response.json();
  const assets = Array.isArray(manifest && manifest.assets) ? manifest.assets : [];
  const byName = new Map();
  for (const asset of assets) {
    const path = String(asset && asset.path || '');
    const name = path.split('/').pop();
    if (name) byName.set(name, String(asset.url || ''));
  }

  const pyodideScriptUrl = byName.get('pyodide.js');
  if (!pyodideScriptUrl) throw new Error('字体运行组件清单缺少 Pyodide 入口');
  const canonicalRoot = new URL('./', pyodideScriptUrl);
  if (canonicalRoot.origin !== RUNTIME_SOURCE_ORIGINS.jsdelivr) {
    throw new Error('字体运行组件清单来源无效');
  }
  const cdnRoot = new URL(canonicalRoot.pathname, sourceOrigin).href;
  for (const fileName of REQUIRED_PYODIDE_ASSETS) {
    const declaredUrl = byName.get(fileName);
    if (!declaredUrl || new URL(declaredUrl).href !== new URL(fileName, canonicalRoot).href) {
      throw new Error(`字体运行组件清单中的 ${fileName} 版本不一致`);
    }
  }
  self.__JUNEOVER24_FETCH_522_RETRY_LIMITS__ = { [sourceOrigin]: 2 };
  return cdnRoot;
}

function importRuntimeScript(url) {
  let lastError = null;
  for (let attempt = 1; attempt <= 3; attempt += 1) {
    try {
      importScripts(url);
      return;
    } catch (error) {
      lastError = error;
      if (attempt < 3) console.warn(`[font-compress] 字体运行入口加载失败，立即重试 ${attempt}/2`);
    }
  }
  throw lastError || new Error('字体运行入口加载失败');
}

async function ensureRuntime() {
  if (!pyodidePromise) {
    pyodidePromise = (async () => {
      post('runtime-progress', { stage: 'runtime', message: '正在加载运行组件…' });
      const pyodideUrl = runtimeSource === 'local'
        ? LOCAL_PYODIDE_URL
        : await resolveCdnPyodideUrl(runtimeSource);
      importRuntimeScript(new URL('pyodide.js', pyodideUrl).href);
      const pyodide = await loadPyodide({ indexURL: pyodideUrl });
      post('runtime-progress', { stage: 'fonttools', message: '正在加载字体处理组件…' });
      await pyodide.loadPackage('fonttools');
      // 裸 CFF1 适配器必须先注入同一 Pyodide 全局，再注入压缩核心；这样处理器
      // 的标准 TTF/OTF 路径不变，裸流仅增加一次受限 OTTO 封装。两份源码都带版本
      // 查询参数，继续沿用现有静态缓存和失败重试语义。
      const adapterUrl = new URL('bare_cff_adapter.py', ROOT_URL);
      adapterUrl.searchParams.set('v', runtimeAssetVersion);
      const adapterSource = await fetch(adapterUrl.href, { cache: 'no-cache' }).then((response) => {
        if (!response.ok) throw new Error(`裸 CFF 适配器加载失败（${response.status}）`);
        return response.text();
      });
      pyodide.runPython(adapterSource);

      const processorUrl = new URL('font_compress_processor.py', ROOT_URL);
      processorUrl.searchParams.set('v', runtimeAssetVersion);
      const source = await fetch(processorUrl.href, { cache: 'no-cache' }).then((response) => {
        if (!response.ok) throw new Error(`字体压缩核心加载失败（${response.status}）`);
        return response.text();
      });
      pyodide.runPython(source);
      analyzeProxy = pyodide.globals.get('analyze_font_file_json');
      processProxy = pyodide.globals.get('process_font_file_json');
      extractSvgProxy = pyodide.globals.get('extract_svg_documents_json');
      post('runtime-ready');
      return pyodide;
    })().catch((error) => {
      pyodidePromise = null;
      throw error;
    });
  }
  return pyodidePromise;
}

function safeName(value) {
  return String(value || 'font').replace(/[^a-zA-Z0-9._-]+/g, '_').slice(-80) || 'font';
}

async function withInputFile(requestId, bytes, filename, callback) {
  const pyodide = await ensureRuntime();
  const suffix = safeName(filename);
  const inputPath = `/tmp/font-compress-${requestId}-${suffix}`;
  const outputPath = `/tmp/font-compress-output-${requestId}-${suffix}`;
  try {
    pyodide.FS.writeFile(inputPath, new Uint8Array(bytes));
    return await callback(pyodide, inputPath, outputPath);
  } finally {
    for (const path of [inputPath, outputPath]) {
      try { pyodide.FS.unlink(path); } catch (_) { /* ignore */ }
    }
  }
}

self.onmessage = async (event) => {
  const message = event.data || {};
  const requestId = String(message.requestId || 'unknown');
  try {
    if (message.type === 'init') {
      await ensureRuntime();
      post('response', { requestId, ok: true, result: { ready: true } });
      return;
    }

    if (message.type === 'analyze') {
      const result = await withInputFile(requestId, message.bytes, message.filename, async (_pyodide, inputPath) => {
        post('progress', { requestId, stage: 'analyze', message: '正在解析字体结构…' });
        return JSON.parse(String(analyzeProxy(inputPath, String(message.filename || 'font'))));
      });
      if (!result.ok) throw new Error(result.error || '字体分析失败');
      post('response', { requestId, ok: true, result });
      return;
    }

    if (message.type === 'extract-svg') {
      const result = await withInputFile(requestId, message.bytes, message.filename, async (_pyodide, inputPath) => {
        post('progress', { requestId, stage: 'svg', message: '正在提取彩色位图…' });
        return JSON.parse(String(extractSvgProxy(inputPath)));
      });
      if (!result.ok) throw new Error(result.error || 'SVG 文档提取失败');
      post('response', { requestId, ok: true, result });
      return;
    }

    if (message.type === 'process') {
      post('progress', { requestId, stage: 'process', message: '正在压缩字体…' });
      const response = await withInputFile(requestId, message.bytes, message.filename, async (pyodide, inputPath, outputPath) => {
        const result = JSON.parse(String(processProxy(
          inputPath,
          outputPath,
          JSON.stringify({ ...(message.options || {}), filename: message.filename || 'font' }),
        )));
        if (!result.ok) throw new Error(result.error || '字体压缩失败');
        post('progress', { requestId, stage: 'verify', message: '正在生成字体文件…' });
        const output = pyodide.FS.readFile(outputPath);
        return { result, output };
      });
      post('response', {
        requestId,
        ok: true,
        result: response.result,
        output: response.output.buffer,
      }, [response.output.buffer]);
      return;
    }

    throw new Error('字体压缩请求无效');
  } catch (error) {
    post('response', {
      requestId,
      ok: false,
      runtimeSource,
      error: String(error && error.message ? error.message : error || '处理失败'),
    });
  }
};
