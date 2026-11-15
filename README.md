<p align="center">
  <img src="assets/banner.svg" alt="Clausewitz MCP" width="100%">
</p>

<p align="center">
  <b>Let an AI play Hearts of Iron IV by clicking the same buttons you do.</b><br>
  <sub>Reads the live process. Sends orders through the game's own command queue.</sub>
</p>

<p align="center">
  <img src="https://img.shields.io/badge/Hearts%20of%20Iron%20IV-1.19.5.0-2b3440?style=for-the-badge">
  <img src="https://img.shields.io/badge/platform-Windows%20x64-3d6b4a?style=for-the-badge">
  <img src="https://img.shields.io/badge/MCP-2024--11--05-c8a04a?style=for-the-badge">
  <img src="https://img.shields.io/badge/tools-153-2b3440?style=for-the-badge">
  <img src="https://img.shields.io/badge/dependencies-1-3d6b4a?style=for-the-badge">
  <img src="https://img.shields.io/badge/license-MIT-2b3440?style=for-the-badge">
</p>

<p align="center">
  <a href="#quick-start">Quick start</a> ·
  <a href="#what-it-does">What it does</a> ·
  <a href="#how-it-works">How it works</a> ·
  <a href="#roadmap">Roadmap</a> ·
  <a href="research/FINDINGS.md">RE notes</a>
</p>

---

> ### Fork note — `1.19.5.0` (Windows x64)
>
> This is a fork of [smerss/Clausewitz-MCP](https://github.com/smerss/Clausewitz-MCP).
> It tracks the **Steam Windows build of Hearts of Iron IV 1.19.5.0**
> (`Operation Postern v1.19.5.0.7c6c (c184)`) instead of the Linux build upstream
> targets. The upstream design is unchanged: reads come from the live process, every
> action goes through `CCommandQueue` and is still checked by the game's own
> `CanExecute`.
>
> It adds **46 tools (107 → 153)**, a complete 1.19.5.0 offset/table set, MSVC RTTI, a
> PE command catalogue and a thread-hijack injector. See the
> [`1.19.5.0` release](https://github.com/cjiuf/Clausewitz-MCP/releases) for the full
> list.
>
> **This is a port, not a verified release.** The read side was re-derived field by
> field against the live process; the action side is wired end to end but most tools
> have never been run in a real campaign, so availability is not guaranteed. The Linux
> path (1.19.4.1.99a5) is untouched and still works. See
> [Windows (1.19.5.0)](#windows-11950) below.

---

Paradox never shipped a way to reach live game state. There's no IPC, no scripting hook
that sees the running simulation, no symbols. Just a stripped 68 MB binary.

So this reads the process memory directly, and pushes actions back through
`CCommandQueue`, which is the path your mouse clicks take. That detail matters more than
it sounds. Because the orders go through the real channel, the game still runs
`CanExecute` on every one of them. Hand an army group to a general who isn't in the
current government and you get refused, with the same reason a player would get.

```console
$ python3 clausewitz.py status
1936.01.01.11  --  German Reich (GER)
  factories : 35 civilian  28 military  10 dockyards
  political power: 2   stability: 75%   war support: 30%
  multiplayer: False (1 players)
  at war: False   active land combats: 0
```

### Why not just parse the save file?

Because saves are a snapshot written on autosave, and almost nothing you need for a
decision survives to disk. Organisation this hour. The damage rate inside a battle
that's happening right now. Which province a division is walking toward, and how far
along it is. You can only get that from the heap.

---

## Quick start

```bash
git clone https://github.com/smerss/Clausewitz-MCP
cd Clausewitz-MCP
pip install -r requirements.txt     # numpy, that's the whole list
python3 clausewitz.py doctor
```

On Windows the same lines work in PowerShell (`python` instead of `python3`):

```powershell
git clone https://github.com/cjiuf/Clausewitz-MCP
cd Clausewitz-MCP
pip install -r requirements.txt
python clausewitz.py doctor
```

`doctor` looks for your install in the usual Steam places, in extra Steam library
drives, and in WSL mounts. If it comes up empty, point it yourself:

```bash
export HOI4_PATH="$HOME/.steam/steam/steamapps/common/Hearts of Iron IV"
```

```powershell
$env:HOI4_PATH = "C:\Program Files (x86)\Steam\steamapps\common\Hearts of Iron IV"
```

Reading another process needs permission. On Linux run as root, or do this once per
boot:

```bash
sudo sysctl -w kernel.yama.ptrace_scope=0
```

On Windows, `OpenProcess`/`ReadProcessMemory` need the tool host and the game to run at
the same integrity level — start the MCP server from an elevated shell if the game is
elevated (or neither).

Then add it to your MCP client:

```bash
claude mcp add hoi4 -- python3 /path/to/Clausewitz-MCP/clausewitz.py serve
```

<details>
<summary>Config file instead (Claude Desktop, Cursor, anything with stdio MCP)</summary>

```json
{
  "mcpServers": {
    "hoi4": {
      "command": "python3",
      "args": ["/path/to/Clausewitz-MCP/clausewitz.py", "serve"],
      "env": { "HOI4_PATH": "/path/to/Hearts of Iron IV" }
    }
  }
}
```

More examples in [`mcp/config/`](mcp/config/).
</details>

---

## What it does

153 tools covering focus tree, research, production, construction, politics, decisions,
diplomacy, armies, fleets, templates and save analysis. Most of that is plumbing. The
interesting parts are below.

### Attacks it won't make, and why

Every coefficient here is read out of the shipped game files, `common/terrain/00_terrain.txt`
and `common/defines/00_defines.lua`. Not from a wiki, and not from memory.

```
attack   -> 11529  | 4 divisions from 3 directions (limit 11, width 108/120)
skip     -> 11444  | ratio 0.47 < 1.15 needed
                     (forest -15%, river crossing(small) -30%, entrenchment 9.0 +18%)
skip     ->  9517  | ratio 1.62 < 2.00 needed (urban -30%, entrenchment 3.0 +6%)
```

That river penalty comes from `rivers.bmp`. Crossings aren't stored anywhere convenient,
so they get recovered from the pixel borders between provinces: 4,385 of them, cached on
first run.

### Piling on divisions makes an attack worse

```
COMBAT_STACKING_START   = 5      COMBAT_STACKING_EXTRA   = 3
COMBAT_STACKING_PENALTY = -0.02  (per division over the line, applied to everything)
```

Ten divisions attacking from one direction is a 10% penalty on the whole attack. The fix
is another direction, not another division, since each direction raises both the stacking
allowance and the combat width. The planner works that out before committing.

### Encirclements

One province almost never cuts a front of any width, so the search runs over sets of one,
two and three provinces, looking for the cheapest cut that disconnects enemy divisions
from their capital. It holds the plan across ticks and attacks whichever piece it can
currently reach.

```
encircle -> 334    | POCKET: 2 enemy divisions cut off
                     6 divisions from 3 directions (limit 11, width 108/120)
```

Pockets don't get attacked immediately. Units carry 72 hours of supply, so it waits on
the game's `out_of_supply_days` counter instead of assuming that a graph cut means
starvation. That distinction came from a pocket that was sitting at 108% supply the whole
time, quietly fed by a port.

### Combat is read, never simulated

Reimplementing HOI4's combat maths would drift out of sync with the next patch. The game
already computes, every tick, how much damage each side is taking. The engine reads that,
smooths it, and divides it into each side's organisation pool. Terrain, doctrine, air
support, entrenchment and supply are already inside those numbers because the game put
them there.

### It can sleep

```python
hoi4_wait_for(max_days=30, signals=["focus", "research", "war"])
→ woke_because: ["focus finished/changed: industrial_effort -> None"]
```

Checking every second burns tokens. Never checking misses things. This watches the game
and returns when something needs a decision, and it always returns by `max_days`, because
nothing here is allowed to wait forever.

### Speed

The micro engine runs once per in-game day. A tick is about 46 ms. It used to be a full
second, almost all of it spent reading 26,826 province objects one at a time to work out
who owned what. Province-to-state membership never changes during a game, so that half
now comes from static files and only ownership is read live. 646 ms down to 35 ms,
checked against the old path across all 10,272 provinces with no disagreements.

---

## How it works

```
                    ┌──────────────────────────────────────────┐
   MCP client ──────▶  mcp/server.py      JSON-RPC over stdio  │
                    └───────────────┬──────────────────────────┘
                                    │
                    ┌───────────────▼──────────────────────────┐
                    │  calling/      the memory layer          │
                    │                                          │
   reads  ──────────┼─▶ process_vm_readv   never stops the game│
   actions ─────────┼─▶ CCommandQueue::Post  the player's path │
                    └───────────────┬──────────────────────────┘
                                    │
                    ┌───────────────▼──────────────────────────┐
                    │  Hearts of Iron IV   (stripped, 68 MB)   │
                    └──────────────────────────────────────────┘
```

| directory | what's in it |
|---|---|
| **`mcp/`** | the server and client configs. Thin. |
| **`calling/`** | memory access, the game model, generated tables. This is the part worth reusing if you're building something else. |
| **`calling/tables/`** | plain text reference: verified call addresses, struct offsets, 358 player commands, handle types, terrain constants |
| **`research/`** | scanners, dumps, and [`FINDINGS.md`](research/FINDINGS.md) |

None of it started from a symbol name. Classes came out of Itanium C++ RTTI, 22,479
typeinfo records worth. Function boundaries came from `.eh_frame_hdr`, 128,144 of them.
The struct layouts came from the game's serializer, which writes field names next to
offsets when it saves, so `CArmy+0x428` is called `organisation` here because that's what
Paradox calls it.

After a patch, regenerate:

```bash
python3 clausewitz.py tables
```

---

## Safety

Reads never stop the game. Calls do: on Linux they ptrace-stop all 28 threads and hijack
the main one; on Windows they suspend the owning thread, rewrite its context, run the
call and restore it. Either way anything running in a loop is read-only and only drops
that guard to post a batch of orders inside a single attach.

Three rules live in the code. Each one cost something:

| rule | what it cost |
|---|---|
| Only call addresses whose signature was recovered by disassembly | two guessed virtuals segfaulted the game |
| Check handle types before posting | passing `CCharacter` (73) where the game wanted `CArmyLeader` (4713): one crash, and separately one silent no-op that took a while to notice |
| No injected calls inside a loop | a status call inside a tick loop froze a live multiplayer session, audio and all |

The write-ups are in [`FINDINGS.md`](research/FINDINGS.md). The reasoning is more useful
than the rule.

---

## Windows (1.19.5.0)

The Steam Windows build is **1.19.5.0**, a different binary from the Linux
**1.19.4.1.99a5** one, so it needs its own offset table. That table now exists:
`calling/offsets_win.py`, selected automatically by `calling/offsets.py` on `win32`.

Two layers, and they are not at the same level of proof:

* **Reads** were re-derived field by field and cross-checked against the live process —
  game state, countries, states, buildings, manpower, modifiers, the focus tree, research.
* **Writes** all go through one generic command layer: `calling/pecmd.py` recovers the
  `C*Command` catalogue out of `hoi4.exe`, `calling/cmdwin.py` builds a command through
  its real constructor and posts it to `CCommandQueue`. Only the families listed below
  have actually been fired in a running game.

Working today:

| piece | status |
|---|---|
| `calling/process/windows.py` | `OpenProcess` + `VirtualQueryEx` + `ReadProcessMemory`/`WriteProcessMemory`; 255 MB/s reads |
| MSVC RTTI | `research/windows/rtti_win.py` recovers **19 706 types / 16 567 vtables / 15 141 classes** in 0.5 s |
| gamestate | `g_gameState` → `CCurrentGameState`; `GS_COUNTRIES` = 0x310 (440 countries), `GS_TAG2INDEX` = 0x340 |
| countries | `CCountry` size 0x1710, tag id at +0x08, localised name at +0x10, states at +0x460 |
| states | `CState` owner/controller at +0xC8 / +0xCC |
| political power | `CPoliticalStatus` + 0xE0, verified by differential scan |
| console | all **370** commands decoded from the inline table at RVA 0x33DD968 (stride 0x1C8, same as Linux); command ABI `SResult* fn(SResult*, const CPdxArray<CString>*)` verified live — `pp 7` grants exactly +7 |
| `g.console()` | **works** — `consolecmd.py` has a Windows path, so `pp`, `manpower`, `xp`, `add_equipment`, `add_stability` and the aliases (`ae`, `political_power`, `wp`) all run from Python at 3-10 ms per call |
| tags | `GS_TAG_DB` = gamestate+0x358, an inline `CPdxArray<std::string>` (443 entries incl. the dynamic `D01..D03`), so `country("GER")` / `country("INS")` resolve; `GS_PLAYER_TAG` = 0x520 |
| injection | `calling/process/winject.py` — hijack a running thread, run a stub that sets the arguments and calls the target, restore the saved context. 60 back-to-back `pp` calls verified in one attach; see `research/WINDOWS_1.19.5.md` §12 |
| command catalogue | `calling/pecmd.py` recovers **380 of the 405** `C*Command` classes straight from `hoi4.exe` (vtable → constructor → field offsets, field sizes, which argument each field comes from), cached to `calling/pecmds.pkl`. `calling/cmdwin.py` builds any of them with the real constructor, checks the vtable back, swaps in a no-op destructor and enqueues — the same path a button click takes |
| MCP tools | 29 read tools answer from the live process (`HANDLERS[name](args)` — the same entry point the transport uses) |
| army orders | `hoi4_army_battleplans` builds the real order tree on hoi4.exe: `armyops_win` does create_army / front line / offensive line / execute / arrow `+0x248`, and `game.py` dispatches every army method to it. Verified taking ground: 21 of the Netherlands' 25 provinces, then capitulation (pass `wait_seconds=600` to have the call wait for it). The plan must be built while the game is running — a paused game yields a child with `assigned == 0` (§70.2) |
| peace conference | `hoi4_peace_status` / `hoi4_peace_demands` / `hoi4_peace_finish`: states, puppet and take-navy demands all applied by the engine. Take-navy moved 6 of Denmark's 7 ships — one fleet per action (§69) |
| focus / research writes | `hoi4_set_focus` / `hoi4_drop_focus` / `hoi4_start_research` build the real command objects and queue them. `CDropCurrentNationalFocusCommand` was re-derived from RTTI + its vtable (`0x2A12988` → ctor `0x11CE6F0`, `(cmd, CCountryTag*)`, size 0x30); two set→drop cycles ran clean. `hoi4_research` reports a real percentage and days left: progress is `CTechnology+0x198 / 1e5` research-days against `CTechnologyTemplate+0x3E0 / 1000` total days (§72) |

Still open: `tag <TAG>` takes ~20 s (it reloads the player country) and needs the
long-poll path described in §15, and an offensive line drawn by the tool has not yet
been watched taking ground — the order tree is right, the divisions were still
marching (§70.4). Everything the generic command layer covers but the table above does
not name — production lines, division training, army moves, ideas and laws — is built
and unit-checked but has never been run against a live campaign. Full write-up and the
reproducible probes: [`research/WINDOWS_1.19.5.md`](research/WINDOWS_1.19.5.md) and
[`research/windows/`](research/windows/).

---

## Roadmap

- [x] Live state reads, 153 MCP tools
- [x] Player command channel with the game's own legality checks
- [x] Combat read from the game's damage output
- [x] Terrain, rivers, forts, stacking and combat width in the planner
- [x] Encirclement search over the province graph
- [x] Turn loop with wake-on-event
- [x] **Windows port** (branch `feat/windows-backend`, target build 1.19.5.0): PE backend
      (`ReadProcessMemory` / thread hijack), every offset table re-derived from
      `hoi4.exe`, MSVC RTTI, and a PE command catalogue that recovers the constructor and
      argument layout of 380 `C*Command` classes.
- [ ] **Exercising the port.** 25 command classes carry a hand-checked argument spec;
      the other 355 are recovered but have never been built against a live game.
- [ ] Air and navy micro. The commands are extracted; none have been fired in anger.
- [x] Peace conference automation (Windows backend: `calling/peacewin.py`.
      `hoi4_peace_demands` reproduces the window's select-all + 提交要求 by writing a
      `CPeaceBiddingTurn` into `CPeaceConference+0x1F8`; verified live by taking all five
      Dutch states in 11 game days -- see `research/WINDOWS_1.19.5.md` §62-63.)

---

## Contributing

The Windows side is where the work is: every command family in
`calling/tables/commands.txt` that is not in the verified list above needs a real game
session behind it. [`research/README.md`](research/README.md) explains how the tables are
derived; [`research/WINDOWS_1.19.5.md`](research/WINDOWS_1.19.5.md) has the 1.19.5.0
version, including the command-layer design.

After that, the unwrapped command areas. Division templates (25 commands), air missions
(35), navy (42), intelligence (20). All of them are extracted with their parameter
layouts and none have been tested in-game. `calling/tables/commands.txt` is the map.

Found an offset that moved in a newer patch? Open an issue with the version and what
broke.

---

<p align="center">
  <sub>Not affiliated with Paradox Interactive. Built for single-player;<br>
  multiplayer is detected and the action surface narrows on its own.</sub><br><br>
  <sub>MIT, see <a href="LICENSE">LICENSE</a></sub>
</p>

<p align="center">
  <b>A ⭐ helps, and it's what unlocks the Windows port.</b>
</p>
