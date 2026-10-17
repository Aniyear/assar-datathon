"""Сравнение моделей на одинаковых кадрах, окнах, промпте и визуальном бюджете (model_compare.json).

Кадры каждого видео декодируются один раз и подаются всем вариантам. Варианты запускаются строго по
очереди (одна копия llama-server на GPU). Содержательные ответы и кадры остаются в --out (outputs/,
исключён из Git); в консоль и summary.json — только технические агрегаты.

    python -m app.inference.experiments.compare --videos outputs/experiments/videos.local.json
"""
from __future__ import annotations

import argparse
import json
import platform
import statistics
import sys
import time
from datetime import datetime
from pathlib import Path
from typing import Callable

from app.contracts import AnalysisError, ModelUnavailableError

from .. import prompts
from ..bench_baseline import VramMonitor
from ..video import Frame, probe, sample_frames, windows
from ..vlm import LlamaServer, ServerConfig
from .fetch import MANIFEST, ROOT, load_manifest

ROLES = ("diagnostic", "dev")


def load_videos(path: Path) -> list[dict]:
    """[{"id", "path", "role": diagnostic|dev, "source_label": optional}] — список выбирает человек заранее."""
    items = json.loads(Path(path).read_text(encoding="utf-8"))
    ids = set()
    for it in items:
        if it.get("role") not in ROLES:
            raise SystemExit(f"role must be one of {ROLES} (frozen test is not allowed)")
        if not it.get("id") or it["id"] in ids:
            raise SystemExit("each video needs a unique id")
        ids.add(it["id"])
        if not Path(it["path"]).is_file():
            raise SystemExit(f"video file missing for id {it['id']}")
    return items


def server_config(m: dict, variant: str, plat: str, root: Path = ROOT) -> ServerConfig:
    v, s = m["variants"][variant], m["shared"]["server"]
    rt = m["runtime"][plat]
    rdir = root / rt["dir"]
    server_bin = rdir / rt["server_bin"]
    if not server_bin.is_file():
        found = sorted(rdir.rglob(rt["server_bin"]))
        server_bin = found[0] if found else server_bin
    d = root / v["dir"]
    return ServerConfig(server_bin=server_bin, model=d / v["weights"]["file"], mmproj=d / v["mmproj"]["file"],
                        ctx=s["ctx"], n_gpu_layers=s["n_gpu_layers"], parallel=s["parallel"],
                        image_max_tokens=s["image_max_tokens"], offline=s["offline"], no_webui=s["no_webui"],
                        jinja=s["jinja"], extra_args=list(s["extra_args"]))


def _window_status(resp: dict, idx: list[int]) -> tuple[str, dict | None]:
    choice = resp["choices"][0]
    if choice.get("finish_reason") == "length":
        return "truncated", None
    if choice.get("finish_reason") != "stop":
        return "not_finished", None
    try:
        obj = json.loads(choice["message"].get("content") or "")
    except (ValueError, TypeError):
        return "invalid_json", None
    return ("ok", obj) if prompts.validate(obj, idx) else ("invalid_schema", None)


def run_variant(name: str, server, video_frames: dict[str, list[Frame]], m: dict, raw, monitor) -> dict:
    """Все окна всех видео одним сервером. Возвращает агрегаты варианта (без содержимого)."""
    g = m["shared"]["generation"]
    window_sec = m["shared"]["sampling"]["window_sec"]
    call_timeout = m["shared"]["call_timeout_sec"]
    agg: dict = {"load_sec": None, "restarts": 0, "windows": 0, "status": {}, "latency_sec": [],
                 "observations": 0, "categories": {}, "unassessable_windows": 0, "videos": {}}
    agg["load_sec"] = round(server.start(), 3)
    if monitor:
        time.sleep(0.5)
        agg["vram_after_load_mib"] = monitor.last
    try:
        for vid, frames in video_frames.items():
            vstat = {"windows": 0, "ok": 0, "unassessable": 0, "observations": 0, "sec": 0.0}
            for wi, win in enumerate(windows(frames, window_sec)):
                idx = [f.index for f in win]
                t0 = time.monotonic()
                content, obj, usage = None, None, {}
                try:
                    resp = server.client.chat(prompts.window_parts(win), prompts.schema(idx),
                                              max_tokens=g["max_tokens"], timeout=call_timeout,
                                              temperature=g["temperature"], top_k=g["top_k"], seed=g["seed"],
                                              cache_prompt=g["cache_prompt"])
                    status, obj = _window_status(resp, idx)
                    content = resp["choices"][0]["message"].get("content")
                    usage = resp.get("usage") or {}
                except AnalysisError as e:
                    status = f"error:{type(e).__name__}"
                    if isinstance(e, ModelUnavailableError) and agg["restarts"] < 1:
                        agg["restarts"] += 1  # один перезапуск собственного сервера на вариант
                        server.stop()
                        server.start()
                dt = time.monotonic() - t0
                agg["windows"] += 1
                agg["status"][status] = agg["status"].get(status, 0) + 1
                agg["latency_sec"].append(dt)
                vstat["windows"] += 1
                vstat["sec"] += dt
                if status == "ok":
                    vstat["ok"] += 1
                    if not obj["frames_assessable"]:
                        vstat["unassessable"] += 1
                        agg["unassessable_windows"] += 1
                    for o in obj["observations"]:
                        agg["observations"] += 1
                        vstat["observations"] += 1
                        agg["categories"][o["category"]] = agg["categories"].get(o["category"], 0) + 1
                # Содержательный ответ — только в локальный файл.
                raw.write(json.dumps({"variant": name, "video": vid, "window": wi,
                                      "frames": [[f.index, round(f.pts_sec, 3)] for f in win], "status": status,
                                      "latency_sec": round(dt, 3), "prompt_tokens": usage.get("prompt_tokens"),
                                      "completion_tokens": usage.get("completion_tokens"), "content": content},
                                     ensure_ascii=False) + "\n")
            vstat["sec"] = round(vstat["sec"], 3)
            agg["videos"][vid] = vstat
    finally:
        server.stop()
    lat = agg.pop("latency_sec")
    agg["window_sec_mean"] = round(statistics.fmean(lat), 3) if lat else None
    agg["window_sec_max"] = round(max(lat), 3) if lat else None
    return agg


def summarize(agg: dict, videos: list[dict]) -> dict:
    """Видео-уровень: технический failed = хотя бы одно окно не ok; полностью непригодное — отдельно."""
    by = {}
    for v in videos:
        st = agg["videos"].get(v["id"])
        if st is None:
            continue
        outcome = ("failed_technical" if st["ok"] < st["windows"] else
                   "all_unassessable" if st["unassessable"] == st["windows"] else "complete")
        for key in ("all", f"role={v['role']}", f"label={v.get('source_label') or 'unknown'}"):
            b = by.setdefault(key, {"videos": 0, "complete": 0, "failed_technical": 0, "all_unassessable": 0,
                                    "observations": 0, "video_sec_max": 0.0})
            b["videos"] += 1
            b[outcome] += 1
            b["observations"] += st["observations"]
            b["video_sec_max"] = max(b["video_sec_max"], st["sec"])
    for b in by.values():
        b["failed_technical_share"] = round(b["failed_technical"] / b["videos"], 3) if b["videos"] else None
    out = {k: v for k, v in agg.items() if k != "videos"}
    out["by_group"] = by
    return out


def run_experiment(manifest: dict, videos: list[dict], variants: list[str], out: Path, plat: str,
                   server_factory: Callable[[str, ServerConfig, Path], object] | None = None,
                   use_vram: bool = True) -> dict:
    if manifest["prompt_version"] != prompts.PROMPT_VERSION:
        raise SystemExit("manifest prompt_version differs from code; the comparison must use one prompt")
    out.mkdir(parents=True, exist_ok=True)
    smp = manifest["shared"]["sampling"]
    t0 = time.monotonic()
    video_frames: dict[str, list[Frame]] = {}
    frames_index = {}
    for v in videos:
        info = probe(Path(v["path"]))
        fr = sample_frames(Path(v["path"]), out / "frames" / v["id"], smp["step_sec"], info,
                           model_height=smp["model_height"])
        video_frames[v["id"]] = fr
        frames_index[v["id"]] = {"duration_sec": info.duration_sec,
                                 "frames": [[f.index, f.pts_sec, str(f.evidence_path.relative_to(out))] for f in fr]}
    (out / "frames.json").write_text(json.dumps(frames_index), encoding="utf-8")
    decode_sec = time.monotonic() - t0
    factory = server_factory or (lambda name, cfg, log: LlamaServer(cfg, log))
    gpu = VramMonitor.snapshot() if use_vram else None
    summary = {"experiment_id": manifest["experiment_id"], "prompt_version": manifest["prompt_version"],
               "shared": manifest["shared"], "platform": plat, "host": {"os": platform.platform(),
               "gpu": gpu and gpu["name"], "gpu_total_mib": gpu and gpu["total_mib"], "driver": gpu and gpu["driver"]},
               "videos": len(videos), "decode_sec_total": round(decode_sec, 3), "variants": {}}
    with open(out / "responses.jsonl", "w", encoding="utf-8") as raw:
        for name in variants:
            v = manifest["variants"][name]
            mon = None
            if use_vram:
                before = VramMonitor.snapshot()["used_mib"]
                mon = VramMonitor()
                mon.start()
            try:
                server = factory(name, server_config(manifest, name, plat), out / f"llama-server-{name}.log")
                agg = run_variant(name, server, video_frames, manifest, raw, mon)
            finally:
                if mon:
                    time.sleep(0.3)
                    mon.stop()
            s = summarize(agg, videos)
            s.update(model=f"{v['repo']}@{v['revision'][:7]} {v['weights']['quantization']}"
                           f"+mmproj-{v['mmproj']['quantization']}")
            if mon:
                s.update(vram_before_mib=before, vram_peak_mib=mon.peak, vram_peak_delta_mib=mon.peak - before)
            summary["variants"][name] = s
    (out / "summary.json").write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")
    return summary


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--videos", type=Path, required=True)
    ap.add_argument("--variants", default="2b-q8,4b-q4km")
    ap.add_argument("--platform", choices=["windows-x64", "linux-x64"],
                    default="windows-x64" if sys.platform == "win32" else "linux-x64")
    ap.add_argument("--manifest", type=Path, default=MANIFEST)
    ap.add_argument("--out", type=Path, default=ROOT / "outputs" / "experiments")
    args = ap.parse_args()
    run_dir = args.out / datetime.now().strftime("%Y%m%d-%H%M%S")
    summary = run_experiment(load_manifest(args.manifest), load_videos(args.videos),
                             [x.strip() for x in args.variants.split(",")], run_dir, args.platform)
    print(json.dumps(summary, ensure_ascii=False, indent=2))
    print(f"local run dir: {run_dir}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
