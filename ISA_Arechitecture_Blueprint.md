Architectural Blueprint for the Intelligent Secretary Assistant (ISA): A Local, Air-Gapped Obsidian Integration
Executive Summary and System Topography
The demand for autonomous, privacy-preserving digital assistants has catalyzed a paradigm shift toward localized, air-gapped artificial intelligence systems. The Intelligent Secretary Assistant (ISA) represents a highly sophisticated implementation of this paradigm, explicitly engineered to operate entirely offline while executing complex administrative and secretarial duties. By leveraging Obsidian—a highly extensible, local-first Markdown knowledge base—as both its primary user interface and central memory store, ISA bypasses the vulnerability and latency inherent in traditional web interfaces and cloud-based database infrastructures.
At the cognitive core of this system lies the gemma4-12b model. Google DeepMind's Gemma 4 12B offers near-frontier reasoning, code generation, and instruction-following capabilities within a parameter footprint that permits execution on consumer-grade hardware via advanced quantization techniques1. To fulfill its operational mandate, ISA requires a robust architectural bridge connecting the language model to the Obsidian vault. This entails reading dynamic contexts through Retrieval-Augmented Generation (RAG), synthesizing historical documents, scheduling calendar events, and dynamically managing tasks based on conversational natural language inputs.
This report outlines an exhaustive, end-to-end software architectural plan for constructing ISA. It details the deployment strategies for the local Large Language Model (LLM), the topological layout of the asynchronous Python-based agentic loop, the file-system event monitoring required for real-time interaction, the semantic vector retrieval mechanisms necessary for deep vault comprehension, and the specific Markdown and YAML syntaxes required to interface seamlessly with Obsidian's rich ecosystem of plugins. Furthermore, it strictly adheres to the constraint of absolute network isolation, providing concrete, OS-level methodologies to enforce a zero-egress computing environment.
The system topology is organized into four primary, decoupled subsystems operating asynchronously. The User Interface Layer is entirely native to Obsidian, utilizing specific Markdown files as "inboxes" where the user types natural language commands. The Event and Orchestration Layer, driven by a Python backend, continuously monitors these designated inbox files for operating system-level modification events. Upon detecting a user command, this orchestrator manages the cognitive workflow. The Cognitive and Reasoning Layer is powered by the local gemma4-12b inference engine, utilizing structured JSON output schemas to convert natural language into precise tool calls3. Finally, the Semantic Memory Layer executes these tool calls by interacting with a local vector database to fetch relevant historical context, which is then fed back to the LLM to generate the final, context-aware output.
The Cognitive Engine: Local LLM Deployment Strategies
The selection of the Gemma 4 12B model is optimal for a local, highly capable secretarial agent. As an instruction-tuned model, it exhibits exceptional coding, reasoning, and context-retention capabilities, boasting a maximum theoretical context window of 256,000 tokens5. However, deploying a 12-billion parameter model in an unoptimized state requires hardware far exceeding typical consumer capabilities. The system must employ strategic quantization to compress the model weights into the GGUF (GPT-Generated Unified Format), allowing it to fit within standard VRAM constraints while maintaining reasoning fidelity.
Quantization Mathematics and Hardware Scaling
Quantization reduces the precision of the model's neural network weights from their original 16-bit floating-point (FP16) representation down to 8-bit, 4-bit, or custom fractional bit formats2. This compression dramatically lowers VRAM requirements and accelerates inference speed by reducing memory bandwidth bottlenecks, with only mathematically marginal impacts on the model's instruction-following quality2.

Quantization Level
Estimated File Size
Minimum VRAM Required
Performance Characteristics and Recommended Hardware
FP16
~24.0 GB
~26.0 GB
Uncompressed baseline. Requires dual 16GB GPUs or single 32GB+ VRAM (RTX 5090 / Mac M4 Max).
Q8_0
~12.7 GB
~15.0 GB
Near-lossless fidelity. Recommended for 16GB to 24GB VRAM configurations (RTX 4080/4090).
Q6_K
~9.8 GB
~12.0 GB
Exceptionally high fidelity. Ideal balance for 12GB to 16GB VRAM hardware (RTX 4070).
Q5_K_M
~8.6 GB
~10.0 GB
Strong daily driver. Noticeably better reasoning than 4-bit on highly complex logistical logic tasks.
Q4_K_M
~7.4 GB
~9.0 GB
The optimal balance of size and intelligence. Fits comfortably on 8GB - 12GB VRAM GPUs (RTX 4060).
Q3_K_M
~6.1 GB
~8.0 GB
Noticeable degradation in reasoning quality. Recommended only for strictly constrained 8GB budgets.

The architectural recommendation for ISA is the deployment of the Q4_K_M or Q5_K_M quantization. The Q4_K_M variant fits comfortably within 9 GB of VRAM, allowing it to execute efficiently on standard consumer GPUs while leaving sufficient memory overhead for the Key-Value (KV) cache2. The KV cache is a critical component of LLM inference that stores intermediate attention states; as the context window grows, the KV cache expands proportionally. While Gemma 4 technically supports up to 256K tokens, utilizing the full context window can consume an additional 4 to 8 GB of VRAM alone5. For day-to-day secretarial operations, the system architecture should artificially constrain the context window parameter (num_ctx) to 8,192 or 16,384 tokens to ensure absolute system stability without causing out-of-memory (OOM) errors during extensive vault summarization tasks2. Benchmarks indicate that utilizing Quantization-Aware Training (QAT) variants of Gemma 4 12B on optimized hardware can yield inference speeds of up to 120 tokens per second, ensuring highly responsive interactions7.
Inference Engine Selection and Tool Calling
To serve the GGUF model files locally, the architecture requires an OpenAI-compatible local inference server. The industry standard tools for this are llama.cpp (running as a bare-metal HTTP server) and Ollama, which acts as a sophisticated management layer over the llama.cpp backend2.
Ollama is heavily recommended for the ISA backend architecture due to its seamless integration with modern Python orchestration frameworks like LangChain and LlamaIndex, and critically, its native support for advanced agentic paradigms such as structured JSON outputs and tool calling (also known as function calling)3. Tool calling fundamentally shifts the LLM from a text generator to a deterministic reasoning engine. Instead of generating conversational prose, the Python orchestrator passes a JSON schema defining available tools—for instance, create_calendar_event(title: string, start_time: string, end_time: string) or search_vault(query: string). The gemma4-12b model analyzes the user's natural language input, determines if a tool is required, and outputs a structured JSON payload dictating exactly which tool the Python backend should execute and with what parameters3. This deterministic formatting is the cornerstone of ISA's ability to manipulate the Obsidian vault without parsing errors.
Enforcing Absolute Network Isolation (The Air-Gap)
The foundational security requirement of ISA is that it operates without any internet access. This constraint must be aggressively enforced at both the application configuration level and the operating system level to prevent accidental telemetry, automated updates, or unauthorized model downloading during runtime. Modern inference servers, by default, periodically check for updates and maintain outbound connections to remote model registries9.
Application-Level Environment Controls
To secure the local deployment, specific environment variables must be strictly configured in the host system prior to launching the Ollama inference server. These variables override the default application behaviors and sever all logical outbound network paths.


Environment Variable
Required Value
Architectural Purpose
OLLAMA_NO_CLOUD
1
Disables all proprietary cloud features, remote inference fallbacks, and internal web search capabilities, enforcing strict local execution9.
OLLAMA_NO_UPDATE_CHECK
1
Prevents the engine from polling external release endpoints (e.g., GitHub or Ollama servers) for version updates, eliminating diagnostic pinging10.
OLLAMA_HOST
127.0.0.1:11434
Binds the local HTTP server strictly to the loopback interface, ensuring it cannot be accessed by other machines on the local network, securing the API endpoint9.
COPILOT_OFFLINE
true
If any associated development extensions or agent frameworks are utilized in the broader environment, this ensures full telemetry lockdown12.
OPENCODE_AIRGAPPED
1
Disables automated manifest fetching and background plugin updates for secondary code-generation agents that might share the Python environment13.

Operating System-Level Enforcement
Application-level controls, while necessary, require a fallback mechanism in case of software misconfiguration. True air-gapping requires OS-level enforcement. The system administrator must connect to the internet temporarily to execute the initial model acquisition (e.g., ollama pull gemma4:12b) or manually transfer the pre-quantized .gguf file via a secure USB drive from an internet-connected terminal, importing it locally using a custom Modelfile2.
Once the neural network weights are securely stored on the local disk, absolute network isolation is enforced via firewall rules. On Windows environments, the administrator must utilize Windows Defender Firewall with Advanced Security to create an Outbound Rule targeting %LOCALAPPDATA%\Programs\Ollama\ollama.exe (or the respective Python binary), setting the action to strictly "Block"10. On Linux systems, this can be achieved using iptables or ufw, routing all outbound traffic from the daemon's process ID to a null interface, permitting traffic solely on the 127.0.0.1 loopback address10. This multi-layered approach guarantees that highly sensitive personal and professional notes processed by ISA can never traverse the internet.
Interface and Bi-Directional Vault Communication
Obsidian is fundamentally a collection of local Markdown files. While a naive implementation might attempt direct Python file I/O (Input/Output) to manipulate the vault, this approach introduces severe concurrency risks, potential file corruption, and critically, it bypasses Obsidian's internal indexing and metadata resolution mechanisms. To achieve reliable integration, ISA must utilize the Obsidian Local REST API plugin16.
RESTful Architectural Integration
The Local REST API plugin instantiates a secure local server running concurrently within the Obsidian application process (typically binding to 127.0.0.1:27123 for unencrypted HTTP or 27124 for HTTPS)18. This architectural bridge exposes the entire state of the vault to programmatic interaction through comprehensive CRUD endpoints. The Python daemon executing the ISA logic interacts with these endpoints by passing a cryptographically generated API key in the Authorization: Bearer <token> header18. Because all traffic routes exclusively through the local loopback interface, this communication remains inherently secure and air-gapped18.
The critical endpoints for ISA's operational success include:
GET /vault/{path}: Reads the raw content, frontmatter, and tags of any file. This is crucial for retrieving the exact state of a document before modifying it17.
POST /vault/{path}: Instantiates new Markdown files within the vault, utilized by ISA when generating reports or creating entirely new calendar entries18.
PATCH /vault/{path}: The most sophisticated endpoint, providing a precise editing engine. It allows ISA to append content to the end of a file, target specific Markdown headings, or modify individual YAML frontmatter fields without needing to overwrite the entire file structure, dramatically reducing the risk of data corruption17.
POST /search/simple/: Executes high-speed, full-text searches leveraging Obsidian’s native caching and indexing mechanisms17.
POST /commands/{commandId}/: Allows the external Python script to trigger native Obsidian commands, such as opening a file in a specific pane or executing third-party plugin macros17.
Model Context Protocol (MCP) Capabilities
Recent advancements in the Obsidian ecosystem have introduced the Model Context Protocol (MCP) server endpoint (/mcp/) to the Local REST API plugin16. MCP is an emerging standard designed specifically to expose software capabilities directly as structured tools for AI assistants. While the Python daemon can construct raw HTTP requests, utilizing the MCP extension abstracts significant complexity.
The MCP implementation within Obsidian provides pre-configured tools tailored for agentic workflows. For example, the vault_read_binary tool resolves a significant limitation in standard REST APIs: it allows an AI agent to read image attachments17. Previously, requesting an image returned raw base64 strings, which consumed hundreds of thousands of context tokens without granting the model actual visual comprehension21. The MCP tool intercepts the image, downscales it on Obsidian's internal canvas to fit within a 1568px boundary, and returns a standardized image block alongside size and MIME type metadata, significantly reducing token bloat21. While ISA primarily focuses on text, leveraging the MCP definitions (vault_list, vault_append, vault_get_document_map) ensures the Python orchestrator uses the most token-efficient and model-friendly data structures available17. Furthermore, "Second Brain" extensions to the MCP server introduce native C++ multi-core binary execution for high-performance vault indexing, utilizing Graph Traversal (Breadth-First Search) to navigate internal links and gather rich contextual knowledge surrounding specific query entry points16.
Asynchronous Event-Driven Orchestration
For ISA to function truly as a secretary, the interaction model must be proactive and highly responsive. Polling the Obsidian vault for changes every few seconds is computationally wasteful and introduces unacceptable latency. The architecture mandates an event-driven approach utilizing watchdog, a cross-platform Python library that hooks directly into the underlying operating system's file monitoring APIs (e.g., inotify on Linux, FSEvents on macOS, or ReadDirectoryChangesW on Windows)22.
The FileSystemEventHandler Implementation
The integration requires defining a designated interaction file within the Obsidian vault, conceptually acting as the secretary's desk (e.g., ISA_Inbox.md). The Python orchestration script instantiates a watchdog.observers.Observer thread attached to a custom subclass of FileSystemEventHandler23.
When the user types a command within the inbox note—for instance, "@ISA, review the project notes from yesterday and draft a summary email"—and saves the file, the operating system instantly broadcasts a file modification signal. The watchdog daemon intercepts this signal, triggering the on_modified callback method22.
This implementation requires careful debouncing logic. Because the Python agent will eventually write its response back into the very same ISA_Inbox.md file, this write action will trigger another on_modified event. Without debouncing or checksum tracking, this creates an infinite, catastrophic feedback loop. The Python logic must track the last modified timestamp or calculate an MD5 hash of the file contents. When an event fires, the script checks if the new content originates from the user (by comparing hashes or looking for a specific prompt delimiter) before initiating the expensive LLM inference cycle22.
The ReAct Agentic Loop
Once a legitimate user command is detected and extracted, the Python daemon initiates the ReAct (Reasoning and Acting) execution loop3.
System Framing: The orchestrator wraps the raw user prompt in a heavily engineered system message. This message injects critical temporal context (the exact current date, time, and day of the week), defines ISA's secretarial persona, and provides the strict JSON schemas for the available toolset1.
Cognitive Evaluation: The gemma4-12b model receives the prompt. Utilizing its tool-calling capabilities, the LLM determines if it possesses the necessary information to satisfy the request immediately, or if it must query the vault.
Tool Dispatch: If the prompt asks to summarize a past meeting, the LLM outputs a JSON payload requesting the search_vault tool. The Python daemon intercepts this JSON, executes the retrieval process (detailed in the subsequent section), and appends the raw results to the conversation history.
Synthesis and Final Action: The updated history is passed back to the LLM. With the retrieved context now in its working memory, Gemma 4 synthesizes the final summary and outputs a command to utilize the vault_append tool. The Python daemon translates this into a PATCH request to the Local REST API, pushing the generated text directly back into the Obsidian inbox note, notifying the user that the task is complete17.
This asynchronous loop, managed by Python's asyncio library, allows the background processing to occur without blocking the user's interface, creating a dynamic, reactive operating system layered on top of static Markdown files18.
Semantic Memory and Retrieval-Augmented Generation (RAG)
An intelligent secretary must possess a comprehensive understanding of the user's historical files. Given that Gemma 4 12B cannot ingest an entire expansive vault in its finite prompt context (and attempting to do so would cause severe VRAM overflow and immense processing latency), a local Retrieval-Augmented Generation (RAG) pipeline is an absolute architectural requirement5.
Vector Database Architecture: LanceDB
The recommended vector storage engine for this highly localized architecture is LanceDB. Traditional vector databases (such as PostgreSQL with PGVector, or standalone Qdrant containers) require dedicated, persistent background server processes, consuming continuous CPU cycles and memory. LanceDB, conversely, is an embedded, serverless database built upon the open Lance columnar data format27. It stores its multi-dimensional indices directly on disk within the local file system, reading them into memory only when queried. This adds zero operational overhead to the air-gapped machine when idle, yet easily scales to search millions of embeddings in milliseconds27.
Local Embedding Models and Chunking Strategies
To transpose Obsidian's textual Markdown files into a searchable mathematical vector space, the system requires a local embedding model. Text embeddings translate semantic meaning into high-dimensional numerical arrays, where concepts with similar meanings cluster together. The optimal choices for local deployment are:


Model Designation
Dimensionality
Footprint
Execution Profile
nomic-embed-text-v1.5
768 Dimensions
~274 MB
Highly performant general-purpose model. Excellent for mixed code and text environments26.
BAAI/bge-small-en-v1.5
384 Dimensions
~133 MB
Highly optimized, resource-constrained model. Exceptionally fast on CPU-only execution via the fastembed Rust crate26.

Generating these embeddings locally using the sentence-transformers library or Ollama ensures that no proprietary vault data ever touches an external API26.
Before embedding, the documents must be processed. Obsidian vaults are not amorphous text; they are highly structured Markdown. Utilizing naive, fixed-size token splitting algorithms often destroys context by arbitrarily severing paragraphs in half or separating critical H2 headings from their underlying explanatory text. The RAG pipeline must utilize a context-aware parser, specifically the MarkdownHeaderTextSplitter from frameworks like LangChain or LlamaIndex32. This utility chunks the vault strictly along heading boundaries (H1, H2, H3), preserving the hierarchical, parent-child relationship of the user's thoughts and ensuring the generated embeddings encapsulate complete, unified concepts32.
Hybrid Search and Reciprocal Rank Fusion (RRF)
Once the vault is chunked and embedded into LanceDB, the retrieval phase occurs. Pure semantic vector search is excellent for conceptual queries (e.g., "Find notes about marketing strategies") but notoriously fails at exact keyword retrieval (e.g., searching for a specific alphanumeric product serial number or a unique, newly coined project codename)33.
To solve this, the architecture implements Hybrid Search. This technique simultaneously executes a dense semantic vector search and a sparse, exact-match BM25 keyword search34. When querying, LanceDB returns two separate, independently ranked lists of document chunks.
These disparate lists are mathematically reconciled using the Reciprocal Rank Fusion (RRF) algorithm28. RRF is a state-of-the-art rank aggregation method that does not require arbitrary weight tuning or machine learning models to fuse the results36. The algorithm calculates a unified score for each document based on its inverse rank position across the different lists:

Where  is the specific document being evaluated,  represents the set of ranked lists (the vector results and the BM25 results),  is the document's rank position in a given list, and  is a predefined smoothing constant (typically calibrated to 60)37.
By calculating the RRF score, the system ensures that document chunks matching both the exact specialized vocabulary of the user's prompt and the broader semantic intent bubble to the absolute top of the retrieved context window28. This model-free fusion strategy is fed directly into the Gemma 4 12B prompt, significantly reducing LLM hallucinations and eliminating information retrieval blind spots36.
Execution of Secretarial Duties: Scheduling and Calendar Management
For ISA to function as an effective secretary, it cannot simply return conversational text to the inbox. It must format its output to programmatically trigger Obsidian's specific UI plugins. This requires precise prompt engineering and structured output constraints applied to the LLM.
Obsidian Full Calendar is the premier plugin for managing schedules within a vault. It dynamically renders a graphical calendar UI by aggregating specialized Markdown notes40. Full Calendar identifies events strictly through YAML frontmatter properties located at the very top of a note file40.
To create a new calendar event, ISA must generate a wholly new Markdown file containing this specific metadata schema. The required schema mandates the inclusion of standard start and end dates, times, and a type parameter (which structurally defaults to single or recurring)41.
Consider a user prompt: "Schedule a 1-on-1 performance review with Alice for tomorrow from 10 AM to 11 AM."
ISA's Python agent will interpret this via Gemma 4, formulate the parameters, and utilize the REST API to POST a new file (e.g., Review with Alice.md) containing the following exact syntax:



YAML
---
title: Performance Review with Alice
allDay: false
startTime: 10:00
endTime: 11:00
date: 2026-10-22
endDate: 2026-10-22
type: single
---
### Agenda
- Goal review
- Q3 Feedback


The critical challenge here is temporal relativity. "Tomorrow" is meaningless to a stateless LLM. The system prompt provided by the Python daemon must explicitly inject the current localized date and time (e.g., "Current System Time: Wednesday, October 21, 2026, 8:43 AM"). The LLM, constrained by its JSON tool schema, computes "tomorrow" as "2026-10-22" and passes this absolute date string back to the Python daemon for execution40.
Execution of Secretarial Duties: Task Management Tracking
The Obsidian Tasks plugin relies on a radically different architectural approach than the Calendar plugin. Instead of isolating data in YAML frontmatter, it utilizes a highly flexible, inline Markdown syntax. It leverages specific Unicode emojis to designate actionable metadata, allowing tasks to be scattered anywhere across daily notes or project files, which are then aggregated dynamically via complex query blocks42.
The fundamental syntax structure follows: - [ ] Task Description [Tags] [Emojis].
Crucially, the prompt engineering for Gemma 4 12B must force the LLM to master and accurately deploy these emoji signifiers:
📅 indicates the absolute Due Date42.
⏳ indicates the Scheduled Date42.
🛫 indicates the Start Date42.
🔁 indicates the Recurrence Signifier42.
⏫, 🔼, 🔽 dictate priority levels43.
Translating natural language into the strict recurrence rules required by the Obsidian Tasks plugin involves navigating specific linguistic patterns and edge-case logic. The syntax must follow exact patterns following the 🔁 signifier, such as every weekday, every week on Sunday, or every month on the last Friday42.
The plugin exhibits highly specific deterministic behaviors that the LLM must be aware of to prevent data corruption. For example, if a user requests a task to recur on the final day of the month, the syntax every month on the 31st is used. If the new calculated due date falls on a month with fewer than 31 days (e.g., April), the plugin's internal rrule engine automatically skips forward or rolls the date backward to the nearest valid date (April 30th)42. Furthermore, if a task is generated with an invalid highest-priority date (e.g., February 30th), the recurrence rule will silently fail and disappear upon task completion42.
When the user commands ISA: "Remind me to submit the weekly budget report every Friday, mark it high priority," the LLM will generate a tool call instructing the agent to append data. ISA will locate the designated active tasks note via the REST API's PATCH endpoint and inject:
- [ ] Submit the weekly budget report 🔁 every week on Friday 📅 2026-10-23 ⏫42.
By formatting this string flawlessly within the payload, the Tasks plugin will ingest it instantaneously, rendering the high-priority task on the user's management dashboard without requiring any manual UI interaction.
Automated Document Generation and Template Instantiation
A core mandate of an intelligent secretary is the ability to draft, format, and generate documents upon request. Obsidian relies heavily on the "Templater" plugin for standardizing complex note creation. Templater uses the Eta syntax engine (<% ... %>) to inject dynamic variables, execute internal JavaScript, and prompt users with modals dynamically upon file creation44.
ISA handles document generation through two distinct procedural methodologies, depending on the complexity and standardisation of the user's request:
Direct Generative Instantiation
For highly bespoke, non-standard documents (e.g., "Read the transcripts from the last three executive meetings and write a cohesive summary of our marketing strategy shift"), ISA relies entirely on its RAG pipeline and LLM capabilities. The Python agent retrieves the relevant chunks via the LanceDB hybrid search, passes them to Gemma 4 12B, and streams the generated Markdown directly. It then utilizes the REST API's POST /vault/{filename} endpoint to create a brand new file, injecting the synthesized report directly into the vault18.
Programmatic Template Triggering
If the user requests a standardized document that adheres to a strict corporate or personal structure (e.g., "Create a new daily standup note for today"), ISA does not need to hallucinate the structural scaffolding. Instead, the Python agent leverages Templater's internal execution engine.
Templater allows for advanced JavaScript execution blocks (<%* ... -%>) that can define internal variables, execute automated file renames (tp.file.rename), calculate dates (tp.date.now), and move files to specific directory paths based on frontmatter conditions44.
To trigger this, ISA constructs a minimal file payload containing a Templater include statement: <% tp.file.include("[[Daily Standup Template]]") %>. Alternatively, the Python daemon can utilize the REST API to trigger a specific Obsidian command ID (POST /commands/{commandId}/) linked directly to a Templater execution hotkey17.
By writing a file containing these specific tags, or by triggering the API command, ISA offloads the rigid formatting logic to the vault's native mechanisms17. The design philosophy here is paramount: allow the neural network (Gemma 4) to focus computational resources entirely on semantic content retrieval and synthesis, while allowing the deterministic JavaScript engine (Templater) to handle structural scaffolding and repetitive boilerplate formatting.
Strategic Rollout and Implementation Roadmap
The Intelligent Secretary Assistant represents a highly complex, multi-layered synthesis of the most advanced open-source, local-first technologies currently available. By anchoring the system in Obsidian, ISA benefits from an ultra-fast, universally readable Markdown data structure that is immune to vendor lock-in. By utilizing Gemma 4 12B via an air-gapped Ollama instance, it gains top-tier cognitive reasoning without sacrificing data sovereignty or privacy. By implementing LanceDB with Reciprocal Rank Fusion, it achieves an enterprise-grade semantic memory retrieval system that fits entirely on a local solid-state drive.
The implementation of this architectural blueprint should be phased systematically:
Phase One: Infrastructural Scaffolding and Security Validation. Deploy the local gemma4-12b model via Ollama, configure the absolute air-gap environment variables, and validate that zero outbound network traffic occurs via firewall packet inspection. Install the Obsidian Local REST API and establish the baseline Python watchdog loop. Success in this phase is defined by the Python script successfully writing a static "Hello World" note into the vault upon detecting a user command in the inbox.
Phase Two: Memory Ingestion and RAG Calibration. Initialize the LanceDB embedded database. Execute the initial embedding process over the entire historical vault using the MarkdownHeaderTextSplitter and bge-small-en-v1.526. Calibrate the Reciprocal Rank Fusion constant () to ensure exact keyword matches (e.g., project names) successfully surface alongside broader semantic queries28.
Phase Three: Syntactic Precision and Tool Calling. Refine the system prompts and the JSON schema definitions provided to the LLM. Iteratively test the generation of the specific YAML frontmatter required by Obsidian Full Calendar and the emoji-based inline syntax required by Obsidian Tasks40. Implement rigorous error-handling within the Python daemon to catch and format LLM hallucinations before they corrupt the vault's Markdown files.
Once fully optimized and calibrated across these three phases, ISA will transcend the limitations of a standard script, providing a seamless, entirely private, and highly autonomous administrative capability directly integrated into the user's digital workflow.
Works cited
unsloth/gemma-4-12b-it-GGUF - Hugging Face, https://huggingface.co/unsloth/gemma-4-12b-it-GGUF
Gemma 4 GGUF Download: Best Q4, Q5 & Q8 Quantization Guide, https://gemma4-ai.com/blog/gemma4-gguf-guide
Tool calling - Ollama documentation, https://docs.ollama.com/capabilities/tool-calling
Structured outputs · Ollama Blog, https://ollama.com/blog/structured-outputs
liodon-ai/gemma-4-12B-it-GGUF - Hugging Face, https://huggingface.co/liodon-ai/gemma-4-12B-it-GGUF
Gemma 3 VRAM Requirements (2026): 12B Runs in 6.7GB, https://willitrunai.com/blog/gemma-3-local-inference-guide
120 tok/s on 12GB VRAM with Gemma 4 12B QAT MTP - Reddit, https://www.reddit.com/r/LocalLLaMA/comments/1typjmc/120_toks_on_12gb_vram_with_gemma_4_12b_qat_mtp/
google/gemma-3-12b-it-qat-q4_0-gguf - Hugging Face, https://huggingface.co/google/gemma-3-12b-it-qat-q4_0-gguf
FAQ - Ollama documentation, https://docs.ollama.com/faq
Full Offline Ollama: Block All Telemetry and External Traffic, https://www.tawroth.com/category/privacy/ollama-full-offline/
Ollama Environment Variables — Complete Reference - Medium, https://medium.com/@smrati.katiyar/ollama-environment-variables-complete-reference-909fe2533786
Copilot CLI now supports BYOK and local models - GitHub Changelog, https://github.blog/changelog/2026-04-07-copilot-cli-now-supports-byok-and-local-models/
Full air-gapped / offline mode support – disable all non-LLM network, https://github.com/anomalyco/opencode/issues/16117
Is Ollama fully offline? — privacy & local mode guide, https://ai-ollama.github.io/privacy-offline.html
Air-Gapped AI Coding: Setup Guide (2026) | Bodega One, https://www.bodegaone.ai/blog/air-gapped-ai-coding-guide-2026
Local REST API Second Brain MCP Extension - Obsidian Plugin, https://community.obsidian.md/plugins/local-rest-api-second-brain-mcp-extension
Local REST API with MCP - Obsidian Plugin, https://community.obsidian.md/plugins/obsidian-local-rest-api
REST API - Obsidian Plugin, https://community.obsidian.md/plugins/rest-api
Obs186｜Local REST API & MCP Server 4.0 Major Overhaul, https://quaily.com/jdevtw-en/p/obs-local-rest-api-and-mcp-server-big-upgrade-natural-language-search-notes-write-files-manage-tags
Local Rest API for Obsidian: Interactive API Documentation, https://coddingtonbear.github.io/obsidian-local-rest-api/
Releases · coddingtonbear/obsidian-local-rest-api - GitHub, https://github.com/coddingtonbear/obsidian-local-rest-api/releases
Build a File Sync Tool with Python and Watchdog - DEV Community, https://dev.to/qingluan/build-a-file-sync-tool-with-python-and-watchdog-4db0
Quickstart — watchdog 2.1.5 documentation, https://python-watchdog.readthedocs.io/en/stable/quickstart.html
File Monitoring using watchdog library - Stack Overflow, https://stackoverflow.com/questions/57322546/file-monitoring-using-watchdog-library
Yuhanbo Python Runner - Obsidian Plugin, https://community.obsidian.md/plugins/yuhanbo-python-runner
Local Rag Private Documents Ai - The Hallway Journal, https://blog.paperhallway.com/article/local-rag-private-documents-ai
Vector Databases & AI Agent Memory Compared | OpenGauntlet, https://opengauntlet.com/memory.html
Python API Reference - LanceDB - GitHub Pages, https://lancedb.github.io/lancedb/python/python/
Ragpilot - Learn AI - Miraheze, https://ai.miraheze.org/wiki/Ragpilot
cognee/CLAUDE.md at main - GitHub, https://github.com/topoteretes/cognee/blob/main/CLAUDE.md
AnythingLLM vs PrivateGPT vs Open WebUI: Best Local RAG in 2026, https://www.promptquorum.com/power-local-llm/anythingllm-vs-privategpt-vs-openwebui-rag
Free HTML to Markdown Converter — Clean .md from Web Pages, https://taloratools.com/web-converters/html-to-markdown/
Reciprocal Rank Fusion: The Formula That Makes Hybrid ... - Scaler, https://www.scaler.com/topics/reciprocal-rank-fusion/
Hybrid RAG Search with BM25 and Rank Fusion in Python, https://www.rodrigo-arenas.com/en/blogs/hybrid-rag-search-bm25-rank-fusion-python/
Evaluating Hybrid Search Performance - Reranking - LanceDB, https://docs.lancedb.com/reranking/eval
[Feature] Add RRF (Reciprocal Rank Fusion) as a rerank strategy for, https://github.com/langgenius/dify/discussions/34643
Reciprocal Rank Fusion: the one-line algorithm behind hybrid search, https://blog.serghei.pl/posts/reciprocal-rank-fusion-explained/
RAG — VII (Reranking with RRF). Reciprocal Rank Fusion ... - Medium, https://medium.com/@danushidk507/rag-vii-reranking-with-rrf-d8a13dba96de
Hybrid RAG Pipeline — Dense Search, BM25, RRF, and Reranking, https://www.reddit.com/r/LangChain/comments/1wy39hv/hybrid_rag_pipeline_dense_search_bm25_rrf_and/
Full Calendar - Obsidian Plugin, https://community.obsidian.md/plugins/obsidian-full-calendar
Custom Note Template · community-archive obsidian-full-calendar, https://github.com/obsidian-community/obsidian-full-calendar/discussions/396
Recurring Tasks - Tasks User Guide - Obsidian Publish, https://publish.obsidian.md/tasks/Getting+Started/Recurring+Tasks
Obsidian Tasks Plugin: How to Use It, Syntax and Queries - TaskForge, https://taskforge.md/blog/obsidian-tasks-guide/
Obsidian Templater Fun - Macdrifter, http://www.macdrifter.com/2021/08/obsidian-templater-fun.html
Making a new note using a template AND providing a link in ... - Reddit, https://www.reddit.com/r/ObsidianMD/comments/18vvz2b/using_templater_making_a_new_note_using_a/
Templater snippets - zachyoung.dev, https://zachyoung.dev/posts/templater-snippets/
TIP: Templater and tp.file.include issues with shared information, https://www.reddit.com/r/ObsidianMD/comments/1rzfxmq/tip_templater_and_tpfileinclude_issues_with/


# ISA (Intelligent Secretary Assistant) - 12-Week Development Roadmap

A structured 12-week schedule to build, calibrate, and harden ISA across three months, organized into 2-week sprints.

## Development Sprints

| Sprint | Timeline | Primary Objective | Key Deliverables & Milestones |
| :--- | :--- | :--- | :--- |
| **Sprint 1** | Weeks 1–2 | **Environment & Offline Core** | • Run `gemma4-12b` locally via Ollama / `llama.cpp` (already completed via `nvfp4`).<br>• Enforce OS-level and config-level air-gapping (`OLLAMA_NO_CLOUD`, loopback binding).<br>• Configure Obsidian Local REST API plugin and issue initial test requests via Python. |
| **Sprint 2** | Weeks 3–4 | **Event Loop & Bidirectional I/O** | • Implement `watchdog` daemon to monitor `ISA_Inbox.md`.<br>• Build content hashing and debouncing to prevent recursive write loops.<br>• Set up ReAct dispatch using PydanticAI: user prompt → local LLM parsing → formatted response appended back to the inbox. |
| **Sprint 3** | Weeks 5–6 | **Local RAG & Vault Ingestion** | • Set up embedded LanceDB and a local embedding model.<br>• Build a Markdown-aware chunking pipeline using `MarkdownHeaderTextSplitter`.<br>• Implement hybrid retrieval (dense semantic search + BM25 keyword matching) fused with Reciprocal Rank Fusion (RRF). |
| **Sprint 4** | Weeks 7–8 | **Calendar & Task Tool-Calling** | • Define strict Pydantic models for tool calling.<br>• Implement Full Calendar integration (generating event files with required YAML frontmatter).<br>• Implement Tasks integration (generating inline tasks with exact emoji signifiers: 📅, ⏳, 🛫, 🔁, priority tags). |
| **Sprint 5** | Weeks 9–10 | **Document Synthesis & Templater** | • Implement multi-document synthesis workflows (meeting minutes summaries, project briefing notes).<br>• Integrate Templater triggering via REST API commands or template file instantiation.<br>• Add output guardrails to validate syntax before writing changes to the vault. |
| **Sprint 6** | Weeks 11–12 | **End-to-End Hardening & Polish** | • Test real-world edge cases (conflicting dates, malformed inputs, large prompt contexts).<br>• Profile latency, memory footprint, and KV-cache limits (`num_ctx`).<br>• Package the Python service into an autostart background daemon/service for seamless daily use. |

---

## Critical Milestones & Exit Gates

* **Month 1 Gate (End of Week 4):** A fully air-gapped agent responds to typed prompts in `ISA_Inbox.md` and reliably writes back without triggering file-loop cascades.
* **Month 2 Gate (End of Week 8):** ISA can query the past vault context via LanceDB hybrid search and accurately create scheduled events and tasks that render cleanly in Obsidian plugins via PydanticAI tool calls.
* **Month 3 Gate (End of Week 12):** Autonomous document drafting and template expansion are operational, latency is tuned, and the system runs as a stable local background process.
