from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml


ROOT = Path(__file__).resolve().parents[1]


def _load_dotenv(path: Path) -> None:
    """加载 .env 中的键值到进程环境；已存在的环境变量优先，不覆盖。"""
    if not path.exists():
        return
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        key = key.strip()
        value = value.strip().strip('"').strip("'")
        if key and value and key not in os.environ:
            os.environ[key] = value


_load_dotenv(ROOT / ".env")


@dataclass(frozen=True)
class ProviderProfile:
    name: str
    protocol: str
    model: str
    base_url: str | None = None
    secret_env: str | None = None
    supports_vision: bool = False
    supports_json: bool = False
    context_window: int | None = None
    timeout_seconds: float = 120
    max_retries: int = 3
    omit_parameters: tuple[str, ...] = ()
    parameters: dict[str, Any] = field(default_factory=dict)
    usage_schema: str = "openai"

    def __post_init__(self):
        if self.usage_schema not in ("openai", "anthropic_cache_additive"):
            raise ValueError("unsupported provider usage schema")

    @property
    def configured(self) -> bool:
        return not self.secret_env or bool(os.getenv(self.secret_env))

    def public_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "protocol": self.protocol,
            "model": self.model,
            "base_url": self.base_url,
            "secret_env": self.secret_env,
            "configured": self.configured,
            "supports_vision": self.supports_vision,
            "supports_json": self.supports_json,
            "context_window": self.context_window,
            "timeout_seconds": self.timeout_seconds,
            "usage_schema": self.usage_schema,
            "omit_parameters": list(self.omit_parameters),
        }


@dataclass(frozen=True)
class LabSettings:
    root: Path = ROOT
    host: str = "127.0.0.1"
    port: int = 8852
    runs_dir: Path = ROOT / "runs"
    env_backend: str = "mock"
    scenegen_url: str = "http://127.0.0.1:8860"
    inspector_url: str = "http://127.0.0.1:8851"
    provider_config: Path = ROOT / "config" / "providers.example.yaml"
    focused_timeout_seconds: float = 300
    context_character_budget: int = 40000

    @classmethod
    def from_env(cls) -> "LabSettings":
        return cls(
            host=os.getenv("AGENTLAB_HOST", "127.0.0.1"),
            port=int(os.getenv("AGENTLAB_PORT", "8852")),
            runs_dir=Path(os.getenv('AGENTLAB_RUNS_DIR', str(ROOT / 'runs'))),
            provider_config=Path(os.getenv("AGENTLAB_PROVIDER_CONFIG", str(ROOT / "config" / "providers.example.yaml"))),
            env_backend=os.getenv("AGENTLAB_ENV_BACKEND", "mock"),
            scenegen_url=os.getenv("AGENTLAB_SCENEGEN_URL", "http://127.0.0.1:8860"),
            inspector_url=os.getenv("AGENTLAB_INSPECTOR_URL", "http://127.0.0.1:8851"),
        )


def load_provider_profiles(path: Path | None = None) -> dict[str, ProviderProfile]:
    config_path = path or Path(os.getenv("AGENTLAB_PROVIDER_CONFIG", str(ROOT / "config" / "providers.example.yaml")))
    raw = yaml.safe_load(config_path.read_text(encoding="utf-8")) or {}
    profiles: dict[str, ProviderProfile] = {}
    for name, item in raw.get("providers", {}).items():
        item = item or {}
        profiles[name] = ProviderProfile(
            name=name,
            protocol=str(item["protocol"]),
            model=str(item["model"]),
            base_url=item.get("base_url"),
            secret_env=item.get("secret_env"),
            supports_vision=bool(item.get("supports_vision", False)),
            supports_json=bool(item.get("supports_json", False)),
            context_window=item.get("context_window"),
            timeout_seconds=float(item.get("timeout_seconds", 120)),
            max_retries=int(item.get("max_retries", 3)),
            omit_parameters=tuple(item.get("omit_parameters", [])),
            parameters=dict(item.get("parameters", {})),
            usage_schema=str(item.get("usage_schema", "openai")),
        )
    return profiles
