"""Local, synthetic-only browser harness. Never starts a model or reads datasets."""
import argparse
from pathlib import Path
import subprocess
import sys
import tempfile

from fastapi import FastAPI
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
import uvicorn


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--backend-checkout", type=Path)
    parser.add_argument("--port", type=int, default=8765)
    args = parser.parse_args()
    web = Path(__file__).resolve().parents[1]
    repo = web.parents[1]
    app = FastAPI()
    with tempfile.TemporaryDirectory(prefix="assar-synthetic-ui-") as temp:
        clip = Path(temp) / "synthetic.mov"
        subprocess.run(["ffmpeg", "-nostdin", "-hide_banner", "-loglevel", "error", "-f", "lavfi",
                        "-i", "color=c=blue:s=160x120:r=10:d=5", "-an", "-c:v",
                        "libx264", "-threads", "1", "-pix_fmt", "yuv420p", str(clip)], check=True, timeout=30)

        @app.get("/__synthetic.mov")
        def synthetic_video():
            return FileResponse(clip, media_type="video/quicktime")

        @app.get("/__fixture.json")
        def synthetic_result():
            return FileResponse(repo / "tests/fixtures/contracts/done_violation.json")

        if args.backend_checkout:
            sys.path.insert(0, str(args.backend_checkout.resolve()))
            from app.api.main import create_app
            backend = create_app(web_dir=web)
            assert any(getattr(route, "path", None) == "/api/v1/analyze" for route in backend.routes)
            assert backend.routes[-1].name == "web"
            app.mount("/", backend)
        else:
            app.mount("/", StaticFiles(directory=web, html=True))
        # This harness deliberately disables startup; HTTP API calls are mocked in
        # the test iframe. This is a static/UI check, never an inference check.
        uvicorn.run(app, host="127.0.0.1", port=args.port, lifespan="off", access_log=False)
