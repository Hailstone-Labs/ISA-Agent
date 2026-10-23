"""Main entry point for the Intelligent Secretary Assistant (ISA)."""

from __future__ import annotations

import argparse
import asyncio
from datetime import datetime
import fcntl
import logging
import os
import resource
import signal
import time
import httpx
from config.settings import enforce_airgap_env, get_settings
from src.agent import ISADependencies, build_isa_agent, dispatch_inbox_command
from src.memory.context_window import RollingContextWindow
from src.memory.ingest import LocalEmbedder
from src.memory.vector_db import LocalVectorStore, ingest_vault_to_lancedb
from src.monitor.observer import create_inbox_observer, extract_pending_command
from src.tools.notifications import ActiveReminderMonitor
from src.tools.obsidian import ObsidianRestClient

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
logger = logging.getLogger("isa")


async def verify_sprint1_environment(write_hello: bool = True) -> int:
    """Verify air-gap variables, LanceDB initialization, Obsidian REST API, and local LLM."""
    enforced = enforce_airgap_env()
    logger.info("1. Enforced air-gap environment controls: %s", enforced)

    settings = get_settings()
    logger.info(
        "2. Loaded Vault Path: %s (exists=%s)",
        settings.obsidian_vault_path,
        settings.obsidian_vault_path.exists(),
    )

    vector_store = LocalVectorStore(settings)
    logger.info(
        "3. Initialized embedded LanceDB at %s (tables: %s)",
        vector_store.uri,
        vector_store.list_tables(),
    )

    obsidian = ObsidianRestClient(settings)
    logger.info("4. Connecting to Obsidian Local REST API at %s ...", obsidian.base_url)
    status = await obsidian.health_check()
    logger.info(
        "   Obsidian REST API Status: status=%s, authenticated=%s, versions=%s",
        status.get("status"),
        status.get("authenticated"),
        status.get("versions"),
    )
    if not status.get("authenticated"):
        logger.error("Obsidian REST API reported authenticated=False. Check OBSIDIAN_API_KEY.")
        return 1

    if write_hello:
        timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        hello_msg = (
            f"\n- ✅ **[Sprint 1 Smoke Test - {timestamp}]** "
            "ISA connected via Obsidian Local REST API and verified air-gapped loopback communication.\n"
        )
        if not settings.inbox_path.exists():
            await obsidian.create_or_overwrite_note(
                settings.inbox_filename,
                f"# ISA Inbox\n\nType commands below using `@ISA`.\n{hello_msg}",
            )
        else:
            await obsidian.append_to_note(settings.inbox_filename, hello_msg)

        read_back = await obsidian.get_note(settings.inbox_filename)
        logger.info(
            "5. Successfully wrote and read back '%s' via REST API (%d bytes, disk exists=%s).",
            settings.inbox_filename,
            len(read_back),
            settings.inbox_path.exists(),
        )

    try:
        async with httpx.AsyncClient(timeout=5.0) as client:
            resp = await client.get(f"{settings.llm_base_url.rstrip('/')}/models")
            resp.raise_for_status()
            models_data = resp.json().get("data", [])
            model_ids = [m.get("id") for m in models_data]
            logger.info(
                "6. Local LLM server reachable at %s. Available models: %s",
                settings.llm_base_url,
                model_ids,
            )
    except Exception as exc:
        logger.warning("6. Local LLM server check at %s: %s", settings.llm_base_url, exc)

    return 0


def clear_rolling_memory() -> None:
    """Reset the persisted rolling context window JSON to a clean empty state."""
    settings = get_settings()
    window = RollingContextWindow(
        max_entries=settings.context_window_size,
        persist_path=settings.context_window_path,
    )
    window.clear()
    logger.info(
        "Cleared rolling context memory at %s (0/%d entries).",
        settings.context_window_path,
        settings.context_window_size,
    )


def run_ingestion() -> int:
    """Ingest all Markdown notes in the Obsidian vault into LanceDB."""
    enforce_airgap_env()
    settings = get_settings()
    count = ingest_vault_to_lancedb(settings=settings)
    logger.info("Vault ingestion complete (%d total chunks indexed).", count)
    return 0


def run_profile() -> int:
    """Sprint 6 profiling: measure vault ingestion, Hybrid RRF retrieval latency, and RSS memory."""
    enforce_airgap_env()
    settings = get_settings()
    store = LocalVectorStore(settings)
    embedder = LocalEmbedder(settings)

    t0 = time.perf_counter()
    chunk_count = ingest_vault_to_lancedb(
        settings=settings,
        embedder=embedder,
        vector_store=store,
    )
    ingest_ms = (time.perf_counter() - t0) * 1000.0

    t1 = time.perf_counter()
    hits = store.hybrid_search("blog standup meeting", embedder=embedder, limit=5)
    search_ms = (time.perf_counter() - t1) * 1000.0

    # On macOS, ru_maxrss is reported in bytes
    rss_mb = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss / (1024 * 1024)
    logger.info(
        "=== ISA Sprint 6 Performance Profile ===\n"
        "  • Vault Chunks Indexed : %d chunks\n"
        "  • Ingestion Latency    : %.1f ms\n"
        "  • Hybrid RRF Query     : %.2f ms (%d hits returned, k=%d)\n"
        "  • Peak Process Memory  : %.1f MB RSS\n"
        "  • LLM Context Window   : num_ctx=%d (%s)",
        chunk_count,
        ingest_ms,
        search_ms,
        len(hits),
        settings.rrf_k,
        rss_mb,
        settings.llm_num_ctx,
        settings.llm_model,
    )
    return 0


async def process_once() -> int:
    """Process a single pending @ISA command from ISA_Inbox.md and exit."""
    enforce_airgap_env()
    settings = get_settings()
    obsidian = ObsidianRestClient(settings)
    vector_store = LocalVectorStore(settings)
    embedder = LocalEmbedder(settings)
    context_window = RollingContextWindow(
        max_entries=settings.context_window_size,
        persist_path=settings.context_window_path,
    )
    deps = ISADependencies(
        settings=settings,
        obsidian=obsidian,
        vector_store=vector_store,
        embedder=embedder,
        context_window=context_window,
    )

    content = await obsidian.get_note(settings.inbox_filename)
    command = extract_pending_command(content, require_newline=False)
    if not command:
        logger.info("No unanswered @ISA command found in %s.", settings.inbox_filename)
        return 0

    await dispatch_inbox_command(command=command, deps=deps)
    return 0


async def run_watchdog_daemon(memory_cleared: bool = False) -> int:
    """Run the asynchronous watchdog event loop monitoring ISA_Inbox.md."""
    enforce_airgap_env()
    settings = get_settings()

    # Acquire exclusive single-instance lock so two daemons never run simultaneously
    lock_path = settings.lancedb_uri.parent / "isa_daemon.lock"
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    lock_file = lock_path.open("a+", encoding="utf-8")
    try:
        fcntl.flock(lock_file.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        lock_file.seek(0)
        existing_pid = lock_file.read().strip() or "unknown"
        if memory_cleared:
            logger.info(
                "Rolling context memory cleared on disk; active ISA daemon (PID=%s) will use the clean slate on the next turn.",
                existing_pid,
            )
            lock_file.close()
            return 0
        logger.error(
            "Another ISA watchdog daemon is already running (PID=%s). "
            "Exiting to prevent duplicate responses.",
            existing_pid,
        )
        lock_file.close()
        return 1

    lock_file.seek(0)
    lock_file.truncate()
    lock_file.write(f"{os.getpid()}\n")
    lock_file.flush()

    obsidian = ObsidianRestClient(settings)
    vector_store = LocalVectorStore(settings)
    embedder = LocalEmbedder(settings)

    # Index the vault on startup so Hybrid RAG is immediately ready
    ingest_vault_to_lancedb(
        settings=settings,
        embedder=embedder,
        vector_store=vector_store,
    )

    context_window = RollingContextWindow(
        max_entries=settings.context_window_size,
        persist_path=settings.context_window_path,
    )
    if (
        not memory_cleared
        and not settings.context_window_path.exists()
        and settings.inbox_path.exists()
    ):
        try:
            context_window.sync_from_inbox_markdown(
                settings.inbox_path.read_text(encoding="utf-8")
            )
        except OSError:
            pass
    logger.info(
        "Initialized rolling context window (%d/%d entries active at %s).",
        len(context_window),
        settings.context_window_size,
        settings.context_window_path,
    )

    deps = ISADependencies(
        settings=settings,
        obsidian=obsidian,
        vector_store=vector_store,
        embedder=embedder,
        context_window=context_window,
    )
    agent = build_isa_agent(settings)
    reminder_monitor = ActiveReminderMonitor(
        vault_path=settings.obsidian_vault_path,
        calendar_folder=settings.calendar_folder,
        tasks_filename=settings.active_tasks_filename,
    )

    async def periodic_reminder_loop() -> None:
        while not stop_event.is_set():
            try:
                reminder_monitor.check_and_notify()
            except Exception as exc:
                logger.debug("Reminder scan error: %s", exc)
            try:
                await asyncio.wait_for(stop_event.wait(), timeout=60.0)
            except TimeoutError:
                pass

    loop = asyncio.get_running_loop()
    command_queue: asyncio.Queue[str] = asyncio.Queue()
    stop_event = asyncio.Event()

    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(sig, stop_event.set)
        except NotImplementedError:
            pass

    def on_command_detected(cmd: str) -> None:
        loop.call_soon_threadsafe(command_queue.put_nowait, cmd)

    vault_reindex_lock = asyncio.Lock()

    def on_vault_file_changed(changed_path: Path) -> None:
        async def _debounced_reindex() -> None:
            await asyncio.sleep(2.0)
            elapsed = time.monotonic() - handler.last_vault_edit_time
            if elapsed < 1.8:
                return
            async with vault_reindex_lock:
                try:
                    count = ingest_vault_to_lancedb(
                        settings=settings,
                        embedder=embedder,
                        vector_store=vector_store,
                    )
                    logger.info("Real-time LanceDB sync completed (%d chunks indexed)", count)
                except Exception as exc:
                    logger.debug("Background LanceDB sync failed: %s", exc)

        loop.call_soon_threadsafe(asyncio.create_task, _debounced_reindex())

    observer, handler = create_inbox_observer(
        inbox_path=settings.inbox_path,
        on_user_command=on_command_detected,
        debounce_seconds=settings.debounce_seconds,
        require_newline=True,
        manual_trigger_only=settings.manual_trigger_only,
        on_vault_change=on_vault_file_changed,
    )
    observer.start()
    mode_desc = "MANUAL (Single Enter / newline; punctuation ignored)" if settings.manual_trigger_only else "AUTO (Punctuation [.!?] + Enter OR Enter twice)"
    logger.info(
        "Watchdog daemon monitoring %s (debounce=%.2fs, mode=%s). Waiting for @ISA commands...",
        settings.inbox_path,
        settings.debounce_seconds,
        mode_desc,
    )

    if settings.inbox_path.exists():
        initial_cmd = extract_pending_command(
            settings.inbox_path.read_text(encoding="utf-8"),
            require_newline=True,
            manual_trigger_only=settings.manual_trigger_only,
        )
        if initial_cmd:
            await command_queue.put(initial_cmd)

    reminder_task = asyncio.create_task(periodic_reminder_loop())
    try:
        while not stop_event.is_set():
            queue_task = asyncio.create_task(command_queue.get())
            stop_task = asyncio.create_task(stop_event.wait())
            done, pending = await asyncio.wait(
                {queue_task, stop_task},
                return_when=asyncio.FIRST_COMPLETED,
            )
            for task in pending:
                task.cancel()

            if queue_task in done:
                _ = queue_task.result()
                # True trailing-edge quiet window: wait until 2.5s have passed with zero file edits
                settle_window = max(settings.debounce_seconds, 2.5)
                while True:
                    elapsed = time.monotonic() - handler.last_user_edit_time
                    remaining = settle_window - elapsed
                    if remaining <= 0:
                        break
                    try:
                        _ = await asyncio.wait_for(
                            command_queue.get(), timeout=remaining
                        )
                    except TimeoutError:
                        pass

                # Drain any residual notifications accumulated during settle
                while not command_queue.empty():
                    try:
                        command_queue.get_nowait()
                    except asyncio.QueueEmpty:
                        break

                try:
                    latest_content = settings.inbox_path.read_text(encoding="utf-8")
                    cmd = extract_pending_command(
                        latest_content,
                        require_newline=True,
                        manual_trigger_only=settings.manual_trigger_only,
                    )
                    if cmd and handler.can_dispatch_command(cmd):
                        await dispatch_inbox_command(
                            command=cmd,
                            deps=deps,
                            handler=handler,
                            agent=agent,
                        )
                except Exception as exc:
                    logger.exception("Error processing @ISA command: %s", exc)
    finally:
        reminder_task.cancel()
        observer.stop()
        observer.join(timeout=5.0)
        try:
            fcntl.flock(lock_file.fileno(), fcntl.LOCK_UN)
            lock_file.close()
        except Exception:
            pass
        logger.info("Watchdog daemon stopped cleanly.")

    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description="Intelligent Secretary Assistant (ISA)")
    parser.add_argument(
        "--check",
        action="store_true",
        help="Validate air-gap config, LanceDB store, Obsidian REST API, and local LLM.",
    )
    parser.add_argument(
        "--ingest",
        action="store_true",
        help="Ingest and index all Obsidian vault Markdown notes into LanceDB.",
    )
    parser.add_argument(
        "--profile",
        action="store_true",
        help="Profile vault ingestion, Hybrid RRF query latency, and memory footprint.",
    )
    parser.add_argument(
        "--once",
        action="store_true",
        help="Process a single pending @ISA command in ISA_Inbox.md and exit.",
    )
    parser.add_argument(
        "--clear-memory",
        action="store_true",
        help="Clear the persisted rolling context window JSON before starting ISA.",
    )
    parser.add_argument(
        "--manual-trigger",
        action="store_true",
        help="Trigger on pressing Enter (a newline) after the prompt, ignoring terminal punctuation.",
    )
    parser.add_argument(
        "--auto-trigger",
        action="store_true",
        help="Trigger on terminal punctuation (.!?) + Enter or pressing Enter twice.",
    )
    args = parser.parse_args()

    if args.manual_trigger:
        os.environ["ISA_MANUAL_TRIGGER_ONLY"] = "true"
    elif args.auto_trigger:
        os.environ["ISA_MANUAL_TRIGGER_ONLY"] = "false"

    if args.clear_memory:
        clear_rolling_memory()

    if args.check:
        return asyncio.run(verify_sprint1_environment(write_hello=True))
    if args.ingest:
        return run_ingestion()
    if args.profile:
        return run_profile()
    if args.once:
        return asyncio.run(process_once())

    return asyncio.run(run_watchdog_daemon(memory_cleared=args.clear_memory))


if __name__ == "__main__":
    raise SystemExit(main())
