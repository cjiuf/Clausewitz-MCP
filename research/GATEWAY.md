# Gateway design — fixing the four inherited defects

> Written 2026-11-15, branch `feat/windows-backend`, after the 1.19.5.0 release.
> Scope: the defects that came from upstream, and the control layer that makes
> them stop mattering.

---

## 1. The four defects, and what was done

| # | Where | What was wrong | Fix |
|---|---|---|---|
| 1 | `calling/objindex.py` | the cache was keyed on `(build signature, pid)` only. Nothing described the heap, so objects allocated after the scan were invisible and the index aged silently. `refresh_maps()` was only patched into the fallback path. | the cache now records `ts` + a writable-region fingerprint, and refuses to load when either is stale; `refresh()` re-reads only the regions that appeared since the scan; `_maybe_refresh()` tops up at half the ttl and rescans at the full ttl (`ttl_s`, default 600 s) |
| 2 | `calling/ragemicro.py` | `_ordered[division] = (province, kind)` was written on success and never removed. A destroyed division, a retreat, or a refused order left an entry that blocked every later order with the same target — for the rest of the campaign. The key was a raw pointer, which the allocator recycles. | entries are `(province, kind, clock_hours, ok)` with a ttl: 48 h for a success, 6 h for a refusal. Expired entries are dropped on read, `_prune_orders()` clears anything past twice its ttl at the top of every tick, and a refused order is now recorded (short ttl) instead of being forgotten |
| 3 | `calling/commands.py` | on win32 `post()` calls `cmdwin.enqueue`, which hands the object to the queue and returns. `CanExecute` runs later, on the game thread; a refusal leaves no trace, and every tool reported `ok: true`. | a read-back layer: `calling/verify.py` polls an observable with a deadline and separates `verified` / `rejected` / `unverified`. A frozen clock is `unverified`, never a rejection, and `unverified` clears `ok`. Wired into `hoi4_add_idea` and `hoi4_train_divisions`; the rest of the write tools follow the same pattern |
| 4 | `mcp/server.py` | per-tool `limit` existed, but nothing bounded a whole reply, and several tools return every division and every front. | the gateway (below) replaces raw dumps with a capped, aggregated brief; `hoi4_brief` is the only read entry point the model gets |

### 1.1 What `CanExecute` turned out to be

Worth writing down, because it is not where the Linux build put it.

- `CCommand`'s vtable at RVA `0x279F4A0`. Slot `+0x58` is `GetType()` — every
  command overrides it with `mov dword ptr [rdx], <type id>; mov rax, rdx; ret`,
  and the ids match `calling/commands.py` (`CSetNationalFocusCommand` = `0x33BD`).
- Slot `+0x68` is `Clone()`.
- Slots `+0x48` and `+0x50` are **not** a fixed pair: different classes override
  different ones. `CSetNationalFocusCommand+0x50` takes `(country, focus_ptr)` and
  tail-calls a validity helper; `CAddProductionLineFactoriesCommand+0x48` checks the
  line handle and returns a bool; `CMoveCommand+0x48/+0x50` merely forward to the
  embedded `CUnitMoveAction` at `+0x28`. So there is no single "the predicate"
  slot to call generically.
- `CCommandQueue::Post` lives at RVA `0x23F5D40` (read live from
  `queue->vt[0x28]`). It has **no direct `E8` callers** — every call site goes
  through the vtable — so `CCommand::Run` cannot be found by scanning for its
  callees. That is why the gate is read-back rather than a pre-flight call.

`docs`: the `CCommand::Run` route stays open (find it by its callers once a
symbol-bearing build or a debugger trace is available), but nothing depends on it.

---

## 2. The gateway

```
LLM ── 12 tools ──► gateway ──► reflex layer ──► native battle planner
                      │              │
                      │              └── fill fronts, plug gaps, rotate, keep
                      │                  production/construction/research busy
                      └── calling/ engine (reads = memory, writes = CCommandQueue)
```

Twelve tools, not 153:

`hoi4_brief` · `hoi4_advance` · `hoi4_focus` · `hoi4_research` · `hoi4_production`
· `hoi4_construction` · `hoi4_law` · `hoi4_diplomacy` · `hoi4_army_intent`
· `hoi4_spearhead` · `hoi4_encircle` · `hoi4_note`

### 2.1 Read side: theatre aggregation + delta

- One full brief, then deltas. Scalars carry thresholds (PP 5, stability 2 %,
  war support 2 %, manpower 5 000, factories 0.5, fuel 250, convoys 5); queues
  diff by shape; events are always sent in full.
- Fronts are ranked by salience — losing ground > pocket forming > capital
  threatened > supply below 50 % > attacking — the top four are expanded, the
  rest fold into one line that still reports whether anything in the fold is in
  trouble.
- Every brief is token-counted; over budget, quiet theatres are dropped first,
  then thresholds are raised.

This is the rule set from `Xocas12/hoi4-agent-harness`'s `observation/delta.py`
and `observation/fronts.py`, applied to memory reads instead of save files.

### 2.2 Write side: reflex layer + read-back

Every action declares four things:

```
preflight  - what must be true before building (handle resolves, pointer readable)
apply      - build the command, enqueue it
verify     - a predicate over live state, plus a deadline in game hours
cost       - how many game days the action needs to show up
```

The result is `verified` / `rejected` / `unverified`, never a bare `ok`. The
reflex layer runs the same actions on a timer without the model, which is what
keeps fronts filled while the model thinks about 1939.

---

## 3. The adapter

`calling/harness_adapter.py` implements the harness's `GameAdapter` contract on
top of this engine: `read_state()` fills its `GameState` from `game.overview()`,
`apply()` maps a small action set onto the command layer, `pause/resume` use
`session.pause()` (the idler byte, no hotkey), and `advance()` runs the clock by
polling the date.

Why this and not the harness's own adapters: its savegame reader is exact but
dead, its screen reader is live but blind, and its input driver needs a working
mouse — which we do not have. Memory is live *and* exact, and `session.pause()`
removes the last UI dependency.

The delegation seam (`delegate_army_to_ai`) does **not** need the harness's mod:
the native operational AI in single-player *is* the battle planner, and
`hoi4_army_battleplans` already drives it through `CFront` +
`COrderExecuteCommand` (verified: 21 of the Netherlands' 25 provinces, then
capitulation). The mod's `ai_strategy` flags are how the harness reaches the
*country* AI without memory access; we do not need that detour.

---

## 4. What is tested, and what is not

| Piece | State |
|---|---|
| `objindex` cache invalidation (ttl, region fingerprint, partial load) | unit-tested with a fake process: fresh loads, expired rejects, changed heap rejects |
| `ragemicro` order expiry (success ttl, refusal ttl, prune) | unit-tested against the real methods |
| `verify.poll` / `attach` (frozen clock vs refused vs verified) | unit-tested |
| `harness_adapter` state mapping + action dispatch | smoke-tested with a fake game; **not** run against the harness |
| gateway brief / reflex loop | not built yet |

The game was sitting in the main menu during this work, so none of it has been
exercised on a live campaign.
