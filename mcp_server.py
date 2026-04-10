"""MCP server exposing the local CAD RAG knowledge base to Claude Code."""

import json
import sys
from pathlib import Path

# Ensure the rag directory is on sys.path
sys.path.insert(0, str(Path(__file__).parent))

from rag_query import query_rag, format_response

# MCP protocol over stdio — implements JSON-RPC 2.0
def send(obj: dict):
    line = json.dumps(obj)
    sys.stdout.write(line + "\n")
    sys.stdout.flush()


def handle(msg: dict) -> dict | None:
    method = msg.get("method", "")
    id_ = msg.get("id")

    if method == "initialize":
        return {
            "jsonrpc": "2.0",
            "id": id_,
            "result": {
                "protocolVersion": "2024-11-05",
                "capabilities": {"tools": {}},
                "serverInfo": {"name": "rag-cad", "version": "1.0.0"},
            },
        }

    if method == "notifications/initialized":
        return None  # no reply needed

    if method == "tools/list":
        return {
            "jsonrpc": "2.0",
            "id": id_,
            "result": {
                "tools": [
                    {
                        "name": "query_rag",
                        "description": (
                            "Query the local CAD/AutoCAD knowledge base (ChromaDB RAG). "
                            "Use this whenever the user asks about AutoCAD, CAD programming, "
                            "WPF/WinForms patterns, or UI frontend topics covered in the docs."
                        ),
                        "inputSchema": {
                            "type": "object",
                            "properties": {
                                "query": {
                                    "type": "string",
                                    "description": "The search query in natural language.",
                                },
                                "top": {
                                    "type": "integer",
                                    "description": "Number of results to return (default 3).",
                                    "default": 3,
                                },
                            },
                            "required": ["query"],
                        },
                    },
                    {
                        "name": "list_docs",
                        "description": "List all source documents indexed in the local RAG knowledge base.",
                        "inputSchema": {
                            "type": "object",
                            "properties": {},
                            "required": [],
                        },
                    },
                ]
            },
        }

    if method == "tools/call":
        params = msg.get("params", {})
        tool = params.get("name")
        args = params.get("arguments", {})

        if tool == "query_rag":
            try:
                query = args["query"]
                top = int(args.get("top", 3))
                response = query_rag(query, n_results=top)
                text = format_response(response, query)
                return {
                    "jsonrpc": "2.0",
                    "id": id_,
                    "result": {"content": [{"type": "text", "text": text}]},
                }
            except Exception as e:
                return {
                    "jsonrpc": "2.0",
                    "id": id_,
                    "result": {
                        "content": [{"type": "text", "text": f"Error: {e}"}],
                        "isError": True,
                    },
                }

        if tool == "list_docs":
            try:
                docs_dir = Path(__file__).parent / "docs"
                files = sorted(p.name for p in docs_dir.iterdir() if p.is_file())
                text = "Indexed documents:\n" + "\n".join(f"  - {f}" for f in files)
                return {
                    "jsonrpc": "2.0",
                    "id": id_,
                    "result": {"content": [{"type": "text", "text": text}]},
                }
            except Exception as e:
                return {
                    "jsonrpc": "2.0",
                    "id": id_,
                    "result": {
                        "content": [{"type": "text", "text": f"Error: {e}"}],
                        "isError": True,
                    },
                }

        return {
            "jsonrpc": "2.0",
            "id": id_,
            "error": {"code": -32601, "message": f"Unknown tool: {tool}"},
        }

    # Unknown method
    if id_ is not None:
        return {
            "jsonrpc": "2.0",
            "id": id_,
            "error": {"code": -32601, "message": f"Method not found: {method}"},
        }
    return None


def main():
    for raw in sys.stdin:
        raw = raw.strip()
        if not raw:
            continue
        try:
            msg = json.loads(raw)
        except json.JSONDecodeError:
            continue
        reply = handle(msg)
        if reply is not None:
            send(reply)


if __name__ == "__main__":
    main()
