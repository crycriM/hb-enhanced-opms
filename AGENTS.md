# hb-enhanced-opms

Hummingbot-based Order & Position Management System — the **HB (Hummingbot) execution layer** of `amm-solution`.
Controllers, executors, and a Gateway bridge wiring the `mm_core` brain ("Keeper") to Hummingbot's execution
body. Translates `ExecIntent` into venue-agnostic order specs, runs AC / Passive-Aggressive algorithms,
collects fill-level PnL / markout / slippage analytics. `hb` == **Hummingbot** everywhere in this repo.

## Role in the monorepo

- `/mnt/data1/cricri/projects/amm-solution` monorepo; depends on `mm_core` and `perp_bot` (pip install -e).
- `opms.controllers.perp_mm_controller` is the **only** module that imports `hummingbot`; everything else stays HB-free.

## Duplicate exec layer — READ THIS

- There are **two** execution layers for the same OPMS role, kept in parity:
  - `dex_executor` — the **native** layer (direct exchange adapters).
  - `hb-enhanced-opms` — this repo, the **Hummingbot-based** duplicate (HB executors + controllers).
- This is a "strangler-fig" migration (see `clmm-animation/docs/hummingbot-migration-plan.md`):
  the native layer stays as the parity oracle; this HB layer supersedes a native module only after
  passing a **parity gate and a live gate** (decision-log diffing). Do not delete native modules early.

## Install / env

- **Hummingbot is NOT pip-installable from PyPI.** Install via conda: `conda env create -f <hummingbot-checkout>/setup/environment.yml`, then `conda activate hummingbot-venv`, `pip install -e .`, and install `mm_core` + `perp_bot` inside the same conda env.
- Compulsory virtual env: all work happens inside the dedicated Hummingbot conda/venv env, never global Python.

## Rules

- Test Driven Design: write tests first, confirm they FAIL, commit, then implement. One task per loop. Update planning docs, commit after completion.
- `pytest` is the test runner. Tests need NO live Hummingbot runtime — `conftest.py` injects stub HB modules via `sys.modules` (enums, events, data types, `ExecutorBase`).
- Keep algorithm math (`_ac_math`) pure: no I/O, no asyncio, no service deps, HB-free, independently testable.
- Editing: prefer `patch` with unique context over `write_file`. Re-read the file first; patch hallucinates old_string often.
- When prompting for selection, list items numbered (1, 2, 3...). Never ask more than one yes/no question.
- Safety-first: reversible actions only (trash > rm). Scientific rigor: verify everything, never guess. Minimalist and lean.
- Direct infra changes ask first, or give the exact sudo command. Communication: concise, terse, English only. "y" = go ahead.