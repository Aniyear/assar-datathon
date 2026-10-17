"""Скачивание закреплённого runtime baseline: llama.cpp (CUDA 12.4, Windows x64) и GGUF Qwen3-VL-2B.

Одноразовый шаг подготовки с доступом в интернет. После него inference работает офлайн.
Все файлы проверяются по SHA256; уже скачанный файл с верным хешем повторно не загружается.

    python app/inference/fetch_runtime.py --dest models
"""
from __future__ import annotations

import argparse
import hashlib
import sys
import urllib.request
import zipfile
from pathlib import Path

LLAMA_TAG = "b11669"
GGUF_REPO = "Qwen/Qwen3-VL-2B-Instruct-GGUF"
GGUF_REV = "52d6c8ffea26cc873ac5ad116f8631268d7eb503"
BASE_REPO = "Qwen/Qwen3-VL-2B-Instruct"
BASE_REV = "89644892e4d85e24eaac8bacfd4f463576704203"

_GH = f"https://github.com/ggml-org/llama.cpp/releases/download/{LLAMA_TAG}"
_HF = f"https://huggingface.co/{GGUF_REPO}/resolve/{GGUF_REV}"

# (relative destination, url, sha256 or None for small text files, unzip_to)
FILES = [
    (f"llama.cpp-{LLAMA_TAG}/llama-bin.zip", f"{_GH}/llama-{LLAMA_TAG}-bin-win-cuda-12.4-x64.zip",
     "c2da1f73ed176ae093ac5aceb978a7b167a16113f6d74c6789eeaee2135cb8dc", f"llama.cpp-{LLAMA_TAG}"),
    (f"llama.cpp-{LLAMA_TAG}/cudart.zip", f"{_GH}/cudart-llama-bin-win-cuda-12.4-x64.zip",
     "8c79a9b226de4b3cacfd1f83d24f962d0773be79f1e7b75c6af4ded7e32ae1d6", f"llama.cpp-{LLAMA_TAG}"),
    (f"llama.cpp-{LLAMA_TAG}/LICENSE", f"https://raw.githubusercontent.com/ggml-org/llama.cpp/{LLAMA_TAG}/LICENSE",
     None, None),
    ("qwen3-vl-2b/Qwen3VL-2B-Instruct-Q8_0.gguf", f"{_HF}/Qwen3VL-2B-Instruct-Q8_0.gguf",
     "1e8db19207c8ce0733ddd78c2eff8a9e22c27c82f4443df94c25792ed8fe04f2", None),
    ("qwen3-vl-2b/mmproj-Qwen3VL-2B-Instruct-F16.gguf", f"{_HF}/mmproj-Qwen3VL-2B-Instruct-F16.gguf",
     "c3d5afbef5287953acd57b4043d2269456e5761a4eaccb3b71b062996970aea5", None),
    ("qwen3-vl-2b/mmproj-Qwen3VL-2B-Instruct-Q8_0.gguf", f"{_HF}/mmproj-Qwen3VL-2B-Instruct-Q8_0.gguf",
     "f9a68fabba69c3b81e153367b2c7521030b0fa8bb0de400c9599c8e6725f9c82", None),
    ("qwen3-vl-2b/README.md", f"{_HF}/README.md", None, None),
    # В репозитории Qwen нет файла LICENSE; лицензия (apache-2.0) указана в front-matter карточки базовой модели.
    ("qwen3-vl-2b/BASE_MODEL_CARD.md", f"https://huggingface.co/{BASE_REPO}/resolve/{BASE_REV}/README.md", None, None),
]


def sha256(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 22), b""):
            h.update(chunk)
    return h.hexdigest()


def download(url: str, dst: Path) -> None:
    tmp = dst.with_suffix(dst.suffix + ".part")
    with urllib.request.urlopen(url, timeout=60) as r, open(tmp, "wb") as f:
        total = int(r.headers.get("Content-Length") or 0)
        done = 0
        while chunk := r.read(1 << 22):
            f.write(chunk)
            done += len(chunk)
            if total:
                print(f"\r  {dst.name}: {done / total:6.1%}", end="", flush=True)
    print()
    tmp.replace(dst)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--dest", type=Path, default=Path("models"))
    ap.add_argument("--keep-zip", action="store_true")
    args = ap.parse_args()

    for rel, url, digest, unzip_to in FILES:
        dst = args.dest / rel
        dst.parent.mkdir(parents=True, exist_ok=True)
        marker = dst.with_suffix(".extracted") if unzip_to else None
        if marker is not None and marker.exists() and marker.read_text().strip() == digest:
            print(f"ok (extracted) {rel}")
            continue
        if dst.exists() and (digest is None or sha256(dst) == digest):
            print(f"ok {rel}")
        else:
            print(f"get {rel}")
            download(url, dst)
            if digest is not None and (got := sha256(dst)) != digest:
                dst.unlink()
                print(f"SHA256 mismatch for {rel}: {got}", file=sys.stderr)
                return 1
        if unzip_to:
            with zipfile.ZipFile(dst) as z:
                root = (args.dest / unzip_to).resolve()
                for m in z.namelist():
                    if not (root / m).resolve().is_relative_to(root):
                        print(f"unsafe zip member {m}", file=sys.stderr)
                        return 1
                z.extractall(root)
            marker.write_text(digest)
            if not args.keep_zip:
                dst.unlink()
    print(f"runtime ready in {args.dest.resolve()}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
