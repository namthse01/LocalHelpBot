"""hardware.py — cross-platform machine probing + model recommendation.

This module is imported by ``install.py`` **before** any third-party
package is installed, so it MUST stay on the Python standard library
only. ``psutil`` is used opportunistically if it happens to be present
(e.g. when the app calls this at runtime) but is never required.

The job: figure out how much RAM the box has, how many CPU cores, and
whether there's a usable GPU + how much VRAM — then map that to the
right Ollama model sizes so a fresh clone auto-downloads weights that
actually fit the machine instead of OOM-ing on the first chat.
"""
from __future__ import annotations

import os
import platform
import shutil
import subprocess
from dataclasses import dataclass, field


# ── RAM ──────────────────────────────────────────────────────────
def total_ram_gb() -> float:
    """Total physical RAM in GiB. Best-effort, never raises."""
    # Fast path: psutil if already installed.
    try:
        import psutil  # type: ignore

        return round(psutil.virtual_memory().total / (1024**3), 1)
    except Exception:
        pass

    system = platform.system()
    try:
        if system == "Windows":
            import ctypes

            class _MEMORYSTATUSEX(ctypes.Structure):
                _fields_ = [
                    ("dwLength", ctypes.c_ulong),
                    ("dwMemoryLoad", ctypes.c_ulong),
                    ("ullTotalPhys", ctypes.c_ulonglong),
                    ("ullAvailPhys", ctypes.c_ulonglong),
                    ("ullTotalPageFile", ctypes.c_ulonglong),
                    ("ullAvailPageFile", ctypes.c_ulonglong),
                    ("ullTotalVirtual", ctypes.c_ulonglong),
                    ("ullAvailVirtual", ctypes.c_ulonglong),
                    ("ullAvailExtendedVirtual", ctypes.c_ulonglong),
                ]

            stat = _MEMORYSTATUSEX()
            stat.dwLength = ctypes.sizeof(_MEMORYSTATUSEX)
            ctypes.windll.kernel32.GlobalMemoryStatusEx(ctypes.byref(stat))
            return round(stat.ullTotalPhys / (1024**3), 1)

        if system == "Linux":
            with open("/proc/meminfo", "r", encoding="utf-8") as f:
                for line in f:
                    if line.startswith("MemTotal:"):
                        kb = int(line.split()[1])
                        return round(kb / (1024**2), 1)

        if system == "Darwin":
            out = subprocess.check_output(
                ["sysctl", "-n", "hw.memsize"], text=True
            ).strip()
            return round(int(out) / (1024**3), 1)
    except Exception:
        pass

    # Last resort: assume a modest laptop so we pick a safe small model.
    return 8.0


# ── GPU ──────────────────────────────────────────────────────────
@dataclass
class GpuInfo:
    present: bool = False
    vendor: str = ""          # "nvidia" | "apple" | "amd" | ""
    name: str = ""
    vram_gb: float = 0.0      # 0 when unknown / shared


def detect_gpu() -> GpuInfo:
    """Detect a usable GPU for LLM inference. Best-effort, never raises."""
    # NVIDIA — the common, well-supported case for Ollama.
    if shutil.which("nvidia-smi"):
        try:
            out = subprocess.check_output(
                [
                    "nvidia-smi",
                    "--query-gpu=name,memory.total",
                    "--format=csv,noheader,nounits",
                ],
                text=True,
                stderr=subprocess.DEVNULL,
            ).strip()
            first = out.splitlines()[0]
            name, mem_mib = (p.strip() for p in first.split(","))
            return GpuInfo(
                present=True,
                vendor="nvidia",
                name=name,
                vram_gb=round(int(mem_mib) / 1024, 1),
            )
        except Exception:
            return GpuInfo(present=True, vendor="nvidia", name="NVIDIA GPU")

    # Apple Silicon — unified memory, Metal-accelerated in Ollama.
    if platform.system() == "Darwin" and platform.machine() == "arm64":
        # VRAM is shared with system RAM; report 0 and let the RAM tier drive.
        return GpuInfo(present=True, vendor="apple", name="Apple Silicon (Metal)")

    # AMD ROCm (Linux, less common for Ollama).
    if shutil.which("rocm-smi"):
        return GpuInfo(present=True, vendor="amd", name="AMD GPU (ROCm)")

    return GpuInfo(present=False)


# ── Recommendation ───────────────────────────────────────────────
@dataclass
class Recommendation:
    tier: str                         # "tiny" | "low" | "mid" | "high"
    ram_gb: float
    cpu_cores: int
    gpu: GpuInfo
    chat_model: str
    embed_model: str
    vision_model: str
    large_model: str                  # "" when the box can't host one
    notes: list[str] = field(default_factory=list)

    def pull_list(self) -> list[str]:
        """Distinct Ollama models to `ollama pull`, large first-skipped."""
        models = [self.chat_model, self.embed_model, self.vision_model]
        if self.large_model:
            models.append(self.large_model)
        seen: list[str] = []
        for m in models:
            if m and m not in seen:
                seen.append(m)
        return seen


def recommend(ram_gb: float | None = None,
              gpu: GpuInfo | None = None,
              cpu_cores: int | None = None) -> Recommendation:
    """Map detected hardware to an Ollama model set.

    The driving budget is VRAM when a discrete GPU is present (GPU
    inference is what most people will actually use), otherwise system
    RAM. Apple Silicon shares memory, so it falls back to the RAM tier.
    """
    ram_gb = total_ram_gb() if ram_gb is None else ram_gb
    gpu = detect_gpu() if gpu is None else gpu
    cpu_cores = (os.cpu_count() or 4) if cpu_cores is None else cpu_cores

    # Effective budget (GiB) that decides model size.
    if gpu.present and gpu.vendor == "nvidia" and gpu.vram_gb > 0:
        budget = gpu.vram_gb
        budget_src = f"{gpu.vram_gb:.0f} GB VRAM ({gpu.name})"
    else:
        budget = ram_gb
        budget_src = f"{ram_gb:.0f} GB RAM"

    notes: list[str] = [f"Model sizing driven by {budget_src}."]

    # Abliterated Qwen 2.5 family is the project default chat model.
    # Tiers chosen so weights + KV cache comfortably fit the budget.
    if budget < 6:
        tier = "tiny"
        chat = "llama3.2:3b"
        embed = "nomic-embed-text:latest"   # 274 MB, light on RAM
        vision = "moondream:latest"          # 1.7 GB tiny VLM
        large = ""
        notes.append("Very low memory - using 3B chat + tiny embed/vision.")
    elif budget < 12:
        tier = "low"
        chat = "huihui_ai/qwen2.5-abliterate:7b"
        embed = "mxbai-embed-large:latest"
        vision = "llava:7b"
        large = ""
    elif budget < 24:
        tier = "mid"
        chat = "huihui_ai/qwen2.5-abliterate:14b"   # project default
        embed = "mxbai-embed-large:latest"
        vision = "llava:latest"
        large = ""
        notes.append("Comfortable mid-tier - 14B default chat model.")
    else:
        tier = "high"
        chat = "huihui_ai/qwen2.5-abliterate:32b"
        embed = "mxbai-embed-large:latest"
        vision = "llava:latest"
        large = "glm-4.7-flash:latest"
        notes.append("High memory - 32B chat + a large reasoning model.")

    if gpu.present and gpu.vendor == "apple":
        notes.append("Apple Silicon: Ollama uses Metal; RAM is shared with GPU.")
    elif not gpu.present:
        notes.append("No discrete GPU — running on CPU; expect slower tokens/s.")

    return Recommendation(
        tier=tier,
        ram_gb=ram_gb,
        cpu_cores=cpu_cores,
        gpu=gpu,
        chat_model=chat,
        embed_model=embed,
        vision_model=vision,
        large_model=large,
        notes=notes,
    )


def summary() -> str:
    """One-shot human-readable probe — handy for `python -m core.hardware`."""
    rec = recommend()
    bar = "-" * 40
    lines = [
        "TheAgent0 - hardware probe",
        bar,
        f"OS         : {platform.system()} {platform.release()} ({platform.machine()})",
        f"Python     : {platform.python_version()}",
        f"CPU cores  : {rec.cpu_cores}",
        f"RAM        : {rec.ram_gb:.0f} GB",
        f"GPU        : {rec.gpu.name or 'none'}"
        + (f" - {rec.gpu.vram_gb:.0f} GB VRAM" if rec.gpu.vram_gb else ""),
        f"Tier       : {rec.tier}",
        bar,
        "Recommended models:",
        f"  chat   : {rec.chat_model}",
        f"  embed  : {rec.embed_model}",
        f"  vision : {rec.vision_model}",
        f"  large  : {rec.large_model or '(none - machine too small)'}",
        "",
    ]
    lines += [f"- {n}" for n in rec.notes]
    return "\n".join(lines)


if __name__ == "__main__":
    print(summary())
