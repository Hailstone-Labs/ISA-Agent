"""Centralized configuration and air-gap environment enforcement for ISA."""

from __future__ import annotations

import os
from pathlib import Path
from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


AIRGAP_ENV_DEFAULTS: dict[str, str] = {
    "OLLAMA_NO_CLOUD": "1",
    "OLLAMA_NO_UPDATE_CHECK": "1",
    "OLLAMA_HOST": "127.0.0.1:11434",
    "COPILOT_OFFLINE": "true",
    "OPENCODE_AIRGAPPED": "1",
    "PYDANTIC_AI_NO_BANNER": "1",
    "HF_HUB_OFFLINE": "1",
}


def enforce_airgap_env() -> dict[str, str]:
    """Ensure strict offline environment variables are set before initializing services."""
    for key, required_val in AIRGAP_ENV_DEFAULTS.items():
        os.environ[key] = required_val
    return {k: os.environ[k] for k in AIRGAP_ENV_DEFAULTS}


class ISASettings(BaseSettings):
    """Application settings loaded from environment variables or .env file."""

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    # Air-gap enforcement flags
    ollama_no_cloud: int = Field(default=1, alias="OLLAMA_NO_CLOUD")
    ollama_no_update_check: int = Field(default=1, alias="OLLAMA_NO_UPDATE_CHECK")
    ollama_host: str = Field(default="127.0.0.1:11434", alias="OLLAMA_HOST")

    # Local LLM settings
    llm_base_url: str = Field(
        default="http://127.0.0.1:11434/v1", alias="ISA_LLM_BASE_URL"
    )
    llm_model: str = Field(default="gemma4:12b", alias="ISA_LLM_MODEL")
    llm_num_ctx: int = Field(default=8192, alias="ISA_LLM_NUM_CTX")

    # Obsidian Local REST API settings
    obsidian_api_url: str = Field(
        default="https://127.0.0.1:27124", alias="OBSIDIAN_API_URL"
    )
    obsidian_api_key: str = Field(default="", alias="OBSIDIAN_API_KEY")
    obsidian_verify_ssl: bool = Field(default=False, alias="OBSIDIAN_VERIFY_SSL")

    # Vault & Watchdog settings
    obsidian_vault_path: Path = Field(
        default=Path("./vault"), alias="OBSIDIAN_VAULT_PATH"
    )
    inbox_filename: str = Field(default="ISA_Inbox.md", alias="ISA_INBOX_FILENAME")
    active_tasks_filename: str = Field(
        default="Tasks.md", alias="ISA_ACTIVE_TASKS_FILENAME"
    )
    calendar_folder: str = Field(
        default="Calendar", alias="ISA_CALENDAR_FOLDER"
    )
    debounce_seconds: float = Field(default=1.0, alias="ISA_DEBOUNCE_SECONDS")

    # Semantic Memory (LanceDB) settings
    lancedb_uri: Path = Field(default=Path("./data/lancedb"), alias="LANCEDB_URI")
    embedding_model: str = Field(
        default="BAAI/bge-small-en-v1.5", alias="ISA_EMBEDDING_MODEL"
    )
    rrf_k: int = Field(default=60, alias="ISA_RRF_K")

    @property
    def inbox_path(self) -> Path:
        """Full filesystem path to the monitored Obsidian inbox Markdown note."""
        return self.obsidian_vault_path / self.inbox_filename


def get_settings() -> ISASettings:
    """Enforce air-gap environment controls and return validated settings."""
    enforce_airgap_env()
    return ISASettings()
