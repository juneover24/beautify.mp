"""浏览器本地字体调整核心。

该文件由 Pyodide 在 Web Worker 内执行。输入、输出都位于浏览器内存文件系统，
不会通过 HTTP 上传字体内容。
"""

from __future__ import annotations

import hashlib
import json
import importlib.util
import math
import os
import re
import shutil
import sys
import tempfile
import unicodedata
from contextlib import contextmanager
from statistics import median
from typing import Any, Dict, Iterable, Iterator, List, Optional, Sequence, Set, Tuple

try:
    from bare_cff_adapter import BareCffError, is_bare_cff1, normalize_bare_cff1
except ModuleNotFoundError as error:
    # 独立测试会用 importlib 从文件加载处理器，此时同目录不在 sys.path；Pyodide
    # 启动则会预先注册 bare_cff_adapter，优先复用同一个模块实例和实现。
    if error.name != "bare_cff_adapter" or not globals().get("__file__"):
        raise
    _adapter_spec = importlib.util.spec_from_file_location(
        "bare_cff_adapter", os.path.join(os.path.dirname(__file__), "bare_cff_adapter.py")
    )
    if _adapter_spec is None or _adapter_spec.loader is None:
        raise ImportError("同目录裸 CFF 适配器加载失败") from error
    _adapter_module = importlib.util.module_from_spec(_adapter_spec)
    sys.modules["bare_cff_adapter"] = _adapter_module
    _adapter_spec.loader.exec_module(_adapter_module)
    BareCffError = _adapter_module.BareCffError
    is_bare_cff1 = _adapter_module.is_bare_cff1
    normalize_bare_cff1 = _adapter_module.normalize_bare_cff1
    del _adapter_spec, _adapter_module

from fontTools.misc.fixedTools import otRound
from fontTools.pens.basePen import NullPen
from fontTools.pens.boundsPen import BoundsPen
from fontTools.pens.recordingPen import RecordingPen
from fontTools.pens.t2CharStringPen import T2CharStringPen
from fontTools.ttLib import TTFont, TTLibError
from fontTools.ttLib.scaleUpem import ScalerVisitor
from fontTools.ttLib.tables.C_O_L_R_ import LayerRecord
from fontTools.ttLib.tables.C_P_A_L_ import Color
from fontTools.ttLib.tables.DefaultTable import DefaultTable
from fontTools.ttLib.tables.ttProgram import Program
from fontTools.varLib.instancer import instantiateVariableFont


PROCESSOR_VERSION = "1.9.1"
SUPPORTED_SIGNATURES = {b"\x00\x01\x00\x00": "TTF", b"OTTO": "OTF"}
BITMAP_OR_SVG_TABLES = {"CBDT", "CBLC", "EBDT", "EBLC", "sbix", "SVG "}
AAT_OR_GRAPHITE_TABLES = {
    "morx", "mort", "kerx", "ankr", "trak", "Feat", "Silf", "Glat", "Gloc"
}
VARIABLE_TABLES = {"fvar", "gvar", "HVAR", "VVAR", "MVAR", "avar", "STAT", "VARC", "cvar"}

# 修符导出时写入的字形分组表。OpenType 自定义表 tag 必须恰好为 4 个 ASCII 字符；
# 表内只保存当前字体的 glyph name，不保存用户、文件路径或其它业务信息。字体调整在
# 第一次处理历史字体时可从 COLR v0 推断已修改字形，并立即补写该表；之后即使打底字
# 也被转换成 COLR 彩色字形，仍能稳定区分两组，避免下一次打开后全部混为“已修改”。
COLOR_GROUP_TABLE_TAG = "J24C"
COLOR_GROUP_SCHEMA = "juneover24.color-groups"
COLOR_GROUP_VERSION = 1
MAX_COLOR_GROUP_TABLE_BYTES = 1024 * 1024

# TrueType 指令会引用原始轮廓点、控制值和设备像素尺寸。轮廓发生非等价变化后，
# 继续保留这些数据比删除更危险：轻则小字号抖动，重则字形在某些渲染器中错位。
# TSI* 是 Visual TrueType 的源数据表，虽然渲染时不用，但也不能与已修改的轮廓
# 一起伪装成仍可继续编辑的有效提示源码。
TRUETYPE_HINT_TABLES = {
    "cvt ", "fpgm", "prep", "hdmx", "LTSH", "VDMX",
    "TSI0", "TSI1", "TSI2", "TSI3", "TSI5", "TSIC", "TSID",
    "TSIJ", "TSIP", "TSIS", "TSIV",
}

# CSS/OpenType 字重数值并没有规定应增加多少字体单位。这里采用 FreeType
# FT_GlyphSlot_Embolden 的基准：增加 300 字重约等于增加 1/24 UPEM 的轮廓厚度。
# 因而每增加 1 字重，对应 UPEM / 7200 字体单位。
SYNTHETIC_WEIGHT_SCALE = 7200.0

# 静态字体没有设计师提供的粗细母版，过强的算法加粗会封闭字腔或挤压字距。
# 1/14 UPEM 允许常见 Regular(400) 调到约 900，同时限制 Thin 字体一次跨越到
# 极黑字重。最终上限还会继续受坐标和 advance width 的格式范围约束。
MAX_SYNTHETIC_STRENGTH_EM = 1.0 / 14.0

CFF_HINT_PRIVATE_ATTRIBUTES = (
    "BlueValues", "OtherBlues", "FamilyBlues", "FamilyOtherBlues",
    "BlueScale", "BlueShift", "BlueFuzz", "StemSnapH", "StemSnapV",
    "StdHW", "StdVW", "ForceBold", "LanguageGroup", "ExpansionFactor",
)

# 与“字体修符”的 fontNaming.ts 保持同一产品规则。这里不能直接运行 TypeScript，
# 但输入规范化、128 字符限制、FNV-1a 中文回退、Regular 样式判断以及 63 字符
# PostScript 上限必须逐项一致，并由共享测试向量防止以后两端悄悄漂移。
MAX_FAMILY_NAME_LENGTH = 128
TARGET_FONT_NAME_IDS = {
    1: "fontFamily",
    2: "fontSubFamily",
    3: "uniqueSubFamily",
    4: "fullName",
    6: "postScriptName",
    16: "preferredFamily",
    17: "preferredSubFamily",
    18: "compatibleFull",
}

# OpenType 只规定字体坐标和排版字段，并没有规定“所有字体看起来一样大”时应占
# unitsPerEm 的多少比例。因此统一标准必须明确成产品规则，不能把任意 90% 冒充行业
# 标准。以下目标值分别参考常见中文全字面、拉丁大写高度、小写 x-height 和数字高度：
# 分析时优先读取真实字形轮廓的中位高度，避免被某个带重音、装饰或异常边界的字形
# 拉偏；缩放时仍保持水平、垂直同一比例，不改变原字体的宽高设计关系。
OUTLINE_STANDARD_REFERENCE_GROUPS = (
    (
        "cjk",
        "中文常用字",
        "永国田中日目回口王正",
        ((0x3400, 0x4DBF), (0x4E00, 0x9FFF), (0xF900, 0xFAFF)),
        88.0,
        3,
        12,
    ),
    (
        "latin-cap",
        "拉丁大写字母",
        "HIOENXABCD",
        ((0x0041, 0x005A),),
        70.0,
        3,
        10,
    ),
    (
        "latin-x",
        "拉丁小写字母",
        "xnoehacems",
        ((0x0061, 0x007A),),
        50.0,
        3,
        10,
    ),
    (
        "digits",
        "数字",
        "0123456789",
        ((0x0030, 0x0039),),
        70.0,
        4,
        10,
    ),
)


class FontAdjustmentError(Exception):
    """可直接展示给用户的字体处理错误。"""


def _table_tags(font: TTFont) -> Set[str]:
    return {str(tag) for tag in font.keys() if str(tag) != "GlyphOrder"}


def _sha256_file(path: str) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as source:
        while True:
            chunk = source.read(1024 * 1024)
            if not chunk:
                break
            digest.update(chunk)
    return digest.hexdigest()


def _read_signature(path: str) -> bytes:
    with open(path, "rb") as source:
        return source.read(4)


def _read_file_bytes(path: str) -> bytes:
    try:
        with open(path, "rb") as source:
            return source.read()
    except OSError as error:
        raise FontAdjustmentError(f"字体文件无法读取：{error}") from error


def _normalize_bare_cff_path(path: str, filename: str) -> Tuple[str, str, str, List[str], int]:
    """把裸 CFF 临时封装成 OTF，并返回临时路径、容器和能力 warning。"""
    raw = _read_file_bytes(path)
    temporary_path: Optional[str] = None
    if not is_bare_cff1(raw):
        raise FontAdjustmentError("文件不是受支持的 TTF、OTF 或裸 CFF1 字体")
    try:
        normalized = normalize_bare_cff1(raw, filename)
    except BareCffError as error:
        raise FontAdjustmentError(f"裸 CFF 字体无法解析：{error.message}") from error
    try:
        handle = tempfile.NamedTemporaryFile(
            mode="wb",
            prefix="font-adjustment-bare-cff-",
            suffix=".otf",
            delete=False,
        )
        temporary_path = handle.name
        with handle:
            handle.write(normalized.content)
    except OSError as error:
        if temporary_path:
            try:
                os.remove(temporary_path)
            except OSError:
                pass
        raise FontAdjustmentError(f"裸 CFF 封装结果无法写入临时文件：{error}") from error
    return temporary_path, "OTF", "bare-cff1", list(normalized.report.warnings), len(raw)


@contextmanager
def _prepared_font_path(path: str, filename: str) -> Iterator[Tuple[str, str, str, List[str], int]]:
    """统一准备 TTFont 输入，并在任务结束后清理裸 CFF 的临时 OTF。"""
    signature = _read_signature(path)
    if signature in SUPPORTED_SIGNATURES:
        yield path, SUPPORTED_SIGNATURES[signature], SUPPORTED_SIGNATURES[signature], [], os.path.getsize(path)
        return
    if signature == b"ttcf":
        raise FontAdjustmentError("第一版暂不支持 TTC 字体集合，请先拆分为单个 TTF/OTF")
    if signature in {b"wOFF", b"wOF2"}:
        raise FontAdjustmentError("第一版暂不支持 WOFF/WOFF2，请使用原始 TTF/OTF")
    temporary_path, container, source_container, warnings, source_size = _normalize_bare_cff_path(path, filename)
    try:
        yield temporary_path, container, source_container, warnings, source_size
    finally:
        try:
            os.remove(temporary_path)
        except OSError:
            pass


def _validate_container(path: str) -> str:
    signature = _read_signature(path)
    if signature in SUPPORTED_SIGNATURES:
        return SUPPORTED_SIGNATURES[signature]
    if signature == b"ttcf":
        raise FontAdjustmentError("第一版暂不支持 TTC 字体集合，请先拆分为单个 TTF/OTF")
    if signature in {b"wOFF", b"wOF2"}:
        raise FontAdjustmentError("第一版暂不支持 WOFF/WOFF2，请使用原始 TTF/OTF")
    if is_bare_cff1(_read_file_bytes(path)):
        return "OTF"
    raise FontAdjustmentError("文件不是受支持的 TTF、OTF 或裸 CFF1 字体")


def _load_font(path: str) -> TTFont:
    _validate_container(path)
    try:
        font = TTFont(path, lazy=False, recalcBBoxes=True, recalcTimestamp=False)
    except (TTLibError, AssertionError, ValueError, OSError) as error:
        raise FontAdjustmentError(f"字体结构无法解析：{error}") from error
    if "head" not in font or "maxp" not in font or "hmtx" not in font:
        font.close()
        raise FontAdjustmentError("字体缺少 head、maxp 或 hmtx 等必要表")
    return font


def _font_outline_kind(font: TTFont) -> str:
    if "glyf" in font:
        return "TrueType glyf"
    if "CFF2" in font:
        return "OpenType CFF2"
    if "CFF " in font:
        return "OpenType CFF"
    return "未知轮廓"


def _normalize_existing_name_part(value: Any) -> str:
    text = unicodedata.normalize("NFC", str(value or ""))
    text = re.sub(r"[\x00-\x1f\x7f]", " ", text)
    return re.sub(r"\s+", " ", text).strip()


def _normalize_font_family_name(value: Any) -> str:
    """复用修符规则校验用户可理解的 Unicode 字体族名。"""
    normalized = _normalize_existing_name_part(value)
    if not normalized:
        raise FontAdjustmentError("字体内部名称不能为空")
    if len(normalized) > MAX_FAMILY_NAME_LENGTH:
        raise FontAdjustmentError(
            f"字体内部名称不能超过 {MAX_FAMILY_NAME_LENGTH} 个字符"
        )
    return normalized


def _fnv1a_hex(value: str) -> str:
    """与修符 JavaScript 的 Math.imul/FNV-1a 32 位实现保持一致。"""
    result = 0x811C9DC5
    for character in value:
        result ^= ord(character)
        result = (result * 0x01000193) & 0xFFFFFFFF
    return f"{result:08x}"


def _postscript_part(value: str) -> str:
    text = unicodedata.normalize("NFKD", value)
    text = re.sub(r"[\u0300-\u036f]", "", text)
    text = re.sub(r"\s+", "-", text)
    text = re.sub(r"[^\x21-\x7e]", "-", text)
    text = re.sub(r"[\[\](){}<>/%]", "-", text)
    return re.sub(r"-+", "-", text).strip("-")


def _postscript_family_part(family_name: str) -> str:
    return _postscript_part(family_name) or f"Font-{_fnv1a_hex(family_name)}"


def _create_postscript_font_name(family_name: str, subfamily_name: str) -> str:
    """生成修符同款、仅含安全 ASCII 且不超过 63 字符的 PostScript Name。"""
    family_part = _postscript_family_part(family_name)
    subfamily_part = _postscript_part(subfamily_name)
    # 中文“常规”等会在 ASCII 化后变成空串；真正的 Regular 判定见 _is_regular_style_name，
    # 这里只决定是否把样式拼进 PostScript Name。
    include_subfamily = bool(
        subfamily_part and not _is_regular_style_name(subfamily_part)
    )
    value = f"{family_part}-{subfamily_part}" if include_subfamily else family_part
    value = value[:63].rstrip("-")
    return value or f"Font-{_fnv1a_hex(family_name)}"


def _is_regular_style_name(value: Any) -> bool:
    """判断样式名是否等价于 Regular。

    Windows 资源管理器“标题”和字体查看器主名称读的是 nameID 4（Full font name）。
    OpenType 约定 Regular/Normal/Roman/Book 时 Full name 应等于族名；大量中文字体却把
    nameID 2/17 写成“常规/标准/正常”。若只认英文，改名后 Title 会变成“新名 常规”，
    用户会认为电脑上的标题没改对。两端必须用同一组中英文 Regular 别名。
    """
    normalized = _normalize_existing_name_part(value)
    if not normalized:
        return True
    return bool(
        re.fullmatch(
            r"regular|normal|roman|book|常规|標準|标准|正常",
            normalized,
            re.IGNORECASE,
        )
    )


def _debug_name(font: TTFont, *name_ids: int) -> str:
    if "name" not in font:
        return ""
    for name_id in name_ids:
        value = font["name"].getDebugName(name_id)
        normalized = _normalize_existing_name_part(value)
        if normalized:
            return normalized
    return ""


def _font_naming_info(font: TTFont) -> Dict[str, str]:
    """读取与修符 FontNameFields 对应的常用名称，供页面预填和成品验证。"""
    family_name = _debug_name(font, 16, 1)
    subfamily_name = _debug_name(font, 17, 2) or "Regular"
    full_name = _debug_name(font, 4) or (
        family_name
        if _is_regular_style_name(subfamily_name)
        else f"{family_name} {subfamily_name}".strip()
    )
    return {
        "fontFamily": family_name,
        "fontSubFamily": subfamily_name,
        "preferredFamily": _debug_name(font, 16) or family_name,
        "preferredSubFamily": _debug_name(font, 17) or subfamily_name,
        "fullName": full_name,
        "compatibleFull": _debug_name(font, 18) or full_name,
        "postScriptName": _debug_name(font, 6),
        "uniqueSubFamily": _debug_name(font, 3),
        "wwsFamily": _debug_name(font, 21),
        "wwsSubFamily": _debug_name(font, 22),
        "manufacturer": _debug_name(font, 8),
        "version": _debug_name(font, 5),
    }


def _build_renamed_font_naming(
    current: Dict[str, str],
    requested_family_name: Any,
) -> Dict[str, str]:
    """按修符 buildRenamedFontNameFields 的字段关系生成统一名称。"""
    family_name = _normalize_font_family_name(requested_family_name)
    subfamily_name = _normalize_existing_name_part(current.get("fontSubFamily")) or "Regular"
    # Regular 等价样式：Full font name / compatibleFull 只写族名，这样 Windows
    # 资源管理器“标题”和字体查看器主名称直接等于用户设置的内部名称。
    full_name = (
        family_name
        if _is_regular_style_name(subfamily_name)
        else f"{family_name} {subfamily_name}"
    )
    postscript_name = _create_postscript_font_name(family_name, subfamily_name)
    unique_name = "; ".join(filter(None, (
        _normalize_existing_name_part(current.get("manufacturer")),
        full_name,
        _normalize_existing_name_part(current.get("version")),
    )))
    return {
        **current,
        "fontFamily": family_name,
        "fontSubFamily": subfamily_name,
        "preferredFamily": family_name,
        "preferredSubFamily": subfamily_name,
        "fullName": full_name,
        "compatibleFull": full_name,
        "postScriptName": postscript_name,
        "uniqueSubFamily": unique_name,
        "wwsFamily": family_name if current.get("wwsFamily") else "",
        "wwsSubFamily": subfamily_name if current.get("wwsSubFamily") else "",
    }


def _replace_name_value(font: TTFont, name_id: int, value: str) -> None:
    """删除目标 nameID 的旧语言副本，并写入稳定的 Unicode/Windows 记录。

    若保留旧语言记录，不同系统可能继续读取旧族名，出现“文件已改名但软件里仍显示
    原名”。Mac Roman 无法表达中文和表情，因此统一写 Unicode 平台与 Windows
    Unicode BMP/full repertoire；其它授权、版本说明和打标私有 nameID 完全保留。
    """
    name_table = font["name"]
    name_table.names = [record for record in name_table.names if record.nameID != name_id]
    for platform_id, encoding_id, language_id in (
        (0, 4, 0),
        (3, 1, 0x0409),
        (3, 1, 0x0804),
        (3, 10, 0x0409),
    ):
        name_table.setName(value, name_id, platform_id, encoding_id, language_id)


def _cff_safe_descriptive_name(value: str, fallback: str) -> str:
    """CFF String INDEX 采用 PostScript 字符串约束，Unicode 名称需使用 ASCII 回退。"""
    if value and all(0x20 <= ord(character) <= 0x7E for character in value):
        return value
    return fallback


def _apply_font_naming(font: TTFont, requested_family_name: Any) -> Dict[str, str]:
    """写回 name/CFF/变量实例名称，并返回用于成品验证的期望字段。"""
    if "name" not in font:
        raise FontAdjustmentError("字体缺少 name 表，无法修改字体名称")
    current = _font_naming_info(font)
    renamed = _build_renamed_font_naming(current, requested_family_name)
    for name_id, field_name in TARGET_FONT_NAME_IDS.items():
        _replace_name_value(font, name_id, renamed[field_name])

    # WWS Family/Subfamily（nameID 21/22）不是每个字体都声明。已有记录时必须一起
    # 替换，否则部分设计软件仍会优先显示旧族名；未声明的普通字体则保持原表结构，
    # 不为一次改名凭空新增 WWS 模型。
    for name_id, field_name in ((21, "wwsFamily"), (22, "wwsSubFamily")):
        if current.get(field_name):
            _replace_name_value(font, name_id, renamed[field_name])

    # 变量字体可以通过 nameID 25 或具名实例 postscriptNameID 暴露旧族名。只改默认
    # nameID 6 会让某些设计软件在切换实例后重新出现旧名，因此这里同步所有已声明项。
    if "fvar" in font:
        postscript_prefix = _postscript_family_part(renamed["fontFamily"])[:63].rstrip("-")
        _replace_name_value(font, 25, postscript_prefix)
        for instance in getattr(font["fvar"], "instances", []) or []:
            postscript_name_id = int(getattr(instance, "postscriptNameID", 0xFFFF))
            if postscript_name_id == 0xFFFF:
                continue
            instance_style = _debug_name(
                font,
                int(getattr(instance, "subfamilyNameID", 0)),
            ) or renamed["fontSubFamily"]
            _replace_name_value(
                font,
                postscript_name_id,
                _create_postscript_font_name(renamed["fontFamily"], instance_style),
            )

    # CFF1 还在 Name INDEX/TopDict 保存独立名称。FontName 必须是 PostScript ASCII；
    # FamilyName/FullName 遇到中文时使用同源 ASCII 回退，真正面向系统展示的 Unicode
    # 名称仍由上面的 OpenType name 表提供。CFF2 不再包含这些 CFF1 名称字段。
    if "CFF " in font:
        cff = font["CFF "].cff
        postscript_name = renamed["postScriptName"]
        family_fallback = _postscript_family_part(renamed["fontFamily"])
        full_fallback = postscript_name.replace("-", " ")
        if cff.fontNames:
            cff.fontNames[0] = postscript_name
        top_dict = cff.topDictIndex[0]
        top_dict.FamilyName = _cff_safe_descriptive_name(
            renamed["fontFamily"],
            family_fallback,
        )
        top_dict.FullName = _cff_safe_descriptive_name(
            renamed["fullName"],
            full_fallback,
        )
        renamed["cffFontName"] = postscript_name
        renamed["cffFamilyName"] = str(top_dict.FamilyName)
        renamed["cffFullName"] = str(top_dict.FullName)

    return renamed


def _color_to_hex(color: Any) -> str:
    """把 CPAL Color 规范化为前端使用的十六进制颜色。

    完全不透明时输出常见的 #RRGGBB；存在透明度时输出 #RRGGBBAA，避免颜色选择器
    修改 RGB 后无意丢失原字体设计中的 alpha。
    """
    red = max(0, min(255, int(color.red)))
    green = max(0, min(255, int(color.green)))
    blue = max(0, min(255, int(color.blue)))
    alpha = max(0, min(255, int(color.alpha)))
    rgb = f"#{red:02X}{green:02X}{blue:02X}"
    return rgb if alpha == 255 else f"{rgb}{alpha:02X}"


def _normalize_hex_color(value: Any, field_name: str) -> str:
    """接受 3/4/6/8 位十六进制颜色并统一成 #RRGGBBAA。

    CPAL 在字体内部始终保存 RGBA。前端允许用户输入常见的短格式，但进入字体核心后
    必须先严格规范化，不能依赖 Color.fromHex 对异常字符做宽松解释。
    """
    text = str(value or "").strip().upper()
    if text.startswith("#"):
        text = text[1:]
    if len(text) in {3, 4}:
        text = "".join(character * 2 for character in text)
    if len(text) == 6:
        text += "FF"
    if len(text) != 8 or any(character not in "0123456789ABCDEF" for character in text):
        raise FontAdjustmentError(
            f"{field_name}不是有效的十六进制颜色，请使用 #RRGGBB 或 #RRGGBBAA"
        )
    return f"#{text}"


def _color_palette_info(font: TTFont) -> Optional[Dict[str, Any]]:
    """返回全部 CPAL 调色板信息；没有有效调色板时返回 None。

    CPAL 规范要求所有调色板拥有相同数量的 entry。这里仍逐个验证，避免损坏字体在
    页面上显示一组无法安全写回的数据。所有调色板使用同一颜色编辑器切换修改，导出
    时再一次性写回，避免默认调色板和其它调色板走两套逻辑。
    """
    if "CPAL" not in font:
        return None
    palettes = list(getattr(font["CPAL"], "palettes", []) or [])
    if not palettes or not palettes[0]:
        return None
    entry_count = len(palettes[0])
    if any(len(palette) != entry_count for palette in palettes):
        raise FontAdjustmentError("字体 CPAL 调色板颜色数量不一致，无法安全修改")
    return {
        "paletteCount": len(palettes),
        "entryCount": entry_count,
        "palettes": [
            [_color_to_hex(color) for color in palette]
            for palette in palettes
        ],
    }


def _normalize_palette_data_option(
    value: Any,
    palette_info: Optional[Dict[str, Any]],
) -> Optional[List[List[str]]]:
    """校验页面提交的全部调色板，并返回统一的八位 RGBA 二维列表。"""
    if value is None:
        return None
    if palette_info is None:
        raise FontAdjustmentError("当前字体不包含可修改的彩色调色板")
    if not isinstance(value, list):
        raise FontAdjustmentError("彩色调色板参数格式无效")
    expected_palette_count = int(palette_info["paletteCount"])
    if len(value) != expected_palette_count:
        raise FontAdjustmentError(
            f"彩色调色板数量异常：需要 {expected_palette_count} 组，实际 {len(value)} 组"
        )
    expected_count = int(palette_info["entryCount"])
    normalized_palettes: List[List[str]] = []
    for palette_index, palette in enumerate(value):
        if not isinstance(palette, list):
            raise FontAdjustmentError(f"调色板 #{palette_index + 1} 参数格式无效")
        if len(palette) != expected_count:
            raise FontAdjustmentError(
                f"调色板 #{palette_index + 1} 颜色数量异常："
                f"需要 {expected_count} 项，实际 {len(palette)} 项"
            )
        normalized_palettes.append([
            _normalize_hex_color(
                color,
                f"调色板 #{palette_index + 1} 颜色 #{color_index + 1}",
            )
            for color_index, color in enumerate(palette)
        ])
    return normalized_palettes


def _apply_color_palettes(font: TTFont, normalized_palettes: List[List[str]]) -> int:
    """使用统一逻辑写回所有调色板，返回实际发生变化的颜色总数。"""
    palette_info = _color_palette_info(font)
    if palette_info is None:
        raise FontAdjustmentError("当前字体不包含可修改的彩色调色板")
    if len(normalized_palettes) != int(palette_info["paletteCount"]):
        raise FontAdjustmentError("彩色调色板数量与字体不一致")

    modified = 0
    for palette_index, normalized_colors in enumerate(normalized_palettes):
        palette = font["CPAL"].palettes[palette_index]
        if len(normalized_colors) != len(palette):
            raise FontAdjustmentError(f"调色板 #{palette_index + 1} 颜色数量与字体不一致")
        for color_index, normalized in enumerate(normalized_colors):
            color = Color.fromHex(normalized)
            current = palette[color_index]
            if (
                int(current.red), int(current.green), int(current.blue), int(current.alpha)
            ) != (
                int(color.red), int(color.green), int(color.blue), int(color.alpha)
            ):
                palette[color_index] = color
                modified += 1
    return modified


def _normalized_palette_data(palette_info: Dict[str, Any]) -> List[List[str]]:
    """把分析结果中的颜色统一为八位 RGBA，供变更比较和最终验证复用。"""
    return [
        [
            _normalize_hex_color(
                color,
                f"原调色板 #{palette_index + 1} 颜色 #{color_index + 1}",
            )
            for color_index, color in enumerate(palette)
        ]
        for palette_index, palette in enumerate(palette_info["palettes"])
    ]


def _read_modified_glyph_manifest(font: TTFont) -> Tuple[str, Optional[Set[str]]]:
    """读取已修改字形清单，并区分“历史缺失”和“现有数据损坏”。

    历史修符字体本来就没有 J24C，可以从当时仅包含已修改字形的 COLR 表安全推断。
    但已经经过分组调色的字体会让打底字也拥有 COLR 记录；若此时 J24C 损坏，再把
    “无效”当成“不存在”回退，就会把所有打底字误判为已修改字形。因此调用方必须
    根据 missing / valid / invalid 三种状态选择不同策略，不能静默猜测。
    """
    if COLOR_GROUP_TABLE_TAG not in font:
        return "missing", None
    raw = bytes(getattr(font[COLOR_GROUP_TABLE_TAG], "data", b"") or b"")
    if not raw or len(raw) > MAX_COLOR_GROUP_TABLE_BYTES:
        return "invalid", None
    try:
        payload = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        return "invalid", None
    if (
        not isinstance(payload, dict)
        or payload.get("schema") != COLOR_GROUP_SCHEMA
        or payload.get("version") != COLOR_GROUP_VERSION
        or not isinstance(payload.get("modifiedGlyphs"), list)
    ):
        return "invalid", None
    glyph_order = set(font.getGlyphOrder())
    raw_names = payload["modifiedGlyphs"]
    # 只要列表中出现非字符串或不存在的 glyph name，就说明清单与当前字体不再对应。
    # 不能只过滤坏项后继续处理，否则部分丢失同样会导致两组颜色被错误合并。
    if any(not isinstance(name, str) or name not in glyph_order for name in raw_names):
        return "invalid", None
    return "valid", set(raw_names)


def _write_modified_glyph_manifest(font: TTFont, glyph_names: Iterable[str]) -> None:
    """写入稳定、紧凑的字形分组表；按名称保存以避免重新编译后 glyph ID 漂移。"""
    glyph_order = set(font.getGlyphOrder())
    normalized = sorted({str(name) for name in glyph_names if str(name) in glyph_order})
    payload = json.dumps(
        {
            "schema": COLOR_GROUP_SCHEMA,
            "version": COLOR_GROUP_VERSION,
            "modifiedGlyphs": normalized,
        },
        ensure_ascii=False,
        separators=(",", ":"),
    ).encode("utf-8")
    if len(payload) > MAX_COLOR_GROUP_TABLE_BYTES:
        raise FontAdjustmentError("已修改字形清单过大，无法安全写入字体")
    table = DefaultTable(COLOR_GROUP_TABLE_TAG)
    table.data = payload
    font[COLOR_GROUP_TABLE_TAG] = table


def _colr_v0_layers(font: TTFont) -> Optional[Dict[str, List[Any]]]:
    """返回 COLR v0 的可写图层映射；v1 使用 Paint 图，不能套用 v0 重映射逻辑。"""
    if "COLR" not in font:
        return None
    colr = font["COLR"]
    if int(getattr(colr, "version", -1)) != 0:
        return None
    layers = getattr(colr, "ColorLayers", None)
    return layers if isinstance(layers, dict) else None


def _mapped_ink_glyph_names(font: TTFont) -> List[str]:
    """返回 cmap 中真正有 glyf 轮廓的字形，排除空格、控制符和图层辅助字形。"""
    if "glyf" not in font:
        return []
    glyph_order = font.getGlyphOrder()
    order_index = {name: index for index, name in enumerate(glyph_order)}
    mapped = set((font.getBestCmap() or {}).values())
    result: List[str] = []
    for glyph_name in sorted(mapped, key=lambda name: order_index.get(name, len(glyph_order))):
        if glyph_name not in font["glyf"]:
            continue
        glyph = font["glyf"][glyph_name]
        # 简单字形 numberOfContours>0，复合字形为 -1，空字形为 0。读取该字段不需要
        # 像 BoundsPen 那样逐点遍历整套中文字体，可避免分组分析再次拖慢首次读取。
        if int(getattr(glyph, "numberOfContours", 0)) != 0:
            result.append(glyph_name)
    return result


def _color_group_info(
    font: TTFont,
    palette_info: Optional[Dict[str, Any]],
) -> Optional[Dict[str, Any]]:
    """分析“已修改字形/未修改打底字”两组及各自实际引用的调色板索引。"""
    if palette_info is None or "CPAL" not in font:
        return None
    # 分组写回需要追加 palette entry。CPAL v1 还包含 entry label 等并行数组，直接
    # 追加会破坏结构；修符输出固定为 CPAL v0，因此当前只对 v0 开启分组功能。
    if int(getattr(font["CPAL"], "version", 0)) != 0:
        return None
    color_layers = _colr_v0_layers(font)
    if color_layers is None:
        return None

    manifest_state, embedded_manifest = _read_modified_glyph_manifest(font)
    if manifest_state == "invalid":
        return {
            "supported": False,
            "manifestSource": "invalid",
            "reason": (
                "字体的字形分组信息不完整，为避免已修改字形与打底字串色，"
                "已停用本次调色。请重新从字体修符导出完整字体"
            ),
        }
    if manifest_state == "missing":
        # 兼容已经导出的历史修符字体：修符只给真正修改过的彩色字形建立 COLR
        # BaseGlyphRecord，普通打底字保持单色轮廓，因此首次可从 COLR 精确推断。
        modified_names = set(color_layers.keys())
        manifest_source = "colr"
    else:
        # valid 状态保证集合存在；显式断言可防止以后扩展状态时错误落入空集合。
        assert embedded_manifest is not None
        modified_names = set(embedded_manifest)
        manifest_source = "embedded"

    mapped_ink_names = _mapped_ink_glyph_names(font)
    mapped_set = set(mapped_ink_names)
    mapped_cmap_names = set((font.getBestCmap() or {}).values())
    modified_names &= set(font.getGlyphOrder())
    unmodified_names = [name for name in mapped_ink_names if name not in modified_names]

    modified_palette_indices: Set[int] = set()
    unmodified_palette_indices: Set[int] = set()
    for glyph_name, layers in color_layers.items():
        target = modified_palette_indices if glyph_name in modified_names else unmodified_palette_indices
        if glyph_name not in modified_names and glyph_name not in mapped_set:
            # 未映射的内部辅助字形不属于用户所说的打底字，也不能因批量调色被改写。
            continue
        for layer in layers:
            color_id = int(getattr(layer, "colorID", -1))
            if 0 <= color_id < int(palette_info["entryCount"]):
                target.add(color_id)

    uniform_palette_index: Optional[int] = None
    uniform_layered = bool(unmodified_names)
    monochrome_count = 0
    for glyph_name in unmodified_names:
        layers = color_layers.get(glyph_name)
        if not layers:
            monochrome_count += 1
            uniform_layered = False
            continue
        if len(layers) != 1 or str(layers[0].name) != glyph_name:
            uniform_layered = False
            continue
        color_id = int(layers[0].colorID)
        if uniform_palette_index is None:
            uniform_palette_index = color_id
        elif uniform_palette_index != color_id:
            uniform_layered = False

    if not uniform_layered:
        uniform_palette_index = None
    palettes = palette_info["palettes"]
    uniform_colors = (
        [str(palette[uniform_palette_index]) for palette in palettes]
        if uniform_palette_index is not None
        else ["#000000" for _ in palettes]
    )
    return {
        "supported": bool(modified_names or unmodified_names),
        "manifestSource": manifest_source,
        "modifiedGlyphCount": len(modified_names & mapped_cmap_names),
        "unmodifiedGlyphCount": len(unmodified_names),
        "modifiedPaletteIndices": sorted(modified_palette_indices),
        "unmodifiedPaletteIndices": sorted(unmodified_palette_indices),
        "unmodifiedMonochromeGlyphCount": monochrome_count,
        "unmodifiedUniformLayered": uniform_layered,
        "unmodifiedUniformPaletteIndex": uniform_palette_index,
        "unmodifiedUniformColors": uniform_colors,
        # 以下名称只在 Python 核心内部写回/验证使用，返回页面前会移除，避免大字体
        # 把数万 glyph name 塞进 postMessage，增加序列化时间和移动端内存占用。
        "_modifiedGlyphNames": sorted(modified_names),
        "_unmodifiedGlyphNames": unmodified_names,
    }


def _public_color_group_info(grouping: Optional[Dict[str, Any]]) -> Optional[Dict[str, Any]]:
    if grouping is None:
        return None
    return {key: value for key, value in grouping.items() if not key.startswith("_")}


def _normalize_target_characters_option(value: Any) -> str:
    """规范化指定改色文字：保留用户输入顺序，去掉控制符，重复字只保留首次。"""
    if value is None:
        return ""
    if not isinstance(value, str):
        raise FontAdjustmentError("指定改色文字参数格式无效")
    # 只去掉控制字符，不压缩普通空格：空格本身不是可改色轮廓字，后面解析时会跳过。
    text = re.sub(r"[\x00-\x1f\x7f]", "", value)
    seen: Set[str] = set()
    ordered: List[str] = []
    for character in text:
        if character in seen:
            continue
        seen.add(character)
        ordered.append(character)
    return "".join(ordered)


def _resolve_target_color_glyphs(
    font: TTFont,
    characters: str,
    modified_names: Set[str],
    unmodified_names: Sequence[str],
) -> Tuple[List[str], List[str], List[str]]:
    """把指定文字映射到可独立改色的打底字形。

    返回：
    - glyph_names：按输入顺序去重后的目标字形
    - missing_characters：字体 cmap 中不存在的字符
    - skipped_modified_characters：映射到已修改/修符字形的字符（不能覆盖修符层）
    """
    cmap = font.getBestCmap() or {}
    unmodified_set = set(unmodified_names)
    glyph_names: List[str] = []
    seen_glyphs: Set[str] = set()
    missing_characters: List[str] = []
    skipped_modified_characters: List[str] = []
    for character in characters:
        codepoint = ord(character)
        # 空白和控制类字符没有可渲染轮廓，直接跳过，不计入“缺字”。
        if character.isspace() or unicodedata.category(character).startswith("C"):
            continue
        glyph_name = cmap.get(codepoint)
        if not glyph_name:
            missing_characters.append(character)
            continue
        glyph_name = str(glyph_name)
        if glyph_name in modified_names or glyph_name not in unmodified_set:
            # 修符字形保持自己的多层彩色结构；指定文字改色只作用于普通打底字。
            skipped_modified_characters.append(character)
            continue
        if glyph_name in seen_glyphs:
            continue
        seen_glyphs.add(glyph_name)
        glyph_names.append(glyph_name)
    return glyph_names, missing_characters, skipped_modified_characters


def _normalize_color_group_data_option(
    value: Any,
    palette_info: Optional[Dict[str, Any]],
) -> Optional[Dict[str, Any]]:
    """校验页面提交的分组调色数据，不接受缺失分析能力时的猜测性写回。"""
    if value is None:
        return None
    if palette_info is None or not isinstance(palette_info.get("grouping"), dict):
        raise FontAdjustmentError("当前字体不支持按字形分组修改颜色")
    if not palette_info["grouping"].get("supported"):
        raise FontAdjustmentError("当前字体不支持按字形分组修改颜色")
    if not isinstance(value, dict):
        raise FontAdjustmentError("字形分组调色参数格式无效")

    modified_palette_data = _normalize_palette_data_option(
        value.get("modifiedPaletteData"),
        palette_info,
    )
    if modified_palette_data is None:
        raise FontAdjustmentError("已修改字形调色数据缺失")

    expected_palette_count = int(palette_info["paletteCount"])
    raw_unmodified_colors = value.get("unmodifiedUniformColors")
    unmodified_colors = None
    if raw_unmodified_colors is not None:
        if not isinstance(raw_unmodified_colors, list):
            raise FontAdjustmentError("未修改打底字颜色参数格式无效")
        if len(raw_unmodified_colors) != expected_palette_count:
            raise FontAdjustmentError(
                f"未修改打底字颜色数量异常：需要 {expected_palette_count} 项，"
                f"实际 {len(raw_unmodified_colors)} 项"
            )
        unmodified_colors = [
            _normalize_hex_color(color, f"调色板 #{index + 1} 的打底字统一颜色")
            for index, color in enumerate(raw_unmodified_colors)
        ]

    target_characters = _normalize_target_characters_option(value.get("targetCharacters"))
    raw_target_colors = value.get("targetUniformColors")
    target_colors = None
    if raw_target_colors is not None:
        if not isinstance(raw_target_colors, list):
            raise FontAdjustmentError("指定文字颜色参数格式无效")
        if len(raw_target_colors) != expected_palette_count:
            raise FontAdjustmentError(
                f"指定文字颜色数量异常：需要 {expected_palette_count} 项，"
                f"实际 {len(raw_target_colors)} 项"
            )
        target_colors = [
            _normalize_hex_color(color, f"调色板 #{index + 1} 的指定文字颜色")
            for index, color in enumerate(raw_target_colors)
        ]
    if target_colors is not None and not target_characters:
        raise FontAdjustmentError("请先输入需要单独改色的文字")
    if target_characters and target_colors is None:
        raise FontAdjustmentError("指定文字改色缺少颜色参数")

    return {
        "modifiedPaletteData": modified_palette_data,
        "unmodifiedUniformColors": unmodified_colors,
        "targetCharacters": target_characters,
        "targetUniformColors": target_colors,
    }


def _color_group_change_flags(
    palette_info: Optional[Dict[str, Any]],
    group_data: Optional[Dict[str, Any]],
) -> Tuple[bool, bool, bool]:
    """分别判断已修改字形、未修改打底字和指定文字是否真的需要写回。"""
    if palette_info is None or group_data is None:
        return False, False, False
    grouping = palette_info.get("grouping") or {}
    original = _normalized_palette_data(palette_info)
    desired = group_data["modifiedPaletteData"]
    modified_changed = any(
        desired[palette_index][color_index] != original[palette_index][color_index]
        for color_index in grouping.get("modifiedPaletteIndices", [])
        for palette_index in range(int(palette_info["paletteCount"]))
    )

    requested_unmodified = group_data.get("unmodifiedUniformColors")
    unmodified_changed = False
    if requested_unmodified is not None:
        current_uniform = [
            _normalize_hex_color(color, f"当前打底字统一颜色 #{index + 1}")
            for index, color in enumerate(grouping.get("unmodifiedUniformColors", []))
        ]
        unmodified_changed = (
            not bool(grouping.get("unmodifiedUniformLayered"))
            or requested_unmodified != current_uniform
        )
    # 指定文字每次都按“独立 palette index + 目标字形 COLR 层”写回。即使颜色值碰巧
    # 与当前打底色相同，也要建立独立索引，避免之后改打底统一色时把指定字一起带走。
    target_changed = group_data.get("targetUniformColors") is not None
    return modified_changed, unmodified_changed, target_changed


def _palette_column(font: TTFont, color_index: int) -> List[str]:
    return [
        _normalize_hex_color(
            _color_to_hex(palette[color_index]),
            f"调色板 #{palette_index + 1} 颜色 #{color_index + 1}",
        )
        for palette_index, palette in enumerate(font["CPAL"].palettes)
    ]


def _append_palette_column(font: TTFont, normalized_colors: Sequence[str]) -> int:
    """向所有调色板同步追加一个 entry，并返回新 palette index。"""
    palettes = list(font["CPAL"].palettes)
    if len(palettes) != len(normalized_colors):
        raise FontAdjustmentError("新增调色板颜色数量与字体不一致")
    new_index = len(palettes[0])
    if new_index >= 0xFFFF:
        raise FontAdjustmentError("彩色调色板颜色数量已达到格式上限")
    for palette, color in zip(palettes, normalized_colors):
        if len(palette) != new_index:
            raise FontAdjustmentError("字体 CPAL 调色板颜色数量不一致，无法追加颜色")
        palette.append(Color.fromHex(color))
    # FontTools 的 CPAL 编译器不会根据 palettes 自动重算该字段；若只 append 列表，
    # 保存时会因长度与旧 numPaletteEntries 不一致触发 AssertionError。
    font["CPAL"].numPaletteEntries = new_index + 1
    return new_index


def _set_palette_column(font: TTFont, color_index: int, normalized_colors: Sequence[str]) -> int:
    """原位更新一个 palette index，返回实际变化的 ColorRecord 数量。"""
    modified = 0
    for palette, normalized in zip(font["CPAL"].palettes, normalized_colors):
        next_color = Color.fromHex(normalized)
        current = palette[color_index]
        if (
            int(current.red), int(current.green), int(current.blue), int(current.alpha)
        ) != (
            int(next_color.red), int(next_color.green), int(next_color.blue), int(next_color.alpha)
        ):
            palette[color_index] = next_color
            modified += 1
    return modified


def _colr_palette_users(color_layers: Dict[str, List[Any]]) -> Dict[int, Set[str]]:
    users: Dict[int, Set[str]] = {}
    for glyph_name, layers in color_layers.items():
        for layer in layers:
            users.setdefault(int(layer.colorID), set()).add(glyph_name)
    return users


def _assign_uniform_color_layers(
    font: TTFont,
    color_layers: Dict[str, List[Any]],
    glyph_names: Sequence[str],
    normalized_colors: Sequence[str],
    exclusive_users: Set[str],
    preferred_index: Optional[int] = None,
) -> Tuple[int, int, int]:
    """给一组字形写入统一单色 COLR 层，必要时复制独立 palette index。

    返回 (palette_records_modified, layers_remapped_or_colored, palette_index)。
    """
    if not glyph_names:
        return 0, 0, -1
    palette_users = _colr_palette_users(color_layers)
    can_update_current = (
        preferred_index is not None
        and not (palette_users.get(int(preferred_index), set()) - set(exclusive_users))
        and _palette_column(font, int(preferred_index)) == list(normalized_colors)
    )
    modified_records = 0
    if can_update_current:
        target_index = int(preferred_index)
    else:
        # 为该组建立专用 palette index。即使颜色值恰好与其它组相同也不复用，保证
        # 用户下一次单独修改任一组时不必再次猜测共享关系。
        # 若 preferred 索引仅被本组使用且颜色变化，可原位更新，避免无限追加 entry。
        can_inplace = (
            preferred_index is not None
            and not (palette_users.get(int(preferred_index), set()) - set(exclusive_users))
        )
        if can_inplace:
            target_index = int(preferred_index)
            modified_records += _set_palette_column(font, target_index, normalized_colors)
        else:
            target_index = _append_palette_column(font, normalized_colors)
            modified_records += len(normalized_colors)

    colored_glyphs = 0
    for glyph_name in glyph_names:
        previous = color_layers.get(glyph_name, [])
        if (
            len(previous) != 1
            or str(previous[0].name) != glyph_name
            or int(previous[0].colorID) != target_index
        ):
            colored_glyphs += 1
        # COLR v0 渲染 LayerRecord 时直接绘制对应字形轮廓，不会递归解析同名
        # BaseGlyphRecord，因此“字形引用自身作为单色层”是无需复制轮廓、也不会
        # 增加 glyph 数量的标准写法；轮廓缩放和粗细调整仍只处理这一份轮廓。
        color_layers[glyph_name] = [LayerRecord(glyph_name, target_index)]
    return modified_records, colored_glyphs, target_index


def _apply_color_group_edits(
    font: TTFont,
    group_data: Dict[str, Any],
) -> Dict[str, Any]:
    """按字形组写回颜色，并在共享 palette index 时自动复制和重映射。

    支持三类动作同时提交：
    1. 已修改/修符字形的 palette 颜色
    2. 未修改打底字的统一色
    3. 用户指定文字的独立统一色

    指定文字可与打底统一色同次使用：先写打底色到“非指定”打底字，再给指定字
    单独 palette index，避免后续改打底色时把指定字一起带走。
    """
    palette_info = _color_palette_info(font)
    grouping = _color_group_info(font, palette_info)
    if palette_info is None or grouping is None or not grouping.get("supported"):
        raise FontAdjustmentError("当前字体不支持按字形分组修改颜色")
    color_layers = _colr_v0_layers(font)
    if color_layers is None:
        raise FontAdjustmentError("当前字体的彩色图层格式不支持分组修改")

    modified_names = set(grouping["_modifiedGlyphNames"])
    unmodified_names = list(grouping["_unmodifiedGlyphNames"])
    desired_modified = group_data["modifiedPaletteData"]
    palette_users = _colr_palette_users(color_layers)
    modified_records = 0
    remapped_layers = 0
    warnings: List[str] = []

    for color_index in grouping["modifiedPaletteIndices"]:
        desired_column = [palette[color_index] for palette in desired_modified]
        if desired_column == _palette_column(font, color_index):
            continue
        # 同一个 CPAL index 可能同时被已修改字形和打底字引用。直接改 CPAL 会让两组
        # 一起变色；只要发现组外使用者，就复制一列并仅重映射已修改组的 LayerRecord。
        users = palette_users.get(color_index, set())
        shared_outside_modified = bool(users - modified_names)
        if shared_outside_modified:
            target_index = _append_palette_column(font, desired_column)
            modified_records += len(desired_column)
            for glyph_name in modified_names:
                for layer in color_layers.get(glyph_name, []):
                    if int(layer.colorID) == color_index:
                        layer.colorID = target_index
                        remapped_layers += 1
        else:
            modified_records += _set_palette_column(font, color_index, desired_column)

    target_characters = str(group_data.get("targetCharacters") or "")
    target_colors = group_data.get("targetUniformColors")
    target_glyph_names: List[str] = []
    missing_target_characters: List[str] = []
    skipped_modified_characters: List[str] = []
    if target_colors is not None:
        (
            target_glyph_names,
            missing_target_characters,
            skipped_modified_characters,
        ) = _resolve_target_color_glyphs(
            font,
            target_characters,
            modified_names,
            unmodified_names,
        )
        if missing_target_characters:
            preview = "".join(missing_target_characters[:12])
            more = "…" if len(missing_target_characters) > 12 else ""
            warnings.append(f"以下指定文字在字体中不存在，已跳过：{preview}{more}")
        if skipped_modified_characters:
            preview = "".join(skipped_modified_characters[:12])
            more = "…" if len(skipped_modified_characters) > 12 else ""
            warnings.append(
                f"以下指定文字属于已修改/修符字形，已跳过以免破坏彩色图层：{preview}{more}"
            )
        if not target_glyph_names:
            raise FontAdjustmentError(
                "指定文字没有可单独改色的打底字形，请检查输入或改用打底统一色"
            )

    target_set = set(target_glyph_names)
    unmodified_colors = group_data.get("unmodifiedUniformColors")
    colored_unmodified_glyphs = 0
    if unmodified_colors is not None:
        # 与指定文字同次提交时，打底统一色只覆盖“非指定”打底字，避免先上色再被
        # 指定色覆盖时仍把指定字算进打底统计，也避免两组共用同一 palette index。
        base_unmodified_names = [
            name for name in unmodified_names if name not in target_set
        ]
        preferred_index = grouping.get("unmodifiedUniformPaletteIndex")
        can_prefer = bool(grouping.get("unmodifiedUniformLayered")) and preferred_index is not None
        records, colored, _ = _assign_uniform_color_layers(
            font,
            color_layers,
            base_unmodified_names,
            unmodified_colors,
            set(base_unmodified_names),
            int(preferred_index) if can_prefer else None,
        )
        modified_records += records
        colored_unmodified_glyphs += colored

    colored_target_glyphs = 0
    if target_colors is not None:
        records, colored, _ = _assign_uniform_color_layers(
            font,
            color_layers,
            target_glyph_names,
            target_colors,
            set(target_glyph_names),
            None,
        )
        modified_records += records
        colored_target_glyphs += colored

    # 历史修符字体没有 J24C 表时，第一次分组改色后必须补写推断出的 modified 名单。
    # 否则打底字也已拥有 COLR 记录，下次分析会把所有字形都误判为已修改。
    _write_modified_glyph_manifest(font, modified_names)
    final_palette_info = _color_palette_info(font)
    if final_palette_info is None:
        raise FontAdjustmentError("分组调色后字体丢失 CPAL 表")
    final_grouping = _color_group_info(font, final_palette_info)
    return {
        "paletteEntriesModified": modified_records,
        "paletteLayersRemapped": remapped_layers,
        "unmodifiedGlyphsColored": colored_unmodified_glyphs,
        "targetGlyphsColored": colored_target_glyphs,
        "targetCharacters": target_characters,
        "missingTargetCharacters": missing_target_characters,
        "skippedModifiedCharacters": skipped_modified_characters,
        "paletteData": _normalized_palette_data(final_palette_info),
        "grouping": final_grouping,
        "unmodifiedUniformColors": unmodified_colors,
        "targetUniformColors": target_colors,
        "warnings": warnings,
    }


def _weight_axis(font: TTFont) -> Optional[Dict[str, float]]:
    if "fvar" not in font:
        return None
    for axis in font["fvar"].axes:
        if axis.axisTag == "wght":
            return {
                "min": float(axis.minValue),
                "default": float(axis.defaultValue),
                "max": float(axis.maxValue),
            }
    return None


def _base_static_weight(font: TTFont) -> int:
    """读取静态字体声明的基础字重，并归一到产品滑块使用的 100～900。"""
    if "OS/2" in font:
        value = int(getattr(font["OS/2"], "usWeightClass", 400) or 400)
        return max(100, min(900, value))
    return 400


def _is_fixed_pitch(font: TTFont) -> bool:
    """判断是否应保留等宽字体的固定 advance，避免加粗后破坏列对齐。"""
    if "post" in font and int(getattr(font["post"], "isFixedPitch", 0) or 0) != 0:
        return True
    positive_advances = {
        int(advance)
        for advance, _ in font["hmtx"].metrics.values()
        if int(advance) > 0
    }
    return len(positive_advances) == 1


def _maximum_synthetic_strength(font: TTFont, maximum_size_factor: float = 1.0) -> int:
    """计算静态加粗可使用的最大字体单位，所有限制均 fail-closed。

    FreeType 文档说明尖角处的新包围盒最坏可增加到 4 * strength，且实际点
    可能在正负两个方向移动。因此四个 int16 坐标余量都按四倍强度折算；
    非等宽字体的 advance width 只增加 strength。轮廓比例和粗细可以组合使用，
    所有格式上限都先除以允许的最大轮廓缩放倍数，避免两个单项都合法、组合后
    却越界。最后再与审美上限取最小值，并预留 2 个字体单位给取整和编译重算。
    """
    upem = max(1, int(font["head"].unitsPerEm))
    size_factor = max(1.0, float(maximum_size_factor))
    head = font["head"]
    limits = [
        float(upem) * MAX_SYNTHETIC_STRENGTH_EM,
        (float(getattr(head, "xMin", 0)) + 32768.0 / size_factor) / 4.0,
        (float(getattr(head, "yMin", 0)) + 32768.0 / size_factor) / 4.0,
        (32767.0 / size_factor - float(getattr(head, "xMax", 0))) / 4.0,
        (32767.0 / size_factor - float(getattr(head, "yMax", 0))) / 4.0,
    ]
    if not _is_fixed_pitch(font):
        positive_advances = [
            int(advance)
            for advance, _ in font["hmtx"].metrics.values()
            if int(advance) > 0
        ]
        if positive_advances:
            limits.append(65535.0 / size_factor - max(positive_advances))
        if "OS/2" in font and hasattr(font["OS/2"], "xAvgCharWidth"):
            limits.append(
                32767.0 / size_factor - float(font["OS/2"].xAvgCharWidth)
            )

    # 加粗后会把实际轮廓新增的上下增长量补到布局边界。最坏按每侧
    # 4 * strength 预留，保证之后再做轮廓缩放仍不会溢出相应字段。
    if "hhea" in font:
        limits.extend([
            (32767.0 / size_factor - float(font["hhea"].ascent)) / 4.0,
            (float(font["hhea"].descent) + 32768.0 / size_factor) / 4.0,
        ])
    if "OS/2" in font:
        os2 = font["OS/2"]
        if hasattr(os2, "sTypoAscender"):
            limits.append((32767.0 / size_factor - float(os2.sTypoAscender)) / 4.0)
        if hasattr(os2, "sTypoDescender"):
            limits.append((float(os2.sTypoDescender) + 32768.0 / size_factor) / 4.0)
        if hasattr(os2, "usWinAscent"):
            limits.append((65535.0 / size_factor - float(os2.usWinAscent)) / 4.0)
        if hasattr(os2, "usWinDescent"):
            limits.append((65535.0 / size_factor - float(os2.usWinDescent)) / 4.0)

    # 竖排 advance 与横排非等宽 advance 一样会随合成加粗增长。
    if "vmtx" in font:
        vertical_advances = [
            int(advance)
            for advance, _ in font["vmtx"].metrics.values()
            if int(advance) > 0
        ]
        if vertical_advances:
            limits.append(65535.0 / size_factor - max(vertical_advances))
    return max(0, int(math.floor(min(limits))) - 2)


def _synthetic_weight_range(
    font: TTFont,
    maximum_size_factor: float = 1.0,
) -> Optional[Dict[str, int]]:
    base = _base_static_weight(font)
    upem = max(1, int(font["head"].unitsPerEm))
    maximum_strength = _maximum_synthetic_strength(font, maximum_size_factor)
    maximum_delta = int(math.floor(maximum_strength * SYNTHETIC_WEIGHT_SCALE / upem))
    maximum = min(900, base + maximum_delta)
    # 静态字重没有真实连续轴，用 10 作为产品步长比显示 408、416 一类数值更
    # 容易理解；处理时任何正变化都至少映射为 1 个字体单位，保证不会假成功。
    step = 10
    maximum = base + ((maximum - base) // step) * step
    if maximum <= base:
        return None
    return {"min": base, "default": base, "max": maximum, "step": step}


def _weight_capability(
    font: TTFont,
    outline_kind: str,
    unsupported_tables: Iterable[str],
    maximum_size_factor: float,
) -> Tuple[str, Optional[Dict[str, float]]]:
    """返回内部粗细模式和对应范围。

    变量字体优先走设计师提供的真实 wght 轴。没有 wght 的其它变量字体不能
    当作静态字体处理，否则会让 gvar/CFF2 与修改后的默认轮廓失去对应关系。
    静态 TTF 与 CFF1 才进入合成加粗；位图、SVG、AAT 和 Graphite 字体继续
    保守禁用，避免只改一部分轮廓而留下不同步的渲染数据。
    """
    axis = _weight_axis(font)
    if axis is not None:
        if unsupported_tables:
            return "none", None
        return "variable", axis

    tables = _table_tags(font)
    if "fvar" in tables or "CFF2" in tables or unsupported_tables:
        return "none", None

    synthetic_range = _synthetic_weight_range(font, maximum_size_factor)
    if synthetic_range is None:
        return "none", None
    if outline_kind == "TrueType glyf":
        return "synthetic-ttf", synthetic_range
    if outline_kind == "OpenType CFF":
        return "synthetic-cff", synthetic_range
    return "none", None


def _safe_outline_scale_range(font: TTFont, transform_supported: bool) -> Dict[str, Any]:
    if not transform_supported:
        return {"min": 100, "max": 100, "default": 100, "step": 0.1}

    # head 边界框覆盖静态轮廓全局极值；hmtx 和排版指标另外按各自整数类型
    # 计算。变量/复杂布局表还可能含额外坐标，因此使用更保守的 1.25 上限，
    # 避免为了分析范围就遍历数万字形和全部 gvar/COLR 数据。
    signed_values: List[int] = []
    unsigned_values: List[int] = []
    head = font["head"]
    signed_values.extend(int(getattr(head, attr, 0)) for attr in ("xMin", "yMin", "xMax", "yMax"))
    if "hhea" in font:
        hhea = font["hhea"]
        signed_values.extend(int(getattr(hhea, attr, 0)) for attr in (
            "ascent", "descent", "lineGap", "minLeftSideBearing",
            "minRightSideBearing", "xMaxExtent", "caretOffset",
        ))
        unsigned_values.append(int(getattr(hhea, "advanceWidthMax", 0)))
    for advance, lsb in font["hmtx"].metrics.values():
        unsigned_values.append(int(advance))
        signed_values.append(int(lsb))
    if "OS/2" in font:
        os2 = font["OS/2"]
        for attr in (
            "xAvgCharWidth", "ySubscriptXSize", "ySubscriptYSize",
            "ySubscriptXOffset", "ySubscriptYOffset", "ySuperscriptXSize",
            "ySuperscriptYSize", "ySuperscriptXOffset", "ySuperscriptYOffset",
            "yStrikeoutSize", "yStrikeoutPosition", "sTypoAscender",
            "sTypoDescender", "sTypoLineGap", "sxHeight", "sCapHeight",
        ):
            if hasattr(os2, attr):
                signed_values.append(int(getattr(os2, attr)))
        for attr in ("usWinAscent", "usWinDescent"):
            if hasattr(os2, attr):
                unsigned_values.append(int(getattr(os2, attr)))

    tables = _table_tags(font)
    complex_tables = {"fvar", "gvar", "HVAR", "MVAR", "GPOS", "GDEF", "BASE", "MATH", "COLR", "VARC"}
    maximum_factor = 1.25 if tables & complex_tables else 1.5
    for value in signed_values:
        if value:
            limit = 32768 if value < 0 else 32767
            maximum_factor = min(maximum_factor, float(limit) / abs(value))
    for value in unsigned_values:
        if value > 0:
            maximum_factor = min(maximum_factor, 65535.0 / value)

    # 留出 2% 编译舍入余量；最终保存后仍会再次回读，任何遗漏字段都会 fail-closed。
    maximum = max(100, min(150, int(math.floor(maximum_factor * 98))))
    return {"min": 50, "max": maximum, "default": 100, "step": 0.1}


def _codepoint_in_ranges(codepoint: int, ranges: Sequence[Tuple[int, int]]) -> bool:
    return any(start <= codepoint <= end for start, end in ranges)


def _reference_codepoints(
    cmap: Dict[int, str],
    preferred_characters: str,
    ranges: Sequence[Tuple[int, int]],
    maximum_samples: int,
) -> List[int]:
    """选择少量、稳定且有代表性的参考字符，避免遍历大型中文字库全部轮廓。

    先使用产品定义的常见字符；子集字体缺少这些字符时，再从该 Unicode 区段中
    等距抽样。等距而不是只取区段开头，可以降低偏旁、兼容字或按编码顺序聚集的
    相似字形对中位值造成的偏差。
    """
    selected: List[int] = []
    selected_set: Set[int] = set()
    for character in preferred_characters:
        codepoint = ord(character)
        if codepoint in cmap and codepoint not in selected_set:
            selected.append(codepoint)
            selected_set.add(codepoint)
            if len(selected) >= maximum_samples:
                return selected

    remaining = maximum_samples - len(selected)
    if remaining <= 0:
        return selected
    candidates = [
        codepoint
        for codepoint in sorted(cmap)
        if codepoint not in selected_set and _codepoint_in_ranges(codepoint, ranges)
    ]
    if len(candidates) <= remaining:
        selected.extend(candidates)
        return selected
    if remaining == 1:
        selected.append(candidates[len(candidates) // 2])
        return selected

    # round 的结果在极少量候选中可能重复，因此先去重，再从尚未选择的候选补齐。
    sampled_indices = {
        int(round(index * (len(candidates) - 1) / (remaining - 1)))
        for index in range(remaining)
    }
    selected.extend(candidates[index] for index in sorted(sampled_indices))
    if len(selected) < maximum_samples:
        for codepoint in candidates:
            if codepoint not in selected:
                selected.append(codepoint)
                if len(selected) >= maximum_samples:
                    break
    return selected


def _merge_bounds(
    first: Optional[Tuple[float, float, float, float]],
    second: Optional[Tuple[float, float, float, float]],
) -> Optional[Tuple[float, float, float, float]]:
    if first is None:
        return second
    if second is None:
        return first
    return (
        min(first[0], second[0]),
        min(first[1], second[1]),
        max(first[2], second[2]),
        max(first[3], second[3]),
    )


def _glyph_visual_bounds(
    font: TTFont,
    glyph_set: Any,
    glyph_name: str,
) -> Optional[Tuple[float, float, float, float]]:
    """返回参考字形的可见边界，并兼容常见 COLR v0 分层字形。

    普通 glyf/CFF 字形直接绘制到 BoundsPen。COLR v0 的基字形经常没有自身轮廓，
    真实图形存在于 ColorLayers 引用的多个字形中，所以还要合并这些图层的边界。
    推荐值只是辅助功能：单个损坏或无法绘制的参考字形应被跳过，不能让本来可以
    手动处理的整份字体因为“无法推荐标准比例”而分析失败。
    """

    def draw_bounds(name: str) -> Optional[Tuple[float, float, float, float]]:
        if name not in glyph_set:
            return None
        pen = BoundsPen(glyph_set)
        try:
            glyph_set[name].draw(pen)
        except Exception:
            return None
        return pen.bounds

    bounds = draw_bounds(glyph_name)
    color_layers = None
    try:
        if "COLR" in font:
            color_layers = getattr(font["COLR"], "ColorLayers", None)
    except Exception:
        # COLR 损坏不应让可独立绘制的普通轮廓失去推荐能力；输出阶段仍会按完整
        # 字体表解析规则检查真正需要保留的数据。
        color_layers = None
    if isinstance(color_layers, dict):
        for layer in color_layers.get(glyph_name, []) or []:
            layer_name = str(getattr(layer, "name", "") or "")
            if layer_name and layer_name != glyph_name:
                bounds = _merge_bounds(bounds, draw_bounds(layer_name))
    return bounds


def _outline_standard_result(
    *,
    kind: str,
    label: str,
    source_height: float,
    target_face_percent: float,
    upem: int,
    scale_range: Dict[str, Any],
    sample_characters: str,
    sample_count: int,
    source_type: str,
) -> Dict[str, Any]:
    current_face_percent = source_height * 100.0 / upem
    ideal_scale_percent = target_face_percent * 100.0 / current_face_percent
    bounded_scale = min(
        float(scale_range["max"]),
        max(float(scale_range["min"]), ideal_scale_percent),
    )
    recommended_scale = round(bounded_scale, 1)
    # 浮点舍入可能把边界外 0.05 推到下一位；最终再夹一次，保证推荐值一定能直接
    # 通过处理核心的安全范围校验。
    recommended_scale = min(
        float(scale_range["max"]),
        max(float(scale_range["min"]), recommended_scale),
    )
    return {
        "available": True,
        "kind": kind,
        "label": label,
        "sourceType": source_type,
        "sampleCharacters": sample_characters,
        "sampleCount": sample_count,
        "currentFacePercent": round(current_face_percent, 1),
        "targetFacePercent": round(target_face_percent, 1),
        "idealScalePercent": round(ideal_scale_percent, 1),
        "recommendedScalePercent": recommended_scale,
        "limitedBySafety": abs(recommended_scale - ideal_scale_percent) >= 0.05,
        # “轴线中心”在本工具中固定定义为字体坐标轴交点 (0, 0)。ScalerVisitor
        # 对所有坐标执行同源缩放，因而不会在不同字体间引入额外的平移基准差异。
        "anchor": "axis-center",
        "anchorLabel": "轴线中心",
    }


def _outline_standard_info(
    font: TTFont,
    transform_supported: bool,
    scale_range: Dict[str, Any],
) -> Dict[str, Any]:
    """计算跨字体可比较的推荐轮廓比例；无法可靠判断时明确返回不可用。

    标准化只负责给出一个安全推荐值，绝不静默改变默认参数。用户仍需主动点击
    “应用标准比例”，这样仅修改粗细、颜色或间距时不会意外把原轮廓一并缩放。
    """
    base = {
        "available": False,
        "anchor": "axis-center",
        "anchorLabel": "轴线中心",
    }
    if not transform_supported:
        return {**base, "reason": "当前字体不支持安全轮廓缩放"}

    upem = int(font["head"].unitsPerEm)
    try:
        cmap = font.getBestCmap() or {}
        glyph_set = font.getGlyphSet()
    except Exception:
        return {**base, "reason": "字体参考字形无法解析"}
    if upem <= 0 or not cmap:
        return {**base, "reason": "字体缺少可用于统一字面的字符映射"}

    for (
        kind,
        label,
        preferred_characters,
        unicode_ranges,
        target_face_percent,
        minimum_samples,
        maximum_samples,
    ) in OUTLINE_STANDARD_REFERENCE_GROUPS:
        heights: List[float] = []
        used_characters: List[str] = []
        used_glyphs: Set[str] = set()
        for codepoint in _reference_codepoints(
            cmap,
            preferred_characters,
            unicode_ranges,
            maximum_samples,
        ):
            glyph_name = str(cmap.get(codepoint) or "")
            if not glyph_name or glyph_name in used_glyphs:
                continue
            bounds = _glyph_visual_bounds(font, glyph_set, glyph_name)
            if bounds is None:
                continue
            height = float(bounds[3]) - float(bounds[1])
            # 大于 4 UPEM 通常是装饰、错误坐标或极端上下标，不应参与普通字面标准。
            if height <= 0 or height > upem * 4:
                continue
            heights.append(height)
            used_characters.append(chr(codepoint))
            used_glyphs.add(glyph_name)
        if len(heights) >= minimum_samples:
            return _outline_standard_result(
                kind=kind,
                label=label,
                source_height=float(median(heights)),
                target_face_percent=target_face_percent,
                upem=upem,
                scale_range=scale_range,
                sample_characters="".join(used_characters),
                sample_count=len(heights),
                source_type="outline-median",
            )

    # 部分拉丁字体是小型子集，真实参考字符不足三个，但 OS/2 仍声明了可信的
    # sCapHeight/sxHeight。只接受正数且不超过 2 UPEM，避免默认 0、未初始化字段或
    # 明显损坏值生成误导性推荐。中文没有对应的标准字段，因此不做指标臆测。
    if "OS/2" in font:
        os2 = font["OS/2"]
        for kind, label, attribute, target_face_percent, unicode_range in (
            ("latin-cap-metric", "字体大写高度", "sCapHeight", 70.0, (0x0041, 0x005A)),
            ("latin-x-metric", "字体小写高度", "sxHeight", 50.0, (0x0061, 0x007A)),
        ):
            if not any(unicode_range[0] <= codepoint <= unicode_range[1] for codepoint in cmap):
                continue
            source_height = int(getattr(os2, attribute, 0) or 0)
            if 0 < source_height <= upem * 2:
                return _outline_standard_result(
                    kind=kind,
                    label=label,
                    source_height=float(source_height),
                    target_face_percent=target_face_percent,
                    upem=upem,
                    scale_range=scale_range,
                    sample_characters="",
                    sample_count=0,
                    source_type="declared-metric",
                )

    return {**base, "reason": "字体缺少足够的中文、拉丁或数字参考字形"}


def _safe_spacing_range(
    font: TTFont,
    outline_scale_range: Dict[str, Any],
    maximum_extra_advance: int = 0,
) -> Dict[str, int]:
    upem = int(font["head"].unitsPerEm)
    advances = [int(advance) for advance, _ in font["hmtx"].metrics.values() if int(advance) > 0]
    if not advances or upem <= 0:
        return {"min": 0, "max": 0, "default": 0}

    # 间距在轮廓缩放之后应用。上下限按最坏缩放组合计算，保证用户在 UI
    # 允许范围内任意组合轮廓比例与间距时，都不会让 advance width 越界。
    minimum_scaled_advance = math.floor(min(advances) * outline_scale_range["min"] / 100.0)
    maximum_scaled_advance = (
        math.ceil(max(advances) * outline_scale_range["max"] / 100.0)
        + max(0, int(maximum_extra_advance))
    )
    min_delta = 1 - minimum_scaled_advance
    max_delta = 65535 - maximum_scaled_advance

    # xAvgCharWidth 与 hmtx 一起被大小、间距和非等宽合成粗细修改，但字段本身是
    # int16。把它纳入同一组合范围，避免 hmtx 仍合法时平均字宽被静默截断。
    if "OS/2" in font and hasattr(font["OS/2"], "xAvgCharWidth"):
        average_width = int(font["OS/2"].xAvgCharWidth)
        scaled_averages = (
            average_width * outline_scale_range["min"] / 100.0,
            average_width * outline_scale_range["max"] / 100.0,
        )
        minimum_scaled_average = math.floor(min(scaled_averages))
        maximum_scaled_average = (
            math.ceil(max(scaled_averages))
            + max(0, int(maximum_extra_advance))
        )
        min_delta = max(min_delta, -32768 - minimum_scaled_average)
        max_delta = min(max_delta, 32767 - maximum_scaled_average)
    minimum = max(-25, int(math.ceil(min_delta * 100.0 / upem)))
    maximum = min(50, int(math.floor(max_delta * 100.0 / upem)))
    return {"min": min(0, minimum), "max": max(0, maximum), "default": 0}


def _line_height_models(font: TTFont) -> Iterable[Tuple[int, int]]:
    """返回 (核心字形高度, 当前行间隙)。

    ascender/descender 是字形安全边界，不应为了减小“行高”而一起压缩；真正可
    安全调节的是 lineGap。两个模型分别对应 hhea 和 OS/2 typo metrics。
    """
    if "hhea" in font:
        hhea = font["hhea"]
        core = int(hhea.ascent) - int(hhea.descent)
        if core > 0:
            yield core, int(hhea.lineGap)
    if "OS/2" in font:
        os2 = font["OS/2"]
        if all(hasattr(os2, attr) for attr in ("sTypoAscender", "sTypoDescender", "sTypoLineGap")):
            core = int(os2.sTypoAscender) - int(os2.sTypoDescender)
            if core > 0:
                yield core, int(os2.sTypoLineGap)


def _safe_line_height_range(
    font: TTFont,
    outline_scale_range: Dict[str, Any],
    maximum_core_growth: int = 0,
) -> Dict[str, int]:
    models = list(_line_height_models(font))
    if not models:
        return {"min": 100, "max": 100, "default": 100}

    minimum_factor = 0.75
    maximum_factor = 2.0
    maximum_outline_scale_factor = max(1.0, outline_scale_range["max"] / 100.0)
    for core, gap in models:
        core += max(0, int(maximum_core_growth))
        total = core + gap
        if total <= 0:
            return {"min": 100, "max": 100, "default": 100}

        # 正行间隙可以安全减到 0；负行间隙属于原字体的既有设计，但不能进一步
        # 变得更负。这样 100% 永远保持原值，低于 100% 也不会侵入字形边界。
        minimum_gap = min(0, gap)
        minimum_factor = max(minimum_factor, (core + minimum_gap) / total)

        # lineGap 是 int16。轮廓比例会同步缩放相关 metrics，因此按允许的最大
        # outlineScalePercent 计算最坏情况，为最终取整保留 2% 余量。
        scaled_core = core * maximum_outline_scale_factor
        scaled_total = total * maximum_outline_scale_factor
        maximum_factor = min(
            maximum_factor,
            (scaled_core + 32767.0) / scaled_total,
        )

    minimum = min(100, max(75, int(math.ceil(minimum_factor * 100.0))))
    maximum = max(100, min(200, int(math.floor(maximum_factor * 98.0))))
    return {"min": minimum, "max": maximum, "default": 100}


def _analyze_prepared(
    path: str,
    filename: str,
    *,
    container: Optional[str] = None,
    source_container: Optional[str] = None,
    adapter_warnings: Optional[Sequence[str]] = None,
    file_size: Optional[int] = None,
) -> Dict[str, Any]:
    container = container or _validate_container(path)
    font = _load_font(path)
    try:
        tables = _table_tags(font)
        outline_kind = _font_outline_kind(font)
        color_palette = _color_palette_info(font)
        color_grouping = _color_group_info(font, color_palette)
        if color_palette is not None:
            color_palette = {
                **color_palette,
                "grouping": _public_color_group_info(color_grouping),
            }
        font_naming = _font_naming_info(font)
        unsupported_tables = sorted((BITMAP_OR_SVG_TABLES | AAT_OR_GRAPHITE_TABLES) & tables)
        transform_supported = outline_kind != "未知轮廓" and not unsupported_tables
        outline_scale_range = _safe_outline_scale_range(font, transform_supported)
        outline_standard = _outline_standard_info(
            font,
            transform_supported,
            outline_scale_range,
        )
        weight_mode, weight_range = _weight_capability(
            font,
            outline_kind,
            unsupported_tables,
            outline_scale_range["max"] / 100.0,
        )
        warnings: List[str] = []

        if unsupported_tables:
            warnings.append("当前字体包含特殊图形数据，暂不支持调整轮廓比例")
        if color_grouping and color_grouping.get("manifestSource") == "invalid":
            warnings.append(str(color_grouping.get("reason") or "字体的字形分组信息不完整"))
        if "CFF " in tables or "CFF2" in tables:
            warnings.append("当前字体处理后文件大小可能发生变化")
        if weight_mode == "none":
            warnings.append("当前字体不支持粗细调整")

        maximum_weight_strength = 0
        maximum_extra_advance = 0
        if weight_mode in {"synthetic-ttf", "synthetic-cff"} and weight_range:
            maximum_weight_strength = _synthetic_weight_strength(
                font,
                float(weight_range["max"]),
                float(weight_range["default"]),
            )
            if not _is_fixed_pitch(font):
                maximum_extra_advance = math.ceil(
                    maximum_weight_strength * outline_scale_range["max"] / 100.0
                )
        warnings = [*(adapter_warnings or []), *warnings]
        result = {
            "ok": True,
            "processorVersion": PROCESSOR_VERSION,
            "filename": filename,
            "fileSize": int(file_size if file_size is not None else os.path.getsize(path)),
            "container": container,
            "sourceContainer": source_container or container,
            "recommendedExtension": ".ttf" if container == "TTF" else ".otf",
            "outlineKind": outline_kind,
            "unitsPerEm": int(font["head"].unitsPerEm),
            "glyphCount": len(font.getGlyphOrder()),
            "tableCount": len(tables),
            "tables": sorted(tables),
            "unsupportedTables": unsupported_tables,
            "fontNaming": font_naming,
            "outlineStandard": outline_standard,
            "capabilities": {
                "outlineScale": transform_supported,
                # size 是 1.4.0 之前页面使用的兼容字段；其实际语义始终是宽高统一的
                # 轮廓缩放，并不是修改字号或 unitsPerEm。
                "size": transform_supported,
                "spacing": "hmtx" in tables,
                "lineHeight": "hhea" in tables or "OS/2" in tables,
                "weight": weight_mode != "none",
                "weightMode": weight_mode,
                "colorPalette": color_palette is not None,
                "colorGrouping": bool(color_grouping and color_grouping.get("supported")),
            },
            "ranges": {
                "outlineScalePercent": outline_scale_range,
                "sizePercent": outline_scale_range,
                "spacingEmPercent": _safe_spacing_range(
                    font,
                    outline_scale_range,
                    maximum_extra_advance,
                ),
                # 单侧轮廓最坏增长 4 * strength，上下两侧合计按 8 倍计入核心行高。
                "lineHeightPercent": _safe_line_height_range(
                    font,
                    outline_scale_range,
                    maximum_weight_strength * 8,
                ),
                "weight": weight_range,
            },
            "colorPalette": color_palette,
            "warnings": warnings,
        }
        return result
    finally:
        font.close()


def _analyze(path: str, filename: str) -> Dict[str, Any]:
    with _prepared_font_path(path, filename) as (
        prepared_path,
        container,
        source_container,
        adapter_warnings,
        source_size,
    ):
        return _analyze_prepared(
            prepared_path,
            filename,
            container=container,
            source_container=source_container,
            adapter_warnings=adapter_warnings,
            file_size=source_size,
        )


def analyze_font_file_json(path: str, filename: str) -> str:
    try:
        return json.dumps(_analyze(path, filename), ensure_ascii=False)
    except FontAdjustmentError as error:
        return json.dumps({"ok": False, "error": str(error)}, ensure_ascii=False)
    except Exception as error:
        return json.dumps({"ok": False, "error": f"字体分析失败：{error}"}, ensure_ascii=False)


def _finite_number(options: Dict[str, Any], key: str, default: float) -> float:
    """读取自由数值参数，只拒绝无法计算的 NaN、Infinity 和非数字内容。

    产品推荐区间属于滑块交互信息，不应在这里再次变成隐藏的业务上下限。具体
    OpenType 字段能否容纳该结果，由各变换函数在写入真实字段时给出明确错误。
    """
    value = options.get(key, default)
    try:
        numeric = float(value)
    except (TypeError, ValueError) as error:
        raise FontAdjustmentError(f"参数 {key} 不是有效数字") from error
    if not math.isfinite(numeric):
        raise FontAdjustmentError(f"参数 {key} 不是有限数字")
    return numeric


def _outline_orientation(points: List[List[float]], end_points: List[int]) -> int:
    """返回 FreeType 约定的轮廓方向：-1=TrueType，1=PostScript，0=无效。

    实现对应 FreeType 2.14.1 `FT_Outline_Get_Orientation`。它使用控制点
    多边形而不是把贝塞尔曲线折线化，因此不会改变二次/三次曲线的点拓扑。
    """
    if not points:
        return -1
    xs = [point[0] for point in points]
    ys = [point[1] for point in points]
    if min(xs) == max(xs) or min(ys) == max(ys):
        return 0

    area = 0.0
    first = 0
    for last in end_points:
        if last < first or last >= len(points):
            return 0
        previous_x, previous_y = points[last]
        for index in range(first, last + 1):
            current_x, current_y = points[index]
            area += (current_y - previous_y) * (current_x + previous_x)
            previous_x, previous_y = current_x, current_y
        first = last + 1
    if first != len(points):
        return 0
    if area > 0:
        return 1
    if area < 0:
        return -1
    return 0


def _embolden_outline(
    source_points: Iterable[Tuple[float, float]],
    end_points: Iterable[int],
    x_strength: int,
    y_strength: int,
) -> List[Tuple[int, int]]:
    """按 FreeType 2.14.1 `FT_Outline_EmboldenXY` 加粗一组封闭轮廓。

    这里保留原算法的三个关键性质：不增加轮廓点、不把曲线折线化、按轮廓
    方向向填充区域外侧移动。Python 使用双精度数完成固定点运算的等价计算，
    最后统一按 OpenType 规则取整；输入点数和轮廓结束点始终保持不变。
    """
    points = [[float(x), float(y)] for x, y in source_points]
    contours = [int(value) for value in end_points]
    if not points or not contours or (x_strength == 0 and y_strength == 0):
        return [(otRound(x), otRound(y)) for x, y in points]

    orientation = _outline_orientation(points, contours)
    if orientation == 0:
        # 部分商业字体把空格保存成“一个位于原点的退化轮廓”。它没有任何可见
        # 填充区域，FreeType 也不会得到可加粗的形状；原样保留即可，不能因为
        # 这类合法但多余的数据拒绝整套字体。
        xs = [point[0] for point in points]
        ys = [point[1] for point in points]
        if min(xs) == max(xs) or min(ys) == max(ys):
            return [(otRound(x), otRound(y)) for x, y in points]
        raise FontAdjustmentError("字体包含无法判断方向的退化轮廓，不能安全调整粗细")

    half_x = float(x_strength) / 2.0
    half_y = float(y_strength) / 2.0
    last = -1
    for contour_end in contours:
        first = last + 1
        last = contour_end
        if last < first:
            continue

        length_in = 0.0
        in_x = in_y = 0.0
        anchor_x = anchor_y = anchor_length = 0.0
        index_i = last
        index_j = first
        anchor_index = -1

        # FreeType 的循环会跳过连续重合点，并在第一次实际移动的点处设置锚点。
        # guard 只防御损坏字体导致的非收敛状态，正常轮廓不会触发。
        guard = 0
        guard_limit = max(8, (last - first + 1) * 4)
        while index_j != index_i and index_i != anchor_index:
            guard += 1
            if guard > guard_limit:
                raise FontAdjustmentError("字体轮廓结构异常，粗细计算未能收敛")

            if index_j != anchor_index:
                out_x = points[index_j][0] - points[index_i][0]
                out_y = points[index_j][1] - points[index_i][1]
                length_out = math.hypot(out_x, out_y)
                if length_out == 0:
                    index_j = index_j + 1 if index_j < last else first
                    continue
                out_x /= length_out
                out_y /= length_out
            else:
                out_x, out_y = anchor_x, anchor_y
                length_out = anchor_length

            if length_in != 0:
                if anchor_index < 0:
                    anchor_index = index_i
                    anchor_x, anchor_y = in_x, in_y
                    anchor_length = length_in

                dot = in_x * out_x + in_y * out_y
                if dot > -0.9375:  # 与 FreeType 的 -0xF000 / 65536 相同
                    denominator = dot + 1.0
                    shift_x = in_y + out_y
                    shift_y = in_x + out_x
                    if orientation < 0:  # TrueType：填充区域位于绘制方向右侧
                        shift_x = -shift_x
                    else:
                        shift_y = -shift_y

                    cross = out_x * in_y - out_y * in_x
                    if orientation < 0:
                        cross = -cross
                    shortest = min(length_in, length_out)

                    if half_x * cross <= shortest * denominator:
                        shift_x = shift_x * half_x / denominator
                    else:
                        shift_x = shift_x * shortest / cross
                    if half_y * cross <= shortest * denominator:
                        shift_y = shift_y * half_y / denominator
                    else:
                        shift_y = shift_y * shortest / cross
                else:
                    shift_x = shift_y = 0.0

                while index_i != index_j:
                    points[index_i][0] = otRound(points[index_i][0] + half_x + shift_x)
                    points[index_i][1] = otRound(points[index_i][1] + half_y + shift_y)
                    index_i = index_i + 1 if index_i < last else first
            else:
                index_i = index_j

            in_x, in_y = out_x, out_y
            length_in = length_out
            index_j = index_j + 1 if index_j < last else first

    return [(otRound(x), otRound(y)) for x, y in points]


def _synthetic_weight_strength(font: TTFont, target_weight: float, base_weight: float) -> int:
    delta = max(0.0, float(target_weight) - float(base_weight))
    if delta == 0:
        return 0
    return max(1, otRound(int(font["head"].unitsPerEm) * delta / SYNTHETIC_WEIGHT_SCALE))


def _drop_truetype_hinting(font: TTFont) -> Set[str]:
    """删除与已修改 glyf 轮廓不再匹配的 TrueType 提示数据。"""
    removed: Set[str] = set()
    if "glyf" in font:
        for glyph in font["glyf"].glyphs.values():
            if hasattr(glyph, "program"):
                glyph.program = Program()
    for table_tag in TRUETYPE_HINT_TABLES:
        if table_tag in font:
            del font[table_tag]
            removed.add(table_tag)
    return removed


def _checked_add_signed(value: int, delta: int, field_name: str) -> int:
    result = int(value) + int(delta)
    if not -32768 <= result <= 32767:
        raise FontAdjustmentError(f"粗细调整使 {field_name} 超出字体格式范围")
    return result


def _checked_add_unsigned(value: int, delta: int, field_name: str) -> int:
    result = int(value) + int(delta)
    if not 0 <= result <= 65535:
        raise FontAdjustmentError(f"粗细调整使 {field_name} 超出字体格式范围")
    return result


def _apply_synthetic_weight_metrics(font: TTFont, x_strength: int, y_strength: int) -> None:
    """同步更新加粗后会受影响的 advance；轮廓上下边界稍后按实测增长更新。"""
    fixed_pitch = _is_fixed_pitch(font)
    if not fixed_pitch:
        for glyph_name, (advance, lsb) in list(font["hmtx"].metrics.items()):
            if int(advance) <= 0:
                continue  # 组合附标的零字宽必须保持为零
            font["hmtx"].metrics[glyph_name] = (
                _checked_add_unsigned(int(advance), x_strength, f"hmtx.{glyph_name}.advance"),
                int(lsb),
            )
        if "OS/2" in font and hasattr(font["OS/2"], "xAvgCharWidth"):
            font["OS/2"].xAvgCharWidth = _checked_add_signed(
                int(font["OS/2"].xAvgCharWidth), x_strength, "OS/2.xAvgCharWidth"
            )

    if "hhea" in font:
        hhea = font["hhea"]
        hhea.advanceWidthMax = max(int(advance) for advance, _ in font["hmtx"].metrics.values())

    if "vmtx" in font:
        for glyph_name, (advance, tsb) in list(font["vmtx"].metrics.items()):
            if int(advance) <= 0:
                continue
            font["vmtx"].metrics[glyph_name] = (
                _checked_add_unsigned(int(advance), y_strength, f"vmtx.{glyph_name}.advance"),
                int(tsb),
            )
        if "vhea" in font:
            font["vhea"].advanceHeightMax = max(
                int(advance) for advance, _ in font["vmtx"].metrics.values()
            )



def _current_outline_bounds(font: TTFont) -> Tuple[int, int, int, int]:
    """计算所有字形的真实联合边界，兼容 glyf、复合字形和 CFF。"""
    bounds: Optional[Tuple[float, float, float, float]] = None
    glyph_set = font.getGlyphSet()
    for glyph_name in font.getGlyphOrder():
        pen = BoundsPen(glyph_set)
        glyph_set[glyph_name].draw(pen)
        if pen.bounds is None:
            continue
        if bounds is None:
            bounds = pen.bounds
        else:
            bounds = (
                min(bounds[0], pen.bounds[0]),
                min(bounds[1], pen.bounds[1]),
                max(bounds[2], pen.bounds[2]),
                max(bounds[3], pen.bounds[3]),
            )
    if bounds is None:
        return 0, 0, 0, 0
    return tuple(otRound(value) for value in bounds)  # type: ignore[return-value]


def _apply_outline_growth_metrics(
    font: TTFont,
    original_bounds: Tuple[int, int, int, int],
) -> None:
    """只把加粗新增的上下越界量补进行布局边界，不重写字体原有度量策略。"""
    new_bounds = _current_outline_bounds(font)
    top_growth = max(0, new_bounds[3] - original_bounds[3])
    bottom_growth = max(0, original_bounds[1] - new_bounds[1])
    if "hhea" in font:
        hhea = font["hhea"]
        hhea.ascent = _checked_add_signed(int(hhea.ascent), top_growth, "hhea.ascent")
        hhea.descent = _checked_add_signed(int(hhea.descent), -bottom_growth, "hhea.descent")
    if "OS/2" in font:
        os2 = font["OS/2"]
        if hasattr(os2, "sTypoAscender"):
            os2.sTypoAscender = _checked_add_signed(
                int(os2.sTypoAscender), top_growth, "OS/2.sTypoAscender"
            )
        if hasattr(os2, "sTypoDescender"):
            os2.sTypoDescender = _checked_add_signed(
                int(os2.sTypoDescender), -bottom_growth, "OS/2.sTypoDescender"
            )
        if hasattr(os2, "usWinAscent"):
            os2.usWinAscent = _checked_add_unsigned(
                int(os2.usWinAscent), top_growth, "OS/2.usWinAscent"
            )
        if hasattr(os2, "usWinDescent"):
            os2.usWinDescent = _checked_add_unsigned(
                int(os2.usWinDescent), bottom_growth, "OS/2.usWinDescent"
            )


def _update_weight_metadata(font: TTFont, target_weight: float) -> None:
    """让字体匹配器看到的字重和粗体标志与实际输出轮廓保持一致。"""
    normalized = max(1, min(1000, int(round(target_weight))))
    is_bold = normalized >= 700
    is_regular = 350 <= normalized <= 450
    if "OS/2" in font:
        os2 = font["OS/2"]
        os2.usWeightClass = normalized
        selection = int(getattr(os2, "fsSelection", 0))
        selection = selection | (1 << 5) if is_bold else selection & ~(1 << 5)
        selection = selection | (1 << 6) if is_regular else selection & ~(1 << 6)
        os2.fsSelection = selection
    if "head" in font:
        mac_style = int(getattr(font["head"], "macStyle", 0))
        font["head"].macStyle = mac_style | 1 if is_bold else mac_style & ~1


def _embolden_truetype(font: TTFont, strength: int) -> int:
    glyf_table = font["glyf"]
    modified = 0
    for glyph_name in font.getGlyphOrder():
        glyph = glyf_table[glyph_name]
        if glyph.isComposite() or int(getattr(glyph, "numberOfContours", 0)) <= 0:
            continue
        coordinates = glyph.coordinates
        updated = _embolden_outline(
            coordinates,
            glyph.endPtsOfContours,
            strength,
            strength,
        )
        if len(updated) != len(coordinates):
            raise FontAdjustmentError(f"字形 {glyph_name} 粗细调整后点数量异常")
        original = [(int(x), int(y)) for x, y in coordinates]
        for index, point in enumerate(updated):
            coordinates[index] = point
        glyph.recalcBounds(glyf_table)
        if updated != original:
            modified += 1

    # 复合字形本身不拆平；其引用的简单字形已经变粗，只需递归重算边界框。
    bounds_done: Set[str] = set()
    for glyph_name in font.getGlyphOrder():
        glyph = glyf_table[glyph_name]
        if glyph.isComposite():
            glyph.recalcBounds(glyf_table, boundsDone=bounds_done)
            bounds_done.add(glyph_name)
    return modified


def _recording_outline(recording: RecordingPen) -> Tuple[List[Tuple[float, float]], List[int]]:
    points: List[Tuple[float, float]] = []
    end_points: List[int] = []
    contour_open = False
    for operator, operands in recording.value:
        if operator == "addComponent":
            raise FontAdjustmentError("当前 CFF 字体包含无法安全展开的组件字形")
        if operator == "moveTo":
            if contour_open:
                raise FontAdjustmentError("当前 CFF 字体包含未闭合轮廓")
            contour_open = True
        if operator in {"moveTo", "lineTo", "curveTo", "qCurveTo"}:
            for operand in operands:
                if operand is None:  # qCurveTo 的隐式闭合标记，不是实际坐标点
                    continue
                points.append((float(operand[0]), float(operand[1])))
        elif operator == "closePath":
            if contour_open:
                end_points.append(len(points) - 1)
                contour_open = False
        elif operator == "endPath":
            raise FontAdjustmentError("当前 CFF 字体包含开放轮廓，不能安全调整粗细")
    if contour_open:
        raise FontAdjustmentError("当前 CFF 字体包含未闭合轮廓")
    return points, end_points


def _replay_emboldened_recording(
    original: RecordingPen,
    points: List[Tuple[int, int]],
    destination: T2CharStringPen,
) -> None:
    cursor = 0
    transformed = RecordingPen()
    for operator, operands in original.value:
        new_operands = []
        for operand in operands:
            if operand is None:
                new_operands.append(None)
                continue
            if operator not in {"moveTo", "lineTo", "curveTo", "qCurveTo"}:
                new_operands.append(operand)
                continue
            if cursor >= len(points):
                raise FontAdjustmentError("CFF 轮廓写回时点数量不足")
            new_operands.append(points[cursor])
            cursor += 1
        transformed.value.append((operator, tuple(new_operands)))
    if cursor != len(points):
        raise FontAdjustmentError("CFF 轮廓写回时点数量不一致")
    transformed.replay(destination)


def _cff_private_dicts(font: TTFont) -> List[Any]:
    top_dict = font["CFF "].cff.topDictIndex[0]
    if hasattr(top_dict, "FDArray"):
        return [fd.Private for fd in top_dict.FDArray]
    private = getattr(top_dict, "Private", None)
    return [private] if private is not None else []


def _clear_cff_hints_and_subroutines(font: TTFont) -> None:
    cff = font["CFF "].cff
    cff.GlobalSubrs.clear()
    for private in _cff_private_dicts(font):
        for attr in CFF_HINT_PRIVATE_ATTRIBUTES:
            if hasattr(private, attr):
                setattr(private, attr, None)
        if hasattr(private, "Subrs"):
            del private.Subrs
        raw_dict = getattr(private, "rawDict", None)
        if isinstance(raw_dict, dict):
            raw_dict.pop("Subrs", None)


def _cff_charstring_width_operand(char_string: Any, advance: int) -> Optional[float]:
    """把 OpenType hmtx 的绝对字宽换算为 Type 2 CharString 宽度操作数。

    CFF CharString 并不直接保存绝对字宽：等于 defaultWidthX 时完全省略，否则
    保存 `advance - nominalWidthX`。把 hmtx 的绝对值直接交给 T2CharStringPen
    会在 nominalWidthX 非零的真实字体中把字宽重复加一次，文件虽可解析，内部
    两套度量却互相矛盾。CID 字体的每个 FD 可以有独立 Private，因此必须逐字形
    使用原 CharString 所属的 Private 计算。
    """
    private = getattr(char_string, "private", None)
    default_width = float(getattr(private, "defaultWidthX", 0) or 0)
    nominal_width = float(getattr(private, "nominalWidthX", 0) or 0)
    if float(advance) == default_width:
        return None
    return float(advance) - nominal_width


def _embolden_cff(font: TTFont, strength: int) -> int:
    cff = font["CFF "].cff
    top_dict = cff.topDictIndex[0]
    char_strings = top_dict.CharStrings
    glyph_set = font.getGlyphSet()
    modified = 0

    for glyph_name in font.getGlyphOrder():
        recording = RecordingPen()
        glyph_set[glyph_name].draw(recording)
        points, end_points = _recording_outline(recording)
        updated = _embolden_outline(points, end_points, strength, strength) if points else []
        shape_changed = updated != [(otRound(x), otRound(y)) for x, y in points]
        old_char_string, selector = char_strings.getItemAndSelector(glyph_name)
        width = int(font["hmtx"].metrics[glyph_name][0])
        pen = T2CharStringPen(_cff_charstring_width_operand(old_char_string, width), None)
        _replay_emboldened_recording(recording, updated, pen)
        new_char_string = pen.getCharString(
            private=old_char_string.private,
            globalSubrs=cff.GlobalSubrs,
            optimize=True,
        )
        if selector is not None:
            new_char_string.fdSelectIndex = selector
        char_strings[glyph_name] = new_char_string
        if shape_changed:
            modified += 1

    _clear_cff_hints_and_subroutines(font)
    return modified


def _validate_cff_charstring_widths(font: TTFont, glyph_order: Iterable[str]) -> None:
    """确认重建后的 CFF CharString 字宽与 OpenType hmtx 完全一致。"""
    top_dict = font["CFF "].cff.topDictIndex[0]
    char_strings = top_dict.CharStrings
    for glyph_name in glyph_order:
        char_string, _ = char_strings.getItemAndSelector(glyph_name)
        # draw 会执行本地/全局 subroutine 并由 FontTools 解出最终绝对 width；
        # NullPen 不保存轮廓，避免为了度量验证再分配一份大字形记录。
        char_string.draw(NullPen())
        actual_width = float(getattr(char_string, "width", 0))
        expected_width = int(font["hmtx"].metrics[glyph_name][0])
        if abs(actual_width - expected_width) > 1e-6:
            raise FontAdjustmentError(
                f"输出字形 {glyph_name} 的 CFF 字宽与 hmtx 不一致："
                f"{actual_width:g} != {expected_width}"
            )


def _apply_spacing(font: TTFont, spacing_em_percent: float) -> int:
    if spacing_em_percent == 0:
        return 0
    upem = int(font["head"].unitsPerEm)
    delta = otRound(upem * spacing_em_percent / 100.0)
    for glyph_name, (advance, lsb) in list(font["hmtx"].metrics.items()):
        if advance <= 0:
            continue
        new_advance = int(advance) + delta
        if not 1 <= new_advance <= 65535:
            raise FontAdjustmentError(f"字间距会使字形 {glyph_name} 的字宽超出字体格式范围")
        font["hmtx"].metrics[glyph_name] = (new_advance, lsb)

    if "hhea" in font:
        hhea = font["hhea"]
        hhea.advanceWidthMax = max(advance for advance, _ in font["hmtx"].metrics.values())
        hhea.minRightSideBearing = max(-32768, min(32767, int(hhea.minRightSideBearing) + delta))
    if "OS/2" in font and hasattr(font["OS/2"], "xAvgCharWidth"):
        os2 = font["OS/2"]
        new_average_width = int(os2.xAvgCharWidth) + delta
        if not -32768 <= new_average_width <= 32767:
            raise FontAdjustmentError("字间距会使 OS/2.xAvgCharWidth 超出字体格式范围")
        os2.xAvgCharWidth = new_average_width
    return delta


def _apply_line_height(font: TTFont, line_height_percent: float) -> None:
    factor = line_height_percent / 100.0
    if factor == 1.0:
        return

    def adjusted_gap(core: int, gap: int, field_name: str) -> int:
        total = core + gap
        target_total = otRound(total * factor)
        # OpenType 的 hhea.lineGap 与 OS/2.sTypoLineGap 都是有符号 int16。自由输入
        # 低于分析推荐值时，负 lineGap 是表达“行框小于字形核心高度”的合法方式；
        # 若继续强制钳制到 0，页面会接受数值却生成与请求不一致的行高。滑块推荐区间
        # 仍默认避免字形重叠，但直接输入时按用户数值精确计算，最终只受字段格式约束。
        result = target_total - core
        if not -32768 <= result <= 32767:
            raise FontAdjustmentError(f"行高参数使 {field_name} 超出 int16 范围")
        return result

    if "hhea" in font:
        hhea = font["hhea"]
        core = int(hhea.ascent) - int(hhea.descent)
        hhea.lineGap = adjusted_gap(core, int(hhea.lineGap), "hhea.lineGap")
    if "OS/2" in font:
        os2 = font["OS/2"]
        if all(hasattr(os2, attr) for attr in ("sTypoAscender", "sTypoDescender", "sTypoLineGap")):
            core = int(os2.sTypoAscender) - int(os2.sTypoDescender)
            os2.sTypoLineGap = adjusted_gap(core, int(os2.sTypoLineGap), "OS/2.sTypoLineGap")


def _validate_output(
    input_path: str,
    output_path: str,
    input_tables: Set[str],
    allowed_removed_tables: Set[str],
    expected_glyph_count: int,
    expected_outline_kind: str,
    changed: bool,
    validate_cff_widths: bool = False,
    expected_palette_data: Optional[List[List[str]]] = None,
    expected_font_naming: Optional[Dict[str, str]] = None,
    expected_color_grouping: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    try:
        output_font = TTFont(output_path, lazy=False, recalcTimestamp=False)
    except Exception as error:
        raise FontAdjustmentError(f"输出字体无法重新解析：{error}") from error
    try:
        output_tables = _table_tags(output_font)
        # TTFont 即使 lazy=False 也可能延迟到首次访问才反序列化具体表。
        # 逐表读取一次，避免只验证目录存在却漏过内部结构损坏。
        for table_tag in sorted(output_tables):
            try:
                output_font[table_tag]
            except Exception as error:
                raise FontAdjustmentError(f"输出字体表 {table_tag} 无法解析：{error}") from error
        required = {"head", "maxp", "hmtx"}
        missing_required = sorted(required - output_tables)
        if missing_required:
            raise FontAdjustmentError("输出字体缺少必要表：" + "、".join(missing_required))

        removed = sorted(input_tables - output_tables)
        unexpected_removed = sorted(set(removed) - allowed_removed_tables)
        if unexpected_removed:
            raise FontAdjustmentError("处理后意外丢失字体表：" + "、".join(unexpected_removed))

        glyph_order = output_font.getGlyphOrder()
        if len(glyph_order) != expected_glyph_count:
            raise FontAdjustmentError(
                f"处理后字形数量异常：原字体 {expected_glyph_count}，输出 {len(glyph_order)}"
            )
        if _font_outline_kind(output_font) != expected_outline_kind:
            raise FontAdjustmentError("处理后字体轮廓格式发生了意外变化")

        # 访问 glyf/CFF 表对象并不能保证每个字形程序都已反序列化。逐字形绘制到
        # BoundsPen 会真正执行所有简单、复合和 CFF CharString，从而在返回文件前
        # 捕获坏 loca、坏组件引用、坏 subroutine 或操作数栈错误。
        glyph_set = output_font.getGlyphSet()
        for glyph_name in glyph_order:
            try:
                glyph_set[glyph_name].draw(BoundsPen(glyph_set))
            except Exception as error:
                raise FontAdjustmentError(f"输出字形 {glyph_name} 无法解析：{error}") from error
        if validate_cff_widths:
            try:
                _validate_cff_charstring_widths(output_font, glyph_order)
            except FontAdjustmentError:
                raise
            except Exception as error:
                raise FontAdjustmentError(f"输出 CFF 字宽无法验证：{error}") from error

        if expected_palette_data is not None:
            output_palette = _color_palette_info(output_font)
            if output_palette is None:
                raise FontAdjustmentError("输出字体丢失彩色调色板")
            actual_palette_data = [
                [
                    _normalize_hex_color(
                        color,
                        f"输出调色板 #{palette_index + 1} 颜色 #{color_index + 1}",
                    )
                    for color_index, color in enumerate(palette)
                ]
                for palette_index, palette in enumerate(output_palette["palettes"])
            ]
            if actual_palette_data != expected_palette_data:
                raise FontAdjustmentError("输出字体彩色调色板与设置值不一致")

        output_color_grouping = None
        if expected_color_grouping is not None:
            output_palette_for_group = _color_palette_info(output_font)
            output_color_grouping = _color_group_info(output_font, output_palette_for_group)
            if output_color_grouping is None:
                raise FontAdjustmentError("输出字体丢失字形颜色分组")
            if output_color_grouping.get("manifestSource") != "embedded":
                raise FontAdjustmentError("输出字体未保存已修改字形清单")
            if set(output_color_grouping.get("_modifiedGlyphNames", [])) != set(
                expected_color_grouping.get("_modifiedGlyphNames", [])
            ):
                raise FontAdjustmentError("输出字体的已修改字形分组与输入不一致")
            expected_uniform_colors = expected_color_grouping.get("expectedUnmodifiedUniformColors")
            if expected_uniform_colors is not None:
                actual_uniform_colors = [
                    _normalize_hex_color(color, f"输出打底字统一颜色 #{index + 1}")
                    for index, color in enumerate(
                        output_color_grouping.get("unmodifiedUniformColors", [])
                    )
                ]
                if (
                    not output_color_grouping.get("unmodifiedUniformLayered")
                    or actual_uniform_colors != expected_uniform_colors
                ):
                    raise FontAdjustmentError("输出字体的未修改打底字颜色与设置值不一致")

        output_naming = None
        if expected_font_naming is not None:
            output_naming = _font_naming_info(output_font)
            fields_to_verify = [
                "fontFamily", "fontSubFamily", "preferredFamily", "preferredSubFamily",
                "fullName", "compatibleFull", "postScriptName", "uniqueSubFamily",
            ]
            for optional_field in ("wwsFamily", "wwsSubFamily"):
                if expected_font_naming.get(optional_field):
                    fields_to_verify.append(optional_field)
            for field_name in fields_to_verify:
                if output_naming.get(field_name) != expected_font_naming.get(field_name):
                    raise FontAdjustmentError(
                        f"输出字体名称字段 {field_name} 与设置值不一致"
                    )
            if "CFF " in output_font:
                cff = output_font["CFF "].cff
                top_dict = cff.topDictIndex[0]
                actual_cff_names = {
                    "cffFontName": str(cff.fontNames[0] if cff.fontNames else ""),
                    "cffFamilyName": str(getattr(top_dict, "FamilyName", "") or ""),
                    "cffFullName": str(getattr(top_dict, "FullName", "") or ""),
                }
                for field_name, actual_value in actual_cff_names.items():
                    if actual_value != expected_font_naming.get(field_name):
                        raise FontAdjustmentError(
                            f"输出 CFF 名称字段 {field_name} 与设置值不一致"
                        )

        output_hash = _sha256_file(output_path)
        input_hash = _sha256_file(input_path)
        if changed and output_hash == input_hash:
            raise FontAdjustmentError("参数未实际改变字体内容，已拒绝返回假成功结果")

        return {
            "inputSha256": input_hash,
            "outputSha256": output_hash,
            "outputSize": os.path.getsize(output_path),
            "tableCount": len(output_tables),
            "glyphCount": len(glyph_order),
            "removedTables": removed,
            "paletteData": output_palette["palettes"] if expected_palette_data is not None else None,
            "colorGrouping": _public_color_group_info(output_color_grouping),
            "fontNaming": output_naming,
        }
    finally:
        output_font.close()


def _process_prepared(
    input_path: str,
    output_path: str,
    options: Dict[str, Any],
    analysis: Dict[str, Any],
) -> Dict[str, Any]:
    # outlineScalePercent 是统一轮廓比例的新名称。旧页面仍可能从 WebView 缓存中
    # 发送 sizePercent，因此核心继续接受它；两者同时存在时以新字段为准。
    outline_scale_key = "outlineScalePercent" if "outlineScalePercent" in options else "sizePercent"
    outline_scale_percent = _finite_number(options, outline_scale_key, 100)
    spacing_percent = _finite_number(options, "spacingEmPercent", 0)
    line_percent = _finite_number(options, "lineHeightPercent", 100)
    weight_value_raw = options.get("weightValue")
    weight_value = None if weight_value_raw is None else _finite_number(options, "weightValue", 0)
    requested_family_raw = options.get("familyName")
    requested_family_name = (
        None
        if requested_family_raw is None
        else _normalize_font_family_name(requested_family_raw)
    )
    current_font_naming = dict(analysis.get("fontNaming") or {})
    apply_naming = bool(
        requested_family_name is not None
        and requested_family_name != current_font_naming.get("fontFamily")
    )
    palette_info = analysis.get("colorPalette")
    palette_data = _normalize_palette_data_option(
        options.get("paletteData"),
        palette_info,
    )
    color_group_data = _normalize_color_group_data_option(
        options.get("colorGroupData"),
        palette_info,
    )
    if palette_data is not None and color_group_data is not None:
        raise FontAdjustmentError("整表调色和字形分组调色不能同时提交")

    ranges = analysis["ranges"]
    # 页面允许直接输入推荐区间外数值，因此核心不再用分析阶段的快捷滑块范围拒绝
    # 请求。轮廓比例和行高必须保持正数，这是参数本身的语义要求；真正的坐标、
    # advance、lineGap 等格式边界由下方实际变换和保存回读逐项验证。
    if outline_scale_percent <= 0:
        raise FontAdjustmentError("轮廓比例必须大于 0")
    if line_percent <= 0:
        raise FontAdjustmentError("行高比例必须大于 0")

    weight_range = ranges.get("weight")
    weight_mode = str(analysis["capabilities"].get("weightMode") or "none")
    apply_weight = False
    if weight_value is not None:
        if not weight_range or weight_mode == "none":
            raise FontAdjustmentError("当前字体不支持粗细调整")
        if weight_mode == "variable" and (
            weight_value < weight_range["min"] or weight_value > weight_range["max"]
        ):
            raise FontAdjustmentError(
                f"粗细值超出该字体自身设计轴：{weight_range['min']}～{weight_range['max']}"
            )
        if weight_mode in {"synthetic-ttf", "synthetic-cff"} and weight_value < weight_range["default"]:
            # 当前静态轮廓算法只做可验证的外扩；内缩会在窄笔画、自交轮廓和复合字形
            # 上产生不可恢复的拓扑问题。数值框不设 HTML 最小值，但核心必须明确拒绝
            # 无法可靠实现的“减细”，不能把它误处理成 1 个字体单位的加粗。
            raise FontAdjustmentError("静态字体暂不支持减细，请输入不小于原始粗细的数值")
        apply_weight = abs(weight_value - weight_range["default"]) > 1e-6

    original_palette_data = None
    if palette_info is not None:
        original_palette_data = _normalized_palette_data(palette_info)
    apply_palette = (
        palette_data is not None
        and palette_data != original_palette_data
    )
    apply_modified_group, apply_unmodified_group, apply_target_group = _color_group_change_flags(
        palette_info,
        color_group_data,
    )
    apply_color_grouping = (
        apply_modified_group
        or apply_unmodified_group
        or apply_target_group
    )

    changed = (
        abs(outline_scale_percent - 100) > 1e-6
        or abs(spacing_percent) > 1e-6
        or abs(line_percent - 100) > 1e-6
        or apply_weight
        or apply_palette
        or apply_color_grouping
        or apply_naming
    )
    if not changed:
        shutil.copyfile(input_path, output_path)
        verification = _validate_output(
            input_path,
            output_path,
            set(analysis["tables"]),
            set(),
            int(analysis["glyphCount"]),
            str(analysis["outlineKind"]),
            False,
        )
        return {
            "ok": True,
            "processorVersion": PROCESSOR_VERSION,
            "changed": False,
            "container": analysis.get("container"),
            "sourceContainer": analysis.get("sourceContainer"),
            "recommendedExtension": analysis.get("recommendedExtension"),
            "applied": {
                "outlineScalePercent": 100,
                "sizePercent": 100,
                "spacingEmPercent": 0,
                "lineHeightPercent": 100,
                "weightValue": weight_range["default"] if weight_range else None,
                "weightMode": weight_mode,
                "colorPaletteChanged": False,
                "paletteEntriesModified": 0,
                "paletteLayersRemapped": 0,
                "unmodifiedGlyphsColored": 0,
                "targetGlyphsColored": 0,
                "targetCharacters": "",
                "missingTargetCharacters": [],
                "skippedModifiedCharacters": [],
                "targetGlyphColorsChanged": False,
                "fontFamilyChanged": False,
                "fontFamily": current_font_naming.get("fontFamily") or "",
            },
            "warnings": analysis["warnings"],
            "verification": verification,
        }

    font = _load_font(input_path)
    input_tables = _table_tags(font)
    warnings = list(analysis["warnings"])
    # 任何字体内容变化都会使数字签名失效；FontTools 的变量实例化过程可能在
    # 到达显式删除逻辑前就移除 DSIG，因此从处理开始就把它列为唯一通用例外。
    allowed_removed_tables: Set[str] = {"DSIG"}
    synthetic_strength = 0
    modified_weight_glyphs = 0
    modified_palette_entries = 0
    remapped_palette_layers = 0
    colored_unmodified_glyphs = 0
    colored_target_glyphs = 0
    target_characters_applied = ""
    missing_target_characters: List[str] = []
    skipped_modified_characters: List[str] = []
    expected_font_naming: Optional[Dict[str, str]] = None
    expected_palette_data = (
        palette_data
        if palette_data is not None
        else original_palette_data if color_group_data is not None else None
    )
    expected_color_grouping: Optional[Dict[str, Any]] = None
    try:
        if apply_weight and weight_mode == "variable":
            font = instantiateVariableFont(
                font,
                {"wght": float(weight_value)},
                inplace=True,
                optimize=True,
                # 任意滑块值未必在 STAT 中有命名实例。强制更新 name 表会让
                # FontTools 对合法的中间值抛错，因此这里只同步 OS/2/head 标志。
                updateFontNames=False,
            )
            allowed_removed_tables |= VARIABLE_TABLES
            _update_weight_metadata(font, float(weight_value))
        elif apply_weight and weight_mode not in {"synthetic-ttf", "synthetic-cff"}:
            raise FontAdjustmentError("当前字体不支持粗细调整")

        if apply_palette:
            # 颜色修改独立于轮廓变换，直接更新全部 CPAL 调色板即可。COLR 图层只保存
            # palette index，不应为了改色重建字形或图层映射。所有调色板共用同一写回
            # 函数，避免只有默认调色板受到严格校验。
            modified_palette_entries = _apply_color_palettes(font, palette_data)
            if modified_palette_entries <= 0:
                raise FontAdjustmentError("彩色调色板参数未实际改变字体内容")

        if apply_color_grouping:
            # 分组调色不仅修改 CPAL，还可能复制共享 palette index、重映射 COLR
            # LayerRecord，并把普通打底字/指定文字变成引用自身轮廓的单色 COLR 字形。
            # 所有动作必须在同一个 TTFont 保存周期内完成，避免任何中间状态被交付。
            group_result = _apply_color_group_edits(font, color_group_data)
            modified_palette_entries += int(group_result["paletteEntriesModified"])
            remapped_palette_layers = int(group_result["paletteLayersRemapped"])
            colored_unmodified_glyphs = int(group_result["unmodifiedGlyphsColored"])
            colored_target_glyphs = int(group_result.get("targetGlyphsColored") or 0)
            target_characters_applied = str(group_result.get("targetCharacters") or "")
            missing_target_characters = list(group_result.get("missingTargetCharacters") or [])
            skipped_modified_characters = list(group_result.get("skippedModifiedCharacters") or [])
            for warning in group_result.get("warnings") or []:
                if warning and warning not in warnings:
                    warnings.append(str(warning))
            expected_palette_data = group_result["paletteData"]
            # 指定文字会从“未修改打底字”里拆出独立 palette。此时整组打底字不再统一，
            # 不能继续用 unmodifiedUniformColors 做整组一致性校验；只在没有指定字时校验。
            expected_color_grouping = {
                **group_result["grouping"],
                "expectedUnmodifiedUniformColors": (
                    None
                    if group_result.get("targetUniformColors") is not None
                    else group_result["unmodifiedUniformColors"]
                ),
            }
            if (
                modified_palette_entries <= 0
                and remapped_palette_layers <= 0
                and colored_unmodified_glyphs <= 0
                and colored_target_glyphs <= 0
            ):
                raise FontAdjustmentError("字形分组调色参数未实际改变字体内容")

        if apply_naming:
            # 名称与轮廓、颜色互不依赖，但必须在同一次 FontTools 保存中完成，才能让
            # name/CFF/变量实例名称和最终校验共享一个原子成品，避免前端二次改表。
            expected_font_naming = _apply_font_naming(font, requested_family_name)

        if abs(outline_scale_percent - 100) > 1e-6:
            if not analysis["capabilities"]["outlineScale"]:
                raise FontAdjustmentError("该字体包含不安全支持的图形表，不能调整轮廓比例")
            original_upem = int(font["head"].unitsPerEm)
            if "CFF " in font:
                # fontTools 对未显式写入 FontMatrix 的 CFF 使用 cffLib 的共享默认
                # list。ScalerVisitor 会原地除以缩放因子；若不先复制，当前任务会污染
                # 同一 Worker/Python 进程中后续字体的默认 FontMatrix，甚至让独立裸 CFF
                # fixture 解析成 0.000909… 这样的错误 unitsPerEm。
                for top_dict in font["CFF "].cff.topDictIndex:
                    matrix = getattr(top_dict, "FontMatrix", None)
                    if isinstance(matrix, list):
                        top_dict.FontMatrix = list(matrix)
            ScalerVisitor(outline_scale_percent / 100.0).visit(font)
            # 统一轮廓比例等价于常见字体编辑器的 Scale(p, p)：水平和垂直始终使用
            # 同一个百分比。ScalerVisitor 会同步轮廓、度量及包含坐标的 OpenType 表，
            # 避免只缩放 glyf/CFF 后造成组合字形、字距或布局定位失配；最后恢复 UPEM，
            # 得到“字号基准不变、轮廓与相关坐标真实等比缩放”的结果。
            font["head"].unitsPerEm = original_upem
            if "glyf" in font:
                # ScalerVisitor 会缩放点坐标，但不会重写 TrueType 字节码、CVT 和
                # 设备度量；继续保留会造成小字号错误，因此轮廓变化也统一去提示。
                allowed_removed_tables |= _drop_truetype_hinting(font)

        spacing_delta = _apply_spacing(font, spacing_percent)

        if apply_weight and weight_mode in {"synthetic-ttf", "synthetic-cff"}:
            original_outline_bounds = (
                int(font["head"].xMin),
                int(font["head"].yMin),
                int(font["head"].xMax),
                int(font["head"].yMax),
            )
            base_strength = _synthetic_weight_strength(
                font,
                float(weight_value),
                float(weight_range["default"]),
            )
            # 先做轮廓缩放，再按相同倍数应用合成粗细。均匀缩放与轮廓外扩在
            # 几何上可交换，这个顺序还能让 CFF 重建时一次写入最终 hmtx 字宽，
            # 避免轮廓比例或间距随后改变度量而使 CharString width 再次失配。
            synthetic_strength = max(1, otRound(base_strength * outline_scale_percent / 100.0))
            _apply_synthetic_weight_metrics(font, synthetic_strength, synthetic_strength)
            if weight_mode == "synthetic-ttf":
                modified_weight_glyphs = _embolden_truetype(font, synthetic_strength)
                allowed_removed_tables |= _drop_truetype_hinting(font)
            else:
                modified_weight_glyphs = _embolden_cff(font, synthetic_strength)
            if modified_weight_glyphs <= 0:
                raise FontAdjustmentError("当前字体没有可调整粗细的有效字形轮廓")
            _apply_outline_growth_metrics(font, original_outline_bounds)
            _update_weight_metadata(font, float(weight_value))

        _apply_line_height(font, line_percent)

        if "DSIG" in font:
            del font["DSIG"]
            allowed_removed_tables.add("DSIG")

        try:
            font.save(output_path, reorderTables=False)
        except Exception as error:
            raise FontAdjustmentError(f"字体编译失败，参数可能超出格式上限：{error}") from error
    finally:
        font.close()

    verification = _validate_output(
        input_path,
        output_path,
        input_tables,
        allowed_removed_tables,
        int(analysis["glyphCount"]),
        str(analysis["outlineKind"]),
        True,
        weight_mode == "synthetic-cff" and apply_weight,
        # 页面提交了调色板数据时，即使颜色本身没变、只调整了轮廓比例或粗细，也要验证
        # FontTools 保存后所有调色板仍与输入一致。
        expected_palette_data,
        expected_font_naming,
        expected_color_grouping,
    )
    return {
        "ok": True,
        "processorVersion": PROCESSOR_VERSION,
        "changed": True,
        "container": analysis.get("container"),
        "sourceContainer": analysis.get("sourceContainer"),
        "recommendedExtension": analysis.get("recommendedExtension"),
        "applied": {
            "outlineScalePercent": outline_scale_percent,
            "sizePercent": outline_scale_percent,
            "spacingEmPercent": spacing_percent,
            "spacingFontUnits": spacing_delta,
            "lineHeightPercent": line_percent,
            "weightValue": weight_value if weight_range else None,
            "weightMode": weight_mode,
            "syntheticWeightStrength": synthetic_strength,
            "weightGlyphsModified": modified_weight_glyphs,
            "colorPaletteChanged": apply_palette or apply_color_grouping,
            "paletteEntriesModified": modified_palette_entries,
            "paletteLayersRemapped": remapped_palette_layers,
            "unmodifiedGlyphsColored": colored_unmodified_glyphs,
            "targetGlyphsColored": colored_target_glyphs,
            "targetCharacters": target_characters_applied,
            "missingTargetCharacters": missing_target_characters,
            "skippedModifiedCharacters": skipped_modified_characters,
            "modifiedGlyphColorsChanged": apply_modified_group,
            "unmodifiedGlyphColorsChanged": apply_unmodified_group,
            "targetGlyphColorsChanged": apply_target_group,
            "fontFamilyChanged": apply_naming,
            "fontFamily": (
                expected_font_naming["fontFamily"]
                if expected_font_naming is not None
                else current_font_naming.get("fontFamily") or ""
            ),
        },
        "warnings": warnings,
        "verification": verification,
    }


def _process(input_path: str, output_path: str, options: Dict[str, Any]) -> Dict[str, Any]:
    filename = str(options.get("filename") or "font")
    with _prepared_font_path(input_path, filename) as (
        prepared_path,
        container,
        source_container,
        adapter_warnings,
        source_size,
    ):
        analysis = _analyze_prepared(
            prepared_path,
            filename,
            container=container,
            source_container=source_container,
            adapter_warnings=adapter_warnings,
            file_size=source_size,
        )
        return _process_prepared(prepared_path, output_path, options, analysis)


def process_font_file_json(input_path: str, output_path: str, options_json: str) -> str:
    try:
        options = json.loads(options_json or "{}")
        return json.dumps(_process(input_path, output_path, options), ensure_ascii=False)
    except FontAdjustmentError as error:
        try:
            if os.path.exists(output_path):
                os.remove(output_path)
        except OSError:
            pass
        return json.dumps({"ok": False, "error": str(error)}, ensure_ascii=False)
    except Exception as error:
        try:
            if os.path.exists(output_path):
                os.remove(output_path)
        except OSError:
            pass
        return json.dumps({"ok": False, "error": f"字体处理失败：{error}"}, ensure_ascii=False)
