#!/usr/bin/env python3
"""Audit ForteBank source files locally without inspecting or transmitting images.

Only Python's standard library, ffprobe, and optional ffmpeg are required.
The original XLSX and video files remain unchanged. Generated data is private.
Example:
  python scripts/audit_dataset.py --archive PATH/open.zip --decode
"""
from __future__ import annotations

import argparse
import collections
import concurrent.futures
import csv
import hashlib
import html
import json
import os
from pathlib import Path, PurePosixPath
import re
import shutil
import stat
import subprocess
import time
import unicodedata
import xml.etree.ElementTree as ET
import zipfile
import zlib

NS = {"s": "http://schemas.openxmlformats.org/spreadsheetml/2006/main"}
ROOT = Path(__file__).resolve().parents[1]


def digest(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


def crc32(path: Path) -> int:
    value = 0
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(1024 * 1024), b""):
            value = zlib.crc32(chunk, value)
    return value & 0xFFFFFFFF


def extract_safely(archive: Path, target: Path) -> list[dict]:
    target = target.resolve()
    target.mkdir(parents=True, exist_ok=True)
    inventory = []
    seen = set()
    with zipfile.ZipFile(archive) as zf:
        entries = zf.infolist()
        if sum(i.file_size for i in entries) > 20 * 1024**3:
            raise ValueError("Archive exceeds 20 GiB extraction limit")
        # Validate all entries before writing any member.
        for item in entries:
            relative = PurePosixPath(item.filename.replace("\\", "/"))
            if relative.is_absolute() or ".." in relative.parts or any(":" in p for p in relative.parts):
                raise ValueError(f"Unsafe archive member: {item.filename!r}")
            if stat.S_ISLNK(item.external_attr >> 16):
                raise ValueError(f"Archive symlink is not allowed: {item.filename!r}")
            destination = (target / Path(*relative.parts)).resolve()
            if not destination.is_relative_to(target):
                raise ValueError(f"Archive member escapes extraction root: {item.filename!r}")
            key = str(destination).casefold()
            if key in seen:
                raise ValueError(f"Duplicate archive destination: {item.filename!r}")
            seen.add(key)
            inventory.append({"name": item.filename, "bytes": item.file_size,
                              "compressed_bytes": item.compress_size, "crc32": f"{item.CRC:08x}"})
        for item in entries:
            destination = target / Path(*PurePosixPath(item.filename.replace("\\", "/")).parts)
            if item.is_dir():
                destination.mkdir(parents=True, exist_ok=True)
                continue
            destination.parent.mkdir(parents=True, exist_ok=True)
            if destination.exists():
                if destination.stat().st_size != item.file_size or crc32(destination) != item.CRC:
                    raise ValueError(f"Existing file differs; refusing to overwrite: {destination}")
                continue
            with zf.open(item) as src, destination.open("xb") as dst:
                shutil.copyfileobj(src, dst)
            if crc32(destination) != item.CRC:
                raise ValueError(f"CRC check failed: {destination}")
    return inventory


def canonical_id(value: str) -> str:
    value = unicodedata.normalize("NFKC", value.strip())
    value = re.sub(r"\.(mov|mp4)$", "", value, flags=re.I)
    value = value.replace("Х", "X").replace("х", "X").upper()
    match = re.fullmatch(r"X\s*[-–—]\s*(\d+)", value)
    return f"X-{int(match.group(1))}" if match else value


def parse_timestamp(value: str | None) -> float | None:
    if value is None or not value.strip():
        return None
    text = value.strip().lower()
    match = re.fullmatch(r"(?:(\d+)\s*минут[аы]?\s*)?(\d+(?:[.,]\d+)?)\s*секунд[аы]?", text)
    if not match:
        raise ValueError(f"Unsupported source timestamp: {value!r}")
    return 60 * int(match.group(1) or 0) + float(match.group(2).replace(",", "."))


def read_workbook(path: Path) -> dict:
    """Read OOXML values and cell storage/format metadata without editing XLSX."""
    with zipfile.ZipFile(path) as zf:
        shared = []
        if "xl/sharedStrings.xml" in zf.namelist():
            for si in ET.fromstring(zf.read("xl/sharedStrings.xml")).findall("s:si", NS):
                shared.append("".join(t.text or "" for t in si.iter(f"{{{NS['s']}}}t")))
        styles = ET.fromstring(zf.read("xl/styles.xml"))
        formats = {int(e.attrib["numFmtId"]): e.attrib["formatCode"] for e in styles.findall("s:numFmts/s:numFmt", NS)}
        xfs = [int(e.attrib.get("numFmtId", 0)) for e in styles.findall("s:cellXfs/s:xf", NS)]
        rels = {e.attrib["Id"]: e.attrib["Target"] for e in ET.fromstring(zf.read("xl/_rels/workbook.xml.rels"))}
        workbook = ET.fromstring(zf.read("xl/workbook.xml"))
        sheets = []
        for sheet in workbook.findall("s:sheets/s:sheet", NS):
            rel_id = sheet.attrib["{http://schemas.openxmlformats.org/officeDocument/2006/relationships}id"]
            target = rels[rel_id].lstrip("/")
            if not target.startswith("xl/"):
                target = "xl/" + target
            xml = ET.fromstring(zf.read(target))
            cells, rows = [], []
            for row in xml.findall("s:sheetData/s:row", NS):
                row_values = {}
                for cell in row.findall("s:c", NS):
                    cell_type = cell.attrib.get("t", "n")
                    raw = cell.find("s:v", NS)
                    raw_value = raw.text if raw is not None else None
                    if cell_type == "inlineStr":
                        value = "".join(t.text or "" for t in cell.findall("s:is//s:t", NS))
                    elif cell_type == "s":
                        value = shared[int(raw_value)]
                    else:
                        value = raw_value
                    format_id = xfs[int(cell.attrib.get("s", 0))]
                    formula = cell.find("s:f", NS)
                    record = {"cell": cell.attrib["r"], "value": value, "storage_type": cell_type,
                              "num_fmt_id": format_id, "num_fmt_code": formats.get(format_id, "General" if format_id == 0 else None),
                              "formula": formula.text if formula is not None else None}
                    cells.append(record)
                    row_values[re.sub(r"\d+$", "", cell.attrib["r"])] = value
                rows.append({"row": int(row.attrib["r"]), "values": row_values})
            dimension = xml.find("s:dimension", NS)
            sheets.append({"name": sheet.attrib["name"], "dimension": dimension.attrib.get("ref") if dimension is not None else None,
                           "cells": cells, "rows": rows})
    return {"path": str(path.resolve()), "sha256": digest(path), "sheets": sheets}


def fraction(value: str | None) -> float | None:
    if not value or value in {"0/0", "N/A"}:
        return None
    if "/" in value:
        n, d = value.split("/")
        return float(n) / float(d) if float(d) else None
    return float(value)


def inspect_video(path: Path, ffprobe: str) -> dict:
    run = subprocess.run([ffprobe, "-v", "error", "-show_format", "-show_streams", "-print_format", "json", str(path)],
                         capture_output=True, text=True, encoding="utf-8", timeout=120)
    base = {"path": str(path.resolve()), "relative_path": path.relative_to(ROOT).as_posix() if path.is_relative_to(ROOT) else str(path),
            "filename": path.name, "canonical_id": canonical_id(path.stem), "bytes": path.stat().st_size, "sha256": digest(path),
            "probe_returncode": run.returncode, "probe_stderr": run.stderr.strip()}
    if run.returncode:
        return base
    probe = json.loads(run.stdout)
    streams = probe.get("streams", [])
    video = [s for s in streams if s.get("codec_type") == "video"]
    audio = [s for s in streams if s.get("codec_type") == "audio"]
    stream = video[0] if video else {}
    rotation = stream.get("tags", {}).get("rotate")
    for side in stream.get("side_data_list", []):
        if "rotation" in side:
            rotation = side["rotation"]
    base.update({"duration_sec": float(probe.get("format", {}).get("duration", stream.get("duration", 0))),
                 "video_streams": len(video), "audio_streams": len(audio), "codec": stream.get("codec_name"),
                 "width": stream.get("width"), "height": stream.get("height"), "pix_fmt": stream.get("pix_fmt"),
                 "avg_fps": fraction(stream.get("avg_frame_rate")), "nominal_fps": fraction(stream.get("r_frame_rate")),
                 "avg_frame_rate": stream.get("avg_frame_rate"), "r_frame_rate": stream.get("r_frame_rate"),
                 "time_base": stream.get("time_base"), "declared_frame_count": stream.get("nb_frames"),
                 "rotation_degrees": float(rotation) if rotation is not None else 0,
                 "audio_codecs": [s.get("codec_name") for s in audio], "probe": probe})
    return base


def decode_video(video: dict, ffmpeg: str) -> dict:
    start = time.monotonic()
    try:
        run = subprocess.run([ffmpeg, "-nostdin", "-hide_banner", "-loglevel", "error", "-xerror", "-err_detect", "explode",
                              "-threads", "2", "-i", video["path"], "-map", "0:v:0", "-an", "-sn", "-dn", "-fps_mode", "passthrough",
                              "-progress", "pipe:1", "-nostats", "-f", "null", "-"],
                             capture_output=True, text=True, encoding="utf-8", timeout=600)
        progress = dict(line.split("=", 1) for line in run.stdout.splitlines() if "=" in line)
        return {"sha256": video["sha256"], "filename": video["filename"], "returncode": run.returncode,
                "stderr": run.stderr.strip(), "wall_seconds": round(time.monotonic() - start, 3),
                "decoded_frames": int(progress["frame"]) if "frame" in progress else None,
                "progress": progress.get("progress"), "status": "ok" if run.returncode == 0 and not run.stderr.strip() else "error_or_warning"}
    except subprocess.TimeoutExpired:
        return {"sha256": video["sha256"], "filename": video["filename"], "status": "timeout", "wall_seconds": 600}


def write_json(path: Path, value) -> None:
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def write_csv(path: Path, rows: list[dict], fields: list[str]) -> None:
    with path.open("w", encoding="utf-8-sig", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=fields, extrasaction="ignore")
        writer.writeheader()
        writer.writerows(rows)


def review_page(out: Path, videos: list[dict], labels: list[dict]) -> None:
    review_path = out / "review.html"
    # Preserve a locally customized review page, just like the annotation CSV.
    # Exported review JSON is never opened or overwritten by this audit.
    if review_path.exists():
        return
    by_id = {r["canonical_id"]: r for r in labels}
    records = []
    for video in videos:
        source = by_id.get(video["canonical_id"], {})
        records.append({"id": video["canonical_id"], "name": video["filename"], "sha256": video["sha256"],
                        "uri": Path(video["path"]).as_uri(), "verdict": source.get("verdict"),
                        "anchor_sec": source.get("timestamp_sec"), "duration_sec": video.get("duration_sec")})
    payload = json.dumps(records, ensure_ascii=False).replace("<", "\\u003c")
    page = r'''<!doctype html><html lang="ru"><meta charset="utf-8">
<meta http-equiv="Content-Security-Policy" content="default-src 'none'; media-src file:; script-src 'unsafe-inline'; style-src 'unsafe-inline'; connect-src 'none'">
<title>assar — локальный просмотр</title><style>
body{max-width:1200px;margin:28px auto;font:16px system-ui;padding:0 20px;background:#101827;color:#eef2ff}h1{font-size:25px}video{width:100%;max-height:65vh;background:#000}button,select,input,textarea{font:inherit;padding:8px;border-radius:5px}button,select{margin:4px}label{display:block;margin:12px 0}input,textarea{display:block;width:96%}textarea{min-height:70px}small{color:#b4c3db}a{color:#8cd7ff}.grid{display:grid;grid-template-columns:1.6fr 1fr;gap:24px}@media(max-width:850px){.grid{grid-template-columns:1fr}}
</style><h1>assar — локальный просмотр датасета</h1>
<p>Просмотр предназначен для человека на этом компьютере. Не отправляйте видео, кадры или описания их содержания внешним ИИ-сервисам.</p>
<select id="choice"></select><button id="previous">Предыдущий</button><button id="next">Следующий</button>
<div class="grid"><section><video id="player" controls preload="metadata"></video><p id="info"></p><p id="clock"></p>
<button id="anchor">К исходному таймкоду</button><button id="capture">Записать текущее время</button><p><a id="external">Открыть файл отдельным проигрывателем</a></p>
<small>Если браузер не поддерживает MOV, откройте исходный файл по ссылке. Исходная разметка содержит только бинарную метку и одну временную отметку. Дубликаты с разными отметками требуют отдельной проверки.</small></section>
<section><label>Проверяющий<input id="reviewer"></label><label>Тип события (определяет человек)<input id="event_type"></label>
<label>Наблюдаемое действие<textarea id="observation"></textarea></label><label>Время события, сек<input id="timecode_sec" type="number" step="0.01"></label>
<label>Начало / конец эпизода, сек<input id="interval" placeholder="Например: 10.0–14.0"></label>
<label>Правило и источник<textarea id="rule"></textarea></label><label>Неопределённость / примечания<textarea id="notes"></textarea></label>
<button id="save">Сохранить запись в памяти</button><button id="export">Скачать локальные заметки JSON</button><p id="notice"></p>
<small>Заметки хранятся только в памяти страницы до экспорта. Перед закрытием скачайте JSON в приватную папку analysis/local. Дополнительные события добавляйте отдельными записями в CSV/JSON.</small></section></div>
<script>const videos=PAYLOAD; const notes={}; const fields=['reviewer','event_type','observation','timecode_sec','interval','rule','notes']; const $=id=>document.getElementById(id); let current=null;
videos.forEach((v,i)=>{const o=document.createElement('option');o.value=i;o.textContent=v.name; $('choice').append(o)});
function save(){if(current===null)return;notes[videos[current].id]={...Object.fromEntries(fields.map(f=>[f,$(f).value])),canonical_id:videos[current].id,sha256:videos[current].sha256,source_timestamp_sec:videos[current].anchor_sec};}
function select(){save();current=+$('choice').value;const v=videos[current];$('player').src=v.uri;$('external').href=v.uri;$('info').textContent=`${v.id}; исходная метка: ${v.verdict}; отметка: ${v.anchor_sec??'нет'} с; длительность: ${v.duration_sec} с`;fields.forEach(f=>$(f).value=notes[v.id]?.[f]??'');$('notice').textContent='';}
$('choice').onchange=select;$('previous').onclick=()=>{$('choice').value=Math.max(0,current-1);select()};$('next').onclick=()=>{$('choice').value=Math.min(videos.length-1,current+1);select()};
$('anchor').onclick=()=>{if(videos[current].anchor_sec!==null)$('player').currentTime=videos[current].anchor_sec};$('capture').onclick=()=>{$('timecode_sec').value=$('player').currentTime.toFixed(2)};
$('player').ontimeupdate=()=>{$('clock').textContent=`Текущее время: ${$('player').currentTime.toFixed(2)} с`};$('save').onclick=()=>{save();$('notice').textContent='Запись в памяти сохранена; скачайте JSON до закрытия страницы.'};
$('export').onclick=()=>{save();const blob=new Blob([JSON.stringify(Object.values(notes),null,2)],{type:'application/json'});const a=document.createElement('a');a.href=URL.createObjectURL(blob);a.download='assar-local-human-review.json';a.click();setTimeout(()=>URL.revokeObjectURL(a.href),1000)};select();
</script></html>'''.replace("PAYLOAD", payload)
    with review_path.open("x", encoding="utf-8") as f:
        f.write(page)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--archive", type=Path, required=True)
    parser.add_argument("--raw-dir", type=Path, default=ROOT / "data/raw")
    parser.add_argument("--out-dir", type=Path, default=ROOT / "analysis/local")
    parser.add_argument("--ffprobe", default=shutil.which("ffprobe") or "C:/ffmpeg/bin/ffprobe.exe")
    parser.add_argument("--ffmpeg", default=shutil.which("ffmpeg") or "C:/ffmpeg/bin/ffmpeg.exe")
    parser.add_argument("--workers", type=int, default=2)
    parser.add_argument("--decode", action="store_true", help="Fully decode each unique video to a null sink; no frames saved")
    args = parser.parse_args()
    out = args.out_dir.resolve()
    out.mkdir(parents=True, exist_ok=True)
    inventory = extract_safely(args.archive, args.raw_dir)
    write_json(out / "archive_inventory.json", inventory)
    xlsx = list(args.raw_dir.rglob("*.xlsx"))
    if len(xlsx) != 1:
        raise ValueError(f"Expected exactly one workbook, found {len(xlsx)}")
    workbook = read_workbook(xlsx[0])
    write_json(out / "labels_cells.json", workbook)
    labels, issues = [], []
    for sheet in workbook["sheets"]:
        if not sheet["rows"]:
            continue
        headers = sheet["rows"][0]["values"]
        expected = {"A": "наименование файла", "B": "таймстемп", "C": "нарушение / без нарушения"}
        if headers != expected:
            raise ValueError(f"Unexpected headers: {headers!r}")
        for row in sheet["rows"][1:]:
            values = row["values"]
            if not values:
                continue
            original_id = values.get("A", "")
            raw_label = values.get("C")
            verdict = {"нарушение": "violation", "без нарушения": "no_violation"}.get(raw_label)
            raw_timestamp = values.get("B")
            try:
                timestamp = parse_timestamp(raw_timestamp)
            except ValueError as exc:
                timestamp = None
                issues.append({"kind": "timestamp_parse_error", "id": original_id, "detail": str(exc)})
            record = {"sheet": sheet["name"], "row": row["row"], "original_id": original_id,
                      "canonical_id": canonical_id(original_id), "label_raw": raw_label,
                      "verdict": verdict, "timestamp_raw": raw_timestamp, "timestamp_sec": timestamp}
            labels.append(record)
            if verdict is None or (verdict == "violation" and timestamp is None) or (verdict == "no_violation" and timestamp is not None):
                issues.append({"kind": "unexpected_label_or_timestamp", "record": record})
    paths = sorted(args.raw_dir.rglob("*.mov"), key=lambda p: int(canonical_id(p.stem).split("-")[-1]))
    with concurrent.futures.ThreadPoolExecutor(max_workers=max(1, args.workers)) as pool:
        videos = list(pool.map(lambda p: inspect_video(p, args.ffprobe), paths))
    labels_by_id = collections.defaultdict(list)
    videos_by_id = collections.defaultdict(list)
    groups = collections.defaultdict(list)
    for label in labels:
        labels_by_id[label["canonical_id"]].append(label)
    for video in videos:
        videos_by_id[video["canonical_id"]].append(video)
        groups[video["sha256"]].append(video)
        if video["probe_returncode"]:
            issues.append({"kind": "probe_error", "id": video["canonical_id"]})
        matches = labels_by_id.get(video["canonical_id"], [])
        video["label_count"] = len(matches)
        if len(matches) == 1:
            video.update({k: matches[0][k] for k in ["verdict", "timestamp_sec", "timestamp_raw", "label_raw"]})
        for label in matches:
            timestamp = label["timestamp_sec"]
            if timestamp is not None and not 0 <= timestamp < video.get("duration_sec", 0):
                issues.append({"kind": "timestamp_out_of_bounds", "id": video["canonical_id"], "timestamp_sec": timestamp,
                               "duration_sec": video.get("duration_sec")})
    duplicate_groups = []
    for sha, group in groups.items():
        if len(group) > 1:
            verdicts = {v.get("verdict") for v in group}
            stamps = {v.get("timestamp_sec") for v in group}
            entry = {"sha256": sha, "ids": [v["canonical_id"] for v in group], "verdicts": [v.get("verdict") for v in group],
                     "timestamps_sec": [v.get("timestamp_sec") for v in group], "binary_label_conflict": len(verdicts) > 1,
                     "timestamp_disagreement": len(stamps) > 1}
            duplicate_groups.append(entry)
            if len(verdicts) > 1 or len(stamps) > 1:
                issues.append({"kind": "duplicate_annotation_disagreement", **entry})
    unmatched_labels = sorted(set(labels_by_id) - set(videos_by_id))
    unlabeled_videos = sorted(set(videos_by_id) - set(labels_by_id))
    duplicate_ids = {"labels": {k: len(v) for k, v in labels_by_id.items() if len(v) > 1},
                     "videos": {k: len(v) for k, v in videos_by_id.items() if len(v) > 1}}
    unique = [group[0] for group in groups.values()]
    durations = [v["duration_sec"] for v in videos if "duration_sec" in v]
    summary = {"archive_bytes": args.archive.stat().st_size, "archive_sha256": digest(args.archive),
               "video_count": len(videos), "unique_video_count_sha256": len(groups), "label_rows": len(labels),
               "video_bytes_total": sum(v["bytes"] for v in videos), "unique_video_bytes": sum(v["bytes"] for v in unique),
               "labels_by_verdict": dict(collections.Counter(r["verdict"] for r in labels)),
               "unique_videos_by_verdict": dict(collections.Counter(v.get("verdict") for v in unique)),
               "runtime_all_sec": round(sum(durations), 6), "runtime_unique_sec": round(sum(v.get("duration_sec", 0) for v in unique), 6),
               "duration_min_sec": min(durations) if durations else None, "duration_max_sec": max(durations) if durations else None,
               "width_height_counts": dict(collections.Counter(f"{v.get('width')}x{v.get('height')}" for v in videos)),
               "codec_counts": dict(collections.Counter(v.get("codec") for v in videos)),
               "pixel_format_counts": dict(collections.Counter(v.get("pix_fmt") for v in videos)),
               "fps_range": [min(v["avg_fps"] for v in videos if v.get("avg_fps")), max(v["avg_fps"] for v in videos if v.get("avg_fps"))],
               "rotation_counts": dict(collections.Counter(v.get("rotation_degrees") for v in videos)),
               "videos_with_audio": sum(bool(v.get("audio_streams")) for v in videos),
               "duplicate_groups": duplicate_groups, "unmatched_label_ids": unmatched_labels,
               "unlabeled_video_ids": unlabeled_videos, "duplicate_canonical_ids": duplicate_ids,
               "missing_numeric_ids_between_1_and_max": sorted(set(range(1, max(int(v["canonical_id"].split("-")[-1]) for v in videos)+1)) - {int(v["canonical_id"].split("-")[-1]) for v in videos}),
               "issues": issues, "semantic_visual_analysis_performed": False,
               "source_note": "Labels contain binary verdict and a single anchor only. Event types/intervals are not supplied. No video content was viewed by or sent to a cloud AI tool."}
    write_json(out / "audit_summary.json", summary)
    write_json(out / "video_manifest.json", videos)
    write_json(out / "labels_normalized.json", labels)
    write_csv(out / "labels_normalized.csv", labels, ["canonical_id", "original_id", "label_raw", "verdict", "timestamp_raw", "timestamp_sec", "sheet", "row"])
    write_csv(out / "video_manifest.csv", videos, ["canonical_id", "filename", "relative_path", "sha256", "bytes", "duration_sec", "codec", "width", "height", "pix_fmt", "avg_fps", "nominal_fps", "avg_frame_rate", "r_frame_rate", "time_base", "declared_frame_count", "rotation_degrees", "audio_streams", "verdict", "timestamp_sec", "probe_returncode"])
    skeleton = [{"canonical_id": v["canonical_id"], "sha256": v["sha256"], "source_verdict": v.get("verdict"), "source_timestamp_sec": v.get("timestamp_sec"),
                 "duplicate_group_ids": ";".join(x["canonical_id"] for x in groups[v["sha256"]])} for v in videos]
    skeleton_path = out / "human_annotation_template.csv"
    if not skeleton_path.exists():
        write_csv(skeleton_path, skeleton, ["canonical_id", "sha256", "duplicate_group_ids", "source_verdict", "source_timestamp_sec", "reviewer", "review_status", "event_index", "event_type", "start_sec", "end_sec", "timecode_sec", "observable_action", "rule_id", "source_url", "source_clause", "uncertainty", "notes"])
    review_page(out, videos, labels)
    print(json.dumps(summary, ensure_ascii=False, indent=2), flush=True)
    if args.decode:
        print(f"Decoding {len(unique)} unique videos to a null sink...", flush=True)
        with concurrent.futures.ThreadPoolExecutor(max_workers=max(1, args.workers)) as pool:
            results = list(pool.map(lambda v: decode_video(v, args.ffmpeg), unique))
        write_json(out / "decode_checks.json", results)
        summary["full_decode"] = {"unique_videos_checked": len(results), "statuses": dict(collections.Counter(r["status"] for r in results)),
                                  "decoded_unique_frames": sum(r.get("decoded_frames") or 0 for r in results),
                                  "errors": [r for r in results if r["status"] != "ok"]}
        write_json(out / "audit_summary.json", summary)
        print(json.dumps(summary["full_decode"], ensure_ascii=False, indent=2), flush=True)


if __name__ == "__main__":
    main()
