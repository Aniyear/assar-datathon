"""Synthetic nvidia-smi inventory: desktop graphics are not an ML worker."""
import xml.etree.ElementTree as ET

import pytest

from scripts.gpu_processes import process_inventory


def xml(*rows):
    root = ET.Element("nvidia_smi_log")
    section = ET.SubElement(ET.SubElement(root, "gpu"), "processes")
    for pid, kind, path in rows:
        info = ET.SubElement(section, "process_info")
        for key, value in (("pid", pid), ("type", kind), ("process_name", path)):
            ET.SubElement(info, key).text = str(value)
    return ET.tostring(root, encoding="unicode")


def test_only_known_gui_paths_are_exempt(monkeypatch):
    monkeypatch.setenv("SystemRoot", r"C:\Windows")
    result = process_inventory(xml((1, "C+G", r"C:\Windows\explorer.exe"),
                                   (2, "C+G", r"C:\unknown\explorer.exe"),
                                   (3, "C+G", r"C:\models\llama-server.exe"),
                                   (4, "C+G", r"C:\Python\python.exe"),
                                   (5, "C+G", "unknown"), (6, "G", "desktop")))
    assert result == {"compute_pids": [2, 3, 4, 5], "background_graphics_pids": [1, 6]}


def test_pure_compute_is_never_exempt_even_at_gui_path(monkeypatch):
    monkeypatch.setenv("SystemRoot", r"C:\Windows")
    assert process_inventory(xml((1, "C", r"C:\Windows\explorer.exe")))["compute_pids"] == [1]


@pytest.mark.parametrize("data", ["<broken", "<nvidia_smi_log/>", xml(("bad", "C", "python"))])
def test_missing_or_malformed_inventory_is_not_idle(data):
    with pytest.raises((ValueError, ET.ParseError)):
        process_inventory(data)


def test_known_empty_inventory_is_idle():
    assert process_inventory(xml()) == {"compute_pids": [], "background_graphics_pids": []}


@pytest.mark.parametrize("data,status,ready", [
    (xml((1, "C+G", r"C:\Windows\explorer.exe")), "ready", True),
    (xml((1, "C+G", r"C:\models\llama-server.exe")), "ready", False),
    ("<broken", "ready", False),
    (xml(), "acceptance_failed", False),
])
def test_preflight_still_requires_idle_model_and_readiness(monkeypatch, data, status, ready):
    import argparse
    import json
    from types import SimpleNamespace
    from scripts import service
    from app.inference import service as ml_service
    monkeypatch.setenv("SystemRoot", r"C:\Windows")
    cfg = json.loads((service.ROOT / "configs/model.yaml").read_text(encoding="utf-8"))
    cfg["status"] = status
    monkeypatch.setattr(service, "environment_problems", lambda: [])
    monkeypatch.setattr(service, "resolved_config", lambda *a: cfg)
    monkeypatch.setattr(ml_service, "check_runtime", lambda *a: None)
    monkeypatch.setattr(service.shutil, "which", lambda name: name)
    monkeypatch.setattr(service.subprocess, "run", lambda args, **kw: SimpleNamespace(
        stdout=data if "-x" in args else "Synthetic GPU, 6144, 1000"))
    report = service.preflight(argparse.Namespace(mode="production", model_config=None, runtime_root=None))
    assert report["ready"] is ready
    assert report["gpu_started"] is False
