import argparse
from pathlib import Path

import chromadb
from sentence_transformers import SentenceTransformer

DB_PATH = Path("cad_db")
COLLECTION_NAME = "cad_docs"
EMBED_MODEL = "all-mpnet-base-v2"
SCORING_THRESHOLD = 0.3

RULES = """
You are a CAD knowledge assistant powered by a local knowledge base.

Rules:
1. ALWAYS use the local RAG store to answer CAD-related questions.
2. ONLY use information returned from the local RAG retrieval.
3. Do not fabricate facts or rely on external sources.
4. If the best score is below 0.3, report that the knowledge base has insufficient data and ask the user for permission to search the web.
5. Show source metadata and score for each retrieved result.
"""


def get_collection():
    if not DB_PATH.exists():
        raise FileNotFoundError(f"RAG database path not found: {DB_PATH}")
    client = chromadb.PersistentClient(path=str(DB_PATH))
    return client.get_collection(name=COLLECTION_NAME)


def embed_query(query: str):
    model = SentenceTransformer(EMBED_MODEL)
    embedding = model.encode([query], show_progress_bar=False, convert_to_numpy=True)[0]
    return embedding.tolist()


def query_rag(query: str, n_results: int = 3):
    collection = get_collection()
    query_embedding = embed_query(query)
    response = collection.query(
        query_embeddings=[query_embedding],
        n_results=n_results,
        include=["documents", "metadatas", "distances"],
    )
    return response


def format_response(response: dict, query: str) -> str:
    lines = []
    docs = response.get("documents", [[]])[0]
    metadatas = response.get("metadatas", [[]])[0]
    distances = response.get("distances", [[]])[0]
    ids = response.get("ids", [[]])[0]

    if not docs:
        return f"No results found for query: {query}"

    best_score = 0.0
    for i, (doc, meta, dist, id_) in enumerate(zip(docs, metadatas, distances, ids)):
        score = 1 - dist
        best_score = max(best_score, score)
        lines.append(f"[{i + 1}] score={score:.3f} | {meta.get('file_name', '')} | {meta.get('semantic_type', '')}")
        lines.append(f"source: {meta.get('source')}")
        lines.append("content:")
        lines.append(f"  {doc.strip().replace(chr(10), chr(10) + '  ')}")
        lines.append("")

    if best_score < SCORING_THRESHOLD:
        lines.append("Thiếu thông tin trong knowledge base. Hãy cho phép dò thông tin trên mạng hay không")

    return "\n".join(lines)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Query the local CAD RAG store.")
    parser.add_argument("--query", required=True, help="Search query text")
    parser.add_argument("--top", type=int, default=3, help="Number of results to return")
    args = parser.parse_args()

    response = query_rag(args.query, n_results=args.top)
    print(format_response(response, args.query))
