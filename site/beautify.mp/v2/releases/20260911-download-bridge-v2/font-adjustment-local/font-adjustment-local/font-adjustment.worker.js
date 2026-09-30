/* global importScripts, loadPyodide */

/**
 * 生成规范的小写资源根地址。
 *
 * 即使当前 Worker 是从历史缓存中的大写目录启动，也不能继续把错误大小写传播给
 * Pyodide、WASM 和 Python 核心资源；否则 Linux 静态目录会返回 404。这里只替换
 * 已知目录段，保留来源域名、部署前缀以及 Worker 自身的版本查询参数之外的结构。
 */
function canonicalRootUrl() {
  const rootUrl = new URL('./', self.location.href);
  const canonicalSegment = '/static/font-adjustment-local/';
  const segmentIndex = rootUrl.pathname.toLowerCase().indexOf(canonicalSegment);
  if (segmentIndex >= 0) {
    rootUrl.pathname = `${rootUrl.pathname.slice(0, segmentIndex)}${canonicalSegment}${rootUrl.pathname.slice(segmentIndex + canonicalSegment.length)}`;
  }
  return rootUrl;
}

const ROOT_URL = canonicalRootUrl();
const LOCAL_PYODIDE_URL = new URL('vendor/pyodide/', ROOT_URL).href;
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
const runtimeAssetVersion = workerQuery.get('v') || '1.9.4';
const REQUIRED_PYODIDE_ASSETS = [
  'pyodide.js',
  'pyodide.asm.js',
  'pyodide.asm.wasm',
  'python_stdlib.zip',
  'pyodide-lock.json',
  'fonttools-4.56.0-py3-none-any.whl',
];

// Web Worker 拥有独立全局环境，不会继承页面中安装的 fetch 包装器。先在 Worker 内
// 安装同一策略；自有资源保持持续重试，第三方来源会在解析清单后设置有限重试次数，
// 以便持续异常时能够退出当前 Worker 并切换备用资源。
importScripts(new URL('../fetch-522-retry.js', ROOT_URL).href);

let pyodidePromise = null;
let analyzeProxy = null;
let processProxy = null;

function post(type, payload = {}, transfer = []) {
  self.postMessage({ type, ...payload }, transfer);
}

/**
 * 从仓库固定资产清单解析 Pyodide CDN 根地址。
 *
 * runtime-assets.json 同时记录官方来源、文件大小和 SHA-256，是第三方运行时版本的
 * 唯一事实来源。Worker 不再另写一份容易漂移的 CDN 版本；并且要求六个关键文件都
 * 位于同一目录，避免 pyodide.js 来自一个版本、WASM 或 FontTools 却来自另一个版本。
 */
async function resolveCdnPyodideUrl(sourceId) {
  const sourceOrigin = RUNTIME_SOURCE_ORIGINS[sourceId];
  if (!sourceOrigin) throw new Error('字体运行节点标识无效');
  const manifestUrl = new URL('vendor/runtime-assets.json', ROOT_URL);
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

  // query 只允许固定 source ID，真实根地址始终由清单路径和内置 origin 映射组合；
  // 不接受任意 URL，避免页面参数把 importScripts 和 WASM 加载导向未授权来源。
  self.__JUNEOVER24_FETCH_522_RETRY_LIMITS__ = {
    [sourceOrigin]: 2,
  };
  return cdnRoot;
}

/** importScripts 本身不经过 fetch 补丁，因此对入口脚本单独做三次立即尝试。 */
function importRuntimeScript(url) {
  let lastError = null;
  for (let attempt = 1; attempt <= 3; attempt += 1) {
    try {
      importScripts(url);
      return;
    } catch (error) {
      lastError = error;
      if (attempt < 3) console.warn(`[font-adjustment] 字体运行入口加载失败，立即重试 ${attempt}/2`);
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
      const adapterUrl = new URL('bare_cff_adapter.py', ROOT_URL);
      adapterUrl.searchParams.set('v', runtimeAssetVersion);
      const adapterSource = await fetch(adapterUrl.href, { cache: 'no-cache' }).then((response) => {
        if (!response.ok) throw new Error(`裸 CFF 适配器加载失败（${response.status}）`);
        return response.text();
      });
      // 先把 adapter 注册为真正的 bare_cff_adapter module，再执行处理核心；这样
      // font_processor.py 只能复用同目录的单一 CFF1 规范化实现，不会在 Pyodide 的
      // __main__ 命名空间里形成第二套隐式实现。运行时生命周期内保留该模块，任务级
      // 字体输入/输出临时文件仍由 withInputFile 的 finally 独立清理。
      pyodide.FS.writeFile('/tmp/bare_cff_adapter.py', new TextEncoder().encode(adapterSource));
      pyodide.runPython(`
import importlib.util
import sys
_adapter_spec = importlib.util.spec_from_file_location(
    'bare_cff_adapter', '/tmp/bare_cff_adapter.py'
)
if _adapter_spec is None or _adapter_spec.loader is None:
    raise RuntimeError('裸 CFF 适配器模块加载失败')
_adapter_module = importlib.util.module_from_spec(_adapter_spec)
sys.modules['bare_cff_adapter'] = _adapter_module
_adapter_spec.loader.exec_module(_adapter_module)
del _adapter_spec, _adapter_module
`);
      const processorUrl = new URL('font_processor.py', ROOT_URL);
      processorUrl.searchParams.set('v', runtimeAssetVersion);
      const source = await fetch(processorUrl.href, { cache: 'no-cache' }).then((response) => {
        if (!response.ok) throw new Error(`字体处理核心加载失败（${response.status}）`);
        return response.text();
      });
      pyodide.runPython(source);
      analyzeProxy = pyodide.globals.get('analyze_font_file_json');
      processProxy = pyodide.globals.get('process_font_file_json');
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
  const inputPath = `/tmp/font-adjustment-${requestId}-${suffix}`;
  const outputPath = `/tmp/font-adjustment-output-${requestId}-${suffix}`;
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
      const response = await withInputFile(requestId, message.bytes, message.filename, async (pyodide, inputPath, outputPath) => {
        // analyze 消息可能在首次运行环境尚未准备完成时到达。必须等 ensureRuntime
        // 真正完成后再切换到“解析字体”，否则用户会把冷启动耗时误认为文件读取慢。
        post('progress', { requestId, stage: 'analyze', message: '正在解析字体结构和安全范围…' });
        const result = JSON.parse(String(analyzeProxy(inputPath, String(message.filename || 'font'))));
        if (!result.ok || result.sourceContainer !== 'bare-cff1') {
          return { result, preview: null };
        }
        // 裸 CFF1 不是 SFNT，浏览器 FontFace 不能直接加载原始字节。复用同一条
        // Python 处理入口做“无参数”规范化，既不改轮廓也不丢 glyph order，只为页面
        // 初始预览生成临时 OTTO；marker inspect 和后续正式处理仍继续使用原始输入。
        const previewResult = JSON.parse(String(processProxy(
          inputPath,
          outputPath,
          JSON.stringify({ filename: message.filename || 'font' }),
        )));
        if (!previewResult.ok) throw new Error(previewResult.error || '裸 CFF 预览字体生成失败');
        const preview = pyodide.FS.readFile(outputPath);
        return { result, preview };
      });
      if (!response.result.ok) throw new Error(response.result.error || '字体分析失败');
      const payload = {
        requestId,
        ok: true,
        result: response.result,
      };
      if (response.preview) {
        payload.preview = response.preview.buffer;
        post('response', payload, [response.preview.buffer]);
      } else {
        post('response', payload);
      }
      return;
    }

    if (message.type === 'process') {
      post('progress', { requestId, stage: 'process', message: '正在处理字体…' });
      const response = await withInputFile(requestId, message.bytes, message.filename, async (pyodide, inputPath, outputPath) => {
        const result = JSON.parse(String(processProxy(
          inputPath,
          outputPath,
          JSON.stringify({ ...(message.options || {}), filename: message.filename || 'font' }),
        )));
        if (!result.ok) throw new Error(result.error || '字体处理失败');
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

    throw new Error('字体处理请求无效');
  } catch (error) {
    post('response', {
      requestId,
      ok: false,
      runtimeSource,
      error: String(error && error.message ? error.message : error || '处理失败'),
    });
  }
};
