"""ML-адаптер assar. Импорт для backend worker: см. app/inference/README.md, раздел «Интеграция»."""
from . import conditions, facts_prompt
from .analyzer import Analyzer, AnalyzerNotReadyError, analyze_video, init_worker
from .config import ModelConfig, ModelConfigError, load_model_config, load_rule_catalog, verify_runtime_files
from .vlm import LlamaClient, LlamaServer, ServerConfig, build_command, free_port

__all__ = ["Analyzer", "AnalyzerNotReadyError", "analyze_video", "init_worker", "ModelConfig", "ModelConfigError",
           "load_model_config", "load_rule_catalog", "verify_runtime_files", "LlamaClient", "LlamaServer",
           "ServerConfig", "build_command", "free_port"]
