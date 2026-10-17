"""make_video_list: SHA256, кириллица в именах, дубликаты групп, запрет test. Синтетические файлы."""
import hashlib
import json

import pytest

from app.inference.experiments import make_video_list as mvl


def _setup(tmp_path, split="dev"):
    vdir = tmp_path / "videos"
    vdir.mkdir(parents=True)
    data = {"X-2.mov": b"a", "Х-4.mov": b"b", "Х-47.mov": b"b"}  # X-2 латиница, остальные кириллица
    for n, d in data.items():
        (vdir / n).write_bytes(d)
    h = {n: hashlib.sha256(d).hexdigest() for n, d in data.items()}
    rows = ["canonical_id,filename,sha256,group_id,split",
            f"X-2,X-2.mov,{h['X-2.mov']},{h['X-2.mov']},{split}",
            f"X-4,Х-4.mov,{h['Х-4.mov']},{h['Х-4.mov']},dev",
            f"X-47,Х-47.mov,{h['Х-47.mov']},{h['Х-4.mov']},dev"]
    (tmp_path / "split.csv").write_text("\n".join(rows), encoding="utf-8")
    (tmp_path / "labels.csv").write_text("video_id,label\nX-2,0\nX-4,1\nX-47,1\nX-9,1\n", encoding="utf-8")
    return vdir


def test_build_dedupes_groups_and_reads_selected_labels(tmp_path):
    vdir = _setup(tmp_path)
    items = mvl.build(tmp_path / "split.csv", vdir, tmp_path / "labels.csv", {"X-4"}, {"dev"})
    assert [i["id"] for i in items] == ["X-2", "X-4"]
    assert items[1]["role"] == "diagnostic" and items[1]["source_label"] == "violation"
    assert items[0]["source_label"] == "no_violation"


def test_build_rejects_test_split_and_hash_mismatch(tmp_path):
    vdir = _setup(tmp_path, split="test")
    with pytest.raises(SystemExit):
        mvl.build(tmp_path / "split.csv", vdir, None, set(), {"dev", "train"})
    vdir = _setup(tmp_path / "b")
    (vdir / "X-2.mov").write_bytes(b"changed")
    with pytest.raises(SystemExit):
        mvl.build(tmp_path / "b" / "split.csv", vdir, None, set(), {"dev"})
