"""Загрузка файлов эксперимента сравнения моделей (манифест model_compare.json), с проверкой SHA256.

Production-конфигурацию и baseline не меняет: 2B и Windows-runtime берутся из уже скачанного baseline
(app/inference/fetch_runtime.py), здесь докачиваются только 4B, карточки моделей и, по запросу, Linux-сборка.

    python -m app.inference.experiments.fetch --variants 4b-q4km
    python -m app.inference.experiments.fetch --variants 4b-q4km --platform linux-x64
"""
from __future__ import annotations

import argparse
import json
import sys
import tarfile
from pathlib import Path, PurePosixPath

from app.inference.fetch_runtime import download, sha256

ROOT = Path(__file__).resolve().parents[3]
MANIFEST = Path(__file__).with_name("model_compare.json")


def load_manifest(path: Path = MANIFEST) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


def _get(url: str, dst: Path, digest: str | None, size: int | None) -> None:
    if dst.exists() and (digest is None or (dst.stat().st_size == size and sha256(dst) == digest)):
        print(f"ok {dst.relative_to(ROOT)}")
        return
    dst.parent.mkdir(parents=True, exist_ok=True)
    print(f"get {dst.relative_to(ROOT)}")
    download(url, dst)
    if digest is not None and (dst.stat().st_size != size or sha256(dst) != digest):
        dst.unlink()
        raise SystemExit(f"SHA256/size mismatch: {dst.name}")


def fetch_variant(m: dict, name: str, root: Path = ROOT) -> None:
    v = m["variants"][name]
    d = root / v["dir"]
    hf = f"https://huggingface.co/{v['repo']}/resolve/{v['revision']}"
    for key in ("weights", "mmproj"):
        f = v[key]
        _get(f"{hf}/{f['file']}", d / f["file"], f["sha256"], f["bytes"])
    _get(f"{hf}/README.md", d / "README.md", None, None)
    # Лицензия указана в front-matter карточки базовой модели (отдельного LICENSE в репозиториях Qwen нет).
    _get(f"https://huggingface.co/{v['base_repo']}/resolve/{v['base_revision']}/README.md",
         d / v["base_card"], None, None)


def _safe_extract(archive: Path, dest: Path) -> None:
    """Python 3.11.0 без tarfile filter: вручную запрещаем выход за dest, абсолютные пути и внешние ссылки."""
    dest = dest.resolve()
    with tarfile.open(archive, "r:gz") as t:
        for mem in t.getmembers():
            p = PurePosixPath(mem.name)
            if p.is_absolute() or ".." in p.parts or not (dest / p).resolve().is_relative_to(dest):
                raise SystemExit(f"unsafe tar member: {mem.name}")
            if mem.issym() or mem.islnk():
                target = (dest / p.parent / mem.linkname) if mem.issym() else (dest / mem.linkname)
                if PurePosixPath(mem.linkname).is_absolute() or not target.resolve().is_relative_to(dest):
                    raise SystemExit(f"unsafe link in tar: {mem.name}")
            elif not (mem.isfile() or mem.isdir()):
                raise SystemExit(f"unsupported tar member type: {mem.name}")
        t.extractall(dest)


def fetch_linux_runtime(m: dict, root: Path = ROOT) -> Path:
    rt = m["runtime"]["linux-x64"]
    d = root / rt["dir"]
    for a in rt["archives"]:
        dst = d / Path(a["url"]).name
        marker = dst.with_name(dst.name + ".extracted")
        if marker.exists() and marker.read_text().strip() == a["sha256"]:
            print(f"ok (extracted) {dst.relative_to(root)}")
            continue
        _get(a["url"], dst, a["sha256"], a["bytes"])
        _safe_extract(dst, d)
        marker.write_text(a["sha256"])
        dst.unlink()
    found = sorted(d.rglob(rt["server_bin"]))
    if not found:
        raise SystemExit("llama-server not found in extracted Linux archives")
    print(f"linux server: {found[0].relative_to(root)}")
    return found[0]


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--variants", default="4b-q4km", help="через запятую; 2b-q8 обычно уже скачан baseline")
    ap.add_argument("--platform", choices=["windows-x64", "linux-x64"], default="windows-x64")
    args = ap.parse_args()
    m = load_manifest()
    for name in args.variants.split(","):
        fetch_variant(m, name.strip())
    if args.platform == "linux-x64":
        fetch_linux_runtime(m)
    else:
        server = ROOT / m["runtime"]["windows-x64"]["dir"] / m["runtime"]["windows-x64"]["server_bin"]
        if not server.is_file():
            print("Windows runtime missing: run python app/inference/fetch_runtime.py --dest models", file=sys.stderr)
            return 1
    print("experiment files ready")
    return 0


if __name__ == "__main__":
    sys.exit(main())
