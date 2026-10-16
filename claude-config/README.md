# Claude Code instruction files

| File | Put it at | Applies to |
|---|---|---|
| `user/CLAUDE.md` | `C:\Users\<you>\.claude\CLAUDE.md` | Every project on your PC |
| `lisa/CLAUDE.md` | `<LISA repo root>\CLAUDE.md` (commit it, so the whole team gets it) | LISA only; adds to and overrides the personal file |

The IntelOps `CLAUDE_ProjectLevel.md` stays as the `CLAUDE.md` of the IntelOps repo. `CLAUDE_basic.md` is not needed: everything in it is in `user/CLAUDE.md`.

**What changed from your originals:**

| # | Conflict | Resolution |
|---|---|---|
| 1 | Docstrings "state what it does" (advanced) vs "explain why, not what" (project) | Both files now say **why**, one line, skip it when the name says it |
| 2 | `requirements.txt` in sync + `uv pip install -r` (advanced) vs `uv add` / `uv sync` only (project) | pyproject projects: `uv add` / `uv sync` / `uv run`. requirements-only projects: `uv pip`. CUDA torch is pinned in `pyproject.toml` (`[tool.uv.sources]`), because a manual `uv pip install torch --index-url …` is undone by the next `uv sync` |
| 3 | Blanket `*.csv` in `.gitignore` vs tests on held-out labeled sets | Ignore bulk / generated data by folder or name; small fixtures in `tests/fixtures/` stay committed. Also added: `.env` (with `.env.example` committed) |
| 4 | Reference to the `ai-app-scaffold` skill | Now conditional: used if installed, otherwise the file's own rules apply |
| 5 | Checkpoint tests with `SqliteSaver` | The LISA file tests interrupt / resume through `PostgresSaver` against a real PostgreSQL. The IntelOps file is unchanged |

Also merged: "plan before build" and "outline the plan for 3+ files" were two rules saying the same thing, now one. Added "nothing speculative — no 'scale to millions' design unless asked", so the engineering prompts can't override it.

**CUDA check on your PC** (download.pytorch.org was not reachable from the environment where these files were written, so the `[tool.uv.sources]` snippet was syntax-checked by uv but not resolved):

```powershell
uv lock
uv run python -c "import torch; print(torch.__version__, torch.cuda.is_available())"   # expect something like 2.x.x+cu128 True
```

**Keep them short:** Claude Code loads these files at the start of every session, so every line costs context. Put long guidance in skills or linked docs.
