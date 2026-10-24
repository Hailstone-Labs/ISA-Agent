"""Interactive Rich TUI installer and provisioner for the Intelligent Secretary Assistant (ISA)."""

from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import platform
import secrets
import shutil
import subprocess
import urllib.request
from rich.console import Console
from rich.panel import Panel
from rich.prompt import Confirm, Prompt
from rich.table import Table
from rich.text import Text

console = Console()

OBSIDIAN_PLUGINS: dict[str, tuple[str, str]] = {
    "obsidian-local-rest-api": (
        "coddingtonbear/obsidian-local-rest-api",
        "Local REST API (Vault CRUD bridge)",
    ),
    "obsidian-full-calendar": (
        "davish/obsidian-full-calendar",
        "Full Calendar (Visual schedule grid)",
    ),
    "obsidian-tasks-plugin": (
        "obsidian-tasks-group/obsidian-tasks",
        "Tasks (Recurring inline emoji tasks)",
    ),
    "templater-obsidian": (
        "SilentVoid13/Templater",
        "Templater (Dynamic note templates)",
    ),
}

DEFAULT_TEMPLATES: dict[str, str] = {
    "Daily Standup Template.md": (
        "---\n"
        'title: "<% tp.file.title %>"\n'
        'date: <% tp.date.now("YYYY-MM-DD") %>\n'
        "type: standup\n"
        "tags:\n"
        "  - standup\n"
        "  - daily\n"
        "---\n"
        '# Daily Standup — <% tp.date.now("YYYY-MM-DD") %>\n\n'
        "## ✅ Completed Yesterday\n- \n\n"
        "## 🎯 Today's Priorities\n- \n\n"
        "## 🚧 Blockers & Dependencies\n- None\n"
    ),
    "Meeting Minutes Template.md": (
        "---\n"
        'title: "<% tp.file.title %>"\n'
        'date: <% tp.date.now("YYYY-MM-DD") %>\n'
        'time: "<% tp.date.now("HH:mm") %>"\n'
        "type: meeting-minutes\n"
        "tags:\n"
        "  - meeting\n"
        "  - minutes\n"
        "---\n"
        "# Meeting Minutes: <% tp.file.title %>\n\n"
        '**Date:** <% tp.date.now("YYYY-MM-DD") %>\n\n'
        "## 👥 Attendees\n- \n\n"
        "## 📋 Agenda & Discussion Notes\n- \n\n"
        "## ⚡ Action Items\n- [ ] \n"
    ),
    "Project Brief Template.md": (
        "---\n"
        'title: "<% tp.file.title %>"\n'
        'created: <% tp.date.now("YYYY-MM-DD") %>\n'
        "status: draft\n"
        "type: project-brief\n"
        "tags:\n"
        "  - project\n"
        "  - brief\n"
        "---\n"
        "# Project Brief: <% tp.file.title %>\n\n"
        "## 🎯 Executive Summary\n- \n\n"
        "## 📐 Scope & Objectives\n- \n\n"
        "## 🗓️ Milestones & Timeline\n- \n"
    ),
}


def discover_obsidian_vaults() -> list[Path]:
    """Detect existing Obsidian vaults from the OS-specific obsidian.json config."""
    candidates = [
        Path.home() / "Library" / "Application Support" / "obsidian" / "obsidian.json",
        Path.home() / ".config" / "obsidian" / "obsidian.json",
    ]
    vaults: list[Path] = []
    for cfg_path in candidates:
        if not cfg_path.exists():
            continue
        try:
            data = json.loads(cfg_path.read_text(encoding="utf-8"))
            for info in data.get("vaults", {}).values():
                vpath = Path(info.get("path", ""))
                if vpath.exists() and vpath not in vaults:
                    vaults.append(vpath)
        except Exception:
            continue
    return vaults


def resolve_or_generate_obsidian_api_key(
    vault_path: Path,
    project_root: Path | None = None,
    port: int = 27124,
) -> str:
    """Read existing Local REST API key from the vault/.env or generate & pre-configure a new key."""
    plugin_dir = vault_path / ".obsidian" / "plugins" / "obsidian-local-rest-api"
    data_file = plugin_dir / "data.json"

    if data_file.exists():
        try:
            existing = json.loads(data_file.read_text(encoding="utf-8"))
            if existing.get("apiKey"):
                return str(existing["apiKey"])
        except Exception:
            pass

    if project_root is not None and (project_root / ".env").exists():
        try:
            for line in (project_root / ".env").read_text(encoding="utf-8").splitlines():
                if line.startswith("OBSIDIAN_API_KEY="):
                    val = line.split("=", 1)[1].strip()
                    if val and val != "your_obsidian_local_rest_api_key_here":
                        return val
        except Exception:
            pass

    new_key = secrets.token_hex(32)
    payload = {
        "apiKey": new_key,
        "port": port,
        "insecurePort": 27123,
        "enableInsecureServer": False,
    }
    try:
        plugin_dir.mkdir(parents=True, exist_ok=True)
        data_file.write_text(json.dumps(payload, indent=2), encoding="utf-8")
    except OSError:
        pass
    return new_key


def install_obsidian_plugins(vault_path: Path) -> list[str]:
    """Download and pre-configure Local REST API, Full Calendar, Tasks, and Templater plugins."""
    obsidian_dir = vault_path / ".obsidian"
    plugins_dir = obsidian_dir / "plugins"
    plugins_dir.mkdir(parents=True, exist_ok=True)

    installed: list[str] = []
    for plugin_id, (repo, label) in OBSIDIAN_PLUGINS.items():
        target = plugins_dir / plugin_id
        target.mkdir(parents=True, exist_ok=True)
        if (target / "main.js").exists() and (target / "manifest.json").exists():
            console.print(f"  [green]✓[/green] {label} ([cyan]{plugin_id}[/cyan]) already present.")
            installed.append(plugin_id)
            continue

        console.print(f"  [yellow]↓[/yellow] Downloading {label} ([cyan]{repo}[/cyan])...")
        req = urllib.request.Request(
            f"https://api.github.com/repos/{repo}/releases/latest",
            headers={"User-Agent": "ISA-TUI-Installer"},
        )
        with urllib.request.urlopen(req, timeout=15) as resp:
            release = json.loads(resp.read().decode("utf-8"))

        for asset in release.get("assets", []):
            name = asset.get("name", "")
            if name in ("main.js", "manifest.json", "styles.css"):
                urllib.request.urlretrieve(asset["browser_download_url"], target / name)
        installed.append(plugin_id)
        console.print(f"  [green]✓[/green] Installed [cyan]{plugin_id}[/cyan].")

    # Pre-configure Full Calendar to watch "Calendar" folder
    fc_config = plugins_dir / "obsidian-full-calendar" / "data.json"
    if not fc_config.exists():
        fc_config.write_text(
            json.dumps(
                {
                    "calendarSources": [
                        {"type": "local", "color": "#3b82f6", "directory": "Calendar"}
                    ],
                    "defaultCalendar": 0,
                    "firstDay": 0,
                    "initialView": {"desktop": "timeGridWeek", "mobile": "timeGrid3Days"},
                    "timeFormat24h": False,
                    "clickToCreateEventFromMonthView": True,
                },
                indent=2,
            ),
            encoding="utf-8",
        )

    # Pre-configure Templater to use "Templates" folder
    tp_config = plugins_dir / "templater-obsidian" / "data.json"
    if not tp_config.exists():
        tp_config.write_text(
            json.dumps(
                {
                    "command_timeout": 5,
                    "templates_folder": "Templates",
                    "templates_pairs": [["", ""]],
                    "trigger_on_file_creation": True,
                    "auto_jump_to_cursor": False,
                    "enable_system_commands": False,
                },
                indent=2,
            ),
            encoding="utf-8",
        )

    # Enable plugins in community-plugins.json
    cp_file = obsidian_dir / "community-plugins.json"
    enabled: list[str] = []
    if cp_file.exists():
        try:
            enabled = json.loads(cp_file.read_text(encoding="utf-8"))
        except Exception:
            enabled = []
    for pid in OBSIDIAN_PLUGINS:
        if pid not in enabled:
            enabled.append(pid)
    cp_file.write_text(json.dumps(enabled, indent=2), encoding="utf-8")
    return installed


def scaffold_vault_files(vault_path: Path) -> None:
    """Create Calendar/, Templates/, Tasks.md, and ISA_Inbox.md in the target vault if missing."""
    (vault_path / "Calendar").mkdir(parents=True, exist_ok=True)
    templates_dir = vault_path / "Templates"
    templates_dir.mkdir(parents=True, exist_ok=True)

    for filename, content in DEFAULT_TEMPLATES.items():
        tp_file = templates_dir / filename
        if not tp_file.exists():
            tp_file.write_text(content, encoding="utf-8")

    tasks_file = vault_path / "Tasks.md"
    if not tasks_file.exists():
        tasks_file.write_text(
            "# Tasks\n\n"
            "## Active Dashboard\n"
            "```tasks\n"
            "not done\n"
            "path does not include ISA_Inbox\n"
            "path does not include Archive\n"
            "path does not include Briefings\n"
            "path does not include Reports\n"
            "path does not include Templates\n"
            "description regex does not match /^$/\n"
            "sort by priority\n"
            "sort by due\n"
            "```\n\n"
            "## Task List\n\n",
            encoding="utf-8",
        )

    inbox_file = vault_path / "ISA_Inbox.md"
    if not inbox_file.exists():
        inbox_file.write_text(
            "# ISA Inbox\n\n"
            "Type commands below starting with `@ISA` and press `Enter`.\n",
            encoding="utf-8",
        )


def write_env_file(
    project_root: Path,
    vault_path: Path,
    api_key: str,
    llm_model: str = "gemma4:12b",
    llm_base_url: str = "http://127.0.0.1:11434/v1",
    obsidian_api_url: str = "https://127.0.0.1:27124",
) -> Path:
    """Write a complete .env configuration file for ISA."""
    env_path = project_root / ".env"
    content = (
        "# ============================================================================\n"
        "# ISA (Intelligent Secretary Assistant) - Environment Configuration\n"
        "# ============================================================================\n\n"
        "# --- Air-Gapped / Offline Enforcement ---\n"
        "OLLAMA_NO_CLOUD=1\n"
        "OLLAMA_NO_UPDATE_CHECK=1\n"
        "OLLAMA_HOST=127.0.0.1:11434\n"
        "COPILOT_OFFLINE=true\n"
        "OPENCODE_AIRGAPPED=1\n"
        "HF_HUB_OFFLINE=1\n"
        "PYDANTIC_AI_NO_BANNER=1\n\n"
        "# --- Local LLM Inference (Ollama / OpenAI-compatible) ---\n"
        f"ISA_LLM_BASE_URL={llm_base_url}\n"
        f"ISA_LLM_MODEL={llm_model}\n"
        "ISA_LLM_NUM_CTX=8192\n\n"
        "# --- Obsidian Local REST API ---\n"
        f"OBSIDIAN_API_URL={obsidian_api_url}\n"
        f"OBSIDIAN_API_KEY={api_key}\n"
        "OBSIDIAN_VERIFY_SSL=false\n\n"
        "# --- Vault & File Monitoring (Watchdog) ---\n"
        f'OBSIDIAN_VAULT_PATH="{vault_path}"\n'
        "ISA_INBOX_FILENAME=ISA_Inbox.md\n"
        "ISA_ACTIVE_TASKS_FILENAME=Tasks.md\n"
        "ISA_CALENDAR_FOLDER=Calendar\n"
        "ISA_DEBOUNCE_SECONDS=1.0\n\n"
        "# --- Semantic Memory (LanceDB & Local Embeddings) ---\n"
        "LANCEDB_URI=./data/lancedb\n"
        "ISA_EMBEDDING_MODEL=BAAI/bge-small-en-v1.5\n"
        "ISA_RRF_K=60\n"
    )
    env_path.write_text(content, encoding="utf-8")
    return env_path


def generate_launchd_plist(project_root: Path) -> Path:
    """Generate a machine-specific macOS LaunchAgent plist with resolved absolute paths."""
    plist_path = project_root / "config" / "com.isa.assistant.plist"
    plist_path.parent.mkdir(parents=True, exist_ok=True)
    start_script = project_root / "start_isa.sh"
    out_log = project_root / "data" / "isa_daemon.out.log"
    err_log = project_root / "data" / "isa_daemon.err.log"

    xml_content = f"""<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
    <key>Label</key>
    <string>com.isa.assistant</string>
    <key>ProgramArguments</key>
    <array>
        <string>{start_script}</string>
    </array>
    <key>WorkingDirectory</key>
    <string>{project_root}</string>
    <key>EnvironmentVariables</key>
    <dict>
        <key>OLLAMA_NO_CLOUD</key>
        <string>1</string>
        <key>OLLAMA_NO_UPDATE_CHECK</key>
        <string>1</string>
        <key>OLLAMA_HOST</key>
        <string>127.0.0.1:11434</string>
        <key>COPILOT_OFFLINE</key>
        <string>true</string>
        <key>OPENCODE_AIRGAPPED</key>
        <string>1</string>
        <key>HF_HUB_OFFLINE</key>
        <string>1</string>
        <key>PYDANTIC_AI_NO_BANNER</key>
        <string>1</string>
    </dict>
    <key>RunAtLoad</key>
    <true/>
    <key>KeepAlive</key>
    <true/>
    <key>StandardOutPath</key>
    <string>{out_log}</string>
    <key>StandardErrorPath</key>
    <string>{err_log}</string>
</dict>
</plist>
"""
    plist_path.write_text(xml_content, encoding="utf-8")
    return plist_path


def run_interactive_tui(non_interactive: bool = False, vault_arg: str | None = None) -> int:
    """Execute the Rich TUI installation wizard."""
    project_root = Path(__file__).resolve().parent.parent

    banner = Text.from_markup(
        "[bold cyan]ISA — Intelligent Secretary Assistant[/bold cyan]\n"
        "[dim]Air-Gapped Local Obsidian + Gemma 4 12B + LanceDB Hybrid RAG Setup Wizard[/dim]"
    )
    console.print(Panel(banner, border_style="cyan", expand=False))

    # --- Step 1: Host & Ollama Check ---
    console.print("\n[bold]Step 1/5: Checking Local Inference Environment[/bold]")
    ollama_bin = shutil.which("ollama")
    if ollama_bin:
        console.print(f"  [green]✓[/green] Found Ollama binary at [cyan]{ollama_bin}[/cyan]")
    else:
        console.print(
            "  [yellow]![/yellow] Ollama binary not found on PATH. Install from https://ollama.com before running inference."
        )

    llm_model = "gemma4:12b"
    if not non_interactive:
        llm_model = Prompt.ask("  Local Ollama model tag", default="gemma4:12b")

    if ollama_bin and not non_interactive:
        try:
            res = subprocess.run(
                [ollama_bin, "list"],
                capture_output=True,
                text=True,
                timeout=5,
                check=False,
            )
            if llm_model not in res.stdout:
                if Confirm.ask(
                    f"  Model [cyan]{llm_model}[/cyan] is not yet pulled in Ollama. Pull it now?",
                    default=False,
                ):
                    subprocess.run([ollama_bin, "pull", llm_model], check=False)
            else:
                console.print(f"  [green]✓[/green] Model [cyan]{llm_model}[/cyan] is available locally.")
        except Exception:
            pass

    # --- Step 2: Obsidian Vault Selection ---
    console.print("\n[bold]Step 2/5: Selecting Obsidian Vault[/bold]")
    discovered = discover_obsidian_vaults()
    selected_vault: Path | None = None

    if vault_arg:
        selected_vault = Path(vault_arg).expanduser().resolve()
    elif discovered:
        table = Table(title="Detected Obsidian Vaults", show_header=True, header_style="bold magenta")
        table.add_column("#", style="cyan", width=4)
        table.add_column("Vault Path", style="green")
        for idx, vpath in enumerate(discovered, start=1):
            table.add_row(str(idx), str(vpath))
        console.print(table)

        if non_interactive:
            selected_vault = discovered[0]
        else:
            choice = Prompt.ask(
                "  Select vault number (or type a custom directory path)",
                default="1",
            )
            if choice.isdigit() and 1 <= int(choice) <= len(discovered):
                selected_vault = discovered[int(choice) - 1]
            else:
                selected_vault = Path(choice).expanduser().resolve()
    else:
        default_v = str(Path.home() / "Documents" / "Obsidian Vault")
        raw_path = (
            default_v
            if non_interactive
            else Prompt.ask("  Enter full path to your Obsidian Vault", default=default_v)
        )
        selected_vault = Path(raw_path).expanduser().resolve()

    selected_vault.mkdir(parents=True, exist_ok=True)
    console.print(f"  [green]✓[/green] Target Vault: [bold]{selected_vault}[/bold]")

    # --- Step 3: Obsidian Plugins, Templates & API Key ---
    console.print("\n[bold]Step 3/5: Provisioning Obsidian Plugins, Templates & REST API Key[/bold]")
    do_plugins = True if non_interactive else Confirm.ask(
        "  Download & pre-configure Obsidian plugins (Local REST API, Full Calendar, Tasks, Templater)?",
        default=True,
    )
    if do_plugins:
        try:
            install_obsidian_plugins(selected_vault)
        except Exception as exc:
            console.print(f"  [yellow]![/yellow] Plugin download skipped/offline: {exc}")

    try:
        scaffold_vault_files(selected_vault)
        console.print("  [green]✓[/green] Scaffolded [cyan]Calendar/[/cyan], [cyan]Templates/[/cyan], [cyan]Tasks.md[/cyan], and [cyan]ISA_Inbox.md[/cyan].")
    except OSError as exc:
        console.print(f"  [yellow]![/yellow] Vault files already present or read-only ({exc}).")

    api_key = resolve_or_generate_obsidian_api_key(selected_vault, project_root=project_root)
    env_file = write_env_file(
        project_root=project_root,
        vault_path=selected_vault,
        api_key=api_key,
        llm_model=llm_model,
    )
    console.print(f"  [green]✓[/green] Configured [cyan]{env_file}[/cyan] with Local REST API key.")

    # --- Step 4: Pre-Cache Local Embedding Model & Index Vault ---
    console.print("\n[bold]Step 4/5: Caching Offline Embedding Model & Indexing Vault into LanceDB[/bold]")
    # Temporarily allow download if model cache is empty
    models_cache = project_root / "data" / "models"
    if not models_cache.exists() or not any(models_cache.iterdir()):
        os.environ.pop("HF_HUB_OFFLINE", None)

    from config.settings import ISASettings
    from src.memory.vector_db import ingest_vault_to_lancedb

    settings = ISASettings(
        OBSIDIAN_VAULT_PATH=selected_vault,
        OBSIDIAN_API_KEY=api_key,
        ISA_LLM_MODEL=llm_model,
    )
    chunk_count = ingest_vault_to_lancedb(settings=settings)
    console.print(
        f"  [green]✓[/green] Cached [cyan]{settings.embedding_model}[/cyan] and indexed [bold]{chunk_count}[/bold] chunks into LanceDB."
    )

    # --- Step 5: Background Service Generation ---
    console.print("\n[bold]Step 5/5: Background Service Configuration[/bold]")
    plist_path = generate_launchd_plist(project_root)
    console.print(f"  [green]✓[/green] Generated LaunchAgent plist at [cyan]{plist_path}[/cyan]")

    if platform.system() == "Darwin" and not non_interactive:
        if Confirm.ask(
            "  Install & load macOS LaunchAgent so ISA starts automatically at login?",
            default=False,
        ):
            dest = Path.home() / "Library" / "LaunchAgents" / "com.isa.assistant.plist"
            dest.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(plist_path, dest)
            subprocess.run(["launchctl", "unload", str(dest)], capture_output=True, check=False)
            subprocess.run(["launchctl", "load", str(dest)], check=False)
            console.print(f"  [green]✓[/green] Loaded [cyan]{dest}[/cyan] via launchctl.")

    # --- Final Summary Table ---
    summary = Table(title="ISA Installation Summary", show_header=True, header_style="bold cyan")
    summary.add_column("Component", style="bold")
    summary.add_column("Status / Location", style="green")
    summary.add_row("Obsidian Vault", str(selected_vault))
    summary.add_row("Inbox File", str(selected_vault / "ISA_Inbox.md"))
    summary.add_row("Environment Config", str(env_file))
    summary.add_row("Local LLM", f"{llm_model} (127.0.0.1:11434)")
    summary.add_row("LanceDB Memory", f"{chunk_count} Markdown chunks indexed")
    summary.add_row("Start Command", "./start_isa.sh")
    console.print("\n", summary)
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description="ISA Interactive Rich TUI Installer")
    parser.add_argument(
        "--non-interactive",
        action="store_true",
        help="Run installation automatically using detected defaults without prompting.",
    )
    parser.add_argument(
        "--vault",
        type=str,
        default=None,
        help="Explicit path to the Obsidian vault directory.",
    )
    args = parser.parse_args()
    return run_interactive_tui(non_interactive=args.non_interactive, vault_arg=args.vault)


if __name__ == "__main__":
    raise SystemExit(main())
