# AGENTS.md

This file provides guidance to Codex (Codex.ai/code) when working with code in this repository.

## What this repository is

This is a **Codex Skill** (`vllm-ascend-developer`) for developing and debugging inference precision in **vLLM + vLLM-Ascend** on Ascend NPUs. It is not a runnable application — it is a set of Markdown modules/workflows plus a few small Python glue scripts that Codex drives. There is also a **sibling component**, `HyperScript/` (a TUI installer), which lives at the repo root one level up and is unrelated to the skill's runtime.

- Plugin Skill entry point: `skills/diagnose/SKILL.md` (invoked as `/vllm-ascend-developer:diagnose`).
- Root `SKILL.md` is a temporary legacy entry. Plugin runtime resources must be addressed through `${CLAUDE_PLUGIN_ROOT}`; never assume Claude Code's current directory is the Plugin folder.
- See the repo-root `README.md` for the install/operations overview (mostly duplicates `SKILL.md`).

## Architecture (the part that spans multiple files)

The skill is a **pipeline of Markdown modules orchestrated by a workflow**, all parameterized by YAML config, with one Python tool as the sole remote-execution path.

```
SKILL.md (entry) → workflows/precision-diagnosis.md (the canonical loop)
                        │
   ┌────────┬───────────┼───────────┬───────────────┬─────────────┐
   ▼        ▼           ▼           ▼               ▼             ▼
service  test-runner  verifier  aisbench-evaluator  log-analyzer  auto-fixer
 (lifecycle) (curl)   (compare)  (dataset eval)     (classify)    (patch vllm-ascend)
   │        │                       │                  │             │
   └────────┴───────────┬───────────┘                  └──────┬──────┘
                        ▼                                     ▼
              scripts/ssh_utils.py          {model.vllm_ascend_source}  ← only place code is edited
              (sole remote path)              .vllm-ascend/runs/<run-id>/records/fix_N.md
```

Key cross-file concepts:

- **Configuration is resolved project-first.** Real configuration belongs under `${CLAUDE_PROJECT_DIR}/.vllm-ascend/config/`; the bundled `config/*.yaml` files are read-only templates. Every module references placeholders like `{standalone.service_port}`, `{docker.name}`, and `{vllm_ascend_source}`.
- **`scripts/ssh_utils.py` is the ONLY way to touch a remote server.** It reads connection info from the resolved config directory (no `~/.ssh/config`), starts a persistent Paramiko daemon under `.vllm-ascend/runtime/ssh-daemon/`, and reuses it. Passwords are passed in-memory, never on the command line. Idle daemons self-exit after 60 min.
- **Local writes are machine-checked.** `scripts/path_policy.py` creates `.vllm-ascend/runs/<run-id>/` and handles the Skill-scoped `PreToolUse` policy. Plugin files are read-only; generated scripts, downloads, logs, and records stay inside the active run.
- **Node references** resolve a logical name to connection info: `standalone`, `pd-separated.p[0]` / `pd-separated.d[0]` (Prefill/Decode nodes), and `eval` (aisbench machine, defined in `aisbench.yaml`).
- **`test.yaml` is the single source of prompts.** `scripts/generate_curl.py` renders it into the active run's `generated/curl_test.sh` — never hand-write curl, or A/B comparisons break on prompt mismatch.
- **Two deployment modes**, selected by `mode:` in `service.yaml`:
  - `standalone` — prefill+decode mixed on one machine.
  - `pd-separated` — P nodes and D nodes on separate machines coordinated by a proxy (`proxy_script` is **required** in this mode; optional in standalone unless running distributed DP).
- **The precision-diagnosis workflow is a strict loop** (`workflows/precision-diagnosis.md`): start service → health check → test → verify → (analyze log → patch code → restart → retest). Each full iteration is recorded in the active run's `records/fix_N.md` (N increments).
- **Precision debugging methodology** lives in `docs/` (not just examples): inject `DEBUG-CMP-FINAL out_sum=...` per attention layer, run two configs into separate logs (`service_A.log`/`service_B.log`), diff per-layer `out_sum` to find the first divergence (>1% is abnormal). See `docs/dcp2tp4-precision-fix.md` (float32→float64 LSE merge) and `docs/pcp-hybrid-nan-fix.md` (`fill_()` write-back trap).

## Commands

First-time setup (dependencies for `ssh_utils.py` / `generate_curl.py`):

```bash
pip install paramiko pyyaml -i https://pypi.tsinghua.edu.cn/simple
python -c "import paramiko, yaml; print('ok')"   # verify
```

Remote execution (run from the user project; Plugin paths are absolute):

```bash
python3 "${CLAUDE_PLUGIN_ROOT}/scripts/path_policy.py" init-run
python3 "${CLAUDE_PLUGIN_ROOT}/scripts/ssh_utils.py" exec   standalone|pd-separated.p[0]|pd-separated.d[0]|eval "<cmd>"
python3 "${CLAUDE_PLUGIN_ROOT}/scripts/ssh_utils.py" wait   <node> "<logfile>" "<keyword>" --timeout 900 --interval 30
python3 "${CLAUDE_PLUGIN_ROOT}/scripts/ssh_utils.py" upload <node> <local> <remote>
python3 "${CLAUDE_PLUGIN_ROOT}/scripts/ssh_utils.py" download <node> <remote> <local>
python3 "${CLAUDE_PLUGIN_ROOT}/scripts/ssh_utils.py" status <node>
python3 "${CLAUDE_PLUGIN_ROOT}/scripts/ssh_utils.py" stop   <node>
```

Test generation:

```bash
python3 "${CLAUDE_PLUGIN_ROOT}/scripts/generate_curl.py" [--dry-run] [--test-index N] [--prompt-index N]
# → writes <active-run>/generated/curl_test.sh
```

HyperScript installer (repo root, separate from the skill):

```bash
bash HyperScript/HyperScript.sh --install-Codex [DIR]
bash HyperScript/HyperScript.sh --install-vllm-all [DIR]
bash HyperScript/HyperScript.sh --install-vllm [DIR] [REPO] [BRANCH]
bash HyperScript/HyperScript.sh --check-npu | --kill-all | --stop-all-containers | --help
```

## Critical invariants (non-obvious — all called out in SKILL.md / modules)

1. **Only edit `vllm-ascend` code.** Never modify vLLM upstream (`{model.vllm_source}` is read-only reference).
2. **Each `ssh_utils.py exec` call is independent.** Never chain multiple exec calls with `&&`. Inside a single `docker exec ... bash -c '...'`, separate commands with `;` (not `&&` — a failed exit code would skip the rest).
3. **Don't reinstall.** vLLM and vLLM-Ascend are preinstalled in the container; do not `pip install` them without explicit user approval.
4. **Kill by port, not by name:** `fuser -k {service_port}/tcp` (then `fuser {service_port}/tcp` to confirm free). For PD-separated, kill proxy_port too if configured.
5. **Health check (HTTP 200 on `/v1/models`) is a strict precondition** before any inference request or aisbench run. Use the **real bound IP** (`standalone.host`), not `localhost` — the server binds `--host` to an external IP. Inside the container, `unset http_proxy; unset https_proxy` first or you'll get a 504.
6. **Code edits need no reinstall** (host and container share the same vllm/vllm-ascend mount). Edit via `ssh_utils exec cat/sed` or `upload`, then restart the container service. Python-only changes take effect on restart; changes under `csrc/` require recompile+reinstall.
7. **Startup takes 10+ minutes.** `wait` for `Application startup complete` in the log. When diagnosing errors, read **from that keyword onward** — later errors are usually cascades.
8. **Per-iteration record:** every modify→restart→retest cycle writes `<active-run>/records/fix_N.md` (N=1,2,…).
9. **Clear plog before each restart** (`rm -rf /root/ascend/log/debug/plog/*`) so NPU logs match the current run.
10. **Use the Tsinghua pip mirror** (`-i https://pypi.tsinghua.edu.cn/simple`) — default PyPI commonly times out.
11. **Filter logs to one rank** with `dist.get_rank() == 0` to avoid multi-rank spam.
12. **aisbench endpoints are configured in its `config.py`, not via `--host/--port/--model` CLI flags.** aisbench runs on the **eval machine** and targets the **inference machine**.

## Repo hygiene notes

- Bundled `config/*.yaml` files contain placeholders only. Real credentials belong in the project-private `.vllm-ascend/config/`; `init-run` creates `.vllm-ascend/.gitignore` so configs, daemon state, and run artifacts remain untracked. Never put real credentials in the Plugin templates.
