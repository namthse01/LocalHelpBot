#!/usr/bin/env python3
"""Launch a vLLM server with DFlash speculative decoding for TheAgent0.

DFlash (https://arxiv.org/abs/2602.06036, z-lab) is a block-diffusion draft
model for speculative decoding: a tiny draft proposes 15-16 tokens that the
target model verifies in parallel, giving 2-4× faster generation with identical
outputs. It plugs into vLLM via `--speculative-config`.

This wrapper picks the matching `z-lab/*-DFlash` draft for a target model and
builds the right `vllm serve ...` command. Point TheAgent0 at the resulting
OpenAI-compatible endpoint by setting a provider slot to
`{"type": "vllm", "model": "<TARGET>", "base_url": "http://localhost:8000/v1"}`.

Requirements: vLLM >= 0.20.1, an NVIDIA/AMD GPU, Linux or WSL2.

Usage:
    python scripts/serve_vllm_dflash.py --model Qwen/Qwen3.5-27B
    python scripts/serve_vllm_dflash.py --model Qwen/Qwen3.5-27B --print  # dry run
    python scripts/serve_vllm_dflash.py --list                            # show drafts
"""
from __future__ import annotations

import argparse
import json
import shutil
import subprocess
import sys

# Target model → z-lab DFlash draft (from the dflash README model table).
DFLASH_DRAFTS = {
    "google/gemma-4-31B-it": "z-lab/gemma-4-31B-it-DFlash",
    "google/gemma-4-26B-A4B-it": "z-lab/gemma-4-26B-A4B-it-DFlash",
    "Qwen/Qwen3.6-27B": "z-lab/Qwen3.6-27B-DFlash",
    "Qwen/Qwen3.6-35B-A3B": "z-lab/Qwen3.6-35B-A3B-DFlash",
    "Qwen/Qwen3.5-4B": "z-lab/Qwen3.5-4B-DFlash",
    "Qwen/Qwen3.5-9B": "z-lab/Qwen3.5-9B-DFlash",
    "Qwen/Qwen3.5-27B": "z-lab/Qwen3.5-27B-DFlash",
    "Qwen/Qwen3.5-35B-A3B": "z-lab/Qwen3.5-35B-A3B-DFlash",
    "Qwen/Qwen3.5-122B-A10B": "z-lab/Qwen3.5-122B-A10B-DFlash",
    "Qwen/Qwen3-Coder-30B-A3B": "z-lab/Qwen3-Coder-30B-A3B-DFlash",
    "Qwen/Qwen3-Coder-Next": "z-lab/Qwen3-Coder-Next-DFlash",
    "openai/gpt-oss-20b": "z-lab/gpt-oss-20b-DFlash",
    "openai/gpt-oss-120b": "z-lab/gpt-oss-120b-DFlash",
    "meta-llama/Llama-3.1-8B-Instruct": "z-lab/LLaMA3.1-8B-Instruct-DFlash-UltraChat",
}


def build_command(args) -> list[str]:
    draft = args.draft or DFLASH_DRAFTS.get(args.model)
    if not draft:
        sys.exit(
            f"No known DFlash draft for '{args.model}'. Pass --draft <z-lab/...-DFlash> "
            f"explicitly, or --list to see supported targets."
        )
    spec = {
        "method": "dflash",
        "model": draft,
        "num_speculative_tokens": args.num_speculative_tokens,
    }
    cmd = [
        "vllm", "serve", args.model,
        "--host", args.host,
        "--port", str(args.port),
        "--speculative-config", json.dumps(spec),
        "--attention-backend", args.attention_backend,
        "--max-num-batched-tokens", str(args.max_num_batched_tokens),
    ]
    if args.api_key:
        cmd += ["--api-key", args.api_key]
    if args.extra:
        cmd += args.extra
    return cmd


def main() -> None:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--model", help="Target model id (HF repo).")
    p.add_argument("--draft", help="Override the DFlash draft model id.")
    p.add_argument("--host", default="0.0.0.0")
    p.add_argument("--port", type=int, default=8000)
    p.add_argument("--num-speculative-tokens", type=int, default=15)
    p.add_argument("--attention-backend", default="flash_attn")
    p.add_argument("--max-num-batched-tokens", type=int, default=32768)
    p.add_argument("--api-key", default="", help="Optional vLLM API key.")
    p.add_argument("--print", action="store_true", help="Print the command and exit (dry run).")
    p.add_argument("--list", action="store_true", help="List supported target→draft pairs.")
    p.add_argument("extra", nargs="*", help="Extra args passed through to `vllm serve`.")
    args = p.parse_args()

    if args.list:
        print("Supported DFlash target → draft:")
        for tgt, drf in DFLASH_DRAFTS.items():
            print(f"  {tgt:42s} → {drf}")
        return
    if not args.model:
        p.error("--model is required (or use --list)")

    cmd = build_command(args)
    printable = " ".join(json.dumps(c) if " " in c else c for c in cmd)
    print(f"# vLLM + DFlash launch command:\n{printable}\n")
    if args.print:
        return
    if shutil.which("vllm") is None:
        sys.exit("`vllm` not found on PATH. Install with: uv pip install -e \".[vllm]\" "
                 "(see the dflash README). This needs an NVIDIA/AMD GPU on Linux/WSL2.")
    raise SystemExit(subprocess.call(cmd))


if __name__ == "__main__":
    main()
