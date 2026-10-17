"""CPU-only shared-contract smoke check; no service or inference is started."""
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from app.contracts import AnalyzeAccepted, DoneJob, JOB_RESPONSE_ADAPTER, RuleCatalog, validate_done_for_video


def main() -> None:
    folder = ROOT / "tests/fixtures/contracts"
    expected = {"accepted", "processing", "failed", "done_no_violation", "done_violation"}
    if {path.stem for path in folder.glob("*.json")} != expected:
        raise ValueError("Expected exactly five synthetic contract fixtures")
    for path in sorted(folder.glob("*.json")):
        if path.stem == "accepted":
            AnalyzeAccepted.model_validate_json(path.read_text(encoding="utf-8"))
        else:
            result = JOB_RESPONSE_ADAPTER.validate_json(path.read_text(encoding="utf-8"))
            if isinstance(result, DoneJob):
                validate_done_for_video(result, duration_sec=5.0)
    catalog = RuleCatalog.model_validate_json((ROOT / "app/rules/catalog.json").read_text(encoding="utf-8"))
    print(f"Contract 0.1: 5 synthetic fixtures valid; verified rules: {sum(r.status == 'verified' for r in catalog.rules)}.")
    print("This check runs shared contracts only; no service or inference was run.")


if __name__ == "__main__":
    main()
