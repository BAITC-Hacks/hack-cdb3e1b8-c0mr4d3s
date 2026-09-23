# AGENTS.md

## Goal
Build a small, working hackathon product with a Python backend and React frontend. Prefer the simplest implementation that meets the current requirements.

## Stack
- Backend: Python 3.10 or newer. Use the framework already present; if none, prefer FastAPI.
- Frontend: React. Use the existing setup; if none, prefer Vite.
- Keep using the repository's existing package managers and conventions.

## Dependencies
- Use the project `.venv` for installing packages and running Python commands. Do not install project dependencies globally.
- Pin installed package versions, including transitive dependencies, in `requirements.txt`; keep it consistent with the verified environment.
- Prefer the Python standard library and existing dependencies.
- Add a dependency only when it provides clear value or avoids substantial custom code.
- Prefer mature, actively maintained packages with stable APIs.
- Check for an existing suitable dependency before adding another.

## Implementation
- Keep solutions small and direct. Don’t add layers, abstractions, or configuration without a concrete need.
- Follow the existing code style and structure. Use clear names and focused functions.
- Write readable code; avoid clever or overly compressed implementations.
- Add comments only to explain non-obvious reasoning or constraints. Don’t narrate what the code already says.
- Don’t add features that weren’t requested.

## Python
- Target Python 3.10 or newer.
- Prefer built-in generic types such as `list[int]`, `dict[str, int]`, and `tuple[str, ...]` over `typing.List`, `typing.Dict`, and similar legacy aliases.
- Import specific `typing` features only when built-in types cannot express what is needed (for example, `TypedDict` or `Protocol`).

## Configuration and prompts
- Load application settings and API keys from local `config.json`. Keep this file in `.gitignore`; never commit or print its secrets.
- Do not use `os.getenv`, `os.environ`, or environment variables as application configuration sources.
- Define structured configuration with dataclasses and useful defaults in `config.py`. Missing files or fields use defaults; malformed settings fail with a clear error.
- Keep tunable constants and model prompts outside the agent's business logic. Expose them through the configuration objects.
- Resolve configuration and data paths relative to the project modules, so agent behavior does not depend on the current working directory.

## External services and logging
- Keep LLM SDK calls in provider adapters behind a small interface, such as `JsonLLMClient.generate_json(prompt, schema)`. Allow injecting a replacement client without editing the agent's algorithm.
- Apply the same separation to other external service calls when they are introduced. Add adapters only for services needed by the task.
- Use the Google Gen AI Python SDK for the current Gemini adapter. Keep deterministic local behavior available when the optional LLM is disabled, unavailable, or returns invalid output.
- Bound external request time and retries; validate responses before using them in decisions.
- Centralize logger creation and configuration in a dedicated module. Reuse the initialized logger instead of calling `getLogger` at each log site.
- Make logging setup idempotent: repeated agent creation must not add duplicate handlers or reconfigure the root logger. Read log settings from the structured configuration.
- Do not log API keys or raw external exception messages that may contain secrets.

## Changes and verification
- Make the smallest coherent change that solves the task; avoid unrelated rewrites.
- Run relevant existing checks when practical and report what was run. If a check can’t be run, say why; don’t claim it passed if it wasn’t run.
