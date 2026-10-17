"""Local-only, deterministic group split. Prints aggregates, never dataset rows.

Only the standard library is required. No decoding, images, network, or model calls.
"""
from __future__ import annotations

import argparse
import csv
import hashlib
import io
import json
import math
from pathlib import Path
import re
import shutil
import sys

ALGORITHM = "assar-group-stratified-dp-v1"
SCHEMA = "1"
SPLITS = ("train", "dev", "test")
CLASSES = ("violation", "no_violation")
SHA_RE = re.compile(r"[0-9a-f]{64}\Z")
ID_RE = re.compile(r"X-[1-9][0-9]*\Z")
ML_FIELDS = ["canonical_id", "relative_path", "sha256", "group_id", "split",
             "verdict", "source_timestamp_sec", "duration_sec", "bytes", "rotation_degrees"]


class SplitError(ValueError):
    """A safe aggregate diagnostic, without private row values."""


def require(condition: bool, message: str) -> None:
    if not condition:
        raise SplitError(message)


def encoded(value) -> bytes:
    return (json.dumps(value, ensure_ascii=False, sort_keys=True, indent=2, allow_nan=False) + "\n").encode("utf-8")


def digest(path: Path) -> str:
    value = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            value.update(chunk)
    return value.hexdigest()


def json_digest(value) -> str:
    return hashlib.sha256(encoded(value)).hexdigest()


def read_json(path: Path):
    return json.loads(path.read_text(encoding="utf-8-sig"))


def read_csv(path: Path, required: set[str]) -> list[dict]:
    with path.open(encoding="utf-8-sig", newline="") as stream:
        reader = csv.DictReader(stream)
        require(reader.fieldnames is not None and required <= set(reader.fieldnames), "Audit CSV columns are missing.")
        rows = list(reader)
    require(bool(rows), "Audit CSV is empty.")
    return rows


def number(value, *, positive: bool = False) -> float:
    result = float(value)
    require(math.isfinite(result) and (not positive or result > 0), "Invalid numeric audit metadata.")
    return result


def canonical(value: str) -> str:
    value = value.strip()
    if value[:1] in ("Х", "х", "x"):
        value = "X" + value[1:]
    require(bool(ID_RE.fullmatch(value)), "Invalid normalized video identifier.")
    return value


def inside(root: Path, relative: str) -> Path:
    rel = Path(relative)
    require(not rel.is_absolute() and ".." not in rel.parts, "Source path must be relative to the data root.")
    candidate = (root / rel).resolve()
    require(candidate.is_relative_to(root.resolve()), "Source path escapes the data root.")
    require(candidate.is_file(), "A source file is missing.")
    return candidate


def load_inputs(audit: Path, data_root: Path) -> tuple[list[dict], dict]:
    """Ignore free-text labels/notes; validate labels locally without printing them."""
    paths = {name: audit / name for name in ("video_manifest.csv", "labels_normalized.csv", "decode_checks.json")}
    fingerprints = {name: digest(path) for name, path in paths.items()}
    videos = read_csv(paths["video_manifest.csv"], {"canonical_id", "relative_path", "sha256", "bytes", "duration_sec", "rotation_degrees", "verdict", "timestamp_sec", "probe_returncode"})
    labels = read_csv(paths["labels_normalized.csv"], {"canonical_id", "verdict", "timestamp_sec"})
    label_map = {}
    for label in labels:
        key = canonical(label["canonical_id"])
        require(key not in label_map, "Duplicate label identifiers.")
        require(label["verdict"] in CLASSES, "Unknown binary source label.")
        label_map[key] = label
    checks = read_json(paths["decode_checks.json"])
    require(isinstance(checks, list), "Invalid decode report format.")
    decode = {}
    for check in checks:
        sha = check.get("sha256", "")
        require(bool(SHA_RE.fullmatch(sha)) and sha not in decode, "Invalid or duplicate decode identity.")
        require(check.get("status") == "ok" and check.get("returncode") == 0 and check.get("progress") == "end" and isinstance(check.get("decoded_frames"), int) and check["decoded_frames"] > 0 and not check.get("stderr"), "Decode report contains incomplete or failed processing.")
        decode[sha] = True
    result, seen_ids, seen_paths, sha_classes = [], set(), set(), {}
    for raw in videos:
        key, sha = canonical(raw["canonical_id"]), raw["sha256"]
        require(key not in seen_ids and key in label_map, "Video/label matching is not one-to-one.")
        require(bool(SHA_RE.fullmatch(sha)), "Invalid content hash.")
        path = inside(data_root, raw["relative_path"])
        require(path not in seen_paths, "Duplicate source paths.")
        seen_paths.add(path)
        seen_ids.add(key)
        require(raw["probe_returncode"] == "0", "Probe report contains failed processing.")
        size, duration = int(raw["bytes"]), number(raw["duration_sec"], positive=True)
        require(size > 0 and path.stat().st_size == size and digest(path) == sha, "Source bytes differ from the technical audit.")
        label = label_map[key]
        require(raw["verdict"] == label["verdict"], "Manifest and source binary labels disagree.")
        stamp = None if not label["timestamp_sec"] else number(label["timestamp_sec"])
        raw_stamp = None if not raw["timestamp_sec"] else number(raw["timestamp_sec"])
        require(stamp == raw_stamp, "Manifest and source timestamps disagree.")
        require((label["verdict"] == "no_violation" and stamp is None) or (label["verdict"] == "violation" and stamp is not None and 0 <= stamp < duration), "Invalid binary label/timestamp combination.")
        require(sha not in sha_classes or sha_classes[sha] == label["verdict"], "Exact duplicates have conflicting binary labels.")
        sha_classes[sha] = label["verdict"]
        result.append({"canonical_id": key, "relative_path": raw["relative_path"].replace("\\", "/"), "sha256": sha,
                       "bytes": size, "duration_sec": duration, "rotation_degrees": number(raw["rotation_degrees"]),
                       "verdict": label["verdict"], "source_timestamp_sec": stamp, "decode_status": "ok"})
    require(seen_ids == set(label_map), "Audit has unmatched source labels.")
    require(set(sha_classes) == set(decode), "Decode report does not cover exactly the unique source videos.")
    return sorted(result, key=lambda row: row["canonical_id"]), fingerprints


def group_config(value=None) -> dict:
    if value is None:
        return {"schema_version": SCHEMA, "review_status": "pending", "groups": []}
    require(isinstance(value, dict) and set(value) == {"schema_version", "review_status", "groups"}, "Invalid manual group document fields.")
    require(value["schema_version"] == SCHEMA and value["review_status"] in ("pending", "complete"), "Invalid manual group version/review status.")
    require(isinstance(value["groups"], list), "Manual groups must be a list.")
    groups = []
    for group in value["groups"]:
        require(isinstance(group, list) and len(group) >= 2 and all(isinstance(key, str) for key in group), "Manual groups require at least two identifiers.")
        normalized = sorted(canonical(key) for key in group)
        require(len(set(normalized)) == len(normalized), "Repeated member inside a manual group.")
        groups.append(normalized)
    return {"schema_version": SCHEMA, "review_status": value["review_status"], "groups": sorted(groups)}


def make_groups(records: list[dict], config: dict) -> list[dict]:
    by_id = {row["canonical_id"]: row for row in records}
    parent = {row["sha256"]: row["sha256"] for row in records}

    def find(key):
        while parent[key] != key:
            parent[key] = parent[parent[key]]
            key = parent[key]
        return key

    for members in config["groups"]:
        require(set(members) <= set(by_id), "Manual grouping references an unknown source identifier.")
        roots = sorted({find(by_id[key]["sha256"]) for key in members})
        for root in roots[1:]:
            parent[root] = roots[0]
    grouped = {}
    for row in records:
        grouped.setdefault(find(row["sha256"]), []).append(row)
    result = []
    for rows in grouped.values():
        hashes = sorted({row["sha256"] for row in rows})
        unique = {row["sha256"]: row["verdict"] for row in rows}
        result.append({"group_id": hashes[0] if len(hashes) == 1 else "manual-" + json_digest(hashes),
                       "sha256": hashes, "members": sorted(row["canonical_id"] for row in rows),
                       "unique_videos": len(hashes), "positive": sum(value == "violation" for value in unique.values())})
    return sorted(result, key=lambda group: group["group_id"])


def allocate(groups: list[dict], sizes: list[int], seed: int, no_test_ids: list[str]) -> tuple[dict, list[int]]:
    total, positives = sum(g["unique_videos"] for g in groups), sum(g["positive"] for g in groups)
    require(len(sizes) == 3 and all(isinstance(n, int) and n > 1 for n in sizes) and sum(sizes) == total, "Split sizes must cover every unique video and allow both classes.")
    require(0 < positives < total, "Both binary classes are required.")
    # Largest remainder stratification, with fixed train/dev/test tie order.
    quotas = [n * positives // total for n in sizes]
    remainder_order = sorted(range(3), key=lambda i: (-(sizes[i] * positives % total), i))
    for index in remainder_order[:positives - sum(quotas)]:
        quotas[index] += 1
    require(all(0 < p < n for p, n in zip(quotas, sizes)), "Requested sizes cannot retain both classes.")
    all_ids = {key for group in groups for key in group["members"]}
    require(set(no_test_ids) <= all_ids, "A holdout-exclusion identifier is absent from the audit.")
    ordered = sorted(groups, key=lambda g: hashlib.sha256(f"{ALGORITHM}|{seed}|{g['group_id']}".encode()).hexdigest())
    # Joint DP: whole groups only, exact sizes + class quotas, no random retries.
    states = {(0, 0, 0, 0): ()}
    seen_n = seen_p = 0
    for group in ordered:
        new = {}
        n, p = group["unique_videos"], group["positive"]
        seen_n += n
        seen_p += p
        for (dn, dp, tn, tp), assignment in states.items():
            for split in SPLITS:
                if split == "test" and set(group["members"]) & set(no_test_ids):
                    continue
                key = (dn + (n if split == "dev" else 0), dp + (p if split == "dev" else 0), tn + (n if split == "test" else 0), tp + (p if split == "test" else 0))
                counts = (seen_n - key[0] - key[2], key[0], key[2])
                pos = (seen_p - key[1] - key[3], key[1], key[3])
                if all(0 <= c <= goal and 0 <= cp <= qp and 0 <= c - cp <= goal - qp for c, cp, goal, qp in zip(counts, pos, sizes, quotas)):
                    new.setdefault(key, assignment + (split,))
        states = new
    final = states.get((sizes[1], quotas[1], sizes[2], quotas[2]))
    require(final is not None, "Whole-group constraints cannot satisfy the requested sizes/class quotas; do not split a group or retry seeds silently.")
    return {g["group_id"]: split for g, split in zip(ordered, final)}, quotas


def build_manifest(records, fingerprints, config, sizes, seed, no_test_ids) -> dict:
    groups = make_groups(records, config)
    assignment, quotas = allocate(groups, sizes, seed, no_test_ids)
    index = {key: group["group_id"] for group in groups for key in group["members"]}
    assigned = [{**row, "group_id": index[row["canonical_id"]], "split": assignment[index[row["canonical_id"]]]} for row in records]
    summary = {}
    for split in SPLITS:
        rows = [row for row in assigned if row["split"] == split]
        unique = {row["sha256"]: row["verdict"] for row in rows}
        summary[split] = {"files": len(rows), "groups": len({row["group_id"] for row in rows}), "unique_videos": len(unique),
                          "positive": sum(value == "violation" for value in unique.values()), "negative": sum(value == "no_violation" for value in unique.values())}
    return {"schema_version": SCHEMA, "algorithm": ALGORITHM, "seed": seed, "sizes": dict(zip(SPLITS, sizes)),
            "positive_quotas": dict(zip(SPLITS, quotas)), "no_test_ids": sorted(no_test_ids), "input_sha256": fingerprints,
            "group_config": config, "groups_sha256": json_digest(config), "near_duplicate_review": config["review_status"],
            "summary": summary, "groups": [{**g, "split": assignment[g["group_id"]]} for g in groups], "records": assigned}


def ml_csv(manifest) -> bytes:
    stream = io.StringIO(newline="")
    writer = csv.DictWriter(stream, fieldnames=ML_FIELDS, extrasaction="ignore", lineterminator="\n")
    writer.writeheader()
    writer.writerows(row for row in manifest["records"] if row["split"] != "test")
    return stream.getvalue().encode("utf-8")


def receipt_for(manifest, ml_content) -> dict:
    return {"schema_version": SCHEMA, "algorithm": ALGORITHM, "seed": manifest["seed"],
            "manifest_sha256": json_digest(manifest), "train_dev_manifest_sha256": hashlib.sha256(ml_content).hexdigest(),
            "input_sha256": manifest["input_sha256"], "summary": manifest["summary"],
            "near_duplicate_review": manifest["near_duplicate_review"],
            "source_bytes_verified": True, "decode_audit_verified": True,
            "holdout_exclusion_groups": sum(bool(set(g["members"]) & set(manifest["no_test_ids"])) for g in manifest["groups"]),
            "model_tuning_allowed": manifest["near_duplicate_review"] == "complete"}


def freeze(args) -> dict:
    require(not args.out.exists(), "Freeze destination already exists; frozen assignments are immutable. Use validate, not regeneration.")
    require(not args.receipt.exists(), "Public receipt already exists; it must not be silently replaced.")
    records, fingerprints = load_inputs(args.audit_dir, args.data_root)
    config = group_config(read_json(args.groups) if args.groups else None)
    manifest = build_manifest(records, fingerprints, config, args.sizes, args.seed, sorted(set(canonical(x) for x in args.exclude_test)))
    content = ml_csv(manifest)
    receipt = receipt_for(manifest, content)
    args.out.mkdir(parents=True, exist_ok=False)
    (args.out / "frozen.json").write_bytes(encoded(manifest))
    (args.out / "train_dev.csv").write_bytes(content)
    (args.out / "receipt.json").write_bytes(encoded(receipt))
    args.receipt.parent.mkdir(parents=True, exist_ok=True)
    with args.receipt.open("xb") as stream:
        stream.write(encoded(receipt))
    return receipt


def validate(args) -> tuple[dict, dict]:
    receipt = read_json(args.receipt)
    path = args.freeze / "frozen.json"
    require(digest(path) == receipt["manifest_sha256"], "Frozen manifest hash differs from the committed receipt.")
    manifest = read_json(path)
    require(manifest.get("schema_version") == SCHEMA and manifest.get("algorithm") == ALGORITHM, "Unsupported freeze version/algorithm.")
    records, fingerprints = load_inputs(args.audit_dir, args.data_root)
    require(fingerprints == manifest["input_sha256"], "Audit fingerprint changed; existing freeze cannot be reused.")
    config = group_config(manifest["group_config"])
    rebuilt = build_manifest(records, fingerprints, config, [manifest["sizes"][s] for s in SPLITS], manifest["seed"], manifest["no_test_ids"])
    require(rebuilt == manifest, "Frozen assignments or metadata fail deterministic reconstruction.")
    content = ml_csv(manifest)
    require((args.freeze / "train_dev.csv").read_bytes() == content, "Train/dev manifest differs from the frozen selection.")
    require(receipt_for(manifest, content) == receipt, "Public receipt does not match the complete frozen state.")
    require(read_json(args.freeze / "receipt.json") == receipt, "Private and committed receipts disagree.")
    return manifest, receipt


def review(args) -> dict:
    manifest, receipt = validate(args)
    target = args.freeze / "group_review.json"
    require(not target.exists(), "Group review already exists and cannot be overwritten.")
    config = group_config(read_json(args.groups))
    require(config["review_status"] == "complete", "Human similarity review must be complete before model tuning.")
    merged = group_config({**config, "groups": manifest["group_config"]["groups"] + config["groups"]})
    groups = make_groups(manifest["records"], merged)
    split_by_id = {row["canonical_id"]: row["split"] for row in manifest["records"]}
    require(all(len({split_by_id[key] for key in group["members"]}) == 1 for group in groups), "Human similarity groups cross frozen splits. Stop tuning; integrator must retire this freeze and make a documented new version before use.")
    result = {"schema_version": SCHEMA, "manifest_sha256": receipt["manifest_sha256"], "review_status": "complete", "groups": groups, "group_config": merged}
    with target.open("xb") as stream:
        stream.write(encoded(result))
    return {"review_status": "complete", "groups": len(groups), "model_tuning_allowed": True, "review_sha256": digest(target)}


def reviewed_groups(args, manifest, receipt) -> list[dict]:
    if manifest["near_duplicate_review"] == "complete":
        return manifest["groups"]
    target = args.freeze / "group_review.json"
    require(target.is_file(), "Human similarity review is pending; train/dev export and model tuning are blocked.")
    record = read_json(target)
    require(record.get("schema_version") == SCHEMA and record.get("review_status") == "complete" and record.get("manifest_sha256") == receipt["manifest_sha256"], "Human review is not bound to this freeze.")
    groups = make_groups(manifest["records"], group_config(record["group_config"]))
    require(groups == record["groups"], "Human review groups are inconsistent.")
    by_id = {row["canonical_id"]: row["split"] for row in manifest["records"]}
    require(all(len({by_id[key] for key in g["members"]}) == 1 for g in groups), "Similarity groups cross frozen splits.")
    return groups


def export(args) -> dict:
    manifest, receipt = validate(args)
    groups = reviewed_groups(args, manifest, receipt)
    require(not args.out.exists(), "ML package destination already exists; refusing overwrite.")
    group_by_id = {key: g["group_id"] for g in groups for key in g["members"]}
    rows = [row for row in manifest["records"] if row["split"] in ("train", "dev")]
    args.out.mkdir(parents=True, exist_ok=False)
    packaged = []
    for row in rows:
        source = inside(args.data_root, row["relative_path"])
        relative = f"videos/{row['canonical_id']}{source.suffix.lower()}"
        target = args.out / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(source, target)
        require(digest(target) == row["sha256"], "Copied source hash mismatch; package is incomplete.")
        packaged.append({**row, "relative_path": relative, "group_id": group_by_id[row["canonical_id"]]})
    (args.out / "train_dev.csv").write_bytes(ml_csv({"records": packaged}))
    package = {"schema_version": SCHEMA, "source_manifest_sha256": receipt["manifest_sha256"], "algorithm": ALGORITHM,
               "model_tuning_allowed": True, "train_dev_manifest_sha256": digest(args.out / "train_dev.csv"),
               "summary": {split: manifest["summary"][split] for split in ("train", "dev")}}
    (args.out / "package.json").write_bytes(encoded(package))
    return package


def parser() -> argparse.ArgumentParser:
    root = argparse.ArgumentParser(description=__doc__)
    commands = root.add_subparsers(dest="command", required=True)
    create = commands.add_parser("freeze", help="Create immutable private split and public aggregate receipt.")
    create.add_argument("--out", type=Path, default=Path("analysis/local/splits/v1"))
    create.add_argument("--groups", type=Path, help="Local human similarity groups, no scene descriptions.")
    create.add_argument("--sizes", type=int, nargs=3, default=[22, 8, 10], metavar=("TRAIN", "DEV", "TEST"))
    create.add_argument("--seed", type=int, default=20261017)
    create.add_argument("--exclude-test", nargs="+", default=["X-4", "X-47"])
    check = commands.add_parser("validate", help="Check receipt, source bytes, classes and exact reconstruction.")
    human = commands.add_parser("review", help="Record completed local human review; never moves frozen groups.")
    human.add_argument("--groups", type=Path, required=True)
    package = commands.add_parser("export-train-dev", help="Copy only train/dev after human review, without frozen-test labels.")
    package.add_argument("--out", type=Path, required=True)
    for cmd in (create, check, human, package):
        cmd.add_argument("--audit-dir", type=Path, default=Path("analysis/local"))
        cmd.add_argument("--data-root", type=Path, default=Path("."))
        cmd.add_argument("--receipt", type=Path, default=Path("docs/data_split_receipt.json"))
        if cmd is not create:
            cmd.add_argument("--freeze", type=Path, default=Path("analysis/local/splits/v1"))
    return root


def main() -> int:
    args = parser().parse_args()
    try:
        if args.command == "freeze":
            result = freeze(args)
        elif args.command == "validate":
            _, result = validate(args)
        elif args.command == "review":
            result = review(args)
        else:
            result = export(args)
        print(json.dumps(result, ensure_ascii=True, sort_keys=True, indent=2))
        return 0
    except SplitError as exc:
        print(f"Split rejected: {exc}", file=sys.stderr)
    except (OSError, ValueError, TypeError, KeyError, AttributeError):
        # Raw exceptions may contain dataset paths, identities or annotation text.
        print("Split rejected: invalid/missing local input or output; inspect files locally.", file=sys.stderr)
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
