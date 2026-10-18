"""Локальная страница слепой ручной проверки: ответы двух вариантов по одинаковым окнам рядом с кадрами.

Варианты показаны как «A»/«B» в случайном порядке для каждого окна; ключ — blind_key.json в каталоге
прогона (не открывать до окончания проверки). Страница работает из файла, без сети; оценки скачиваются
кнопкой в review_export.json. Содержимое остаётся на этом компьютере.

    python -m app.inference.experiments.review outputs/experiments/<run>
"""
from __future__ import annotations

import argparse
import html
import json
import random
import sys
from pathlib import Path

_CSS = """
:root{--bg:#fff;--fg:#1b1f24;--mut:#5b6470;--line:#d6dbe1;--card:#f6f8fa;--acc:#0b63ce}
@media (prefers-color-scheme:dark){:root{--bg:#0f1216;--fg:#e6e9ee;--mut:#9aa4b1;--line:#2b323b;--card:#161b22;--acc:#5aa2ff}}
body{margin:0;background:var(--bg);color:var(--fg);font:14px/1.45 system-ui,sans-serif}
main{max-width:1200px;margin:0 auto;padding:16px}
h1{font-size:20px;margin:0 0 4px} h2{font-size:16px;margin:24px 0 8px}
.mut{color:var(--mut)} .win{border:1px solid var(--line);border-radius:8px;margin:12px 0;padding:12px}
.thumbs{display:flex;gap:4px;overflow-x:auto;padding-bottom:4px} .thumbs figure{margin:0;text-align:center}
.thumbs img{height:120px;border-radius:4px;display:block} .thumbs figcaption{font-size:11px;color:var(--mut)}
.cols{display:grid;grid-template-columns:1fr 1fr;gap:12px;margin-top:8px}
@media (max-width:700px){.cols{grid-template-columns:1fr}}
.side{background:var(--card);border-radius:6px;padding:8px} .obs{border-top:1px solid var(--line);padding:6px 0}
.obs:first-of-type{border-top:0} label{margin-right:10px;white-space:nowrap}
button{background:var(--acc);color:#fff;border:0;border-radius:6px;padding:8px 14px;font:inherit;cursor:pointer}
.bar{position:sticky;top:0;background:var(--bg);border-bottom:1px solid var(--line);padding:8px 0;z-index:1}
"""

_JS = """
const KEY='assar-review-'+document.body.dataset.run;
function collect(){const items={};document.querySelectorAll('input[type=radio]:checked').forEach(r=>items[r.name]=r.value);
 const missed=[...document.querySelectorAll('input[type=checkbox]:checked')].map(c=>c.name);
 return {run_id:document.body.dataset.run,reviewer:document.getElementById('reviewer').value.trim(),items,missed};}
function save(){try{localStorage.setItem(KEY,JSON.stringify(collect()))}catch(e){} count();}
function count(){const n=document.querySelectorAll('.obs').length, d=document.querySelectorAll('.obs input[type=radio]:checked').length;
 document.getElementById('progress').textContent=d+' / '+n+' наблюдений оценено';}
function restore(){let s=null;try{s=JSON.parse(localStorage.getItem(KEY)||'null')}catch(e){}
 if(!s)return; document.getElementById('reviewer').value=s.reviewer||'';
 for(const [k,v] of Object.entries(s.items||{})){const el=document.querySelector(`input[name="${CSS.escape(k)}"][value="${v}"]`); if(el)el.checked=true;}
 for(const k of s.missed||[]){const el=document.querySelector(`input[name="${CSS.escape(k)}"]`); if(el)el.checked=true;}}
function download(){const b=new Blob([JSON.stringify(collect(),null,1)],{type:'application/json'});
 const a=document.createElement('a');a.href=URL.createObjectURL(b);a.download='review_export.json';a.click();}
document.addEventListener('change',save);restore();count();
"""


def _obs_block(key: str, content: str | None, status: str) -> str:
    if status != "ok":
        return f'<p class="mut">Ответ не принят: {html.escape(status)}</p>'
    obj = json.loads(content)
    parts = []
    if not obj["frames_assessable"]:
        parts.append('<p class="mut">Модель: кадры непригодны для оценки</p>')
    if not obj["observations"]:
        parts.append('<p class="mut">Наблюдений нет</p>')
    for i, o in enumerate(obj["observations"]):
        name = f"{key}|{i}"
        radios = "".join(f'<label><input type="radio" name="{html.escape(name)}" value="{v}"> {t}</label>'
                         for v, t in (("confirmed", "Подтверждено"), ("not_confirmed", "Не подтверждено"),
                                      ("unclear", "Неясно")))
        parts.append(f'<div class="obs"><b>{html.escape(o["category"])}</b> · кадры '
                     f'{html.escape(", ".join(map(str, o["frame_indices"])))}<br>{html.escape(o["description"])}'
                     f'<br>{radios}</div>')
    parts.append(f'<label><input type="checkbox" name="{html.escape(key)}|missed"> Видимое действие из списка '
                 'категорий пропущено этим ответом</label>')
    return "".join(parts)


def build(run_dir: Path, seed: int = 0) -> Path:
    frames = json.loads((run_dir / "frames.json").read_text(encoding="utf-8"))
    rows = [json.loads(line) for line in (run_dir / "responses.jsonl").read_text(encoding="utf-8").splitlines()]
    variants = sorted({r["variant"] for r in rows})
    if len(variants) != 2:
        raise SystemExit("blind review expects exactly two variants")
    by_win: dict[tuple[str, int], dict[str, dict]] = {}
    for r in rows:
        by_win.setdefault((r["video"], r["window"]), {})[r["variant"]] = r
    rng = random.Random(seed)
    key_map, body = {}, []
    current = None
    for (vid, wi), resp in sorted(by_win.items()):
        if vid != current:
            current = vid
            body.append(f"<h2>Видео {html.escape(vid)}</h2>")
        order = variants[:]
        rng.shuffle(order)
        key_map[f"{vid}|{wi}"] = {"A": order[0], "B": order[1]}
        any_r = next(iter(resp.values()))
        idx = {i for i, _ in any_r["frames"]}
        fr = [f for f in frames[vid]["frames"] if f[0] in idx]
        thumbs = "".join(f'<figure><img loading="lazy" src="{html.escape(p.replace(chr(92), "/"))}" alt="">'
                         f'<figcaption>{i} · {t:.1f} с</figcaption></figure>' for i, t, p in fr)
        cols = "".join(f'<div class="side"><b>Ответ {side}</b>'
                       f'{_obs_block(f"{vid}|{wi}|{side}", resp.get(order[n], {}).get("content"), resp.get(order[n], {}).get("status", "missing"))}</div>'
                       for n, side in enumerate("AB"))
        body.append(f'<section class="win"><b>Окно {wi}</b> <span class="mut">{fr[0][1]:.1f}–{fr[-1][1]:.1f} с</span>'
                    f'<div class="thumbs">{thumbs}</div><div class="cols">{cols}</div></section>')
    run_id = run_dir.name
    (run_dir / "blind_key.json").write_text(json.dumps({"run_id": run_id, "seed": seed, "windows": key_map}),
                                            encoding="utf-8")
    page = (f'<!doctype html><html lang="ru"><head><meta charset="utf-8"><meta name="viewport" '
            f'content="width=device-width,initial-scale=1"><title>Слепая сверка моделей</title><style>{_CSS}</style>'
            f'</head><body data-run="{html.escape(run_id)}"><main><div class="bar"><h1>Слепая сверка: ответ A и B</h1>'
            '<p class="mut">Порядок A/B случайный в каждом окне. Оценивайте только то, что видно на кадрах; '
            'при сомнении — «Неясно». Страница локальная, ничего не отправляет.</p>'
            '<label>Проверяющий <input id="reviewer"></label> <span id="progress" class="mut"></span> '
            '<button onclick="download()">Скачать review_export.json</button></div>'
            f'{"".join(body)}</main><script>{_JS}</script></body></html>')
    out = run_dir / "review.html"
    out.write_text(page, encoding="utf-8")
    return out


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("run_dir", type=Path)
    ap.add_argument("--seed", type=int, default=0)
    args = ap.parse_args()
    print(f"review page: {build(args.run_dir, args.seed)}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
