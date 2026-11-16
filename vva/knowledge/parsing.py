"""文档解析与切分：把 txt / md / pdf / docx 变成带位置信息的文本段落。

- txt、md：直接读取（自动识别 UTF-8 / GBK）
- docx：直接解析压缩包里的 word/document.xml，不需要额外依赖
- pdf：需要 pypdf（扫描件没有文字层，无法提取）
"""
from __future__ import annotations

import io
import re
import zipfile
from dataclasses import dataclass
from xml.etree import ElementTree

SUPPORTED = (".txt", ".md", ".markdown", ".pdf", ".docx")
_W = "{http://schemas.openxmlformats.org/wordprocessingml/2006/main}"


@dataclass
class Chunk:
    text: str
    position: str = ""      # 出处位置，如“第 3 页”“二、巡检要求”


def _decode(data: bytes) -> str:
    for enc in ("utf-8-sig", "gb18030"):
        try:
            return data.decode(enc)
        except UnicodeDecodeError:
            continue
    return data.decode("utf-8", errors="ignore")


def _docx_paragraphs(data: bytes) -> list[str]:
    with zipfile.ZipFile(io.BytesIO(data)) as z:
        root = ElementTree.fromstring(z.read("word/document.xml"))
    paras = []
    for p in root.iter(f"{_W}p"):
        text = "".join(t.text or "" for t in p.iter(f"{_W}t"))
        if text.strip():
            paras.append(text.strip())
    for row in root.iter(f"{_W}tr"):      # 表格按行拼成一段，单元格用“ | ”分隔
        cells = ["".join(t.text or "" for t in c.iter(f"{_W}t")).strip() for c in row.iter(f"{_W}tc")]
        if any(cells):
            paras.append(" | ".join(cells))
    return paras


def _pdf_pages(data: bytes) -> list[str]:
    try:
        from pypdf import PdfReader
    except ImportError as e:
        raise RuntimeError("解析 PDF 需要安装 pypdf：uv pip install pypdf") from e
    reader = PdfReader(io.BytesIO(data))
    return [(page.extract_text() or "") for page in reader.pages]


def extract_sections(filename: str, data: bytes) -> list[tuple[str, str]]:
    """返回 [(位置, 文本)]。pdf 按页，其他按整篇（位置由切分时的标题补充）。"""
    name = filename.lower()
    if name.endswith(".pdf"):
        return [(f"第 {i} 页", t) for i, t in enumerate(_pdf_pages(data), 1) if t.strip()]
    if name.endswith(".docx"):
        return [("", "\n".join(_docx_paragraphs(data)))]
    if name.endswith((".txt", ".md", ".markdown")):
        return [("", _decode(data))]
    raise ValueError(f"不支持的文件格式：{filename}（支持 {'、'.join(SUPPORTED)}）")


_HEADING_RE = re.compile(r"^(#{1,6}\s+.+|[一二三四五六七八九十]+[、.．].{1,40}|第[一二三四五六七八九十\d]+[章节条].{0,40}|\d+(\.\d+)*[、.．\s].{1,40})$")


def split_text(sections: list[tuple[str, str]], max_chars: int = 400, overlap: int = 60) -> list[Chunk]:
    """按段落切分，每段不超过 max_chars；记录最近的标题作为出处，相邻段落有少量重叠，避免把一句话切断。"""
    chunks: list[Chunk] = []

    def emit(buf: str, base_pos: str, heading: str) -> str:
        """把缓冲区作为一段输出，返回留给下一段的重叠部分。只有标题、没有正文的不单独成段。"""
        if buf.strip() and buf.strip() != heading:
            chunks.append(Chunk(buf.strip(), "，".join(x for x in (base_pos, heading) if x)))
        return buf[-overlap:] if overlap and len(buf) > overlap else ""

    for base_pos, text in sections:
        heading, buf = "", ""
        for raw in (ln.strip() for ln in text.splitlines()):
            if not raw:
                continue
            if _HEADING_RE.match(raw) and len(raw) <= 50:
                emit(buf, base_pos, heading)
                heading = raw.lstrip("#").strip()
                buf = heading + "\n"
                continue
            # 超长段落先按长度切开，再逐段放入
            for line in (raw[i:i + max_chars] for i in range(0, len(raw), max_chars)):
                if len(buf) + len(line) + 1 > max_chars:
                    buf = emit(buf, base_pos, heading)
                buf += line + "\n"
        emit(buf, base_pos, heading)
    return [c for c in chunks if len(c.text) >= 5]
