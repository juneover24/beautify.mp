"""浏览器本地字体压缩核心。

该文件由 Pyodide 在 Web Worker 内执行。输入、输出都位于浏览器内存文件系统，
不会通过 HTTP 上传字体内容。算法对齐本机 pyftsubset：
  --unicodes-file / --no-hinting / --desubroutinize / --no-layout-closure
  默认不保留 GID（去掉 --retain-gids）。

SVG 表内嵌 PNG 降采样由 JS 侧 Canvas 完成后再把替换后的 XML 字符串回传；
本模块只负责把改写后的 SVG 文档写回字体表，避免在浏览器默认加载 Pillow。
"""

from __future__ import annotations

import hashlib
import importlib.util
import io
import json
import os
import re
import sys
from typing import Any, Dict, List, Optional, Sequence, Set

from fontTools import subset
from fontTools.ttLib import TTFont, TTLibError


# Pyodide 通过 exec 先注入同目录的 bare_cff_adapter.py，而本地测试则按文件路径
# 加载本处理器。两条路径都只复用同一份 adapter，不在压缩器内复制 CFF1 解析逻辑。
try:
    from bare_cff_adapter import BareCffError, is_bare_cff1, normalize_bare_cff1
except ImportError:
    _adapter_scope = globals()
    BareCffError = _adapter_scope.get("BareCffError")
    is_bare_cff1 = _adapter_scope.get("is_bare_cff1")
    normalize_bare_cff1 = _adapter_scope.get("normalize_bare_cff1")
    if not callable(is_bare_cff1) or not callable(normalize_bare_cff1):
        try:
            _adapter_path = os.path.join(os.path.dirname(__file__), "bare_cff_adapter.py")
            _adapter_spec = importlib.util.spec_from_file_location(
                "font_compress_bare_cff_adapter", _adapter_path
            )
            if _adapter_spec is None or _adapter_spec.loader is None:
                raise ImportError("裸 CFF 适配器模块不可加载")
            _adapter_module = importlib.util.module_from_spec(_adapter_spec)
            # dataclass 在执行 adapter 模块时会通过 sys.modules 查找 __module__；
            # 直接按文件路径加载时也必须先注册临时模块名。
            sys.modules[_adapter_spec.name] = _adapter_module
            _adapter_spec.loader.exec_module(_adapter_module)
            BareCffError = _adapter_module.BareCffError
            is_bare_cff1 = _adapter_module.is_bare_cff1
            normalize_bare_cff1 = _adapter_module.normalize_bare_cff1
        except Exception:
            BareCffError = None
            is_bare_cff1 = None
            normalize_bare_cff1 = None


PROCESSOR_VERSION = "1.0.0"
SUPPORTED_SIGNATURES = {b"\x00\x01\x00\x00": "TTF", b"OTTO": "OTF"}
# 与字体调整保持一致：这些容器不在本工具支持范围。
UNSUPPORTED_SIGNATURES = {
    b"ttcf": "TTC 字体合集暂不支持，请先拆成单字体",
    b"wOFF": "WOFF 暂不支持，请使用 TTF 或 OTF",
    b"wOF2": "WOFF2 暂不支持，请使用 TTF 或 OTF",
}
MAX_INPUT_BYTES = 50 * 1024 * 1024
# 移动端 WebView 上 20MB+ 的彩色/SVG 字体内存压力显著升高；超过该值只做警告，
# 超过硬上限才拒绝，避免用户误以为工具“卡死”。
SOFT_INPUT_WARNING_BYTES = 20 * 1024 * 1024
TABLE_SIZE_KEYS = (
    "glyf", "CFF ", "CFF2", "SVG ", "loca", "hmtx", "vmtx",
    "cmap", "GSUB", "GPOS", "COLR", "CPAL", "post", "name",
)


def _error(message: str) -> str:
    return json.dumps({"ok": False, "error": message}, ensure_ascii=False)


def _sha256_file(path: str) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as handle:
        while True:
            chunk = handle.read(1024 * 1024)
            if not chunk:
                break
            digest.update(chunk)
    return digest.hexdigest()


def _read_file_bytes(path: str) -> bytes:
    with open(path, "rb") as handle:
        return handle.read()


def _detect_container(path: str) -> str:
    with open(path, "rb") as handle:
        signature = handle.read(4)
    if signature in UNSUPPORTED_SIGNATURES:
        raise ValueError(UNSUPPORTED_SIGNATURES[signature])
    container = SUPPORTED_SIGNATURES.get(signature)
    if container:
        return container
    # 扩展名不是格式事实；只有真实符合 CFF1 header 的二进制才进入 adapter。
    # 具体 INDEX/DICT/CharString 结构由 normalize_bare_cff1 再次严格校验。
    if callable(is_bare_cff1) and is_bare_cff1(_read_file_bytes(path)):
        return "BARE-CFF1"
    raise ValueError("仅支持 TTF、OTF 或裸 CFF1 字体文件")


def _normalize_bare_cff_input(
    path: str,
    filename: str,
) -> tuple[io.BytesIO, Any]:
    if not callable(normalize_bare_cff1):
        raise ValueError("裸 CFF1 适配器不可用")
    try:
        normalized = normalize_bare_cff1(_read_file_bytes(path), filename)
    except BareCffError as error:
        code = getattr(error, "code", "BARE_CFF_INVALID")
        message = getattr(error, "message", str(error))
        raise ValueError(f"{code}: {message}") from error
    return io.BytesIO(normalized.content), normalized.report


def _open_font_source(
    path: str,
    filename: str,
    *,
    lazy: bool,
) -> tuple[TTFont, Optional[Any], Optional[io.BytesIO]]:
    """打开标准 SFNT 或先封装裸 CFF1；返回的 BytesIO 由调用方保持生命周期。"""
    container = _detect_container(path)
    if container == "BARE-CFF1":
        source, report = _normalize_bare_cff_input(path, filename)
        try:
            return TTFont(source, lazy=lazy), report, source
        except Exception:
            source.close()
            raise
    return TTFont(path, lazy=lazy), None, None


def _table_lengths(font: TTFont) -> Dict[str, int]:
    lengths: Dict[str, int] = {}
    reader = getattr(font, "reader", None)
    tables = getattr(reader, "tables", None) if reader is not None else None
    if not isinstance(tables, dict):
        return lengths
    for tag, entry in tables.items():
        if tag == "GlyphOrder":
            continue
        try:
            lengths[str(tag)] = int(getattr(entry, "length", 0) or 0)
        except Exception:
            continue
    return lengths


def _summarize_tables(lengths: Dict[str, int]) -> Dict[str, int]:
    summary: Dict[str, int] = {}
    for key in TABLE_SIZE_KEYS:
        if key in lengths:
            summary[key.strip() or key] = lengths[key]
    # 其它表合并为 residual，便于 UI 展示“大头在哪”。
    known = set(TABLE_SIZE_KEYS)
    residual = sum(size for tag, size in lengths.items() if tag not in known)
    if residual:
        summary["other"] = residual
    summary["total"] = sum(lengths.values())
    return summary


def _best_cmap(font: TTFont) -> Dict[int, str]:
    try:
        cmap = font.getBestCmap() or {}
    except Exception:
        cmap = {}
    return {int(code): str(name) for code, name in cmap.items()}


def _svg_image_count(font: TTFont) -> int:
    if "SVG " not in font:
        return 0
    try:
        docs = font["SVG "].docList or []
    except Exception:
        return 0
    count = 0
    pattern = re.compile(r"data:image/[^;]+;base64,[A-Za-z0-9+/=]+")
    for doc in docs:
        data = getattr(doc, "data", None)
        if data is None:
            continue
        text = data if isinstance(data, str) else data.decode("utf-8", errors="replace")
        count += len(pattern.findall(text))
    return count


def _recommended_extension(container: str) -> str:
    return ".otf" if container in {"OTF", "BARE-CFF1"} else ".ttf"


def _output_container(container: str) -> str:
    return "OTF" if container in {"OTF", "BARE-CFF1"} else "TTF"


def _bare_cff_warnings(report: Optional[Any]) -> List[str]:
    if report is None:
        return []
    warnings = [str(item) for item in getattr(report, "warnings", []) if item]
    if getattr(report, "cmap", None) != "reliable":
        warnings.append(
            "裸 CFF1 没有可靠的 cmap，压缩时将保留全部 glyph，不执行按码点删减"
        )
    return warnings


def _append_warning(warnings: List[str], warning: str) -> None:
    """按稳定顺序加入 warning，避免 adapter 与处理阶段重复展示同一代码。"""
    if warning and warning not in warnings:
        warnings.append(warning)


def analyze_font_file_json(input_path: str, filename: str = "font") -> str:
    """分析字体体积结构与压缩能力，供 UI 展示字集命中预估所需基础信息。"""
    try:
        with open(input_path, "rb") as handle:
            raw_size = handle.seek(0, 2)
        if raw_size <= 0:
            return _error("字体文件内容为空")
        if raw_size > MAX_INPUT_BYTES:
            return _error(f"字体超过 {MAX_INPUT_BYTES // (1024 * 1024)}MB 上限，请在电脑端处理")

        container = _detect_container(input_path)
        font, bare_report, source_handle = _open_font_source(
            input_path, filename, lazy=True
        )
        try:
            cmap = _best_cmap(font)
            glyph_count = len(font.getGlyphOrder())
            table_lengths = _table_lengths(font)
            table_summary = _summarize_tables(table_lengths)
            has_svg = "SVG " in font
            svg_image_count = _svg_image_count(font) if has_svg else 0
            has_cff = "CFF " in font or "CFF2" in font
            warnings: List[str] = _bare_cff_warnings(bare_report)
            if raw_size >= SOFT_INPUT_WARNING_BYTES:
                _append_warning(
                    warnings,
                    f"当前字体约 {raw_size / (1024 * 1024):.1f}MB，移动端处理可能较慢或内存不足",
                )
            if has_svg and svg_image_count > 0:
                _append_warning(
                    warnings,
                    f"检测到 SVG 表内嵌 {svg_image_count} 张位图，可在压缩时降低分辨率",
                )
            elif has_svg:
                _append_warning(warnings, "检测到 SVG 表；若为矢量路径，降分辨率选项不会生效")

            output_container = _output_container(container)
            result = {
                "ok": True,
                "processorVersion": PROCESSOR_VERSION,
                "filename": filename,
                # container 保持输入语义；裸流明确标为 BARE-CFF1，不伪装成普通 OTF。
                "container": container,
                "sourceContainer": "bare-cff1" if container == "BARE-CFF1" else container,
                "outputContainer": output_container,
                "recommendedExtension": _recommended_extension(container),
                "inputSize": int(raw_size),
                "numGlyphs": int(glyph_count),
                "numCodepoints": len(cmap),
                "codepoints": sorted(cmap.keys()),
                "tableSizes": table_summary,
                "capabilities": {
                    "subset": bare_report is None or getattr(bare_report, "cmap", None) == "reliable",
                    "cmapReliable": bare_report is None or getattr(bare_report, "cmap", None) == "reliable",
                    "preservesAllGlyphs": bool(
                        bare_report is not None
                        and getattr(bare_report, "cmap", None) != "reliable"
                    ),
                    "noHinting": True,
                    "desubroutinize": bool(has_cff),
                    "layoutClosure": True,
                    "retainGids": True,
                    "svgImageDownsample": bool(svg_image_count > 0),
                    "svgImageCount": int(svg_image_count),
                },
                "warnings": warnings,
            }
            return json.dumps(result, ensure_ascii=False)
        finally:
            font.close()
            if source_handle is not None:
                source_handle.close()
    except Exception as error:
        return _error(str(error) or "字体分析失败")


def _normalize_unicodes(raw: Any) -> List[int]:
    if not isinstance(raw, list) or not raw:
        raise ValueError("请至少选择一个字集或输入自定义文字")
    codes: Set[int] = set()
    for item in raw:
        try:
            value = int(item)
        except Exception as error:
            raise ValueError("字集码点无效") from error
        if value < 0 or value > 0x10FFFF:
            raise ValueError(f"字集码点越界: {value}")
        codes.add(value)
    return sorted(codes)


def _apply_svg_documents(font: TTFont, documents: Sequence[Dict[str, Any]]) -> int:
    """把 JS 侧改写后的 SVG 文档写回字体。

    documents 项：
      {
        "startGlyphID": int,
        "endGlyphID": int,
        "data": str,          # 完整 SVG XML
        "compressed": bool    # 是否压缩存储（本工具统一写未压缩字符串）
      }
    """
    if "SVG " not in font or not documents:
        return 0
    svg_table = font["SVG "]
    original = list(svg_table.docList or [])
    if not original:
        return 0

    # fontTools SVGDocument 是 namedtuple-like；按位置或字段构造均可。
    try:
        from fontTools.ttLib.tables.S_V_G_ import SVGDocument
    except Exception:
        SVGDocument = None  # type: ignore

    rewritten = 0
    by_range = {
        (int(item.get("startGlyphID")), int(item.get("endGlyphID"))): item
        for item in documents
        if isinstance(item, dict)
    }
    new_docs = []
    for doc in original:
        start = int(getattr(doc, "startGlyphID", -1))
        end = int(getattr(doc, "endGlyphID", -1))
        replacement = by_range.get((start, end))
        if not replacement:
            new_docs.append(doc)
            continue
        data = replacement.get("data")
        if not isinstance(data, str) or not data:
            new_docs.append(doc)
            continue
        if SVGDocument is not None:
            new_docs.append(SVGDocument(data, start, end, False))
        else:
            # 兼容旧 wheel：直接复用原对象字段。
            try:
                doc.data = data
                doc.compressed = False
            except Exception:
                pass
            new_docs.append(doc)
        rewritten += 1
    svg_table.docList = new_docs
    return rewritten


def extract_svg_documents_json(input_path: str) -> str:
    """提取 SVG 表全部文档供 JS 侧 Canvas 降采样后回写。

    SVG 表里是带 `<image>` 标签内嵌 base64 PNG 的 XML 文档。JS 拿到完整
    文档字符串后对位图降采样并替换 base64，再通过 process 的 svgDocuments
    传回本模块写表。这里只做提取，不解析图片内容。
    """
    source_handle = None
    try:
        font, _bare_report, source_handle = _open_font_source(
            input_path, "font", lazy=False
        )
        try:
            if "SVG " not in font:
                return json.dumps({"ok": True, "documents": []}, ensure_ascii=False)
            items = []
            for doc in font["SVG "].docList or []:
                data = getattr(doc, "data", None)
                if data is None:
                    text = ""
                elif isinstance(data, str):
                    text = data
                else:
                    text = data.decode("utf-8", errors="replace")
                items.append({
                    "startGlyphID": int(getattr(doc, "startGlyphID", 0)),
                    "endGlyphID": int(getattr(doc, "endGlyphID", 0)),
                    "data": text,
                })
            return json.dumps({"ok": True, "documents": items}, ensure_ascii=False)
        finally:
            font.close()
    except Exception as error:
        return _error(str(error) or "SVG 文档提取失败")
    finally:
        if source_handle is not None:
            source_handle.close()


def _build_subset_options(
    *,
    no_hinting: bool,
    desubroutinize: bool,
    layout_closure: bool,
    retain_gids: bool,
) -> subset.Options:
    options = subset.Options()
    # 与本机 pyftsubset 默认产品策略对齐：去掉 hint 利于减体积；CFF 去子程序
    # 提升兼容性；默认不做 layout closure，确保“用户勾选的码点”就是最终保留集。
    options.hinting = not bool(no_hinting)
    options.desubroutinize = bool(desubroutinize)
    options.layout_closure = bool(layout_closure)
    options.retain_gids = bool(retain_gids)
    # 不保留空字形以外的冗余；name 表保留基础记录即可。
    options.name_IDs = ["*"]
    options.name_legacy = True
    options.name_languages = ["*"]
    options.notdef_outline = True
    options.recalc_bounds = True
    options.recalc_timestamp = False
    return options


def process_font_file_json(input_path: str, output_path: str, options_json: str) -> str:
    """按字集子集化字体，并可选写回 JS 侧降采样后的 SVG 文档。"""
    try:
        options = json.loads(options_json or "{}")
        if not isinstance(options, dict):
            return _error("压缩参数无效")

        with open(input_path, "rb") as handle:
            raw_size = handle.seek(0, 2)
        if raw_size <= 0:
            return _error("字体文件内容为空")
        if raw_size > MAX_INPUT_BYTES:
            return _error(f"字体超过 {MAX_INPUT_BYTES // (1024 * 1024)}MB 上限，请在电脑端处理")

        container = _detect_container(input_path)
        bare_report = None
        source_handle = None
        font = None
        try:
            if container == "BARE-CFF1":
                # 适配器已经完成一次独立保存回读；这里在同一字体对象上继续走现有
                # Subsetter，避免绕过压缩选项和 SVG 写回路径。
                font, bare_report, source_handle = _open_font_source(
                    input_path, str(options.get("filename") or "font.cff"), lazy=False
                )
            else:
                font, bare_report, source_handle = _open_font_source(
                    input_path, str(options.get("filename") or "font"), lazy=False
                )

            cmap_is_reliable = (
                bare_report is None
                or getattr(bare_report, "cmap", None) == "reliable"
            )
            # 无可靠 cmap 的裸 CFF 不能将用户选择的码点当作完整保留集，否则会静默
            # 删除无法映射的 glyph。使用全部 glyph 名称作为保留集，让 Subsetter 仍可
            # 应用 noHinting/desubroutinize 等安全压缩选项，同时明确保留全部 glyph。
            if cmap_is_reliable:
                unicodes = _normalize_unicodes(options.get("unicodes"))
            else:
                raw_unicodes = options.get("unicodes")
                unicodes = _normalize_unicodes(raw_unicodes) if raw_unicodes else []

            no_hinting = bool(options.get("noHinting", True))
            desubroutinize = bool(options.get("desubroutinize", True))
            layout_closure = bool(options.get("layoutClosure", False))
            retain_gids = bool(options.get("retainGids", False))
            svg_documents = options.get("svgDocuments") or []
            if svg_documents is not None and not isinstance(svg_documents, list):
                return _error("SVG 降采样参数无效")

            input_sha = _sha256_file(input_path)
            before_tables = _summarize_tables(_table_lengths(font))
            before_glyphs = len(font.getGlyphOrder())
            before_codepoints = len(_best_cmap(font))
            had_cff = "CFF " in font or "CFF2" in font
            if not had_cff:
                # 非 CFF 字体没有 charstring 子程序可去；强制关闭避免无意义开销。
                desubroutinize = False

            subset_options = _build_subset_options(
                no_hinting=no_hinting,
                desubroutinize=desubroutinize,
                layout_closure=layout_closure,
                retain_gids=retain_gids,
            )
            subsetter = subset.Subsetter(options=subset_options)
            if cmap_is_reliable:
                subsetter.populate(unicodes=unicodes)
            else:
                subsetter.populate(glyphs=font.getGlyphOrder())
            subsetter.subset(font)

            svg_rewritten = 0
            if svg_documents:
                svg_rewritten = _apply_svg_documents(font, svg_documents)

            font.save(output_path)
        finally:
            if font is not None:
                font.close()
            if source_handle is not None:
                source_handle.close()

        # 重新打开输出做回读校验，避免写出损坏字体却被 UI 当成成功。
        verify = TTFont(output_path, lazy=True)
        try:
            after_glyphs = len(verify.getGlyphOrder())
            after_cmap = _best_cmap(verify)
            after_tables = _summarize_tables(_table_lengths(verify))
            after_container = "OTF" if ("CFF " in verify or "CFF2" in verify) else "TTF"
            expected_container = _output_container(container)
            if after_container != expected_container:
                raise ValueError("输出字体容器校验失败")
        finally:
            verify.close()

        with open(output_path, "rb") as handle:
            output_size = handle.seek(0, 2)
        output_sha = _sha256_file(output_path)

        if cmap_is_reliable:
            kept_requested = sum(1 for code in unicodes if code in after_cmap)
            missing_requested = len(unicodes) - kept_requested
        else:
            # 无可靠 cmap 时，用户选择的码点无法与 glyph 建立可信对应关系；这里的
            # unicodes 仅用于记录请求规模，不能伪造“缺失码点”或“命中码点”统计。
            kept_requested = 0
            missing_requested = 0
        warnings: List[str] = _bare_cff_warnings(bare_report)
        if missing_requested > 0:
            _append_warning(
                warnings,
                f"所选字集中有 {missing_requested} 个码点在原字体中不存在，已自动跳过",
            )
        if retain_gids:
            _append_warning(warnings, "已开启保留 GID：loca/hmtx 可能不会明显缩小")
        if layout_closure:
            _append_warning(warnings, "已开启布局闭包：可能额外保留替换/组合字形，体积会更大")
        if svg_rewritten:
            _append_warning(warnings, f"已重写 {svg_rewritten} 个 SVG 文档（位图已降分辨率）")

        changed = output_sha != input_sha
        result = {
            "ok": True,
            "processorVersion": PROCESSOR_VERSION,
            "changed": changed,
            "container": container,
            "sourceContainer": "bare-cff1" if container == "BARE-CFF1" else container,
            "outputContainer": expected_container,
            "recommendedExtension": _recommended_extension(container),
            "applied": {
                "subsetApplied": cmap_is_reliable,
                "preservesAllGlyphs": not cmap_is_reliable,
                "cmapReliable": cmap_is_reliable,
                "unicodesRequested": len(unicodes),
                "unicodesKept": kept_requested,
                "unicodesMissing": missing_requested,
                "noHinting": no_hinting,
                "desubroutinize": desubroutinize,
                "layoutClosure": layout_closure,
                "retainGids": retain_gids,
                "svgDocumentsRewritten": svg_rewritten,
                "glyphsBefore": before_glyphs,
                "glyphsAfter": after_glyphs,
                "codepointsBefore": before_codepoints,
                "codepointsAfter": len(after_cmap),
            },
            "tableSizesBefore": before_tables,
            "tableSizesAfter": after_tables,
            "verification": {
                "inputSha256": input_sha,
                "outputSha256": output_sha,
                "inputSize": int(raw_size),
                "outputSize": int(output_size),
                "tableCount": int(after_tables.get("total") and len(after_tables) or 0),
            },
            "warnings": warnings,
        }
        # tableCount 用真实 sfnt 表数量更准确。
        verify2 = TTFont(output_path, lazy=True)
        try:
            tags = [tag for tag in verify2.keys() if tag != "GlyphOrder"]
            result["verification"]["tableCount"] = len(tags)
        finally:
            verify2.close()
        return json.dumps(result, ensure_ascii=False)
    except Exception as error:
        return _error(str(error) or "字体压缩失败")
