"""受限裸 CFF1 读取器与 OTTO 封装器。

裸 CFF 没有 SFNT 外围表，不能直接交给 :class:`fontTools.ttLib.TTFont`。
本模块只实现统一字体链路需要的安全子集：CFF1、单一 Top DICT、非 CID、
没有 FDArray/FDSelect 的字体。输入先经过独立的边界/资源检查，再由
``fontTools.cffLib`` 读取语义，最后集中构造真正的 ``OTTO + CFF `` 字体。

这里不把扩展名当作格式事实，也不返回输入 ``bytes`` 的 view。所有成功结果
都经过 TTFont 保存、重新加载、CFF 重读、字形顺序、宽度和轮廓回读验证。
"""

from __future__ import annotations

from dataclasses import dataclass
from io import BytesIO
import math
import re
from pathlib import Path
import unicodedata
from typing import Any, Iterable, Literal, Optional, Sequence

from fontTools import agl, cffLib
from fontTools.fontBuilder import FontBuilder
from fontTools.misc.fixedTools import otRound
from fontTools.pens.basePen import NullPen
from fontTools.pens.boundsPen import BoundsPen
from fontTools.ttLib import TTFont, TTLibError, newTable


# 稳定错误码由 Server、Runtime 和外部 asset-marker 共同使用。调用方可以按
# ``BareCffError.code`` 分类，而不必把 fontTools 的内部异常文本暴露给用户。
BARE_CFF_INVALID_HEADER = "BARE_CFF_INVALID_HEADER"
BARE_CFF_UNSUPPORTED_VERSION = "BARE_CFF_UNSUPPORTED_VERSION"
BARE_CFF_INDEX_TRUNCATED = "BARE_CFF_INDEX_TRUNCATED"
BARE_CFF_INDEX_INVALID_OFFSET = "BARE_CFF_INDEX_INVALID_OFFSET"
BARE_CFF_TOP_DICT_COUNT = "BARE_CFF_TOP_DICT_COUNT"
BARE_CFF_CID_KEYED = "BARE_CFF_CID_KEYED"
BARE_CFF_FD_ARRAY_UNSUPPORTED = "BARE_CFF_FD_ARRAY_UNSUPPORTED"
BARE_CFF_FD_SELECT_UNSUPPORTED = "BARE_CFF_FD_SELECT_UNSUPPORTED"
BARE_CFF_MISSING_CHARSTRINGS = "BARE_CFF_MISSING_CHARSTRINGS"
BARE_CFF_CHARSET_INVALID = "BARE_CFF_CHARSET_INVALID"
BARE_CFF_PRIVATE_INVALID = "BARE_CFF_PRIVATE_INVALID"
BARE_CFF_CHARSTRING_INVALID = "BARE_CFF_CHARSTRING_INVALID"
BARE_CFF_WIDTH_INVALID = "BARE_CFF_WIDTH_INVALID"
BARE_CFF_UNSUPPORTED_FONT_MATRIX = "BARE_CFF_UNSUPPORTED_FONT_MATRIX"
BARE_CFF_RESOURCE_LIMIT = "BARE_CFF_RESOURCE_LIMIT"
BARE_CFF_CMAP_REQUIRED = "BARE_CFF_CMAP_REQUIRED"
BARE_CFF_WRAP_FAILED = "BARE_CFF_WRAP_FAILED"

# 补充的结构分类。它们仍然是稳定、脱敏的错误码；上面列出的公共码保持不变。
BARE_CFF_DICT_INVALID = "BARE_CFF_DICT_INVALID"
BARE_CFF_NAME_INVALID = "BARE_CFF_NAME_INVALID"
BARE_CFF_BBOX_INVALID = "BARE_CFF_BBOX_INVALID"


@dataclass(frozen=True)
class BareCffLimits:
    """裸 CFF 解析和封装的集中资源门禁。

    INDEX 的数量字段本身是 card16，但仍保留更严格的产品预算，以防恶意输入
    在进入 fontTools 前制造大量 Python 对象。数值是单任务上限，不是数据库或
    进程全局状态。
    """

    max_input_bytes: int = 50 * 1024 * 1024
    max_glyph_count: int = 65535
    max_index_count: int = 65535
    max_charstring_bytes: int = 4 * 1024 * 1024
    max_total_charstring_bytes: int = 32 * 1024 * 1024
    max_private_bytes: int = 1024 * 1024
    max_subr_bytes: int = 8 * 1024 * 1024
    max_type2_ops: int = 1_000_000
    max_type2_calls: int = 100_000
    max_type2_recursion_depth: int = 32
    max_type2_stack: int = 48
    max_output_bytes: int = 100 * 1024 * 1024

    # 这些只读别名方便不同入口按统一契约命名，不增加第二套配置。
    @property
    def max_glyphs(self) -> int:
        return self.max_glyph_count

    @property
    def max_calls(self) -> int:
        return self.max_type2_calls

    @property
    def max_call_depth(self) -> int:
        return self.max_type2_recursion_depth


@dataclass(frozen=True)
class BareCffCapabilityReport:
    container: Literal["bare-cff1"]
    output_container: Literal["otf"]
    cff_version: Literal["cff1"]
    single_top_dict: bool
    cid_keyed: bool
    fd_array: bool
    fd_select: bool
    glyph_count: int
    cmap: Literal["reliable", "partial", "unavailable"]
    cmap_action: Literal["mapped", "preserved-all-glyphs", "rejected"]
    warnings: list[str]
    save_verified: bool
    # 扩展字段用于跨语言语义快照；不改变上面的统一必需字段。
    units_per_em: int = 1000
    font_matrix: tuple[float, ...] = (0.001, 0.0, 0.0, 0.001, 0.0, 0.0)
    glyph_order: tuple[str, ...] = ()
    metrics: dict[str, tuple[int, int]] | None = None
    font_bbox: tuple[int, int, int, int] = (0, 0, 0, 0)
    cmap_map: dict[int, str] | None = None
    outline_bounds: dict[str, tuple[int, int, int, int] | None] | None = None

    def to_dict(self) -> dict[str, Any]:
        """返回适合 summary/跨语言 fixture 的普通 JSON 数据。"""
        return {
            "container": self.container,
            "output_container": self.output_container,
            "cff_version": self.cff_version,
            "single_top_dict": self.single_top_dict,
            "cid_keyed": self.cid_keyed,
            "fd_array": self.fd_array,
            "fd_select": self.fd_select,
            "glyph_count": self.glyph_count,
            "cmap": self.cmap,
            "cmap_action": self.cmap_action,
            "warnings": list(self.warnings),
            "save_verified": self.save_verified,
            "units_per_em": self.units_per_em,
            "font_matrix": list(self.font_matrix),
            "glyph_order": list(self.glyph_order),
            "metrics": {
                name: [int(values[0]), int(values[1])]
                for name, values in (self.metrics or {}).items()
            },
            "font_bbox": list(self.font_bbox),
            "cmap_map": {str(code): name for code, name in (self.cmap_map or {}).items()},
            "outline_bounds": {
                name: list(b)
                for name, b in (self.outline_bounds or {}).items()
                if b is not None
            },
        }


@dataclass(frozen=True)
class BareCffNormalization:
    content: bytes
    output_filename: str
    report: BareCffCapabilityReport
    source_container: Literal["bare-cff1"] = "bare-cff1"
    output_container: Literal["otf"] = "otf"


class BareCffError(ValueError):
    """可安全跨服务传递的裸 CFF 错误。"""

    def __init__(self, code: str, message: str):
        self.code = str(code)
        # message 只由本模块生成，不拼接原始数据、路径或用户身份字段。
        self.message = str(message)
        super().__init__(f"{self.code}: {self.message}")


@dataclass(frozen=True)
class _IndexData:
    offset: int
    end: int
    count: int
    off_size: int
    items: tuple[bytes, ...]


@dataclass(frozen=True)
class _DictData:
    values: dict[tuple[int, ...], tuple[int | float, ...]]


@dataclass(frozen=True)
class _ParsedBareCff:
    raw: bytes
    name_index: _IndexData
    top_index: _IndexData
    string_index: _IndexData
    global_subrs: _IndexData
    top_dict: _DictData
    private_dict: _DictData
    local_subrs: _IndexData
    charstrings: _IndexData
    glyph_order: tuple[str, ...]
    font_matrix: tuple[float, ...]
    units_per_em: int
    source_name: str
    source_font: TTFont
    cff: cffLib.CFFFontSet
    top: Any
    private: Any
    charstrings_object: Any


# CFF DICT 运算符由 fontTools 公共模块导出；在这里建立 opcode -> 名称映射，
# 避免依赖某个私有 parser 的异常容错行为。
def _operator_map(operators: Iterable[tuple[Any, str, Any, Any, Any]]) -> dict[tuple[int, ...], str]:
    result: dict[tuple[int, ...], str] = {}
    for opcode, name, *_ in operators:
        key = (opcode,) if isinstance(opcode, int) else tuple(opcode)
        result[key] = str(name)
    return result


_TOP_OPERATOR_NAMES = _operator_map(cffLib.topDictOperators)
_PRIVATE_OPERATOR_NAMES = _operator_map(cffLib.privateDictOperators)


def _type2_operator_map() -> dict[tuple[int, ...], str]:
    result: dict[tuple[int, ...], str] = {}
    from fontTools.misc.psCharStrings import t2Operators

    for opcode, name in t2Operators:
        key = (opcode,) if isinstance(opcode, int) else tuple(opcode)
        result[key] = str(name)
    return result


_T2_OPERATOR_NAMES = _type2_operator_map()


def _error(code: str, message: str) -> None:
    raise BareCffError(code, message)


def _as_bytes(content: bytes | bytearray | memoryview) -> bytes:
    if isinstance(content, bytes):
        return bytes(content)
    if isinstance(content, (bytearray, memoryview)):
        return bytes(content)
    _error(BARE_CFF_INVALID_HEADER, "裸 CFF 输入不是二进制数据")


def is_bare_cff1(content: bytes | bytearray | memoryview) -> bool:
    """仅按 CFF1 header 判断输入是否可能是裸 CFF1。"""
    try:
        raw = _as_bytes(content)
    except BareCffError:
        return False
    if len(raw) < 4 or raw[0] != 1:
        return False
    return 4 <= raw[2] <= len(raw) and 1 <= raw[3] <= 4


def _read_uint(data: bytes, offset: int, size: int, code: str) -> int:
    if size < 0 or offset < 0 or offset + size > len(data):
        _error(code, "结构数据被截断")
    return int.from_bytes(data[offset : offset + size], "big", signed=False)


def _read_int(data: bytes, offset: int, size: int, code: str) -> int:
    if size < 0 or offset < 0 or offset + size > len(data):
        _error(code, "结构数据被截断")
    return int.from_bytes(data[offset : offset + size], "big", signed=True)


def _parse_index(
    data: bytes,
    offset: int,
    *,
    label: str,
    limits: BareCffLimits,
) -> _IndexData:
    """严格读取一个 CFF1 INDEX（offset 是 INDEX 起始位置）。"""
    if offset < 0 or offset + 2 > len(data):
        _error(BARE_CFF_INDEX_TRUNCATED, f"{label} INDEX 头部被截断")
    count = _read_uint(data, offset, 2, BARE_CFF_INDEX_TRUNCATED)
    if count > limits.max_index_count:
        _error(BARE_CFF_RESOURCE_LIMIT, f"{label} INDEX 数量超限")
    if count == 0:
        return _IndexData(offset, offset + 2, 0, 0, ())

    if offset + 3 > len(data):
        _error(BARE_CFF_INDEX_TRUNCATED, f"{label} INDEX 缺少 OffSize")
    off_size = data[offset + 2]
    if off_size < 1 or off_size > 4:
        _error(BARE_CFF_INDEX_INVALID_OFFSET, f"{label} INDEX OffSize 无效")
    offsets_start = offset + 3
    offsets_end = offsets_start + (count + 1) * off_size
    if offsets_end > len(data):
        _error(BARE_CFF_INDEX_TRUNCATED, f"{label} INDEX offset 数组被截断")

    offsets = [
        int.from_bytes(data[pos : pos + off_size], "big", signed=False)
        for pos in range(offsets_start, offsets_end, off_size)
    ]
    if not offsets or offsets[0] != 1:
        _error(BARE_CFF_INDEX_INVALID_OFFSET, f"{label} INDEX 首 offset 不是 1")
    if any(value < 1 for value in offsets) or any(
        right < left for left, right in zip(offsets, offsets[1:])
    ):
        _error(BARE_CFF_INDEX_INVALID_OFFSET, f"{label} INDEX offset 不单调")

    offset_base = offsets_end - 1
    data_end = offset_base + offsets[-1]
    if data_end < offsets_end or data_end > len(data):
        _error(BARE_CFF_INDEX_INVALID_OFFSET, f"{label} INDEX 数据越过输入边界")

    items: list[bytes] = []
    for index in range(count):
        start = offset_base + offsets[index]
        end = offset_base + offsets[index + 1]
        if start < offsets_end or end < start or end > data_end:
            _error(BARE_CFF_INDEX_INVALID_OFFSET, f"{label} INDEX 对象边界无效")
        items.append(bytes(data[start:end]))
    return _IndexData(offset, data_end, count, off_size, tuple(items))


def _decode_real(data: bytes, offset: int) -> tuple[float, int]:
    text: list[str] = []
    terminated = False
    position = offset + 1
    while position < len(data):
        byte = data[position]
        position += 1
        for nibble in (byte >> 4, byte & 0x0F):
            if nibble == 0x0F:
                terminated = True
                break
            if nibble <= 9:
                text.append(str(nibble))
            elif nibble == 0x0A:
                text.append(".")
            elif nibble == 0x0B:
                text.append("E")
            elif nibble == 0x0C:
                text.append("E-")
            elif nibble == 0x0E:
                text.append("-")
            else:
                _error(BARE_CFF_DICT_INVALID, "DICT real 数字包含无效 nibble")
        if terminated:
            break
    if not terminated or not text:
        _error(BARE_CFF_DICT_INVALID, "DICT real 数字被截断")
    value_text = "".join(text)
    try:
        value = float(value_text)
    except (TypeError, ValueError) as exc:
        raise BareCffError(BARE_CFF_DICT_INVALID, "DICT real 数字无效") from exc
    if not math.isfinite(value):
        _error(BARE_CFF_DICT_INVALID, "DICT real 数字不是有限值")
    return value, position


def _decode_dict_number(data: bytes, offset: int) -> tuple[int | float, int] | None:
    if offset >= len(data):
        return None
    b0 = data[offset]
    if 32 <= b0 <= 246:
        return b0 - 139, offset + 1
    if 247 <= b0 <= 250:
        if offset + 1 >= len(data):
            _error(BARE_CFF_DICT_INVALID, "DICT 整数被截断")
        return (b0 - 247) * 256 + data[offset + 1] + 108, offset + 2
    if 251 <= b0 <= 254:
        if offset + 1 >= len(data):
            _error(BARE_CFF_DICT_INVALID, "DICT 整数被截断")
        return -(b0 - 251) * 256 - data[offset + 1] - 108, offset + 2
    if b0 == 28:
        return _read_int(data, offset + 1, 2, BARE_CFF_DICT_INVALID), offset + 3
    if b0 == 29:
        return _read_int(data, offset + 1, 4, BARE_CFF_DICT_INVALID), offset + 5
    if b0 == 30:
        return _decode_real(data, offset)
    if b0 == 255:
        fixed = _read_int(data, offset + 1, 4, BARE_CFF_DICT_INVALID)
        value = fixed / 65536.0
        if not math.isfinite(value):
            _error(BARE_CFF_DICT_INVALID, "DICT fixed 数字不是有限值")
        return value, offset + 5
    return None


def _parse_dict(data: bytes, *, label: str, operators: dict[tuple[int, ...], str]) -> _DictData:
    values: dict[tuple[int, ...], tuple[int | float, ...]] = {}
    stack: list[int | float] = []
    offset = 0
    while offset < len(data):
        number = _decode_dict_number(data, offset)
        if number is not None:
            value, offset = number
            stack.append(value)
            if len(stack) > 48:
                _error(BARE_CFF_DICT_INVALID, f"{label} DICT 操作数栈超限")
            continue

        b0 = data[offset]
        if b0 == 12:
            if offset + 1 >= len(data):
                _error(BARE_CFF_DICT_INVALID, f"{label} DICT 双字节 operator 被截断")
            key = (12, data[offset + 1])
            offset += 2
        else:
            key = (b0,)
            offset += 1
        if key not in operators:
            _error(BARE_CFF_DICT_INVALID, f"{label} DICT 含不支持的 operator")
        if key in values:
            _error(BARE_CFF_DICT_INVALID, f"{label} DICT 含重复 operator")
        values[key] = tuple(stack)
        stack.clear()
    if stack:
        _error(BARE_CFF_DICT_INVALID, f"{label} DICT 末尾存在未消费操作数")
    return _DictData(values)


def _values(values: dict[tuple[int, ...], tuple[int | float, ...]], key: tuple[int, ...]) -> tuple[int | float, ...] | None:
    return values.get(key)


def _require_one_int(
    values: dict[tuple[int, ...], tuple[int | float, ...]],
    key: tuple[int, ...],
    *,
    code: str,
    label: str,
    minimum: int | None = None,
) -> int:
    raw = values.get(key)
    if raw is None or len(raw) != 1:
        _error(code, f"{label} 参数数量无效")
    value = raw[0]
    if not math.isfinite(float(value)) or abs(float(value) - round(float(value))) > 1e-6:
        _error(code, f"{label} 必须是整数")
    result = int(round(float(value)))
    if minimum is not None and result < minimum:
        _error(code, f"{label} 越界")
    return result


def _optional_int(
    values: dict[tuple[int, ...], tuple[int | float, ...]],
    key: tuple[int, ...],
    *,
    code: str,
    label: str,
    default: int = 0,
) -> int:
    raw = values.get(key)
    if raw is None:
        return default
    if len(raw) != 1:
        _error(code, f"{label} 参数数量无效")
    value = raw[0]
    if not math.isfinite(float(value)) or abs(float(value) - round(float(value))) > 1e-6:
        _error(code, f"{label} 必须是整数")
    return int(round(float(value)))


def _sid_name(sid: int, string_index: _IndexData) -> str:
    standard = cffLib.cffStandardStrings
    if sid < 0:
        _error(BARE_CFF_CHARSET_INVALID, "Charset SID 越界")
    if sid < len(standard):
        return str(standard[sid])
    custom_index = sid - len(standard)
    if custom_index < 0 or custom_index >= string_index.count:
        _error(BARE_CFF_CHARSET_INVALID, "Charset SID 不存在")
    try:
        return string_index.items[custom_index].decode("latin-1")
    except UnicodeDecodeError as exc:
        raise BareCffError(BARE_CFF_CHARSET_INVALID, "Charset 字符串无法解析") from exc


def _parse_charset(
    data: bytes,
    offset: int,
    glyph_count: int,
    string_index: _IndexData,
) -> tuple[str, ...]:
    if glyph_count < 1:
        _error(BARE_CFF_CHARSET_INVALID, "Charset 至少需要 .notdef")
    # 0、1、2 是 CFF 预定义 Charset 编号，不是数据偏移；MVP 明确拒绝，
    # 避免不同实现对预定义表和自定义 SID 的解释不一致。
    if offset in (0, 1, 2):
        _error(BARE_CFF_CHARSET_INVALID, "不支持预定义 Charset")
    if offset < 0 or offset >= len(data):
        _error(BARE_CFF_CHARSET_INVALID, "Charset offset 越过输入边界")
    fmt = data[offset]
    cursor = offset + 1
    sids: list[int] = []
    if fmt == 0:
        end = cursor + max(0, glyph_count - 1) * 2
        if end > len(data):
            _error(BARE_CFF_CHARSET_INVALID, "Charset format 0 被截断")
        for _ in range(glyph_count - 1):
            sids.append(_read_uint(data, cursor, 2, BARE_CFF_CHARSET_INVALID))
            cursor += 2
    elif fmt in (1, 2):
        while len(sids) < glyph_count - 1:
            if cursor + 2 > len(data):
                _error(BARE_CFF_CHARSET_INVALID, "Charset range 被截断")
            first_sid = _read_uint(data, cursor, 2, BARE_CFF_CHARSET_INVALID)
            cursor += 2
            if fmt == 1:
                if cursor >= len(data):
                    _error(BARE_CFF_CHARSET_INVALID, "Charset format 1 range 被截断")
                n_left = data[cursor]
                cursor += 1
            else:
                if cursor + 2 > len(data):
                    _error(BARE_CFF_CHARSET_INVALID, "Charset format 2 range 被截断")
                n_left = _read_uint(data, cursor, 2, BARE_CFF_CHARSET_INVALID)
                cursor += 2
            count = n_left + 1
            if len(sids) + count > glyph_count - 1:
                _error(BARE_CFF_CHARSET_INVALID, "Charset range 超过字形数量")
            sids.extend(first_sid + index for index in range(count))
    else:
        _error(BARE_CFF_CHARSET_INVALID, "Charset 格式不受支持")

    names = [".notdef", *(_sid_name(sid, string_index) for sid in sids)]
    if names[0] != ".notdef" or any(not name for name in names):
        _error(BARE_CFF_CHARSET_INVALID, "Charset 缺少合法 .notdef 或 glyph name")
    if len(set(names)) != len(names):
        _error(BARE_CFF_CHARSET_INVALID, "Charset 含重复 glyph name")
    return tuple(names)


def _finite_number(value: Any, *, code: str, label: str) -> float:
    try:
        result = float(value)
    except (TypeError, ValueError) as exc:
        raise BareCffError(code, f"{label} 不是数字") from exc
    if not math.isfinite(result):
        _error(code, f"{label} 不是有限值")
    return result


def _font_matrix_and_upem(top_values: _DictData) -> tuple[tuple[float, ...], int]:
    key = (12, 7)
    raw = top_values.values.get(key)
    if raw is None:
        matrix = (0.001, 0.0, 0.0, 0.001, 0.0, 0.0)
    else:
        if len(raw) != 6:
            _error(BARE_CFF_UNSUPPORTED_FONT_MATRIX, "FontMatrix 必须包含六个数")
        matrix = tuple(
            _finite_number(value, code=BARE_CFF_UNSUPPORTED_FONT_MATRIX, label="FontMatrix")
            for value in raw
        )
    a, b, c, d, e, f = matrix
    if any(abs(value) > 1e-12 for value in (b, c, e, f)):
        _error(BARE_CFF_UNSUPPORTED_FONT_MATRIX, "FontMatrix 含 shear 或 translation")
    if a <= 0 or d <= 0 or abs(a - d) > max(1e-9, abs(a) * 1e-6):
        _error(BARE_CFF_UNSUPPORTED_FONT_MATRIX, "FontMatrix 必须是正向统一缩放")
    reciprocal = 1.0 / a
    reciprocal_tolerance = max(1e-5, abs(reciprocal) * 1e-6)
    if not math.isfinite(reciprocal) or abs(reciprocal - round(reciprocal)) > reciprocal_tolerance:
        _error(BARE_CFF_UNSUPPORTED_FONT_MATRIX, "FontMatrix 无法推导整数 unitsPerEm")
    units_per_em = int(round(reciprocal))
    if units_per_em < 16 or units_per_em > 16384:
        _error(BARE_CFF_UNSUPPORTED_FONT_MATRIX, "FontMatrix 推导的 unitsPerEm 越界")
    return tuple(float(value) for value in matrix), units_per_em


def _safe_int(value: float, *, code: str, label: str, minimum: int, maximum: int) -> int:
    if not math.isfinite(value):
        _error(code, f"{label} 不是有限值")
    rounded = int(otRound(value))
    if abs(value - rounded) > 1e-4 and code not in {BARE_CFF_BBOX_INVALID}:
        _error(code, f"{label} 不是可表示的整数")
    if rounded < minimum or rounded > maximum:
        _error(code, f"{label} 越界")
    return rounded


def _safe_text(value: Any, fallback: str, *, max_length: int = 128) -> tuple[str, bool]:
    text = unicodedata.normalize("NFC", str(value or ""))
    text = re.sub(r"[\x00-\x1f\x7f]", " ", text)
    text = re.sub(r"\s+", " ", text).strip()
    if len(text) > max_length:
        text = text[:max_length].rstrip()
    if not text:
        return fallback, True
    return text, False


def _safe_postscript(value: str, fallback: str) -> str:
    text = unicodedata.normalize("NFKD", value)
    text = re.sub(r"[̀-ͯ]", "", text)
    text = re.sub(r"[^\x21-\x7e]", "-", text)
    text = re.sub(r"[\[\](){}<>/%]", "-", text)
    text = re.sub(r"-+", "-", text).strip("-")
    text = text[:63].rstrip("-")
    return text or fallback


def _safe_output_filename(filename: str) -> str:
    text = str(filename or "font.cff").replace("\\", "/").split("/")[-1]
    text = re.sub(r"[\x00-\x1f\x7f\"<>|:*?]", "_", text).strip(" .")
    if not text:
        text = "font.cff"
    stem = Path(text).stem or "font"
    stem = re.sub(r"\s+", " ", stem).strip() or "font"
    return f"{stem}.otf"


def _type2_number(data: bytes, offset: int) -> tuple[int | float, int] | None:
    if offset >= len(data):
        return None
    b0 = data[offset]
    if 32 <= b0 <= 246:
        return b0 - 139, offset + 1
    if 247 <= b0 <= 250:
        if offset + 1 >= len(data):
            _error(BARE_CFF_CHARSTRING_INVALID, "Type2 整数被截断")
        return (b0 - 247) * 256 + data[offset + 1] + 108, offset + 2
    if 251 <= b0 <= 254:
        if offset + 1 >= len(data):
            _error(BARE_CFF_CHARSTRING_INVALID, "Type2 整数被截断")
        return -(b0 - 251) * 256 - data[offset + 1] - 108, offset + 2
    if b0 == 28:
        return _read_int(data, offset + 1, 2, BARE_CFF_CHARSTRING_INVALID), offset + 3
    # Type2 没有 DICT 的 real/fixed 编码；255 是 16.16 fixed，接受它并在
    # 轮廓执行阶段要求有限值，避免把合法 fixed 操作数误判为 operator。
    if b0 == 255:
        fixed = _read_int(data, offset + 1, 4, BARE_CFF_CHARSTRING_INVALID)
        value = fixed / 65536.0
        if not math.isfinite(value):
            _error(BARE_CFF_CHARSTRING_INVALID, "Type2 fixed 数字不是有限值")
        return value, offset + 5
    return None


def _subr_bias(count: int) -> int:
    if count < 1240:
        return 107
    if count < 33900:
        return 1131
    return 32768


def _to_stack_int(value: Any) -> int:
    numeric = _finite_number(value, code=BARE_CFF_CHARSTRING_INVALID, label="Type2 栈值")
    if abs(numeric - round(numeric)) > 1e-6:
        _error(BARE_CFF_CHARSTRING_INVALID, "Type2 subr 索引必须是整数")
    return int(round(numeric))


class _Type2Validator:
    """受预算约束的 Type2 结构检查器。

    fontTools 负责真正的 CharString 轮廓语义；这里负责在其之前检查数字、栈、
    hint mask、subr bias、递归和结束操作。算术栈运算只做结构级执行，若目标
    fontTools 轮廓提取器不能安全绘制，外层仍会 fail closed。
    """

    _STEM_OPS = {"hstem", "vstem", "hstemhm", "vstemhm"}
    _MOVETO_OPS = {"rmoveto", "hmoveto", "vmoveto"}
    _PATH_OPS = {
        "rlineto", "hlineto", "vlineto", "rrcurveto", "rcurveline",
        "rlinecurve", "vvcurveto", "hhcurveto", "vhcurveto", "hvcurveto",
        "hflex", "flex", "hflex1", "flex1",
    }

    def __init__(
        self,
        *,
        local_subrs: tuple[bytes, ...],
        global_subrs: tuple[bytes, ...],
        limits: BareCffLimits,
    ):
        self.local_subrs = local_subrs
        self.global_subrs = global_subrs
        self.limits = limits
        self.ops = 0
        self.calls = 0
        self.stack: list[int | float] = []
        self.transient: list[int | float] = [0] * 32
        self.width_seen = False
        self.hint_count = 0
        self.path_seen = False
        self._active_subrs: set[tuple[str, int]] = set()

    def _check_stack_limit(self) -> None:
        if len(self.stack) > self.limits.max_type2_stack:
            _error(BARE_CFF_CHARSTRING_INVALID, "Type2 栈深度超限")

    def _budget_op(self) -> None:
        self.ops += 1
        if self.ops > self.limits.max_type2_ops:
            _error(BARE_CFF_RESOURCE_LIMIT, "Type2 operator 数量超限")
        self._check_stack_limit()

    def _pop(self, count: int) -> list[int | float]:
        if count < 0 or len(self.stack) < count:
            _error(BARE_CFF_CHARSTRING_INVALID, "Type2 栈下溢")
        if count == 0:
            return []
        result = self.stack[-count:]
        del self.stack[-count:]
        return result

    def _consume_width(self, kind: str) -> None:
        if self.width_seen:
            return
        count = len(self.stack)
        explicit = False
        if kind in ("stem", "rmoveto"):
            explicit = bool(count % 2)
        elif kind in ("hmoveto", "vmoveto"):
            explicit = not bool(count % 2)
        if explicit:
            if not self.stack:
                _error(BARE_CFF_WIDTH_INVALID, "Type2 显式字宽缺失")
            self.stack.pop(0)
        self.width_seen = True

    def _check_args(self, name: str) -> None:
        count = len(self.stack)
        if name in self._STEM_OPS:
            self._consume_width("stem")
            if len(self.stack) < 2 or len(self.stack) % 2:
                _error(BARE_CFF_CHARSTRING_INVALID, "Type2 hint 参数数量无效")
        elif name == "rmoveto":
            self._consume_width("rmoveto")
            if len(self.stack) < 2 or len(self.stack) % 2:
                _error(BARE_CFF_CHARSTRING_INVALID, "Type2 rmoveto 参数数量无效")
            self.path_seen = True
        elif name in ("hmoveto", "vmoveto"):
            self._consume_width(name)
            if len(self.stack) < 1 or len(self.stack) % 2 == 0:
                _error(BARE_CFF_CHARSTRING_INVALID, "Type2 moveto 参数数量无效")
            self.path_seen = True
        elif name == "rlineto":
            if len(self.stack) < 2 or len(self.stack) % 2:
                _error(BARE_CFF_CHARSTRING_INVALID, "Type2 rlineto 参数数量无效")
        elif name in ("hlineto", "vlineto"):
            if not self.stack:
                _error(BARE_CFF_CHARSTRING_INVALID, "Type2 lineto 缺少参数")
        elif name == "rrcurveto":
            if len(self.stack) < 6 or len(self.stack) % 6:
                _error(BARE_CFF_CHARSTRING_INVALID, "Type2 rrcurveto 参数数量无效")
        elif name == "rcurveline":
            if len(self.stack) < 8 or (len(self.stack) - 2) % 6:
                _error(BARE_CFF_CHARSTRING_INVALID, "Type2 rcurveline 参数数量无效")
        elif name == "rlinecurve":
            if len(self.stack) < 8 or (len(self.stack) - 6) % 2:
                _error(BARE_CFF_CHARSTRING_INVALID, "Type2 rlinecurve 参数数量无效")
        elif name in ("vvcurveto", "hhcurveto"):
            if len(self.stack) < 4 or len(self.stack) % 4 not in (0, 1):
                _error(BARE_CFF_CHARSTRING_INVALID, "Type2 曲线参数数量无效")
        elif name in ("vhcurveto", "hvcurveto"):
            if len(self.stack) < 4 or len(self.stack) % 4 not in (0, 1):
                _error(BARE_CFF_CHARSTRING_INVALID, "Type2 曲线参数数量无效")
        elif name == "hflex":
            if len(self.stack) != 7:
                _error(BARE_CFF_CHARSTRING_INVALID, "Type2 hflex 参数数量无效")
        elif name == "flex":
            if len(self.stack) != 13:
                _error(BARE_CFF_CHARSTRING_INVALID, "Type2 flex 参数数量无效")
        elif name == "hflex1":
            if len(self.stack) != 9:
                _error(BARE_CFF_CHARSTRING_INVALID, "Type2 hflex1 参数数量无效")
        elif name == "flex1":
            if len(self.stack) != 11:
                _error(BARE_CFF_CHARSTRING_INVALID, "Type2 flex1 参数数量无效")

    def _arithmetic(self, name: str) -> None:
        if name in {"and", "or", "add", "sub", "mul", "div", "eq"}:
            left, right = self._pop(2)
            if name == "and":
                self.stack.append(1 if left and right else 0)
            elif name == "or":
                self.stack.append(1 if left or right else 0)
            elif name == "add":
                self.stack.append(left + right)
            elif name == "sub":
                self.stack.append(left - right)
            elif name == "mul":
                self.stack.append(left * right)
            elif name == "div":
                if abs(float(right)) <= 1e-12:
                    _error(BARE_CFF_CHARSTRING_INVALID, "Type2 除数不能为零")
                self.stack.append(left / right)
            else:
                self.stack.append(1 if left == right else 0)
        elif name in {"not", "abs", "neg", "sqrt", "drop"}:
            value = self._pop(1)[0]
            if name == "not":
                self.stack.append(0 if value else 1)
            elif name == "abs":
                self.stack.append(abs(value))
            elif name == "neg":
                self.stack.append(-value)
            elif name == "sqrt":
                if float(value) < 0:
                    _error(BARE_CFF_CHARSTRING_INVALID, "Type2 sqrt 参数不能为负")
                self.stack.append(math.sqrt(float(value)))
            else:
                # drop 不再压回。
                pass
        elif name == "dup":
            self.stack.append(self._pop(1)[0])
            self.stack.append(self.stack[-1])
        elif name == "exch":
            first, second = self._pop(2)
            self.stack.extend((second, first))
        elif name == "index":
            index = _to_stack_int(self._pop(1)[0])
            if index < 0 or index >= len(self.stack):
                _error(BARE_CFF_CHARSTRING_INVALID, "Type2 index 越界")
            self.stack.append(self.stack[-1 - index])
        elif name == "roll":
            j = _to_stack_int(self._pop(1)[0])
            n = _to_stack_int(self._pop(1)[0])
            if n < 0 or n > len(self.stack):
                _error(BARE_CFF_CHARSTRING_INVALID, "Type2 roll 数量越界")
            if n:
                j %= n
                if j:
                    self.stack[-n:] = self.stack[-j:] + self.stack[-n:-j]
        elif name == "ifelse":
            s1, s2, v1, v2 = self._pop(4)
            self.stack.append(v1 if s1 <= s2 else v2)
        elif name == "random":
            # random 不能作为可重复字体轮廓的一部分；压入固定有限值，随后由
            # fontTools 绘制阶段决定是否支持。结构检查仍保持确定性。
            self.stack.append(0)
        elif name in {"store", "put", "get", "load"}:
            if name == "store":
                value, index = self._pop(2)
                index_int = _to_stack_int(index)
                if not 0 <= index_int < len(self.transient):
                    _error(BARE_CFF_CHARSTRING_INVALID, "Type2 transient index 越界")
                self.transient[index_int] = value
            elif name == "put":
                index, value = self._pop(2)
                index_int = _to_stack_int(index)
                if not 0 <= index_int < len(self.transient):
                    _error(BARE_CFF_CHARSTRING_INVALID, "Type2 transient index 越界")
                self.transient[index_int] = value
            else:
                index_int = _to_stack_int(self._pop(1)[0])
                if not 0 <= index_int < len(self.transient):
                    _error(BARE_CFF_CHARSTRING_INVALID, "Type2 transient index 越界")
                self.stack.append(self.transient[index_int])
        else:
            _error(BARE_CFF_CHARSTRING_INVALID, "Type2 算术 operator 不受支持")

    def _call_subr(self, *, global_subr: bool, depth: int) -> None:
        if not self.stack:
            _error(BARE_CFF_CHARSTRING_INVALID, "Type2 callsubr 缺少索引")
        operand = _to_stack_int(self.stack.pop())
        subrs = self.global_subrs if global_subr else self.local_subrs
        index = operand + _subr_bias(len(subrs))
        if index < 0 or index >= len(subrs):
            _error(BARE_CFF_CHARSTRING_INVALID, "Type2 subr 索引越界")
        self.calls += 1
        if self.calls > self.limits.max_type2_calls:
            _error(BARE_CFF_RESOURCE_LIMIT, "Type2 subr 调用次数超限")
        if depth >= self.limits.max_type2_recursion_depth:
            _error(BARE_CFF_RESOURCE_LIMIT, "Type2 subr 递归深度超限")
        key = ("g" if global_subr else "l", index)
        if key in self._active_subrs:
            _error(BARE_CFF_RESOURCE_LIMIT, "Type2 subr 出现递归环")
        self._active_subrs.add(key)
        try:
            self._parse_program(subrs[index], is_subr=True, depth=depth + 1)
        finally:
            self._active_subrs.remove(key)

    def _parse_program(self, data: bytes, *, is_subr: bool, depth: int) -> None:
        offset = 0
        returned = False
        ended = False
        while offset < len(data):
            token = _type2_number(data, offset)
            if token is not None:
                value, offset = token
                if not math.isfinite(float(value)):
                    _error(BARE_CFF_CHARSTRING_INVALID, "Type2 数字不是有限值")
                self.stack.append(value)
                self._budget_op()
                continue

            b0 = data[offset]
            if b0 == 12:
                if offset + 1 >= len(data):
                    _error(BARE_CFF_CHARSTRING_INVALID, "Type2 双字节 operator 被截断")
                key = (12, data[offset + 1])
                offset += 2
            else:
                key = (b0,)
                offset += 1
            name = _T2_OPERATOR_NAMES.get(key)
            if name is None:
                _error(BARE_CFF_CHARSTRING_INVALID, "Type2 operator 不受支持")
            self._budget_op()

            if name in self._STEM_OPS:
                self._check_args(name)
                self.hint_count += len(self.stack) // 2
                self.stack.clear()
            elif name in self._MOVETO_OPS or name in self._PATH_OPS:
                self._check_args(name)
                self.stack.clear()
            elif name in ("hintmask", "cntrmask"):
                if self.stack:
                    self._consume_width("stem")
                    if len(self.stack) < 2 or len(self.stack) % 2:
                        _error(BARE_CFF_CHARSTRING_INVALID, "Type2 hintmask 前的 stem 参数无效")
                    self.hint_count += len(self.stack) // 2
                    self.stack.clear()
                mask_bytes = (self.hint_count + 7) // 8
                if offset + mask_bytes > len(data):
                    _error(BARE_CFF_CHARSTRING_INVALID, "Type2 hintmask 数据被截断")
                offset += mask_bytes
            elif name == "callsubr":
                self._call_subr(global_subr=False, depth=depth)
            elif name == "callgsubr":
                self._call_subr(global_subr=True, depth=depth)
            elif name == "return":
                if not is_subr or returned:
                    _error(BARE_CFF_CHARSTRING_INVALID, "Type2 return 位置无效")
                returned = True
                if offset != len(data):
                    _error(BARE_CFF_CHARSTRING_INVALID, "Type2 return 后存在数据")
                break
            elif name == "endchar":
                if offset != len(data):
                    _error(BARE_CFF_CHARSTRING_INVALID, "Type2 endchar 后存在数据")
                if is_subr:
                    # 历史 CFF 把 endchar 当作 Subr 的终止符。它在被调用时
                    # 只结束当前 Subr，不得消费 caller 栈或重复决定字宽；这些
                    # 状态属于同一 glyph 的连续 Type2 执行上下文。
                    ended = True
                    break
                if ended:
                    _error(BARE_CFF_CHARSTRING_INVALID, "Type2 endchar 重复")
                if not self.width_seen:
                    if len(self.stack) in (1, 5):
                        self.stack.pop(0)
                    elif len(self.stack) not in (0, 4):
                        _error(BARE_CFF_WIDTH_INVALID, "Type2 endchar 字宽参数无效")
                    self.width_seen = True
                elif len(self.stack) not in (0, 4):
                    _error(BARE_CFF_CHARSTRING_INVALID, "Type2 endchar 参数数量无效")
                self.stack.clear()
                ended = True
                break
            elif name in {
                "and", "or", "not", "store", "abs", "add", "sub", "div", "load",
                "neg", "eq", "drop", "put", "get", "ifelse", "random", "mul",
                "sqrt", "dup", "exch", "index", "roll",
            }:
                self._arithmetic(name)
            elif name in {"ignore"}:
                # ignore 没有改变绘制语义，但仍不允许携带未知栈状态。
                self.stack.clear()
            else:
                # CFF2 blend/vsindex 和其它未在上面处理的 operator 不属于本次
                # CFF1 受限能力集合。
                _error(BARE_CFF_CHARSTRING_INVALID, "Type2 operator 不属于受限 CFF1")
            self._check_stack_limit()

        if is_subr and not returned and not ended:
            # 合法 CFF 允许 Subr 在数据末尾隐式 return；caller 继续使用同一
            # operand/hint/width 上下文。显式 return/endchar 则已在循环内结束。
            return
        if not is_subr and not ended:
            _error(BARE_CFF_CHARSTRING_INVALID, "Type2 CharString 缺少 endchar")

    def validate(self, glyph_programs: Sequence[bytes]) -> None:
        # 只从 glyph 入口执行 Subr。hintmask 的 mask 长度、width 和 operand 栈
        # 都依赖 caller 上下文，因此不能把未到达的 Subr 当成独立程序扫描。
        self.ops = 0
        self.calls = 0
        for data in glyph_programs:
            self.stack.clear()
            self.transient = [0] * 32
            self.width_seen = False
            self.hint_count = 0
            self.path_seen = False
            self._active_subrs.clear()
            self._parse_program(data, is_subr=False, depth=0)
            if self.stack:
                _error(BARE_CFF_CHARSTRING_INVALID, "Type2 字形结束后栈未清空")


# 下面的 key 常量用 CFF spec 的 opcode 表达，避免散落 magic number。
_KEY_ROS = (12, 30)
_KEY_FD_SELECT = (12, 37)
_KEY_FD_ARRAY = (12, 36)
_KEY_CHARSTRINGS = (17,)
_KEY_CHARSET = (15,)
_KEY_PRIVATE = (18,)
_KEY_FONT_MATRIX = (12, 7)
_KEY_FONT_BBOX = (5,)
_KEY_CHARSTRING_TYPE = (12, 6)
_KEY_FONT_NAME = (12, 38)
_KEY_FULL_NAME = (2,)
_KEY_FAMILY_NAME = (3,)
_KEY_WEIGHT = (4,)
_KEY_MAXSTACK = (12, 25)
_KEY_SUBRS = (19,)


def _parse_bare_cff(raw: bytes, limits: BareCffLimits) -> _ParsedBareCff:
    if len(raw) < 4:
        _error(BARE_CFF_INVALID_HEADER, "裸 CFF header 被截断")
    major, _minor, header_size, header_off_size = raw[:4]
    if major != 1:
        _error(BARE_CFF_UNSUPPORTED_VERSION, "仅支持 CFF1")
    if header_size < 4 or header_size > len(raw) or header_off_size not in (1, 2, 3, 4):
        _error(BARE_CFF_INVALID_HEADER, "CFF1 header 参数无效")

    name_index = _parse_index(raw, header_size, label="Name", limits=limits)
    if name_index.count != 1 or not name_index.items[0]:
        _error(BARE_CFF_NAME_INVALID, "Name INDEX 必须恰好包含一个非空名称")
    try:
        source_name = name_index.items[0].decode("latin-1")
    except UnicodeDecodeError as exc:
        raise BareCffError(BARE_CFF_NAME_INVALID, "Name INDEX 无法解析") from exc
    if len(name_index.items[0]) > 127:
        _error(BARE_CFF_NAME_INVALID, "Name INDEX 名称过长")

    top_index = _parse_index(raw, name_index.end, label="Top DICT", limits=limits)
    if top_index.count != 1:
        _error(BARE_CFF_TOP_DICT_COUNT, "Top DICT 必须恰好包含一个字体")
    top_dict = _parse_dict(top_index.items[0], label="Top", operators=_TOP_OPERATOR_NAMES)
    if _KEY_ROS in top_dict.values:
        _error(BARE_CFF_CID_KEYED, "CID-keyed CFF 不在受限能力集合内")
    if _KEY_FD_ARRAY in top_dict.values:
        _error(BARE_CFF_FD_ARRAY_UNSUPPORTED, "FDArray 不在受限能力集合内")
    if _KEY_FD_SELECT in top_dict.values:
        _error(BARE_CFF_FD_SELECT_UNSUPPORTED, "FDSelect 不在受限能力集合内")
    for cid_key in ((12, 31), (12, 32), (12, 33), (12, 34), (12, 35)):
        if cid_key in top_dict.values:
            _error(BARE_CFF_CID_KEYED, "CID 相关 Top DICT operator 不受支持")

    string_index = _parse_index(raw, top_index.end, label="String", limits=limits)
    global_subrs = _parse_index(raw, string_index.end, label="GlobalSubr", limits=limits)
    if sum(len(item) for item in global_subrs.items) > limits.max_subr_bytes:
        _error(BARE_CFF_RESOURCE_LIMIT, "Global Subr 数据超限")

    charstrings_offset = _require_one_int(
        top_dict.values,
        _KEY_CHARSTRINGS,
        code=BARE_CFF_MISSING_CHARSTRINGS,
        label="CharStrings offset",
        minimum=0,
    )
    charset_offset = _require_one_int(
        top_dict.values,
        _KEY_CHARSET,
        code=BARE_CFF_CHARSET_INVALID,
        label="Charset offset",
        minimum=0,
    )
    private_raw = top_dict.values.get(_KEY_PRIVATE)
    if private_raw is None or len(private_raw) != 2:
        _error(BARE_CFF_PRIVATE_INVALID, "非 CID CFF 必须包含合法 Private DICT")
    private_size = _safe_int(
        _finite_number(private_raw[0], code=BARE_CFF_PRIVATE_INVALID, label="Private size"),
        code=BARE_CFF_PRIVATE_INVALID,
        label="Private size",
        minimum=1,
        maximum=limits.max_private_bytes,
    )
    private_offset = _safe_int(
        _finite_number(private_raw[1], code=BARE_CFF_PRIVATE_INVALID, label="Private offset"),
        code=BARE_CFF_PRIVATE_INVALID,
        label="Private offset",
        minimum=0,
        maximum=len(raw),
    )
    if private_offset + private_size > len(raw):
        _error(BARE_CFF_PRIVATE_INVALID, "Private DICT 越过输入边界")
    private_dict = _parse_dict(
        raw[private_offset : private_offset + private_size],
        label="Private",
        operators=_PRIVATE_OPERATOR_NAMES,
    )

    local_subrs = _IndexData(private_offset + private_size, private_offset + private_size, 0, 0, ())
    if _KEY_SUBRS in private_dict.values:
        subr_rel = _require_one_int(
            private_dict.values,
            _KEY_SUBRS,
            code=BARE_CFF_PRIVATE_INVALID,
            label="Local Subrs offset",
            minimum=0,
        )
        subr_offset = private_offset + subr_rel
        if subr_offset < private_offset or subr_offset >= len(raw):
            _error(BARE_CFF_PRIVATE_INVALID, "Local Subrs offset 越过输入边界")
        local_subrs = _parse_index(raw, subr_offset, label="LocalSubr", limits=limits)
        if sum(len(item) for item in local_subrs.items) > limits.max_subr_bytes:
            _error(BARE_CFF_RESOURCE_LIMIT, "Local Subr 数据超限")

    charstrings = _parse_index(raw, charstrings_offset, label="CharStrings", limits=limits)
    if charstrings.count < 1:
        _error(BARE_CFF_MISSING_CHARSTRINGS, "CharStrings INDEX 为空")
    if charstrings.count > limits.max_glyph_count:
        _error(BARE_CFF_RESOURCE_LIMIT, "字形数量超限")
    total_charstring_bytes = sum(len(item) for item in charstrings.items)
    if any(len(item) > limits.max_charstring_bytes for item in charstrings.items):
        _error(BARE_CFF_RESOURCE_LIMIT, "单个 CharString 数据超限")
    if total_charstring_bytes > limits.max_total_charstring_bytes:
        _error(BARE_CFF_RESOURCE_LIMIT, "CharString 总数据超限")

    glyph_order = _parse_charset(raw, charset_offset, charstrings.count, string_index)
    if glyph_order[0] != ".notdef":
        _error(BARE_CFF_CHARSET_INVALID, "GID 0 必须是 .notdef")

    matrix, units_per_em = _font_matrix_and_upem(top_dict)
    charstring_type = _optional_int(
        top_dict.values,
        _KEY_CHARSTRING_TYPE,
        code=BARE_CFF_CHARSTRING_INVALID,
        label="CharstringType",
        default=2,
    )
    if charstring_type != 2:
        _error(BARE_CFF_CHARSTRING_INVALID, "仅支持 Type2 CharString")
    if _KEY_FONT_BBOX not in top_dict.values:
        _error(BARE_CFF_BBOX_INVALID, "CFF FontBBox 缺失")
    bbox_raw = top_dict.values[_KEY_FONT_BBOX]
    if len(bbox_raw) != 4:
        _error(BARE_CFF_BBOX_INVALID, "CFF FontBBox 参数数量无效")
    for value in bbox_raw:
        _finite_number(value, code=BARE_CFF_BBOX_INVALID, label="FontBBox")

    source_buffer = BytesIO(raw)
    source_font = TTFont(recalcTimestamp=False, recalcBBoxes=True)
    cff = cffLib.CFFFontSet()
    try:
        cff.decompile(source_buffer, source_font, isCFF2=False)
        if len(cff) != 1:
            _error(BARE_CFF_TOP_DICT_COUNT, "fontTools 读取到多个 Top DICT")
        top = cff.topDictIndex[0]
        private = top.Private
        charstrings_object = top.CharStrings
        parsed_order = tuple(top.getGlyphOrder())
        if parsed_order != glyph_order:
            _error(BARE_CFF_CHARSET_INVALID, "fontTools Charset 与受限 reader 不一致")
        if len(charstrings_object) != charstrings.count:
            _error(BARE_CFF_MISSING_CHARSTRINGS, "CharStrings 数量不一致")
        # 强制加载所有延迟对象，使后续输出不依赖已关闭的输入 view。
        for index in range(len(cff.GlobalSubrs)):
            _ = cff.GlobalSubrs[index]
        if hasattr(private, "Subrs"):
            for index in range(len(private.Subrs)):
                _ = private.Subrs[index]
        for name in glyph_order:
            char_string = charstrings_object[name]
            if not isinstance(char_string.bytecode, bytes):
                _error(BARE_CFF_CHARSTRING_INVALID, "CharString 字节码无法读取")
    except BareCffError:
        source_font.close()
        raise
    except (AssertionError, IndexError, KeyError, TypeError, ValueError, TTLibError) as exc:
        source_font.close()
        raise BareCffError(BARE_CFF_WRAP_FAILED, "fontTools 无法读取裸 CFF1 语义") from exc

    return _ParsedBareCff(
        raw=bytes(raw),
        name_index=name_index,
        top_index=top_index,
        string_index=string_index,
        global_subrs=global_subrs,
        top_dict=top_dict,
        private_dict=private_dict,
        local_subrs=local_subrs,
        charstrings=charstrings,
        glyph_order=glyph_order,
        font_matrix=matrix,
        units_per_em=units_per_em,
        source_name=source_name,
        source_font=source_font,
        cff=cff,
        top=top,
        private=private,
        charstrings_object=charstrings_object,
    )


def _cff_text(top: Any, attr: str) -> str:
    try:
        value = getattr(top, attr)
    except (AttributeError, KeyError, TypeError, ValueError):
        return ""
    return str(value or "")


def _glyph_unicode(name: str) -> int | None:
    base = name.split(".", 1)[0]
    if base in agl.AGL2UV:
        return int(agl.AGL2UV[base])
    if re.fullmatch(r"uni[0-9A-Fa-f]{4}", base):
        return int(base[3:], 16)
    if re.fullmatch(r"u[0-9A-Fa-f]{4,6}", base):
        codepoint = int(base[1:], 16)
        if 0 <= codepoint <= 0x10FFFF:
            return codepoint
    return None


def _build_cmap(glyph_order: Sequence[str]) -> tuple[dict[int, str], Literal["reliable", "partial", "unavailable"], list[str]]:
    cmap: dict[int, str] = {}
    warnings: list[str] = []
    mapped_glyphs = 0
    for name in glyph_order:
        if name == ".notdef":
            continue
        codepoint = _glyph_unicode(name)
        if codepoint is None:
            continue
        if codepoint not in cmap:
            cmap[codepoint] = name
            mapped_glyphs += 1
    drawable_count = max(0, len(glyph_order) - 1)
    if not cmap:
        warnings.append("BARE_CFF_CMAP_UNAVAILABLE")
        return cmap, "unavailable", warnings
    if mapped_glyphs < drawable_count:
        warnings.append("BARE_CFF_CMAP_PARTIAL")
        return cmap, "partial", warnings
    return cmap, "reliable", warnings


def _bounds_for_charstring(char_string: Any, glyph_set: Any) -> tuple[int, int, int, int] | None:
    pen = BoundsPen(glyph_set)
    char_string.draw(pen)
    if pen.bounds is None:
        return None
    values = tuple(
        _safe_int(float(value), code=BARE_CFF_BBOX_INVALID, label="字形 bounds", minimum=-2147483648, maximum=2147483647)
        for value in pen.bounds
    )
    return values  # type: ignore[return-value]


def _union_bounds(bounds: Iterable[tuple[int, int, int, int] | None]) -> tuple[int, int, int, int]:
    current: tuple[int, int, int, int] | None = None
    for item in bounds:
        if item is None:
            continue
        if current is None:
            current = item
        else:
            current = (
                min(current[0], item[0]),
                min(current[1], item[1]),
                max(current[2], item[2]),
                max(current[3], item[3]),
            )
    return current or (0, 0, 0, 0)


def _validate_and_collect_semantics(
    parsed: _ParsedBareCff,
    limits: BareCffLimits,
) -> tuple[dict[str, tuple[int, int]], dict[str, tuple[int, int, int, int] | None], tuple[int, int, int, int], list[str]]:
    local_subrs = tuple(parsed.local_subrs.items)
    global_subrs = tuple(parsed.global_subrs.items)
    validator = _Type2Validator(local_subrs=local_subrs, global_subrs=global_subrs, limits=limits)
    programs = tuple(parsed.charstrings.items)
    # 含 Subr 时也必须从每个 glyph 入口执行真实调用路径；这样 hintmask 的
    # caller hint 数量、共享 operand 栈、隐式 return，以及递归/调用预算都能在
    # 同一执行上下文中校验。未被调用的 Subr 仍由 INDEX 和 max_subr_bytes 保护，
    # 但不做无法获得 caller 上下文的独立语义扫描。
    validator.validate(programs)

    metrics: dict[str, tuple[int, int]] = {}
    bounds: dict[str, tuple[int, int, int, int] | None] = {}
    for name in parsed.glyph_order:
        char_string = parsed.charstrings_object[name]
        try:
            char_string.decompile()
            char_string.draw(NullPen())
            width = _safe_int(
                _finite_number(getattr(char_string, "width", None), code=BARE_CFF_WIDTH_INVALID, label="CharString width"),
                code=BARE_CFF_WIDTH_INVALID,
                label="CharString width",
                minimum=0,
                maximum=65535,
            )
            glyph_bounds = _bounds_for_charstring(char_string, parsed.charstrings_object)
        except BareCffError:
            raise
        except RecursionError as exc:
            raise BareCffError(BARE_CFF_RESOURCE_LIMIT, "Type2 执行递归超限") from exc
        except (AssertionError, IndexError, KeyError, TypeError, ValueError, NotImplementedError, ZeroDivisionError) as exc:
            raise BareCffError(BARE_CFF_CHARSTRING_INVALID, "Type2 CharString 无法执行") from exc
        lsb = glyph_bounds[0] if glyph_bounds is not None else 0
        metrics[name] = (width, lsb)
        bounds[name] = glyph_bounds

    actual_bbox = _union_bounds(bounds.values())
    source_bbox_raw = parsed.top_dict.values[_KEY_FONT_BBOX]
    source_bbox = tuple(
        _safe_int(
            _finite_number(value, code=BARE_CFF_BBOX_INVALID, label="FontBBox"),
            code=BARE_CFF_BBOX_INVALID,
            label="FontBBox",
            minimum=-32768,
            maximum=32767,
        )
        for value in source_bbox_raw
    )
    warnings: list[str] = []
    if source_bbox != actual_bbox:
        if any(abs(left - right) > 2 for left, right in zip(source_bbox, actual_bbox)):
            raise BareCffError(BARE_CFF_BBOX_INVALID, "FontBBox 与实际轮廓 bounds 差异过大")
        warnings.append("BARE_CFF_FONT_BBOX_REPAIRED")
    return metrics, bounds, actual_bbox, warnings


def _set_cff_string(top: Any, attr: str, value: str) -> None:
    try:
        setattr(top, attr, value)
    except (AttributeError, TypeError, ValueError):
        # 编译器会继续使用原始字段；名称表仍然提供经过清洗的 Unicode 语义。
        pass


def _build_otf(
    parsed: _ParsedBareCff,
    *,
    filename: str,
    metrics: dict[str, tuple[int, int]],
    bounds: dict[str, tuple[int, int, int, int] | None],
    actual_bbox: tuple[int, int, int, int],
    cmap: dict[int, str],
    limits: BareCffLimits,
) -> tuple[bytes, dict[str, str], list[str]]:
    top = parsed.top
    warnings: list[str] = []
    cff_name = parsed.source_name
    family_source = _cff_text(top, "FamilyName") or _cff_text(top, "FullName") or cff_name
    full_source = _cff_text(top, "FullName") or family_source
    style_source = _cff_text(top, "Weight") or "Regular"
    family_name, family_fallback = _safe_text(family_source, "Bare CFF Font")
    style_name, style_fallback = _safe_text(style_source, "Regular", max_length=64)
    full_name, full_fallback = _safe_text(full_source, family_name)
    if family_fallback or style_fallback or full_fallback:
        warnings.append("BARE_CFF_NAME_FALLBACK")
    ps_name = _safe_postscript(cff_name or family_name, "BareCFFFont")
    cff_family = _safe_postscript(family_name, "BareCFFFont")
    cff_full = _safe_postscript(full_name, cff_family.replace("-", " "))

    # CFF 内部名称必须保持 PostScript 可编译；面向系统和 UI 的 Unicode 名称
    # 由 OpenType name 表承载。原始 CharString、Private、Subr 语义不在这里重写。
    if parsed.cff.fontNames:
        parsed.cff.fontNames[0] = ps_name
    _set_cff_string(top, "FontName", ps_name)
    _set_cff_string(top, "FamilyName", cff_family)
    _set_cff_string(top, "FullName", cff_full)
    _set_cff_string(top, "Weight", _safe_postscript(style_name, "Regular"))
    top.charset = list(parsed.glyph_order)
    top.FontBBox = list(actual_bbox)
    top.FontMatrix = list(parsed.font_matrix)

    ascent = max(0, actual_bbox[3])
    descent = min(0, actual_bbox[1])
    if ascent == 0 and descent == 0:
        ascent = parsed.units_per_em
        descent = 0
    advances = [values[0] for values in metrics.values()]
    left_bearings = [values[1] for values in metrics.values()]
    extents: list[int] = []
    for name, glyph_bounds in bounds.items():
        if glyph_bounds is not None:
            extents.append(metrics[name][1] + (glyph_bounds[2] - glyph_bounds[0]))
    advance_max = max(advances, default=0)
    min_lsb = min(left_bearings, default=0)
    min_rsb = min(
        (metrics[name][0] - (metrics[name][1] + (glyph_bounds[2] if glyph_bounds else 0)))
        for name, glyph_bounds in bounds.items()
    ) if bounds else 0
    x_max_extent = max(extents, default=0)

    builder = FontBuilder(parsed.units_per_em, isTTF=False)
    font = builder.font
    font.sfntVersion = "OTTO"
    builder.setupGlyphOrder(list(parsed.glyph_order))
    builder.setupCharacterMap(cmap, allowFallback=True)
    builder.setupHorizontalMetrics(metrics)
    builder.setupHorizontalHeader(
        ascent=max(-32768, min(32767, ascent)),
        descent=max(-32768, min(32767, descent)),
        lineGap=0,
        advanceWidthMax=max(0, min(65535, advance_max)),
        minLeftSideBearing=max(-32768, min(32767, min_lsb)),
        minRightSideBearing=max(-32768, min(32767, min_rsb)),
        xMaxExtent=max(-32768, min(32767, x_max_extent)),
        numberOfHMetrics=len(parsed.glyph_order),
    )
    head = font["head"]
    head.unitsPerEm = parsed.units_per_em
    head.xMin, head.yMin, head.xMax, head.yMax = actual_bbox
    builder.setupNameTable(
        {
            "familyName": family_name,
            "styleName": style_name,
            "uniqueFontIdentifier": full_name,
            "fullName": full_name,
            "psName": ps_name,
            "typographicFamily": family_name,
            "typographicSubfamily": style_name,
        }
    )
    builder.setupOS2(
        sTypoAscender=max(-32768, min(32767, ascent)),
        sTypoDescender=max(-32768, min(32767, descent)),
        usWinAscent=max(0, min(65535, ascent)),
        usWinDescent=max(0, min(65535, -descent)),
    )
    builder.setupPost(keepGlyphNames=True)
    builder.setupMaxp()
    font["maxp"].numGlyphs = len(parsed.glyph_order)

    cff_table = newTable("CFF ")
    cff_table.cff = parsed.cff
    parsed.cff.otFont = font
    font["CFF "] = cff_table

    output = BytesIO()
    try:
        font.save(output)
    except (AssertionError, IndexError, KeyError, OSError, TypeError, ValueError, TTLibError) as exc:
        raise BareCffError(BARE_CFF_WRAP_FAILED, "裸 CFF 无法封装为 OTTO") from exc
    result = bytes(output.getvalue())
    if not result.startswith(b"OTTO"):
        raise BareCffError(BARE_CFF_WRAP_FAILED, "封装结果不是 OTTO")
    if len(result) > limits.max_output_bytes:
        raise BareCffError(BARE_CFF_RESOURCE_LIMIT, "OTF 输出超出资源上限")
    return result, {
        "family": family_name,
        "style": style_name,
        "full": full_name,
        "postscript": ps_name,
    }, warnings


def _verify_otf(
    content: bytes,
    *,
    parsed: _ParsedBareCff,
    metrics: dict[str, tuple[int, int]],
    bounds: dict[str, tuple[int, int, int, int] | None],
    cmap: dict[int, str],
) -> None:
    verify: TTFont | None = None
    try:
        verify = TTFont(BytesIO(bytes(content)), lazy=False, recalcBBoxes=True, recalcTimestamp=False)
        if verify.sfntVersion != "OTTO" or "CFF " not in verify:
            _error(BARE_CFF_WRAP_FAILED, "OTF 回读缺少 OTTO 或 CFF 表")
        required = {"CFF ", "head", "hhea", "hmtx", "maxp", "cmap", "name", "OS/2", "post"}
        if not required.issubset({str(tag) for tag in verify.keys()}):
            _error(BARE_CFF_WRAP_FAILED, "OTF 回读缺少必要外围表")
        if tuple(verify.getGlyphOrder()) != parsed.glyph_order:
            _error(BARE_CFF_CHARSET_INVALID, "OTF 回读 glyph order 不一致")
        if len(verify.getGlyphOrder()) != len(parsed.glyph_order):
            _error(BARE_CFF_CHARSET_INVALID, "OTF 回读 glyph 数量不一致")
        if verify["head"].unitsPerEm != parsed.units_per_em:
            _error(BARE_CFF_UNSUPPORTED_FONT_MATRIX, "OTF 回读 unitsPerEm 不一致")
        for name, expected in metrics.items():
            actual = verify["hmtx"].metrics.get(name)
            if actual is None or tuple(actual) != tuple(expected):
                _error(BARE_CFF_WIDTH_INVALID, "OTF 回读 hmtx 与 CharString width 不一致")
        actual_cmap = verify.getBestCmap() or {}
        for codepoint, name in cmap.items():
            if actual_cmap.get(codepoint) != name:
                _error(BARE_CFF_WRAP_FAILED, "OTF 回读 cmap 不一致")
        output_cff = verify["CFF "].cff
        if len(output_cff) != 1 or output_cff.major != 1:
            _error(BARE_CFF_WRAP_FAILED, "OTF 回读 CFF 版本或 Top DICT 数量无效")
        output_top = output_cff.topDictIndex[0]
        if tuple(output_top.getGlyphOrder()) != parsed.glyph_order:
            _error(BARE_CFF_CHARSET_INVALID, "CFF 回读 Charset 不一致")
        for name, expected_bounds in bounds.items():
            char_string = output_top.CharStrings[name]
            pen = BoundsPen(output_top.CharStrings)
            char_string.draw(pen)
            actual_bounds = None if pen.bounds is None else tuple(_safe_int(float(value), code=BARE_CFF_BBOX_INVALID, label="回读 bounds", minimum=-32768, maximum=32767) for value in pen.bounds)
            if actual_bounds != expected_bounds:
                _error(BARE_CFF_WRAP_FAILED, "OTF 回读轮廓 bounds 不一致")
            char_string.draw(NullPen())
            width = _safe_int(_finite_number(getattr(char_string, "width", None), code=BARE_CFF_WIDTH_INVALID, label="回读 width"), code=BARE_CFF_WIDTH_INVALID, label="回读 width", minimum=0, maximum=65535)
            if width != metrics[name][0]:
                _error(BARE_CFF_WIDTH_INVALID, "OTF 回读 CharString width 不一致")
    except BareCffError:
        raise
    except RecursionError as exc:
        raise BareCffError(BARE_CFF_RESOURCE_LIMIT, "Type2 执行递归超限") from exc
    except (AssertionError, IndexError, KeyError, OSError, TypeError, ValueError, TTLibError) as exc:
        raise BareCffError(BARE_CFF_WRAP_FAILED, "OTF 回读验证失败") from exc
    finally:
        if verify is not None:
            try:
                verify.close()
            except Exception:
                pass


def normalize_bare_cff1(
    content: bytes | bytearray | memoryview,
    filename: str = "",
    *,
    limits: BareCffLimits | None = None,
) -> BareCffNormalization:
    """把受限裸 CFF1 封装为可重新加载的 OTTO/CFF OTF。"""
    policy = limits or BareCffLimits()
    raw = _as_bytes(content)
    if not raw:
        _error(BARE_CFF_INVALID_HEADER, "裸 CFF 输入为空")
    if len(raw) > policy.max_input_bytes:
        _error(BARE_CFF_RESOURCE_LIMIT, "裸 CFF 输入超出大小上限")

    parsed: _ParsedBareCff | None = None
    try:
        parsed = _parse_bare_cff(raw, policy)
        metrics, bounds, actual_bbox, semantic_warnings = _validate_and_collect_semantics(parsed, policy)
        cmap, cmap_state, cmap_warnings = _build_cmap(parsed.glyph_order)
        output, names, name_warnings = _build_otf(
            parsed,
            filename=filename,
            metrics=metrics,
            bounds=bounds,
            actual_bbox=actual_bbox,
            cmap=cmap,
            limits=policy,
        )
        _verify_otf(output, parsed=parsed, metrics=metrics, bounds=bounds, cmap=cmap)
        warnings = [*semantic_warnings, *cmap_warnings, *name_warnings]
        report = BareCffCapabilityReport(
            container="bare-cff1",
            output_container="otf",
            cff_version="cff1",
            single_top_dict=True,
            cid_keyed=False,
            fd_array=False,
            fd_select=False,
            glyph_count=len(parsed.glyph_order),
            cmap=cmap_state,
            cmap_action="mapped" if cmap_state != "unavailable" else "preserved-all-glyphs",
            warnings=warnings,
            save_verified=True,
            units_per_em=parsed.units_per_em,
            font_matrix=parsed.font_matrix,
            glyph_order=tuple(parsed.glyph_order),
            metrics=dict(metrics),
            font_bbox=actual_bbox,
            cmap_map=dict(cmap),
            outline_bounds=dict(bounds),
        )
        return BareCffNormalization(
            content=bytes(output),
            output_filename=_safe_output_filename(filename),
            report=report,
        )
    finally:
        if parsed is not None:
            try:
                parsed.source_font.close()
            except Exception:
                pass


# 同义导出给早期入口/集成代理使用；主 API 仍是 normalize_bare_cff1。
normalize_bare_cff = normalize_bare_cff1
is_bare_cff = is_bare_cff1


__all__ = [
    "BareCffError",
    "BareCffLimits",
    "BareCffCapabilityReport",
    "BareCffNormalization",
    "is_bare_cff1",
    "normalize_bare_cff1",
    "is_bare_cff",
    "normalize_bare_cff",
    "BARE_CFF_INVALID_HEADER",
    "BARE_CFF_UNSUPPORTED_VERSION",
    "BARE_CFF_INDEX_TRUNCATED",
    "BARE_CFF_INDEX_INVALID_OFFSET",
    "BARE_CFF_TOP_DICT_COUNT",
    "BARE_CFF_CID_KEYED",
    "BARE_CFF_FD_ARRAY_UNSUPPORTED",
    "BARE_CFF_FD_SELECT_UNSUPPORTED",
    "BARE_CFF_MISSING_CHARSTRINGS",
    "BARE_CFF_CHARSET_INVALID",
    "BARE_CFF_PRIVATE_INVALID",
    "BARE_CFF_CHARSTRING_INVALID",
    "BARE_CFF_WIDTH_INVALID",
    "BARE_CFF_UNSUPPORTED_FONT_MATRIX",
    "BARE_CFF_RESOURCE_LIMIT",
    "BARE_CFF_CMAP_REQUIRED",
    "BARE_CFF_WRAP_FAILED",
]
