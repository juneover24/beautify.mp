(() => {
  'use strict';

  const query = new URLSearchParams(location.search);
  const inMiniProgram = query.get('miniProgram') === '1';
  const CURRENT_ASSET_VERSION = '1.9.4';
  const requestedAssetVersion = query.get('pageVersion');
  // pageVersion 只是小程序传入的缓存标识，不是向后兼容开关。旧版小程序仍可能传
  // 1.7.0；新版 index.html 已经加载 1.9.4 app.js，此时继续沿用旧值会让 Worker 和
  // Python 核心命中旧缓存，形成“新页面配旧处理器”。当前页面版本必须始终优先。
  const assetVersion = requestedAssetVersion === CURRENT_ASSET_VERSION
    ? requestedAssetVersion
    : CURRENT_ASSET_VERSION;
  const bridgeTransferId = query.get('transferId') || `font_${Date.now()}_${Math.random().toString(36).slice(2, 10)}`;
  const CHUNK_BYTES = 1024 * 1024;
  const RUNTIME_PROBE_TIMEOUT_MS = 2_500;
  const RUNTIME_RACE_STAGGER_MS = 4_000;
  const RUNTIME_CANDIDATE_TIMEOUT_MS = 45_000;
  const MAX_HEAVY_RUNTIME_WORKERS = 2;
  const RUNTIME_SOURCE_PROFILES = Object.freeze({
    local: Object.freeze({ priority: 0, probeUrl: 'vendor/pyodide/pyodide.js' }),
    jsdelivr: Object.freeze({ priority: 1, probeUrl: 'https://cdn.jsdelivr.net/pyodide/v0.29.3/full/pyodide.js' }),
    'jsdelivr-fastly': Object.freeze({ priority: 2, probeUrl: 'https://fastly.jsdelivr.net/pyodide/v0.29.3/full/pyodide.js' }),
    'jsdelivr-gcore': Object.freeze({ priority: 3, probeUrl: 'https://gcore.jsdelivr.net/pyodide/v0.29.3/full/pyodide.js' }),
  });
  // 必须与 font_processor.py 的静态粗细映射保持一致。页面只用它计算低成本的
  // 趋势预览；真正导出的轮廓仍由 Python 核心逐字形处理并完成结构校验。
  const SYNTHETIC_WEIGHT_SCALE = 7200;
  const PREVIEW_FONT_SIZE_PX = 31;
  // 与现有字体构建协议保持一致。该值只用于跨端算法兼容，不承担鉴权用途。
  const FONT_MARKER_STEGO_KEY = '24';
  let bridgeSequence = 0;

  const elements = Object.fromEntries([
    'font-file', 'picker-title', 'picker-subtitle', 'launch-notice', 'runtime-status', 'font-meta', 'warnings',
    'controls-card', 'preview-card', 'action-card', 'size-range', 'size-value', 'size-output', 'size-hint',
    'outline-standard', 'outline-standard-output', 'outline-standard-hint', 'outline-standard-button',
    'font-family-input', 'output-file-name-input', 'font-naming-sync-input', 'font-name-output', 'font-name-hint',
    'spacing-range', 'spacing-value', 'spacing-output', 'spacing-hint',
    'line-range', 'line-value', 'line-output', 'line-hint',
    'weight-range', 'weight-value', 'weight-output', 'weight-hint', 'reset-button', 'preview-input',
    'palette-control', 'palette-output', 'color-scope-select', 'palette-select', 'palette-list', 'palette-hint',
    'target-color-control', 'target-characters-input', 'target-color-input', 'target-hex-input', 'target-color-hint',
    'preview-original', 'preview-adjusted', 'adjusted-preview-label', 'progress-copy', 'progress-bar',
    'process-button', 'cancel-button', 'result-card', 'result-title', 'result-summary',
    'result-warnings', 'download-button', 'return-button', 'delivery-hint', 'toast',
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
    paletteData: null,
    activePaletteIndex: 0,
    paletteGrouping: null,
    colorScope: 'unmodified',
    unmodifiedUniformColors: null,
    initialUnmodifiedUniformColors: null,
    // 指定文字改色与打底统一色可同时提交；输入框内容始终保留，颜色按调色板组保存。
    targetCharacters: '',
    targetUniformColors: null,
    initialTargetUniformColors: null,
    initialFontFamilyInput: '',
    fontFamilyDirty: false,
    fontNamingSupported: false,
    fontNamingSyncEnabled: true,
    // 只保存从本次原始字体中确认恢复出的 metadata。未标记字体始终为 null，
    // 因而不会因为经过字体调整而被自动添加新的标记。
    sourceMarkerMetadata: null,
    outputBuffer: null,
    outputBlob: null,
    outputName: '',
    outputReport: null,
    launchAuthorized: false,
    originalFontFace: null,
    outputFontFace: null,
    originalFontFamily: '',
    outputFontFamily: '',
    palettePreviewStyle: null,
    busy: false,
    toastTimer: null,
    selectionVersion: 0,
    processingVersion: 0,
  };

  function formatBytes(value) {
    const bytes = Number(value) || 0;
    if (bytes < 1024) return `${bytes} B`;
    if (bytes < 1024 * 1024) return `${(bytes / 1024).toFixed(1)} KB`;
    return `${(bytes / 1024 / 1024).toFixed(2)} MB`;
  }

  const MAX_FAMILY_NAME_LENGTH = 128;
  const MAX_OUTPUT_FILE_NAME_LENGTH = 120;
  const WINDOWS_RESERVED_FILE_NAMES = /^(con|prn|aux|nul|com[1-9]|lpt[1-9])$/i;

  /**
   * 统一字体名称的比较文本，但不在这里套用“新名称最多 128 字符”的限制。
   *
   * 少数字体原有名称可能已经超过产品输入上限。只要用户没有修改名称，就应允许
   * 继续调整其它参数并原样保留旧名称；只有真正提交新名称时才执行严格校验。
   */
  function normalizeFontFamilyNameForComparison(input) {
    return String(input || '')
      .normalize('NFC')
      .replace(/[\u0000-\u001f\u007f]/g, ' ')
      .replace(/\s+/g, ' ')
      .trim();
  }

  /** 与修符 normalizeFontFamilyName 保持一致，中文、表情等合法 Unicode 均保留。 */
  function normalizeFontFamilyName(input) {
    const normalized = normalizeFontFamilyNameForComparison(input);
    if (!normalized) throw new Error('字体内部名称不能为空');
    if (Array.from(normalized).length > MAX_FAMILY_NAME_LENGTH) {
      throw new Error(`字体内部名称不能超过 ${MAX_FAMILY_NAME_LENGTH} 个字符`);
    }
    return normalized;
  }

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

  /**
   * 复用修符的文件名安全规则，但保留字体调整的真实 SFNT 输出格式。
   * 裸 CFF1 输入会先由处理核心封装为 OTF，用户即使输入了另一种扩展名，最终也必须
   * 使用处理核心推荐的真实容器扩展名，
   * 避免内容为 OTF 却命名成 .ttf，导致系统和分享面板误判格式。
   */
  function normalizeFontOutputFileName(input, extension, fallback) {
    const normalizedExtension = extension === '.otf' ? '.otf' : '.ttf';
    const requestedBase = sanitizeOutputFileBase(input);
    const fallbackBase = sanitizeOutputFileBase(fallback) || 'font';
    const maximumBaseLength = MAX_OUTPUT_FILE_NAME_LENGTH - normalizedExtension.length;
    const base = Array.from(requestedBase || fallbackBase)
      .slice(0, maximumBaseLength)
      .join('')
      .replace(/[. ]+$/g, '') || 'font';
    return `${base}${normalizedExtension}`;
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
   * 轮廓比例采用统一的“宽 × 高”标准：一个滑块值同时作为水平和垂直百分比。
   * 例如 90 显示为 90% × 90%，对应常见字体编辑器中的等比 Scale(90, 90)。
   * 不提供两个容易误触的独立值，可以保证字形比例不会被意外拉伸或压扁。
   */
  function formatOutlineScale(value) {
    const rounded = Math.round((Number(value) || 0) * 10) / 10;
    const text = Number.isInteger(rounded) ? String(rounded) : rounded.toFixed(1);
    return `${text}% × ${text}%`;
  }

  function outlineScaleRange(analysis) {
    // sizePercent 是 1.4.0 之前的协议字段。保留回退后，新页面即使命中旧分析
    // 结果也能继续工作；新版核心会同时返回两个字段，便于缓存版本平滑过渡。
    return analysis.ranges.outlineScalePercent || analysis.ranges.sizePercent;
  }

  function supportsOutlineScale(analysis) {
    return Boolean(analysis?.capabilities?.outlineScale ?? analysis?.capabilities?.size);
  }

  function syncOutlineStandardButton() {
    const button = elements['outline-standard-button'];
    const recommended = Number(button.dataset.recommendedScale);
    const current = Number(elements['size-value'].value);
    if (!Number.isFinite(recommended) || !supportsOutlineScale(state.analysis || {})) {
      button.disabled = true;
      button.textContent = '应用标准比例';
      return;
    }
    const matches = Math.abs(current - recommended) < 0.05;
    button.disabled = matches;
    button.textContent = matches ? '已应用标准比例' : '应用标准比例';
  }

  /**
   * 展示处理核心给出的跨字体字面推荐。
   *
   * “统一字面”不是 OpenType 自带字段，而是用代表字形真实轮廓的中位高度与产品
   * 目标字面率比较后得到的推荐比例。页面不自动套用，避免用户只想改色或调粗细时
   * 轮廓被静默改变；必须点击按钮才会写入滑块，之后仍可继续手动微调。
   */
  function renderOutlineStandard(analysis) {
    const standard = analysis.outlineStandard || {};
    const output = elements['outline-standard-output'];
    const hint = elements['outline-standard-hint'];
    const button = elements['outline-standard-button'];
    button.dataset.recommendedScale = '';

    if (!standard.available || !Number.isFinite(Number(standard.recommendedScalePercent))) {
      output.textContent = '暂无可靠推荐';
      hint.textContent = `${standard.reason || '当前字体缺少足够的代表字形'}；基准：${standard.anchorLabel || '轴线中心'}。`;
      button.disabled = true;
      button.textContent = '应用标准比例';
      return;
    }

    const recommended = Number(standard.recommendedScalePercent);
    const sampleDescription = Number(standard.sampleCount) > 0
      ? `${standard.label} ${standard.sampleCount} 个参考字形`
      : standard.label;
    const safetyCopy = standard.limitedBySafety ? '，推荐值已按安全范围限制' : '';
    output.textContent = `推荐 ${formatOutlineScale(recommended)}`;
    hint.textContent = `${sampleDescription}：当前字面约 ${standard.currentFacePercent}%，统一目标 ${standard.targetFacePercent}%；基准：${standard.anchorLabel || '轴线中心'}${safetyCopy}。`;
    button.dataset.recommendedScale = String(recommended);
    syncOutlineStandardButton();
  }

  function applyOutlineStandard() {
    const recommended = Number(elements['outline-standard-button'].dataset.recommendedScale);
    if (!Number.isFinite(recommended) || !state.analysis) {
      toast('当前字体暂无可靠的标准比例');
      return;
    }
    setParameterValue('size-range', 'size-value', recommended, true);
    syncRangeLabels();
    revokeOutput();
    updatePreview();
    toast('已应用统一字面比例');
  }

  function createDefaultOutputName() {
    const extension = state.analysis?.recommendedExtension === '.otf' ? '.otf' : '.ttf';
    const sourceName = state.file?.name || `font${extension}`;
    const sourceBase = sanitizeOutputFileBase(sourceName) || 'font';
    return normalizeFontOutputFileName(
      `${sourceBase}-调整后${extension}`,
      extension,
      sourceName,
    );
  }

  function setFontNamingSyncEnabled(enabled) {
    state.fontNamingSyncEnabled = Boolean(enabled && state.fontNamingSupported);
    elements['font-naming-sync-input'].checked = state.fontNamingSyncEnabled;
  }

  function syncFamilyNameFromOutputFileName() {
    if (!state.fontNamingSyncEnabled || !state.analysis || !state.fontNamingSupported) return;
    const extension = state.analysis.recommendedExtension === '.otf' ? '.otf' : '.ttf';
    const outputName = normalizeFontOutputFileName(
      elements['output-file-name-input'].value,
      extension,
      createDefaultOutputName(),
    );
    const familyName = normalizeFontFamilyName(sanitizeOutputFileBase(outputName));
    elements['font-family-input'].value = familyName;
    syncFontNamingState();
  }

  function syncFontNamingState() {
    const input = elements['font-family-input'];
    const output = elements['font-name-output'];
    if (!state.analysis || !state.fontNamingSupported) {
      state.fontFamilyDirty = false;
      output.textContent = '当前版本不支持';
      elements['font-name-hint'].textContent = '当前字体处理组件版本不支持修改内部名称。';
      return;
    }
    const normalized = normalizeFontFamilyNameForComparison(input.value);
    const initial = normalizeFontFamilyNameForComparison(state.initialFontFamilyInput);
    state.fontFamilyDirty = normalized !== initial;
    const syncHint = state.fontNamingSyncEnabled
      ? '编辑导出文件名会同步内部名称；成品同时更新字体族名、完整名称和 PostScript 名称。'
      : '已拆分命名；导出文件名与安装、导入后显示的字体内部名称可分别设置。';
    if (!state.fontFamilyDirty) {
      output.textContent = '保持原名';
      elements['font-name-hint'].textContent = syncHint;
      return;
    }
    try {
      normalizeFontFamilyName(input.value);
      output.textContent = '将修改名称';
      elements['font-name-hint'].textContent = syncHint;
    } catch (error) {
      output.textContent = '名称待修正';
      elements['font-name-hint'].textContent = error.message || '字体内部名称无效';
    }
  }

  function renderFontNaming(analysis) {
    const naming = analysis.fontNaming;
    const sourceBase = sanitizeOutputFileBase(state.file?.name || '') || 'font';
    const sourceFamilyName = String(naming?.fontFamily || '');
    const inputValue = sourceFamilyName || sourceBase;
    state.fontNamingSupported = Boolean(naming && Object.prototype.hasOwnProperty.call(naming, 'fontFamily'));
    state.initialFontFamilyInput = inputValue;
    state.fontFamilyDirty = false;
    elements['font-family-input'].value = inputValue;
    elements['font-family-input'].disabled = !state.fontNamingSupported;
    elements['output-file-name-input'].value = createDefaultOutputName();
    elements['output-file-name-input'].disabled = false;
    elements['font-naming-sync-input'].disabled = !state.fontNamingSupported;
    // 默认输出名带“调整后”，加载或重置时不能据此静默改 name 表；只有用户实际
    // 编辑文件名，或者主动重新开启联动后，才把去扩展名后的安全名称写入内部名称。
    setFontNamingSyncEnabled(true);
    syncFontNamingState();
  }

  /**
   * 返回字体调整工具的规范静态资源地址。
   *
   * 历史入口曾出现过目录首字母大写的地址。Windows 开发环境不会暴露问题，但在
   * Linux 线上会让 Worker 继续按错误大小写加载后续脚本。这里仅规范已知目录段，
   * 同时保留当前域名和可能存在的部署前缀，确保页面从旧缓存入口恢复后也能自愈。
   */
  function fontAdjustmentAssetUrl(relativePath) {
    const baseUrl = new URL('./', location.href);
    const canonicalSegment = '/static/font-adjustment-local/';
    const segmentIndex = baseUrl.pathname.toLowerCase().indexOf(canonicalSegment);
    if (segmentIndex >= 0) {
      baseUrl.pathname = `${baseUrl.pathname.slice(0, segmentIndex)}${canonicalSegment}${baseUrl.pathname.slice(segmentIndex + canonicalSegment.length)}`;
    }
    return new URL(relativePath, baseUrl);
  }

  /** 根据权限、运行时和当前任务统一控制两个会触发字体处理的入口。 */
  function syncRuntimeDependentControls() {
    elements['font-file'].disabled = (
      !state.launchAuthorized
      || state.busy
      || !state.workerReady
    );
    elements['process-button'].disabled = (
      !state.launchAuthorized
      || state.busy
      || !state.workerReady
      || !state.file
      || !state.analysis
    );
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
    const workerUrl = fontAdjustmentAssetUrl('font-adjustment.worker.js');
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
      ? fontAdjustmentAssetUrl(profile.probeUrl).href
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
   * 字体轮廓和度量调整完成后，在最终成品上重新写入原 metadata。
   * 成功条件与字体修符一致：保存检查通过、至少一个维度能恢复完整 metadata，且
   * 最终成品中的冗余记录全部一致。任何一项失败都不允许进入下载或回传步骤。
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

  function revokeOutput() {
    state.outputBuffer = null;
    state.outputBlob = null;
    state.outputName = '';
    state.outputReport = null;
    elements['result-card'].classList.add('hidden');
    if (state.outputFontFace) {
      document.fonts.delete(state.outputFontFace);
      state.outputFontFace = null;
    }
    state.outputFontFamily = '';
    elements['adjusted-preview-label'].textContent = '参数预览';
  }

  /** 清除静态粗细的近似预览样式，避免真实输出字体再次叠加浏览器效果。 */
  function clearSyntheticWeightPreview() {
    const preview = elements['preview-adjusted'];
    preview.classList.remove('synthetic-weight-preview', 'synthetic-weight-stroke-preview');
    preview.style.removeProperty('--synthetic-weight-stroke');
  }

  /**
   * 为没有 wght 轴的静态字体显示连续、可见的粗细趋势。
   *
   * 页面全局禁用了浏览器字体合成，防止普通界面文字因缺少字重文件而失真；旧实现
   * 只修改 font-weight，因此静态字体滑块实际上没有任何视觉响应。单色字体现在按
   * Python 核心相同的 `UPEM * weightDelta / 7200` 映射，把字体单位换算为当前预览
   * 像素并用文字描边近似轮廓外扩。彩色字体不能叠加单色描边，否则会污染 COLR
   * 图层颜色，所以只在预览元素上局部恢复浏览器 weight synthesis。两种方式都只是
   * 参数趋势预览；处理完成后 loadPreviewFont 会清除这些样式并显示真实成品轮廓。
   */
  function applySyntheticWeightPreview(weightMode, weight, outlineScale) {
    const preview = elements['preview-adjusted'];
    const weightRange = state.analysis?.ranges?.weight;
    const baseWeight = Number(weightRange?.default) || 400;
    const weightDelta = Math.max(0, Number(weight) - baseWeight);
    const isSynthetic = String(weightMode || '').startsWith('synthetic-') && weightDelta > 0;
    const usesStroke = isSynthetic && !state.analysis?.capabilities?.colorPalette;

    preview.classList.toggle('synthetic-weight-preview', isSynthetic);
    preview.classList.toggle('synthetic-weight-stroke-preview', usesStroke);
    if (usesStroke) {
      const scaledFontSize = PREVIEW_FONT_SIZE_PX * Number(outlineScale || 100) / 100;
      const strokeWidth = scaledFontSize * weightDelta / SYNTHETIC_WEIGHT_SCALE;
      preview.style.setProperty('--synthetic-weight-stroke', `${strokeWidth.toFixed(3)}px`);
      // 描边已经按实际强度连续变化；保持基础字重可防止浏览器再额外合成一次粗体。
      preview.style.fontWeight = String(baseWeight);
    } else {
      preview.style.removeProperty('--synthetic-weight-stroke');
      preview.style.fontWeight = isSynthetic ? String(weight) : 'normal';
    }
  }

  function revokeOriginalPreview() {
    if (state.originalFontFace) {
      document.fonts.delete(state.originalFontFace);
      state.originalFontFace = null;
    }
    state.originalFontFamily = '';
    elements['preview-original'].style.fontFamily = '';
    updatePalettePreview();
  }

  function clearSelectedFont() {
    state.file = null;
    state.analysis = null;
    state.paletteData = null;
    state.activePaletteIndex = 0;
    state.paletteGrouping = null;
    state.colorScope = 'unmodified';
    state.unmodifiedUniformColors = null;
    state.initialUnmodifiedUniformColors = null;
    state.targetCharacters = '';
    state.targetUniformColors = null;
    state.initialTargetUniformColors = null;
    state.initialFontFamilyInput = '';
    state.fontFamilyDirty = false;
    state.fontNamingSupported = false;
    state.sourceMarkerMetadata = null;
    if (elements['target-characters-input']) elements['target-characters-input'].value = '';
    if (elements['target-color-control']) elements['target-color-control'].classList.add('hidden');
    revokeOutput();
    revokeOriginalPreview();
    elements['controls-card'].classList.add('hidden');
    elements['preview-card'].classList.add('hidden');
    elements['action-card'].classList.add('hidden');
    elements['font-meta'].classList.add('hidden');
    showWarnings(elements.warnings, []);
    syncRuntimeDependentControls();
  }

  function showWarnings(target, warnings) {
    const list = Array.isArray(warnings) ? warnings.filter(Boolean) : [];
    target.innerHTML = list.map((warning) => `<div>• ${escapeHtml(warning)}</div>`).join('');
    target.classList.toggle('hidden', list.length === 0);
  }

  function escapeHtml(value) {
    return String(value).replace(/[&<>'"]/g, (char) => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', "'": '&#39;', '"': '&quot;' })[char]);
  }

  /**
   * 接受常见的 3/4/6/8 位十六进制颜色并返回统一的大写形式。
   * 六位颜色表示完全不透明；八位颜色的最后两位是 alpha。返回 null 代表输入仍不
   * 完整或包含非法字符，输入框会保留原文，方便用户继续编辑而不是强制跳回旧值。
   */
  function normalizeHexColor(value) {
    let text = String(value || '').trim().toUpperCase();
    if (text.startsWith('#')) text = text.slice(1);
    if ([3, 4].includes(text.length)) {
      text = Array.from(text, (character) => character + character).join('');
    }
    if (![6, 8].includes(text.length) || !/^[0-9A-F]+$/.test(text)) return null;
    return `#${text}`;
  }

  function clonePaletteData(palettes) {
    return Array.isArray(palettes)
      ? palettes.map((palette) => Array.isArray(palette) ? palette.map((color) => String(color)) : [])
      : null;
  }

  function cloneColorList(colors) {
    return Array.isArray(colors) ? colors.map((color) => String(color)) : null;
  }

  function normalizePaletteDataState() {
    if (!Array.isArray(state.paletteData)) throw new Error('彩色调色板数据尚未准备完成');
    const normalized = state.paletteData.map((palette, paletteIndex) => {
      if (!Array.isArray(palette)) throw new Error(`调色板 ${paletteIndex + 1} 数据无效`);
      return palette.map((color, colorIndex) => {
        const value = normalizeHexColor(color);
        if (!value) {
          throw new Error(`调色板 ${paletteIndex + 1} 的颜色 #${colorIndex + 1} 格式无效`);
        }
        return value;
      });
    });
    state.paletteData = clonePaletteData(normalized);
    return normalized;
  }

  function unmodifiedUniformColorDirty() {
    const current = state.unmodifiedUniformColors;
    const initial = state.initialUnmodifiedUniformColors;
    if (!Array.isArray(current) || !Array.isArray(initial) || current.length !== initial.length) return false;
    return current.some((color, index) => normalizeHexColor(color) !== normalizeHexColor(initial[index]));
  }

  function normalizeTargetCharacters(input) {
    // 与 Python 核心保持一致：去掉控制符、按首次出现去重，保留用户输入顺序。
    const text = String(input || '').replace(/[\x00-\x1f\x7f]/g, '');
    const seen = new Set();
    let ordered = '';
    for (const character of text) {
      if (seen.has(character)) continue;
      seen.add(character);
      ordered += character;
    }
    return ordered;
  }

  function targetUniformColorDirty() {
    const current = state.targetUniformColors;
    const initial = state.initialTargetUniformColors;
    if (!Array.isArray(current) || !Array.isArray(initial) || current.length !== initial.length) return false;
    return current.some((color, index) => normalizeHexColor(color) !== normalizeHexColor(initial[index]));
  }

  function targetCharactersDirty() {
    return normalizeTargetCharacters(state.targetCharacters || elements['target-characters-input']?.value || '') !== '';
  }

  function renderTargetColorControls() {
    const control = elements['target-color-control'];
    if (!control) return;
    if (!state.paletteGrouping?.supported || !Array.isArray(state.targetUniformColors)) {
      control.classList.add('hidden');
      return;
    }
    control.classList.remove('hidden');
    elements['target-characters-input'].value = state.targetCharacters || '';
    const rawColor = state.targetUniformColors[state.activePaletteIndex] || '#000000';
    const normalized = normalizeHexColor(rawColor);
    elements['target-color-input'].value = normalized ? normalized.slice(0, 7) : '#000000';
    elements['target-hex-input'].value = rawColor;
    elements['target-hex-input'].classList.toggle('invalid', !normalized);
    const count = normalizeTargetCharacters(state.targetCharacters).length;
    elements['target-color-hint'].textContent = count > 0
      ? `将单独改色 ${count} 个字；可与上方打底统一色同时使用，不改修符字形。`
      : '可与上方打底统一色同时使用；只给输入文字单独上色，不改修符字形。';
  }

  function renderActivePalette() {
    const list = elements['palette-list'];
    const palettes = state.paletteData;
    const palette = palettes && palettes[state.activePaletteIndex];
    if (!Array.isArray(palette)) {
      list.innerHTML = '';
      list.classList.add('hidden');
      renderTargetColorControls();
      return;
    }

    const grouping = state.paletteGrouping;
    const grouped = Boolean(grouping?.supported);
    if (grouped && state.colorScope === 'unmodified') {
      const rawColor = state.unmodifiedUniformColors?.[state.activePaletteIndex] || '#000000';
      const normalized = normalizeHexColor(rawColor);
      const pickerValue = normalized ? normalized.slice(0, 7) : '#000000';
      list.innerHTML = `
        <div class="palette-row base-uniform" data-color-kind="unmodified">
          <span class="palette-index">统一色</span>
          <input class="palette-color-input" type="color" value="${pickerValue}" aria-label="未修改打底字统一颜色">
          <input class="palette-hex-input${normalized ? '' : ' invalid'}" type="text" value="${escapeHtml(rawColor)}"
            maxlength="9" autocomplete="off" spellcheck="false" inputmode="text"
            aria-label="未修改打底字统一颜色十六进制值">
        </div>`;
      elements['palette-output'].textContent = `${grouping.unmodifiedGlyphCount} 字 · 统一色`;
      elements['palette-hint'].textContent = `统一修改 ${grouping.unmodifiedGlyphCount} 个未修改打底字，不影响已修改字形。下方可继续指定文字单独改色。`;
      list.classList.remove('hidden');
      renderTargetColorControls();
      return;
    }

    const visibleIndices = grouped
      ? (grouping.modifiedPaletteIndices || [])
      : palette.map((_, index) => index);
    if (grouped && visibleIndices.length === 0) {
      list.innerHTML = '<div class="palette-empty">当前已修改字形没有可单独调整的彩色图层。</div>';
      elements['palette-output'].textContent = `${grouping.modifiedGlyphCount} 字 · 0 色`;
      elements['palette-hint'].textContent = '当前已修改字形没有引用彩色调色板颜色。下方可继续指定文字单独改色。';
      list.classList.remove('hidden');
      renderTargetColorControls();
      return;
    }

    list.innerHTML = visibleIndices.map((colorIndex) => {
      const rawColor = palette[colorIndex];
      const normalized = normalizeHexColor(rawColor);
      const pickerValue = normalized ? normalized.slice(0, 7) : '#000000';
      return `
        <div class="palette-row" data-color-kind="palette" data-color-index="${colorIndex}">
          <span class="palette-index">#${colorIndex + 1}</span>
          <input class="palette-color-input" type="color" value="${pickerValue}" aria-label="调色板颜色 ${colorIndex + 1}">
          <input class="palette-hex-input${normalized ? '' : ' invalid'}" type="text" value="${escapeHtml(rawColor)}"
            maxlength="9" autocomplete="off" spellcheck="false" inputmode="text"
            aria-label="调色板颜色 ${colorIndex + 1} 十六进制值">
        </div>`;
    }).join('');
    if (grouped) {
      elements['palette-output'].textContent = `${grouping.modifiedGlyphCount} 字 · ${visibleIndices.length} 色`;
      elements['palette-hint'].textContent = `仅显示 ${grouping.modifiedGlyphCount} 个已修改字形实际引用的颜色，不影响未修改打底字。下方可继续指定文字单独改色。`;
    } else {
      elements['palette-output'].textContent = `${palettes.length} 组 · ${palette.length} 色`;
    }
    list.classList.remove('hidden');
    renderTargetColorControls();
  }

  function renderPaletteControls(analysis) {
    const paletteInfo = analysis.colorPalette;
    const supported = Boolean(
      analysis.capabilities.colorPalette
      && paletteInfo
      && Array.isArray(paletteInfo.palettes)
      && paletteInfo.paletteCount > 0
      && paletteInfo.entryCount > 0
    );
    if (!supported) {
      state.paletteData = null;
      state.activePaletteIndex = 0;
      state.paletteGrouping = null;
      state.unmodifiedUniformColors = null;
      state.initialUnmodifiedUniformColors = null;
      state.targetCharacters = '';
      state.targetUniformColors = null;
      state.initialTargetUniformColors = null;
      elements['palette-output'].textContent = '不支持';
      elements['color-scope-select'].innerHTML = '';
      elements['color-scope-select'].classList.add('hidden');
      elements['palette-select'].innerHTML = '';
      elements['palette-select'].classList.add('hidden');
      elements['palette-list'].innerHTML = '';
      elements['palette-list'].classList.add('hidden');
      elements['palette-hint'].textContent = '当前字体不包含可修改的彩色调色板。';
      renderTargetColorControls();
      return;
    }

    state.paletteData = clonePaletteData(paletteInfo.palettes);
    state.activePaletteIndex = 0;
    const groupingUnavailableReason = (
      paletteInfo.grouping?.supported === false
      && paletteInfo.grouping?.manifestSource === 'invalid'
      && String(paletteInfo.grouping?.reason || '').trim()
    );
    if (groupingUnavailableReason) {
      // 分组清单损坏时不能退回“整张调色板直接修改”。已经处理过的打底字和修符字形
      // 可能共享或分离 palette index，继续显示颜色输入框会让用户误以为仍能安全分组。
      // 保留 paletteData 仅供原字体预览和无颜色改动的其它参数处理，不提供写入入口。
      state.paletteGrouping = null;
      state.unmodifiedUniformColors = null;
      state.initialUnmodifiedUniformColors = null;
      state.targetCharacters = '';
      state.targetUniformColors = null;
      state.initialTargetUniformColors = null;
      elements['palette-output'].textContent = '暂不可用';
      elements['color-scope-select'].innerHTML = '';
      elements['color-scope-select'].classList.add('hidden');
      elements['palette-select'].innerHTML = '';
      elements['palette-select'].classList.add('hidden');
      elements['palette-list'].innerHTML = '';
      elements['palette-list'].classList.add('hidden');
      elements['palette-hint'].textContent = groupingUnavailableReason;
      renderTargetColorControls();
      return;
    }
    state.paletteGrouping = analysis.capabilities.colorGrouping && paletteInfo.grouping?.supported
      ? { ...paletteInfo.grouping }
      : null;
    state.colorScope = state.paletteGrouping?.unmodifiedGlyphCount > 0 ? 'unmodified' : 'modified';
    state.unmodifiedUniformColors = cloneColorList(state.paletteGrouping?.unmodifiedUniformColors);
    state.initialUnmodifiedUniformColors = cloneColorList(state.paletteGrouping?.unmodifiedUniformColors);
    // 指定文字颜色默认跟当前打底统一色一致，方便只改字不改色值；真正提交时仍会
    // 建独立 palette index，保证之后改打底色不会带走指定字。
    const defaultTargetColors = cloneColorList(
      state.paletteGrouping?.unmodifiedUniformColors
      || paletteInfo.palettes.map((palette) => (Array.isArray(palette) && palette[0]) || '#000000'),
    );
    state.targetUniformColors = cloneColorList(defaultTargetColors);
    state.initialTargetUniformColors = cloneColorList(defaultTargetColors);
    if (!state.targetCharacters) state.targetCharacters = '';
    if (state.paletteGrouping) {
      elements['color-scope-select'].innerHTML = [
        state.paletteGrouping.unmodifiedGlyphCount > 0
          ? `<option value="unmodified">未修改打底字（${state.paletteGrouping.unmodifiedGlyphCount} 个）</option>`
          : '',
        state.paletteGrouping.modifiedGlyphCount > 0
          ? `<option value="modified">已修改字形（${state.paletteGrouping.modifiedGlyphCount} 个）</option>`
          : '',
      ].join('');
      elements['color-scope-select'].value = state.colorScope;
      elements['color-scope-select'].classList.remove('hidden');
    } else {
      elements['color-scope-select'].innerHTML = '';
      elements['color-scope-select'].classList.add('hidden');
    }
    elements['palette-select'].innerHTML = Array.from(
      { length: paletteInfo.paletteCount },
      (_, index) => `<option value="${index}">调色板 ${index + 1}${index === 0 ? '（默认）' : ''}</option>`,
    ).join('');
    elements['palette-select'].value = '0';
    elements['palette-select'].classList.toggle('hidden', paletteInfo.paletteCount <= 1);
    if (!state.paletteGrouping) {
      elements['palette-hint'].textContent = paletteInfo.paletteCount > 1
        ? '可切换修改全部调色板；支持 #RRGGBB 和带透明度的 #RRGGBBAA。'
        : '支持 #RRGGBB 和带透明度的 #RRGGBBAA。';
    }
    renderActivePalette();
  }

  function collectColorOptions() {
    if (!state.analysis?.capabilities?.colorPalette) {
      return { paletteData: null, colorGroupData: null };
    }
    const normalizedPalettes = normalizePaletteDataState();
    if (!state.paletteGrouping) {
      return { paletteData: normalizedPalettes, colorGroupData: null };
    }
    let unmodifiedUniformColors = null;
    if (unmodifiedUniformColorDirty()) {
      unmodifiedUniformColors = state.unmodifiedUniformColors.map((color, paletteIndex) => {
        const normalized = normalizeHexColor(color);
        if (!normalized) throw new Error(`调色板 ${paletteIndex + 1} 的打底字统一颜色格式无效`);
        return normalized;
      });
      state.unmodifiedUniformColors = cloneColorList(unmodifiedUniformColors);
    }
    const targetCharacters = normalizeTargetCharacters(
      elements['target-characters-input']?.value || state.targetCharacters || '',
    );
    state.targetCharacters = targetCharacters;
    let targetUniformColors = null;
    // 指定文字只要输入了内容就提交；颜色可与打底统一色同时存在，由核心隔离 palette。
    if (targetCharacters) {
      if (!Array.isArray(state.targetUniformColors) || state.targetUniformColors.length === 0) {
        throw new Error('指定文字颜色尚未准备完成');
      }
      targetUniformColors = state.targetUniformColors.map((color, paletteIndex) => {
        const normalized = normalizeHexColor(color);
        if (!normalized) throw new Error(`调色板 ${paletteIndex + 1} 的指定文字颜色格式无效`);
        return normalized;
      });
      state.targetUniformColors = cloneColorList(targetUniformColors);
    }
    return {
      paletteData: null,
      colorGroupData: {
        modifiedPaletteData: normalizedPalettes,
        unmodifiedUniformColors,
        targetCharacters: targetCharacters || null,
        targetUniformColors,
      },
    };
  }

  function paletteRowBinding(row) {
    if (!(row instanceof HTMLElement)) return null;
    if (row.dataset.colorKind === 'unmodified') {
      const colors = state.unmodifiedUniformColors;
      if (!Array.isArray(colors) || state.activePaletteIndex >= colors.length) return null;
      return {
        value: () => colors[state.activePaletteIndex],
        setValue: (value) => { colors[state.activePaletteIndex] = value; },
      };
    }
    const colorIndex = Number(row.dataset.colorIndex);
    const palette = state.paletteData && state.paletteData[state.activePaletteIndex];
    if (
      !Array.isArray(palette)
      || !Number.isInteger(colorIndex)
      || colorIndex < 0
      || colorIndex >= palette.length
    ) return null;
    return {
      value: () => palette[colorIndex],
      setValue: (value) => { palette[colorIndex] = value; },
    };
  }

  /**
   * 使用 CSS Font Palettes 为当前选中的调色板生成即时预览。
   *
   * 原始预览只切换 base-palette；参数预览在字体尚未生成时通过 override-colors 覆盖
   * 当前草稿。生成完成后则直接读取输出字体中的对应调色板。浏览器不支持该 CSS 能力
   * 时会自然回退到默认调色板，不影响最终字体文件的真实写入。
   */
  function updatePalettePreview() {
    if (!state.palettePreviewStyle) {
      state.palettePreviewStyle = document.createElement('style');
      state.palettePreviewStyle.dataset.role = 'font-adjustment-palette-preview';
      document.head.appendChild(state.palettePreviewStyle);
    }

    const palette = state.paletteData && state.paletteData[state.activePaletteIndex];
    if (!state.analysis?.capabilities?.colorPalette || !state.originalFontFamily || !Array.isArray(palette)) {
      state.palettePreviewStyle.textContent = '';
      elements['preview-original'].style.setProperty('font-palette', 'normal');
      elements['preview-adjusted'].style.setProperty('font-palette', 'normal');
      elements['preview-original'].style.color = '';
      elements['preview-adjusted'].style.color = '';
      return;
    }

    const previewPalette = [...palette];
    const baseColorDirty = unmodifiedUniformColorDirty();
    const uniformPaletteIndex = Number(state.paletteGrouping?.unmodifiedUniformPaletteIndex);
    const unmodifiedColor = normalizeHexColor(
      state.unmodifiedUniformColors?.[state.activePaletteIndex],
    );
    if (
      baseColorDirty
      && Number.isInteger(uniformPaletteIndex)
      && uniformPaletteIndex >= 0
      && uniformPaletteIndex < previewPalette.length
      && unmodifiedColor
    ) {
      // 已经由本工具生成过的打底字拥有专用 palette index，预览时直接覆盖该索引；
      // 首次处理的普通单色打底字没有 COLR 层，则由下方 CSS color 提供即时预览。
      previewPalette[uniformPaletteIndex] = unmodifiedColor;
    }
    const overrideColors = previewPalette
      .map((color, index) => {
        const normalized = normalizeHexColor(color);
        return normalized ? `${index} ${normalized}` : null;
      })
      .filter(Boolean)
      .join(', ');
    const adjustedFamily = state.outputFontFamily || state.originalFontFamily;
    const adjustedOverrides = state.outputFontFamily || !overrideColors
      ? ''
      : `override-colors: ${overrideColors};`;
    state.palettePreviewStyle.textContent = `
      @font-palette-values --FontAdjustmentOriginalPalette {
        font-family: "${state.originalFontFamily}";
        base-palette: ${state.activePaletteIndex};
      }
      @font-palette-values --FontAdjustmentAdjustedPalette {
        font-family: "${adjustedFamily}";
        base-palette: ${state.activePaletteIndex};
        ${adjustedOverrides}
      }
    `;
    elements['preview-original'].style.setProperty('font-palette', '--FontAdjustmentOriginalPalette');
    elements['preview-adjusted'].style.setProperty('font-palette', '--FontAdjustmentAdjustedPalette');
    elements['preview-original'].style.color = '';
    elements['preview-adjusted'].style.color = (
      !state.outputFontFamily && baseColorDirty && unmodifiedColor ? unmodifiedColor : ''
    );
  }

  /**
   * 配置“快捷滑块 + 自由数值输入”参数控件。
   *
   * 滑块仍使用分析阶段给出的推荐区间，方便日常微调；旁边的 number 输入不写
   * min/max，用户可直接输入区间外数值。输入超出滑块窗口时先扩展窗口再赋值，
   * 避免 range 元素按旧边界静默截断。最终是否能生成由处理核心根据真实字体字段
   * 和 OpenType 整数范围判断，不再由页面固定上下限提前拦截。
   */
  function configureRange(element, valueElement, output, range, formatter, enabled = true) {
    const step = Number(range.step || 1);
    // 推荐区间只服务于滑块的日常微调，不是数值输入的业务边界。保存原始窗口后，
    // setParameterValue 可在自由输入回到常用区间时自动收回滑块，避免曾输入一个
    // 极大数值后滑块永久失去精度。
    element.dataset.recommendedMin = String(range.min);
    element.dataset.recommendedMax = String(range.max);
    element.min = String(range.min);
    element.max = String(range.max);
    element.step = String(step);
    valueElement.step = String(step);
    valueElement.removeAttribute('min');
    valueElement.removeAttribute('max');
    element.disabled = !enabled || range.min === range.max;
    valueElement.disabled = !enabled;
    setParameterValue(element.id, valueElement.id, Number(range.default), true);
    output.textContent = formatter(Number(range.default));
  }

  function setParameterValue(rangeId, valueId, value, expandRange = false) {
    const numeric = Number(value);
    if (!Number.isFinite(numeric)) return false;
    const range = elements[rangeId];
    const valueInput = elements[valueId];
    if (expandRange) {
      const recommendedMin = Number(range.dataset.recommendedMin);
      const recommendedMax = Number(range.dataset.recommendedMax);
      const baseMin = Number.isFinite(recommendedMin) ? recommendedMin : numeric;
      const baseMax = Number.isFinite(recommendedMax) ? recommendedMax : numeric;
      // 每次都从推荐窗口重新计算，而不是在当前窗口上只增不减。这样自由输入超出
      // 区间时滑块仍能表示该值，回到常用区间或点击“恢复默认”后又能恢复微调精度。
      range.min = String(Math.min(baseMin, numeric));
      range.max = String(Math.max(baseMax, numeric));
    }
    range.value = String(numeric);
    valueInput.value = String(numeric);
    return true;
  }

  function numericInputValue(valueId) {
    const raw = String(elements[valueId].value || '').trim();
    return raw ? Number(raw) : Number.NaN;
  }

  function parameterValue(valueId, fallback) {
    const value = numericInputValue(valueId);
    return Number.isFinite(value) ? value : fallback;
  }

  function requiredParameterValue(valueId, label) {
    const value = numericInputValue(valueId);
    if (!Number.isFinite(value)) throw new Error(`请输入有效的${label}`);
    return value;
  }

  function renderAnalysis(analysis) {
    state.analysis = analysis;
    const meta = [
      ['格式', `${analysis.container} · ${analysis.outlineKind}`],
      ['文件大小', formatBytes(analysis.fileSize)],
      ['字形数量', String(analysis.glyphCount)],
      ['字体表', `${analysis.tableCount} 个`],
    ];
    elements['font-meta'].innerHTML = meta.map(([label, value]) => (
      `<div class="meta-item"><span>${escapeHtml(label)}</span><strong>${escapeHtml(value)}</strong></div>`
    )).join('');
    elements['font-meta'].classList.remove('hidden');
    showWarnings(elements.warnings, analysis.warnings);

    const outlineRange = outlineScaleRange(analysis);
    const outlineSupported = supportsOutlineScale(analysis);
    configureRange(elements['size-range'], elements['size-value'], elements['size-output'], outlineRange, formatOutlineScale, outlineSupported);
    renderOutlineStandard(analysis);
    renderFontNaming(analysis);
    configureRange(elements['spacing-range'], elements['spacing-value'], elements['spacing-output'], analysis.ranges.spacingEmPercent, (v) => `${v > 0 ? '+' : ''}${v}% EM`, analysis.capabilities.spacing);
    configureRange(elements['line-range'], elements['line-value'], elements['line-output'], analysis.ranges.lineHeightPercent, (v) => `${v}%`, analysis.capabilities.lineHeight);

    const weight = analysis.ranges.weight;
    if (weight && analysis.capabilities.weight) {
      configureRange(elements['weight-range'], elements['weight-value'], elements['weight-output'], weight, (v) => String(v), true);
      elements['weight-hint'].textContent = analysis.capabilities.weightMode === 'variable'
        ? `默认 ${weight.default}；可直接输入数值，变量字体仍遵循自身设计轴。`
        : `默认 ${weight.default}；可直接输入更大的数值，静态字体暂不支持减细。`;
    } else {
      elements['weight-range'].disabled = true;
      elements['weight-value'].disabled = true;
      elements['weight-output'].textContent = '静态字体不支持';
      elements['weight-hint'].textContent = '当前字体不支持粗细调整。';
    }
    renderPaletteControls(analysis);

    elements['size-hint'].textContent = outlineSupported
      ? '水平与垂直使用同一比例，100% 为原始轮廓；可直接输入其它比例。'
      : '当前字体不支持轮廓缩放。';
    elements['spacing-hint'].textContent = '0 为原始间距；可直接输入正数或负数。';
    elements['line-hint'].textContent = '100% 为原始行高；可直接输入其它比例。';
    elements['controls-card'].classList.remove('hidden');
    elements['preview-card'].classList.remove('hidden');
    elements['action-card'].classList.remove('hidden');
    setProgress('可以开始处理', 0);
    syncRuntimeDependentControls();
    updatePreview();
  }

  async function loadPreviewFont(file, output = false, isCurrent = () => true) {
    try {
      const family = output ? `AdjustedPreview_${Date.now()}` : `OriginalPreview_${Date.now()}`;
      const face = new FontFace(family, await file.arrayBuffer());
      await face.load();
      // FontFace.load 也可能持续数秒。选择已变化时丢弃旧 face，不能让旧字体在
      // 新分析结果之后重新占用预览区域。
      if (!isCurrent()) return;
      document.fonts.add(face);
      if (output) {
        if (state.outputFontFace) document.fonts.delete(state.outputFontFace);
        state.outputFontFace = face;
        state.outputFontFamily = family;
        elements['preview-adjusted'].style.fontFamily = `"${family}"`;
        elements['preview-adjusted'].style.fontSize = '31px';
        elements['preview-adjusted'].style.letterSpacing = 'normal';
        elements['preview-adjusted'].style.lineHeight = '1.25';
        elements['preview-adjusted'].style.fontVariationSettings = 'normal';
        // 成品字体的轮廓已经真实改变，必须移除参数阶段的描边/合成效果，否则会
        // 出现“实际加粗一次、预览又加粗一次”的双重显示。
        clearSyntheticWeightPreview();
        elements['preview-adjusted'].style.fontWeight = 'normal';
        elements['adjusted-preview-label'].textContent = '真实输出字体';
        updatePalettePreview();
      } else {
        if (state.originalFontFace) document.fonts.delete(state.originalFontFace);
        state.originalFontFace = face;
        state.originalFontFamily = family;
        elements['preview-original'].style.fontFamily = `"${family}"`;
        updatePreview();
      }
    } catch (error) {
      console.warn('字体预览加载失败:', error);
    }
  }

  function updatePreview() {
    const text = elements['preview-input'].value || 'Hello 你好 1234 世界！';
    elements['preview-original'].textContent = text;
    elements['preview-adjusted'].textContent = text;
    updatePalettePreview();
    if (state.outputFontFace) return;
    const outlineScale = parameterValue('size-value', 100);
    const spacing = parameterValue('spacing-value', 0);
    const line = parameterValue('line-value', 100);
    const weight = parameterValue('weight-value', 400);
    const weightMode = state.analysis?.capabilities?.weightMode || 'none';
    elements['preview-adjusted'].style.fontFamily = elements['preview-original'].style.fontFamily || 'inherit';
    // 浏览器参数预览使用同一个值模拟宽高等比缩放；成品生成后会立即改用真实
    // 输出字体，因此这里不会把非等比 CSS 变形误当作最终轮廓结果。
    elements['preview-adjusted'].style.fontSize = `${PREVIEW_FONT_SIZE_PX * outlineScale / 100}px`;
    elements['preview-adjusted'].style.letterSpacing = `${spacing / 100}em`;
    elements['preview-adjusted'].style.lineHeight = String(1.25 * line / 100);
    elements['preview-adjusted'].style.fontVariationSettings = weightMode === 'variable' ? `"wght" ${weight}` : 'normal';
    applySyntheticWeightPreview(weightMode, weight, outlineScale);
  }

  function resetParameters() {
    if (!state.analysis) return;
    setParameterValue('size-range', 'size-value', outlineScaleRange(state.analysis).default, true);
    for (const [rangeId, valueId, rangeName] of [
      ['spacing-range', 'spacing-value', 'spacingEmPercent'],
      ['line-range', 'line-value', 'lineHeightPercent'],
    ]) setParameterValue(rangeId, valueId, state.analysis.ranges[rangeName].default, true);
    if (state.analysis.ranges.weight) {
      setParameterValue('weight-range', 'weight-value', state.analysis.ranges.weight.default, true);
    }
    renderPaletteControls(state.analysis);
    renderFontNaming(state.analysis);
    syncRangeLabels();
    revokeOutput();
    updatePreview();
  }

  function syncRangeLabels() {
    const outlineScale = numericInputValue('size-value');
    const spacing = numericInputValue('spacing-value');
    const line = numericInputValue('line-value');
    const weight = numericInputValue('weight-value');
    elements['size-output'].textContent = Number.isFinite(outlineScale) ? formatOutlineScale(outlineScale) : '请输入数值';
    elements['spacing-output'].textContent = Number.isFinite(spacing) ? `${spacing > 0 ? '+' : ''}${spacing}% EM` : '请输入数值';
    elements['line-output'].textContent = Number.isFinite(line) ? `${line}%` : '请输入数值';
    if (!elements['weight-value'].disabled) {
      elements['weight-output'].textContent = Number.isFinite(weight) ? String(weight) : '请输入数值';
    }
    syncOutlineStandardButton();
  }

  async function selectFont(file) {
    if (!state.launchAuthorized || !state.workerReady) return;
    if (!file) return;
    // File 对象已由事件参数持有，立即清空 input 值，允许解析失败后再次选择同一路径
    // 重试；浏览器默认不会为“连续选择同一个文件”再次触发 change。
    elements['font-file'].value = '';
    // 任何一次新的选择都代表旧分析已失效，包括扩展名错误或空文件。版本号必须
    // 在前置校验之前递增，否则旧大字体的异步分析仍可能在错误提示之后重新出现。
    const selectionVersion = ++state.selectionVersion;
    if (state.markerPending.size > 0) {
      // 标记扫描是单线程顺序任务。用户重新选择后若继续等待旧大字体扫描，新字体请求
      // 会排在其后面，既浪费内存也造成明显卡顿；直接关闭旧实例可立即开始新检查。
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
    syncRuntimeDependentControls();
    state.paletteData = null;
    state.activePaletteIndex = 0;
    state.paletteGrouping = null;
    state.colorScope = 'unmodified';
    state.unmodifiedUniformColors = null;
    state.initialUnmodifiedUniformColors = null;
    state.targetCharacters = '';
    state.targetUniformColors = null;
    state.initialTargetUniformColors = null;
    if (elements['target-characters-input']) elements['target-characters-input'].value = '';
    if (elements['target-color-control']) elements['target-color-control'].classList.add('hidden');
    // 新选择一开始就清空旧 metadata。即使新文件随后扩展名错误、检查失败或用户再次
    // 切换文件，也不能让上一个字体的归属信息留到新的处理任务中。
    state.sourceMarkerMetadata = null;
    revokeOutput();
    revokeOriginalPreview();
    elements['picker-title'].textContent = file.name;
    elements['picker-subtitle'].textContent = `${formatBytes(file.size)} · ${
      state.workerReady ? '正在读取字体' : '正在初始化字体工具'
    }`;
    elements['controls-card'].classList.add('hidden');
    elements['preview-card'].classList.add('hidden');
    elements['action-card'].classList.add('hidden');
    elements['font-meta'].classList.add('hidden');
    showWarnings(elements.warnings, []);
    try {
      const selectedBytes = await file.arrayBuffer();
      if (selectionVersion !== state.selectionVersion) return;

      let inspected;
      try {
        inspected = await inspectFontMarker(selectedBytes, file.name);
      } catch (error) {
        // 重新选择字体会主动终止上一份文件的扫描。旧任务只需静默退出，不能把正常
        // 的取消行为记录成错误，也不能覆盖新文件正在显示的状态。
        if (selectionVersion !== state.selectionVersion) return;
        console.error('[font-adjustment] 字体内部数据检查失败:', error);
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

      // 检查线程已经把同一个 ArrayBuffer 交还；继续 transfer 给字体分析线程，避免
      // 为中文大字体再分配一份等大的常驻副本。
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
      // 用户可能在大字体分析期间重新选择文件。旧 Worker 请求无法撤回，但其
      // 结果绝不能覆盖新文件的状态、预览或导出参数。
      if (selectionVersion !== state.selectionVersion) return;
      state.sourceMarkerMetadata = markerMetadata;
      renderAnalysis(response.result);
      elements['picker-subtitle'].textContent = `${formatBytes(file.size)} · ${response.result.outlineKind}`;
      setRuntimeStatus('字体读取完成', 'ready');
      // 裸 CFF1 不是 SFNT，浏览器 FontFace 不能直接加载原始字节。Worker 只在该
      // 输入类型下返回复用同一 Python 处理核心生成的无参数 OTTO 预览；正式处理和
      // marker 链路仍继续从原始 File 读取，避免把预览转换结果当成交付输入。
      const previewSource = response.result.sourceContainer === 'bare-cff1'
        && response.preview instanceof ArrayBuffer
        ? new Blob([response.preview], { type: 'font/otf' })
        : file;
      await loadPreviewFont(previewSource, false, () => selectionVersion === state.selectionVersion);
    } catch (error) {
      if (selectionVersion !== state.selectionVersion) return;
      state.file = null;
      state.sourceMarkerMetadata = null;
      syncRuntimeDependentControls();
      setRuntimeStatus(error.message || '字体分析失败', 'error');
      toast(error.message || '字体分析失败');
    }
  }

  function buildOutputName() {
    const extension = state.analysis?.recommendedExtension === '.otf' ? '.otf' : '.ttf';
    return normalizeFontOutputFileName(
      elements['output-file-name-input'].value,
      extension,
      createDefaultOutputName(),
    );
  }

  function setBusyDisabled(element, busy) {
    if (!(element instanceof HTMLInputElement || element instanceof HTMLSelectElement || element instanceof HTMLButtonElement)) {
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
    // 参数在 File.arrayBuffer() 和 Worker 处理期间都必须保持冻结。否则用户在读取大
    // 字体时继续拖动滑块或改色，页面会显示新值，但本次成品可能已经使用旧参数。
    // dataset 记录每个控件原本是否禁用，结束后可精确恢复“不支持”的控件状态。
    const parameterControls = [
      elements['font-family-input'], elements['output-file-name-input'], elements['font-naming-sync-input'],
      elements['reset-button'],
      elements['outline-standard-button'], elements['size-range'], elements['size-value'],
      elements['spacing-range'], elements['spacing-value'], elements['line-range'], elements['line-value'],
      elements['weight-range'], elements['weight-value'], elements['color-scope-select'],
      elements['palette-select'], ...elements['palette-list'].querySelectorAll('input'),
      elements['target-characters-input'], elements['target-color-input'], elements['target-hex-input'],
    ];
    for (const control of parameterControls) setBusyDisabled(control, busy);
    if (!busy) {
      syncOutlineStandardButton();
      // setBusyDisabled 会恢复参数控件原状态；文件选择和处理按钮还需重新结合
      // Worker 就绪状态计算，不能在备用运行时仍初始化时被无条件解锁。
      syncRuntimeDependentControls();
    }
    // “取消”只用于终止 Worker 字体处理。文件已经开始 postMessage 后无法撤回
    // 微信缓存的消息，若此时仍允许取消，会出现旧结果继续回传、新任务又被启动的并发状态。
    elements['cancel-button'].classList.toggle('hidden', !busy || !allowCancel);
  }

  async function processFont() {
    if (!state.launchAuthorized) return;
    if (!state.workerReady || !state.file || !state.analysis || state.busy) return;
    const processingVersion = ++state.processingVersion;
    revokeOutput();
    setBusy(true);
    setProgress('正在读取字体…', 10);
    try {
      const outputName = buildOutputName();
      elements['output-file-name-input'].value = outputName;
      const familyName = state.fontFamilyDirty
        ? normalizeFontFamilyName(elements['font-family-input'].value)
        : null;
      if (familyName !== null) elements['font-family-input'].value = familyName;
      const bytes = await state.file.arrayBuffer();
      // “取消”可能发生在大文件仍由浏览器读取、尚未产生 Worker pending 请求时。
      // 仅清空 pending 无法覆盖这段窗口，因此每个 await 后都要核对任务版本。
      if (processingVersion !== state.processingVersion) return;
      const outlineScalePercent = requiredParameterValue('size-value', '轮廓比例');
      const colorOptions = collectColorOptions();
      const options = {
        outlineScalePercent,
        // 同时发送旧字段，确保新版页面与仍在缓存中的旧 Worker/Python 核心组合时
        // 不会静默回退到 100%。服务端不接收这些参数，因此不存在接口重复语义。
        sizePercent: outlineScalePercent,
        spacingEmPercent: requiredParameterValue('spacing-value', '字间距'),
        lineHeightPercent: requiredParameterValue('line-value', '行高'),
        weightValue: state.analysis.capabilities.weight
          ? requiredParameterValue('weight-value', '字体粗细')
          : null,
        paletteData: colorOptions.paletteData,
        colorGroupData: colorOptions.colorGroupData,
        familyName,
      };
      const response = await callWorker('process', { filename: state.file.name, bytes, options }, [bytes], (progress) => {
        if (processingVersion !== state.processingVersion) return;
        const percent = progress.stage === 'verify' ? 82 : 38;
        setProgress(progress.message || '正在处理字体…', percent);
      });
      if (processingVersion !== state.processingVersion) return;
      let output = response.output;
      let report = response.result;
      let finalOutputSha256 = report.verification && report.verification.outputSha256;
      let finalTableCount = report.verification && report.verification.tableCount;

      // 标准 TTF/OTF 无参数时 Python 核心会逐字节复制输入，此时原标记天然保持不变，
      // 不应为了“恢复”而重新编译字体。裸 CFF1 即使参数不变也必然先封装成 OTTO，
      // 其字节和表结构已经变化，原 metadata 不能假定仍可恢复，所以仍需在成品上重写；
      // 未标记字体则完全跳过，保证不会被自动添加标记。
      if (
        state.sourceMarkerMetadata
        && (report.changed !== false || report.sourceContainer === 'bare-cff1')
      ) {
        setProgress('正在生成字体文件…', 88);
        try {
          const restored = await restoreFontMarker(output, outputName, state.sourceMarkerMetadata);
          output = restored.output;
          finalOutputSha256 = restored.outputSha256;
          finalTableCount = restored.verification.table_count;
          report = { ...report, markerVerification: restored.verification };
        } catch (error) {
          // 用户点击取消时，取消函数已经完成界面复位并提升任务版本；旧任务应静默
          // 结束，不能再弹出一次“完整性失败”覆盖“处理已取消”的结果。
          if (processingVersion !== state.processingVersion) return;
          console.error('[font-adjustment] 字体成品内部数据恢复失败:', error);
          throw new Error('字体生成后的完整性校验失败，请重试');
        }
        if (processingVersion !== state.processingVersion) return;
      }

      // 标记恢复会改变最终文件的 name 表、checksum 和文件长度。Python 报告里的摘要
      // 属于恢复前的中间成品；恢复线程已在 transfer 前对最终字节计算 SHA-256，这里
      // 使用该值覆盖中间摘要，确保页面展示与实际下载、分享的文件一致。
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
      state.outputBlob = new Blob([output], { type: state.analysis.recommendedExtension === '.ttf' ? 'font/ttf' : 'font/otf' });
      setProgress('字体处理完成', 100);
      renderResult();
      await loadPreviewFont(state.outputBlob, true);
      elements['result-card'].scrollIntoView({ behavior: 'smooth', block: 'start' });
    } catch (error) {
      if (processingVersion !== state.processingVersion) return;
      setProgress(error.message || '处理失败', 0);
      toast(error.message || '字体处理失败');
    } finally {
      // 旧任务的 finally 可能晚于用户启动的新任务；不能把新任务的按钮提前解锁。
      if (processingVersion === state.processingVersion) setBusy(false);
    }
  }

  function renderResult() {
    const report = state.outputReport;
    const verification = report.verification;
    elements['result-title'].textContent = state.outputName;
    elements['result-summary'].textContent = `${formatBytes(verification.outputSize)} · SHA-256 ${verification.outputSha256.slice(0, 12)}… · 保留 ${verification.tableCount} 个字体表`;
    showWarnings(elements['result-warnings'], report.warnings);
    elements['result-card'].classList.remove('hidden');
    elements['download-button'].classList.toggle('hidden', inMiniProgram);
    elements['return-button'].classList.toggle('hidden', !inMiniProgram);
    if (inMiniProgram) {
      elements['return-button'].disabled = false;
      elements['delivery-hint'].textContent = '点击按钮返回小程序，随后可以分享字体文件。';
    } else {
      elements['delivery-hint'].textContent = '点击按钮下载处理后的字体文件。';
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
(()=>{var c=null,m=new WeakMap;function P(e){let i=window,t=i.wx?.miniProgram,r=!!(t&&typeof t.postMessage=="function"&&typeof t.navigateBack=="function"),o=i.WeixinJSBridge;if(o&&typeof o.invoke=="function")return{postMessage({data:n}){try{o.invoke("invokeMiniProgramAPI",{name:"postMessage",arg:n},()=>{})}catch(g){if(r&&t){t.postMessage({data:n});return}throw g}},navigateBack(){try{o.invoke("invokeMiniProgramAPI",{name:"navigateBack",arg:{delta:1}},()=>{})}catch(n){if(r&&t){t.navigateBack();return}throw n}}};if(r&&t)return t;throw new Error(`\u5F53\u524D\u9875\u9762\u65E0\u6CD5\u8FD4\u56DE\u5C0F\u7A0B\u5E8F\uFF0C\u8BF7\u91CD\u65B0\u8FDB\u5165${e}`)}function S(e){let i="";for(let r=0;r<e.byteLength;r+=32768)i+=String.fromCharCode(...e.subarray(r,Math.min(r+32768,e.byteLength)));return btoa(i)}function y(e){if(!e||e!==e.split(/[\\/]/u).pop()||/[\u0000-\u001f\u007f]/u.test(e))throw new Error("\u7ED3\u679C\u6587\u4EF6\u540D\u5FC5\u987B\u662F\u5B89\u5168 basename")}function w(e){return c||(c=(async()=>{let i=m.get(e.bytes);if(i){i.navigateBack();return}let t=e.maxFileSize??67108864;if(!Number.isSafeInteger(t)||t<=0)throw new Error("strict maxFileSize \u5FC5\u987B\u662F\u975E\u96F6\u6B63\u5B89\u5168\u6574\u6570");if(e.bytes.byteLength<=0||e.bytes.byteLength>t)throw new Error("\u7ED3\u679C\u6587\u4EF6\u8D85\u8FC7 strict bridge \u4E0A\u9650");y(e.fileName);let r=P(e.toolLabel),o=e.idPrefix||"tool",n=new URLSearchParams(window.location.search),g=n.get("requestId")||n.get("transferId")||`${o}_request_${Date.now()}`,b=n.get("transferId")||`${o}_${Date.now()}_${Math.random().toString(36).slice(2,10)}`,d=`${o}_file_${Date.now()}_${Math.random().toString(36).slice(2,10)}`,s=Math.max(1,Math.ceil(e.bytes.byteLength/1048576)),v=0,l=a=>{r.postMessage({data:{...a,protocolVersion:2,requestId:g,transferId:b,sequence:v++,sentAt:Date.now()}})};l({type:"filesStart",total:1,totalFiles:1}),l({type:"chunkStart",fileId:d,fileName:e.fileName,fileSize:e.bytes.byteLength,fileType:e.fileType,totalChunks:s,fileIndex:0,totalFiles:1});for(let a=0;a<s;a+=1){let f=a*1048576,u=Math.min(f+1048576,e.bytes.byteLength);l({type:"chunkData",fileId:d,chunkIndex:a,chunkBytes:u-f,totalChunks:s,fileIndex:0,totalFiles:1,data:S(e.bytes.subarray(f,u))}),e.onProgress?.(a+1,s),await new Promise(p=>setTimeout(p,0))}l({type:"chunkEnd",fileId:d,fileName:e.fileName,fileSize:e.bytes.byteLength,totalChunks:s,fileIndex:0,totalFiles:1}),l({type:"filesEnd",total:1,totalFiles:1,successCount:1,failedCount:0,failedFiles:[]}),await new Promise(a=>setTimeout(a,200)),m.set(e.bytes,r),r.navigateBack()})(),c.finally(()=>{c=null}))}function h(e,i,t){y(i);let r=e.byteOffset===0&&e.byteLength===e.buffer.byteLength?e.buffer:e.buffer.slice(e.byteOffset,e.byteOffset+e.byteLength),o=URL.createObjectURL(new Blob([r],{type:t})),n=document.createElement("a");n.href=o,n.download=i,n.rel="noopener",document.body.appendChild(n),n.click(),n.remove(),setTimeout(()=>URL.revokeObjectURL(o),1e3)}window.__MP_BRIDGE__={returnSingleFile:e=>w({toolLabel:"\u5B57\u4F53\u8C03\u6574",idPrefix:"font_adjustment",...e}),downloadFile:(e,i,t)=>h(e,i,t)};})();
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
        fileType: state.analysis.recommendedExtension === '.ttf' ? 'font/ttf' : 'font/otf',
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
    // 处理后恢复可能正在另一个线程中进行。只终止字体调整线程会让旧恢复结果继续返回，
    // 因此取消必须同时关闭两条链路，并依靠 processingVersion 丢弃已经排队的旧消息。
    failMarkerWorker(new Error('处理已取消'));
    void createWorker(state.workerRuntimeSource, state.runtimeLocalOnly);
    setBusy(false);
    setProgress('处理已取消，可以重新生成', 0);
  }

  elements['font-file'].addEventListener('change', (event) => selectFont(event.target.files && event.target.files[0]));
  elements['process-button'].addEventListener('click', processFont);
  elements['cancel-button'].addEventListener('click', cancelProcessing);
  elements['reset-button'].addEventListener('click', resetParameters);
  elements['outline-standard-button'].addEventListener('click', applyOutlineStandard);
  elements['font-family-input'].addEventListener('input', () => {
    // 手动编辑内部名称就是明确的拆分操作，不能再被下一次文件名输入覆盖。
    setFontNamingSyncEnabled(false);
    syncFontNamingState();
    revokeOutput();
  });
  elements['font-naming-sync-input'].addEventListener('change', (event) => {
    setFontNamingSyncEnabled(event.currentTarget.checked);
    if (state.fontNamingSyncEnabled) syncFamilyNameFromOutputFileName();
    syncFontNamingState();
    revokeOutput();
  });
  elements['output-file-name-input'].addEventListener('input', () => {
    try {
      syncFamilyNameFromOutputFileName();
    } catch (error) {
      elements['font-name-output'].textContent = '名称待修正';
      elements['font-name-hint'].textContent = error.message || '导出文件名称无效';
    }
    revokeOutput();
  });
  elements['output-file-name-input'].addEventListener('blur', () => {
    if (!state.analysis) return;
    try {
      elements['output-file-name-input'].value = buildOutputName();
      syncFamilyNameFromOutputFileName();
    } catch (error) {
      toast(error.message || '导出文件名称无效');
    }
  });
  for (const id of ['font-family-input', 'output-file-name-input']) {
    elements[id].addEventListener('keydown', (event) => {
      if (event.key === 'Enter' && !event.isComposing) {
        event.preventDefault();
        event.target.blur();
      }
    });
  }
  elements['download-button'].addEventListener('click', downloadOutput);
  elements['return-button'].addEventListener('click', returnToMiniProgram);
  elements['preview-input'].addEventListener('input', updatePreview);
  elements['color-scope-select'].addEventListener('change', (event) => {
    const scope = event.target.value;
    if (!state.paletteGrouping || !['unmodified', 'modified'].includes(scope)) return;
    state.colorScope = scope;
    renderActivePalette();
  });
  elements['palette-select'].addEventListener('change', (event) => {
    const index = Number(event.target.value);
    if (!Number.isInteger(index) || !state.paletteData || !state.paletteData[index]) return;
    state.activePaletteIndex = index;
    renderActivePalette();
    updatePreview();
  });
  elements['target-characters-input'].addEventListener('input', (event) => {
    state.targetCharacters = String(event.target.value || '');
    renderTargetColorControls();
    revokeOutput();
    // 指定文字输入时，把预览文案切到这些字，便于直接看到单独改色效果。
    const normalized = normalizeTargetCharacters(state.targetCharacters);
    if (normalized && elements['preview-input']) {
      elements['preview-input'].value = normalized;
      updatePreview();
    }
  });
  elements['target-characters-input'].addEventListener('blur', (event) => {
    const normalized = normalizeTargetCharacters(event.target.value || '');
    state.targetCharacters = normalized;
    event.target.value = normalized;
    renderTargetColorControls();
  });
  const applyTargetColorValue = (rawValue, fromPicker = false) => {
    if (!Array.isArray(state.targetUniformColors)) return;
    let next = String(rawValue || '');
    if (fromPicker) {
      const current = normalizeHexColor(state.targetUniformColors[state.activePaletteIndex]) || '#000000';
      const alpha = current.length === 9 ? current.slice(7) : 'FF';
      const rgb = String(rawValue || '').toUpperCase();
      next = alpha === 'FF' ? rgb : `${rgb}${alpha}`;
    }
    state.targetUniformColors[state.activePaletteIndex] = next;
    const normalized = normalizeHexColor(next);
    elements['target-hex-input'].value = next;
    elements['target-hex-input'].classList.toggle('invalid', !normalized);
    if (normalized) elements['target-color-input'].value = normalized.slice(0, 7);
    revokeOutput();
    updatePreview();
  };
  elements['target-color-input'].addEventListener('input', (event) => {
    applyTargetColorValue(event.target.value, true);
  });
  elements['target-hex-input'].addEventListener('input', (event) => {
    applyTargetColorValue(event.target.value, false);
  });
  elements['target-hex-input'].addEventListener('change', (event) => {
    const normalized = normalizeHexColor(event.target.value);
    if (!normalized) {
      event.target.classList.add('invalid');
      toast('请输入正确的十六进制颜色');
      return;
    }
    applyTargetColorValue(normalized, false);
  });
  elements['palette-list'].addEventListener('input', (event) => {
    const target = event.target;
    if (!(target instanceof HTMLInputElement)) return;
    const row = target.closest('.palette-row');
    const binding = paletteRowBinding(row);
    if (!binding) return;

    if (target.classList.contains('palette-color-input')) {
      // 原生取色器只能提供 RGB；若当前十六进制值带 alpha，则只替换 RGB 并保留
      // 原透明度，避免选择一个新颜色就意外把半透明图层变成完全不透明。
      const current = normalizeHexColor(binding.value()) || '#000000';
      const alpha = current.length === 9 ? current.slice(7) : 'FF';
      const rgb = target.value.toUpperCase();
      const next = alpha === 'FF' ? rgb : `${rgb}${alpha}`;
      binding.setValue(next);
      const hexInput = row.querySelector('.palette-hex-input');
      if (hexInput) {
        hexInput.value = next;
        hexInput.classList.remove('invalid');
      }
      revokeOutput();
      updatePreview();
      return;
    }

    if (target.classList.contains('palette-hex-input')) {
      binding.setValue(target.value);
      const normalized = normalizeHexColor(target.value);
      target.classList.toggle('invalid', !normalized);
      const colorInput = row.querySelector('.palette-color-input');
      if (normalized && colorInput) colorInput.value = normalized.slice(0, 7);
      revokeOutput();
      updatePreview();
    }
  });
  elements['palette-list'].addEventListener('change', (event) => {
    const target = event.target;
    if (!(target instanceof HTMLInputElement) || !target.classList.contains('palette-hex-input')) return;
    const row = target.closest('.palette-row');
    const binding = paletteRowBinding(row);
    if (!binding) return;
    const normalized = normalizeHexColor(target.value);
    if (!normalized) {
      target.classList.add('invalid');
      toast('请输入正确的十六进制颜色');
      return;
    }
    target.value = normalized;
    target.classList.remove('invalid');
    binding.setValue(normalized);
  });
  for (const [rangeId, valueId] of [
    ['size-range', 'size-value'],
    ['spacing-range', 'spacing-value'],
    ['line-range', 'line-value'],
    ['weight-range', 'weight-value'],
  ]) {
    elements[rangeId].addEventListener('input', () => {
      elements[valueId].value = elements[rangeId].value;
      syncRangeLabels();
      revokeOutput();
      updatePreview();
    });
    elements[rangeId].addEventListener('change', () => {
      // 拖动期间保持当前窗口稳定；松手后若已经回到推荐区间，再恢复常用窗口。
      // 直接在 input 事件中缩放窗口会让滑块端点跟着指针移动，导致拖动手感跳变。
      setParameterValue(rangeId, valueId, Number(elements[rangeId].value), true);
    });
    elements[valueId].addEventListener('input', () => {
      const value = numericInputValue(valueId);
      if (Number.isFinite(value)) setParameterValue(rangeId, valueId, value, true);
      syncRangeLabels();
      revokeOutput();
      updatePreview();
    });
    elements[valueId].addEventListener('change', () => {
      const value = numericInputValue(valueId);
      if (Number.isFinite(value)) return;
      elements[valueId].value = elements[rangeId].value;
      syncRangeLabels();
      // input 事件在空值阶段会用参数默认值更新预览；这里恢复的是滑块当前值，
      // 两者可能不同。必须再刷新一次，否则数值框和标签已经恢复，预览却仍停留
      // 在默认参数，直到用户下一次操作才纠正。
      revokeOutput();
      updatePreview();
      toast('请输入有效数字');
    });
  }

  if (inMiniProgram) {
    elements['picker-subtitle'].textContent = '支持 TTF、OTF、裸 CFF1 字体文件';
  }
  window.addEventListener('pagehide', () => {
    // 页面离开后不再接受任何旧任务结果，也及时释放竞速中的全部 Pyodide 实例。
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
    // Safari/微信 WebView 可能把页面放入 BFCache。pagehide 已释放 Pyodide，恢复时必须
    // 重建运行时，否则控件会保留旧 ready 状态却把请求发给已经终止的 Worker。
    if (event.persisted && state.launchAuthorized && !state.workerReady) {
      void createWorker(state.workerRuntimeSource, state.runtimeLocalOnly);
    }
  });
  async function startAuthorizedFontAdjustment() {
    elements['font-file'].disabled = true;
    setRuntimeStatus('正在验证启动权限…', 'loading');
    try {
      const gate = globalThis.JuneOver24ToolLaunchGate;
      if (!gate || typeof gate.verify !== 'function') {
        throw new Error('启动权限模块加载失败，请返回小程序重试');
      }
      const launchResult = await gate.verify('font_adjustment');
      if (launchResult.mode === 'offline') {
        elements['launch-notice'].textContent = launchResult.notice
          || '当前服务器无法访问，字体调整以本地应急模式运行。';
        elements['launch-notice'].classList.remove('hidden');
      } else {
        elements['launch-notice'].textContent = '';
        elements['launch-notice'].classList.add('hidden');
      }
      state.launchAuthorized = true;
      // 先并发做轻量 HEAD 探测，再按响应速度启动第一名；4 秒仍未 ready 才启动第二名。
      // 最多保留两个重型 Pyodide 实例，最快者胜出并立即回收其余线程。
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

  void startAuthorizedFontAdjustment();
})();
