import logging
from chunk import chunk_all
from embed import embed
from store import store

logging.basicConfig(level=logging.INFO, format="%(asctime)s - %(levelname)s - %(message)s")
logger = logging.getLogger(__name__)


def main():
    logger.info("=== Step 1: Chunking new/changed docs ===")
    chunks = chunk_all(docs_dir="docs", max_workers=4)

    logger.info("=== Step 2: Embedding ===")
    embedded = embed(chunks)

    logger.info("=== Step 3: Upserting into RAG store ===")
    store(embedded)

    logger.info("=== Done ===")


if __name__ == "__main__":
    main()
