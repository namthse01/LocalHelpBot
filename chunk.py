import re
import logging
import hashlib
import unicodedata
import uuid
from datetime import datetime, timezone
from pathlib import Path
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, field
from enum import Enum
from typing import Optional

import tiktoken
from langchain_core.documents import Document
from langchain_text_splitters import RecursiveCharacterTextSplitter, MarkdownHeaderTextSplitter
from langchain_community.document_loaders import (
    TextLoader,
    PyPDFLoader,
    UnstructuredXMLLoader,
    BSHTMLLoader,
    Docx2txtLoader,
)

logging.basicConfig(level=logging.INFO, format="%(asctime)s - %(levelname)s - %(message)s")
logger = logging.getLogger(__name__)

TOKENIZER = tiktoken.get_encoding("cl100k_base")
MAX_FILE_SIZE_MB = 50
MD_HEADERS = [("#", "h1"), ("##", "h2"), ("###", "h3"), ("####", "h4")]

LOADER_MAP = {
    ".txt": TextLoader,
    ".pdf": PyPDFLoader,
    ".xml": UnstructuredXMLLoader,
    ".html": BSHTMLLoader,
    ".htm": BSHTMLLoader,
    ".docx": Docx2txtLoader,
    ".py": TextLoader,
    ".cs": TextLoader,
    ".js": TextLoader,
    ".ts": TextLoader,
    ".cpp": TextLoader,
    ".h": TextLoader,
    ".md": TextLoader,
}

CODE_EXTS = {".py", ".cs", ".js", ".ts", ".cpp", ".h"}
DOC_EXTS = {".pdf", ".docx", ".txt", ".html", ".htm", ".xml"}
MD_EXTS = {".md"}


class SemanticType(str, Enum):
    FUNCTION = "function"
    CLASS = "class"
    PARAGRAPH = "paragraph"
    HEADER_SECTION = "header_section"
    CODE_BLOCK = "code_block"
    UNKNOWN = "unknown"


@dataclass
class ChunkConfig:
    parent_size: int
    child_size: int
    child_overlap: int
    separators: list[str]


CODE_CONFIG = ChunkConfig(
    parent_size=400,
    child_size=150,
    child_overlap=15,
    separators=[
        "\npublic ", "\nprivate ", "\nprotected ", "\ninternal ",
        "\nnamespace ", "\nstruct ", "\nclass ", "\ndef ",
        "\n\n", "\n", ""
    ]
)

DOC_CONFIG = ChunkConfig(
    parent_size=500,
    child_size=200,
    child_overlap=20,
    separators=["\n\n", "\n", ". ", " ", ""]
)

MD_CONFIG = ChunkConfig(
    parent_size=500,
    child_size=200,
    child_overlap=20,
    separators=["\n\n", "\n", ""]
)


def _count_tokens(text: str) -> int:
    return len(TOKENIZER.encode(text))


def _clean_text(text: str) -> str:
    text = unicodedata.normalize("NFKC", text)
    text = re.sub(r'[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]', '', text)
    text = re.sub(r'\s+', ' ', text)
    return text.strip()


def _normalize_for_hash(text: str) -> str:
    return re.sub(r'\s+', ' ', text.strip().lower())


def _make_splitter(size: int, overlap: int, separators: list[str]) -> RecursiveCharacterTextSplitter:
    return RecursiveCharacterTextSplitter(
        chunk_size=size,
        chunk_overlap=overlap,
        separators=separators,
        length_function=_count_tokens,
    )


def _detect_semantic_type(text: str, ext: str) -> SemanticType:
    if ext in CODE_EXTS:
        stripped = text.strip()
        if re.search(r'\bclass\s+\w+', stripped):
            return SemanticType.CLASS
        if re.search(r'\b(def |public |private |protected |async )\s*\w+\s*\(', stripped):
            return SemanticType.FUNCTION
        return SemanticType.CODE_BLOCK
    return SemanticType.PARAGRAPH


def _build_metadata(
    file_path: Path,
    docs_dir: Path,
    ext: str,
    chunk_index: int,
    chunk_total: int,
    content: str,
    semantic_type: SemanticType,
    parent_id: Optional[str] = None,
    hierarchy_path: Optional[str] = None,
    depth: int = 0,
) -> dict:
    return {
        "source": str(file_path),
        "file_name": file_path.name,
        "relative_path": str(file_path.relative_to(docs_dir)),
        "file_type": ext,
        "chunk_index": chunk_index,
        "chunk_total": chunk_total,
        "char_count": len(content),
        "token_count": _count_tokens(content),
        "semantic_type": semantic_type.value,
        "parent_id": parent_id,
        "hierarchy_path": hierarchy_path,
        "depth": depth,
        "ingested_at": datetime.now(timezone.utc).isoformat(),
    }


def _make_chunk_id(*parts: str) -> str:
    normalized = "|".join(p.strip() for p in parts if p is not None)
    return hashlib.md5(normalized.encode("utf-8")).hexdigest()


def _dedup(chunks: list[Document]) -> list[Document]:
    seen = set()
    result = []
    for chunk in chunks:
        h = hashlib.md5(_normalize_for_hash(chunk.page_content).encode()).hexdigest()
        if h not in seen:
            seen.add(h)
            result.append(chunk)
    return result


def _make_parent_child(
    raw_docs: list[Document],
    config: ChunkConfig,
    file_path: Path,
    docs_dir: Path,
    ext: str,
) -> list[Document]:
    parent_splitter = _make_splitter(config.parent_size, 0, config.separators)
    child_splitter = _make_splitter(config.child_size, config.child_overlap, config.separators)

    all_chunks: list[Document] = []

    parents = parent_splitter.split_documents(raw_docs)

    for parent in parents:
        parent_id = str(uuid.uuid4())
        semantic_type = _detect_semantic_type(parent.page_content, ext)

        parent_id = _make_chunk_id(str(file_path.relative_to(docs_dir)), "parent", parent.page_content)
        parent_doc = Document(
            page_content=parent.page_content,
            metadata={
                **parent.metadata,
                **_build_metadata(
                    file_path, docs_dir, ext,
                    chunk_index=0, chunk_total=1,
                    content=parent.page_content,
                    semantic_type=semantic_type,
                    parent_id=None,
                    depth=0,
                ),
                "chunk_id": parent_id,
                "is_parent": True,
            }
        )
        all_chunks.append(parent_doc)

        children = child_splitter.split_documents([parent])
        for i, child in enumerate(children):
            if child.page_content.strip() == parent.page_content.strip():
                continue
            child_id = _make_chunk_id(str(file_path.relative_to(docs_dir)), "child", parent_id, str(i), child.page_content)
            child_doc = Document(
                page_content=child.page_content,
                metadata={
                    **child.metadata,
                    **_build_metadata(
                        file_path, docs_dir, ext,
                        chunk_index=i, chunk_total=len(children),
                        content=child.page_content,
                        semantic_type=semantic_type,
                        parent_id=parent_id,
                        depth=1,
                    ),
                    "chunk_id": child_id,
                    "is_parent": False,
                }
            )
            all_chunks.append(child_doc)

    return all_chunks


def _chunk_markdown(file_path: Path, docs_dir: Path) -> list[Document]:
    text = _clean_text(file_path.read_text(encoding="utf-8", errors="ignore"))
    md_splitter = MarkdownHeaderTextSplitter(headers_to_split_on=MD_HEADERS)
    sections = md_splitter.split_text(text)

    all_chunks: list[Document] = []
    for section in sections:
        hierarchy = " > ".join(
            section.metadata.get(h, "") for _, h in MD_HEADERS
            if section.metadata.get(h)
        )
        raw = [Document(page_content=section.page_content, metadata=section.metadata)]
        chunks = _make_parent_child(raw, MD_CONFIG, file_path, docs_dir, ".md")
        for chunk in chunks:
            chunk.metadata["hierarchy_path"] = hierarchy
            chunk.metadata["semantic_type"] = SemanticType.HEADER_SECTION.value
        all_chunks.extend(chunks)

    return all_chunks


def _chunk_code(file_path: Path, docs_dir: Path) -> list[Document]:
    ext = file_path.suffix.lower()
    docs = TextLoader(str(file_path)).load()
    for doc in docs:
        doc.page_content = _clean_text(doc.page_content)
    return _make_parent_child(docs, CODE_CONFIG, file_path, docs_dir, ext)


def _chunk_doc(file_path: Path, docs_dir: Path) -> list[Document]:
    ext = file_path.suffix.lower()
    docs = LOADER_MAP[ext](str(file_path)).load()
    for doc in docs:
        doc.page_content = _clean_text(doc.page_content)
    return _make_parent_child(docs, DOC_CONFIG, file_path, docs_dir, ext)


def chunk_file(args: tuple) -> list[Document]:
    file_path, docs_dir = args
    ext = file_path.suffix.lower()

    if ext not in LOADER_MAP:
        logger.warning(f"Unsupported: {file_path.name}")
        return []

    size_mb = file_path.stat().st_size / (1024 * 1024)
    if size_mb > MAX_FILE_SIZE_MB:
        logger.warning(f"Skipped large file: {file_path.name} ({size_mb:.1f}MB)")
        return []

    try:
        if ext in MD_EXTS:
            chunks = _chunk_markdown(file_path, docs_dir)
        elif ext in CODE_EXTS:
            chunks = _chunk_code(file_path, docs_dir)
        else:
            chunks = _chunk_doc(file_path, docs_dir)

        chunks = _dedup(chunks)
        logger.info(f"Chunked: {file_path.name} -> {len(chunks)} chunks")
        return chunks

    except Exception as e:
        logger.error(f"Failed: {file_path.name} - {e}")
        return []


def chunk_all(docs_dir: str = "docs", max_workers: int = 4) -> list[Document]:
    docs_path = Path(docs_dir)
    files = [
        f for f in docs_path.rglob("*")
        if f.is_file() and f.suffix.lower() in LOADER_MAP
    ]

    all_chunks: list[Document] = []
    args = [(f, docs_path) for f in files]

    with ThreadPoolExecutor(max_workers=max_workers) as executor:
        futures = {executor.submit(chunk_file, a): a for a in args}
        for future in as_completed(futures):
            all_chunks.extend(future.result())

    logger.info(f"Total chunks: {len(all_chunks)} (parents + children)")
    return all_chunks