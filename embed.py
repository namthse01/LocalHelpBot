import logging
from langchain_core.documents import Document
from sentence_transformers import SentenceTransformer
from typing import Optional

logging.basicConfig(level=logging.INFO, format="%(asctime)s - %(levelname)s - %(message)s")
logger = logging.getLogger(__name__)

MODEL_NAME = "all-mpnet-base-v2"
BATCH_SIZE = 64

_model: Optional[SentenceTransformer] = None


def _get_model() -> SentenceTransformer:
    global _model
    if _model is None:
        logger.info(f"Loading embedding model: {MODEL_NAME}")
        _model = SentenceTransformer(MODEL_NAME)
    return _model


def embed(chunks: list[Document]) -> list[tuple[Document, list[float]]]:
    if not chunks:
        return []

    model = _get_model()
    texts = [chunk.page_content for chunk in chunks]
    results: list[tuple[Document, list[float]]] = []

    for i in range(0, len(texts), BATCH_SIZE):
        batch_texts = texts[i:i + BATCH_SIZE]
        batch_chunks = chunks[i:i + BATCH_SIZE]
        embeddings = model.encode(batch_texts, show_progress_bar=True, convert_to_numpy=True)
        for chunk, embedding in zip(batch_chunks, embeddings):
            results.append((chunk, embedding.tolist()))

        logger.info(f"Embedded batch {i // BATCH_SIZE + 1} / {(len(texts) - 1) // BATCH_SIZE + 1}")

    logger.info(f"Total embedded: {len(results)}")
    return results