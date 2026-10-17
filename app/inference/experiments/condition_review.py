"""Локальная проверка человеком результатов dev_eval (режим CASH-01): страница и подсчёт агрегатов.

build: по каждому видео — эталонный вопрос человеку (виден ли уход кассира во время обслуживания) и
по каждой модели (скрыта как «Модель 1/2», ключ — model_key.json) — вердикт, кандидаты, условия со
статусами модели и кадрами, на которых они основаны. Человек отмечает, верна ли оценка каждого условия
и верно ли событие. Исходная бинарная метка не показывается, чтобы не влиять на ответ.
score: TP/FP/FN/TN относительно ответов человека, failed отдельно, точность оценок условий.
Печатает только агрегаты. Страница работает из файла, без сети.

    python -m app.inference.experiments.condition_review build outputs/dev_eval/<время>
    python -m app.inference.experiments.condition_review score outputs/dev_eval/<время> --review <export.json>
"""
from __future__ import annotations

import argparse
import html
import json
import random
import shutil
import sys
from pathlib import Path

from ..conditions import CONDITION_RU as _CR1
from ..rules_v4 import CONDITION_RU as _CR2

CONDITION_RU = {**_CR1, **_CR2}
_TYPE_RU = {"cashier_leaves_during_service": "CASH-01 уход кассира",
            "cash_counted_without_client": "CASH-02 пересчёт без клиента",
            "collection_bag_opened_without_collector": "COLLECT-02 сумка без инкассатора",
            "safe_left_unsecured": "SAFE-01 сейф открыт/ключ при уходе",
            "outsider_in_staff_zone": "ACCESS-01 посторонний в зоне кассира"}
from .review import _CSS

_STATUS_RU = {"confirmed": "подтверждено", "refuted": "опровергнуто", "not_established": "не установлено"}
_DECISION_RU = {"event": "СОБЫТИЕ", "excluded": "исключено контрпримером", "insufficient": "не установлено"}

_JS = """
const KEY='assar-cond-'+document.body.dataset.run;
function collect(){const items={};document.querySelectorAll('input[type=radio]:checked').forEach(r=>items[r.name]=r.value);
 const notes={};document.querySelectorAll('textarea').forEach(t=>{if(t.value.trim())notes[t.name]=t.value.trim()});
 return {run_id:document.body.dataset.run,reviewer:document.getElementById('reviewer').value.trim(),items,notes};}
function save(){try{localStorage.setItem(KEY,JSON.stringify(collect()))}catch(e){} count();}
function count(){const g=new Set([...document.querySelectorAll('input[type=radio]')].map(r=>r.name));
 const d=new Set([...document.querySelectorAll('input[type=radio]:checked')].map(r=>r.name));
 document.getElementById('progress').textContent=d.size+' / '+g.size+' вопросов отмечено';}
function restore(){let s=null;try{s=JSON.parse(localStorage.getItem(KEY)||'null')}catch(e){}
 if(!s)return; document.getElementById('reviewer').value=s.reviewer||'';
 for(const [k,v] of Object.entries(s.items||{})){const el=document.querySelector(`input[name="${CSS.escape(k)}"][value="${v}"]`); if(el)el.checked=true;}
 for(const [k,v] of Object.entries(s.notes||{})){const el=document.querySelector(`textarea[name="${CSS.escape(k)}"]`); if(el)el.value=v;}}
function download(){const b=new Blob([JSON.stringify(collect(),null,1)],{type:'application/json'});
 const a=document.createElement('a');a.href=URL.createObjectURL(b);a.download='condition_review_export.json';a.click();}
document.addEventListener('change',save);document.addEventListener('input',save);restore();count();
"""

_EXTRA_CSS = """
table{border-collapse:collapse;width:100%;margin:6px 0} td,th{border-top:1px solid var(--line);padding:4px 6px;
text-align:left;vertical-align:top} .ref{background:var(--card);border-radius:6px;padding:8px;margin:8px 0}
.ev{font-weight:600} textarea{width:100%;min-height:40px;background:var(--bg);color:var(--fg);border:1px solid var(--line)}
.thumbs img{height:90px}
"""


def _radios(name: str, options: tuple[tuple[str, str], ...]) -> str:
    return "".join(f'<label><input type="radio" name="{html.escape(name)}" value="{v}"> {t}</label>'
                   for v, t in options)


def _variants(run_dir: Path) -> dict[str, list[dict]]:
    out = {}
    for p in sorted(run_dir.iterdir()):
        if (p / "results.json").is_file():
            out[p.name] = json.loads((p / "results.json").read_text(encoding="utf-8"))
    if not out:
        raise SystemExit("no <variant>/results.json in run dir")
    return out


def _thumbs(run_dir: Path, assets: dict, label: str, vid: str, frames: list[int], pts: dict[int, float]) -> str:
    """Кадры копируются в review_assets/ под нейтральными именами: путь не раскрывает модель."""
    out = []
    for i in frames:
        src = run_dir / label / "jobs" / vid / "frames" / f"e_{i + 1:05d}.jpg"
        if (label, vid, i) not in assets:
            name = f"f{len(assets):05d}.jpg"
            assets[(label, vid, i)] = name
            if src.is_file():
                (run_dir / "review_assets").mkdir(exist_ok=True)
                shutil.copyfile(src, run_dir / "review_assets" / name)
        out.append(f'<figure><img loading="lazy" src="review_assets/{assets[(label, vid, i)]}" alt="">'
                   f'<figcaption>{i} · {pts.get(i, 0):.1f} с</figcaption></figure>')
    return "".join(out)


def build(run_dir: Path, seed: int = 0) -> Path:
    variants = _variants(run_dir)
    labels = sorted(variants)
    rng = random.Random(seed)
    order = labels[:]
    rng.shuffle(order)
    key = {f"m{n}": lab for n, lab in enumerate(order, start=1)}
    alias = {lab: f"m{n}" for n, lab in enumerate(order, start=1)}
    names = {lab: f"Модель {n}" for n, lab in enumerate(order, start=1)}
    vids = []
    for lab in labels:
        for r in variants[lab]:
            if r["id"] not in vids:
                vids.append(r["id"])
    body, assets = [], {}
    for vid in vids:
        body.append(f'<h2>Видео {html.escape(vid)}</h2><div class="ref"><b>Эталон (ваш ответ по видео):</b> '
                    'есть ли на видео видимое нарушение кассовой дисциплины (любого типа)? '
                    f'<br>{_radios(f"ref|{vid}", (("yes", "Да"), ("no", "Нет"), ("unclear", "Неясно")))}'
                    f'<br><textarea name="refnote|{html.escape(vid)}" placeholder="Примерное время, если да (локально)"></textarea></div>')
        for lab in order:
            r = next((x for x in variants[lab] if x["id"] == vid), None)
            if r is None:
                continue
            tr = r.get("trace") or {}
            pts = {f[0]: f[1] for f in tr.get("timeline", [])}
            parts = [f'<div class="side"><b>{names[lab]}</b>: итог <b>{html.escape(r["outcome"])}</b>, '
                     f'{r["sec"]:.1f} с']
            cands = tr.get("candidates", [])
            if not cands:
                parts.append('<p class="mut">Кандидатов (отсутствие кассира ≥ 3 кадров) нет.</p>')
            for ci, c in enumerate(cands):
                cid = f"{vid}|{alias[lab]}|{ci}"
                tname = _TYPE_RU.get(c.get("type_id", "cashier_leaves_during_service"), c.get("type_id", ""))
                parts.append(f'<div class="obs"><span class="ev">Кандидат {ci + 1} [{html.escape(tname)}]: '
                             f'{c["absent"][0]:.1f}–'
                             f'{c["absent"][1]:.1f} с — {_DECISION_RU[c["decision"]]}</span>')
                if c["decision"] == "event":
                    parts.append(f'<br>Событие: {_radios(f"event|{cid}", (("correct", "Верно"), ("false", "Ложное"), ("unclear", "Неясно")))}')
                rows = []
                for k, chk in c["checks"].items():
                    rows.append(f'<tr><td>{html.escape(CONDITION_RU[k])}</td><td>{_STATUS_RU[chk["status"]]}'
                                f'{" · " + html.escape(chk["note"]) if chk["note"] else ""}</td>'
                                f'<td><div class="thumbs">{_thumbs(run_dir, assets, lab, vid, chk["frames"][:6], pts)}</div></td>'
                                f'<td>{_radios(f"cond|{cid}|{k}", (("right", "Оценка верна"), ("wrong", "Неверна"), ("unclear", "Неясно")))}</td></tr>')
                parts.append(f'<table><tr><th>Условие</th><th>Оценка модели</th><th>Кадры</th><th>Ваша проверка</th>'
                             f'</tr>{"".join(rows)}</table></div>')
            parts.append("</div>")
            body.append("".join(parts))
    run_id = run_dir.name
    (run_dir / "model_key.json").write_text(json.dumps({"run_id": run_id, "key": key}), encoding="utf-8")
    page = (f'<!doctype html><html lang="ru"><head><meta charset="utf-8"><meta name="viewport" '
            f'content="width=device-width,initial-scale=1"><title>Проверка CASH-01</title>'
            f'<style>{_CSS}{_EXTRA_CSS}</style></head><body data-run="{html.escape(run_id)}"><main><div class="bar">'
            '<h1>Проверка условий CASH-01</h1><p class="mut">Сначала ответьте на эталонный вопрос по видео, затем '
            'проверьте оценки моделей. Оценки условий — мнение модели по кадрам; отмечайте, что видно на самом '
            'деле. Страница локальная и ничего не отправляет.</p><label>Проверяющий <input id="reviewer"></label> '
            '<span id="progress" class="mut"></span> <button onclick="download()">Скачать '
            f'condition_review_export.json</button></div>{"".join(body)}</main><script>{_JS}</script></body></html>')
    out = run_dir / "condition_review.html"
    out.write_text(page, encoding="utf-8")
    return out


def score(run_dir: Path, review_path: Path) -> dict:
    variants = _variants(run_dir)
    key = json.loads((run_dir / "model_key.json").read_text(encoding="utf-8"))["key"]
    alias = {lab: a for a, lab in key.items()}
    review = json.loads(Path(review_path).read_text(encoding="utf-8"))
    if review.get("run_id") != run_dir.name:
        raise SystemExit("review export belongs to another run")
    items = review.get("items", {})
    out = {"run_id": run_dir.name, "reference_answers": {}, "variants": {},
           "note": "Reference is the reviewer's per-video answer for CASH-01; not event F1; failed shown separately."}
    refs = {k.split("|", 1)[1]: v for k, v in items.items() if k.startswith("ref|")}
    for v in refs.values():
        out["reference_answers"][v] = out["reference_answers"].get(v, 0) + 1
    for lab, results in variants.items():
        b = {"videos": len(results), "tp": 0, "fp": 0, "fn": 0, "tn": 0, "failed_ref_yes": 0, "failed_ref_no": 0,
             "ref_unclear_or_missing": 0, "events_correct": 0, "events_false": 0, "events_unclear": 0,
             "conditions": {}}
        for r in results:
            ref = refs.get(r["id"])
            o = r["outcome"]
            if ref not in ("yes", "no"):
                b["ref_unclear_or_missing"] += 1
            elif o.startswith("failed"):
                b[f"failed_ref_{ref}"] += 1
            elif o == "violation":
                b["tp" if ref == "yes" else "fp"] += 1
            else:
                b["fn" if ref == "yes" else "tn"] += 1
            for ci, c in enumerate((r.get("trace") or {}).get("candidates", [])):
                cid = f"{r['id']}|{alias[lab]}|{ci}"
                ev = items.get(f"event|{cid}")
                if ev:
                    b[{"correct": "events_correct", "false": "events_false"}.get(ev, "events_unclear")] += 1
                for k in c["checks"]:
                    ans = items.get(f"cond|{cid}|{k}")
                    if ans:
                        d = b["conditions"].setdefault(k, {"right": 0, "wrong": 0, "unclear": 0})
                        d[ans] += 1
        decided = b["tp"] + b["fp"] + b["fn"] + b["tn"]
        b["decided_videos"] = decided
        b["failed_total"] = b["failed_ref_yes"] + b["failed_ref_no"]
        out["variants"][lab] = b
    return out


def main() -> int:
    ap = argparse.ArgumentParser()
    sub = ap.add_subparsers(dest="cmd", required=True)
    b = sub.add_parser("build")
    b.add_argument("run_dir", type=Path)
    b.add_argument("--seed", type=int, default=0)
    s = sub.add_parser("score")
    s.add_argument("run_dir", type=Path)
    s.add_argument("--review", type=Path, required=True)
    args = ap.parse_args()
    if args.cmd == "build":
        print(f"review page: {build(args.run_dir, args.seed)}")
    else:
        res = score(args.run_dir, args.review)
        (args.run_dir / "condition_score.json").write_text(json.dumps(res, ensure_ascii=False, indent=2),
                                                          encoding="utf-8")
        print(json.dumps(res, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
