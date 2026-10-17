"""Real static files and live API together; analyzer and MOV are SYNTHETIC, no GPU."""
import hashlib
import urllib.request

import pytest

from app.api.main import WEB_DIR, create_app
from app.contracts import DoneJob, validate_done_for_video
from app.jobs.supervisor import RuntimeSpec
from app.settings import Settings
from tests.api.fakes import CATALOG
from tests.api.harness import LiveServer, have_ffmpeg, make_mov


@pytest.mark.skipif(not have_ffmpeg(), reason="ffmpeg/ffprobe not in PATH")
def test_shipped_static_and_api_share_origin_with_synthetic_analyzer(tmp_path):
    control = tmp_path / "mode.txt"
    control.write_text("violation", encoding="utf-8")
    settings = Settings(api_key="synthetic-key", uploads_dir=tmp_path / "uploads",
                        outputs_dir=tmp_path / "outputs", model_stop_grace_sec=2)
    spec = RuntimeSpec("tests.api.fakes:create", {"control": str(control)}, CATALOG)
    clip = make_mov(tmp_path / "synthetic.mov").read_bytes()
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    # Use the real default web_dir; don't replace it with a minimal test index.
    with LiveServer(create_app(settings, lambda _: spec)) as live:
        for route, filename, content_type in (
            ("/", "index.html", "text/html"),
            ("/index.html", "index.html", "text/html"),
            ("/styles.css", "styles.css", "text/css"),
            ("/app.js", "app.js", "javascript"),
        ):
            with opener.open(live.base + route, timeout=10) as response:
                assert response.status == 200
                assert content_type in response.headers["Content-Type"]
                assert hashlib.sha256(response.read()).digest() == hashlib.sha256((WEB_DIR / filename).read_bytes()).digest()
        assert live.request("GET", "/api/v1/jobs/missing")[0] == 401
        assert live.request("GET", "/api/v1/jobs/missing", "synthetic-key")[0] == 404
        assert live.request("GET", "/api/internal/status", "synthetic-key") == (200, {"accepting": True})
        status, accepted = live.upload("synthetic-key", clip)
        assert status == 202
        result = live.wait_terminal("synthetic-key", accepted["job_id"])
        job = DoneJob.model_validate(result)
        validate_done_for_video(job, 3.0)
        assert job.verdict == "violation"
        assert "Синтетическое наблюдение" in job.violations[0].description
        live.wait_accepting("synthetic-key")
        assert not (settings.uploads_dir / job.job_id).exists()
        assert not (settings.outputs_dir / "jobs" / job.job_id).exists()
        assert live.request("GET", f"/api/v1/jobs/{job.job_id}", "synthetic-key") == (200, result)
