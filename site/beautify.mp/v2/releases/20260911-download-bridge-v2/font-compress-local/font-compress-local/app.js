(() => {
  'use strict';

  // 与字体调整工具同源同构：小程序携带 pageVersion 作为缓存标识，但页面当前版本
  // 必须始终优先，避免旧版本小程序把新页面指向旧缓存中的 Worker 与 Python 核心。
  const query = new URLSearchParams(location.search);
  const inMiniProgram = query.get('miniProgram') === '1';
  const CURRENT_ASSET_VERSION = '1.0.0';
  const requestedAssetVersion = query.get('pageVersion');
  const assetVersion = requestedAssetVersion === CURRENT_ASSET_VERSION
    ? requestedAssetVersion
    : CURRENT_ASSET_VERSION;
  const bridgeTransferId = query.get('transferId') || `font_${Date.now()}_${Math.random().toString(36).slice(2, 10)}`;
  const CHUNK_BYTES = 1024 * 1024;
  const RUNTIME_PROBE_TIMEOUT_MS = 2_500;
  const RUNTIME_RACE_STAGGER_MS = 4_000;
  const RUNTIME_CANDIDATE_TIMEOUT_MS = 45_000;
  const MAX_HEAVY_RUNTIME_WORKERS = 2;
  // 运行时按响应速度竞速；自有本地源与调整工具共享 vendor/pyodide，避免再复制约 13MB 资产。
  const RUNTIME_SOURCE_PROFILES = Object.freeze({
    local: Object.freeze({ priority: 0, probeUrl: '../font-adjustment-local/vendor/pyodide/pyodide.js' }),
    jsdelivr: Object.freeze({ priority: 1, probeUrl: 'https://cdn.jsdelivr.net/pyodide/v0.29.3/full/pyodide.js' }),
    'jsdelivr-fastly': Object.freeze({ priority: 2, probeUrl: 'https://fastly.jsdelivr.net/pyodide/v0.29.3/full/pyodide.js' }),
    'jsdelivr-gcore': Object.freeze({ priority: 3, probeUrl: 'https://gcore.jsdelivr.net/pyodide/v0.29.3/full/pyodide.js' }),
  });
  // 与字体调整一致：该值只用于跨端算法兼容，不承担鉴权用途。
  const FONT_MARKER_STEGO_KEY = '24';
  let bridgeSequence = 0;

  // 字集预设为构建期静态表；运行时按需 fetch 解析成 Set<number>（U+XXXX 每行）。
  const CHARSET_PRESETS = [
    { id: 'ascii', label: '基础拉丁', desc: 'ASCII 常用字符', file: 'charsets/ascii.txt' },
    { id: 'basic_symbols', label: '基础符号', desc: '标点、假名、全角、拉丁扩展、注音等', file: 'charsets/basic_symbols.txt' },
    { id: 'gb2312_l1', label: 'GB2312 一级', desc: '简体常用字 3755 个', file: 'charsets/gb2312_l1.txt' },
    { id: 'gb2312_all', label: 'GB2312 全量', desc: '简体全部 6763 个', file: 'charsets/gb2312_all.txt' },
    { id: 'big5_l1', label: 'Big5 一级', desc: '繁体常用字', file: 'charsets/big5_l1.txt' },
    { id: 'big5_all', label: 'Big5 全量', desc: '繁体全部', file: 'charsets/big5_all.txt' },
    { id: 'sc_tc_full', label: '简繁全量', desc: 'GB2312 全部 + Big5 全部 + 基础符号，中文场景推荐', file: 'charsets/sc_tc_full.txt' },
  ];

  const elements = Object.fromEntries([
    'font-file', 'picker-title', 'picker-subtitle', 'launch-notice', 'runtime-status', 'font-meta', 'warnings',
    'controls-card', 'action-card', 'result-card', 'charset-grid', 'charset-required', 'charset-output',
    'custom-text', 'custom-output', 'hit-summary', 'opt-no-hinting', 'opt-desubroutinize', 'opt-layout-closure',
    'opt-retain-gids', 'opt-svg-downsample', 'svg-edge', 'option-output', 'option-hint', 'progress-copy',
    'progress-bar', 'process-button', 'cancel-button', 'result-title', 'result-summary', 'result-warnings',
    'download-button', 'return-button', 'delivery-hint', 'toast',
  ].map((id) => [id, document.getElementById(id)]));

  const state = {
    worker: null,
    workerReady: false,
    workerRuntimeSource: 'local',
    runtimeCandidates: new Map(),
    runtimeRaceQueue: [],
    runtimeRaceFailures: [],
    runtimeRaceGeneration: 0,
    runtimeRaceStaggerTimer: null,
    runtimeLocalOnly: false,
    pending: new Map(),
    requestSequence: 0,
    markerWorker: null,
    markerPending: new Map(),
    markerRequestSequence: 0,
    file: null,
    analysis: null,
    // 只保存从本次原始字体中确认恢复出的 metadata。未标记字体始终为 null，
    // 因而不会因为经过字体压缩而被自动添加新的标记。
    sourceMarkerMetadata: null,
    outputBuffer: null,
    outputBlob: null,
    outputName: '',
    outputReport: null,
    launchAuthorized: false,
    busy: false,
    toastTimer: null,
    selectionVersion: 0,
    processingVersion: 0,
    // 字集选择与自定义码点：selectedUnion 由两者并集得到，用户必须主动勾选/填写才能开始。
    selectedPresets: new Set(),
    presetCache: new Map(),
    presetLoading: new Map(),
    presetInputs: new Map(),
    customCodepoints: new Set(),
    // SVG 内嵌位图降采样结果；仅在勾选且字体含位图时写入。
    svgDocuments: null,
    svgDownsampledCount: 0,
    svgWarnings: [],
  };

  function formatBytes(value) {
    const bytes = Number(value) || 0;
    if (bytes < 1024) return `${bytes} B`;
    if (bytes < 1024 * 1024) return `${(bytes / 1024).toFixed(1)} KB`;
    return `${(bytes / 1024 / 1024).toFixed(2)} MB`;
  }

  function toast(message) {
    elements.toast.textContent = message;
    elements.toast.classList.add('show');
    clearTimeout(state.toastTimer);
    state.toastTimer = setTimeout(() => elements.toast.classList.remove('show'), 2400);
  }

  function setRuntimeStatus(message, kind = '') {
    elements['runtime-status'].textContent = message;
    elements['runtime-status'].className = `runtime-status ${kind}`.trim();
  }

  function setProgress(message, percent) {
    elements['progress-copy'].textContent = message;
    elements['progress-bar'].style.width = `${Math.max(0, Math.min(100, percent))}%`;
  }

  /**
   * 返回字体压缩工具的规范静态资源地址。
   *
   * 历史入口曾出现目录首字母大写的地址。Windows 开发环境不会暴露问题，但在 Linux
   * 线上会让 Worker 继续按错误大小写加载后续脚本。这里仅规范已知目录段，同时保留
   * 当前域名和可能存在的部署前缀，确保页面从旧缓存入口恢复后也能自愈。
   */
  function fontCompressAssetUrl(relativePath) {
    const baseUrl = new URL('./', location.href);
    const canonicalSegment = '/static/font-compress-local/';
    const segmentIndex = baseUrl.pathname.toLowerCase().indexOf(canonicalSegment);
    if (segmentIndex >= 0) {
      baseUrl.pathname = `${baseUrl.pathname.slice(0, segmentIndex)}${canonicalSegment}${baseUrl.pathname.slice(segmentIndex + canonicalSegment.length)}`;
    }
    return new URL(relativePath, baseUrl);
  }

  /** 根据权限、运行时、已选字集与当前任务统一控制两个触发字体处理的入口。 */
  function syncRuntimeDependentControls() {
    elements['font-file'].disabled = (
      !state.launchAuthorized
      || state.busy
      || !state.workerReady
    );
    syncProcessEnabled();
  }

  /**
   * 开始压缩必须同时满足：启动授权、Worker 就绪、已选文件、分析完成、且至少有一个
   * 有效码点。字集与自定义文字都必须由用户主动提供，不允许静默默认。
   */
  function syncProcessEnabled() {
    const canStart = (
      state.launchAuthorized
      && !state.busy
      && state.workerReady
      && state.file
      && state.analysis
      && selectedUnion().size > 0
    );
    elements['process-button'].disabled = !canStart;
  }

  function escapeHtml(value) {
    return String(value).replace(/[&<>'"]/g, (char) => (
      { '&': '&amp;', '<': '&lt;', '>': '&gt;', "'": '&#39;', '"': '&quot;' }[char]
    ));
  }

  function showWarnings(target, warnings) {
    const list = Array.isArray(warnings) ? warnings.filter(Boolean) : [];
    target.innerHTML = list.map((warning) => `<div>• ${escapeHtml(warning)}</div>`).join('');
    target.classList.toggle('hidden', list.length === 0);
  }

  function clearRuntimeRaceTimers() {
    if (state.runtimeRaceStaggerTimer !== null) {
      clearTimeout(state.runtimeRaceStaggerTimer);
      state.runtimeRaceStaggerTimer = null;
    }
    for (const candidate of state.runtimeCandidates.values()) {
      if (candidate.timeoutId !== null) clearTimeout(candidate.timeoutId);
      candidate.timeoutId = null;
    }
  }

  function rejectFontWorkerPending(error) {
    for (const pending of state.pending.values()) pending.reject(error);
    state.pending.clear();
  }

  function normalizeWorkerError(error, fallback = '字体处理组件运行失败') {
    return error instanceof Error ? error : new Error(String(error || fallback));
  }

  function handleActiveWorkerMessage(worker, message) {
    if (state.worker !== worker) return;
    if (message.type === 'progress') {
      const pending = state.pending.get(message.requestId);
      if (pending && pending.onProgress) pending.onProgress(message);
      return;
    }
    if (message.type !== 'response') return;
    const pending = state.pending.get(message.requestId);
    if (!pending) return;
    state.pending.delete(message.requestId);
    if (message.ok) pending.resolve(message);
    else pending.reject(new Error(message.error || '字体处理失败'));
  }

  function scheduleNextRuntimeCandidate(generation, immediate = false) {
    if (generation !== state.runtimeRaceGeneration || state.workerReady) return;
    if (state.runtimeCandidates.size >= MAX_HEAVY_RUNTIME_WORKERS || state.runtimeRaceQueue.length === 0) return;
    const start = () => {
      state.runtimeRaceStaggerTimer = null;
      if (generation !== state.runtimeRaceGeneration || state.workerReady) return;
      const source = state.runtimeRaceQueue.shift();
      if (source) startRuntimeCandidate(source, generation);
    };
    if (immediate) start();
    else if (state.runtimeRaceStaggerTimer === null) {
      state.runtimeRaceStaggerTimer = setTimeout(start, RUNTIME_RACE_STAGGER_MS);
    }
  }

  function failRuntimeCandidate(candidate, error) {
    if (candidate.generation !== state.runtimeRaceGeneration) return;
    const current = state.runtimeCandidates.get(candidate.source);
    if (!current || current.worker !== candidate.worker) return;
    if (candidate.timeoutId !== null) clearTimeout(candidate.timeoutId);
    try { candidate.worker.terminate(); } catch (_) { /* 失败线程可能已经被浏览器回收。 */ }
    state.runtimeCandidates.delete(candidate.source);
    state.runtimeRaceFailures.push(normalizeWorkerError(error));

    if (!state.workerReady && state.runtimeRaceQueue.length > 0) {
      scheduleNextRuntimeCandidate(candidate.generation, true);
      return;
    }
    if (!state.workerReady && state.runtimeCandidates.size === 0) {
      const lastError = state.runtimeRaceFailures[state.runtimeRaceFailures.length - 1];
      setRuntimeStatus(lastError?.message || '字体运行节点均不可用，请稍后重试', 'error');
      syncRuntimeDependentControls();
    }
  }

  function adoptRuntimeWinner(candidate) {
    if (candidate.generation !== state.runtimeRaceGeneration || state.workerReady) return;
    clearRuntimeRaceTimers();
    for (const other of state.runtimeCandidates.values()) {
      if (other.worker === candidate.worker) continue;
      try { other.worker.terminate(); } catch (_) { /* 落后线程可能已自行退出。 */ }
    }
    state.runtimeCandidates.clear();
    state.worker = candidate.worker;
    state.workerRuntimeSource = candidate.source;
    state.workerReady = true;
    candidate.worker.onmessage = (event) => handleActiveWorkerMessage(candidate.worker, event.data || {});
    candidate.worker.onerror = (event) => failFontWorker(
      candidate.worker,
      new Error(event.message || '字体处理组件运行失败'),
    );
    syncRuntimeDependentControls();
    setRuntimeStatus('字体工具已准备完成', 'ready');
  }

  function startRuntimeCandidate(runtimeSource, generation) {
    if (generation !== state.runtimeRaceGeneration || state.workerReady) return;
    if (!Object.prototype.hasOwnProperty.call(RUNTIME_SOURCE_PROFILES, runtimeSource)) return;
    const workerUrl = fontCompressAssetUrl('font-compress.worker.js');
    workerUrl.searchParams.set('v', assetVersion);
    workerUrl.searchParams.set('runtimeSource', runtimeSource);
    let worker;
    try {
      worker = new Worker(workerUrl.toString());
    } catch (error) {
      const normalized = normalizeWorkerError(error, '字体处理组件启动失败');
      state.runtimeRaceFailures.push(normalized);
      if (state.runtimeRaceQueue.length > 0) scheduleNextRuntimeCandidate(generation, true);
      else if (state.runtimeCandidates.size === 0) {
        setRuntimeStatus(normalized.message, 'error');
        syncRuntimeDependentControls();
      }
      return;
    }

    const candidate = { source: runtimeSource, worker, generation, timeoutId: null };
    state.runtimeCandidates.set(runtimeSource, candidate);
    candidate.timeoutId = setTimeout(() => {
      failRuntimeCandidate(candidate, new Error('字体运行节点响应超时'));
    }, RUNTIME_CANDIDATE_TIMEOUT_MS);
    worker.onmessage = (event) => {
      if (generation !== state.runtimeRaceGeneration) return;
      const message = event.data || {};
      if (message.type === 'runtime-progress') {
        setRuntimeStatus(message.message || '正在加载运行组件…', 'loading');
        return;
      }
      if (message.type === 'runtime-ready') {
        adoptRuntimeWinner(candidate);
        return;
      }
      if (message.type === 'response' && message.ok === false) {
        failRuntimeCandidate(candidate, new Error(message.error || '字体处理组件初始化失败'));
      }
    };
    worker.onerror = (event) => {
      failRuntimeCandidate(candidate, new Error(event.message || '字体处理组件运行失败'));
    };
    const requestId = `runtime-init-${generation}-${runtimeSource}`;
    try {
      worker.postMessage({ type: 'init', requestId });
    } catch (error) {
      failRuntimeCandidate(candidate, normalizeWorkerError(error, '字体处理组件启动失败'));
      return;
    }
    scheduleNextRuntimeCandidate(generation);
  }

  async function probeRuntimeSource(source) {
    const profile = RUNTIME_SOURCE_PROFILES[source];
    const probeUrl = source === 'local'
      ? fontCompressAssetUrl(profile.probeUrl).href
      : profile.probeUrl;
    const controller = new AbortController();
    const timer = setTimeout(() => controller.abort(), RUNTIME_PROBE_TIMEOUT_MS);
    const startedAt = performance.now();
    try {
      // 用单字节 Range GET 避免部分 CDN 不支持 HEAD，同时不下载完整 pyodide.js。
      const response = await fetch(probeUrl, {
        method: 'GET',
        headers: { Range: 'bytes=0-0' },
        cache: 'no-store',
        credentials: 'omit',
        signal: controller.signal,
      });
      if (!response.ok) throw new Error(`probe ${response.status}`);
      await response.body?.cancel();
      return { source, latency: performance.now() - startedAt, priority: profile.priority };
    } catch (_) {
      return { source, latency: Number.POSITIVE_INFINITY, priority: profile.priority };
    } finally {
      clearTimeout(timer);
    }
  }

  async function rankRuntimeSources() {
    setRuntimeStatus('正在选择最快运行节点…', 'loading');
    const results = await Promise.all(Object.keys(RUNTIME_SOURCE_PROFILES).map(probeRuntimeSource));
    const reachable = results
      .filter((result) => Number.isFinite(result.latency))
      // 轻量探测只在同类节点内排序；第三方可达时优先走 CDN，避免低延迟的同源入口
      // 总是抢占第一名、使多 CDN 加速形同虚设。自有源仍在所有 CDN 之后立即参与兜底。
      .sort((left, right) => {
        const leftGroup = left.source === 'local' ? 1 : 0;
        const rightGroup = right.source === 'local' ? 1 : 0;
        return leftGroup - rightGroup || left.latency - right.latency || left.priority - right.priority;
      })
      .map((result) => result.source);
    // 探测仅用于排序，不能作为重型资源可用性的最终判断。未响应源仍作为尾部备用，
    // 其中自有资源必须始终保留，避免第三方网络统一故障时失去受控回退。
    const unavailable = results
      .filter((result) => !Number.isFinite(result.latency))
      .sort((left, right) => left.priority - right.priority)
      .map((result) => result.source);
    return [...reachable, ...unavailable];
  }

  async function createWorker(preferredSource = null, localOnly = false) {
    const generation = ++state.runtimeRaceGeneration;
    clearRuntimeRaceTimers();
    for (const candidate of state.runtimeCandidates.values()) {
      try { candidate.worker.terminate(); } catch (_) { /* 重建期间忽略终止异常。 */ }
    }
    state.runtimeCandidates.clear();
    const previousWorker = state.worker;
    state.worker = null;
    try { previousWorker?.terminate(); } catch (_) { /* 旧线程可能已经由浏览器回收。 */ }
    rejectFontWorkerPending(new Error('字体工具正在重新初始化'));
    state.workerReady = false;
    state.runtimeRaceFailures = [];
    state.runtimeLocalOnly = localOnly;
    syncRuntimeDependentControls();
    setRuntimeStatus('正在选择最快运行节点…', 'loading');

    let rankedSources = localOnly ? ['local'] : await rankRuntimeSources();
    if (generation !== state.runtimeRaceGeneration) return;
    if (preferredSource && rankedSources.includes(preferredSource)) {
      rankedSources = [preferredSource, ...rankedSources.filter((source) => source !== preferredSource)];
    }
    state.runtimeRaceQueue = rankedSources;
    setRuntimeStatus('正在加载运行组件…', 'loading');
    scheduleNextRuntimeCandidate(generation, true);
  }

  function failFontWorker(worker, error) {
    if (state.worker !== worker) return;
    state.worker = null;
    state.workerReady = false;
    try { worker.terminate(); } catch (_) { /* 运行错误后线程可能已经被浏览器回收。 */ }
    rejectFontWorkerPending(normalizeWorkerError(error));
    syncRuntimeDependentControls();
    void createWorker('local', true);
  }

  function markerWorkerUrl() {
    const url = new URL('../font-marker-local/marker.worker.js', location.href);
    url.searchParams.set('v', assetVersion);
    return url.toString();
  }

  /**
   * 终止标记线程并拒绝全部未完成请求。
   *
   * ArrayBuffer 通过 transferable 交给线程后，主线程不再持有其内容。如果线程发生
   * 运行错误，继续复用同一实例既无法取回旧 buffer，也可能把后续请求留在未知状态，
   * 所以必须整体关闭，下一次操作再创建干净实例。
   */
  function failMarkerWorker(error) {
    const normalized = error instanceof Error ? error : new Error(String(error || '字体内部数据处理失败'));
    try { state.markerWorker?.terminate(); } catch (_) { /* 线程可能已经被浏览器回收。 */ }
    state.markerWorker = null;
    for (const pending of state.markerPending.values()) pending.reject(normalized);
    state.markerPending.clear();
  }

  function ensureMarkerWorker() {
    if (state.markerWorker) return state.markerWorker;
    const worker = new Worker(markerWorkerUrl());
    worker.onmessage = (event) => {
      const response = event.data || {};
      const pending = state.markerPending.get(response.id);
      if (!pending) return;
      state.markerPending.delete(response.id);
      if (response.ok) pending.resolve(response);
      else pending.reject(new Error(response.error || '字体内部数据处理失败'));
    };
    worker.onerror = (event) => {
      event.preventDefault?.();
      failMarkerWorker(new Error(event.message || '字体内部数据处理组件运行失败'));
    };
    state.markerWorker = worker;
    return worker;
  }

  function callMarkerWorker(operation, payload, buffer) {
    const id = `font-marker-${Date.now().toString(36)}-${++state.markerRequestSequence}`;
    return new Promise((resolve, reject) => {
      state.markerPending.set(id, { resolve, reject });
      try {
        ensureMarkerWorker().postMessage({
          id,
          operation,
          keyText: FONT_MARKER_STEGO_KEY,
          ...payload,
          bytes: buffer,
        }, [buffer]);
      } catch (error) {
        // 当前请求尚未进入线程时先从 pending 移除，再让 failMarkerWorker 拒绝其它请求，
        // 避免同一个 Promise 被重复 reject，也保证下次会创建新线程。
        state.markerPending.delete(id);
        failMarkerWorker(error);
        reject(error instanceof Error ? error : new Error(String(error)));
      }
    });
  }

  /**
   * 在任何字体库重新保存之前检查原始字节，并要求线程把输入 buffer 原样交还。
   * 这样同一份大字体可以继续传给字体分析线程，不需要在移动端内存中同时复制两份。
   */
  async function inspectFontMarker(buffer, filename) {
    const response = await callMarkerWorker('inspect', { filename }, buffer);
    if (
      response.operation !== 'inspect'
      || !(response.input instanceof ArrayBuffer)
      || !response.inspection
      || typeof response.inspection.state !== 'string'
    ) {
      throw new Error('字体内部数据检查结果无效');
    }
    return { input: response.input, inspection: response.inspection };
  }

  /**
   * 字体子集化完成后，在最终成品上重新写入原 metadata。
   * 成功条件与字体调整一致：保存检查通过、至少一个维度能恢复完整 metadata，且最终
   * 成品中的冗余记录全部一致。任何一项失败都不允许进入下载或回传步骤。
   */
  async function restoreFontMarker(buffer, filename, metadata) {
    const response = await callMarkerWorker('mark', { filename, metadata }, buffer);
    const summary = response.summary || {};
    const verification = response.verification || {};
    if (
      response.operation !== 'mark'
      || !(response.output instanceof ArrayBuffer)
      || summary.save_verified !== true
      || Number(summary.recoverable_dimensions_succeeded || 0) <= 0
      || verification.success !== true
      || !/^[0-9a-f]{64}$/.test(String(response.outputSha256 || ''))
    ) {
      throw new Error('字体内部数据恢复后的成品检查失败');
    }
    return { output: response.output, verification, outputSha256: response.outputSha256 };
  }

  function callWorker(type, payload, transfer = [], onProgress = null) {
    const requestId = `${Date.now().toString(36)}-${++state.requestSequence}`;
    return new Promise((resolve, reject) => {
      if (!state.worker || (type !== 'init' && !state.workerReady)) {
        reject(new Error('字体工具尚未初始化完成'));
        return;
      }
      state.pending.set(requestId, { resolve, reject, onProgress });
      try {
        state.worker.postMessage({ type, requestId, ...payload }, transfer);
      } catch (error) {
        // DataCloneError、已终止线程等同步异常不会产生 Worker response。必须立即删除
        // pending，否则后续重建 Worker 时会再次拒绝同一个请求并长期保留无效闭包。
        state.pending.delete(requestId);
        reject(error instanceof Error ? error : new Error(String(error || '字体处理请求发送失败')));
      }
    });
  }

  // ---- 字集预设渲染与懒加载 --------------------------------------------

  function renderCharsetGrid() {
    const grid = elements['charset-grid'];
    grid.innerHTML = CHARSET_PRESETS.map((preset) => `
      <label class="charset-option" for="charset-${escapeHtml(preset.id)}">
        <input type="checkbox" id="charset-${escapeHtml(preset.id)}" data-preset-id="${escapeHtml(preset.id)}">
        <span>
          <strong>${escapeHtml(preset.label)}</strong>
          <small>${escapeHtml(preset.desc)}</small>
        </span>
      </label>
    `).join('');
    state.presetInputs = new Map();
    for (const input of grid.querySelectorAll('input')) {
      state.presetInputs.set(input.dataset.presetId, input);
    }
  }

  /**
   * 懒加载字集：点击/勾选某预设时才 fetch 对应文件并解析为 Set<number>，缓存进 Map，
   * 不预先下载全部字集，避免移动端一次拉起几十 MB 文本。
   */
  async function loadCharsetPreset(id) {
    if (state.presetCache.has(id)) return state.presetCache.get(id);
    if (state.presetLoading.has(id)) return state.presetLoading.get(id);
    const preset = CHARSET_PRESETS.find((item) => item.id === id);
    if (!preset) return null;
    const url = fontCompressAssetUrl(preset.file).toString();
    const promise = fetch(url, { cache: 'no-cache' })
      .then((response) => {
        if (!response.ok) throw new Error(`字集加载失败（${response.status}）`);
        return response.text();
      })
      .then((text) => {
        const codes = new Set();
        for (const line of text.split(/\r?\n/)) {
          const match = line.trim().match(/^U\+([0-9A-Fa-f]{4,6})$/);
          if (!match) continue;
          const value = parseInt(match[1], 16);
          if (value >= 0 && value <= 0x10FFFF) codes.add(value);
        }
        state.presetCache.set(id, codes);
        return codes;
      });
    state.presetLoading.set(id, promise);
    try {
      return await promise;
    } finally {
      state.presetLoading.delete(id);
    }
  }

  /** 所有已勾选且已加载的预设码点与自定义码点的并集。 */
  function selectedUnion() {
    const union = new Set();
    for (const id of state.selectedPresets) {
      const codes = state.presetCache.get(id);
      if (codes) for (const code of codes) union.add(code);
    }
    for (const code of state.customCodepoints) union.add(code);
    return union;
  }

  /**
   * 从自定义文本提取唯一码点：用 for...of 逐字迭代能正确按码点切分代理对；
   * 控制符（C0 与 DEL）不参与压缩，直接忽略。
   */
  function extractCustomCodepoints(text) {
    const codes = new Set();
    for (const character of String(text || '')) {
      const code = character.codePointAt(0);
      if (code === null || code === undefined) continue;
      if (code >= 0x00 && code <= 0x1f) continue;
      if (code === 0x7f) continue;
      codes.add(code);
    }
    return codes;
  }

  function updateHitSummary() {
    const summary = elements['hit-summary'];
    if (!state.analysis || !state.file) {
      summary.textContent = '选择字体并勾选字集后，将显示命中预估。';
      return;
    }
    const union = selectedUnion();
    if (union.size === 0) {
      summary.textContent = '请先勾选字集或输入自定义文字，才能预估命中字符。';
      return;
    }
    const loadingSelected = CHARSET_PRESETS.some(
      (preset) => state.selectedPresets.has(preset.id) && state.presetLoading.has(preset.id),
    );
    if (loadingSelected) {
      summary.textContent = '字集加载中…';
      return;
    }
    const capabilities = state.analysis.capabilities || {};
    if (capabilities.cmapReliable === false) {
      summary.textContent = '当前裸 CFF1 没有可靠 cmap，将保留全部 glyph，不按码点删减';
      return;
    }
    const analysisCodes = new Set(state.analysis.codepoints || []);
    let hit = 0;
    for (const code of union) if (analysisCodes.has(code)) hit += 1;
    summary.textContent = `命中约 ${hit} 个字符 / 原字体 ${state.analysis.numCodepoints} 个码点`;
  }

  function syncCharsetState() {
    const union = selectedUnion();
    const required = elements['charset-required'];
    if (union.size > 0) {
      required.textContent = `已选 ${union.size} 个码点`;
      required.classList.add('ready');
    } else {
      required.textContent = '请至少勾选一个字集，或填写自定义文字后才能开始压缩。';
      required.classList.remove('ready');
    }
    const names = CHARSET_PRESETS
      .filter((preset) => state.selectedPresets.has(preset.id))
      .map((preset) => preset.label);
    if (state.customCodepoints.size > 0) names.push(`自定义 ${state.customCodepoints.size} 字`);
    elements['charset-output'].textContent = names.length > 0 ? names.join('、') : '未选择';
    elements['custom-output'].textContent = `${state.customCodepoints.size} 字`;
    updateHitSummary();
    syncProcessEnabled();
  }

  function updateSvgOptionCopy() {
    const small = elements['opt-svg-downsample'].closest('.option-row')?.querySelector('small');
    if (!small) return;
    const capabilities = state.analysis?.capabilities;
    if (capabilities?.svgImageDownsample && Number(capabilities.svgImageCount) > 0) {
      small.textContent = `检测到 ${capabilities.svgImageCount} 张彩色位图`;
    } else {
      small.textContent = '仅当字体含 SVG 位图时生效';
    }
  }

  function syncOptionOutput() {
    const output = elements['option-output'];
    const hint = elements['option-hint'];
    if (!state.analysis || !state.file) {
      output.textContent = '默认推荐';
      hint.textContent = '请先选择字体。未勾选字集时无法开始压缩。';
      return;
    }
    const parts = [];
    if (elements['opt-no-hinting'].checked) parts.push('去 hinting');
    if (elements['opt-desubroutinize'].checked) parts.push('CFF 去子程序');
    if (elements['opt-layout-closure'].checked) parts.push('布局闭包');
    if (elements['opt-retain-gids'].checked) parts.push('保留 GID');
    if (elements['opt-svg-downsample'].checked && state.analysis.capabilities.svgImageDownsample) {
      parts.push(`SVG 降分辨率 ${elements['svg-edge'].value}px`);
    }
    output.textContent = parts.length > 0 ? parts.join(' · ') : '默认推荐';
    hint.textContent = selectedUnion().size > 0
      ? '可以开始压缩。'
      : '已选择字体，请勾选字集或输入自定义文字后开始压缩。';
  }

  function renderAnalysis(result) {
    state.analysis = result;
    const tableSizes = Object.entries(result.tableSizes || {})
      .filter(([tag]) => tag !== 'total')
      .sort((left, right) => right[1] - left[1])
      .slice(0, 3)
      .map(([tag, size]) => `${tag} ${formatBytes(size)}`);
    const meta = [
      ['文件名', state.file?.name || result.filename || ''],
      ['格式', result.container === 'BARE-CFF1' ? '裸 CFF1（输出 OTF）' : String(result.container || '')],
      ['原大小', formatBytes(result.inputSize)],
      ['字形数量', String(result.numGlyphs)],
      ['码点数量', String(result.numCodepoints)],
      ['主要表体积', tableSizes.join(' · ') || '—'],
    ];
    elements['font-meta'].innerHTML = meta.map(([label, value]) => (
      `<div class="meta-item"><span>${escapeHtml(label)}</span><strong>${escapeHtml(value)}</strong></div>`
    )).join('');
    elements['font-meta'].classList.remove('hidden');
    showWarnings(elements.warnings, result.warnings || []);
    updateSvgOptionCopy();
    elements['controls-card'].classList.remove('hidden');
    elements['action-card'].classList.remove('hidden');
    setProgress('可以开始压缩', 0);
    syncCharsetState();
    syncOptionOutput();
    syncRuntimeDependentControls();
  }

  // ---- 选文件流程 ------------------------------------------------------

  function clearSelectedFont() {
    state.file = null;
    state.analysis = null;
    state.sourceMarkerMetadata = null;
    state.svgDocuments = null;
    state.svgDownsampledCount = 0;
    state.svgWarnings = [];
    revokeOutput();
    elements['controls-card'].classList.add('hidden');
    elements['action-card'].classList.add('hidden');
    elements['font-meta'].classList.add('hidden');
    showWarnings(elements.warnings, []);
    syncCharsetState();
    syncOptionOutput();
    syncRuntimeDependentControls();
  }

  async function selectFont(file) {
    if (!state.launchAuthorized || !state.workerReady) return;
    if (!file) return;
    // File 对象已由事件参数持有，立即清空 input 值，允许解析失败后再次选择同一路径
    // 重试；浏览器默认不会为“连续选择同一个文件”再次触发 change。
    elements['font-file'].value = '';
    // 任何一次新的选择都代表旧分析已失效。版本号必须在前置校验之前递增，否则旧大字体的
    // 异步分析仍可能在错误提示之后重新出现。
    const selectionVersion = ++state.selectionVersion;
    if (state.markerPending.size > 0) {
      // 标记扫描是单线程顺序任务。用户重新选择后若继续等待旧大字体扫描，新字体请求会排
      // 在其后面，既浪费内存也造成明显卡顿；直接关闭旧实例可立即开始新检查。
      failMarkerWorker(new Error('字体选择已更新'));
    }
    const extension = (file.name.match(/\.[^.]+$/) || [''])[0].toLowerCase();
    if (!['.ttf', '.otf', '.cff'].includes(extension)) {
      clearSelectedFont();
      toast('请选择 TTF、OTF 或裸 CFF1 字体');
      return;
    }
    if (file.size <= 0) {
      clearSelectedFont();
      toast('字体文件内容为空');
      return;
    }
    state.file = file;
    state.analysis = null;
    // 新选择一开始就清空旧 metadata。即使新文件随后扩展名错误、检查失败或用户再次切换
    // 文件，也不能让上一个字体的归属信息留到新的处理任务中。
    state.sourceMarkerMetadata = null;
    state.svgDocuments = null;
    state.svgDownsampledCount = 0;
    state.svgWarnings = [];
    revokeOutput();
    elements['picker-title'].textContent = file.name;
    elements['picker-subtitle'].textContent = `${formatBytes(file.size)} · 正在读取字体`;
    elements['controls-card'].classList.add('hidden');
    elements['action-card'].classList.add('hidden');
    elements['font-meta'].classList.add('hidden');
    showWarnings(elements.warnings, []);
    syncRuntimeDependentControls();
    try {
      const selectedBytes = await file.arrayBuffer();
      if (selectionVersion !== state.selectionVersion) return;

      let inspected;
      try {
        inspected = await inspectFontMarker(selectedBytes, file.name);
      } catch (error) {
        // 重新选择字体会主动终止上一份文件的扫描。旧任务只需静默退出，不能把正常的
        // 取消行为记录成错误，也不能覆盖新文件正在显示的状态。
        if (selectionVersion !== state.selectionVersion) return;
        console.error('[font-compress] 字体内部数据检查失败:', error);
        throw new Error('字体内部数据校验失败，请重新选择原始字体文件');
      }
      if (selectionVersion !== state.selectionVersion) return;

      // 新 Worker 用 confirmed/recovered/partial/stripped 提供更精确诊断；页面仍按旧四态
      // 执行安全分支。回退到 state 仅用于兼容发布切换期间仍在缓存中的旧 Worker。
      const markerState = typeof inspected.inspection.legacyState === 'string'
        ? inspected.inspection.legacyState
        : inspected.inspection.state;
      if (markerState === 'conflict' || markerState === 'unrecoverable') {
        throw new Error('字体内部数据不完整，无法安全处理，请重新选择原始字体文件');
      }
      if (!['marked', 'unmarked'].includes(markerState)) {
        throw new Error('字体内部数据检查结果无效');
      }
      const markerMetadata = markerState === 'marked' ? inspected.inspection.metadata : null;
      if (markerState === 'marked' && (!markerMetadata || typeof markerMetadata !== 'object')) {
        throw new Error('字体内部数据不完整，无法安全处理，请重新选择原始字体文件');
      }

      // 检查线程已经把同一个 ArrayBuffer 交还；继续 transfer 给字体分析线程，避免为
      // 中文大字体再分配一份等大的常驻副本。
      const response = await callWorker(
        'analyze',
        { filename: file.name, bytes: inspected.input },
        [inspected.input],
        (progress) => {
          if (selectionVersion === state.selectionVersion) {
            setRuntimeStatus(progress.message || '正在分析字体…', 'loading');
            elements['picker-subtitle'].textContent = `${formatBytes(file.size)} · 正在解析字体`;
          }
        },
      );
      // 用户可能在大字体分析期间重新选择文件。旧 Worker 请求无法撤回，但其结果绝不能
      // 覆盖新文件的状态与压缩参数。
      if (selectionVersion !== state.selectionVersion) return;
      state.sourceMarkerMetadata = markerMetadata;
      renderAnalysis(response.result);
      elements['picker-subtitle'].textContent = response.result.container === 'BARE-CFF1'
        ? `${formatBytes(file.size)} · 裸 CFF1（将输出 OTF）`
        : `${formatBytes(file.size)} · ${response.result.container}`;
      setRuntimeStatus('字体读取完成', 'ready');
    } catch (error) {
      if (selectionVersion !== state.selectionVersion) return;
      state.file = null;
      state.sourceMarkerMetadata = null;
      syncRuntimeDependentControls();
      setRuntimeStatus(error.message || '字体分析失败', 'error');
      toast(error.message || '字体分析失败');
    }
  }

  // ---- SVG 内嵌位图降采样（Canvas） ------------------------------------

  const SVG_IMAGE_DOWNSAMPLE_CONCURRENCY = 2;

  async function decodeDataUriToBitmap(mime, base64) {
    const binary = atob(base64);
    const bytes = new Uint8Array(binary.length);
    for (let i = 0; i < binary.length; i += 1) bytes[i] = binary.charCodeAt(i);
    const blob = new Blob([bytes], { type: `image/${mime}` });
    if (typeof createImageBitmap === 'function') {
      return createImageBitmap(blob);
    }
    // 老浏览器回退：object URL + Image 解码，结束后由调用方负责 revoke。
    return new Promise((resolve, reject) => {
      const url = URL.createObjectURL(blob);
      const image = new Image();
      image.onload = () => { URL.revokeObjectURL(url); resolve(image); };
      image.onerror = () => { URL.revokeObjectURL(url); reject(new Error('内嵌位图解码失败')); };
      image.src = url;
    });
  }

  /**
   * 把单个 SVG 文档中所有 data URI 位图等比缩放到目标边长（保持纵横比，不做放大），
   * 用 LANCZOS 语义的 canvas 高质采样导出 PNG（保留 alpha）后替换原文。
   * 并发上限避免移动端峰值内存；单张失败只保留原图并计数，不让整个流程失败。
   */
  async function downsampleDocumentXml(xml, targetEdge) {
    const matches = [];
    for (const match of xml.matchAll(/data:image\/(png|jpeg|webp);base64,([A-Za-z0-9+/=]+)/g)) {
      matches.push({
        start: match.index,
        end: match.index + match[0].length,
        original: match[0],
        mime: match[1],
        base64: match[2],
      });
    }
    if (matches.length === 0) return { data: xml, downsampled: 0, failed: 0 };

    const results = new Array(matches.length);
    let nextIndex = 0;
    const run = async () => {
      while (nextIndex < matches.length) {
        const index = nextIndex;
        nextIndex += 1;
        const item = matches[index];
        try {
          const source = await decodeDataUriToBitmap(item.mime, item.base64);
          let outcome;
          try {
            const sourceWidth = source.width || 0;
            const sourceHeight = source.height || 0;
            const maxEdge = Math.max(sourceWidth, sourceHeight);
            if (maxEdge <= 0) throw new Error('内嵌位图尺寸无效');
            const target = Number(targetEdge) || 640;
            if (maxEdge <= target) {
              // 压缩场景绝不放大已有位图，否则体积反而增加。
              outcome = { kind: 'skipped', replacement: item.original };
            } else {
              const scale = target / maxEdge;
              const canvasWidth = Math.max(1, Math.round(sourceWidth * scale));
              const canvasHeight = Math.max(1, Math.round(sourceHeight * scale));
              const canvas = document.createElement('canvas');
              canvas.width = canvasWidth;
              canvas.height = canvasHeight;
              const context = canvas.getContext('2d', { alpha: true });
              if (!context) throw new Error('Canvas 渲染不可用');
              context.imageSmoothingEnabled = true;
              context.imageSmoothingQuality = 'high';
              context.clearRect(0, 0, canvasWidth, canvasHeight);
              context.drawImage(source, 0, 0, canvasWidth, canvasHeight);
              outcome = { kind: 'downsampled', replacement: canvas.toDataURL('image/png') };
            }
          } finally {
            if (source && typeof source.close === 'function') source.close();
          }
          results[index] = outcome;
        } catch (error) {
          results[index] = { kind: 'failed', error };
        }
      }
    };
    const workers = [];
    for (let i = 0; i < Math.min(SVG_IMAGE_DOWNSAMPLE_CONCURRENCY, matches.length); i += 1) {
      workers.push(run());
    }
    await Promise.all(workers);

    let output = '';
    let cursor = 0;
    let downsampled = 0;
    let failed = 0;
    for (let i = 0; i < matches.length; i += 1) {
      const item = matches[i];
      output += xml.slice(cursor, item.start);
      const result = results[i];
      if (result.kind === 'downsampled') {
        output += result.replacement;
        downsampled += 1;
      } else {
        output += item.original;
        if (result.kind === 'failed') failed += 1;
      }
      cursor = item.end;
    }
    output += xml.slice(cursor);
    return { data: output, downsampled, failed };
  }

  /**
   * 仅当用户勾选降分辨率且分析显示字体含 SVG 位图时执行：先提取全部 SVG 文档，
   * 逐文档降采样后组装成 worker 期望的 svgDocuments 结构。提取阶段不 transfer 字节，
   * 主线程仍需保留 bytes 供随后的 process 请求使用。
   */
  async function downsampleSvgDocuments(bytes) {
    setProgress('正在提取彩色位图…', 15);
    const extraction = await callWorker('extract-svg', { filename: state.file.name, bytes });
    const documents = extraction.result && Array.isArray(extraction.result.documents)
      ? extraction.result.documents
      : null;
    if (!documents) throw new Error('SVG 位图提取结果无效');
    const targetEdge = Number(elements['svg-edge'].value) || 640;
    const warnings = [];
    let totalDownsampled = 0;
    let totalFailed = 0;
    const svgDocuments = [];
    for (const doc of documents) {
      const startGlyphID = Number(doc.startGlyphID);
      const endGlyphID = Number(doc.endGlyphID);
      let data = String(doc.data || '');
      try {
        const outcome = await downsampleDocumentXml(data, targetEdge);
        data = outcome.data;
        totalDownsampled += outcome.downsampled;
        totalFailed += outcome.failed;
        if (outcome.failed > 0) {
          warnings.push(`第 ${startGlyphID}-${endGlyphID} 号字形区间有 ${outcome.failed} 张位图降采样失败，已保留原图`);
        }
      } catch (error) {
        warnings.push(`第 ${startGlyphID}-${endGlyphID} 号字形区间降采样失败，已保留原图`);
      }
      svgDocuments.push({ startGlyphID, endGlyphID, data });
    }
    state.svgDocuments = svgDocuments;
    state.svgDownsampledCount = totalDownsampled;
    state.svgWarnings = warnings;
    return { totalDownsampled, totalFailed };
  }

  // ---- 处理与结果 ------------------------------------------------------

  function revokeOutput() {
    state.outputBuffer = null;
    state.outputBlob = null;
    state.outputName = '';
    state.outputReport = null;
    elements['result-card'].classList.add('hidden');
  }

  function createDefaultOutputName() {
    const extension = state.analysis?.recommendedExtension === '.otf' ? '.otf' : '.ttf';
    const sourceName = state.file?.name || `font${extension}`;
    const base = sanitizeOutputFileBase(sourceName) || 'font';
    const suffix = `-压缩${extension}`;
    const maximumBaseLength = MAX_OUTPUT_FILE_NAME_LENGTH - Array.from(suffix).length;
    const trimmedBase = Array.from(base).slice(0, maximumBaseLength).join('').replace(/[. ]+$/g, '') || 'font';
    return `${trimmedBase}${suffix}`;
  }

  const MAX_OUTPUT_FILE_NAME_LENGTH = 120;
  const WINDOWS_RESERVED_FILE_NAMES = /^(con|prn|aux|nul|com[1-9]|lpt[1-9])$/i;

  /** 与字体调整保持一致的文件名清洗规则，避免非法字符与保留名导致下载失败。 */
  function sanitizeOutputFileBase(input) {
    const cleaned = String(input || '')
      .normalize('NFC')
      .replace(/[<>:"/\\|?*\u0000-\u001f\u007f]/g, '_')
      .replace(/\s+/g, ' ')
      .trim()
      .replace(/[. ]+$/g, '')
      .replace(/\.[^.]+$/u, '')
      .replace(/[. ]+$/g, '');
    if (!cleaned) return '';
    return WINDOWS_RESERVED_FILE_NAMES.test(cleaned) ? `_${cleaned}` : cleaned;
  }

  function setBusyDisabled(element, busy) {
    if (!(element instanceof HTMLInputElement
      || element instanceof HTMLSelectElement
      || element instanceof HTMLButtonElement
      || element instanceof HTMLTextAreaElement)) {
      return;
    }
    if (busy) {
      if (!Object.prototype.hasOwnProperty.call(element.dataset, 'disabledBeforeBusy')) {
        element.dataset.disabledBeforeBusy = element.disabled ? '1' : '0';
      }
      element.disabled = true;
      return;
    }
    if (Object.prototype.hasOwnProperty.call(element.dataset, 'disabledBeforeBusy')) {
      element.disabled = element.dataset.disabledBeforeBusy === '1';
      delete element.dataset.disabledBeforeBusy;
    }
  }

  function setBusy(busy, allowCancel = true) {
    state.busy = busy;
    syncRuntimeDependentControls();
    // 参数在 File.arrayBuffer() 与 Worker 处理期间都必须保持冻结。否则用户在读取大字体
    // 时继续切换字集或选项，页面会显示新值，但本次成品可能已经使用旧参数。
    const parameterControls = [
      elements['opt-no-hinting'], elements['opt-desubroutinize'], elements['opt-layout-closure'],
      elements['opt-retain-gids'], elements['opt-svg-downsample'], elements['svg-edge'],
      elements['custom-text'],
    ];
    for (const control of parameterControls) setBusyDisabled(control, busy);
    for (const input of elements['charset-grid'].querySelectorAll('input')) setBusyDisabled(input, busy);
    if (!busy) syncRuntimeDependentControls();
    // “取消”只用于终止 Worker 字体处理。文件已经开始 postMessage 后无法撤回微信缓存的
    // 消息，若此时仍允许取消，会出现旧结果继续回传、新任务又被启动的并发状态。
    elements['cancel-button'].classList.toggle('hidden', !busy || !allowCancel);
  }

  function renderResult() {
    const report = state.outputReport;
    const applied = report.applied || {};
    const verification = report.verification || {};
    const inputSize = Number(verification.inputSize || 0);
    const outputSize = Number(verification.outputSize || 0);
    const ratio = inputSize > 0
      ? Math.max(0, Math.min(100, Math.round((1 - outputSize / inputSize) * 100)))
      : 0;
    elements['result-title'].textContent = state.outputName;
    const preservationSummary = applied.cmapReliable === false
      ? `保留全部 ${applied.glyphsAfter ?? '—'} 个字形（无可靠 cmap，未按码点删减）`
      : `保留 ${applied.glyphsAfter ?? '—'} 个字形 / ${applied.codepointsAfter ?? '—'} 个码点`;
    elements['result-summary'].textContent = (
      `${formatBytes(outputSize)} · 体积减小约 ${ratio}% · ${preservationSummary}`
    );
    const warnings = [...(report.warnings || [])];
    if (state.svgDownsampledCount > 0) {
      warnings.push(`已降分辨率 ${state.svgDownsampledCount} 张内嵌位图（目标边长 ${elements['svg-edge'].value}px）`);
    }
    for (const warning of state.svgWarnings) warnings.push(warning);
    showWarnings(elements['result-warnings'], warnings);
    elements['result-card'].classList.remove('hidden');
    elements['download-button'].classList.toggle('hidden', inMiniProgram);
    elements['return-button'].classList.toggle('hidden', !inMiniProgram);
    if (inMiniProgram) {
      elements['return-button'].disabled = false;
      elements['delivery-hint'].textContent = '点击按钮返回小程序，随后可以分享字体文件。';
    } else {
      elements['delivery-hint'].textContent = '点击按钮下载压缩后的字体文件。';
    }
  }

  async function processFont() {
    if (!state.launchAuthorized) return;
    if (!state.workerReady || !state.file || !state.analysis || state.busy) return;
    const union = selectedUnion();
    if (union.size === 0) {
      toast('请至少勾选一个字集，或填写自定义文字');
      return;
    }
    const processingVersion = ++state.processingVersion;
    revokeOutput();
    setBusy(true);
    setProgress('正在读取字体…', 5);
    try {
      const outputName = createDefaultOutputName();
      const bytes = await state.file.arrayBuffer();
      // “取消”可能发生在大文件仍由浏览器读取、尚未产生 Worker pending 请求时。
      // 仅清空 pending 无法覆盖这段窗口，因此每个 await 后都要核对任务版本。
      if (processingVersion !== state.processingVersion) return;

      const options = {
        unicodes: Array.from(union).sort((a, b) => a - b),
        noHinting: elements['opt-no-hinting'].checked,
        desubroutinize: elements['opt-desubroutinize'].checked,
        layoutClosure: elements['opt-layout-closure'].checked,
        retainGids: elements['opt-retain-gids'].checked,
        svgDocuments: null,
      };

      // 仅当用户勾选且分析确认字体含 SVG 位图时才做降采样。extract-svg 不 transfer
      // bytes，主线程仍持有原始字节供随后的 process 请求使用。
      if (elements['opt-svg-downsample'].checked && state.analysis.capabilities.svgImageDownsample) {
        await downsampleSvgDocuments(bytes);
        if (processingVersion !== state.processingVersion) return;
        options.svgDocuments = state.svgDocuments;
      }

      setProgress('正在压缩字体…', 40);
      const response = await callWorker(
        'process',
        { filename: state.file.name, bytes, options },
        [bytes],
        (progress) => {
          if (processingVersion !== state.processingVersion) return;
          const percent = progress.stage === 'verify' ? 82 : 45;
          setProgress(progress.message || '正在压缩字体…', percent);
        },
      );
      if (processingVersion !== state.processingVersion) return;

      let output = response.output;
      let report = response.result;
      let finalOutputSha256 = report.verification && report.verification.outputSha256;
      let finalTableCount = report.verification && report.verification.tableCount;

      // 若字体没有发生任何变化（如所选码点集合与原始字体一致），Python 核心会逐字节
      // 复制原文件，此时原标记天然保持不变，不应为了“恢复”而重新编译字体。已打标字体
      // 在内容确实变化时重新应用原 metadata；未标记字体则完全跳过，不会被自动加标。
      if (state.sourceMarkerMetadata && report.changed !== false) {
        setProgress('正在生成字体文件…', 88);
        try {
          const restored = await restoreFontMarker(output, outputName, state.sourceMarkerMetadata);
          output = restored.output;
          finalOutputSha256 = restored.outputSha256;
          finalTableCount = restored.verification.table_count;
          report = { ...report, markerVerification: restored.verification };
        } catch (error) {
          // 用户点击取消时，取消函数已经完成界面复位并提升任务版本；旧任务应静默结束，
          // 不能再弹出一次“完整性失败”覆盖“处理已取消”的结果。
          if (processingVersion !== state.processingVersion) return;
          console.error('[font-compress] 字体成品内部数据恢复失败:', error);
          throw new Error('字体生成后的完整性校验失败，请重试');
        }
        if (processingVersion !== state.processingVersion) return;
      }

      // 标记恢复会改变最终文件的 name 表、checksum 和文件长度。Python 报告里的摘要属于
      // 恢复前的中间成品；恢复线程已在 transfer 前对最终字节计算 SHA-256，这里使用该值
      // 覆盖中间摘要，确保页面展示与实际下载、分享的文件一致。
      setProgress('正在生成字体文件…', 94);
      if (!/^[0-9a-f]{64}$/.test(String(finalOutputSha256 || ''))) {
        throw new Error('字体文件摘要校验失败，请重试');
      }
      if (!Number.isInteger(Number(finalTableCount)) || Number(finalTableCount) <= 0) {
        throw new Error('字体文件结构校验失败，请重试');
      }
      report = {
        ...report,
        verification: {
          ...(report.verification || {}),
          outputSha256: finalOutputSha256,
          outputSize: output.byteLength,
          tableCount: Number(finalTableCount),
        },
      };
      state.outputBuffer = output;
      state.outputName = outputName;
      state.outputReport = report;
      state.outputBlob = new Blob([output], { type: state.analysis.outputContainer === 'TTF' ? 'font/ttf' : 'font/otf' });
      setProgress('字体压缩完成', 100);
      renderResult();
      elements['result-card'].scrollIntoView({ behavior: 'smooth', block: 'start' });
    } catch (error) {
      if (processingVersion !== state.processingVersion) return;
      setProgress(error.message || '处理失败', 0);
      toast(error.message || '字体压缩失败');
    } finally {
      // 旧任务的 finally 可能晚于用户启动的新任务；不能把新任务的按钮提前解锁。
      if (processingVersion === state.processingVersion) setBusy(false);
    }
  }

  function downloadOutput() {
    if (!state.outputBlob) return;
    const url = URL.createObjectURL(state.outputBlob);
    const anchor = document.createElement('a');
    anchor.href = url;
    anchor.download = state.outputName;
    anchor.rel = 'noopener';
    document.body.appendChild(anchor);
    anchor.click();
    anchor.remove();
    setTimeout(() => URL.revokeObjectURL(url), 1000);
  }

// __MP_BRIDGE_SEGMENT_BEGIN__（构建期注入共享桥实现；本区段由 scripts/build.mjs 管理，勿手改）
(()=>{var c=null,m=new WeakMap;function P(e){let i=window,t=i.wx?.miniProgram,r=!!(t&&typeof t.postMessage=="function"&&typeof t.navigateBack=="function"),o=i.WeixinJSBridge;if(o&&typeof o.invoke=="function")return{postMessage({data:n}){try{o.invoke("invokeMiniProgramAPI",{name:"postMessage",arg:n},()=>{})}catch(g){if(r&&t){t.postMessage({data:n});return}throw g}},navigateBack(){try{o.invoke("invokeMiniProgramAPI",{name:"navigateBack",arg:{delta:1}},()=>{})}catch(n){if(r&&t){t.navigateBack();return}throw n}}};if(r&&t)return t;throw new Error(`\u5F53\u524D\u9875\u9762\u65E0\u6CD5\u8FD4\u56DE\u5C0F\u7A0B\u5E8F\uFF0C\u8BF7\u91CD\u65B0\u8FDB\u5165${e}`)}function S(e){let i="";for(let r=0;r<e.byteLength;r+=32768)i+=String.fromCharCode(...e.subarray(r,Math.min(r+32768,e.byteLength)));return btoa(i)}function y(e){if(!e||e!==e.split(/[\\/]/u).pop()||/[\u0000-\u001f\u007f]/u.test(e))throw new Error("\u7ED3\u679C\u6587\u4EF6\u540D\u5FC5\u987B\u662F\u5B89\u5168 basename")}function w(e){return c||(c=(async()=>{let i=m.get(e.bytes);if(i){i.navigateBack();return}let t=e.maxFileSize??67108864;if(!Number.isSafeInteger(t)||t<=0)throw new Error("strict maxFileSize \u5FC5\u987B\u662F\u975E\u96F6\u6B63\u5B89\u5168\u6574\u6570");if(e.bytes.byteLength<=0||e.bytes.byteLength>t)throw new Error("\u7ED3\u679C\u6587\u4EF6\u8D85\u8FC7 strict bridge \u4E0A\u9650");y(e.fileName);let r=P(e.toolLabel),o=e.idPrefix||"tool",n=new URLSearchParams(window.location.search),g=n.get("requestId")||n.get("transferId")||`${o}_request_${Date.now()}`,b=n.get("transferId")||`${o}_${Date.now()}_${Math.random().toString(36).slice(2,10)}`,d=`${o}_file_${Date.now()}_${Math.random().toString(36).slice(2,10)}`,s=Math.max(1,Math.ceil(e.bytes.byteLength/1048576)),p=0,l=a=>{r.postMessage({data:{...a,protocolVersion:2,requestId:g,transferId:b,sequence:p++,sentAt:Date.now()}})};l({type:"filesStart",total:1,totalFiles:1}),l({type:"chunkStart",fileId:d,fileName:e.fileName,fileSize:e.bytes.byteLength,fileType:e.fileType,totalChunks:s,fileIndex:0,totalFiles:1});for(let a=0;a<s;a+=1){let f=a*1048576,u=Math.min(f+1048576,e.bytes.byteLength);l({type:"chunkData",fileId:d,chunkIndex:a,chunkBytes:u-f,totalChunks:s,fileIndex:0,totalFiles:1,data:S(e.bytes.subarray(f,u))}),e.onProgress?.(a+1,s),await new Promise(v=>setTimeout(v,0))}l({type:"chunkEnd",fileId:d,fileName:e.fileName,fileSize:e.bytes.byteLength,totalChunks:s,fileIndex:0,totalFiles:1}),l({type:"filesEnd",total:1,totalFiles:1,successCount:1,failedCount:0,failedFiles:[]}),await new Promise(a=>setTimeout(a,200)),m.set(e.bytes,r),r.navigateBack()})(),c.finally(()=>{c=null}))}function h(e,i,t){y(i);let r=e.byteOffset===0&&e.byteLength===e.buffer.byteLength?e.buffer:e.buffer.slice(e.byteOffset,e.byteOffset+e.byteLength),o=URL.createObjectURL(new Blob([r],{type:t})),n=document.createElement("a");n.href=o,n.download=i,n.rel="noopener",document.body.appendChild(n),n.click(),n.remove(),setTimeout(()=>URL.revokeObjectURL(o),1e3)}window.__MP_BRIDGE__={returnSingleFile:e=>w({toolLabel:"\u5B57\u4F53\u538B\u7F29",idPrefix:"font_compress",...e}),downloadFile:(e,i,t)=>h(e,i,t)};})();
  // 旧内联宽松桥（bytesToBase64/postToMiniProgram + 手工分片循环）已整体迁往共享层
  // server/web_tools/shared/src/mini-program-transfer.ts（Strict Bridge v2：WeixinJSBridge
  // 底层桥优先派发、completedTransfers 终态重试去重、chunkEnd 补 fileSize/totalChunks
  // 终止帧元数据）。构建期由 scripts/build.mjs 把共享桥编译注入本标记区段并挂载
  // window.__MP_BRIDGE__；src 形态只保留委托调用与本页自身的进度/错误提示，业务零改动。
  async function returnToMiniProgram() {
    if (!state.outputBuffer || !state.outputReport || state.busy) return;
    // 运行时保护：未过构建注入的 src 形态被直接部署时立即显式失败，不静默走丢。
    if (!window.__MP_BRIDGE__) throw new Error('共享桥未注入，请使用构建产物');
    const bytes = new Uint8Array(state.outputBuffer);
    setBusy(true, false);
    try {
      // 按钮禁用与提示语必须先于桥调用写入：共享桥在「发帧 + 200ms 等待 + navigateBack」
      // 全部完成后才 resolve，届时页面已在导航，事后更新 UI 用户不可见。
      elements['return-button'].disabled = true;
      elements['delivery-hint'].textContent = '结果已准备完成，正在返回小程序写盘…';
      await window.__MP_BRIDGE__.returnSingleFile({
        bytes,
        fileName: state.outputName,
        fileType: state.analysis.outputContainer === 'TTF' ? 'font/ttf' : 'font/otf',
        onProgress: (completed, total) => {
          setProgress(`正在准备回传 ${completed}/${total}`, Math.round(completed / total * 100));
        },
      });
    } catch (error) {
      toast(error.message || '传回小程序失败');
      setBusy(false);
      elements['return-button'].disabled = false;
      elements['delivery-hint'].textContent = '返回小程序失败，请点击按钮重试。';
    }
  }
// __MP_BRIDGE_SEGMENT_END__

  function cancelProcessing() {
    if (!state.busy) return;
    state.processingVersion += 1;
    for (const pending of state.pending.values()) pending.reject(new Error('处理已取消'));
    state.pending.clear();
    // 压缩后的标记恢复可能正在另一个线程中进行。只终止压缩线程会让旧恢复结果继续返回，
    // 因此取消必须同时关闭两条链路，并依靠 processingVersion 丢弃已经排队的旧消息。
    failMarkerWorker(new Error('处理已取消'));
    void createWorker(state.workerRuntimeSource, state.runtimeLocalOnly);
    setBusy(false);
    setProgress('处理已取消，可以重新生成', 0);
  }

  // ---- 事件绑定与启动 --------------------------------------------------

  elements['font-file'].addEventListener('change', (event) => selectFont(event.target.files && event.target.files[0]));
  elements['process-button'].addEventListener('click', processFont);
  elements['cancel-button'].addEventListener('click', cancelProcessing);
  elements['download-button'].addEventListener('click', downloadOutput);
  elements['return-button'].addEventListener('click', returnToMiniProgram);

  function handleCharsetGridChange(event) {
    const input = event.target;
    if (!(input instanceof HTMLInputElement) || !input.dataset.presetId) return;
    const id = input.dataset.presetId;
    if (input.checked) {
      state.selectedPresets.add(id);
      void loadCharsetPreset(id).then(() => {
        syncCharsetState();
        revokeOutput();
      }).catch((error) => {
        // 加载失败时撤销勾选，避免出现“已勾选但码点从未加载成功”的假象。
        state.selectedPresets.delete(id);
        const checkbox = state.presetInputs.get(id);
        if (checkbox) checkbox.checked = false;
        toast(error.message || '字集加载失败');
        syncCharsetState();
        revokeOutput();
      });
    } else {
      state.selectedPresets.delete(id);
    }
    syncCharsetState();
    revokeOutput();
  }

  function handleCustomTextInput(event) {
    state.customCodepoints = extractCustomCodepoints(event.target.value);
    syncCharsetState();
    revokeOutput();
  }

  function handleOptionChange() {
    syncOptionOutput();
    revokeOutput();
    syncProcessEnabled();
  }

  function handleSvgEdgeChange() {
    syncOptionOutput();
    revokeOutput();
  }

  elements['charset-grid'].addEventListener('change', handleCharsetGridChange);
  elements['custom-text'].addEventListener('input', handleCustomTextInput);
  for (const id of ['opt-no-hinting', 'opt-desubroutinize', 'opt-layout-closure', 'opt-retain-gids', 'opt-svg-downsample']) {
    elements[id].addEventListener('change', handleOptionChange);
  }
  elements['svg-edge'].addEventListener('change', handleSvgEdgeChange);

  renderCharsetGrid();
  syncCharsetState();

  if (inMiniProgram) {
    elements['picker-subtitle'].textContent = '支持 TTF、OTF 和裸 CFF1（将转换为 OTF）';
  }

  window.addEventListener('pagehide', () => {
    // 页面离开后不再接受任何旧任务结果，也及时释放竞速中的全部运行时实例。
    state.selectionVersion += 1;
    state.processingVersion += 1;
    state.runtimeRaceGeneration += 1;
    clearRuntimeRaceTimers();
    for (const candidate of state.runtimeCandidates.values()) {
      try { candidate.worker.terminate(); } catch (_) { /* 页面关闭阶段忽略终止异常。 */ }
    }
    state.runtimeCandidates.clear();
    try { state.worker?.terminate(); } catch (_) { /* 页面关闭阶段忽略终止异常。 */ }
    state.worker = null;
    state.workerReady = false;
    syncRuntimeDependentControls();
    for (const pending of state.pending.values()) pending.reject(new Error('页面已关闭'));
    state.pending.clear();
    failMarkerWorker(new Error('页面已关闭'));
  });

  window.addEventListener('pageshow', (event) => {
    // Safari/微信 WebView 可能把页面放入 BFCache。pagehide 已释放运行时，恢复时必须
    // 重建 Worker，否则控件会保留旧 ready 状态却把请求发给已经终止的线程。
    if (event.persisted && state.launchAuthorized && !state.workerReady) {
      void createWorker(state.workerRuntimeSource, state.runtimeLocalOnly);
    }
  });

  async function startAuthorizedFontCompress() {
    elements['font-file'].disabled = true;
    setRuntimeStatus('正在验证启动权限…', 'loading');
    try {
      const gate = globalThis.JuneOver24ToolLaunchGate;
      if (!gate || typeof gate.verify !== 'function') {
        throw new Error('启动权限模块加载失败，请返回小程序重试');
      }
      const launchResult = await gate.verify('font_compress');
      if (launchResult.mode === 'offline') {
        elements['launch-notice'].textContent = launchResult.notice
          || '当前服务器无法访问，字体压缩以本地应急模式运行。';
        elements['launch-notice'].classList.remove('hidden');
      } else {
        elements['launch-notice'].textContent = '';
        elements['launch-notice'].classList.add('hidden');
      }
      state.launchAuthorized = true;
      // 先并发做轻量探测，再按响应速度启动第一名；超过间隔仍未 ready 才启动第二名。
      // 最多保留两个重型运行时实例，最快者胜出并立即回收其余线程。
      void createWorker();
    } catch (error) {
      state.launchAuthorized = false;
      syncRuntimeDependentControls();
      elements['picker-title'].textContent = '当前入口不可用';
      elements['picker-subtitle'].textContent = error instanceof Error
        ? error.message
        : '启动票据无效，请返回小程序重试';
      setRuntimeStatus(elements['picker-subtitle'].textContent, 'error');
    }
  }

  void startAuthorizedFontCompress();
})();
