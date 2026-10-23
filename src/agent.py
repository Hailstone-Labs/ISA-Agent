"""PydanticAI ReAct agent connected to the local air-gapped Gemma 4 12B model."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from datetime import datetime
import logging
import re
from typing import Literal
from urllib.parse import urlparse
from pydantic import ValidationError
from pydantic_ai import Agent, ModelRetry, RunContext
from pydantic_ai.models.openai import OpenAIChatModel
from pydantic_ai.providers.openai import OpenAIProvider
from pydantic_ai.settings import ModelSettings
from config.settings import ISASettings, get_settings
from src.memory.context_window import RollingContextWindow
from src.memory.ingest import EmbedderProtocol, LocalEmbedder
from src.memory.vector_db import LocalVectorStore, ingest_vault_to_lancedb
from src.monitor.observer import (
    CURSOR_PLACEHOLDER,
    FAST_COMMANDS,
    InboxEventHandler,
    RESPONSE_END_MARKER,
    THINKING_START_MARKER,
)
from src.tools.calendar import (
    CalendarEventSpec,
    cancel_vault_calendar_event,
    create_vault_calendar_event,
    list_vault_calendar_events,
)
from src.tools.documents import (
    SynthesizedDocumentSpec,
    create_synthesized_document,
    instantiate_template_note,
)
from src.tools.notifications import send_macos_notification
from src.tools.obsidian import LOOPBACK_HOSTS, ObsidianRestClient
from src.tools.tasks import TaskSpec, append_vault_task, list_vault_tasks

logger = logging.getLogger("isa.agent")

THINKING_END_MARKER = "<!-- ISA_THINKING_END -->"
THINKING_BLOCK_REGEX = re.compile(
    r"\n*" + re.escape(THINKING_START_MARKER) + r".*?" + re.escape(THINKING_END_MARKER) + r"\n*",
    re.DOTALL,
)


@dataclass
class ISADependencies:
    """Runtime dependencies injected into PydanticAI tool calls."""

    settings: ISASettings
    obsidian: ObsidianRestClient
    vector_store: LocalVectorStore | None = None
    embedder: EmbedderProtocol | None = None
    context_window: RollingContextWindow | None = None
    handler: InboxEventHandler | None = None
    active_command: str = ""
    inbox_lock: asyncio.Lock = field(default_factory=asyncio.Lock)
    turn_tool_context: list[str] = field(default_factory=list)


def _sync_vault_index_immediately(deps: ISADependencies) -> int:
    """Re-index vault Markdown files into LanceDB so newly created/deleted notes are immediately searchable."""
    try:
        return ingest_vault_to_lancedb(
            settings=deps.settings,
            embedder=deps.embedder,
            vector_store=deps.vector_store,
        )
    except Exception as exc:
        logger.debug("Immediate LanceDB sync warning: %s", exc)
        return 0


def build_local_model(settings: ISASettings | None = None) -> OpenAIChatModel:
    """Create an OpenAI-compatible local model pointing strictly to loopback Ollama."""
    cfg = settings or get_settings()
    parsed = urlparse(cfg.llm_base_url)
    if parsed.hostname not in LOOPBACK_HOSTS:
        raise ValueError(
            f"Air-gap violation: LLM base URL host '{parsed.hostname}' must be loopback."
        )
    provider = OpenAIProvider(base_url=cfg.llm_base_url, api_key="ollama-local")
    return OpenAIChatModel(model_name=cfg.llm_model, provider=provider)


def format_thinking_block(
    command: str,
    status_line: str = "Analyzing request with local Gemma 4 12B...",
) -> str:
    """Format an in-place 'ISA is thinking...' callout block for Obsidian."""
    ts = datetime.now().strftime("%H:%M:%S")
    preview = command.replace("\n", " ").strip()
    if len(preview) > 80:
        preview = preview[:77] + "..."
    return (
        f"\n\n{THINKING_START_MARKER}\n"
        f"> [!WARNING] ⏳ ISA is thinking... ({ts})\n"
        f"> **Prompt:** *{preview}*\n"
        f"> **Status:** {status_line}\n"
        f"{THINKING_END_MARKER}\n"
    )


async def _update_thinking_status(deps: ISADependencies, status_line: str) -> None:
    """Update the live thinking indicator inside ISA_Inbox.md with an async lock to prevent races."""
    async with deps.inbox_lock:
        try:
            current = await deps.obsidian.get_note(deps.settings.inbox_filename)
            new_block = format_thinking_block(deps.active_command, status_line)
            if THINKING_START_MARKER in current:
                updated = THINKING_BLOCK_REGEX.sub(new_block, current)
                await deps.obsidian.create_or_overwrite_note(
                    deps.settings.inbox_filename, updated
                )
            else:
                await deps.obsidian.append_to_note(
                    deps.settings.inbox_filename, new_block
                )
            if deps.handler is not None:
                deps.handler.record_agent_write(dispatched_command=deps.active_command)
        except Exception as exc:
            logger.debug("Could not update thinking block: %s", exc)


def build_isa_agent(settings: ISASettings | None = None) -> Agent[ISADependencies, str]:
    """Instantiate the PydanticAI ReAct agent with Hybrid RAG, Full Calendar, and Tasks tools."""
    cfg = settings or get_settings()
    model = build_local_model(cfg)
    agent: Agent[ISADependencies, str] = Agent(
        model=model,
        deps_type=ISADependencies,
        model_settings=ModelSettings(temperature=0.1),
        system_prompt=(
            "You are ISA (Intelligent Secretary Assistant), an air-gapped executive assistant "
            "integrated directly into a local Obsidian vault. Provide concise, accurate, "
            "well-structured Markdown responses.\n"
            "IMPORTANT TOOL RULES:\n"
            "1. When asked to schedule a meeting, appointment, or calendar event, ALWAYS call "
            "`create_calendar_event` with 24-hour HH:MM times (e.g. '10:00', '14:30') and "
            "resolved YYYY-MM-DD dates computed from the Current System Time.\n"
            "2. When asked what is on the schedule or calendar, call `list_calendar_events`.\n"
            "3. When asked to cancel, remove, or delete a calendar meeting or appointment, call `cancel_calendar_event`.\n"
            "4. When asked what tasks, to-dos, or items exist, call `list_tasks`.\n"
            "5. When asked to add or create a task, reminder, or to-do item, call `create_task` "
            "with resolved YYYY-MM-DD dates and valid Obsidian Tasks recurrence rules starting "
            "with 'every ' (e.g. 'every week on Friday', 'every weekday').\n"
            "6. When asked to create a standardized note from a template (e.g. 'Daily Standup Template', "
            "'Meeting Minutes Template', 'Project Brief Template'), call `create_note_from_template`.\n"
            "7. When asked to draft, synthesize, or save a new standalone report/note into the vault, "
            "call `create_document`.\n"
            "8. When asked to manually rescan, refresh, or re-index the vault database, call `rescan_vault_database`.\n"
            "9. Call `search_vault` or `read_vault_note` ONLY when the user asks about existing "
            "notes, files, projects, or past records in their vault.\n"
            "10. Never call `search_vault` more than once or twice per request."
        ),
    )

    @agent.system_prompt
    def inject_temporal_context() -> str:
        now_str = datetime.now().strftime("%A, %B %d, %Y, %I:%M %p")
        return f"Current System Time: {now_str}"

    @agent.system_prompt
    def inject_rolling_context(ctx: RunContext[ISADependencies]) -> str:
        if ctx.deps.context_window is None:
            return ""
        return ctx.deps.context_window.format_for_system_prompt()

    @agent.tool
    async def search_vault(ctx: RunContext[ISADependencies], query: str) -> str:
        """Execute Hybrid RAG search (LanceDB Dense Vectors + BM25 + RRF) across the Obsidian vault."""
        await _update_thinking_status(
            ctx.deps, f"🔍 Searching vault memory (Hybrid RRF) for `{query}`..."
        )
        store = ctx.deps.vector_store or LocalVectorStore(ctx.deps.settings)
        embedder = ctx.deps.embedder or LocalEmbedder(ctx.deps.settings)

        hybrid_hits = store.hybrid_search(
            query=query,
            embedder=embedder,
            limit=5,
            rrf_k=ctx.deps.settings.rrf_k,
        )
        if hybrid_hits:
            formatted_chunks: list[str] = []
            for hit in hybrid_hits:
                formatted_chunks.append(
                    f"### File: `{hit['file_path']}` (Section: `{hit['heading_hierarchy']}`, RRF: {hit['rrf_score']:.4f})\n"
                    f"{hit['text']}"
                )
                ctx.deps.turn_tool_context.append(
                    f"Found `{hit['file_path']}` ({hit['heading_hierarchy']}): {hit['text'][:120]}"
                )
            return "\n\n---\n\n".join(formatted_chunks)

        results = await ctx.deps.obsidian.search_simple(query)
        inbox_name = ctx.deps.settings.inbox_filename
        filtered = [r for r in results if r.get("filename") != inbox_name]
        if not filtered:
            return f"No vault notes matched '{query}'. Proceed by answering directly."
        formatted: list[str] = []
        for item in filtered[:5]:
            filename = item.get("filename", "Unknown")
            matches = item.get("matches", [])
            snippets = [
                m.get("context", "").strip() for m in matches[:2] if m.get("context")
            ]
            formatted.append(f"- **{filename}**: {' ... '.join(snippets)}")
            ctx.deps.turn_tool_context.append(
                f"Matched note `{filename}`: {' ... '.join(snippets)[:120]}"
            )
        return "\n".join(formatted)

    @agent.tool
    async def read_vault_note(ctx: RunContext[ISADependencies], note_path: str) -> str:
        """Read the raw Markdown content of a specific note in the Obsidian vault."""
        await _update_thinking_status(
            ctx.deps, f"📄 Reading note `{note_path}`..."
        )
        try:
            content = await ctx.deps.obsidian.get_note(note_path)
            ctx.deps.turn_tool_context.append(
                f"Read `{note_path}` ({len(content)} chars): {content[:140]}"
            )
            return content
        except Exception as exc:
            return f"Error reading note '{note_path}': {exc}"

    @agent.tool
    async def list_calendar_events(
        ctx: RunContext[ISADependencies],
        date_filter: str | None = None,
    ) -> str:
        """List upcoming and scheduled calendar events recorded in Calendar/*.md.

        Args:
            date_filter: Optional date in YYYY-MM-DD format to filter events for a specific day.
        """
        await _update_thinking_status(
            ctx.deps, f"📅 Checking scheduled calendar events in `{ctx.deps.settings.calendar_folder}`..."
        )
        events = list_vault_calendar_events(
            vault_path=ctx.deps.settings.obsidian_vault_path,
            folder=ctx.deps.settings.calendar_folder,
            date_filter=date_filter,
        )
        if not events:
            filter_msg = f" for date {date_filter}" if date_filter else ""
            return f"No calendar events found{filter_msg}."

        lines = [f"Found {len(events)} calendar event(s):"]
        for ev in events:
            time_str = "All Day" if ev["all_day"] else f"{ev['start_time']}–{ev['end_time']}"
            item_desc = f"- **{ev['title']}** on {ev['date']} ({time_str}) [`{ev['file_path']}`]"
            lines.append(item_desc)
            ctx.deps.turn_tool_context.append(item_desc)
        return "\n".join(lines)

    @agent.tool
    async def cancel_calendar_event(
        ctx: RunContext[ISADependencies],
        title_query: str,
        date_filter: str | None = None,
    ) -> str:
        """Cancel and delete an Obsidian Full Calendar event note from the vault.

        Args:
            title_query: Title or keyword of the event to cancel.
            date_filter: Optional date in YYYY-MM-DD format to target a specific occurrence.
        """
        await _update_thinking_status(
            ctx.deps, f"🗑️ Canceling calendar event matching `{title_query}`..."
        )
        deleted = await cancel_vault_calendar_event(
            obsidian=ctx.deps.obsidian,
            vault_path=ctx.deps.settings.obsidian_vault_path,
            title_query=title_query,
            date_filter=date_filter,
            folder=ctx.deps.settings.calendar_folder,
            notify=True,
        )
        if not deleted:
            return f"No scheduled calendar event matched '{title_query}'."

        _sync_vault_index_immediately(ctx.deps)
        ctx.deps.turn_tool_context.append(f"Canceled calendar event: {', '.join(deleted)}")
        return (
            f"Successfully canceled and removed {len(deleted)} event(s) from the calendar:\n"
            + "\n".join(f"- `{p}`" for p in deleted)
        )

    @agent.tool
    async def create_calendar_event(
        ctx: RunContext[ISADependencies],
        title: str,
        date: str,
        start_time: str | None = None,
        end_time: str | None = None,
        end_date: str | None = None,
        all_day: bool = False,
        agenda_or_notes: str = "",
    ) -> str:
        """Create an Obsidian Full Calendar event note with YAML frontmatter.

        Args:
            title: Title of the event (e.g. 'Performance Review with Alice').
            date: Start date in YYYY-MM-DD format (e.g. '2026-10-22').
            start_time: 24-hour start time in HH:MM format (e.g. '10:00'). Required if all_day is False.
            end_time: 24-hour end time in HH:MM format (e.g. '11:00'). Required if all_day is False.
            end_date: End date in YYYY-MM-DD format (defaults to date).
            all_day: Set True only for full-day events without start/end times.
            agenda_or_notes: Optional Markdown agenda or notes for the event body.
        """
        try:
            spec = CalendarEventSpec(
                title=title,
                date=date,
                end_date=end_date,
                all_day=all_day,
                start_time=start_time,
                end_time=end_time,
                agenda_or_notes=agenda_or_notes,
            )
        except ValidationError as exc:
            raise ModelRetry(f"Calendar event validation failed: {exc}") from exc

        await _update_thinking_status(
            ctx.deps, f"📅 Creating calendar event `{spec.title}` on {spec.date}..."
        )
        created_path = await create_vault_calendar_event(
            obsidian=ctx.deps.obsidian,
            event=spec,
            folder=ctx.deps.settings.calendar_folder,
        )
        _sync_vault_index_immediately(ctx.deps)
        ctx.deps.turn_tool_context.append(f"Created event `{spec.title}` on {spec.date} at `{created_path}`")
        return (
            f"Created Obsidian Full Calendar event at `{created_path}`:\n"
            f"```yaml\n{spec.to_markdown().strip()}\n```"
        )

    @agent.tool
    async def list_tasks(
        ctx: RunContext[ISADependencies],
        include_completed: bool = False,
    ) -> str:
        """List active tasks currently recorded in Tasks.md without adding duplicates.

        Args:
            include_completed: Set to True to also display completed [x] tasks.
        """
        await _update_thinking_status(
            ctx.deps, f"📋 Reading tasks from `{ctx.deps.settings.active_tasks_filename}`..."
        )
        tasks = await list_vault_tasks(
            obsidian=ctx.deps.obsidian,
            tasks_filename=ctx.deps.settings.active_tasks_filename,
            include_completed=include_completed,
        )
        if not tasks:
            return f"No open tasks found in `{ctx.deps.settings.active_tasks_filename}`."

        ctx.deps.turn_tool_context.append(f"Listed {len(tasks)} tasks: {'; '.join(tasks[:4])}")
        return (
            f"Active tasks in `{ctx.deps.settings.active_tasks_filename}` ({len(tasks)} items):\n"
            + "\n".join(tasks)
        )

    @agent.tool
    async def create_task(
        ctx: RunContext[ISADependencies],
        description: str,
        due_date: str | None = None,
        scheduled_date: str | None = None,
        start_date: str | None = None,
        recurrence: str | None = None,
        priority: Literal["high", "medium", "low", "normal"] = "normal",
        tags: list[str] | None = None,
    ) -> str:
        """Append a task formatted with Obsidian Tasks emojis (📅, ⏳, 🛫, 🔁, ⏫/🔼/🔽) to Tasks.md.

        Args:
            description: Actionable task description (without emojis or checkbox prefix).
            due_date: Optional due date in YYYY-MM-DD format (📅).
            scheduled_date: Optional scheduled date in YYYY-MM-DD format (⏳).
            start_date: Optional start date in YYYY-MM-DD format (🛫).
            recurrence: Optional recurrence rule starting with 'every ' (e.g. 'every week on Friday') (🔁).
            priority: Task priority ('high' -> ⏫, 'medium' -> 🔼, 'low' -> 🔽, 'normal' -> none).
            tags: Optional list of hashtags (e.g. ['#finance', '#work']).
        """
        try:
            spec = TaskSpec(
                description=description,
                due_date=due_date,
                scheduled_date=scheduled_date,
                start_date=start_date,
                recurrence=recurrence,
                priority=priority,
                tags=tags or [],
            )
        except ValidationError as exc:
            raise ModelRetry(f"Task validation failed: {exc}") from exc

        await _update_thinking_status(
            ctx.deps,
            f"✅ Adding task `{spec.description}` to `{ctx.deps.settings.active_tasks_filename}`...",
        )
        task_line, created_new = await append_vault_task(
            obsidian=ctx.deps.obsidian,
            task=spec,
            tasks_filename=ctx.deps.settings.active_tasks_filename,
        )
        if not created_new:
            ctx.deps.turn_tool_context.append(f"Task already exists on list: {task_line}")
            return (
                f"Task is already recorded on your active task list in `{ctx.deps.settings.active_tasks_filename}`:\n"
                f"`{task_line}`"
            )

        ctx.deps.turn_tool_context.append(f"Created task: {task_line}")
        return (
            f"Appended task to `{ctx.deps.settings.active_tasks_filename}`:\n"
            f"`{task_line}`"
        )

    @agent.tool
    async def rescan_vault_database(ctx: RunContext[ISADependencies]) -> str:
        """Trigger an instant rescan and re-indexing of all vault Markdown notes into LanceDB."""
        await _update_thinking_status(
            ctx.deps, "🔄 Rescanning and re-indexing vault memory into LanceDB..."
        )
        count = _sync_vault_index_immediately(ctx.deps)
        send_macos_notification(
            title="ISA Database Rescanned",
            subtitle=f"{count} chunks indexed",
            message="LanceDB Hybrid memory updated successfully.",
        )
        ctx.deps.turn_tool_context.append(f"Rescanned database ({count} chunks indexed)")
        return f"Vault database successfully rescanned and updated ({count} chunks indexed in LanceDB)."

    @agent.tool
    async def create_document(
        ctx: RunContext[ISADependencies],
        target_path: str,
        content: str,
    ) -> str:
        """Create or overwrite a standalone Markdown note or synthesized report in the vault.

        Args:
            target_path: Vault-relative path for the new note (e.g. 'Reports/Executive Summary.md').
            content: Full Markdown content (with optional valid YAML frontmatter).
        """
        try:
            spec = SynthesizedDocumentSpec(target_path=target_path, content=content)
        except (ValidationError, ValueError) as exc:
            raise ModelRetry(f"Document guardrail validation failed: {exc}") from exc

        await _update_thinking_status(
            ctx.deps, f"📝 Writing synthesized note `{spec.target_path}`..."
        )
        saved_path = await create_synthesized_document(
            obsidian=ctx.deps.obsidian,
            spec=spec,
        )
        _sync_vault_index_immediately(ctx.deps)
        ctx.deps.turn_tool_context.append(f"Created document `{saved_path}`")
        return f"Successfully created vault document at `{saved_path}`."

    @agent.tool
    async def create_note_from_template(
        ctx: RunContext[ISADependencies],
        template_name: str,
        target_path: str,
        extra_body: str = "",
    ) -> str:
        """Instantiate a standardized note using an Obsidian Templater template.

        Available templates in `Templates/`:
        - 'Daily Standup Template'
        - 'Meeting Minutes Template'
        - 'Project Brief Template'

        Args:
            template_name: Name of the template in `Templates/`.
            target_path: Vault-relative path for the new note (e.g. 'Standups/2026-10-21 Standup.md').
            extra_body: Optional additional Markdown content to append below the template.
        """
        await _update_thinking_status(
            ctx.deps,
            f"📋 Instantiating `{target_path}` from template `{template_name}`...",
        )
        try:
            saved_path = await instantiate_template_note(
                obsidian=ctx.deps.obsidian,
                vault_root=ctx.deps.settings.obsidian_vault_path,
                template_name=template_name,
                target_path=target_path,
                extra_body=extra_body,
            )
        except ValueError as exc:
            raise ModelRetry(f"Template instantiation failed: {exc}") from exc
        _sync_vault_index_immediately(ctx.deps)
        ctx.deps.turn_tool_context.append(f"Instantiated `{saved_path}` from template `{template_name}`")
        return (
            f"Successfully instantiated `{saved_path}` from template `{template_name}`."
        )

    return agent

    return agent


def format_inbox_response(
    response_text: str,
    timestamp: datetime | None = None,
    include_next_prompt_stub: bool = True,
) -> str:
    """Format an ISA response block with strict Obsidian callout separation and a ready-to-type prompt stub.

    Leaves an explicit blank line before and after the blockquote callout and horizontal rule
    so Obsidian's Live Preview CodeMirror 6 engine fully renders and collapses the callout.
    Appends an empty '@ISA ' line with a Templater cursor tag well below the response.
    """
    ts = (timestamp or datetime.now()).strftime("%Y-%m-%d %H:%M:%S")
    cleaned = response_text.strip()
    blockquote_lines = "\n".join(
        f"> {line}" if line else ">" for line in cleaned.splitlines()
    )

    stub = f"\n\n---\n\n@ISA {CURSOR_PLACEHOLDER}\n" if include_next_prompt_stub else "\n"
    return (
        f"\n\n> [!NOTE] ISA Response ({ts})\n"
        f"{blockquote_lines}\n\n"
        f"{RESPONSE_END_MARKER}"
        f"{stub}"
    )


async def dispatch_inbox_command(
    command: str,
    deps: ISADependencies,
    handler: InboxEventHandler | None = None,
    agent: Agent[ISADependencies, str] | None = None,
) -> str:
    """Display a live thinking banner, run the PydanticAI ReAct loop, and replace with the final reply."""
    deps.handler = handler
    deps.active_command = command
    deps.turn_tool_context.clear()
    if handler is not None:
        handler.mark_command_dispatched(command)
    if deps.context_window is None:
        deps.context_window = RollingContextWindow(
            max_entries=deps.settings.context_window_size,
            persist_path=deps.settings.context_window_path,
        )

    # Fast-path for deterministic commands like /rescan
    clean_cmd = command.strip().lower()
    if clean_cmd in FAST_COMMANDS:
        logger.info("Executing fast-path command: %s", clean_cmd)
        count = _sync_vault_index_immediately(deps)
        reply_text = (
            f"🔄 **Vault Database Rescanned Successfully**\n\n"
            f"- **Chunks Indexed:** {count}\n"
            f"- **Store:** LanceDB (`{deps.settings.lancedb_uri}`)\n"
            f"- **Timestamp:** {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}"
        )
        send_macos_notification(
            title="ISA Database Rescanned",
            subtitle=f"{count} chunks indexed",
            message="LanceDB Hybrid memory updated successfully.",
        )
        formatted_block = format_inbox_response(reply_text, include_next_prompt_stub=True)
        deps.context_window.add_entry(
            user_prompt=command,
            assistant_response=reply_text,
            tool_context=[f"Fast-path /rescan: indexed {count} chunks"],
        )
        async with deps.inbox_lock:
            current = await deps.obsidian.get_note(deps.settings.inbox_filename)
            if THINKING_START_MARKER in current:
                cleaned_current = THINKING_BLOCK_REGEX.sub("", current).rstrip()
                updated = f"{cleaned_current}{formatted_block}"
                await deps.obsidian.create_or_overwrite_note(deps.settings.inbox_filename, updated)
            else:
                await deps.obsidian.append_to_note(deps.settings.inbox_filename, formatted_block)

            try:
                await deps.obsidian.execute_command("templater-obsidian:jump-to-next-cursor-location")
            except Exception:
                pass
            if handler is not None:
                handler.record_agent_write(dispatched_command=command)
        return reply_text

    active_agent = agent or build_isa_agent(deps.settings)

    # Reload rolling context window from disk (or seed from inbox only if JSON file does not exist yet)
    try:
        if (
            deps.context_window.persist_path
            and deps.context_window.persist_path.exists()
        ):
            deps.context_window.reload_from_disk()
        else:
            inbox_before = await deps.obsidian.get_note(deps.settings.inbox_filename)
            deps.context_window.sync_from_inbox_markdown(inbox_before)
    except Exception as exc:
        logger.debug("Could not sync rolling context: %s", exc)

    logger.info(
        "Dispatching @ISA command to %s (rolling context=%d/%d entries): %r",
        deps.settings.llm_model,
        len(deps.context_window),
        deps.context_window.max_entries,
        command,
    )

    await _update_thinking_status(deps, "🧠 Thinking with local Gemma 4 12B...")

    try:
        history_messages = deps.context_window.to_pydantic_messages()
        result = await active_agent.run(
            command,
            deps=deps,
            message_history=history_messages if history_messages else None,
        )
        reply_text = result.output
        formatted_block = format_inbox_response(reply_text, include_next_prompt_stub=True)
        deps.context_window.add_entry(
            user_prompt=command,
            assistant_response=reply_text,
            tool_context=list(deps.turn_tool_context),
        )
    except Exception as exc:
        logger.exception("Error during LLM execution: %s", exc)
        reply_text = f"Error processing request: `{exc}`"
        formatted_block = format_inbox_response(reply_text, include_next_prompt_stub=True)

    async with deps.inbox_lock:
        current = await deps.obsidian.get_note(deps.settings.inbox_filename)
        if THINKING_START_MARKER in current:
            cleaned_current = THINKING_BLOCK_REGEX.sub("", current).rstrip()
            updated = f"{cleaned_current}{formatted_block}"
            await deps.obsidian.create_or_overwrite_note(
                deps.settings.inbox_filename, updated
            )
        else:
            await deps.obsidian.append_to_note(
                deps.settings.inbox_filename, formatted_block
            )

        # Trigger Templater cursor jump so user cursor is automatically placed at the new @ISA prompt line
        try:
            await deps.obsidian.execute_command("templater-obsidian:jump-to-next-cursor-location")
        except Exception:
            pass

        if handler is not None:
            handler.record_agent_write(dispatched_command=command)

    logger.info(
        "Appended ISA response (%d chars) to %s",
        len(reply_text),
        deps.settings.inbox_filename,
    )
    return reply_text
