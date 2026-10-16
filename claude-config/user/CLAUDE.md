# Coding Preferences & Project Standards

These apply to every project Claude Code creates or edits. A project's own `CLAUDE.md` adds to them and wins where it is more specific.

## Core Behavior

1. Don't assume. Don't hide confusion. Surface tradeoffs.
2. Minimum code that solves the problem. Nothing speculative — no "scale to millions" design unless asked.
3. Touch only what you must. Clean up only your own mess.
   - If a broader fix is warranted, propose it separately rather than folding it into this task.
4. Define success criteria. Loop until verified.

## How I Work

- Plan before build: for anything beyond a small fix (about 3+ files, or shared/core modules), outline the plan and wait for my OK before generating files.
- After the plan is agreed, give execution-ready output, not outlines or scaffolds.
- When iterating, make targeted corrections — don't rewrite the whole thing.

## Verification

- Show what you ran (tests, lint, build) and the output — don't just claim it works.
- If you can't verify, say so explicitly rather than asserting success.

## Communication

- Ask before assuming scope on ambiguous requests.
- Flag when you're uncertain rather than picking silently.

## Environment (uv)

- Every Python project uses `uv`.
- Projects with a `pyproject.toml`: add packages with `uv add` (updates `pyproject.toml` and `uv.lock`), install with `uv sync`, run with `uv run`. Never hand-edit a `requirements.txt` in these projects.
- Projects that only have a `requirements.txt`: `uv venv`, `uv pip install -r requirements.txt`, and keep the file in sync with what is installed.

## GPU / CUDA

- Any project that loads a local model via `torch` (directly, or through `sentence-transformers`, `transformers`, etc.) must use a CUDA-enabled `torch` when a compatible NVIDIA GPU is present, and fall back to CPU automatically when it isn't — never CPU-only by default, never a hard GPU requirement.
- The plain PyPI `torch` wheel is CPU-only on Windows. In a uv project, pin the CUDA build in `pyproject.toml` — a manual `uv pip install torch --index-url ...` is undone by the next `uv sync`:

  ```toml
  [[tool.uv.index]]
  name = "pytorch-cu128"                      # pick the cuXXX tag the installed driver supports
  url = "https://download.pytorch.org/whl/cu128"
  explicit = true

  [tool.uv.sources]
  torch = [{ index = "pytorch-cu128", marker = "sys_platform == 'win32' or sys_platform == 'linux'" }]
  ```

  Check: `uv lock && uv run python -c "import torch; print(torch.__version__, torch.cuda.is_available())"` → a `+cu128` version and `True`.
  Projects with only a `requirements.txt`: `uv pip install torch --index-url https://download.pytorch.org/whl/cu128` before `uv pip install -r requirements.txt`.
- Make the device choice explicit and visible: `device = "cuda" if torch.cuda.is_available() else "cpu"`, logged — never `device=None` silently auto-detecting.
- Document the GPU install step and its expected speedup in the README (How to run + Tech stack).

## Code Style

- Names: clear, human-readable, natural — the way a thoughtful engineer would name things. No cryptic abbreviations or single letters (except trivial loop counters).
- Docstrings: one line, two at most. Explain **why** the code exists or why this approach — the code already shows what it does. Skip the docstring when the name makes the why obvious.
- Prioritize correctness and readability over cleverness.
- Never mention, comment, or document that code was copied or adapted from another project. Write it as native to the current codebase — no "ported from X" / "based on Y" in code, comments, commit messages, or docs.

## Documentation (create + keep updated on every project change)

Each project maintains:
1. **README.md** — overview, purpose, key features
2. **How to run** — setup, environment variables, commands to start / test
3. **Tech stack** — languages, frameworks, libraries, infra, with brief rationale
4. **Future improvements for production** — known gaps, scaling considerations, hardening not yet done
5. **PPT presentation doc** — update it if one exists; don't create one unless asked

Whenever you edit a project, keep the README, workflow/architecture diagrams and other relevant docs in sync with what the code actually does, in the same session — as you go, not as an afterthought.

## Version Control

- Every project has a `.gitignore` — create one immediately if missing, and keep it updated. Never let large files reach a push. Cover, where applicable:
  - `.env` and other secret files (commit a `.env.example` with placeholder values instead)
  - virtual environments (`.venv`, `venv`), `node_modules`
  - model weights / checkpoints (`*.bin`, `*.safetensors`, `*.onnx`, `*.onnx_data`, `*.gguf`, `*.pt`, `*.pth`)
  - bulk or generated data: data folders (`data/raw/`, exports), archives (`*.7z`, `*.zip`), large CSV/Parquet dumps — by folder or name, **not** a blanket `*.csv`: small hand-made test fixtures (e.g. `tests/fixtures/*.csv`) must stay committed
  - build / cache artifacts (`__pycache__/`, `dist/`, `build/`, `.pytest_cache/`)
  - any file or folder over 50 MB, no exceptions
- Before adding a dependency, model download or data file, check whether it belongs in `.gitignore`.
- If a large file was committed in the past, flag it — removing it from history (filter-repo / BFG) is destructive and needs my explicit sign-off.

## AI / LLM Projects

If the `ai-app-scaffold` skill is installed, it holds the detailed standards for AI apps (stack, provider seam, RAG, long-form GPU guidance) and expands on the rules above without replacing them. If it is not installed, these rules are the standard.

## Never

- Never touch `.env`, secrets, or credentials files without asking.
- Never add or upgrade a dependency without asking first.
- Never modify CI/CD config without flagging it.
- Never `git push --force` without explicit confirmation.
- Never delete files outside the current task's scope.
