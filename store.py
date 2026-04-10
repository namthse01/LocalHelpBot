import logging
from pathlib import Path
from langchain_core.documents import Document
import chromadb
from typing import Optional

logging.basicConfig(level=logging.INFO, format="%(asctime)s - %(levelname)s - %(message)s")
logger = logging.getLogger(__name__)

DB_PATH = "./cad_db"
COLLECTION_NAME = "cad_docs"
BATCH_SIZE = 64

_client: Optional[chromadb.PersistentClient] = None
_collection = None


def _get_collection():
    global _client, _collection
    if _collection is None:
        _client = chromadb.PersistentClient(path=DB_PATH)
        _collection = _client.get_or_create_collection(
            name=COLLECTION_NAME,
            metadata={"hnsw:space": "cosine"},
        )
    return _collection


def store(embedded: list[tuple[Document, list[float]]]) -> None:
    if not embedded:
        return

    collection = _get_collection()

    for i in range(0, len(embedded), BATCH_SIZE):
        batch = embedded[i:i + BATCH_SIZE]

        ids = []
        embeddings = []
        documents = []
        metadatas = []

        for chunk, embedding in batch:
            chunk_id = chunk.metadata.get("chunk_id", f"{i}_{id(chunk)}")
            ids.append(chunk_id)
            embeddings.append(embedding)
            documents.append(chunk.page_content)
            meta = {
                k: v for k, v in chunk.metadata.items()
                if v is not None and isinstance(v, (str, int, float, bool))
            }
            metadatas.append(meta)

        collection.upsert(
            ids=ids,
            embeddings=embeddings,
            documents=documents,
            metadatas=metadatas,
        )

        logger.info(f"Stored batch {i // BATCH_SIZE + 1} / {(len(embedded) - 1) // BATCH_SIZE + 1}")

    logger.info(f"Total stored: {len(embedded)} chunks -> {DB_PATH}")