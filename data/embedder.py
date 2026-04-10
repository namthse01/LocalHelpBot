import json
import logging
import urllib.request
from langchain_core.documents import Document
from config import OLLAMA_BASE, EMBED_MODEL

logging.basicConfig(level=logging.INFO, format="%(asctime)s - %(levelname)s - %(message)s")
logger = logging.getLogger(__name__)

BATCH_SIZE = 32  # mxbai-embed-large nặng hơn, batch nhỏ hơn cho ổn định


def _ollama_embed(texts: list[str]) -> list[list[float]]:
    """Gọi Ollama /api/embed để embed một batch text."""
    body = json.dumps({"model": EMBED_MODEL, "input": texts}).encode()
    req = urllib.request.Request(
        OLLAMA_BASE + "/api/embed",
        data=body,
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=120) as resp:
        data = json.loads(resp.read())
    return data["embeddings"]


def embed(chunks: list[Document]) -> list[tuple[Document, list[float]]]:
    if not chunks:
        return []

    texts = [chunk.page_content for chunk in chunks]
    results: list[tuple[Document, list[float]]] = []
    total_batches = (len(texts) - 1) // BATCH_SIZE + 1

    for i in range(0, len(texts), BATCH_SIZE):
        batch_texts = texts[i:i + BATCH_SIZE]
        batch_chunks = chunks[i:i + BATCH_SIZE]
        batch_num = i // BATCH_SIZE + 1
        logger.info(f"Embedding batch {batch_num}/{total_batches} via Ollama ({EMBED_MODEL})")
        embeddings = _ollama_embed(batch_texts)
        for chunk, embedding in zip(batch_chunks, embeddings):
            results.append((chunk, embedding))

    logger.info(f"Total embedded: {len(results)}")
    return results