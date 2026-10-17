import importlib.util
from pathlib import Path

import pytest

spec = importlib.util.spec_from_file_location("split_policy", Path(__file__).resolve().parents[2] / "scripts/data/freeze_split.py")
split = importlib.util.module_from_spec(spec)
spec.loader.exec_module(split)


def manifest(part="dev"):
    # Entirely invented identities/hashes, no bank metadata.
    return {"records": [
        {"canonical_id": key, "group_id": "synthetic-group", "sha256": "synthetic-hash", "split": part}
        for key in ("X-101", "X-102")
    ]}


def test_pair_exclusively_dev():
    split.verify_development_pair(manifest(), ["X-101", "X-102"])


@pytest.mark.parametrize("part", ["train", "test"])
def test_train_is_not_development_for_required_pair(part):
    with pytest.raises(split.SplitError):
        split.verify_development_pair(manifest(part), ["X-101", "X-102"])


@pytest.mark.parametrize("change", ["group", "hash", "missing", "overlap"])
def test_bad_pair_or_other_group_overlap_rejected(change):
    value = manifest()
    if change == "group":
        value["records"][0]["group_id"] = "different"
    elif change == "hash":
        value["records"][0]["sha256"] = "different"
    elif change == "missing":
        value["records"].pop()
    else:
        value["records"].extend([
            {"canonical_id": "X-103", "group_id": "other", "sha256": "other", "split": "train"},
            {"canonical_id": "X-104", "group_id": "other", "sha256": "other", "split": "test"}])
    with pytest.raises(split.SplitError):
        split.verify_development_pair(value, ["X-101", "X-102"])
