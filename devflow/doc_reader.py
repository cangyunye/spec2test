"""需求文档读取器：支持 .docx / .txt / .md 格式。

集成 Anthropic skills docx 能力，使用 python-docx 提取 Word 文档文本。
CLI 通过 --from-doc <path> 参数传入文档路径，自动提取内容作为初始需求输入。
"""
from __future__ import annotations

from pathlib import Path


def read_doc(path: str | Path) -> str:
    """读取需求文档，返回纯文本。

    支持格式：
      - .docx → python-docx 提取段落 + 表格
      - .txt / .md → 直接读取文本
      - 其他 → 尝试按文本读取

    参数:
      path: 文档文件路径

    返回:
      提取的纯文本内容

    异常:
      FileNotFoundError: 文件不存在
      ValueError: 不支持的格式
    """
    p = Path(path)
    if not p.exists():
        raise FileNotFoundError(f"需求文档不存在: {p}")

    suffix = p.suffix.lower()

    if suffix == ".docx":
        return _read_docx(p)
    if suffix in (".txt", ".md", ".markdown", ""):
        return p.read_text(encoding="utf-8")

    raise ValueError(f"不支持的需求文档格式: {suffix}（支持 .docx / .txt / .md）")


def _read_docx(path: Path) -> str:
    """使用 python-docx 提取 .docx 文件的段落和表格文本。"""
    try:
        from docx import Document
    except ImportError as e:
        raise ImportError(
            "读取 .docx 文件需要 python-docx 库，请运行: pip install python-docx"
        ) from e

    doc = Document(str(path))
    parts: list[str] = []

    # 提取段落
    for para in doc.paragraphs:
        text = para.text.strip()
        if text:
            parts.append(text)

    # 提取表格（需求文档可能用表格列验收标准等）
    for table in doc.tables:
        for row in table.rows:
            cells = [cell.text.strip() for cell in row.cells if cell.text.strip()]
            if cells:
                parts.append(" | ".join(cells))

    return "\n\n".join(parts)
