"""Локальный llama-server (llama.cpp): loopback-клиент и примитивы lifecycle для supervisor.

Владелец процесса сервера — backend supervisor: он вызывает build_command/LlamaServer один раз при старте,
хранит handle и останавливает сервер при зависании. analyze_video получает только LlamaClient.
Клиент разрешает лишь http://127.0.0.1:<port>, обходит системные proxy и не следует редиректам.
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
from urllib.parse import urlsplit

from app.contracts import AnalysisTimeoutError, InvalidAnalysisResultError, ModelUnavailableError

ModelError = ModelUnavailableError  # прежнее имя baseline


@dataclass
class ServerConfig:
    server_bin: Path
    model: Path
    mmproj: Path
    ctx: int = 8192
    n_gpu_layers: int = 99
    parallel: int = 1
    port: int = 0                 # 0 -> свободный порт
    image_max_tokens: int = 256   # 0 -> значение по умолчанию llama.cpp
    offline: bool = True
    no_webui: bool = True
    jinja: bool = True
    extra_args: list[str] = field(default_factory=list)


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def build_command(cfg: ServerConfig, port: int) -> list[str]:
    cmd = [str(cfg.server_bin), "-m", str(cfg.model), "--mmproj", str(cfg.mmproj),
           "--host", "127.0.0.1", "--port", str(port), "-c", str(cfg.ctx), "-ngl", str(cfg.n_gpu_layers),
           "-np", str(cfg.parallel)]
    if cfg.no_webui:
        cmd.append("--no-webui")
    if cfg.offline:
        cmd.append("--offline")
    if cfg.jinja:
        cmd.append("--jinja")
    if cfg.image_max_tokens:
        cmd += ["--image-max-tokens", str(cfg.image_max_tokens)]
    return cmd + list(cfg.extra_args)


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        raise urllib.error.HTTPError(req.full_url, code, "redirects are not allowed", headers, fp)


class LlamaClient:
    """HTTP-клиент собственного llama-server на 127.0.0.1. Не запускает и не останавливает сервер."""

    def __init__(self, base_url: str):
        u = urlsplit(base_url)
        if u.scheme != "http" or u.hostname != "127.0.0.1" or not u.port or u.path not in ("", "/") \
                or u.username or u.password or u.query or u.fragment:
            raise ValueError("LlamaClient accepts only http://127.0.0.1:<port>")
        self.base_url = f"http://127.0.0.1:{u.port}"
        # ProxyHandler({}) — игнорировать HTTP(S)_PROXY окружения; без редиректов.
        self._opener = urllib.request.build_opener(urllib.request.ProxyHandler({}), _NoRedirect())

    def health(self, timeout: float = 2.0) -> bool:
        try:
            with self._opener.open(f"{self.base_url}/health", timeout=timeout) as r:
                return r.status == 200 and json.loads(r.read()).get("status") == "ok"
        except (urllib.error.URLError, OSError, ValueError):
            return False

    def chat(self, parts: list[str | Path], schema: dict | None, *, max_tokens: int, timeout: float,
             temperature: float = 0.0, top_k: int = 1, seed: int = 0, cache_prompt: bool = False,
             extra_body: dict | None = None) -> dict:
        """parts: текст (str) и пути к JPEG (Path) в порядке подачи. Возвращает разобранный ответ сервера.

        timeout — socket timeout; без streaming сервер отвечает одним блоком, поэтому он близок к
        wall-clock времени запроса, но не заменяет watchdog supervisor.
        """
        content = []
        for p in parts:
            if isinstance(p, Path):
                b64 = base64.b64encode(p.read_bytes()).decode("ascii")
                content.append({"type": "image_url", "image_url": {"url": f"data:image/jpeg;base64,{b64}"}})
            else:
                content.append({"type": "text", "text": p})
        body = {"messages": [{"role": "user", "content": content}], "temperature": temperature, "top_k": top_k,
                "seed": seed, "max_tokens": max_tokens, "cache_prompt": cache_prompt, "stream": False}
        if extra_body:  # только для экспериментальных серверов (например, vLLM); production не передаёт
            body.update(extra_body)
        if schema is not None:
            body["response_format"] = {"type": "json_schema", "json_schema": {"name": "obs", "schema": schema}}
        req = urllib.request.Request(f"{self.base_url}/v1/chat/completions", data=json.dumps(body).encode(),
                                     headers={"Content-Type": "application/json"})
        try:
            with self._opener.open(req, timeout=timeout) as r:
                raw = r.read()
        except urllib.error.HTTPError as e:
            # Тело ответа сервера не пробрасываем: оно может содержать фрагменты запроса.
            raise ModelUnavailableError(f"llama-server HTTP {e.code}") from None
        except (TimeoutError, socket.timeout) as e:
            raise AnalysisTimeoutError("model request timeout") from e
        except urllib.error.URLError as e:
            if isinstance(e.reason, (TimeoutError, socket.timeout)):
                raise AnalysisTimeoutError("model request timeout") from e
            raise ModelUnavailableError(f"llama-server unreachable: {type(e.reason).__name__}") from e
        except (ConnectionError, OSError) as e:
            raise ModelUnavailableError(f"llama-server connection failed: {type(e).__name__}") from e
        try:
            resp = json.loads(raw)
            resp["choices"][0]["message"]
        except (ValueError, KeyError, IndexError, TypeError) as e:
            raise InvalidAnalysisResultError("malformed llama-server response") from e
        return resp


class LlamaServer:
    """Процесс llama-server. Используется supervisor'ом (один раз на сервис) и benchmark'ом."""

    def __init__(self, cfg: ServerConfig, log_path: Path):
        self.cfg = cfg
        self.port = cfg.port or free_port()
        self.log_path = log_path
        self.proc: subprocess.Popen | None = None
        self.client = LlamaClient(f"http://127.0.0.1:{self.port}")
        self._log = None

    @property
    def url(self) -> str:
        return self.client.base_url

    def spawn(self) -> subprocess.Popen:
        """Запускает процесс и сразу возвращает handle (до ожидания health) для учёта владельцем."""
        if self.proc is not None:
            raise RuntimeError("server already spawned")
        self.log_path.parent.mkdir(parents=True, exist_ok=True)
        self._log = open(self.log_path, "w", encoding="utf-8", errors="replace")
        self.proc = subprocess.Popen(build_command(self.cfg, self.port), stdout=self._log, stderr=subprocess.STDOUT,
                                     stdin=subprocess.DEVNULL,
                                     creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        return self.proc

    def wait_healthy(self, timeout: float) -> float:
        """Ждёт /health не дольше timeout. Возвращает затраченное время."""
        t0 = time.monotonic()
        while (left := timeout - (time.monotonic() - t0)) > 0:
            if self.proc is None or self.proc.poll() is not None:
                code = None if self.proc is None else self.proc.returncode
                raise ModelUnavailableError(f"llama-server exited with code {code}")
            if self.client.health(timeout=min(2.0, left)):
                return time.monotonic() - t0
            time.sleep(min(0.25, max(0.0, left)))
        raise ModelUnavailableError("llama-server did not become ready in time")

    def start(self, timeout: float = 180) -> float:
        """spawn + wait_healthy; при неудаче процесс останавливается."""
        t0 = time.monotonic()
        self.spawn()
        try:
            self.wait_healthy(timeout)
        except BaseException:
            self.stop()
            raise
        return time.monotonic() - t0

    def stop(self, grace: float = 10.0) -> None:
        """terminate → не дольше grace → kill → ожидание не дольше grace."""
        if self.proc and self.proc.poll() is None:
            self.proc.terminate()
            try:
                self.proc.wait(timeout=grace)
            except subprocess.TimeoutExpired:
                self.proc.kill()
                self.proc.wait(timeout=grace)
        self.proc = None
        if self._log:
            self._log.close()
            self._log = None

    def chat(self, parts, schema, max_tokens: int, timeout: float, seed: int = 0) -> dict:
        return self.client.chat(parts, schema, max_tokens=max_tokens, timeout=timeout, seed=seed)
