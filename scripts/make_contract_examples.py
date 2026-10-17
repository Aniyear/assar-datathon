"""Generate synthetic fixture JSON only. Never imports or reads bank data."""
from __future__ import annotations

import base64
from io import BytesIO
import json
from pathlib import Path

from PIL import Image, ImageDraw

ROOT = Path(__file__).resolve().parents[1]


def main() -> None:
    image = Image.new("RGB", (160, 64), "#172d38")
    ImageDraw.Draw(image).text((16, 24), "SYNTHETIC FIXTURE", fill="white")
    buffer = BytesIO()
    image.save(buffer, format="JPEG", quality=80)
    jpeg = base64.b64encode(buffer.getvalue()).decode("ascii")
    def event(second: float, count: int) -> dict:
        return {"type": "synthetic_contract_example", "timecode_sec": second,
                "description": "СИНТЕТИЧЕСКИЙ ТЕСТ КОНТРАКТА. Это не наблюдение и не нормативное заключение. "
                               "Тестовый источник: https://example.invalid/synthetic-rule (не действующий акт).",
                "evidence_frames": [{"timestamp_sec": second + i / 10, "image_base64": jpeg}
                                    for i in range(count)]}
    examples = {
        "accepted": {"job_id": "synthetic-job"},
        "processing": {"job_id": "synthetic-job", "status": "processing"},
        "failed": {"job_id": "synthetic-job", "status": "failed"},
        "done_no_violation": {"job_id": "synthetic-job", "status": "done",
                              "verdict": "no_violation", "violations": []},
        "done_violation": {"job_id": "synthetic-job", "status": "done", "verdict": "violation",
                           "violations": [event(1.0, 1), event(3.0, 3)]},
    }
    target = ROOT / "tests/fixtures/contracts"
    target.mkdir(parents=True, exist_ok=True)
    for name, payload in examples.items():
        (target / f"{name}.json").write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print("Generated 5 synthetic contract fixtures; no dataset accessed.")


if __name__ == "__main__":
    main()
