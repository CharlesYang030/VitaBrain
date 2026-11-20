from __future__ import annotations

from dataclasses import replace

from agentlab.config import ProviderProfile
from .base import Provider
from .mock import MockProvider
from .openai_compatible import OpenAICompatibleProvider


class ProviderRegistry:
    def __init__(self, profiles: dict[str, ProviderProfile]):
        self.profiles = profiles

    def public_profiles(self) -> list[dict]:
        return [profile.public_dict() for profile in self.profiles.values()]

    def create(self, name: str,
               parameter_overrides: dict | None = None) -> Provider:
        profile = self.profiles.get(name)
        if not profile:
            raise KeyError(f"unknown provider: {name}")
        if parameter_overrides:
            profile = replace(
                profile,
                parameters={**profile.parameters, **parameter_overrides})
        if profile.protocol == "mock":
            return MockProvider(profile.model)
        if profile.protocol == "openai_chat":
            return OpenAICompatibleProvider(profile)
        raise ValueError(f"unsupported provider protocol: {profile.protocol}")

