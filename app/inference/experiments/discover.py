"""Разведка типов нарушений в train/dev: локальная модель нейтрально описывает кадры вокруг таймкода метки,
человек на локальной странице выбирает тип нарушения из списка норм. Наружу — только счётчики типов.

Таймкод метки используется только здесь, для разметки (не для inference анализатора). Frozen test не брать.
Модель — уже запущенный OpenAI-совместимый сервер на 127.0.0.1 (vLLM с 27B на сервер или llama-server).

    python -m app.inference.experiments.discover run --videos outputs/experiments/videos.traindev.local.json \
        --endpoint http://127.0.0.1:8011 --extra-body '{"model": "qwen3.8-27b", "chat_template_kwargs": {"enable_thinking": false}}'
    python -m app.inference.experiments.discover count outputs/discover/<время> --review <export.json>
"""
from __future__ import annotations

import argparse
import html
import json
import shutil
import sys
import time
from datetime import datetime
from pathlib import Path

from app.contracts import AnalysisError

from ..video import probe, sample_frames
from ..vlm import LlamaClient
from .compare import load_videos
from .fetch import ROOT
from .review import _CSS

PROMPT_VERSION = "discover-v1"
PROMPT = (
    "You see {n} frames in time order from a fixed overhead CCTV camera at a bank cash desk, each preceded by "
    "its index and timestamp. Describe neutrally and concretely what happens, frame by frame where it changes. "
    "Use roles by position only: the cashier (staff side of the desk), a client (customer side), a cash "
    "collector (person bringing or taking a cash-collection bag). Do not identify people. Cover: who is "
    "present and where; what is done with cash, bags, seals, documents, cards, the safe, drawers or boxes; "
    "whether cash is counted, handed over, left on the desk or put away; whether someone leaves or arrives; "
    "the order of these actions with frame indices. Say 'not visible' when something cannot be seen. "
    "Do not judge whether anything is a violation."
)

# Типы из docs/normative_candidates.md и docs/MVP_RULES_REVIEW.md (№ 231) + варианты для остального.
TYPES = [
    ("CASH-01", "п. 22 — кассир покинул рабочее место до окончания обслуживания клиента"),
    ("CASH-02", "п. 20 абз. 1 — приём/пересчёт наличных не при клиенте (после его ухода)"),
    ("CASH-03", "п. 20 абз. 2 — ранее принятые деньги вне запираемого хранилища"),
    ("COLLECT-01", "п. 24 (3–4) — инкассаторская сумка принята без проверки целостности/пломбы"),
    ("COLLECT-02", "п. 25 — повреждённая сумка не вскрыта/не пересчитана при инкассаторе"),
    ("CASH-04", "п. 42 — пересчёт/упаковка без контролирующего работника"),
    ("OTHER", "другое (кратко записать норму/действие в поле примечания, локально)"),
    ("UNCLEAR", "по кадрам нельзя понять"),
]


def run(videos: list[dict], client: LlamaClient, out: Path, half_window: float, step: float, max_tokens: int,
        timeout: float, extra_body: dict | None, include_negatives: bool) -> list[dict]:
    recs = []
    for v in videos:
        tc = v.get("source_timecode_sec")
        if v.get("source_label") != "violation" and not include_negatives:
            continue
        info = probe(Path(v["path"]))
        center = tc if tc is not None else info.duration_sec / 2
        frames = sample_frames(Path(v["path"]), out / "frames" / v["id"], step, info)
        win = [f for f in frames if abs(f.pts_sec - center) <= half_window]
        parts: list = [PROMPT.format(n=len(win))]
        for f in win:
            parts += [f"Frame {f.index}, t={f.pts_sec:.1f}s:", f.model_path]
        t0 = time.monotonic()
        rec = {"id": v["id"], "label": v.get("source_label"), "center_sec": center,
               "frames": [[f.index, round(f.pts_sec, 3), str(f.evidence_path.relative_to(out))] for f in win]}
        try:
            resp = client.chat(parts, None, max_tokens=max_tokens, timeout=timeout, extra_body=extra_body)
            ch = resp["choices"][0]
            rec.update(status="ok" if ch.get("finish_reason") == "stop" else f"finish:{ch.get('finish_reason')}",
                       text=ch["message"].get("content") or "")
        except AnalysisError as e:
            rec.update(status=f"error:{type(e).__name__}", text="")
        rec["sec"] = round(time.monotonic() - t0, 2)
        recs.append(rec)
    return recs


_JS = """
const KEY='assar-discover-'+document.body.dataset.run;
function collect(){const items={};document.querySelectorAll('input[type=radio]:checked').forEach(r=>items[r.name]=r.value);
 const notes={};document.querySelectorAll('textarea').forEach(t=>{if(t.value.trim())notes[t.name]=t.value.trim()});
 return {run_id:document.body.dataset.run,items,notes};}
function save(){try{localStorage.setItem(KEY,JSON.stringify(collect()))}catch(e){}}
function restore(){let s=null;try{s=JSON.parse(localStorage.getItem(KEY)||'null')}catch(e){} if(!s)return;
 for(const [k,v] of Object.entries(s.items||{})){const el=document.querySelector(`input[name="${CSS.escape(k)}"][value="${v}"]`); if(el)el.checked=true;}
 for(const [k,v] of Object.entries(s.notes||{})){const el=document.querySelector(`textarea[name="${CSS.escape(k)}"]`); if(el)el.value=v;}}
function download(){const b=new Blob([JSON.stringify(collect(),null,1)],{type:'application/json'});
 const a=document.createElement('a');a.href=URL.createObjectURL(b);a.download='discover_export.json';a.click();}
document.addEventListener('change',save);document.addEventListener('input',save);restore();
"""


def page(recs: list[dict], out: Path) -> Path:
    body = []
    for r in recs:
        thumbs = "".join(f'<figure><img loading="lazy" src="{html.escape(p.replace(chr(92), "/"))}" alt="">'
                         f'<figcaption>{i} · {t:.1f} с</figcaption></figure>' for i, t, p in r["frames"])
        radios = "<br>".join(f'<label><input type="radio" name="type|{html.escape(r["id"])}" value="{k}"> '
                             f'<b>{k}</b> — {html.escape(d)}</label>' for k, d in TYPES)
        body.append(f'<section class="win"><h2>{html.escape(r["id"])} · метка: {html.escape(str(r["label"]))} · '
                    f'центр {r["center_sec"]:.1f} с</h2><div class="thumbs">{thumbs}</div>'
                    f'<div class="cols"><div class="side"><b>Описание модели</b> ({html.escape(r["status"])})'
                    f'<p style="white-space:pre-wrap">{html.escape(r["text"])}</p></div>'
                    f'<div class="side"><b>Тип нарушения (ваш выбор)</b><br>{radios}<textarea name="note|'
                    f'{html.escape(r["id"])}" placeholder="Примечание (локально)" style="width:100%"></textarea>'
                    '</div></div></section>')
    doc = (f'<!doctype html><html lang="ru"><head><meta charset="utf-8"><meta name="viewport" content="width=device-'
           f'width,initial-scale=1"><title>Разведка типов</title><style>{_CSS}</style></head><body data-run="'
           f'{html.escape(out.name)}"><main><div class="bar"><h1>Какие нарушения в положительных видео</h1>'
           '<p class="mut">Смотрите кадры вокруг таймкода метки (при необходимости — само видео) и выберите тип. '
           'Описание модели — подсказка, не истина. Страница локальная.</p><button onclick="download()">Скачать '
           f'discover_export.json</button></div>{"".join(body)}</main><script>{_JS}</script></body></html>')
    p = out / "discover.html"
    p.write_text(doc, encoding="utf-8")
    return p


def count(run_dir: Path, review_path: Path) -> dict:
    recs = json.loads((run_dir / "discover.json").read_text(encoding="utf-8"))
    review = json.loads(Path(review_path).read_text(encoding="utf-8"))
    if review.get("run_id") != run_dir.name:
        raise SystemExit("review export belongs to another run")
    items = review.get("items", {})
    by_type: dict[str, int] = {}
    for r in recs:
        t = items.get(f"type|{r['id']}", "unmarked")
        by_type[t] = by_type.get(t, 0) + 1
    statuses: dict[str, int] = {}
    for r in recs:
        statuses[r["status"]] = statuses.get(r["status"], 0) + 1
    return {"videos": len(recs), "types": by_type, "model_status": statuses}


def main() -> int:
    ap = argparse.ArgumentParser()
    sub = ap.add_subparsers(dest="cmd", required=True)
    r = sub.add_parser("run")
    r.add_argument("--videos", type=Path, required=True)
    r.add_argument("--endpoint", required=True)
    r.add_argument("--extra-body", default="")
    r.add_argument("--half-window", type=float, default=6.0)
    r.add_argument("--step", type=float, default=0.75)
    r.add_argument("--max-tokens", type=int, default=1200)
    r.add_argument("--timeout", type=float, default=300.0)
    r.add_argument("--include-negatives", action="store_true")
    r.add_argument("--out", type=Path, default=ROOT / "outputs" / "discover")
    c = sub.add_parser("count")
    c.add_argument("run_dir", type=Path)
    c.add_argument("--review", type=Path, required=True)
    args = ap.parse_args()
    if args.cmd == "count":
        print(json.dumps(count(args.run_dir, args.review), ensure_ascii=False, indent=2))
        return 0
    out = args.out / datetime.now().strftime("%Y%m%d-%H%M%S")
    out.mkdir(parents=True, exist_ok=True)
    recs = run(load_videos(args.videos), LlamaClient(args.endpoint), out, args.half_window, args.step,
               args.max_tokens, args.timeout, json.loads(args.extra_body) if args.extra_body else None,
               args.include_negatives)
    (out / "discover.json").write_text(json.dumps(recs, ensure_ascii=False, indent=1), encoding="utf-8")
    p = page(recs, out)
    st: dict[str, int] = {}
    for x in recs:
        st[x["status"]] = st.get(x["status"], 0) + 1
    print(json.dumps({"prompt_version": PROMPT_VERSION, "videos": len(recs), "model_status": st,
                      "sec_total": round(sum(x["sec"] for x in recs), 1)}, ensure_ascii=False))
    print(f"local page: {p}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
