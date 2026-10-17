"""Локальный llama-server (llama.cpp) как процесс worker'а: запуск, health, запрос с кадрами, остановка.

Сервер слушает только 127.0.0.1; внешних обращений нет.
"""
from __future__ import annotations

import base64
import json
import socket
import subprocess
import time
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from pathlib import Path


class ModelError(RuntimeError):
    """Модель недоступна или вернула технически непригодный ответ."""


@dataclass
class ServerConfig:
    server_bin: Path
    model: Path
    mmproj: Path
    ctx: int = 16384
    n_gpu_layers: int = 99
    port: int = 0                 # 0 -> свободный порт
    image_max_tokens: int = 0     # 0 -> значение по умолчанию llama.cpp
    extra_args: list[str] = field(default_factory=list)


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


class LlamaServer:
    def __init__(self, cfg: ServerConfig, log_path: Path):
        self.cfg = cfg
        self.port = cfg.port or _free_port()
        self.log_path = log_path
        self.proc: subprocess.Popen | None = None

    @property
    def url(self) -> str:
        return f"http://127.0.0.1:{self.port}"

    def start(self, timeout: float = 180) -> float:
        """Запускает сервер и ждёт готовности. Возвращает время загрузки в секундах."""
        c = self.cfg
        cmd = [str(c.server_bin), "-m", str(c.model), "--mmproj", str(c.mmproj),
               "--host", "127.0.0.1", "--port", str(self.port), "-c", str(c.ctx), "-ngl", str(c.n_gpu_layers),
               "-np", "1", "--no-webui", "--offline", "--jinja"]
        if c.image_max_tokens:
            cmd += ["--image-max-tokens", str(c.image_max_tokens)]
        cmd += c.extra_args
        self.log_path.parent.mkdir(parents=True, exist_ok=True)
        log = open(self.log_path, "w", encoding="utf-8", errors="replace")
        t0 = time.monotonic()
        self.proc = subprocess.Popen(cmd, stdout=log, stderr=subprocess.STDOUT,
                                     creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        while time.monotonic() - t0 < timeout:
            if self.proc.poll() is not None:
                raise ModelError(f"llama-server exited with code {self.proc.returncode}; see {self.log_path}")
            try:
                with urllib.request.urlopen(f"{self.url}/health", timeout=2) as r:
                    if r.status == 200 and json.loads(r.read()).get("status") == "ok":
                        return time.monotonic() - t0
            except (urllib.error.URLError, ConnectionError, TimeoutError, json.JSONDecodeError):
                pass
            time.sleep(0.25)
        self.stop()
        raise ModelError("llama-server did not become ready in time")

    def stop(self) -> None:
        if self.proc and self.proc.poll() is None:
            self.proc.terminate()
            try:
                self.proc.wait(timeout=10)
            except subprocess.TimeoutExpired:
                self.proc.kill()
                self.proc.wait(timeout=10)
        self.proc = None

    def chat(self, parts: list[str | Path], schema: dict | None, max_tokens: int, timeout: float,
             seed: int = 0) -> dict:
        """parts: текст (str) и пути к JPEG (Path) в порядке подачи. Возвращает сырой ответ сервера."""
        content = []
        for p in parts:
            if isinstance(p, Path):
                b64 = base64.b64encode(p.read_bytes()).decode("ascii")
                content.append({"type": "image_url", "image_url": {"url": f"data:image/jpeg;base64,{b64}"}})
            else:
                content.append({"type": "text", "text": p})
        body = {"messages": [{"role": "user", "content": content}], "temperature": 0.0, "top_k": 1,
                "seed": seed, "max_tokens": max_tokens, "cache_prompt": False}
        if schema is not None:
            body["response_format"] = {"type": "json_schema", "json_schema": {"name": "obs", "schema": schema}}
        req = urllib.request.Request(f"{self.url}/v1/chat/completions", data=json.dumps(body).encode(),
                                     headers={"Content-Type": "application/json"})
        try:
            with urllib.request.urlopen(req, timeout=timeout) as r:
                return json.loads(r.read())
        except (urllib.error.URLError, TimeoutError, ConnectionError) as e:
            raise ModelError(f"request failed: {type(e).__name__}") from e
