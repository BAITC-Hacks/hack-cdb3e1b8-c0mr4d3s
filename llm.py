"""Provider-independent JSON generation interface and the Gemini adapter."""

import json
from typing import Protocol

from config import GeminiConfig
from logging_config import logger


class JsonLLMClient(Protocol):
    def generate_json(self, prompt: str, schema: dict) -> object:
        """Return parsed JSON or raise on request/parsing failure."""
        ...


class GeminiClient:
    def __init__(self, settings: GeminiConfig):
        self.settings = settings

    def generate_json(self, prompt: str, schema: dict) -> object:
        from google import genai

        settings = self.settings
        with genai.Client(
            api_key=settings.api_key,
            vertexai=False,
            http_options={
                "timeout": settings.timeout_ms,
                "retry_options": {"attempts": settings.max_attempts},
            },
        ) as client:
            response = client.models.generate_content(
                model=settings.model,
                contents=prompt,
                config={
                    "temperature": settings.temperature,
                    "max_output_tokens": settings.max_output_tokens,
                    "thinking_config": {"thinking_budget": settings.thinking_budget},
                    "response_mime_type": "application/json",
                    "response_json_schema": schema,
                },
            )
        return json.loads(response.text)


def create_llm_client(settings: GeminiConfig) -> JsonLLMClient | None:
    if not settings.enabled:
        return None
    if not settings.api_key:
        logger.warning("LLM key missing; using historical ranking.")
        return None
    return GeminiClient(settings)
