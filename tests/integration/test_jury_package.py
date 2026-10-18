"""Packaging never traverses private data and never promotes a candidate to ready."""
import hashlib
import json
from pathlib import Path
import zipfile

import pytest

from app.contracts import AnalyzeAccepted, JOB_RESPONSE_ADAPTER
from scripts import prepare_jury_package as package


@pytest.fixture
def inputs(tmp_path, monkeypatch):
    root = tmp_path / "repo"
    for name in package.FILES:
        path = root / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes((package.ROOT / name).read_bytes())
    for name in ("outputs/private-result.json", "data/video.mov", ".env", "docs/jury/not-allowlisted-secret.txt"):
        path = root / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("SYNTHETIC-PRIVATE-SENTINEL", encoding="utf-8")
    monkeypatch.setattr(package, "source_state", lambda root: {"source_commit": "a" * 40,
                        "source_branch": "codex/test", "source_tree_dirty": True})
    monkeypatch.setattr(package, "dependency_inventory", lambda lock: ({"packages": [], "missing_license_files": []}, {}))
    return root


def test_draft_allowlist_hashes_and_no_private_files(inputs, tmp_path):
    target = tmp_path / "draft.zip"
    receipt = package.build(target, inputs)
    assert not receipt["published"] and not receipt["service_started"] and not receipt["gpu_started"]
    assert receipt["zip_sha256"] == hashlib.sha256(target.read_bytes()).hexdigest()
    with zipfile.ZipFile(target) as archive:
        manifest = json.loads(archive.read("manifest.json"))
        assert manifest["package_status"] == "draft_not_for_submission"
        assert manifest["release_approved"] is False and manifest["public_origin"] is None
        assert manifest["source_tree_dirty"] is True
        assert set(archive.namelist()) == set(package.FILES) | {"README.md", "openapi.json", "manifest.json", "dependency-inventory.json"}
        for name, digest in manifest["file_sha256"].items():
            data = archive.read(name)
            assert hashlib.sha256(data).hexdigest() == digest
            assert b"SYNTHETIC-PRIVATE-SENTINEL" not in data


def test_ready_model_still_cannot_publish_or_approve_package(inputs, tmp_path):
    path = inputs / "configs/model.yaml"
    cfg = json.loads(path.read_text(encoding="utf-8"))
    cfg["status"] = "ready"
    path.write_text(json.dumps(cfg), encoding="utf-8")
    package.build(tmp_path / "ready-model.zip", inputs)
    with zipfile.ZipFile(tmp_path / "ready-model.zip") as archive:
        manifest = json.loads(archive.read("manifest.json"))
    assert manifest["release_approved"] is False and manifest["package_status"] == "draft_not_for_submission"


def test_missing_allowlisted_file_is_an_error(inputs, tmp_path):
    (inputs / package.FILES[0]).unlink()
    with pytest.raises(ValueError, match="Missing, redirected"):
        package.build(tmp_path / "missing.zip", inputs)
    assert not (tmp_path / "missing.zip").exists()


def test_archive_is_not_silently_overwritten(inputs, tmp_path):
    path = tmp_path / "existing.zip"
    path.write_bytes(b"existing receipt")
    with pytest.raises(FileExistsError):
        package.build(path, inputs)
    assert path.read_bytes() == b"existing receipt"


def test_openapi_matches_routes_and_resolves_every_schema_reference():
    from app.api.main import create_app
    document = package.public_schema()
    public = {r.path for r in create_app().routes if getattr(r, "path", "").startswith("/api/v1/")}
    assert set(document["paths"]) == public
    assert document["components"]["securitySchemes"]["ApiKey"]["name"] == "X-API-Key"
    assert document["security"] == [{"ApiKey": []}]
    def check(node):
        if isinstance(node, dict):
            if "$ref" in node:
                parts = node["$ref"].removeprefix("#/").split("/")
                target = document
                for part in parts:
                    target = target[part]
            for value in node.values():
                check(value)
        elif isinstance(node, list):
            for item in node:
                check(item)
    check(document)


def test_bundled_examples_are_valid_synthetic_contracts():
    folder = package.ROOT / "tests/fixtures/contracts"
    AnalyzeAccepted.model_validate_json((folder / "accepted.json").read_bytes())
    for name in ("processing", "done_no_violation", "done_violation", "failed"):
        result = JOB_RESPONSE_ADAPTER.validate_json((folder / f"{name}.json").read_bytes())
        assert "synthetic" in result.job_id.lower()


def test_links_keep_bundle_targets_and_pin_external_repo_docs():
    data = b"[local](GUIDE.md) [support](../MVP_LAUNCH.md#start) [web](https://example.com)"
    result = package.portable_links("docs/jury/README.md", data, {"docs/jury/GUIDE.md"}, "a" * 40)
    assert b"[local](GUIDE.md)" in result
    assert b"/blob/" + b"a" * 40 + b"/docs/MVP_LAUNCH.md#start" in result
    assert b"[web](https://example.com)" in result


def test_document_link_cannot_escape_repo():
    with pytest.raises(ValueError, match="escapes repository"):
        package.portable_links("README.md", b"[bad](../private.md)", set(), "a" * 40)
