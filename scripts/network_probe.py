"""Explicit network test only. No model, catalog, dataset, uploads or production API."""
from __future__ import annotations

import hmac

from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse, JSONResponse, Response

PAGE = """<!doctype html><html lang="ru"><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>assar — ТЕСТ СЕТИ</title>
<body style="max-width:680px;margin:40px auto;padding:20px;font:18px system-ui">
<h1>assar · ТЕСТ СЕТИ</h1>
<p>Это временная техническая страница. Анализа видео здесь нет.
Загрузка файлов отключена. Банковские данные не используются.</p>
<p id="health">HTTPS-страница доступна.</p>
<label>Отдельный тестовый ключ <input id="key" type="password" autocomplete="off"></label>
<button id="check">Проверить API</button><p id="result" role="status"></p>
<script>
document.getElementById('check').onclick = async () => {
 const input = document.getElementById('key');
 try {
  const r = await fetch('/api/test', {headers:{'X-API-Key':input.value},cache:'no-store'});
  document.getElementById('result').textContent = r.status === 200
   ? 'Тестовый API доступен, ключ принят. Это не inference.'
   : 'Ключ не принят: HTTP ' + r.status;
 } catch (_) { document.getElementById('result').textContent = 'Сеть недоступна'; }
 input.value = '';
};
addEventListener('pagehide', () => { document.getElementById('key').value = ''; });
</script></body></html>"""


def create_probe(key: str, run_id: str) -> FastAPI:
    app = FastAPI(docs_url=None, redoc_url=None, openapi_url=None)

    @app.middleware("http")
    async def restrict(request: Request, call_next):
        # Reject before reading any body. Never log headers, keys or request payloads.
        if request.method != "GET":
            return Response(status_code=405, headers={"Allow": "GET"})
        response = await call_next(request)
        response.headers.update({"Cache-Control": "no-store", "X-Assar-Mode": "network-test",
                                 "X-Content-Type-Options": "nosniff", "Referrer-Policy": "no-referrer"})
        return response

    @app.get("/", response_class=HTMLResponse)
    def page():
        return PAGE

    @app.get("/health")
    def health():
        return {"mode": "network-test", "run_id": run_id, "inference": False, "uploads": False}

    @app.get("/api/test")
    def api(request: Request):
        given = request.headers.get("x-api-key", "")
        if not hmac.compare_digest(given.encode(), key.encode()):
            return JSONResponse({"detail": "Invalid or missing test key"}, status_code=401)
        return {"mode": "network-test", "result": "synthetic-network-ok", "run_id": run_id}

    return app
