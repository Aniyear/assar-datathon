"""Build a draft handoff from an explicit allowlist. No GPU, server, keys or network.

Never packages a workspace recursively. Runtime data and model outputs are not inputs.
The result is always a draft, even when the model's readiness flags change later.
"""
from __future__ import annotations

import argparse
import hashlib
import importlib.metadata
import json
from pathlib import Path
import posixpath
import re
import subprocess
import sys
import time
import zipfile
from urllib.parse import quote

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

FILES = (
    "docs/jury/README.md", "docs/jury/GUIDE.md", "docs/jury/TECHNICAL.md",
    "docs/jury/OPERATOR.md", "docs/jury/MESSAGE_TEMPLATE.txt",
    "docs/ACCEPTED_RULES_V6.md", "docs/FINAL_ACCEPTANCE_3060.md",
    "docs/SERVICE_OPERATIONS.md", "docs/NGROK_TEST.md",
    "configs/model.yaml", "app/rules/catalog.json", "requirements.lock",
    "tests/fixtures/contracts/README.md",
    "tests/fixtures/contracts/accepted.json", "tests/fixtures/contracts/processing.json",
    "tests/fixtures/contracts/done_violation.json", "tests/fixtures/contracts/done_no_violation.json",
    "tests/fixtures/contracts/failed.json",
)


def encoded(value) -> bytes:
    return (json.dumps(value, ensure_ascii=False, sort_keys=True, indent=2) + "\n").encode("utf-8")


def public_schema() -> dict:
    # Importing contracts does not initialize the API, worker or model.
    from pydantic.json_schema import models_json_schema
    from app.contracts import AnalyzeAccepted, ProcessingJob, DoneJob, FailedJob

    models = (AnalyzeAccepted, ProcessingJob, DoneJob, FailedJob)
    _, schema = models_json_schema([(m, "validation") for m in models],
                                  ref_template="#/components/schemas/{model}")
    def response(description, body=None):
        value = {"description": description}
        if body is not None:
            value["content"] = {"application/json": {"schema": body}}
        return value
    def ref(name):
        return {"$ref": "#/components/schemas/" + name}
    errors = {"401": response("Missing or invalid X-API-Key")}
    post = {**errors, "202": response("Accepted; poll this job_id", ref("AnalyzeAccepted"))}
    post.update({str(code): response(text) for code, text in (
        (408, "Upload timeout"), (413, "Upload size limit"), (415, "Expected multipart MOV"),
        (422, "Malformed, empty or undecodable upload"), (503, "Busy or not ready"))})
    return {
        "openapi": "3.1.0",
        "info": {"title": "assar public API", "version": "0.1",
                 "description": "Draft documentation, not a deployment. Supply the approved HTTPS origin. "
                                "Examples are synthetic. failed is not no_violation. "
                                "JPEG decoding and verdict/event consistency are checked at runtime."},
        "security": [{"ApiKey": []}],
        "paths": {
            "/api/v1/analyze": {"post": {"operationId": "analyzeVideo",
                "requestBody": {"required": True, "content": {"multipart/form-data": {"schema": {
                    "type": "object", "required": ["file"], "properties": {
                        "file": {"type": "string", "format": "binary",
                                 "description": "One nonempty MOV, up to 100 MiB by default"}}}}}},
                "responses": post}},
            "/api/v1/jobs/{job_id}": {"get": {"operationId": "getJob",
                "parameters": [{"name": "job_id", "in": "path", "required": True,
                                "schema": {"type": "string"}}],
                "responses": {**errors, "404": response("Unknown job_id"),
                    "200": response("Job state", {"oneOf": [ref(m.__name__) for m in models[1:]],
                        "discriminator": {"propertyName": "status", "mapping": {
                            "processing": "#/components/schemas/ProcessingJob",
                            "done": "#/components/schemas/DoneJob", "failed": "#/components/schemas/FailedJob"}}})}}}},
        "components": {"securitySchemes": {"ApiKey": {"type": "apiKey", "in": "header", "name": "X-API-Key"}},
                       "schemas": schema["$defs"]},
    }


def dependency_inventory(lock: bytes) -> tuple[dict, dict[str, bytes]]:
    rows, licenses = [], {}
    for line in lock.decode("utf-8").splitlines():
        if not line.strip() or line.startswith("#"):
            continue
        match = re.fullmatch(r"([A-Za-z0-9_.-]+)==([A-Za-z0-9_.+-]+)", line.strip())
        if not match:
            raise ValueError("Unsupported lock entry")
        name, version = match.groups()
        dist = importlib.metadata.distribution(name)
        if dist.version != version:
            raise ValueError("Installed dependency does not match lock: " + name)
        paths = []
        for item in dist.files or ():
            if not item.name.lower().startswith(("license", "copying", "notice")):
                continue
            # Only the explicitly named license artifacts of the locked distribution.
            source = Path(dist.locate_file(item))
            if not source.is_file() or source.stat().st_size > 1024 * 1024:
                raise ValueError("License artifact unavailable or too large: " + name)
            target = f"licenses/python/{name}/{len(paths):02d}-{item.name}"
            licenses[target] = source.read_bytes()
            paths.append(target)
        rows.append({"name": name, "version": version,
                     "declared_license": dist.metadata.get("License-Expression") or dist.metadata.get("License") or None,
                     "license_files": paths})
    return {"basis": "Installed distribution metadata; not a legal opinion or a binary redistribution bundle",
            "packages": rows, "missing_license_files": [x["name"] for x in rows if not x["license_files"]]}, licenses


def source_state(root: Path) -> dict:
    def git(*args):
        return subprocess.check_output(["git", *args], cwd=root, text=True).strip()
    return {"source_commit": git("rev-parse", "HEAD"), "source_branch": git("branch", "--show-current"),
            "source_tree_dirty": bool(git("status", "--porcelain=v1"))}


def portable_links(name: str, body: bytes, included: set[str], commit: str) -> bytes:
    """Keep bundled links local; point supporting repository docs at the exact commit."""
    if not name.endswith(".md"):
        return body
    def replace(match):
        target = match.group(1)
        if target.startswith(("#", "/")) or re.match(r"[A-Za-z][A-Za-z0-9+.-]*:", target):
            return match.group(0)
        path, sep, anchor = target.partition("#")
        resolved = posixpath.normpath(posixpath.join(posixpath.dirname(name), path))
        if resolved in included:
            return match.group(0)
        if resolved == ".." or resolved.startswith("../"):
            raise ValueError("Documentation link escapes repository")
        url = f"https://github.com/Aniyear/assar-datathon/blob/{commit}/{quote(resolved)}"
        return "](" + url + (sep + anchor if sep else "") + ")"
    return re.sub(r"\]\(([^\s)]+)\)", replace, body.decode("utf-8")).encode("utf-8")


def build(output: Path, root: Path = ROOT) -> dict:
    root = root.resolve()
    content = {}
    for name in FILES:
        path = root / name
        # Prevent an allowlisted file or parent symlink from redirecting into private data.
        if path.resolve() != path or not path.is_file() or path.stat().st_size > 2 * 1024 * 1024:
            raise ValueError("Missing, redirected or oversized package input: " + name)
        content[name] = path.read_bytes()
    cfg = json.loads(content["configs/model.yaml"])
    catalog = json.loads(content["app/rules/catalog.json"])
    inventory, licenses = dependency_inventory(content["requirements.lock"])
    content.update(licenses)
    content["dependency-inventory.json"] = encoded(inventory)
    content["openapi.json"] = encoded(public_schema())
    content["README.md"] = b"# assar jury DRAFT\n\nNot published or accepted. Start at [docs/jury/README.md](docs/jury/README.md).\n"
    state = source_state(root)
    content = {name: portable_links(name, body, set(content), state["source_commit"])
               for name, body in content.items()}
    manifest = {
        "package_status": "draft_not_for_submission", **state,
        "public_origin": None, "availability_window": None, "release_approved": False,
        "model_status": cfg["status"], "model_id": cfg["model_id"],
        "generation": cfg["generation"], "validation_as_recorded": cfg["validation"],
        "active_rule_ids": [r["rule_id"] for r in catalog["rules"] if r["status"] == "verified"],
        "pending": ["accepted ML correction and relevant final acceptance", "production startup/UI/restart check",
                    "separately authorized publication and external API check", "captain fills and sends handoff; key separately"],
        "file_sha256": {name: hashlib.sha256(body).hexdigest() for name, body in sorted(content.items())},
    }
    content["manifest.json"] = encoded(manifest)
    output.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(output, "x", compression=zipfile.ZIP_DEFLATED) as archive:
        for name, body in sorted(content.items()):
            archive.writestr(name, body)
    receipt = {"package_status": manifest["package_status"], **state,
               "zip_sha256": hashlib.sha256(output.read_bytes()).hexdigest(), "files": len(content),
               "dependencies": len(inventory["packages"]),
               "missing_license_files": inventory["missing_license_files"],
               "gpu_started": False, "service_started": False, "published": False}
    output.with_suffix(".receipt.json").write_bytes(encoded(receipt))
    return receipt


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    output = args.output or ROOT / "outputs/jury" / ("assar-jury-draft-" + time.strftime("%Y%m%dT%H%M%SZ", time.gmtime()) + ".zip")
    receipt = build(output)
    print(json.dumps({**receipt, "archive": str(output.resolve())}, ensure_ascii=True))


if __name__ == "__main__":
    main()
