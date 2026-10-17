"""Benchmark baseline без обучения: Qwen3-VL-2B GGUF через локальный llama-server на одном видео.

Печатает только технические агрегаты (время, VRAM, валидность JSON, число окон/кадров).
Ответы модели и кадры сохраняются локально в --out (каталог outputs/ исключён из Git) и в консоль не выводятся.

    python -m app.inference.bench_baseline --video <path.mov> --repeat 2
"""
from __future__ import annotations

import argparse
import json
import platform
import subprocess
import sys
import threading
import time
from datetime import datetime
from pathlib import Path

from app.contracts import AnalysisError

from . import prompts
from .video import probe, sample_frames, windows
from .vlm import LlamaServer, ServerConfig

ROOT = Path(__file__).resolve().parents[2]
DEFAULT_MODELS = ROOT / "models"


class VramMonitor:
    """Пик memory.used всего GPU через nvidia-smi (per-process на Windows/WDDM обычно недоступен)."""

    def __init__(self, gpu: int = 0, period_ms: int = 100):
        self.cmd = ["nvidia-smi", "-i", str(gpu), "--query-gpu=memory.used", "--format=csv,noheader,nounits",
                    "-lms", str(period_ms)]
        self.peak = self.last = 0
        self.proc: subprocess.Popen | None = None

    @staticmethod
    def snapshot(gpu: int = 0) -> dict:
        r = subprocess.run(["nvidia-smi", "-i", str(gpu), "--query-gpu=name,memory.used,memory.total,driver_version",
                            "--format=csv,noheader,nounits"], capture_output=True, text=True, timeout=10)
        name, used, total, drv = [x.strip() for x in r.stdout.strip().split(",")]
        return {"name": name, "used_mib": int(used), "total_mib": int(total), "driver": drv}

    def start(self) -> None:
        self.proc = subprocess.Popen(self.cmd, stdout=subprocess.PIPE, text=True,
                                     creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        threading.Thread(target=self._read, daemon=True).start()

    def _read(self) -> None:
        for line in self.proc.stdout:
            try:
                self.last = int(line.strip())
                self.peak = max(self.peak, self.last)
            except ValueError:
                pass

    def stop(self) -> None:
        if self.proc:
            self.proc.terminate()


def run_video(server: LlamaServer, video: Path, work: Path, args, raw_log) -> dict:
    t0 = time.monotonic()
    info = probe(video)
    frames = sample_frames(video, work / "frames", args.step, info, model_height=args.model_height)
    t_decode = time.monotonic() - t0
    wins = windows(frames, args.window)
    per_win, n_valid, n_unassessable, n_truncated = [], 0, 0, 0
    prompt_tokens = gen_tokens = 0
    for wi, w in enumerate(wins):
        idx = [f.index for f in w]
        tw = time.monotonic()
        resp = server.chat(prompts.window_parts(w), prompts.schema(idx), args.max_tokens, timeout=args.req_timeout)
        dt = time.monotonic() - tw
        text = resp["choices"][0]["message"].get("content") or ""
        n_truncated += resp["choices"][0].get("finish_reason") == "length"
        try:
            obj = json.loads(text)
            ok = prompts.validate(obj, idx)
        except json.JSONDecodeError:
            obj, ok = None, False
        n_valid += ok
        n_unassessable += bool(ok and not obj["frames_assessable"])
        u = resp.get("usage", {})
        prompt_tokens += u.get("prompt_tokens", 0)
        gen_tokens += u.get("completion_tokens", 0)
        per_win.append(round(dt, 3))
        # Содержательный ответ — только в локальный файл.
        raw_log.write(json.dumps({"window": wi, "frames": [[f.index, round(f.pts_sec, 3)] for f in w],
                                  "valid": ok, "content": text, "timings": resp.get("timings")},
                                 ensure_ascii=False) + "\n")
    total = time.monotonic() - t0
    return {
        "duration_sec": round(info.duration_sec, 3), "resolution": f"{info.width}x{info.height}",
        "rotation": info.rotation, "codec": info.codec, "avg_fps": round(info.avg_fps, 3),
        "frames_sampled": len(frames), "windows": len(wins), "frames_per_window_max": max(len(w) for w in wins),
        "decode_sec": round(t_decode, 3), "inference_sec": round(sum(per_win), 3),
        "window_sec_mean": round(sum(per_win) / len(per_win), 3), "window_sec_max": max(per_win),
        "total_sec": round(total, 3), "realtime_factor": round(total / info.duration_sec, 3),
        "json_valid": f"{n_valid}/{len(wins)}", "truncated_by_max_tokens": n_truncated, "windows_marked_unassessable": n_unassessable,
        "prompt_tokens": prompt_tokens, "completion_tokens": gen_tokens,
        "pts_monotonic": all(b.pts_sec > a.pts_sec for a, b in zip(frames, frames[1:])),
        "pts_within_duration": all(0 <= f.pts_sec <= info.duration_sec for f in frames),
    }


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--video", type=Path, required=True)
    ap.add_argument("--models", type=Path, default=DEFAULT_MODELS)
    ap.add_argument("--llama-tag", default="b11669")
    ap.add_argument("--quant", default="Q8_0", choices=["Q8_0", "Q4_K_M", "F16"])
    ap.add_argument("--mmproj", default="F16", choices=["F16", "Q8_0"])
    ap.add_argument("--step", type=float, default=1.0, help="мин. интервал между кадрами, с")
    ap.add_argument("--window", type=float, default=10.0, help="длина окна, с")
    ap.add_argument("--model-height", type=int, default=0, help="высота кадров для модели, 0 = исходная")
    ap.add_argument("--image-max-tokens", type=int, default=0)
    ap.add_argument("--ctx", type=int, default=16384)
    ap.add_argument("--max-tokens", type=int, default=160)
    ap.add_argument("--req-timeout", type=float, default=120)
    ap.add_argument("--repeat", type=int, default=2, help="1-й проход холодный, остальные тёплые")
    ap.add_argument("--out", type=Path, default=ROOT / "outputs" / "bench")
    args = ap.parse_args()

    if not args.video.is_file():
        print("video not found", file=sys.stderr)
        return 2
    run_dir = args.out / datetime.now().strftime("%Y%m%d-%H%M%S")
    run_dir.mkdir(parents=True, exist_ok=True)
    mdir = args.models / "qwen3-vl-2b"
    cfg = ServerConfig(server_bin=args.models / f"llama.cpp-{args.llama_tag}" / "llama-server.exe",
                       model=mdir / f"Qwen3VL-2B-Instruct-{args.quant}.gguf",
                       mmproj=mdir / f"mmproj-Qwen3VL-2B-Instruct-{args.mmproj}.gguf",
                       ctx=args.ctx, image_max_tokens=args.image_max_tokens)
    gpu_before = VramMonitor.snapshot()
    mon = VramMonitor()
    mon.start()
    server = LlamaServer(cfg, run_dir / "llama-server.log")
    summary: dict = {
        "model": f"Qwen/Qwen3-VL-2B-Instruct-GGUF@52d6c8f {args.quant} + mmproj {args.mmproj}",
        "runtime": f"llama.cpp {args.llama_tag} win-cuda-12.4", "prompt_version": prompts.PROMPT_VERSION,
        "params": {k: (str(v) if isinstance(v, Path) else v) for k, v in vars(args).items()
                   if k not in ("video", "out", "models")},
        "host": {"os": platform.platform(), "python": platform.python_version(), "gpu": gpu_before["name"],
                 "gpu_total_mib": gpu_before["total_mib"], "driver": gpu_before["driver"]},
        "vram_before_mib": gpu_before["used_mib"],
    }
    rc = 0
    try:
        summary["load_sec"] = round(server.start(), 3)
        time.sleep(0.5)
        summary["vram_after_load_mib"] = mon.last
        runs = []
        with open(run_dir / "windows.jsonl", "w", encoding="utf-8") as raw:
            for i in range(args.repeat):
                r = run_video(server, args.video, run_dir / f"run{i}", args, raw)
                r["pass"] = "cold" if i == 0 else "warm"
                runs.append(r)
        summary["runs"] = runs
    except AnalysisError as e:
        summary["error"] = f"{type(e).__name__}: {e}"
        rc = 1
    finally:
        server.stop()
        time.sleep(0.3)
        mon.stop()
        summary["vram_peak_mib"] = mon.peak
        summary["vram_peak_delta_mib"] = mon.peak - gpu_before["used_mib"]
        (run_dir / "summary.json").write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(summary, ensure_ascii=False, indent=2))
    print(f"local run dir: {run_dir}")
    return rc


if __name__ == "__main__":
    sys.exit(main())
