"""Pin the accepted product candidate independently of legacy observation tests."""
import json
from pathlib import Path

from app.contracts import RuleCatalog
from app.inference import Analyzer
from app.inference.config import load_model_config

ROOT = Path(__file__).resolve().parents[2]


def test_selected_candidate_matches_omar_handoff():
    raw = json.loads((ROOT / "configs/model.yaml").read_text(encoding="utf-8"))
    proposal = json.loads((ROOT / "app/inference/proposals/model_config.patch.json").read_text(encoding="utf-8"))
    for key in ("model_id", "model_revision", "weights", "mmproj"):
        assert raw[key] == proposal["option_4b"][key]
    assert raw["generation"]["prompt_version"] == "cash-specialists-v6"
    assert raw["generation"]["max_tokens"] == 500
    assert raw["runtime"]["tag"] == "b11669"
    assert raw["sampling"] == {"step_sec": 1.0, "window_sec": 10.0, "model_height": 0}
    cat = RuleCatalog.model_validate_json((ROOT / "app/rules/catalog.json").read_text(encoding="utf-8"))
    assert {r.rule_id for r in cat.rules} == {"MULTI-01", "CASH-03"}
    assert all(r.status == "verified" for r in cat.rules)
    multi = next(r for r in cat.rules if r.rule_id == "MULTI-01")
    assert "Прямого запрета" in multi.claim_template_ru and "требуется проверка" in multi.claim_template_ru
    analyzer = Analyzer(load_model_config(ROOT / "configs/model.yaml"), cat, object())
    assert set(analyzer.rules) == {"two_clients_at_once", "valuables_left_unattended"}
