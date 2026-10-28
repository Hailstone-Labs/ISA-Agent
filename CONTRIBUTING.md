# Contributing to ISA: Intelligent Secretary Agent

Thank you for your interest in contributing to **ISA (Intelligent Secretary Agent)**! We welcome bug reports, architectural improvements, deterministic tool extensions, and documentation enhancements from the developer community.

Please read this document carefully before submitting any code, as it outlines both our technical workflow and our mandatory **Legal Requirements**.

---

## 1. Legal Requirements (Dual-License & Mandatory CLA)

> **CRITICAL NOTICE — READ BEFORE SUBMITTING A PULL REQUEST**

**ISA: Intelligent Secretary Agent** operates under a **dual-license model** maintained by **Roger Allen / Hailstone Labs**:
1. **Open-Source License**: Distributed publicly under the **GNU General Public License v3.0 (GPLv3)** (see [LICENSE](LICENSE)).
2. **Commercial / Proprietary License**: Licensed separately by **Hailstone Labs** for commercial or proprietary integrations that are incompatible with GPLv3 copyleft terms.

### Mandatory Contributor License Agreement (CLA)
Because **Hailstone Labs** maintains the dual-licensing model for this project, **every contributor must sign the Hailstone Labs Contributor License Agreement (CLA) before any Pull Request can be reviewed or merged**.

By signing the CLA, you agree to the following terms:
- **Copyright Assignment / Full Ownership**: You grant and assign to **Roger Allen / Hailstone Labs** full, irrevocable copyright ownership and title in and to the submitted code, documentation, and associated intellectual property.
- **License-Back to Contributor**: Hailstone Labs grants you back a non-exclusive, worldwide, royalty-free license to use your contributed code for your own purposes.
- **Originality & Authority**: You represent and warrant that your contribution is your original creation, that it does not contain third-party code under incompatible licenses, and that you have the legal authority (including employer authorization, if applicable) to assign copyright to Hailstone Labs.

**Pull Requests submitted without a completed and verified CLA on file will not be reviewed or merged.**

---

## 2. Standard Fork-and-Pull-Request Workflow

All contributions follow the standard GitHub **Fork & Pull Request** workflow:

### Step 1: Fork & Clone the Repository
1. Click **Fork** on the upstream repository page to create a copy under your GitHub account.
2. Clone your fork locally and add the `upstream` remote:
   ```bash
   git clone https://github.com/<your-username>/ISA-Agent.git
   cd ISA-Agent
   git remote add upstream https://github.com/<upstream-owner>/ISA-Agent.git
   ```

### Step 2: Set Up the Local Development Environment
ISA uses **[uv](https://docs.astral.sh/uv/)** for deterministic dependency management (`pyproject.toml` and `uv.lock`):

```bash
# Install project and development dependencies into .venv
uv sync

# Copy the example environment configuration
cp .env.example .env
```

### Step 3: Create a Topic Branch
Always create a descriptive branch off the latest `main` branch rather than committing directly to `main`:

```bash
git fetch upstream
git checkout -b feat/your-feature-name upstream/main
```

Use clear branch prefixes:
- `feat/<name>` — New capabilities or agent tools
- `fix/<name>` — Bug fixes or edge-case handling
- `docs/<name>` — Documentation updates
- `test/<name>` — Unit or integration test coverage

### Step 4: Implement Changes & Follow Architectural Standards
When writing code for ISA, adhere to these core engineering invariants:
1. **100% Air-Gap Compliance**: Never introduce external network calls, cloud telemetry, or non-loopback endpoints (`127.0.0.1`, `localhost`, `::1`).
2. **No Hardcoded Paths or Credentials**: All filesystem paths, API keys, and host URLs must be loaded from `.env` via `python-dotenv` and `config/settings.py`.
3. **Deterministic Tool Validation**: All PydanticAI `@agent.tool` functions must validate inputs via Pydantic models and record verified disk writes in `ctx.deps.turn_write_actions`.
4. **Preserve Documentation**: Maintain existing docstrings and inline architectural comments.

### Step 5: Run the Test Suite & Update Attribution Credits
Verify that all unit tests pass and, if any dependencies were added or updated, regenerate `CREDITS.md`:

```bash
# Run the full pytest suite
uv run pytest -v

# If dependencies changed in pyproject.toml / uv.lock, regenerate CREDITS.md
./scripts/generate_credits.sh
```

### Step 6: Commit & Push to Your Fork
Write concise, conventional commit messages (`feat(...): ...`, `fix(...): ...`) and ensure no sensitive paths or `.env` files are staged:

```bash
git status
git commit -m "feat(tools): describe your change concisely"
git push origin feat/your-feature-name
```

### Step 7: Open a Pull Request & Complete the CLA
1. Open a Pull Request against the `main` branch of the upstream repository.
2. Provide a clear summary of the problem, your architectural approach, and test verification results.
3. Complete the **Hailstone Labs CLA** check so your Pull Request is eligible for maintainer review and merge.
