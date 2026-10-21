"""Obsidian Local REST API client for bi-directional vault communication."""

from __future__ import annotations

from urllib.parse import urlparse
import httpx
from config.settings import ISASettings, get_settings


LOOPBACK_HOSTS = {"127.0.0.1", "localhost", "::1"}


class ObsidianRestClient:
    """Async client for the Obsidian Local REST API plugin."""

    def __init__(self, settings: ISASettings | None = None) -> None:
        self.settings = settings or get_settings()
        self.base_url = self.settings.obsidian_api_url.rstrip("/")
        self._validate_loopback(self.base_url)

    @staticmethod
    def _validate_loopback(url: str) -> None:
        """Enforce air-gap requirement: API must bind strictly to loopback."""
        parsed = urlparse(url)
        if parsed.hostname not in LOOPBACK_HOSTS:
            raise ValueError(
                f"Air-gap violation: Obsidian API host '{parsed.hostname}' is not a loopback address."
            )

    def _headers(self, extra: dict[str, str] | None = None) -> dict[str, str]:
        headers: dict[str, str] = {}
        if self.settings.obsidian_api_key:
            headers["Authorization"] = f"Bearer {self.settings.obsidian_api_key}"
        if extra:
            headers.update(extra)
        return headers

    async def health_check(self) -> dict:
        """Check status of the Obsidian Local REST API server (GET /)."""
        async with httpx.AsyncClient(verify=self.settings.obsidian_verify_ssl) as client:
            response = await client.get(f"{self.base_url}/", headers=self._headers())
            response.raise_for_status()
            return response.json()

    async def get_note(self, vault_path: str) -> str:
        """Read raw Markdown content of a file (GET /vault/{path})."""
        clean_path = vault_path.lstrip("/")
        async with httpx.AsyncClient(verify=self.settings.obsidian_verify_ssl) as client:
            response = await client.get(
                f"{self.base_url}/vault/{clean_path}",
                headers=self._headers({"Accept": "text/markdown"}),
            )
            response.raise_for_status()
            return response.text

    async def create_or_overwrite_note(self, vault_path: str, content: str) -> None:
        """Create or overwrite a Markdown file in the vault (PUT/POST /vault/{path})."""
        clean_path = vault_path.lstrip("/")
        async with httpx.AsyncClient(verify=self.settings.obsidian_verify_ssl) as client:
            response = await client.put(
                f"{self.base_url}/vault/{clean_path}",
                headers=self._headers({"Content-Type": "text/markdown"}),
                content=content.encode("utf-8"),
            )
            response.raise_for_status()

    async def append_to_note(self, vault_path: str, content: str) -> None:
        """Append content to the end of a vault file (POST /vault/{path})."""
        clean_path = vault_path.lstrip("/")
        async with httpx.AsyncClient(verify=self.settings.obsidian_verify_ssl) as client:
            response = await client.post(
                f"{self.base_url}/vault/{clean_path}",
                headers=self._headers({"Content-Type": "text/markdown"}),
                content=content.encode("utf-8"),
            )
            response.raise_for_status()

    async def patch_note(
        self,
        vault_path: str,
        content: str,
        operation: str = "append",
        target_type: str = "heading",
        target: str = "",
    ) -> None:
        """Targeted modification of a note section or frontmatter (PATCH /vault/{path})."""
        clean_path = vault_path.lstrip("/")
        patch_headers = {
            "Content-Type": "text/markdown",
            "Operation": operation,
            "Target-Type": target_type,
            "Target": target,
        }
        async with httpx.AsyncClient(verify=self.settings.obsidian_verify_ssl) as client:
            response = await client.patch(
                f"{self.base_url}/vault/{clean_path}",
                headers=self._headers(patch_headers),
                content=content.encode("utf-8"),
            )
            response.raise_for_status()

    async def search_simple(self, query: str, context_length: int = 100) -> list[dict]:
        """Execute full-text search via Obsidian's native index (POST /search/simple/)."""
        async with httpx.AsyncClient(verify=self.settings.obsidian_verify_ssl) as client:
            response = await client.post(
                f"{self.base_url}/search/simple/",
                headers=self._headers(),
                params={"query": query, "contextLength": context_length},
            )
            response.raise_for_status()
            return response.json()

    async def execute_command(self, command_id: str) -> None:
        """Trigger a native Obsidian or plugin command (POST /commands/{commandId}/)."""
        async with httpx.AsyncClient(verify=self.settings.obsidian_verify_ssl) as client:
            response = await client.post(
                f"{self.base_url}/commands/{command_id}/",
                headers=self._headers(),
            )
            response.raise_for_status()
