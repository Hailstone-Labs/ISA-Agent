"""Theming engine for ISA responses, status banners, persona styles, and Obsidian CSS."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
import json
import logging
from pathlib import Path
import re
from typing import Literal

logger = logging.getLogger("isa.theme")

ThemeName = Literal["default", "terminal", "cyberpunk"]


@dataclass(frozen=True)
class ThemeConfig:
    """Styling, headers, and status line transformations for an ISA theme."""

    name: ThemeName
    callout_response_tag: str
    callout_thinking_tag: str
    response_header_prefix: str
    thinking_header_prefix: str
    prompt_label: str
    status_label: str
    inline_badge: str
    system_prompt_addon: str

    def format_thinking_header(self, ts: str) -> str:
        return f"{self.thinking_header_prefix} ({ts})"

    def format_response_header(self, ts: str) -> str:
        return f"{self.response_header_prefix} ({ts})"

    def format_inline_header(self, cmd: str, ts: str) -> str:
        preview = cmd.replace("\n", " ").strip()
        if len(preview) > 60:
            preview = preview[:57] + "..."
        return f"{self.inline_badge} (`{preview}`) — {ts}"

    def translate_status_message(self, message: str) -> str:
        """Thematically re-flavor a standard status update message."""
        if self.name == "default":
            return message

        clean = message.strip()
        if self.name == "terminal":
            if "Searching vault memory" in clean:
                return f"[LANCE_RRF] Hybrid vector scan underway on query: {clean.split('for')[-1].strip()}"
            if "Reading note" in clean:
                return f"[FS_READ] Ingesting note buffer: {clean.split('`')[1] if '`' in clean else clean}"
            if "Checking scheduled calendar" in clean:
                return "[CAL_SCAN] Polling calendar sector records..."
            if "Creating calendar event" in clean:
                return f"[CAL_WRITE] Allocating calendar event block: {clean.split('`')[1] if '`' in clean else clean}"
            if "Reading tasks" in clean:
                return "[TASKS_SCAN] Reading active task table..."
            if "Adding task" in clean:
                return f"[TASKS_WRITE] Appending item to task queue: {clean.split('`')[1] if '`' in clean else clean}"
            if "Completing task" in clean:
                return f"[TASKS_ACK] Flagging completed item: {clean.split('`')[1] if '`' in clean else clean}"
            if "Generating Daily Executive Briefing" in clean:
                return "[BRIEF_GEN] Compiling executive state summary report..."
            if "Archiving older" in clean:
                return "[GC_FLUSH] Purging and compressing archived exchange logs..."
            if "Thinking with local" in clean:
                return "[CPU_EXEC] Local inference kernel engaged (gemma4:12b)..."
            return f"[EXEC] {clean}"

        if self.name == "cyberpunk":
            if "Searching vault memory" in clean:
                return f"⚡ JACKING IN... Decrypting Sub-Net ICE for: {clean.split('for')[-1].strip()}"
            if "Reading note" in clean:
                return f"💾 Kiroshi optics scanning shard: {clean.split('`')[1] if '`' in clean else clean}"
            if "Checking scheduled calendar" in clean:
                return "📅 Neural Link checking Night City schedule..."
            if "Creating calendar event" in clean:
                return f"⏱️ Syncing gig to trauma/planner feed: {clean.split('`')[1] if '`' in clean else clean}"
            if "Reading tasks" in clean:
                return "📋 Querying mercenary contracts and hit-list..."
            if "Adding task" in clean:
                return f"🦾 Chrome operational: locking new gig to Tasks: {clean.split('`')[1] if '`' in clean else clean}"
            if "Completing task" in clean:
                return f"💥 Flatlined gig confirmed: {clean.split('`')[1] if '`' in clean else clean}"
            if "Generating Daily Executive Briefing" in clean:
                return "🌅 Daily Night City wire briefing initializing..."
            if "Archiving older" in clean:
                return "🗄️ Scrubbing Netrunner trail into cold archive..."
            if "Thinking with local" in clean:
                return "🧠 Relic processor overclocked: analyzing local stream..."
            return f"⚡ {clean}"

        return message


THEMES: dict[ThemeName, ThemeConfig] = {
    "default": ThemeConfig(
        name="default",
        callout_response_tag="NOTE",
        callout_thinking_tag="WARNING",
        response_header_prefix="ISA Response",
        thinking_header_prefix="⏳ ISA is thinking...",
        prompt_label="**Prompt:**",
        status_label="**Status:**",
        inline_badge="> [!NOTE] 🤖 ISA",
        system_prompt_addon="",
    ),
    "terminal": ThemeConfig(
        name="terminal",
        callout_response_tag="isa-terminal",
        callout_thinking_tag="isa-terminal-thinking",
        response_header_prefix="📟 [SYS_OK] // EXECUTION COMPLETE",
        thinking_header_prefix="📟 [TTY1] // ISA_KERNEL v2.4",
        prompt_label="**EXEC_IN:**",
        status_label="**SYS_STATUS:**",
        inline_badge="> [!isa-terminal] 📟 ISA_EXEC",
        system_prompt_addon=(
            "\nTHEME VOICE DIRECTIVE (RETRO CRT TERMINAL):\n"
            "Adopt the crisp, concise voice of a Unix mainframe terminal daemon and systems operator. "
            "Use monospace formatting, system tags like `[OK]`, `[INFO]`, and avoid filler pleasantries. "
            "Remain 100% accurate on all task and calendar instructions."
        ),
    ),
    "cyberpunk": ThemeConfig(
        name="cyberpunk",
        callout_response_tag="isa-cyberpunk",
        callout_thinking_tag="isa-cyberpunk-thinking",
        response_header_prefix="⚡ [RELIC_LINK: ACTIVE] // NIGHT CITY INTEL FEED",
        thinking_header_prefix="⚡ [NETRUNNER ICE BREAKER] // RELIC HUD",
        prompt_label="**TARGET_INTEL:**",
        status_label="**ICE_BREAKER:**",
        inline_badge="> [!isa-cyberpunk] ⚡ CHROME_FEED",
        system_prompt_addon=(
            "\nTHEME VOICE DIRECTIVE (CYBERPUNK 2077 / NETRUNNER):\n"
            "Adopt the voice of a loyal, top-tier Night City netrunner assistant riding the user's neural link. "
            "Sprinkle in authentic Cyberpunk 2077 terminology (e.g. 'Choom', 'ICE', 'gigs', 'chrome', 'Night City', "
            "'shards', 'sub-net', 'eddies'), but keep the answers sharp, actionable, and completely helpful. "
            "Perform all tool operations, tasks, and scheduling with 100% precision."
        ),
    ),
}


def get_theme(theme_name: str | None) -> ThemeConfig:
    """Normalize and resolve a theme by name or alias."""
    if not theme_name:
        return THEMES["default"]
    norm = theme_name.strip().lower()
    if norm in {"terminal", "crt", "matrix", "retro", "hacker", "console"}:
        return THEMES["terminal"]
    if norm in {"cyberpunk", "cyber", "netrunner", "nightcity", "2077", "relic"}:
        return THEMES["cyberpunk"]
    return THEMES.get(norm, THEMES["default"])


OBSIDIAN_THEMES_CSS = """/* ==========================================================================
   ISA THEME ENGINE: RETRO CRT TERMINAL & CYBERPUNK 2077 FOR OBSIDIAN
   Styles both Reading View and Live Preview Editor (CodeMirror 6)
   ========================================================================== */

/* --------------------------------------------------------------------------
   1. RETRO CRT TERMINAL (Phosphor Green & Deep Pitch Black)
   -------------------------------------------------------------------------- */
:root {
    --callout-isa-terminal-color: 0, 255, 102;
    --callout-isa-terminal-icon: lucide-terminal;
    --callout-isa-terminal-thinking-color: 0, 255, 102;
    --callout-isa-terminal-thinking-icon: lucide-terminal;
}

.callout[data-callout="isa-terminal"],
.callout[data-callout="isa-terminal-thinking"],
.cm-embed-block .callout[data-callout="isa-terminal"],
.cm-embed-block .callout[data-callout="isa-terminal-thinking"],
.markdown-rendered .callout[data-callout="isa-terminal"],
.markdown-rendered .callout[data-callout="isa-terminal-thinking"] {
    --callout-color: 0, 255, 102 !important;
    --callout-icon: lucide-terminal !important;
    background-color: #030804 !important;
    background: radial-gradient(circle at 50% 50%, #051608 0%, #020703 100%) !important;
    color: #00ff66 !important;
    border: 1.5px solid #00ff66 !important;
    border-radius: 4px !important;
    box-shadow: 0 0 18px rgba(0, 255, 102, 0.42), inset 0 0 14px rgba(0, 255, 102, 0.15) !important;
    font-family: 'JetBrains Mono', 'Fira Code', 'SF Mono', 'Courier New', monospace !important;
    position: relative !important;
    overflow: hidden !important;
    margin: 1.2em 0 !important;
}

/* CRT Scanlines Overlay */
.callout[data-callout="isa-terminal"]::before,
.callout[data-callout="isa-terminal-thinking"]::before {
    content: " ";
    display: block !important;
    position: absolute !important;
    top: 0 !important; left: 0 !important; bottom: 0 !important; right: 0 !important;
    background: repeating-linear-gradient(
        0deg,
        rgba(0, 0, 0, 0.38) 0px,
        rgba(0, 0, 0, 0.38) 1px,
        transparent 1px,
        transparent 3px
    ) !important;
    z-index: 1 !important;
    pointer-events: none !important;
    opacity: 0.8 !important;
}

/* Terminal Title Bar */
.callout[data-callout="isa-terminal"] .callout-title,
.callout[data-callout="isa-terminal-thinking"] .callout-title,
.callout[data-callout="isa-terminal"] .callout-title-inner,
.callout[data-callout="isa-terminal-thinking"] .callout-title-inner {
    color: #39ff14 !important;
    font-family: 'JetBrains Mono', 'Fira Code', 'Courier New', monospace !important;
    font-weight: 800 !important;
    letter-spacing: 0.1em !important;
    text-transform: uppercase !important;
    text-shadow: 0 0 10px rgba(57, 255, 20, 0.9) !important;
    z-index: 2 !important;
    position: relative !important;
}

.callout[data-callout="isa-terminal"] .callout-title,
.callout[data-callout="isa-terminal-thinking"] .callout-title {
    border-bottom: 1px dashed rgba(0, 255, 102, 0.5) !important;
    padding-bottom: 8px !important;
    margin-bottom: 8px !important;
}

/* Terminal Icons */
.callout[data-callout="isa-terminal"] .callout-icon,
.callout[data-callout="isa-terminal-thinking"] .callout-icon,
.callout[data-callout="isa-terminal"] .callout-icon svg,
.callout[data-callout="isa-terminal-thinking"] .callout-icon svg {
    color: #39ff14 !important;
    stroke: #39ff14 !important;
    filter: drop-shadow(0 0 8px rgba(57, 255, 20, 0.95)) !important;
    z-index: 2 !important;
}

/* ALL Child text elements inside Terminal callout MUST be phosphor green! */
.callout[data-callout="isa-terminal"] .callout-content,
.callout[data-callout="isa-terminal-thinking"] .callout-content,
.callout[data-callout="isa-terminal"] .callout-content *,
.callout[data-callout="isa-terminal-thinking"] .callout-content * {
    color: #00ff66 !important;
    font-family: 'JetBrains Mono', 'Fira Code', 'SF Mono', 'Courier New', monospace !important;
    line-height: 1.6 !important;
    text-shadow: 0 0 5px rgba(0, 255, 102, 0.5) !important;
    z-index: 2 !important;
    position: relative !important;
}

.callout[data-callout="isa-terminal"] strong,
.callout[data-callout="isa-terminal-thinking"] strong {
    color: #70ff8b !important;
    text-shadow: 0 0 8px rgba(112, 255, 139, 0.9) !important;
    font-weight: 700 !important;
}

.callout[data-callout="isa-terminal"] code,
.callout[data-callout="isa-terminal-thinking"] code {
    background-color: #091e0c !important;
    color: #80ffaa !important;
    border: 1px solid rgba(0, 255, 102, 0.6) !important;
    border-radius: 2px !important;
    padding: 2px 6px !important;
    text-shadow: 0 0 6px rgba(0, 255, 102, 0.6) !important;
}

.callout[data-callout="isa-terminal"] a,
.callout[data-callout="isa-terminal-thinking"] a {
    color: #66ffb2 !important;
    text-decoration: underline !important;
}

/* --------------------------------------------------------------------------
   2. CYBERPUNK 2077 (Night City Netrunner Relic HUD)
   -------------------------------------------------------------------------- */
:root {
    --callout-isa-cyberpunk-color: 0, 240, 255;
    --callout-isa-cyberpunk-icon: lucide-zap;
    --callout-isa-cyberpunk-thinking-color: 252, 238, 10;
    --callout-isa-cyberpunk-thinking-icon: lucide-zap;
}

.callout[data-callout="isa-cyberpunk"],
.callout[data-callout="isa-cyberpunk-thinking"],
.cm-embed-block .callout[data-callout="isa-cyberpunk"],
.cm-embed-block .callout[data-callout="isa-cyberpunk-thinking"],
.markdown-rendered .callout[data-callout="isa-cyberpunk"],
.markdown-rendered .callout[data-callout="isa-cyberpunk-thinking"] {
    --callout-color: 0, 240, 255 !important;
    --callout-icon: lucide-zap !important;
    background-color: #0a0a13 !important;
    background: linear-gradient(135deg, #0f0f1c 0%, #06060c 100%) !important;
    color: #fcee0a !important;
    border-top: 2px solid #fcee0a !important;
    border-bottom: 2.5px solid #00f0ff !important;
    border-left: 6px solid #00f0ff !important;
    border-right: 3px solid #ff003c !important;
    border-radius: 0px !important;
    clip-path: polygon(0 0, calc(100% - 15px) 0, 100% 15px, 100% 100%, 15px 100%, 0 calc(100% - 15px)) !important;
    box-shadow: 0 0 24px rgba(0, 240, 255, 0.4), 0 0 14px rgba(255, 0, 60, 0.25), inset 0 0 22px rgba(255, 0, 60, 0.12) !important;
    font-family: 'JetBrains Mono', -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, sans-serif !important;
    position: relative !important;
    overflow: hidden !important;
    margin: 1.2em 0 !important;
}

/* Cyberpunk Holographic Grid Texture Overlay */
.callout[data-callout="isa-cyberpunk"]::before,
.callout[data-callout="isa-cyberpunk-thinking"]::before {
    content: " ";
    display: block !important;
    position: absolute !important;
    top: 0 !important; left: 0 !important; bottom: 0 !important; right: 0 !important;
    background: repeating-linear-gradient(
        0deg,
        rgba(0, 240, 255, 0.05) 0px,
        rgba(0, 240, 255, 0.05) 1px,
        transparent 1px,
        transparent 4px
    ) !important;
    z-index: 1 !important;
    pointer-events: none !important;
}

/* Cyberpunk Title Bar */
.callout[data-callout="isa-cyberpunk"] .callout-title,
.callout[data-callout="isa-cyberpunk-thinking"] .callout-title,
.callout[data-callout="isa-cyberpunk"] .callout-title-inner,
.callout[data-callout="isa-cyberpunk-thinking"] .callout-title-inner {
    color: #00f0ff !important;
    font-weight: 900 !important;
    letter-spacing: 0.15em !important;
    text-transform: uppercase !important;
    text-shadow: 0 0 12px rgba(0, 240, 255, 0.95), 0 0 22px rgba(0, 240, 255, 0.5) !important;
    z-index: 2 !important;
    position: relative !important;
}

.callout[data-callout="isa-cyberpunk"] .callout-title,
.callout[data-callout="isa-cyberpunk-thinking"] .callout-title {
    border-bottom: 1.5px solid rgba(252, 238, 10, 0.6) !important;
    padding-bottom: 8px !important;
    margin-bottom: 8px !important;
}

/* Cyberpunk Icons */
.callout[data-callout="isa-cyberpunk"] .callout-icon,
.callout[data-callout="isa-cyberpunk-thinking"] .callout-icon,
.callout[data-callout="isa-cyberpunk"] .callout-icon svg,
.callout[data-callout="isa-cyberpunk-thinking"] .callout-icon svg {
    color: #00f0ff !important;
    stroke: #00f0ff !important;
    filter: drop-shadow(0 0 10px rgba(0, 240, 255, 1)) !important;
    z-index: 2 !important;
}

/* ALL Child text elements inside Cyberpunk callout */
.callout[data-callout="isa-cyberpunk"] .callout-content,
.callout[data-callout="isa-cyberpunk-thinking"] .callout-content,
.callout[data-callout="isa-cyberpunk"] .callout-content *,
.callout[data-callout="isa-cyberpunk-thinking"] .callout-content * {
    color: #f0f4f8 !important;
    line-height: 1.65 !important;
    z-index: 2 !important;
    position: relative !important;
}

.callout[data-callout="isa-cyberpunk"] strong,
.callout[data-callout="isa-cyberpunk-thinking"] strong {
    color: #fcee0a !important;
    text-shadow: 0 0 10px rgba(252, 238, 10, 0.9) !important;
    font-weight: 800 !important;
}

.callout[data-callout="isa-cyberpunk"] em,
.callout[data-callout="isa-cyberpunk-thinking"] em {
    color: #ff003c !important;
    text-shadow: 0 0 8px rgba(255, 0, 60, 0.8) !important;
    font-style: italic !important;
}

.callout[data-callout="isa-cyberpunk"] code,
.callout[data-callout="isa-cyberpunk-thinking"] code {
    background-color: #17182b !important;
    color: #00f0ff !important;
    border: 1px solid rgba(0, 240, 255, 0.6) !important;
    border-radius: 2px !important;
    padding: 2px 6px !important;
    text-shadow: 0 0 8px rgba(0, 240, 255, 0.8) !important;
}

.callout[data-callout="isa-cyberpunk"] a,
.callout[data-callout="isa-cyberpunk-thinking"] a {
    color: #00f0ff !important;
    text-decoration: underline !important;
    text-decoration-color: #ff003c !important;
}
"""


def ensure_obsidian_themes_snippet(vault_root: Path) -> Path:
    """Write isa-themes.css to the vault's snippets folder and enable it in appearance.json."""
    obsidian_dir = vault_root / ".obsidian"
    snippets_dir = obsidian_dir / "snippets"
    snippets_dir.mkdir(parents=True, exist_ok=True)

    css_path = snippets_dir / "isa-themes.css"
    css_path.write_text(OBSIDIAN_THEMES_CSS, encoding="utf-8")

    # Update appearance.json to enable the snippet if not already listed
    appearance_path = obsidian_dir / "appearance.json"
    data: dict = {}
    if appearance_path.exists():
        try:
            data = json.loads(appearance_path.read_text(encoding="utf-8")) or {}
        except Exception:
            data = {}

    enabled = data.get("enabledCssSnippets", [])
    if "isa-themes" not in enabled:
        enabled.append("isa-themes")
        data["enabledCssSnippets"] = enabled
        appearance_path.write_text(json.dumps(data, indent=2), encoding="utf-8")
        logger.info("Enabled 'isa-themes' CSS snippet in %s", appearance_path)

    return css_path
