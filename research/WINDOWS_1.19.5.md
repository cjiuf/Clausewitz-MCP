# HOI4 Windows 1.19.5.0 — offset recovery log

Target: `D:\SteamLibrary\steamapps\common\Hearts of Iron IV\hoi4.exe`
`Operation Postern v1.19.5.0.7c6c (c184)` — 56 535 160 bytes, PE32+, preferred base `0x140000000`,
`SizeOfImage 0x38A3000`.
Sections: `.text` 0x2783D48 / `.rdata` 0x977F92 / `.data` 0x598754 (raw 0x2E0200) / `.pdata` / `.rsrc` / `.reloc`.

Everything below was measured on a live 1936 campaign (Germany, 1 Nov 1936), PID 42284,
load base `0x7ff698620000`, with the read-only backend in `calling/process/windows.py`.

## 1. RTTI (the key that unlocks everything)

**MSVC RTTI is NOT stripped.** 21 196 `.?AV…@@` name strings, 19 706 valid
`RTTITypeDescriptor`s, 16 567 `RTTICompleteObjectLocator`s, 16 567 vtables,
**15 141 classes**. `research/windows/rtti_win.py` builds `research/windows/rtti_win.pkl`
(`{"classes": {name: [vtable_rva]}, "vt": {vtable_rva: name}, "pe": …}`) in 0.5 s.
This is the Windows counterpart of `calling/rtti.pkl` (18 955 vtables on Linux).

Scanner recipe (all RVAs inside the image; loader relocates the vtable[-1] slot):

```
TypeDescriptor        : {void* pVFTable; void* spare; char name[];}          name at +0x10
RTTICompleteObjectLocator (x64):
    +0x00 u32 signature   (== 1)
    +0x04 u32 offset
    +0x08 u32 cdOffset
    +0x0C u32 pTypeDescriptor   (RVA)
    +0x10 u32 pClassDescriptor  (RVA)
    +0x14 u32 pSelf             (RVA of the locator itself)  <-- self-check
vtable                : qword at vtable-8 == image_base + col_rva
```

## 2. Globals

| symbol | RVA | how it was found |
|---|---|---|
| `G_GAMESTATE` | **0x33DD6B0** | only qword in the whole image pointing at the live `CCurrentGameState`; 1 hit |

`g_gameState` is a `CCurrentGameState*` (vtable `0x279F168`, second vptr at object+0x08
`0x279F1C0`). `CGameState` itself is only a base — no object has its vtable, which is why
scanning for `.?AVCGameState@@` (0x279F0F0 / 0x279F148) returns 0 hits.

## 3. CGameState layout — identical to Linux

| field | offset | value observed |
|---|---|---|
| `GS_COUNTRIES` | **0x310** | `CPdxArray<CCountry*>` data=`0x19eadc4cb30` cap=440 size=440, all 440 entries valid |
| `GS_TAG2INDEX` | **0x340** | `CPdxArray<int32>` cap=474 size=443, **identity map** (`[i]==i`) in this build |
| `GS_PLAYER_TAG` | 0x518 | reads 15 (= Spain) — **not** the player tag on this build, see §6 |

`CPdxArray<T>` = `{ T* data; int32 capacity; int32 size; IAllocator* alloc; }` (0x18) —
same as Linux. The default allocator is the `CPdxNewDeleteAllocator` singleton at
`image_base + 0x30FD000`.

## 4. CCountry

| field | offset | notes |
|---|---|---|
| vtable | 0x00 | `.?AVCCountry@@` → **RVA 0x2864730** |
| tag id (u32) | **0x08** | Germany 1, UK 2, USSR 3, France 5, Spain 15, Italy 16 … |
| name (`std::string`) | **0x10** | **localised** ("德意志第三帝国"), so ASCII scans for "German Reich" find nothing |
| adjective (`std::string`) | **0x30** | "德意志" |
| player flag (u32) | **0x120** | 1 only for the human country (and dummy country 0) |
| **sizeof** | **0x1710** | objects are allocated at a 0x1710 stride |

`std::string` on MSVC = `{ char buf[16]; size_t size; size_t cap; }` (0x20, SSO for len ≤ 15).
`mem.Proc.stdstring()` (libstdc++ layout) returns garbage — a Windows variant is needed.

### 4.1 CCountry sub-object pointers (all RTTI-verified)

```
+0x070..0x088  CNameGroupTracker*  x4
+0x160         CNavyTheater*
+0x218         CVariables*
+0x228         CCountryAI*
+0x230         CFlagManager*
+0xE60         CCountry*            (overlord / original country)
+0xF60         CTechnologyStatus*
+0xF68         CProductionStatus*
+0xF70         CDeploymentStatus*
+0xF78         CReinforcementStatus*
+0xF80         CArmyUpgradesStatus*
+0xF88         CDiplomacyStatus*
+0xF90         CPoliticalStatus*    <-- political power lives here
+0xF98         CLogisticsStatus*
+0xFA0         CDecisionStatus*
+0xFA8         CProgramStatus (NProject)
+0xFB0         CCustomizableBuildingCollection*
+0xFC0         CIntelligenceAgency*
+0xFC8         CLoopHistory*
+0xFD0         CCountryOccupationStatus*
+0xFD8         CCountryCollaborationStatus*
+0xFE0         CCountryReportsManager*
+0xFE8         CCountryIntel*
+0xFF0         CCountryCharacters*
+0xFF8         CIncomingDiplomaticActionStatus*
+0x11F8        CCountryResources*
+0x1310        CPdxArray<CNuke*>   cap 2 size 2
+0x1680        CPdxArray<CProject*> cap 63 size 49
```

This "one pointer per status block" shape is **new** relative to the Linux build, where
`CCountryEconomy*` / `CCountryPolitics*` hang off the country at 0xF40 / 0xF68.

## 5. CPoliticalStatus

| field | offset | evidence |
|---|---|---|
| vtable | 0x00 | `.?AVCPoliticalStatus@@` |
| **political power** | **0xE0** | int64 fixed(1e5); differential scan over 45 s: 3 253 600 → 3 486 000 (+232 400 ≈ +2.3 PP) |
| command power? | 0x560 | int64 fixed(1e5), 1 715 480 → 1 837 300 (+121 820) |

`0xE0` is the **same offset as Linux `PO_POLITICAL_POWER`**, so at least the political
sub-object kept its internal layout.

## 6. Open items

* `GS_PLAYER_TAG` (0x518) does not hold the player on this build — reads 15 while the player
  is Germany (1). `CCountry+0x120` is the reliable marker for now. Needs one tag-switch
  experiment (console did not open: backtick / VK_OEM_3 / SendKeys all ignored).
* Stability / war support are **not** at `CCountry+0x10A8/0x10B0` (those are pointers here).
  They are somewhere in `CPoliticalStatus` (or computed) — not yet found.
* `CState` (`.?AVCState@@` → vtables 0x29B4D58 / 0x29B4DA8) — states array offset inside
  CCountry not yet located; the `CO_STATES = 0x460` Linux offset is unverified.
* Tag string ↔ tag id table (`G_COUNTRYTAG_DB` on Linux) not yet located.

## 7. Step 3 — states, percentages, tags, console table

### 7.1 CGameState arrays (all alloc-tagged, alloc == base+0x30FD000)

| offset | content | observed |
|---|---|---|
| 0x2B0 | provinces | cap/size 13 414 |
| **0x2C8** | `CPdxArray<CState*>` | cap/size **1 082** (1 081 live CState objects) |
| 0x310 | `CPdxArray<CCountry*>` | 440 |
| 0x328 | major countries | cap 9 size 7 |
| 0x340 | `GS_TAG2INDEX` | 443 (identity) |
| 0x640 | `CPdxArray<CArmy*>` | 42 / 35 |
| 0x6F0 | `CReferencedDivisionTemplate` | 711 / 705 |
| 0x708 | `CEquipmentVariant` | 5 395 / 4 513 |

### 7.2 CCountry state lists — `CO_STATES` = 0x460 (same as Linux)

Three consecutive `CPdxArray<CState*>`:

| offset | meaning | GER |
|---|---|---|
| **0x460** | owned states | cap 28 size 24 |
| 0x478 | second state list | cap 28 size 24 |
| 0x4A8 | third state list | cap 28 size 24 |

Sanity check across countries: GER 24, ENG 94, SOV 152 owned states — plausible.

Other CCountry arrays: 0x168 `CTheatre*`, 0x1B8 `CReferencedDivisionTemplate*`, 0x278 `CFleet*`,
0x290 `CArmy*` (42/30 — the 30 starting divisions), 0x5D0/0x5E8 `CModifier*`.

### 7.3 CState

| field | offset |
|---|---|
| vtable | 0x00 (`0x29B4D58`; second vptr at +0x08 `0x29B4DA8`) |
| id (u32) | 0x10 |
| **owner country index** | **0xC8** |
| **controller country index** | **0xCC** |

Verified by requiring `field == 1` for all 24 German states, `== 2` for all 94 British,
`== 3` for all 152 Soviet ones — the only offsets satisfying all three are 0xC8 and 0xCC.

### 7.4 Stability / war support (candidate)

`CCountry + 0x10D0` and `+0x10D8`, int64 fixed(1e5), 8 bytes apart — the same shape as
Linux `CO_STABILITY_BASE = 0x10A8` / `CO_WARSUPPORT_BASE = 0x10B0`.

```
idx  0x10D0  0x10D8
  1   50.0    50.0     GER
  2   70.0    30.0     ENG
  3   59.8    10.0     SOV
  5   50.0    30.0     FRA
  7   65.0    75.0     BEL
```
These are the **base** values; the top bar shows base+modifiers (81 % / 35 % for Germany),
so they are not yet proven to be the exact fields the UI reads.

### 7.5 Country tag table

A contiguous `std::string[443]`, stride **0x20**, index 0 = `'---'`, 1 = `GER`, 2 = `ENG`,
3 = `SOV`, 4 = `SWE`, 5 = `FRA`, … 15 = `SPR`, 16 = `ITA`, 17 = `ROM`, 18 = `YUG`, 19 = `SER`.

**tag id == country index == GS_TAG2INDEX[i]** in this build.

It is a heap array with **no reference from the image**, so there is no static global to read.
Discovery routine instead: find the SSO pattern for `GER` (32 bytes:
`"GER" + 13*0x00 + u64(3) + u64(15)`), then accept the address `a` where
`msstr(a)=='GER' and msstr(a+0x20)=='ENG' and msstr(a+0x40)=='SOV'`.
905 raw `GER` hits → exactly one passes the 3-in-a-row test.

### 7.6 Console command table — **identical layout to Linux**

| field | value |
|---|---|
| array header (`CPdxArray<CConsoleCmd>`) | **RVA 0x33DD968** (directly in `.data`, not a pointer) |
| commands | **370**, all function pointers inside `.text` |
| `CMD_STRIDE` | **0x1C8** |
| `CMD_NAME` | 0x08 |
| `CMD_ALIAS_COUNT` | 0x28 |
| `CMD_ALIASES` | 0x30 (up to 3 × `const char*`) |
| `CMD_FUNC` | 0x68 |

Sample: `tag` 0x2A4570, `annex` 0x256100, `pp` 0x285FF0 (alias `political_power`),
`manpower` 0x281850, `whitepeace` 0x2AFEF0 (alias `wp`), `instant_wargoal` 0x27A8B0,
`observe` 0x285120 (alias `spectator`), `research` 0x294310.

## 8. Step 4 — thread-hijack injection

Implemented in `calling/process/winject.py`, wired into `calling/process/windows.py`
(`Injector` now delegates to it, same surface as `process/linux.Injector`).

### 8.1 Verified

| test | result |
|---|---|
| `kernel32!GetCurrentProcessId()` inside the game | returns the game's own PID (52068) |
| `kernel32!GetTickCount64()` inside the game | 853 806 671 ms |
| repeated calls + `detach()` | process stays alive, no leaked thread |
| hijacked thread | `SuspendThread` previous-count must be 0; threads already suspended by the job system are skipped |

Working thread picking: sample `GetThreadTimes` twice 150 ms apart, take the first
thread with a non-zero CPU delta (fallback: highest total CPU), then confirm
`SuspendThread` reports a previous suspend count of 0.

### 8.2 Two crashes, both understood

1. **Misaligned CONTEXT restore.** `saved = raw.raw[:CTX_SIZE]` copies from the
   buffer base, but `GetThreadContext` was given the 16-byte **aligned** address
   inside it. Restoring a shifted context corrupted every register → instant crash.
   Fix: `ctypes.string_at(ctx.addr, CTX_SIZE)`.
2. **Calling a game function at the main menu.** `test` (0x2A5070) executed and
   killed the process, because console commands touch the gamestate, which does not
   exist before a campaign is loaded. Game functions must only be called in-game.

### 8.3 Console command ABI (from disassembly)

```c
SResult *Cmd(SResult *out, const CPdxArray<std::string> *args);
```

* `SResult` = `{ bool ok; std::string message; }` = 0x28
  (bool at 0, message buffer at +0x08, size at +0x18, capacity at +0x20)
* `CPdxArray<T>` — size of the array is read at **+0x0C**, i.e. `{data, cap, size, alloc}`
* `rcx = out`, `rdx = args`; a command with no arguments still receives a valid empty array

### 8.4 Scratch page layout used by the injector

```
0x000  stub    48 A3 <res>       mov [res], rax        ; captures the return value
                48 B8 <flag>     mov rax, flag
                C7 00 01..       mov dword [rax], 1
                EB FE            jmp $
0x040  res      u64 return value
0x048  flag     u32 done flag
0x100  stack    0x800 bytes; entry RSP % 16 == 8, [rsp]=stub, shadow at +8, args at +0x28
0x1000 data     caller-provided structures (SResult / CPdxArray / std::string)
```

### 8.5 Live results (2026-11-13)

```
GER PP before 74.368
call1 ok=1  msg="Granted 999 political power to 德意志第三帝国"   PP=1073.368
call2 ok=1  msg="Granted 999 political power to 德意志第三帝国"   PP=2000.000   (cap)
call3 -> process died
```

* **The injection really mutates game state** — `pp` executed inside the game and granted
  political power to the player country (Germany), twice in a row.
* **`pp` ignores our argument** (always grants 999) → the console argument array is probably
  not `CPdxArray<std::string>` but `CPdxArray<CString>`, whose element layout differs.
  Needs a look at the copy ctor at `0x14011F770` before any command that takes parameters
  (`tag`, `add_equipment_to_stockpile`, …) can be used.
* **Stability**: the 3rd call killed the process. `game.log` ends with
  `Start RestoreDeviceObjects / End RestoreDeviceObjects`, i.e. a D3D device reset, which
  points at the freeze/thaw of the render thread rather than at the call itself.
  Current model (`_freeze()` all threads → hijack the window thread → `_thaw()`) is too
  invasive for repeated use.

### 8.6 Options for a stable design

1. **Persistent worker thread** — hijack once, let the stolen thread run a loop that waits on
   a request slot in the scratch page; later calls only post work + wait for the flag.
   No repeated freezes, no repeated context surgery.
2. **Pause the game first, do not freeze anything, hijack an idle game thread** — cheapest,
   but thread choice is the thing that crashed the very first attempts.
3. **Call `CConsoleCmdManager::Execute`** once with the whole command line instead of the
   individual `CMD_FUNC` entries — one call site, args parsed by the game itself
   (Linux equivalent `FN_CONSOLE_EXECUTE = 0x344B060`).

## Reproduce

```powershell
python research\windows\rtti_win.py      # 0.5 s  -> research/windows/rtti_win.pkl   (19 706 types / 16 567 vtables)
python research\windows\win_gs.py        # gamestate chain + 440-country table
python research\windows\wmap.py          # CCountry structural map (pointer fields + arrays)
python research\windows\wstate2.py       # alloc-tagged CPdxArray enumeration (states!)
python research\windows\wstate3.py       # CState owner/controller + percentage fields
python research\windows\wtag.py          # country-tag std::string table
python research\windows\wconsole.py      # find the CConsoleCmd array in .data
python research\windows\wconsole3.py     # decode all 370 console commands
python research\windows\wdiff.py         # differential field scan (finds ticking values)
```

## 9. Console manager - static map (basis for the Execute route)

`research/windows/wxref.py` scans `.text` for RIP-relative `lea`/`mov` whose displacement lands on
the console manager global `0x33DD968`. **13 references**, all in `.text`:

```
0x0013CCC   0x0246E70   0x024945F  0x024947D  0x024949B  0x02494B9
0x02B12D5   0x02B12F3   0x02B1311  0x02B132F  0x02B1353
0x02776A34  0x02776A69
```

| address | what it is |
|---|---|
| **0x246E70** | `GetConsoleCmdManager()` - `lea rax,[rip+0x3196AF1]; ret`, i.e. it just returns `0x33DD968`. Free to call, no arguments. |
| **0x2776A30** | ~~command lookup~~ **corrected, see §10: this is `CConsoleCmdManager::Clear()`** - it walks the array (stride 0x1C8) calling each element's destructor and then frees the buffer through the allocator. It never compares names. |
| 0x13C90.. | command **registration/init**: `mov edx,0x1C8` (element size) + `mov r8d,0x157` (reserve 343) + `call 0x25A06E4`, right after the manager is touched |
| 0x2B12D5.. | five consecutive refs - most likely the static command definition table that gets copied in |

Next step for the `Execute` route: find the function that takes `(SResult*, CPdxArray<...>*)`
and calls the lookup at 0x2776A30 - that is `CConsoleCmdManager::Execute`, the Windows
equivalent of the Linux `FN_CONSOLE_EXECUTE = 0x344B060`.

---

## 10. Console command ABI (verified live)

`G_CONSOLE_MANAGER = 0x33DD968` is an inline `CPdxArray<CConsoleCmd>` (data at +0,
capacity at +8, **size at +0x0C**, allocator at +0x10), element stride **0x1C8**.
`CConsoleCmd` layout (matches Linux):

| field | offset |
|---|---|
| `CString name` | 0x08 (MSVC `std::string`, 0x20 bytes) |
| alias count | 0x28 |
| aliases | 0x30 |
| **implementation pointer** | **0x68** |

### Signature

Every command is

```c
SResult*  fn(SResult* out /*rcx*/, const CPdxArray<CString>* args /*rdx*/);
```

`SResult` is `{ bool ok; std::string msg; }` = **0x28 bytes** (`ok` at +0, `msg`
buffer at +8, length at +0x18, capacity at +0x20).  Windows has no hidden
return pointer because the result is written through `out`.

Confirmed directly from RTTI, which also settles the argument element type:

```
?$CPdxArray@VCString@@H@
?$_Func_impl_no_alloc@P6AXAEAUSResult@CConsoleCmd@@AEBV?$CPdxArray@VCString@@H@@...
                                    ^^^^^^^^^^^^^^^^^^^^^^^ ^^^^^^^^^^^^^^^^^^
                                    SResult& (nested in CConsoleCmd)   CPdxArray<CString>
```

so the arguments are `CString`, **not** `std::string` by identity - but `CString`
*is* the MSVC `std::string` layout in this build, which is what matters:

```
0x254ECB0  CString::c_str()      cmp qword [rcx+0x18], 0xF   ; capacity <= 15 -> SSO
                                 jbe done
                                 mov rcx, [rcx]              ; else heap pointer
                          done:  jmp 0x25DDAB8               ; atoi()
```

`pp` (= `0x285FF0`) reads its argument exactly like that:

```
28603E  cmp  dword [rdx+0xC], 1      ; args->size  (note: +0xC, not +0x8)
286042  jle  28605E                  ; 0 args -> use the default
286044  mov  byte [r14], al          ; too many args -> ok = false
286047  ...  "Invalid arguments count."
28605E  mov  ebx, 0x5F45A60          ; 999 * 100000 (fixed point 1e5)
286063  jne  286077                  ; size != 1 -> default
286065  mov  rcx, [rdx]              ; element 0
286068  call 0x254ECB0               ; CString::c_str()
28606D  movsxd rcx, eax
286070  imul rbx, rcx, 0x186A0       ; * 100000
```

### Verified live (1.19.5.0, pid alive, one attach)

| command | message | political power |
|---|---|---|
| - | - | 74.368 |
| `pp 0` | "Granted 0 political power to 德意志第三帝国" | 74.368 |
| `pp 7` | "Granted 7 political power to 德意志第三帝国" | 81.368 |

`pp` is also a ready-made **fixed-point probe**: `CPoliticalStatus+0xE0` is an
`int64` in 1e5 units, so `pp n` must move it by exactly `n*100000`.

### Console manager map

| address | what it is |
|---|---|
| **0x246E70** | `GetConsoleCmdManager()` - returns `0x33DD968`; no arguments, safe canary call |
| **0x2494D0** | `CPdxArray<CConsoleCmd>::append(array)` - used by every registration site (12 callers) |
| **0x249450** | registers four built-in command tables (4 × append) |
| **0x2B10AF** | registers the scripted `console_commands` tables (5 × append) |
| **0x2776A30** | `Clear()` (see above) |

There is **no name lookup that walks the array** - only 13 code references to
`0x33DD968` exist and all of them are registration/teardown.  `0x2537390`
iterates the manager array (stride 0x1C8 again) and pushes every entry into a
separate 0xE0-byte object reached through the global pointer `0x368F540`, so
name resolution happens on that copy, not on the global array.

## 11. Injector status

The ABI is solved; the **hijack** is not yet reliable.  What is established:

* Borrowing an **idle** thread does not work: a thread blocked in
  `NtWaitForSingleObject` is never scheduled, so it sits at the injected entry
  point (`RIP == func_addr`) until the timeout.  Only a *running* thread can be
  hijacked, which in practice means the window/render thread.
* Freezing every thread before the call does not help (99 threads here).
  Suspending only the target thread does not help either.
* A **control-area zeroing bug** in `call()` used to wipe the whole scratch page
  before every call, which silently zeroed any argument payload parked above
  `0x1000`.  That is why `pp <n>` appeared to ignore its argument and always
  granted 999 (the `size == 0` default).  Now only the stub/result/flag/stack
  area is cleared (`CONTROL_SIZE = 0xA00`) and payloads live at `PAYLOAD_OFF`.
* `SetThreadContext` with our offsets is correct: a round-trip on a scratch
  thread of the same process returns every register unchanged, and a read-back
  immediately after setting the hijack context shows `RIP/RSP/RCX/RDX` exactly
  as requested.
* Nevertheless `pp` as an injected call still faults at **`hoi4.exe+0x28601D`**,
  the very first store of the function (`mov byte ptr [rcx], al`, i.e. writing
  `out->ok`), with a first-chance `ACCESS_VIOLATION` on the hijacked thread.
  Under a debugger the register set at fault reads `RCX = func_addr`,
  `R8 = injected RSP`, `RSP = RSP-0x108` - i.e. the *stack* part of the context
  landed but at least `RCX` did not, even though the read-back said it had.
  Next probe: single-step the first instructions of `pp` with the debugger to
  see where `RCX` is overwritten.

## 12. Root cause and fix: build the call in code, not in the context

Two independent bugs were behind everything in §11.

**1. The argument payload was being wiped.** `call()` cleared the whole scratch
page before each call, so a payload parked above it read back as all zeros and
every command saw `args->size == 0`.  That - not the element type - is why
`pp <n>` always granted the default 999.  Fixed by clearing only
`[0, CONTROL_SIZE)` and putting payloads at `PAYLOAD_OFF`.

**2. `SetThreadContext` does not reliably deliver the argument registers.**
Single-stepping the injected call with the trap flag set shows it exactly:

```
after SetThreadContext: RIP=0x7ff6988a5ff0 RSP=0x2ac93f307f8
                        RCX=0x2ac93f31000 (want 0x2ac93f31000)   <- readback agrees
                        RDX=0x2ac93f31040 (want 0x2ac93f31040)
step 1  RIP=0x7ff6988a5ff0 RCX=0x7ff6988a5ff0 RDX=0x0 R8=0x2ac93f307f8
```

At the very first instruction the registers no longer match: `RCX` holds the
entry `RIP`, `R8` holds the entry `RSP`.  The *control* classes (RIP/RSP) land,
the *integer* classes do not, and `SetThreadContext`'s own read-back cannot see
the difference.  A round-trip on a scratch thread of the same process works
perfectly, so the offsets are right - the loss happens on the way into a
suspended game thread.

The fix is to stop asking the context to carry arguments.  The stub in scratch
loads them itself and calls the target:

```asm
sub  rsp, 8                 ; ABI: entry RSP % 16 == 8, so pre-call must be 0
mov  rcx, <a0>
mov  rdx, <a1>
mov  r8,  <a2>
mov  r9,  <a3>
mov  rax, <function>
call rax
add  rsp, 8
mov  [res], rax             ; completion flag for the poller
mov  rax, <flag>
mov  dword [rax], 1
jmp  $
```

Only `RIP` and `RSP` go through the context now - the two that work.  A
third bug surfaced while doing this: the stub is 0x58 bytes and originally sat
at 0x00 with `RES_OFF = 0x40`, so the result/flag writes overwrote the tail of
the code; and the call stack (0x800) grew down onto the stub.  The layout is now
stub at 0x00 (below the stack), result/flag at 0x60/0x70, a 32 KiB stack at
0x1000, and payloads at 0x9200.

### Verified

```
$ python wstress.py 60 0.02
scratch 0x2d9f7a50000  payload 0x9200
after 60 calls: bad=0  dirty=False  calls=61
detached cleanly
```

60 consecutive `pp 0..4` calls in one attach, every one asserting the returned
message is exactly `Granted <n> political power to 德意志第三帝国`.  No crash,
no dropped call, clean detach.  Before the fix the process died on call 1.


## 13. Console commands from Python (verified end-to-end)

`calling/consolecmd.py` now has a Windows path, so `g.console("pp 7")` and the
`hoi4_debug_console` / `hoi4_debug_list_commands` tools work on `hoi4.exe`.
Nothing above `consolecmd` changed: the Linux branch is untouched.

### Where the table lives

Linux had to *scan* writable memory for the first entry, because 1.19.4.1 has no
usable global. 1.19.5.0 does:

```
G_CONSOLE_MANAGER = base + 0x33DD968      CPdxArray<CConsoleCmd>, INLINE (not a pointer)
    +0x00 data  = 0x...                   heap array
    +0x08 cap   = 514
    +0x0C size  = 370
    +0x10 alloc = base + 0x30FD000        the CPdxNewDeleteAllocator singleton
```

So the lookup is O(1) instead of a multi-second scan, and it cannot go stale:
the pointer is re-read on every `table()` call (only the entry index is cached,
keyed on pid + base + count).

Entry layout is identical to Linux (`STRIDE 0x1C8`, name at +0x08, alias count
+0x28, alias `char*[]` +0x30, handler +0x68), but the name is an **MSVC
`std::string`** (SSO 15 bytes, length +0x10, capacity +0x18), so
`Proc.stdstring()` has to be the MSVC branch. 370 entries, all 370 with a
readable name, all 370 handlers inside the image, 82 with aliases:

```
[  0] test              fn=+0x2a5070  alias=[]
[ 17] whitepeace        fn=+0x2afef0  alias=['wp']
[ 87] annex             fn=+0x256100  alias=[]
[ 94] manpower          fn=+0x281850  alias=[]
[169] spawn             fn=+0x2a1190  alias=[]
[208] add_equipment     fn=+0x24fc60  alias=['ae']
[209] pp                fn=+0x285ff0  alias=['political_power']
```

One oddity worth knowing: the game registers **two** entries named
`test_naval_move_danger` (indices 32 and 33, handlers +0x2a5ad0 / +0x2a5840).
`command_catalog()` is a dict, so it reports 369 commands for 370 rows. That is
the game's own table, not a parsing bug.

### Where the arguments come from

`Injector.prepare_console_args()` builds `SResult` + `CPdxArray<CString>` + the
strings **inside the injector's scratch page** and the stub loads the two
pointers. No `operator new`, no per-argument heap block, nothing to leak, and a
long argument cannot smash a neighbouring allocation (the Linux version once
died on `add_equipment 600 transport_plane_equipment_1` for exactly that
reason). Arguments over 15 bytes get a real buffer parked after the slot array.

### Verified live

```
$ python wcon.py "pp 3" "pp 1 2 3" "pp" "manpower" "manpower 1000" "xp 10"
pp 3          ok=True   Granted 3 political power to 德意志第三帝国
pp 1 2 3      ok=False  Invalid arguments count.
pp            ok=True   Granted 999 political power to 德意志第三帝国
manpower      ok=True   Now we have manpower!
manpower 1000 ok=True   Manpower changed by changed by 1000
xp 10         ok=True   Granted 10.000 XP of all kinds to 德意志第三帝国

$ python wcon.py "add_equipment 600 transport_plane_equipment_1" "ae 100 infantry_equipment_1" "add_stability 5"
add_equipment 600 transport_plane_equipment_1 ok=True  Added 600 of transport_plane_equipment_1 ...
ae 100 infantry_equipment_1                   ok=True  Added 100 of infantry_equipment_1 ...   (alias)
add_stability 5                               ok=True  Added 5 stability ...
```

Command latency is 3-10 ms after the first call (`attach` + `VirtualAllocEx`);
the first call in a process costs ~0.7 s.

## 14. The tag database is inside CGameState

`G_COUNTRYTAG_DB` has no image global on this build, and the tag strings sit on
the heap with no pointer to them from `.data` — the earlier note said "must be
found by scanning". That was wrong. It is an **inline** `CPdxArray<std::string>`
in the gamestate, immediately after the tag2index array:

```
GS_TAG2INDEX = 0x340   data=0x...  cap=474 size=443 alloc=base+0x30FD000
GS_TAG_DB    = 0x358   data=0x...  cap=474 size=443 alloc=base+0x30FD000
```

`tags[0] = "---"`, `[1] = "GER"`, `[2] = "ENG"`, `[3] = "SOV"`, and 440..442 are
the **dynamic** tags `D01 D02 D03`, which the static 440-entry string table the
scanner used to find does not contain. Size 443 == tag2index size 443, so unlike
Linux there is no padding: every tag id has a name.

`api._load_tags()` now branches on `sys.platform` and reads that inline array.

```
GER  ptr=0x2db53b80e10  德意志第三帝国
ENG  ptr=0x2db53b93470  大不列颠
SOV  ptr=0x2db53c3a020  苏维埃社会主义共和国联盟
HOL  ptr=0x2db5422d060  荷兰
INS  ptr=0x2db57bf3030  荷属印度
```

### Player tag: 0x520, not 0x518

`GS_PLAYER_TAG = 0x520`. Verified by doing what a test can actually falsify:

```
before    0x518=15  0x520=1     (playing GER)
tag ITA   0x518=15  0x520=16
tag GER   0x518=15  0x520=1
```

0x518 sits at 15 (SPR) through all of it — it is not the player. `0x51C` is also
not (it reads 440, which looks like a valid index and is not).

## 15. Injector: a timeout must never leave the game frozen

Three defects found this round, all in `winject.py`.

**1. A timed-out call left the render thread suspended.** `call()` did
`SuspendThread(h)` before checking the done flag, then raised on the
"not our stub any more" path without resuming. `detach()` only resumes what is in
`self.frozen`, which that path had already cleared. Measured afterwards with
`SuspendThread` (which returns the *previous* count):

```
tid 43964 previous suspend count = 1     <- the window thread, still frozen
```

The game's simulation threads keep running and every read still works, so this
looks like "the game is fine, the UI is just unresponsive". It is not.

**2. A per-injector `dirty` flag is not enough.** Every console command attaches
a fresh injector, so after a timeout the next command happily hijacked the same
thread *again while it was still inside the first call*. That nested two stubs
and destroyed the thread's stack (the render thread ended up spinning at the
first byte of a leaked scratch page; the process had to be killed). There is now
a module-level `_DIRTY_PIDS` set plus `winject.clear_dirty(pid)`.

**3. Do not "self-restore" from inside the stub.** Tried: end the stub with
`mov rsp, orig_rsp / mov rax, orig_rip / jmp rax` so a slow call cannot strand
the thread. It wedges the process — the hijacked thread is stopped in the middle
of unrelated code, so its caller-saved registers (rcx, rdx, r8-r11) are live;
the target call clobbers them and the thread resumes with them destroyed.
Restoring the **whole** context is what makes this safe. The stub stays at
`jmp $` and the injector puts every register back.

A slow command therefore needs a long *poll*, not a clever stub. `tag` switches
the player country and does not return in 8 s; `run_console_cmd` now waits 30 s
and then gives the call a 25 % grace period before deciding it is stuck. If it
really is stuck, it reads RIP first: inside the stub -> the thread is parked and
restoring is safe; anywhere else -> leave it running and refuse further calls.

## 16. Environment: the launcher, and why the game would not start

Not a binary finding, but it cost more time than the binary work, so it is
written down.

* The Paradox launcher was already broken before this session: it opens with
  **"数据库迁移失败 … delete or rename launcher-v2.sqlite"**. Renaming the file
  lets the launcher start, but it comes up with a *fresh* database whose default
  playset is **all 64 subscribed mods** instead of the 9 the campaign uses.
  Launching with 64 incompatible total-conversion mods crashes `hoi4.exe`
  during asset load (a dump lands in `Documents/.../crashes/`, no exception).
* The correct mod set is recorded in the save itself. `autosave.hoi4` is
  `HOI4bin5` and its header carries the mod display names:
  `Downsize AirForce, Downsize LandForce, Flag Rework Revive,
  Force AI to leave faction, Radio Requiem, UMMS, 决议化更多铁路和补给中心,
  国家名称全称补全, 陆军徽章汉化`.
* The old launcher DB (`launcher-v2.sqlite.bak-20261113`) still resolves those
  names to `gameRegistryId` and to load order; `dlc_load.json` was rewritten
  from it (`dlc_load.json.bak-20261113` holds the 64-mod version). With 9 mods
  the game boots to the main menu in <60 s instead of crashing.
* `hoi4.exe` started directly needs `SteamAppId=394360` in the environment or
  `steam_api64.dll` cannot initialise.
* The Steam in-game overlay (Shift+Tab) renders in its own top-level window and
  **swallows every synthetic click and key**. `WindowFromPoint` still returns
  the game window, so this looks exactly like "the game ignores input". Killing
  the `steamwebhelper` that owns that window (it is not the main Steam client)
  restores input.
* Screen is 2560x1440 at 125 % scaling: the captured desktop is 2048x1152 and
  the `windows-mcp` Click/Move coordinates are in 2560-space, i.e. pass
  `screen_pixel * 1.25`. Every earlier "the click missed" note in this file is
  this, not a broken menu.

---

## 17. CCountry sub-object map (RTTI vtable scan, 2026-11-13)

Every pointer-sized slot of a live CCountry was read and its target's first qword
compared against the MSVC RTTI vtable table (`stage/rtti_win.pkl`, 16 567 vtables).
This replaces guessing: the offsets below are *observed*, not inherited from Linux.

```
+0x0F60 CTechnologyStatus      +0x0F68 CProductionStatus
+0x0F70 CDeploymentStatus      +0x0F78 CReinforcementStatus
+0x0F80 CArmyUpgradesStatus    +0x0F88 CDiplomacyStatus
+0x0F90 CPoliticalStatus       +0x0F98 CLogisticsStatus
+0x0FA0 CDecisionStatus        +0x0FC0 CIntelligenceAgency
+0x0FD0 CCountryOccupationStatus              +0x0FE8 CCountryIntel
+0x0FF0 CCountryCharacters
+0x11F8 CCountryResources      +0x1310 CNuke
+0x1350 CBuildingStatus
+0x1370 CNationalFocusTree     +0x1378 CContinuousFocusPalette
+0x1380 CNationalFocusProgress
+0x1580 CCountryFuelStatus     +0x1588 CCountryExperienceStatus
+0x15A8 CCountryOperationManager
```

Differences from the 1.19.4.1 Linux table: the focus block moved 0x1338/0x1340/0x1348
-> 0x1370/0x1378/0x1380, and several status pointers were inserted before it.
Using the Linux values on Windows read a random heap pointer and every focus call
died with `OSError 299`.

### CNationalFocusTree / CNationalFocus

```
tree+0x08  std::string  tree id ("german_focus")
tree+0x30  CPdxArray<CNationalFocus*>   cap 474 / size 438   (alloc = base+0x30FD000)
focus+0x08 int32  id
focus+0x18 CString key          ("GER_remilitarize_the_rhineland")
focus+0x38 CString localisation key
focus+0x60 CString localised name
```

`game.py` now resolves the three CO_FOCUS_* through `getattr(O, ...)` so the Linux
table stays authoritative there and `offsets_win.py` overrides it here.

### Writing the current focus

`CNationalFocusProgress+0x10` holds the live `CNationalFocus*`. The command layer
that normally sets it (`CSetNationalFocusCommand`) is still Linux-only, so the focus
was selected by storing the pointer directly. Verified: the value survives day ticks
and `current_focus()` reads it back; the game keeps progressing it.

## 18. Defects found while playing a real campaign

* `console.annex` returns **ok=False with an empty message but works** — the handler
  writes no SResult; the ok byte is stale. `annex LUX/HOL/BEL/FRA` took GER from 24
  to 109 states with every target left at 0 states. Do not trust `ok` for `annex`.
* `g.date()` is wrong on Windows. It falls back to log scanning, and the answer came
  back as `1945.02.20.01` while the UI showed `31 1月 1936`. `DATE_SIGNATURE_FLOAT`
  is None in the Windows table, so the memory path never runs.
* `Country.political_power` works once `CO_POLITICS` aliases `CO_POLITICAL_STATUS`
  (0xF90) — it read 2 at campaign start, 5 a month later.
* Synthetic mouse clicks into the fullscreen game are dropped while the window is
  minimised (Steam's window manager can iconify it). Menu navigation via `ENTER`
  is reliable; check `MainWindowHandle`/`IsIconic` before blaming the coordinates.

## 19. The object index on MSVC RTTI

`ObjectIndex` (the class -> live objects map behind `g.oi`) was the last piece of
the read layer that was Linux-only. It is platform-neutral apart from two things:

* the class map. `calling/rtti.py` reads Itanium RTTI out of the ELF `.rela.dyn`
  relocations; hoi4.exe has MSVC RTTI instead. `research/windows/rtti_win.py`
  walks `RTTITypeDescriptor` / `RTTICompleteObjectLocator` and finds every vtable
  whose `[-1]` slot points at a locator.
* `calling/rtti_win.py` is the runtime shim: it loads that pickle, reduces the
  decorated names (`.?AVCState@@` -> `CState`, `.?AV?$T@VArg@@@@` -> `T`) and
  returns the same `{"vtables": {name: [rva]}}` shape as the Linux module.
  `game.py` picks it automatically when `sys.platform == "win32"`.

Two smaller fixes were needed in `objindex.py`: `instances()`/`live_instances()`/
`deep_instances()` used a `^\d+` test to recognise the Itanium length prefix, which
never matches a plain MSVC name, and `_rss_mb()` read `/proc/self/status` (0 MB on
Windows = **no memory ceiling at all** during the scan). Both now handle either
build. `Proc.refresh_maps()` forwards to the Windows backend so the fallback scan
sees heap arenas mapped after attach.

Measured on the running campaign (pid 26844):

```
scan            11.9 s   over every writable region, 4 MB chunks
vtable keys     9 975    (8 616 classes, 16 567 vtables in the pickle)
CCountry          440
CState          2 162
CBuilding       6 746
CBuildingDatabase   1
```

`CGameState` has no live instance (the object is a `CCurrentGameState`); use the
`G_GAMESTATE` global.

## 20. CBuildingDatabase and CState.buildings

```
CBuildingDatabase+0x160  CPdxArray<CBuildingTemplate*>   cap 63 / size 51
  template+0x08   int32       building type id
  template+0x230  std::string "<key>_plural"   (only on some entries)
  template+0x270  std::string key              ("industrial_complex")
```

There is no plain display-name string in the template on this build, so
`building_types()` returns the key as `name`. The array lives at **+0x160**, not
the Linux +0x40; +0x40 here is a 56-entry `TNullObject` array.

51 types, and the three the top bar counts are `20002 arms_factory`,
`20013 industrial_complex`, `19446 dockyard`.

`infrastructure` is **not** in that database, yet every state carries a `CBuilding`
of type **12203** whose level is the state's infrastructure. Cross-checked against
the campaign autosave (`industry_by_country()` in `savereader.py`):

```
                 memory          save
GER infrastructure    81            81
GER air_base          47            47
GER civ/mil/dock  35/28/10    35/28/10
FRA civ/mil/dock  35/8/11     35/8/11
ENG civ/mil/dock  32/14/19    32/14/19
```

so `offsets_win.BT_EXTRA = {12203: "infrastructure"}` is added to the id->key map.

### CState layout (Windows)

```
state+0x18   CPdxArray<CProvince*>    (state 5: 11 provinces)
state+0x58   int32                    state id
state+0x158  CPdxArray<CBuilding*>    the buildings the top bar counts
state+0x170  CPdxArray<CBuilding*>    same objects, second view (not used)
```

`ST_PROVINCES` was 0x08 on Linux; reading it here returned `cap=5 size=512`, which
is how the wrong offset was caught.

Still unknown on this build: `ST_RESISTANCE` (+0x278 on Linux) and
`ST_COMPLIANCE` (+0x2A8). `state_detail()` returns `null` for them instead of
printing garbage.

## 21. Manpower (M1.3)

Manpower is not one country-level field on this build: the game keeps a
`CStateManpower` per state and sums it on demand.

```
CState+0x830   embedded CStateManpower      (1081 instances, always +0x830)
    +0x18  int32   manpower AVAILABLE from this state   <- the top-bar number
    +0x1C  int32   population that cannot be recruited
    +0x20  int32   population of the state

CCountry+0x328  embedded CCountryManpower   (440 instances, one per country)
    +0x10  int64 fixed(1e5)  recruitment ratio (GER 2.5%, LUX 1.5%, FRA 1.65%)
    +0x20  int32             NOT the used manpower -- see below
```

Verified against the campaign autosave (`manpower_pool` per state):

```
                 sum(+0x18)   save available
GER   1 318 383 (after save)      1 318 649
FRA     404 504 (after save)        404 704
ENG     490 757 (after save)        501 238
SOV   1 176 509 (after save)      1 188 357
HOL          23 857                  23 857
BEL           9 542                   9 542
LUX           4 432                   4 432
```

and against the top bar screenshot taken at the same moment: **1.31M**.

The total (before what the army already uses) is
`sum(+0x20 - +0x1C)` = 1 641 933 for GER, i.e. population 65 677 797 x 2.5 %,
which is the tooltip's "total manpower 1.63M". `used = total - available`
(323 550 for GER), which is *not* `CCountryManpower+0x20` (330 450) — that field
is something else, so the arithmetic uses only the state table.

`Country.manpower()` now returns the available value, and two extra properties
`manpower_total` / `population` expose the other two numbers (both `None` on the
Linux build, where they are still behind an engine call).

## 22. Stability / war support are base + modifiers

`CCountry+0x10D0` / `+0x10D8` hold the **base** values in fixed(1e5):

```
        base stab   base WS     UI just after loading
GER       0.70       0.30        81% / 35%
FRA       0.45       0.10
ENG       0.596      0.10
SOV       0.50       0.30
```

So the UI value is not stored anywhere reachable in the country object (an
anchored search for 8 100 000 / 0.81 across the country and all its status
sub-objects found nothing) — it is base plus the modifier stack. `modifier()` and
an exact stability reading need `G_MODIFIER_DEFS`, which is still `None` on this
build; that is the remaining half of M1.3.

## 23. Modifiers (M1.3, second half)

`G_MODIFIER_DEFS` = **0x33DD1D0**, a `u64` pointing at a heap array of 4373
`SModifierDef` records (the array is larger than the first 666 `MODIFIER_*`
entries — after a gap the game keeps appending the per-country/per-trait ones).

```
SModifierDef (stride 0x78, same as Linux)
   +0x00  std::string   "MODIFIER_STABILITY_FACTOR"
   +0x6C  int32         id == index
```

Found mechanically: scan every writable region for 24 consecutive records whose
int32 at +0x6C equals their own index — exactly one candidate in the whole 4 GB
process. `modifier_list()` now returns 4373 names and `modifier_id("stability_factor")`
is 141.

### Where a country's modifiers actually live

Not one sorted array (that is the Linux layout). `CCountry+0x5D0` and `+0x5E8`
are `CPdxArray<CModifier*>`; each `CModifier` is a polymorphic object whose

```
   +0x10  ->  array of {int32 id; 4 pad; int64 value}    (value = fixed 1e5)
   +0x18      int32 count
```

so the total for one id is the sum over every CModifier in both arrays. The two
arrays never share an object (50 + 3 for GER, all distinct).

```
GER  stability_factor  +0.05    war_support_factor +0.05
FRA  stability_factor  -0.10    war_support_factor -0.05
ENG  stability_factor   0.00    war_support_factor +0.05
SOV  stability_factor  -0.575   war_support_factor +0.20
```

`Country.stability()` / `war_support()` now return base + that sum. War support
matches the top bar exactly (GER 0.30 + 0.05 = 35%).

### The one term still missing

The stability tooltip breaks the number down itself:

```
基础值:        70.00%
执政党支持率:  +6.00%
帝国议会:      +5.00%
```

Our 0.70 (base) and 0.05 (帝国议会) match two of the three lines; the `+6.00%`
comes from the ruling party's popularity and is **computed, not stored** — an
anchored search for 60000 / 0.6 across CCountry, CPoliticalStatus and the
CPoliticalParty instances found nothing usable. So `stability()` reads 0.75 for
GER where the UI shows 81%. Everything else in M1.3 is verified.

## 24. M2 groundwork (command layer)

### Allocator -- works

`base+0x30FD000` is the `CPdxNewDeleteAllocator` **object** (not a pointer to one);
its vtable is `base+0x2794550`.

```
vtable+0x08  Allocate(this, size)        -> void*       (helper: mov rcx,rdx; call ...)
vtable+0x10  Deallocate(this, ptr)       -> int
```

Verified through the injector: `Allocate(0x40)` returned `0x1e839cc4400` (readable)
and `Deallocate` returned 1. So a command object can be allocated from the same CRT
heap the game frees from -- no "delete on a foreign pointer" crash.

### Idler -- found, but the queue slot is not the Linux one

`G_IDLER = 0x33DDB08` is a `u64` that really holds a `CInGameIdler`
(`ObjectIndex.class_of` says so, and reading `[idler]` gives its vtable).

Linux uses `vtable+0x90` for `GetCommandQueue()` and `+0xC0` for `GetPlayerTag()`.
On this build:

```
+0xB8   mov rax,[rcx+0x6B8]; ret      <- a getter, but ...
+0xC8   mov rax,[rcx+0xA48]; ret
+0x118  mov rax,[rcx+0xA28]; ret
+0x130  mov rax,[rcx+0x760]; ret
+0xC0   xor eax,eax; ret               <- returns NULL
```

Calling the `+0xB8` getter returns exactly `[idler+0x6B8]` (both `0x1e806d29290`),
but that object is a container of **UI views**: its arrays hold `CTopBar`,
`CAchievementsView`, `CCountryLogisticsView`. It is not the command queue, and a
scan of the whole `CInGameIdler` (first 0x2200 bytes) for a `CPdxArray` whose
elements are `*Command` objects found none.

So M2.2 has to be redone from the Windows side: find the queue first, then the
slot that returns it.

### CCommandQueue::Post -- not located yet

Tried and did not produce a unique answer:

* `FF 50 50` + `E8` in `.text` -> 183 sites, top target 0xC08170 (11 hits), which
  is not a queue push.
* `mov rax,[rcx]; call [rax+0x50]; call rel32` (exact adjacency) -> 0 hits.
* `lea` to any `*Command` vtable (410 vtables) -> 1931 sites; the most frequent
  nearby call targets are 0x25A01DC (542), 0x11F6F0 (428), 0x2284870 (204) --
  the first two look like `operator new`/CRT helpers, not a container push.
* `call [rax+0xB8]` (the getter above) followed by `E8` -> 2126 sites, but that
  getter is not the queue.

The next probe should be behavioural: allocate a real `CSetNationalFocusCommand`
(vtable `0x2A128C8`), fill it, then find the queue by watching which container
grows when the game itself posts a command (e.g. start a focus with the mouse and
diff the idler's containers).

### Command objects: the constructors are reachable

Searching `.text` for `lea reg,[rip+X]` where X is a `*Command` vtable pays off: it
lands on the constructors, and they show the object layout directly.

`CSetNationalFocusCommand` ctor = **0x11D0DC0**:

```
lea rax,[rip+..]        ; CSetNationalFocusCommand vtable
mov byte [rcx+8], 0
mov dword [rcx+0xC], -1
mov qword [rcx+0x10], 0
mov [rcx], rax          ; vptr
mov dword [rcx+0x18], 0xFFFF
mov qword [rcx+0x1C], 0
mov eax, [rdx]          ; tag  <- *CCountryTag
mov [rcx+0x28], eax     ; +0x28 int32 tag
mov rax, rcx
mov [rcx+0x30], r8      ; +0x30 CNationalFocus*
ret
```

So the base-init block is identical to Linux (`+0x08/+0x0C/+0x10/+0x18/+0x1C`) but
the payload is four bytes later: **tag at +0x28, focus pointer at +0x30** (Linux:
+0x24 / +0x28), minimum size 0x38.

Same trick gave `CSetResearchCommand` ctor = **0x11D1980** and
`CAddConstructionCommand` ctor = **0x11CC8C0** / **0x11CC920**.

Verified live: allocate 0x40 through the allocator, call the ctor with
(`cmd`, `&tag=1`, `focus_ptr`) and read it back -- vptr, tag and pointer all
correct.

`CanExecute` is *not* confirmed yet: calling `vtable+0x50` on that object returned
0 for `GER_prioritize_economic_growth` (which our playbook does not list as
selectable anyway) and for `GER_remilitarize_the_rhineland` (which it does). Two
possible readings: `+0x50` is not CanExecute on this build, or the command needs
more state than the ctor sets. Note that after the experiment `current_focus()`
reported `GER_remilitarize_the_rhineland`, so *something* in that sequence took
effect -- do not repeat it casually.

Injector hygiene: one of those calls timed out and left the injector dirty
("thread left inside game code"). `winject.clear_dirty(pid)` clears it and the
game kept running normally (reads still fine afterwards).

### 2026-11-14 00:20 -- the game crashed during this experiment

After the sequence above (allocate + `CSetNationalFocusCommand` ctor + a call to
`vtable+0x50`), the next injected call came back with "injector dirty - thread
left inside game code", and a few minutes later `hoi4.exe` was gone (no crash
dialog, `game.log` not updated). So **one of those calls killed the process** --
most likely the `+0x50` slot, which is therefore *not* a harmless predicate on
this build.

State at the time: 1936-03-12 12:00, focus `GER_remilitarize_the_rhineland`,
GER 35/28/10 factories. The campaign is recoverable from
`Documents/Paradox Interactive/Hearts of Iron IV/save games/autosave.hoi4`
(23:24), which is one in-game month behind.

Rule for next time: never call a command vtable slot on a hand-built object until
the slot number has been confirmed against a *disassembly* of the class, not by
analogy with the Linux build. Reads are unaffected.

### 2026-11-14 00:5x -- second crash: posting a command that CanExecute rejects

The first successful post (a *different* focus) worked: `Post(session, cmd, 0)`
returned 1 and `current_focus()` went None -> `GER_remilitarize_the_rhineland`,
surviving day ticks. That proves the whole chain:

```
session = [idler]+0x88 getter      -> CSession*
cmd     = alloc + ctor 0x11D0DC0
Post    = 0x22C4250(session, cmd, 0)
```

The very next experiment -- posting the **same** focus again, i.e. a command the
game would refuse -- killed the process again (no dialog, `game.log` untouched).
So `Post` is only safe when the command would pass `CanExecute`; its failure path
is not safe for a hand-built object (it likely deletes the command and then keeps
touching it).

`cmdwin.set_national_focus` now refuses that case before calling into the game:

    if current_focus().ptr == focus_ptr: return {"ok": False, "reason": "focus already selected"}

Rule: before posting, always verify from the read side that the action is legal
(the game's own `selectable_focuses()` / current state), because a rejected post
crashes the process.

### Research: layout recovered, reading fixed

`CSetResearchCommand` ctor **0x11D1980** = `(cmd, CCountryTag*, CTechnology*,
int slot, bool, bool, bool)` and stores

```
+0x28 int32 tag   +0x30 CTechnology*   +0x38 int32 slot
+0x3C/+0x3D/+0x3E bools          (base init identical to the focus command)
```

The three bools are stack arguments, which the injector cannot pass, so
`cmdwin.make_research()` fills the object directly instead of calling the ctor.

Reading research was broken for a separate reason: `game.py` still used the
**Linux** CCountry subsystem offsets (`CO_TECH_STATUS 0x0F38`, production `+0x40`,
politics `+0x68`, ...). With `0xF60 / 0xF68 / 0xF90 / 0xFA0` the status object
resolves and `research_slots()` returns the four slots again
(`CTechnologyStatus+0xA0` really is `CPdxArray<CResearchSlot*>`, cap 4).

`CAddConstructionCommand` ctor 0x11CC8C0 = `(cmd, bool, int32*, CBuildingReference*,
int level)`; it embeds the `CBuildingReference` vtable at +0x30 and copies the
reference's +8/+0xC/+0x10 into +0x38/+0x3C/+0x40, level at +0x48.

### 2026-11-14 -- third crash: posting SetResearch

`start_research("basic_machine_tools")` (a tech `available_research()` lists as
legal) killed the process again, right at `Post`. Both crashes so far happened on
a command the game would refuse, which points at **Post's rejection path** rather
than at the object layout: the ctor call site proves the layout is right --

```
new(0x40); &tag = GetCountryTag(...)+8; r8 = tech; r9d = slot
stack: [r14+0xC], 0, 0   -> +0x3C, +0x3D, +0x3E
```

so the remaining suspects are (a) the tech pointer is a `CTechnologyTemplate*`
while the command wants a country `CTechnology*`, or (b) `Post` deletes the
command on refusal and then touches it.

Next probe: pass `force=1` to `Post` (the Linux build's `force` skips the
predicate) and see whether the command then applies instead of crashing. Do not
retry the plain path without a CanExecute check.

Meanwhile the tech **reading** chain is fixed and verified:

```
CTechnologyDatabase+0x48  CPdxArray<CTechnologyTemplate*>  cap 711 / size 553
   template+0x10  std::string key ("improved_ship_hull_light")   [Linux: +0x08]
   template+0x3C  int32 id                                       [Linux: +0x34]
```

With those two offsets `_load_tech_db()` returns **552** technologies,
`researched()` **55** and `research_slots()` the four live slots.

### force=1 does not help -- fourth crash

`Post(session, cmd, 1)` on the same SetResearch command also killed the process,
so the flag is not the Linux "skip CanExecute" escape hatch, or the fault happens
before that check. The remaining difference from a real UI post is the **argument
type**: the ctor call site passes `[r14]`, an entry from the UI's research list,
which is almost certainly a country `CTechnology*` instance, while
`start_research()` hands it a `CTechnologyTemplate*` from
`CTechnologyDatabase`. Dereferencing a template as a country technology inside
CanExecute would explain a hard crash every time.

Next probe: build the command with a `CTechnology*` taken from the country's own
technology list (the status object holds `CTechnology` objects -- the researched
array at `CTechnologyStatus+0x40` is one source, and there must be an array of all
of them), not from the database.

Until that is settled, do not call SetResearch on the live game; four crashes came
from this one command.

### Fifth crash -- the CTechnology* pointer did not help

Built the command with a country `CTechnology*` taken from
`CTechnologyStatus+0x88` (CPdxArray<CTechnology*>, 552 entries -- the full list,
which does exist on this build and matches the Linux `TS_ALL_TECHS` offset), for
the very tech `available_research()` lists. `Post` still killed the process.

That rules out the pointer type. What is left:

* the focus command -- the only one that works -- was built by **calling its real
  constructor**; the research command is **hand-filled**. The ctor disassembly
  showed writes only up to +0x3E, so the object should be byte-identical, but this
  is now the main structural difference and the next thing to eliminate: extend
  the injection stub to push the three stack arguments and call ctor 0x11D1980
  for real.
* or the command is being *executed* and crashes inside the game (slot/flag
  semantics), in which case Post is innocent.

Status: SetResearch is **blocked pending a real-ctor build**. Five crashes came
from it; do not post it again until that changes.

### Injector: stack arguments now supported

The stub used to load only rcx/rdx/r8/r9, so any function with more than four
arguments (every command ctor with optional flags) was uncallable. It now builds
a frame and writes args 4..N at [rsp+0x20], [rsp+0x28], ... before the call
(`winject.py`, RES_OFF moved to 0x200 to make room). Verified: calling
`CSetResearchCommand`'s real ctor with seven arguments
`(cmd, &tag, CTechnology*, slot, 0, 0, 0)` returns the command and the object has
the right vptr, tag, technology pointer and slot.

### Sixth crash -- and the conclusion

Posting that ctor-built command still killed the process. So neither the pointer
type nor the construction path is the problem. Every crash so far (6) happened on
a command the game would very likely **refuse**, while the one command that
succeeded (SetNationalFocus, a legal focus) went through. That makes
`CCommandQueue::Post`'s **rejection path** the prime suspect: it appears to
delete the command and then keep using it, which is fatal for any object.

Practical rule until this is understood: only post a command when the read layer
can prove the game will accept it, and prefer `SetNationalFocus`, which is proven.

Next idea: instead of Post, call the command's own Execute virtual (`vtable+0x58`
was overridden on the focus class) after a successful CanExecute -- that skips the
queue's failure path entirely. Do this on a throwaway save.

### Seventh crash -- flag=1 did not help either

Posting the ctor-built research command with its first bool set (the one the UI
fills from its list entry) also killed the process. Seven crashes, all on
SetResearch; the only command that ever posted is a legal SetNationalFocus.

Conclusion for the next session: stop trying to make SetResearch acceptable to
`Post`. Either call the command's own `Execute` virtual directly after a
successful `CanExecute`, or find the real reason `Post` refuses it (the function at
0x22C4250 begins with `test r8b,r8b / jne`, `cmp byte [rcx+0x725],0 / jne`, then
`call [cmd.vtable+0x80]` and `+0x60` -- disassemble past 0x22C4250+0x100 to see
which branch is the fatal one).


## 25. Command channel: the crashes were `Post` deleting our object

The seven SetResearch crashes had nothing to do with the command being illegal.
`Post` (0x22C4250) **owns** the command on every path, and its four drop paths
all end in `call [cmd->vt[0]](cmd, 1)` -- the MSVC deleting destructor, which
releases through the CRT (`operator delete` = 0x25CEC00, reached via
0x222F060 <- 0x25A0218).  Our command objects came from the game's own
`CPdxNewDeleteAllocator`, so the CRT `free()` on rejected memory is what took
the process down.  The single command that ever worked (SetNationalFocus) did
so because it was *accepted*, and the accepted path does not delete.

### What each branch means

`Post` builds one of these strings before dropping:

| condition | message |
|---|---|
| `flag != 0` | `Dropped command: ` |
| `[session+0x725] != 0` | `Dropped command: ` |
| `vt[0x80]` / `vt[0x60]` non-zero | `Dropped command: ` |
| `[session+0x3c] != 0`, `[session+0x6cc] == 0`, `[session+0x726] != 0`, or `vt[0x80]` non-zero after the tick stamp | `Dropped (posted during command execution): ` |
| `vt[0x98] == 0` | `Dropped (sender not authorized): ` |

`vt[0x60]` and `vt[0x80]` are `0x11D530` (`xor al, al; ret`) in every command
vtable, so those two never fire.  `vt[0x98]` is `0x11C13E0` for *both*
CSetResearchCommand and CSetNationalFocusCommand -- the authorization check is
shared, not per class.

The accepted path is 0x22C4683 -> 0x22C4710:

```
0x22c4710  mov rcx, [session+0x40]      ; CCommandQueue*
0x22c4714  mov rax, [rcx]
0x22c4717  mov rdx, cmd
0x22c471a  call [rax+0x28]              ; queue->Push(cmd)
0x22c471d  mov bl, 1                    ; return 1
```

Before that push, `Post` stamps the object: `cmd+0xC = [session+0x84]` (tick)
and `cmd+0x18 = min([session+0x60], 0x7fff)` (sequence).

### The working approach

`cmdwin.enqueue()` reproduces exactly those three steps and nothing else.  No
legality check, no drop path, no delete:

```python
p.write(cmd + 0xC, struct.pack("<i", tick))
p.write(cmd + 0x18, struct.pack("<H", min(seq, 0x7FFF)))
q = p.u64(session + 0x40)
game.call(p.u64(p.u64(q) + 0x28), (q, cmd))     # queue->Push(cmd)
```

Verified live 2026-11-14: `SetResearch('support_weapons', slot 0)` returned 1,
`research_slots()[0]['researching']` read back `support_weapons` with
`required = 30.0` days, and it survived a day tick.  An illegal tech
(`gwtank` before its prerequisites) is dropped silently by the queue and the
game keeps running -- the failure mode is now safe.

Two safety details:

* The object still gets a **private vtable** (`fake_vtable`, slot 0 replaced by
  `0x11D530`) so that if the queue ever releases it, the release does nothing
  instead of calling the CRT deleting destructor.  Cost is a 0x40-byte leak per
  command.
* `operator new` (0x25A01DC) is the *right* allocator in principle, but calling
  the CRT allocator from a hijacked thread kills the process outright
  (`ReadProcessMemory` on the injector's flag page then fails with
  `ERROR_PARTIAL_COPY`).  Do not use it from the injector.

### Correction to section 24

The earlier note ("stop trying to make SetResearch acceptable to `Post` ...
0x22C4367 is the fatal branch") was wrong on both counts: 0x22C4367 is the
"posted during command execution" branch and is not in itself fatal, and the
actual killer was the deleting destructor on the drop paths.


## 26. CAddConstructionCommand -- structure solved, location field still open

Everything about the command is now known except what the location fields mean
at execution time.  Recording it so the next session does not re-derive it.

### Classes (confirmed against the MSVC RTTI map)

| class | vtable |
|---|---|
| `CAddConstructionCommand` | `0x2A10B88` |
| `CBuildingReference` | `0x29EE730` |
| `CProvince` | `0x29EE358` (secondary vtable `0x29EE3A8`) |

`CAddConstructionCommand` is **0x50** bytes -- the game allocates it with
`mov ecx, 0x50; call 0x1425a01dc` at 0xBBDE3A.

### ctor 0x11CC8C0

`(cmd, bool dl, int32* r8, CBuildingReference* r9, int level [rsp+0x28])`

```
+0x08 byte 0        +0x0C int32 -1      +0x10 qword 0
+0x18 dword 0xFFFF  +0x1C qword 0
+0x28 int32  = *(int32*)r8        <-- the building type id
+0x30 qword  = CBuildingReference vtable (embedded sub-object)
+0x38 int32  = ref+0x08
+0x3C int32  = ref+0x0C
+0x40 int32  = ref+0x10
+0x48 int32  = level
+0x4C byte   = dl
```

Call site 0xBBDE71 shows the shape the UI uses: `new(0x50)` -> build a
`CBuildingReference` into a stack slot via `0x1204E40` -> `ctor(cmd, 1, &type,
ref, level)`.

### CBuildingReference 0x20 bytes

```
+0x00 vtable 0x29EE730
+0x08 int32
+0x0C int32
+0x10 int32
+0x18 IAllocator*   (always G_DEFAULT_ALLOC)
```

ctor A `0x145B5A0(CProvince* rdx, int32* r8)`: sets `+0x08 = -1`,
`+0x0C = *(int32*)(rdx+8)`, `+0x10 = *(int32*)r8`, and only on the non-assert
path.  ctor B `0x145B650(CProvince* rdx, X* r8)` sets `+0x08 = *(int32*)(r8+0xA4)`,
`+0x0C = *(int32*)(rdx+8)`.

All 870 live `CBuildingReference` instances read `(-1, 19479, 0)`, so 19479 is
the "no province" sentinel.

### What was tried and what happened

| attempt | result |
|---|---|
| `enqueue` with ref `(state=5, 19479, 0)`, type id | queue accepted (`Push` returned 1), **construction never appeared**; state 5 stayed at one building |
| `enqueue` with ref `(state=5, 19479, 0)`, type pointer | same |
| `Post(..., force=1)` | **process dies** even with the no-free vtable -- the flag=1 branch dereferences something our object does not satisfy |
| `enqueue` after re-loading at 1945, ref `(-1, 5, 0)` | **process dies inside the queue** |

So unlike SetResearch, a wrong AddConstruction is *not* safely discarded: the
queue's handling of this command walks the reference and faults.

Two leads for the next attempt:

* `building_types()` exposes `province_level`, and `industrial_complex` is
  `province_level: False` (state-level) while `infrastructure` is True.  The
  location probably has to be the province id for province-level types and the
  state id for state-level ones, in the *same* field -- which one is still
  unknown because ctor A writes the province into `+0x0C` and ctor B writes
  something from the state into `+0x08`.
* `CProvince` carries two vtables and its id is not at `+0x08` or `+0x10`
  (both read as constant 4 across different provinces), so the value ctor A
  copies into `+0x0C` from `CProvince+8` may not be a province id at all.

### Do not repeat

`Post` with `force=1` is fatal for commands it does not fully recognise.
`enqueue` is safe for SetResearch but **not** for a malformed
CAddConstructionCommand.  Test construction only on a throwaway save.


## 27. AddConstruction solved -- the capture that settled it

Section 26 left the location fields open.  A Frida hook on the real constructor
`0x11CC8C0`, armed while clicking the build UI by hand, produced one sample:

```json
{"typeId": 1, "level": 1, "dl": 2,
 "ref": {"vt": "0x7ff658b6e730", "f08": 59, "f0C": 20013, "f10": 0}}
```

`f0C = 20013` is `industrial_complex`, so **`ref+0x0C` is the building type id**
and `ref+0x08` is the location id (state id for state-level buildings).
`cmd+0x28` is the constant **1**, and `cmd+0x4C` is **2**.  Both of my earlier
guesses were wrong, and either mistake alone makes the queue accept the command
and then silently discard it -- which is exactly the symptom three earlier
layouts produced.

Correct layout (implemented in `calling/cmdwin.py`):

```
CBuildingReference (0x20):  +0x00 vtable 0x29EE730
                            +0x08 state id
                            +0x0C building type id
                            +0x10 0
CAddConstructionCommand (0x50):
                            +0x00 vtable 0x2A10B88
                            +0x0C -1     +0x18 0xFFFF
                            +0x28 1
                            +0x30 CBuildingReference vtable
                            +0x38 state id  +0x3C type id  +0x40 0
                            +0x48 level    +0x4C 2
```

Verified live: six orders (states 5, 42, 50, 51, 52, 60) took
`CBuildingProductionLine` from **113 to 119**, and state 5 gained a
`(20013, level 0)` entry.

Two measurement notes:

* an in-progress building does **not** reliably appear in `CState+0x158`; only
  one of the six did.  Count `CBuildingProductionLine` instances instead.
* `construction_queue()` still returns 0 on Windows while 119 production lines
  exist -- its array offset is wrong and needs its own pass.

`Post(..., force=1)` remains fatal even with the no-free vtable.  `enqueue()` is
the only safe channel.


## 28. Diplomacy: CAction, not CCommand -- Windows 1.19.5 map

The Linux `calling/diplomacy.py` already showed the shape: a
`CDiplomaticActionCommand` (0x30 bytes) wraps a `CDiplomaticAction` subclass and
is posted to the queue.  Windows has the same design, at these addresses
(resolved through the MSVC RTTI map):

| class | vtable RVA |
|---|---|
| `CDiplomaticAction` (base) | `0x27C6F48` |
| `CDiplomaticActionCommand` | `0x2A073C8` |
| `CDeclareWarAction` | `0x2A07548` |
| `CGenerateWarGoalAction` | `0x2A079B8` |

`CDeclareWarAction` constructor: **`0x1180B70`**.  Entry code:

```
0x1180b70  mov r11, rsp
0x1180b7b  mov [r11+8], rcx          ; this
0x1180b84  mov rdi, r9               ; CWarGoal*
0x1180b8a  mov rsi, [r11+0x30]       ; 6th arg (scope)
0x1180b8e  mov eax, [rsi+8]
0x1180bc4  call 0x141180db0          ; CDiplomaticAction base ctor
0x1180bca  lea rax, [rip+0x1886977]  ; = 0x2A07548
0x1180bd1  mov [rbx], rax
0x1180bd4  lea rcx, [rbx+0x78]
0x1180beb  mov rdx, [rdi+8]
0x1180bef  mov [rcx], rdx            ; +0x78 <- wargoal+8
```

so the Windows signature is `(this, ?, ?, CWarGoal* r9, u8, scope*)` and the
object is 0x78 bytes, matching Linux's `SZ_DECLAREWAR`.

`CDiplomaticActionCommand` is a flat 0x30-byte wrapper in Linux; on Windows the
same shape is expected (`+0x24` tag id, `+0x28` action pointer), and the base
vtable is written by the ctor at `0x1180F0F`.

### Why this is not done in one sitting

`CDeclareWarAction::CanExecute` dereferences the war-goal pointer, so a war can
only be declared after a `CWarGoal` exists -- which means `justify_wargoal`
(`CGenerateWarGoalAction` + a `CWarGoalType*` + the embedded Scope block at
`+0x20`) has to work first.  Both actions also need the Scope object that Linux
builds with `SCOPE_MAGIC 0x29C77F8`; the Windows equivalent of that scope builder
has not been located yet.

So the remaining work is a two-step chain:

1. `justify_wargoal` -> `CGenerateWarGoalAction` (0x2A079B8) + `CWarGoalType*`
2. `declare_war`    -> `CDeclareWarAction` (0x2A07548, ctor 0x1180B70)

with the same Frida capture trick that solved AddConstruction (hook the ctor,
click the diplomacy dialog's Send button, read the real field values).

Everything else in M2 is done: SetNationalFocus, SetResearch and AddConstruction
all work end-to-end through `cmdwin.enqueue()`.


### Live inventory (1945 German save)

* `CWarGoal` instances: 3.  Only one is populated -- `+0x08 = 4713`,
  `+0x38 = 16` (owner tag index), `+0x3C = 44` (target tag index).  So that one
  belongs to ITA vs someone, **not Germany**: Germany has no casus belli in this
  save and `justify_wargoal` has to run first.
* `CWarGoalType` instances: 10.  `+0x10` is not a string pointer (all read NULL),
  so the name lookup in `diplomacy.py`'s `wargoal_types()` needs its own pass on
  this build.

That confirms the ordering dependency: `CGenerateWarGoalAction` must work before
`CDeclareWarAction` can be tested at all.


### Constructors found and verified

| item | RVA |
|---|---|
| `CDeclareWarAction` param ctor | `0x1180B70` |
| `CGenerateWarGoalAction` param ctor | `0x1181120` |
| `CGenerateWarGoalAction` default ctor | `0x1181220` |

Both parameterised ctors have the shape
`(this, from*, to*, target_ptr r9, flag, scope* [rsp+0x30])`, matching Linux's
`(this, from*, to*, CWarGoal*/CWarGoalType*, scope*)` plus one extra flag slot.

**The scope magic is identical to Linux: `0x29C77F8`.**  Proof: the default
GenerateWarGoal ctor at `0x1181238` does
`mov dword ptr [rsp+0x48], 0x29c77f8`, and the parameterised ctor at
`0x118113d` reads `[scope+8]` and copies it into the same slot.  So a 0x18-byte
block holding `{0, 0x29C77F8, 0}` is the correct scope object on Windows too.

**Verified live**: calling `0x1180B70(act, &from, &to, wargoal, 1, scope)` on a
real populated `CWarGoal` (owner 16 / target 44) produced an object whose vptr is
exactly `base + 0x2A07548`.  `+0x14 = 16`, `+0x18 = 16`, `+0x1C = 44` -- the
tags landed where the Linux layout says they should.

### Injector bug found and fixed

`process/winject.py` wrote stack arguments with
`mov qword [rsp+d8], imm32`, so the 6th argument (the scope pointer, well above
2^31) was truncated to its low half and `struct.pack` raised
`argument out of range` before the call even started.  Stack arguments are now
stored as full 64-bit values (`mov rax, imm64; mov [rsp+d8], rax`).  This is why
no earlier command ctor with a pointer on the stack could work.

### Still open

The process died shortly after the successful ctor call, so `CanExecute` on the
wrapped `CDiplomaticActionCommand` (vtable `0x2A073C8`, slot `+0x50`) has not
been probed yet, and nothing was actually declared.  Next session: rebuild the
action, wrap it, probe `+0x50`, then `enqueue()` it.


`CDiplomaticActionCommand::CanExecute` is vtable `+0x50` = **`0x118B230`**, a
thunk: `xor edx, edx; jmp 0x118AA80`.  Calling it with `(cmd)` returned a
pointer, not a bool, so `0x118AA80` most likely returns a result/error struct
rather than the flat `bool` the Linux build returns.  That return shape still
needs decoding before `enqueue()` should be trusted with a declaration.


### First real declaration attempt -- enqueued, not executed

`enqueue()` on the wrapped `CDiplomaticActionCommand` returned 1 and the process
stayed alive, but **no war was declared**: `CWar` instances remained 0 and the
`CWarGoal` (owner 16 / target 44) was still present after several day ticks.
So the queue accepts the command and drops it -- the same signature as a wrong
field, which for actions means the `from`/`to`/scope block did not survive the
constructor.

One measurement trap found along the way: `date()` was reporting
`1945.04.02` for a 1936 save until the game was unpaused, then it snapped to
`1936.03.01`.  The stale-copy problem is not fully solved while paused even with
the ticking-copy preference, because a paused process has no ticking copy.

Next step is the same Frida capture trick that settled AddConstruction: hook
`0x1180B70`, declare war by hand through the diplomacy UI, and diff the captured
`from`/`to`/scope bytes against what we build.

## 29. Diplomacy: the action object is right, the command still dies

Everything below was read off `hoi4.exe` 1.19.5.0 and checked against live
objects; addresses are RVAs.

### 29.0 A correction to §28

`CWar` **is not an RTTI class in this build**.  Searching
`rtti_win.pkl` for `CWar` yields `CWarGoal`, `CWarGoalType`, `CWarRelation`,
`CWarScoreBreakdown`, `CWarReparationTimedEffect`, `CWarLengthTrigger` and a pile
of GUI types -- but no plain `CWar`.  So every earlier "CWar instances: 0" was a
false negative, not evidence that no war existed.  The object that actually
represents a war is **`CWarRelation`** (vtable `0x29DCC08`, `sizeof 0x1D0`,
allocated by a factory near `0xD80C7F` via `operator new(0x1D0)`).

The world already contains exactly one `CWarRelation` in the 1936 save.

### 29.1 `CDiplomaticAction` layout (base ctor `0x1180DB0`)

    +0x00  vtable
    +0x10  int      zero
    +0x14  int      *(int*)arg1        // from
    +0x18  int      *(int*)arg1        // from (duplicate)
    +0x1C  int      *(int*)arg2        // to
    +0x20  int      *(int*)arg2        // to (duplicate)
    +0x28  ptr      0x27963D0          // scope pair A head
    +0x30  int      *(int*)(arg3+8)    // scope pair A value
    +0x38  ptr      0x2796400          // scope pair A tail
    +0x40  ptr      0x27963D0          // scope pair B head
    +0x48  int      *(int*)(arg3+8)    // scope pair B value
    +0x50  ptr      0x2796400          // scope pair B tail
    +0x58  ptr      lookup(action_id)  // action definition
    +0x60  ptr      lookup2(action_id) // null when the action id has no entry
    +0x68  int      stack arg
    +0x6C  int      stack arg
    +0x70  word     0                  // control flags, not serialised
    +0x72  byte     1
    +0x73  byte     0

`arg3` is a 0x18 struct `{ptr vtable, int value, ptr more}`.  The default ctor
`0x1180CD0` fills it with the constant triple
`{0x27963D0, 0x29C77F8, 0x2796400}`, which is where `0x29C77F8` comes from --
it is the *scope value*, not a magic number standing on its own.  The two
`lookup` calls are keyed by the derived class's action id.

### 29.2 `CDeclareWarAction` ctor `0x1180B70`

    CDeclareWarAction(this, int* from, int* to, CWarGoal* wg, bool a5, void* scopeOut)

It forwards `from`/`to` straight into the base ctor (`rdx`/`r8` untouched), builds
the scope struct from `*(int*)(scopeOut+8)`, and then:

    +0x08  int      0x277F                 // action id
    +0x78  u64      *(u64*)(wg+8)          // {wargoal id, wargoal type}
    +0x80  byte     a5
    [scopeOut+0] = 0x27961D8               // ctor writes a vtable back into scopeOut

A live build with `from=1, to=73, wg=NULL` reproduces vptr `0x2A07548`,
`+0x08=0x277F`, `+0x14=1`, `+0x1C=73`, `+0x78=0`, `+0x80=1`.  So the *action
object* is correct; the failure is downstream.

### 29.3 Vtable map

| slot | `CDeclareWarAction` | `CDiplomaticAction` | `CGenerateWarGoalAction` | meaning |
|---|---|---|---|---|
| +0x10 | `0x11CB0C0` | `0x11CB130` | `0x11CB260` | Serialize |
| +0x20 | `0x11C82D0` | `0x11C8360` | `0x11C8780` | Serialize (second pass) |
| +0x48 | `0x11D530` | `0x11D530` | `0x11D530` | `xor al,al; ret` |
| +0x50 | `0x188D90` | `0x188D90` | `0x188D90` | `mov al,1; ret` -- always true |
| +0x110 | `0x11C9DF0` | `0x11C9DF0` | `0x11C9E40` | `jmp 0x11C22B0` = IsValid |
| +0x138 | `0x11B9C40` | `0x11BA360` | `0x11BAD40` | IsAvailable |

`CDiplomaticActionCommand` (vtable `0x2A073C8`): `+0x48 = 0x11C2420`,
`+0x50 = 0x118B230` (CanExecute thunk), `+0x58 = 0x11B7E10` (writes id `0x2931`).

### 29.4 The validator

`0x11C2420(cmd, std::string* msgOut) -> bool` is the gate the command's execute
path calls first (`0x118FB90`).  It resolves `action = *(void**)(cmd+0x28)` and
walks: null action / `from <= 0` / `to <= 0` / `IsValid` / `action+0x70` branch /
`vt[0x110]` / `vt[0x48]`, and on failure writes

    "Diplomatic action declare_war is not valid."

That message was reproduced live, which confirms the whole chain.

### 29.5 The measurement trap that cost the most time

`0x11C2420`'s second parameter is only an out-message, **but its low 32 bits are
also pushed as an extra argument into the internal map lookups**
(`mov dword [rsp+0x20], r12d`).  Passing a real `std::string*` therefore changes
which lookup runs.  The game itself calls `0x11C2420(cmd, NULL)`; only with
`msgOut = NULL` does the answer match the game.  With a string pointer the
validator reports "not valid" even for an action it otherwise accepts.

### 29.6 `+0x70`

`CDeclareWarAction::IsAvailable` (`0x11B9C40`) begins with a call to `0x11B8460`,
which returns **true** as soon as `action+0x70 != 0`; every other path in
`0x11B9C40` ends in `return false`.  Setting `+0x70 = 1` therefore makes the
validator return `true` (verified: `validator(cmd, NULL) -> al = 1`).

But the war still does not happen -- and the `+0x70` branch in the validator is
followed by two registry lookups whose hit path drops back into the strict
checks.  Reading it as "force it through" is *not* justified by the code; a
more likely reading is "this action has already been handled".  This flag stays
unexplained and must not be shipped as a workaround.

### 29.7 Executing the command

`CDiplomaticActionCommand`'s execute path near `0x118FAxx` aborts on

* `action == NULL`
* `*(int*)(action+0x14) <= 0`
* `*(int*)(action+0x1C) <= 0`
* `*(int*)(action+0x08) == 0x165`
* `!IsValid(action)`

All five pass for the object we build.  What refuses after that is
`vt[0x138]`, and its only true path is the `+0x70` early-out -- i.e. the action
can never be "available" through the `(action, NULL, 0)` call the command makes.

### 29.8 Where this leaves it

`CDiplomaticActionCommand` is very likely **not** how a player declaration
travels: the command's execute calls `vt[0x138]` with a null context, so it can
only ever accept an action that is already flagged handled.  The player path is
more plausibly `CDiplomaticAction::Execute` driven from the UI/AI with a real
context object, or `CWarRelation`'s factory at `0xD80C7F` directly.

Neither has been exercised.  `declare_war` stays unimplemented; the three
verified commands (SetNationalFocus, SetResearch, AddConstruction) are
unaffected and SetResearch was re-verified after a full game restart.

Also confirmed while chasing this: with the game **paused the command queue does
not drain at all**.  Every "accepted then dropped" observation has to be made
with the clock running, and `date()` cannot be used to tell the two apart
because it still reads a stale `CDate` copy while paused.

## 30. `cmdwin`: `absolute=True` takes a real address, not an RVA

`Game.call(func_vaddr, ..., absolute=False)` documents `func_vaddr` as an **ELF
offset** (an RVA in the PE).  `absolute=True` means the caller already resolved
it.  Three helpers in `cmdwin.py` passed plain RVAs together with
`absolute=True`:

    post()        game.call(FN_POST, ...)        FN_POST   = 0x22C4250
    new_cmd()     game.call(OP_NEW, ...)         OP_NEW    = 0x25A01DC
    delete_cmd()  game.call(OP_DELETE, ...)      OP_DELETE = 0x25CEC00

Each of those called address `0x22C4250` / `0x25A01DC` / `0x25CEC00` -- unmapped
in a 64-bit process (the module lives at `0x7ff6_5618_0000`).  So:

* the seven "SetResearch crashed in `Post`" failures were **not** a CRT/destructor
  problem; `post()` never reached `Post` at all,
* "`operator new` from a hijacked thread kills the process" was **not** a game
  behaviour either -- the call target was simply wrong.

All three now add `p.base`.  §25's conclusion about `Post`'s drop paths was
derived from reading the disassembly, which is still valid; only the live
"it crashes" observations were artefacts.  `enqueue()` remains the safe path and
is unaffected (it resolves the queue's virtual through a real pointer).

This also means `post()` is **untested**, not disproven.  It is the canonical
entry point and worth re-probing now that it actually calls `Post`.

## 31. Declaring war: action object needs a patched vtable too

`CDiplomaticActionCommand`'s execute tail (`0x14118fd45`) ends with

    if (action) action->vt[0](action, 1);      // MSVC deleting destructor
    if (other)  other->vt[0](other, 1);

so the action is destroyed by the command, exactly like a command is destroyed
by `Post`.  An action that came from `CPdxNewDeleteAllocator` must therefore be
given a **private copy of its vtable with slot 0 replaced** by the shared
`xor al,al; ret` stub (`0x11D530`) -- `fake_vtable(..., size=0x1D8)` then
`attach_fake_vtable`.  Everything below slot 0 (`IsValid`, `IsAvailable`,
serialisers, scope helpers) stays real.

`cmdwin.declare_war()` now does that for both the action and the command.  State
of that function: the constructor call succeeds, the validator call that follows
it still takes the process down, and **no declaration has been verified through
it**.  Treat it as unfinished.

## 32. `CGenerateWarGoalAction` signature, from a live AI capture

Frida hooks over a running game caught the AI justifying war goals constantly
(the AI evaluates every pair), which pins the signature down:

    CGenerateWarGoalAction(this, int* from, int* to, CWarGoalType* type, ?, ?)

observed instances, e.g.

    from=16 to=53 type=0x260ecff5da0      from=3  to=43   type=0x260ecff5da0
    from=5  to=47 type=0x260ecff5da0      from=2  to=55   type=0x260ecff5da0
    from=68 to=51 type=0x260ecff5da0

and the resulting object always carries

    +0x08 = 0x277F   +0x18 = from   +0x1C = to   +0x20 = to
    +0x28 = 0x27963D0   +0x30 = 0x29C77F8   +0x38 = 0x2796400
    +0x70 = 0   +0x72 = 1

which is the same base layout as `CDeclareWarAction`, with the wargoal **type**
pointer landing in the scope pair.  The three `CWarGoalType` pointers seen in the
1936 save (`0x260ecff5da0` etc.) are stable, so justifying a wargoal is a matter
of passing one of them.

`CDeclareWarAction` builds captured the same way for `from=1` to `2/3/43/59`
confirm the shape documented in §29.2.

## 33. What actually happened when war was declared on the Dominican Republic

Four declarations (`to` = 73, then 2, 3, 43, 59) were submitted with the
`+0x70` flag set.  The game produced its own World News dialog *"Germany once
again plunges into war"* and a persistent notification
*"Germany declared war on the Dominican Republic"*, i.e. country **73 is the
Dominican Republic** and at least one of those commands really did execute.

No war object could then be located in memory: `CWar` is not an RTTI class
(§29.0), `CWarRelation` scans return one object whose body looks like a
`CDiplomaticAction`, and the `CRelation+0x2D0` "at war" probe reads false for
every pair including Italy.  **The instance enumerator is not trustworthy for
these classes** and no numeric war check exists yet -- the UI notification is the
only reliable evidence so far.

The process died a few minutes later with an access violation whose stack is
unreadable (Paradox ships no symbols; `exception.txt` frames are all
`PHYSFS_*` noise).

## 34. Declaring war works

Resolved.  Two independent game sessions, each freshly loaded from the
1936-03-01 autosave, each submitted exactly one `CDeclareWarAction` for
`from = 1 (GER)`, `to = 73`; in both cases the game itself produced

    World News:  "Germany once again plunges into war"
    Notification: "Germany declared war on the Dominican Republic"

and the process stayed alive and responsive afterwards.  `DOM` resolves to
country id `73`, which cross-checks the id used.

The recipe, now implemented as `cmdwin.declare_war()`:

1. `scope = alloc(0x18)` filled with `{0, 0x29C77F8, 0}`.
2. `act = alloc(0x88)`, then
   `CDeclareWarAction::ctor 0x1180B70(act, &from, &to, wargoal=0, 1, scope)`.
3. `act + 0x70 = 1`.  This is the flag `0x11B8460` tests; without it the
   command's validator rejects every action with
   "Diplomatic action declare_war is not valid."  Its real meaning is still not
   established -- it is what makes `CDeclareWarAction::IsAvailable` return true
   for a null context, and the declaration that results is a real one, so it is
   used deliberately and named `DECLARE_WAR_FLAG_OFF` in the source rather than
   hidden.
4. Give `act` a private copy of vtable `0x2A07548` with slot 0 replaced
   (`fake_vtable(..., size=0x1D8)`), so the command's trailing
   `action->vt[0](action, 1)` cannot CRT-free game-allocator memory.
5. `cmd = alloc(0x30)`: vtable `0x2A073C8`, `BASE_INIT`, `+0x24 = from`,
   `+0x28 = act`; give `cmd` the same no-free vtable treatment.
6. `enqueue(cmd)` -- the queue's `vt[0x28]`.

Two things this needed that were not obvious:

* the two `absolute=True` addressing bugs from §30 (`post`/`new_cmd`/
  `delete_cmd`), and
* the vtable copy for the **action**, not just the command (§31).

`game.declare_war(tag, force=...)` dispatches to this on Windows.  The
availability gate (`action+0x70`) is bypassed, so `war_readiness_check` remains
the only guard -- do not call it with `force=True` in a real campaign without
deciding that the war is intended.

### Still missing

No way to *read* the war back.  `CWar` does not exist as a class (§29.0), the
`CWarRelation` instance scan is unreliable, `CRelation+0x2D0` is not an at-war
flag, and `combat.enemy_tags()` has no Windows path.  `declare_war()` therefore
returns `at_war_verified: None` and the only positive confirmation is the game's
own notification.  Finding the real war container is the next task, and it also
unblocks `war_readiness_check`.

## 35. Reading the war state (Windows)

`CWarRelation` **is** the war object and it is readable.  The reason earlier
scans found "one" war was that `ObjectIndex` is a **memory snapshot** cached per
pid (`objindex.py` `_load_cache`): it cannot see objects created after the scan,
so a freshly declared war is invisible until a rescan.  `oi.build(force=True)`
re-scans in ~25 s and the count went 1 -> 2 immediately.

Layout, confirmed on both wars present in the 1936 save:

    CWarRelation   vtable 0x29DCC08   sizeof 0x1D0
      +0x10  int  attacker country id
      +0x14  int  defender country id
      +0x78  int  attacker id (second copy)
      +0x7C  int  defender id (second copy)
      +0xD4  int  war id           (631, 632 in that save)
      +0xE8  int  defender id
      +0xEC  int  attacker id
      +0x144 int  war id

Read out live:

    Italy vs Ethiopia      -> (16, 44)
    Germany vs Dominican   -> (1, 73)     <- the declaration from 34

`calling/combat.py` now has `_war_relations_win()`, and `war_relations()` /
`enemy_tags()` dispatch to it on Windows (`sys.platform == "win32"`).  Both
accept `refresh=True` to force the rescan; everything else uses the cached index.
They return the same shape as the ELF path, so `combat.enemy_tags(g, "GER")`
works unchanged.

End-to-end check, one session:

    enemy_tags(GER) before      -> ['DOM']
    declare_war('LUX', force)   -> at_war_verified: True
    enemy_tags(GER) after       -> ['DOM', 'LUX']

So `game.declare_war()` now reports a real yes/no instead of guessing from the
queue, and `war_readiness_check` has a Windows backing for `enemy_tags`.

Caveat: `+0x10/+0x14` are country ids (the `CCountry+0x08` space).  The ELF notes
in `combat.py` warn that one of the side fields mixes two id spaces for
civil-war countries; the `+0x78/+0x7C` copy agreed on both wars seen here, and
the reader prefers it, falling back to `+0x10/+0x14`.  Nothing with a civil war
has been exercised yet.

## 36. M3 read side: the division table (Windows)

`CO_ARMIES = 0x290` in `CCountry` is a `CPdxArray<CArmy*>` and **`CArmy` is one
division**, not an army group.  Verified live across the whole map:

    countries with divisions   89
    total divisions            1227
    SOV 139   CHI 87   FRA 74   JAP 60   ITA 46   POL 42
    ENG  38   USA 36   ROM 31   TUR 31   GER 30

The distribution matches a 1936 start, and `game.divisions(tag)` returns the
right **localised template name** per country, which is the strongest check
available without reading the UI:

    GER  Infanterie-Division
    SOV  Strelkovaya Diviziya
    POL  Dywizja Piechoty
    ITA  Divisione di Fanteria / Divisione Alpina
    FRA  Division d'Infanterie / Division Coloniale
    ENG  Infantry Division

`game.divisions()` needed **no Windows changes**: the ELF offsets
`CArmy+0x3B8` (division template) and `CArmy+0x1F0` (province) hold on the PE
build too, and `CO_ARMIES 0x290` is already what the method reads.

### `GS_ARMIES 0x640` is not what the port plan assumed

The port plan said to read `GS_ARMIES 0x640` as the global army list.  It is a real
`CPdxArray<CArmy*>` (36 entries, elements are genuine `CArmy` with vtable
`0x29D6D98`), but 36 cannot be the world total when the countries alone hold
1227.  GER's array and `GS_ARMIES` share their first element, so it looks like a
**selection/temporary** buffer rather than the master list.  Its actual role is
unknown; nothing should depend on it.  Per-country `CO_ARMIES` is the
authoritative source.

Not decoded yet inside `CArmy`: organisation, strength, equipment, experience,
order/assignment state.  The order side of M3 (draw a front, assign, execute)
has not been started.

---

## 37. M3 order side, part 1: CArmy fields and building an army (Windows)

### 37.1 The two pure getters in the CArmy vtable

`.?AVCArmy@@` has four vtables in this build (0x29D6D98, 0x29D6F78, 0x29D6FE8,
0x29D7030) -- it is a multiply-inheriting class.  The live object's vtable is
0x29D6D98.  Of its 96 slots only two are plain "read one field" getters:

    31  0x00CE4D70   mov rax, qword ptr [rcx + 0x420] ; mov [rdx], rax ; ret
    36  0x00CE4D60   mov rax, qword ptr [rcx + 0x428] ; mov [rdx], rax ; ret

Both are 8-byte fields in 1e5 fixed point.  Grouped by division template over
Germany's 30 divisions the values are identical per template:

    template                     n    +0x420        +0x428
    Infanterie-Division         24    227.200       53.4545
    Panzer-Division              3     72.406       23.3333
    Infanterie-Division (mot.)   1    251.700       50.7500
    Kavallerie-Brigade           1    102.200       52.5000
    Gebirgs-Brigade              1    120.000       73.5000

So `+0x420` is the division's strength in HP points and `+0x428` its
organisation -- the same split the Linux table used, and the only assignment
whose magnitudes make sense (no division has 227 organisation, and 53 HP is
not a division).  The template does *not* hold these numbers; they are computed
per division at runtime (a scan of `CDivisionTemplate` for the same fixed-point
values found nothing), so "current == max" here only because the game is at
peace and every division is fresh.

**`+0x430` is still unidentified.**  It reads 2880.0 / 1650.0 / 3570.0 /
1440.0 / 1800.0 for the five templates above.  The Linux table calls this
"A_XP", but 2880 is not an experience value, so that label is *not* carried
over.  Do not report it as experience.

### 37.2 COrdersGroup: the constructors

`.?AVCOrdersGroup@@` has two vtables, 0x29CEE58 and 0x29CEED0.  Exactly two
functions install both: `0x00C3F480` (ctor) and `0x00C3F800` (copy ctor).
`0x00F702D0` is the allocator wrapper:

    0x140f702ee  mov ecx, 0x230          ; sizeof(COrdersGroup) >= 0x230
    0x140f70302  lea rdx, [rbp + 0x48]
    0x140f70306  mov r8, rbp             ; rbp = the CTheatre
    0x140f7030c  call 0x140c3f480        ; COrdersGroup(this, &theatre+0x48, theatre)

and `0x00F70110` is the sibling that allocates 0x250 bytes and calls
`0x00C3F430(this, theatre->0x48, theatre)` -- the army *group* flavour.

### 37.3 COrderGroupCommand on the PE build does not take a CTheatre

The Linux ctor signature is `(this, CPdxArray<CArmy*>*, CTheatre*, CArmyGroup*)`.
The Windows one is **different**: `0x0018C8D50` saves `rdx` and `r8` and ends

    0x1418c8f4a  mov r8, qword ptr [rsi + 0x40]   ; rsi = arg2
    0x1418c8f4e  mov rdx, rbp                     ; rbp = arg1
    0x1418c8f51  mov rcx, rbx
    0x1418c8f54  call 0x1418e0810

It has already stored `[[rsi+0x40]+8]` (a handle), `[GetTheatre(rsi)+8]` and
`[rsi+8]` into `cmd+0x48/0x50/0x58`.  Those three fields are handles, and
`rsi+0x40` being a *pointer to the theatre* is exactly `OG_THEATRE` -- so **arg2
is a parent COrdersGroup, not a CTheatre**.  Passing a CTheatre makes the ctor
read the array allocator as if it were a group (CTheatre+0x40 is the alloc field
of its own `CPdxArray` at +0x30) and would crash.  Three parameters, not four.

### 37.4 The function that actually creates the army

`COrderGroupCommand::Execute` starts at `0x0018D6560`:

    0x1418d656a  cmp dword ptr [rcx + 0x3c], 0     ; cmd+0x3c = 1 -> take the "group" path
    0x1418d6573  cmp byte ptr [rcx + 0x28], 0
    0x1418d6579  add rcx, 0x48                     ; theatre handle
    0x1418d65d4  movzx r9d, byte ptr [rbx + 0x28]  ; flag
    0x1418d65d9  lea r8, [rcx + 0x30]              ; divisions
    0x1418d65dd  lea rdx, [rcx + 0x48]             ; theatre handle
    0x1418d65e1  add rcx, 0x50                     ; parent handle (may be a null handle)
    0x1418d65e5  call 0x140c417a0

    0x00C417A0 (out_parent_handle, theatre_handle, CPdxArray<handle>*, u8) -> COrdersGroup*

`0x00C417A0` resolves the theatre handle, calls `0x00F702D0(theatre,0,0,0,0)`,
stores the flag byte at `+0x198`, then walks the array -- **8-byte handles, not
CArmy pointers** -- resolving each with `0x142284870` and attaching the division
(it takes `resolved - 0x10` as the CArmy base).  A null parent handle is
accepted, which is what makes the first army of a country possible at all:
in this save *no country has a single COrdersGroup* (every `CArmy+0xC0` is 0,
`CTheatre+0x30` is an empty `CPdxArray` for GER/FRA/SOV/ITA/ENG/POL).

Calling `0x00C417A0` directly is the accepted path of the command minus the
command object and the queue -- so it also works while the game is paused.

Verified live:

    create 4 divisions -> 0x278BDE21BB0  handle {53,505}  +0x39=0 (army)
                          +0x40 = the theatre, +0x50 = 4 divisions, +0x1B8 = 0
    game.create_army(count=6) -> "第2集团军" handle {53,506}, 6 divisions
    unassigned 30 -> 20

`OG_NAME 0x160` is still a `std::string` and the game fills in the localised
default name ("第1集团军"), which is a good sanity check that the object really
is a live army and not a shell.

### 37.5 Wrong tool: calling game.malloc on Windows

`Game.malloc` goes through `O.FN_OPERATOR_NEW`, which is `None` in the Windows
offset table, so it raises `TypeError: %X format: an integer is required`.  Use
`cmdwin.alloc(g, size)` (it posts `OP_NEW` with the module base added) for every
buffer on this platform.

---

## 38. M3 order side, part 2: the order primitives (Windows)

### 38.1 Where Execute lives

`CCommand::Execute` is **vtable slot 10** on this build.  Proof: the accepted
path of `COrderGroupCommand` (`0x0018D6560`, the function that calls
`0x00C417A0`) sits in slot 10 of vtable 0x2A84560.  Slots 9 and 11 are the
validator and the type getter (`slot 11 = 0x0018DCB90: mov dword [rdx],0x3327`).

### 38.2 COrderNewFrontCommand

vtable 0x2A853A0, sizeof **0x70** (its two factories `new 0x70` at 0x18CE150 /
0x18D03B0), primary ctor `0x0018C9840` (734 bytes).  Execute = slot 10 =
`0x0018D9070` (2481 bytes), and it only calls four routines outside itself:

    0x18d9221  call 0xc41620        ; rcx = og
    0x18d9229  call 0xc41bb0        ; rcx = og  -> order, then +0x119 = cmd+0x6e
    0x18d92fb  call 0xc41720        ; rcx = og, edx = 2
    0x18d9352  call 0xc41720        ; rcx = og, edx = 1
    0x18d9954  call 0xc4c170

CanExecute = slot 9 = `0x0018DF550`, and it tests exactly: `cmd+0x5c != 0`,
`0xBBB250(&cmd+0x28)` (a handle is non-null/valid), the resolved `cmd+0x28`
object, `0xBBB250(&cmd+0x30)`, `cmd+0x68 != 0`, and the bytes `cmd+0x6d` /
`cmd+0x6e`.  So `cmd+0x28` is a **handle to the COrdersGroup**, `cmd+0x30` a
second handle (parent/army group), `+0x5c` and `+0x68` ints, `+0x6d/+0x6e`
flags.  `0xBBB250` is the "handle is not null" test (`[h]|[h+4]`).

### 38.3 Creating one COrderInstance

    0x00C41720(COrdersGroup* og, int type) -> COrderInstance*   (sizeof 0x3C8)

    0x140c41734  mov ecx, 0x3c8
    0x140c41739  call 0x1425a01dc        ; operator new
    0x140c4174b  lea rcx, [rdi + 0x3c]
    0x140c4174f  call 0x140c07690        ; the group's order-id manager
    0x140c41754  inc dword ptr [rax + 0x1b0]   ; next id
    0x140c41769  call 0x1410adce0        ; COrderInstance(this, type, og, id)

`COrderNewFrontCommand::Execute` asks for type 2 and then type 1, and passes
the cmd's `+0x6d` / `+0x6e` bytes into the order at `+0x118` / `+0x119`.

Not decoded yet: the province path on the order (which offset holds the
`CPdxArray<i32>`), the group's execution type, and `0x00C4C170`.  Next step is
to call `0x00C41720(og, 1)` on the live game, read the fresh `COrderInstance`
back and map its fields against the cmd's geometry, before any of it is wired
into `armyops_win`.

---

## 39. The NewFront crash of 2026-11-14, and what it proved

### 39.1 What was called

    cmd = new(0x70)                                   ; COrderNewFrontCommand, zeroed
    call 0x18C9840(cmd, og, &emptyProvinceArray, 0, &out, 0, 0, 0)

The game died inside the ctor (`ReadProcessMemory ... GetLastError=299`, process
gone).  It was not the province array.

### 39.2 The cause

    0x18C99F9  cmp qword ptr [rbx + 0x38], r13     ; rbx = arg3 = 0  -> reads 0x38 -> AV

The ctor dereferences **arg3 as a COrdersGroup\* candidate** *before* any null
check: it compares `arg3->[0x38]` against the army (r13 = arg1) and, when they
match, copies `arg3->[0x244]` into `cmd+0x68`.  So arg3 is a **parent
COrderInstance**, and 0 crashes.

### 39.3 What the same disassembly confirmed as correct

* `arg2` really is `CPdxArray<i32>*` of **province ids**:

      0x18C9A12  movsxd rbx, dword ptr [rdi + 0xc]   ; size
      0x18C9A16  mov rdi, qword ptr [rdi]            ; data
      0x18C9A27  mov r8, rbx ; shl r8, 2             ; 4-byte elements
      0x18C9A34  call memcpy

* `arg1` is the COrdersGroup: its handle (`arg1+8`) goes to `cmd+0x28`, which is
  exactly what CanExecute (slot 9, `0x18DF550`) tests first.
* the 5th argument (stack, `[rsp+0x20]`) is an out-pointer; the UI passes
  `&[rbp-0x79]` there and `&[rsp+0x50]` (the province array) as arg2.
* after the ctor the UI calls `0x1402B60C0(cmd, X)` -- that, not `Post`, is how
  the army panel submits an order command.

### 39.4 The corrected recipe (next attempt)

A front needs a **parent order** first, which is why
`COrderNewFrontCommand::Execute` itself creates order type 2 (`0xC41BB0`) before
the two `0xC41720` calls:

    root = call 0x00C41BB0(og)        ; type-2 COrderInstance, +0x299 = 1
    cmd  = new(0x70)
    call 0x18C9840(cmd, og, &path_i32_array, root, &out, 0, 0, 0)
    call 0x1402B60C0(cmd, 0)

Do **not** retry with arg3 = 0.

### 39.5 Restart recipe (the display is 2560x1440, the DPI-unaware view is 2048x1152)

PowerShell `SetCursorPos` + `mouse_event` is ignored by this game.  Use the
windows-mcp `Click` tool, whose coordinates are **physical** (2560x1440):

    single player  (1338, 466)
    continue       (1277, 553)

Then poll `cmdwin.player_tag_id(g)` until non-zero (it read 1 within one poll
after the save finished loading).  `game.date()` can report nonsense right after
a load -- it returned 1945-04-02 on a 1936 start; cross-check with division
counts instead (GER 30 / SOV 139 / FRA 74 / ENG 38 / USA 36 is the 1936
scenario).

### 39.6 What survived the crash

Nothing in memory: the two armies were never saved.  The 1936-03-01 autosave on
disk is untouched (mtime 2026-11-13 23:47).  Re-verified after the restart that
`armyops_win.create_army` is the only thing that has to be re-run.

## 40. M3 order side, part 3: the front line is `COrderNewRootCommand` (Windows)

**The wrong class cost five game processes.**  `COrderNewFrontCommand` is a
*child* front — an offensive line drawn on top of an existing root front.  A
front line dragged along a border is `COrderNewRootCommand`.  Its ctor refuses
to run (and `Execute` walks off the end) unless `og+0x98` already holds the
parent order, which is why every earlier attempt produced "CanExecute 1,
Post 1, nothing in the list".

### 40.1 Where a front lives

    CTheatre + 0x50   CPdxArray<CFront*>        (10 fronts for Germany in 1936)
    CFront   + 0x08   {i32 type = 66, i32 id}
    CFront   + 0x20   CPdxArray<CProvince*>     border provinces
    CFront   + 0x58   CPdxArray<CFrontSection*>
    Section  + 0x08   i32 section id
    Section  + 0x30   CPdxArray<CProvince*>     provinces of that section

Germany's Dutch front is `{66,7}` with provinces
`[3444, 3512, 6622, 9281, 9522, 11264, 11346, 11360]`, one section, id 1.
Those eight provinces are exactly what `map/provinces.bmp` adjacency gives for
"GER province touching a HOL province", so the offline adjacency cache
(`stage/prov_adj.pkl`) can be trusted for planning.

### 40.2 The 8-argument constructor is 0x18C9B20

    0x18C9B20(cmd, COrdersGroup* og, void* handle_buf, i32 section,
              i64 from, i64 to, CPdxArray<CArmy*>* divisions|0, u8 flag)

The stores prove the order of the arguments:

    0x18C9C0C  mov rax,[rdi]        ; rdi = arg3 = handle_buf
    0x18C9C0F  mov [r14+0x48],rax   ; cmd+0x48 = the CFront handle
    0x18C9C13  mov [r14+0x50],ebx   ; cmd+0x50 = section (r9d)
    0x18C9C1F  mov [r14+0x58],rax   ; from
    0x18C9C2B  mov [r14+0x60],rax   ; to
    0x18C9C3B  mov [r14+0x6C],al    ; flag

and cmd+0x28 receives the COrdersGroup handle from arg2.  `0x18C9D90` is the
*other* ctor of the same vtable (0x2A856A0): it takes a `CFront*` directly and
has **seven** arguments, so calling it with the eight-argument convention
shifts everything and kills the process — that was crash #5.

### 40.3 Submission is `Post`, and the engine attaches the order itself

`CanExecute` (0x18DF5E0) returns 1 and `cmdwin.post()` returns 1; on the next
tick the engine appends a `COrderInstance` (type 2) to `og+0x98` with
`+0x244` = order id, `+0x258` = the CFront handle, `+0x260` = section,
`+0x268/+0x270` = from/to.  Nothing has to be pushed into the array by hand —
and hand-pushing is what killed crash #3.

Verified live on PID 26368:

    cmd vt      .?AVCOrderNewRootCommand@@
    +0x28       {53,489}          the army
    +0x48       {66,7}            the CFront
    +0x50       1                 section
    +0x58/0x60  0 / 100000        the whole front
    CanExecute  1
    Post        1
    og+0x98     1 order, type 2, id 1

`armyops_win.fronts()` / `draw_front_line()` / `orders()` wrap this.

### 40.4 Two API facts worth keeping

* `CState+0x18` is a `CPdxArray<CProvince*>`, **not** a flat `i32` array.  Reading
  it as ints yields pointer halves (e.g. `522046240`) and silently produces an
  empty province set.
* `CTheatre+0x18` is `CControllerArea`, and `CTheatre+0x30` is a second, always
  empty `CPdxArray`.  The fronts are at `+0x50`.

### 40.5 Crash ledger (all self-inflicted, all before Post)

  1. `0x18C9840` with a null parent order — the ctor dereferences arg3.
  2. `0xC41BB0` called with a vtable address mistaken for a COrdersGroup
     (one missing dereference) — wrote into `.rdata`.
  3. A bare order pushed into `og+0x98` without the engine's back-pointers.
  4. `0x18D9070` (child-front Execute) on a command whose front was invalid.
  5. `0x18C9D90` called with the eight-argument convention.

The rule that follows: **only submit through `CanExecute` + `Post`, and never
fabricate an object the engine is supposed to own.**

## 41. M3 order side, part 4: divisions, war, and a save that survives

### 41.1 The division list must be raw pointers

`COrderNewRootCommand`'s 7th argument is a `CPdxArray<CArmy*>` — **pointers**.
Feeding it `_mkhandlearray` (the 8-byte division handles used by
`create_army`) hangs the ctor: the injector times out with "thread left inside
game code" and the process dies minutes later.  `_mkptrarray` is the correct
builder.

With the real pointer array the same call is instant and the engine attaches the
divisions to the order:

    order +0x210   CPdxArray<CArmy*>   size = 4     <- the army's divisions
    order +0x88    CPdxArray<CProvince*>           <- the front's provinces

and the four divisions immediately start marching (province ids changed every
10 s: 3312/9496/9428/9428 -> 6521/11505/375/3499 -> ...).

Drawing a second front line on the same section does **not** replace the first;
it appends a second type-2 order.  Do it once, with divisions.

### 41.2 The war really was declared; the reader was stale

`declare_war()` returned `posted = 1` and the game raised its own World News
popup ("德国再一次陷入战争").  `combat.war_relations(g)` still said zero because
the Windows reader scans the object index, which is a **snapshot taken at
attach time**; pass `refresh=True` (12 s) and the war appears:

    {'enemy': 'HOL', 'me': 'GER'}   and   {'enemy': 'INS', 'me': 'GER'}

(INS joins because the Dutch East Indies are a subject of the Netherlands.)

### 41.3 Pause is a byte, not a keystroke

`idler = *(base + G_IDLER)`; `idler + 0x6C1` is the pause flag (`session.is_paused`).
Writing 0 unpauses reliably, which keyboard injection does not — the game
re-pauses whenever it loses focus, and a World News popup pauses it too (click
the popup's button to dismiss).

This is why several earlier "posted but nothing happened" tests were void: the
command queue only drains while the byte is 0.

### 41.4 Save before experimenting

The 1936 start is fragile and every mistake costs a 110 s relaunch.  With the
army, the front order and the war in place, an in-game save
(`GER_1936_03_02_12.hoi4`, 41.8 MB) restores all of it — verified: after a
crash, "continue" brought back the army (4 divisions) and the type-2 order with
`root_front {66,7}`.

**Rule: save before every new native call whose signature is not yet proven.**

### 41.5 Still open

* `COrderExecuteCommand` (the green "execute battle plan" tick).  Its vtable is
  0x2A85D60 (CanExecute 0x18DEFB0, Execute 0x18D64A0).  The assumed ctor
  0x18C8C50 with `(og, 1, 1, 0)` **crashed the process**, so that ctor is either
  a different class or takes different arguments.  Not solved.
* The offensive line builds and posts (child `COrderInstance` type 1 appears
  under the root order) but its target-province list was not found inside the
  child, so it is not proven to do anything yet.

### 41.6 Crash ledger, additions

  6. `draw_front_line` with a handle array instead of a pointer array.
  7. `0x18C8C50(og, 1, 1, 0)` — assumed OrderExecuteCommand ctor.

## 42. The execute flag is a byte on the order (`order+0x248`)

`COrderExecuteCommand::Execute` (0x18D64A0) is three lines of real work:

    rsi = resolve(cmd + 0x28)          ; the COrdersGroup
    edx = [cmd + 0x30]                 ; an ORDER ID, not a mode
    if (edx) { rax = 0xC402A0(group, edx);  edx = [cmd + 0x34];
               jmp 0x10CD510(rax, dl, 1); }

and `0x10CD510` starts with

    0x1410CD534  mov byte ptr [rcx + 0x248], dl

so **`COrderInstance + 0x248` is the "this order is executing" flag**, and the
whole command class can be skipped by calling `0x10CD510(order, 1, 1)` directly.

Verified: the flag goes 0 -> 1 on both the root order and the type-1 child, the
game keeps running (pause byte stays 0) — but **the divisions still do not
advance**.  A front line is defensive; the attack has to come from the child
front, and the child we create carries no target-province list, so it is an
empty order.  That is the remaining gap: `COrderNewFrontCommand`'s 3rd argument
(the `CPdxArray<i32>` path, copied to `cmd+0x50`/`cmd+0x5C`) never turns into a
province array inside the resulting `COrderInstance`.

### 42.1 Popups silently stop the clock

A World News popup ("土耳其共和国实现海峡再军事化") held the game for a full
90 s while `idler+0x6C1` read 0 the whole time: the popup pauses the game
without touching the pause byte.  Before every "it did nothing" conclusion,
screenshot the screen and dismiss the popup.

### 42.2 What is proven end-to-end now

    create_army(4 divisions)                     -> COrdersGroup, CArmy+0xC0 set
    draw_front_line(og, CFront{66,7}, sec 1, divisions)
                                                 -> type-2 order in og+0x98,
                                                    order+0x210 = 4 CArmy*,
                                                    all four march to the border
    declare_war(HOL)                             -> World News + war_relations
    0x10CD510(order, 1, 1)                       -> order+0x248 = 1

## 43. The front generator works; the child order does carry its provinces

Hooking `0x10B8620` during an offensive-line `Post` settles the open question:

    in   path=<CPdxArray<i32> cap=7 size=7>  parent=<root COrderInstance>  flag=0x101
    out  ret = 0x1
         out+0x00 CPdxArray: data, cap=1, size=1
         out+0x0C = 1          <- exactly the field Execute tests for zero

So the generator does **not** fail.  Earlier "it produced nothing" was a wrong
conclusion drawn from scanning only for `CPdxArray<CProvince*>`; the child front
stores its province line as a plain **`CPdxArray<i32>` at `COrderInstance+0x70`**.

Live dump of the children under the root front order:

    type=1 id=8   +0x70 n=5 [6622, 9363, 6241, 391, 3314]   exec=1
    type=1 id=11  +0x70 n=5 [6622, 9363, 6241, 391, 3314]   exec=1
    type=1 id=14  +0x70 n=5 [...]  +0x210 CArmy* n=4        exec=1
    type=2 id=6   +0x70 n=7 [68, 3211, 3262, 3314, 6336, 8622, ...]  +0x2a0 = 7
    type=2 id=9   ...                                                +0x2a0 = 10
    type=2 id=12  ...                                                +0x2a0 = 13

`+0x2a0` on a type-2 child is a single order id (7/10/13), i.e. the section
order points at the child front it belongs to.  `+0x1e0` / `+0x1f8` on a type-1
child are single-element `COrderInstance*` arrays.

**Still no advance.**  Every order involved has `+0x248 = 1`, the type-1 child
holds four `CArmy*` and a five-province line, and the four divisions sit on
front provinces 3444 / 9281 / 11264 / 11346 through a full minute of running
time (pause byte 0, no popup).  The army executes nothing, so the missing piece
is no longer the command — it is the group-level "battle plan is running" state
that the green tick owns.

Next probe: diff `COrdersGroup` bytes before/after pressing the green tick in the
UI once.  That is a byte-level question and needs no new constructor.

## 44. `CArmy+0xD8` is the division's current order, and it is what makes it fight

Scanning each `CArmy` for pointers into the order tree found exactly one hit per
division: **`CArmy+0xD8`**, pointing at the root front order (type 2).  Pointing
that field at the type-1 assault order (the child front) instead is what turns a
"deployed" division into an attacking one:

    div0 +0xD8: 0x252a03d8e00 (root, type 2)  ->  0x252c042d5b0 (type 1)
    after ~2 game days: org 53.5 -> 23.4 , hp 227.2 -> 224.7
    div3 (already on the child):        org 15.8 , hp 220.3

So the assault chain is: root front order (defensive) -> type-1 child front with
an `i32` province line at `+0x70` -> divisions whose `+0xD8` names that child.
The engine does **not** repoint `+0xD8` by itself and does not reject the write.

### 44.1 The Dutch border is defended by 8 divisions

    HOL divisions (1936-04): 8, sitting in 9363 / 9309 / 9403 / 11562 /
    153 / 11318 / 480 / 6336 — i.e. exactly the provinces the assault line
    targets.

Four attackers against eight defenders across five provinces do not move the
line (10 minutes of wall clock: org 23.3 -> 22.9, no province change).  A second
army of 20 divisions was raised on the same CFront section, given its own
front-line order and its own assault order, and all 20 had `+0xD8` repointed.

### 44.2 The clock, not the code, is the remaining limit

The build runs at roughly **3 game hours per real minute** at the current speed
setting (speed 1).  A 30-day campaign therefore needs about four hours of wall
clock.  Speed could not be raised from the code: `idler+0x6C0` (an int written 4)
and the speed dial clicks both left the rate unchanged, so the speed field is
still unknown.

A background watcher (`stage/wlong.py`, log `stage/m3_watch.log`) samples the
date and both army sizes every two minutes for an hour.

## 45. Where the crashes actually went: `Documents\dsh`

Seven self-inflicted process kills in one session produced crash reports, and the
game wrote them to **`%USERPROFILE%\Documents\dsh`** — not to the Paradox
`crashes` folder, which stayed at 0.09 GB the whole time.  That folder grew until
the system disk hit **0 bytes free**, which took the whole tool host down
(`ENOSPC` on every command) until it was deleted by hand.

Two things came out of it:

* `%LOCALAPPDATA%\CrashDumps` also collects one ~46 MB minidump per crash
  (`hoi4.exe.<pid>.dmp`); ten of them were sitting there.
* A **directory junction** now puts the dump folder on the data disk:

      %USERPROFILE%\Documents\dsh  ->  <data-disk>\crashdumps

  Verified: a probe file written through the junction leaves `C:` free space
  unchanged.

Rule from here: after any crash, check both locations before starting the next
experiment.  The junction keeps the next one off the system disk, but a crash
still costs a relaunch.

## 46. Game speed is a command, and the clock is the real bottleneck

### 46.1 Why the campaign crawled

Every M3 attempt so far ran at **speed 1**: measured 1936.04.01.04 -> 04.02.05 in
59 s, that is 0.5 game-hours per real second, one game-day per 48 s. A 30-day
operation would have taken 24 real minutes of pure game time, and the *previous*
watcher measured 600 s per game-day, which means the clock was stopped most of
the time (see 46.4).

### 46.2 `CIncreaseGameSpeedCommand` is the safe lever

    CIncreaseGameSpeedCommand   vtable 0x29F3900
    CSetGameSpeedCommand        vtable 0x29F3A80
    CDecreaseGameSpeedCommand   vtable 0x29F39C0   <- crashes when built this way

None of them needs a constructor.  Build the object by writing the vtable and
lets `Post` run the real `CanExecute`:

    cmd = cmdwin.alloc(g, 0x20); p.write(cmd, b"\0"*0x20)
    p.write(cmd, struct.pack("<Q", base + 0x29F3900))
    cmdwin.attach_fake_vtable(g, cmd, 0x29F3900)   # no-free slot 0
    cmdwin.post(g, cmd)

`cmdwin.build_by_vt(g, vt_rva, size, fields)` now wraps exactly that.

Four posts took the game from speed 1 to speed 5: the clock went from 0.5 h/s to
**1.7-2.7 game-days per real second** (1936.05.15 -> 1936.08.16 in 55 s).
`CDecreaseGameSpeedCommand` built the same way took the process down, so the only
way down is to reload.

### 46.3 `game.set_game_speed()` was a process killer on Windows

`commands.py` is the Linux module.  `CommandBuilder.new()` calls
`FN_NEW = 0x510950`, a Linux virtual address; on the PE image that jumps into
unrelated code and kills the game (measured, one relaunch).  `game.set_game_speed`
went through it.

Fixed: `game.set_game_speed` takes the Windows branch, `cmdwin.set_game_speed`
builds `CSetGameSpeedCommand` by vtable, and `commands.new()` now raises on
`win32` instead of jumping.

### 46.4 A popup advances exactly one hour, then the game stays paused

Dismissing a World-News / event popup produces **one game-hour of progress** and
nothing more, even though `idler+0x6C1` reads 0.  The pause only lifts when the
round pause button in the top bar is clicked:

    (2250, 38)   on a 2560x1440 display, windowed or fullscreen

So the loop for any long run is: dismiss the popup, then click the pause button.
`idler+0x6C1 = 0` on its own is not enough.  A run that ignores this looks exactly
like a hung game: the date never moves.

Popups seen in the first two game months: 意大利王国送出军事援助,
对军备重整的日益担忧 (焦点完成), 第二次伦敦海军条约签署 (World News).

### 46.5 `g.date()` can lock onto a stale `CDate`

After a load it reported `1945.02.19`, then `1945.04.02`, then `1955.06.25`,
none of which matched the UI (`1936.02-05`).  `date_raw` keeps a cached address
and only rescans when the value has been static for 3 s, so a freed-but-valid
looking int32 survives.  **Check the date against the top bar before trusting a
timed measurement.**

### 46.6 `COrderExecuteCommand` is real and the engine accepts it

    ctor 0x18C8C50   vtable 0x2A85D60   size 0x38
    (this, COrdersGroup* og, i32 order_id, u8 flag, u8 flag2)
    +0x28 = og handle (copied from og+8)   +0x30 = order_id   +0x38? = flags

The old crash was calling it as `(og, 1, 1, 0)` -- that writes the command header
into the orders group at +0x24/+0x30 and corrupts it.  Built correctly and posted
with `order_id = 0` (the whole plan) the result is **1** for every orders group,
and the engine immediately creates a type-1 child order whose `+0x210` array holds
exactly the army's divisions (4/4 and 26/26 in the last run).

### 46.7 The offensive child still does not move the divisions

State after a clean setup on `GER_1936_03_02_12.hoi4`:

| step | result |
|---|---|
| `create_army(26 unassigned)` | ok, 26 divisions |
| `draw_front_line` on front {66,7} section 1 | ok, root order id 2 / 3 |
| offensive line over the 8 Dutch provinces | child id 6 / 9 |
| `execute_plan(og, 0)` | posted 1, child `+0x210` = 4 / 26 |
| `0x10CD510(child, 1, 1)` | child `+0x248` = 1 |
| `CArmy+0xD8 = child` for all 30 | sticks (30/30 after 4 s) |

and then **nothing moves**: over 12 s of speed-5 time all 30 divisions kept the
same province, three of them sat at org 23 with no Dutch org loss.  So neither
the assignments nor the executing flag is what makes a division march.  The next
thing to try is `COrderAssignCommand` (ctor `0x18C75F0`, signature
`(this, CPdxArray<handle>* divisions, COrdersGroup* og, i32 order_id)` --
calling it with a fifth argument took the process down) or the per-division
`CMoveCommand` path.

### 46.8 The game can hang, not just crash

After the `COrderAssignCommand` attempt the window title read
`Hearts of Iron IV (DirectX 11) (未响应)` and the process burned 1500 s of CPU
without ticking.  `Get-Process ... Responding` is the quick check; a hung process
still answers memory reads, so the Python side sees a live game.  Kill it and
reload.

### 46.9 Menu coordinates, windowed vs fullscreen

When the game comes up **windowed** the main menu sits about 10 px higher and the
save list starts at y~700 in the 1920x1080 preview, i.e. screen y~1003 for the
second save.  Clicking the old fullscreen coordinate there opens 好友列表
instead of 载入游戏.  Always screenshot the menu before clicking.

## 47. The *second* dump folder: `Documents\Paradox Interactive\...\dumps`

The `Documents\dsh` junction from section 45 was not the whole story.  HOI4 keeps
its own crash dumps in

    %USERPROFILE%\Documents\Paradox Interactive\Hearts of Iron IV\dumps

and while a game is hung (`(未响应)`) it writes them **once per second**:
`hoi4_YYYYMMDD_HHMMSS.dmp`, ~42 MB each.  After this round of M3 experiments the
folder held **1169 files / 49.0 GB** and `C:` had gone from 117 GB free to 68 GB.

Cleaned and junctioned the same way as `dsh`:

    %USERPROFILE%\Documents\Paradox Interactive\Hearts of Iron IV\dumps
        ->  <data-disk>\crashdumps\hoi4-dumps

Probe write verified, `C:` stayed at 117.5 GB, and no new dump appeared in 8 s.

So **three** places have to be watched after a crash or a hang:

1. `%USERPROFILE%\Documents\dsh`                (junctioned, section 45)
2. `...\Hearts of Iron IV\dumps`               (junctioned, here)
3. `%LOCALAPPDATA%\CrashDumps`                 (one ~46 MB WER minidump per crash)

A hung game is worse than a crashed one: it keeps burning CPU *and* keeps
producing dumps.  `(Get-Process hoi4).Responding` is the check; kill it at once.

## 48. `COrderAssignCommand`, four arguments: it works, and it changes nothing

### 48.1 The signature

Two constructors write vtable `0x2A84CE0`:

| ctor | size | signature |
|---|---|---|
| `0x18C75F0` | 616 B | `(this, i32* division_handle, COrdersGroup* og, i32 order_id)` |
| `0x18C7860` | 695 B | same plus a 5th `u8` read from `[rsp+0x80]` |

Fields written by `0x18C75F0`:

    +0x28  CPdxArray<handle>         (initialised empty by 0x11F6F0)
    +0x40  COrdersGroup handle       (copied from og+0x08)
    +0x48  i32 order_id
    +0x4C  u8  0

Object needs ~0x50 bytes; 0x80 is safe.

### 48.2 The crash last time was the argument, not the ctor

**arg2 is a pointer to ONE 8-byte handle**, not a `CPdxArray`.  The ctor does
`cmp dword ptr [rdi], 0 / lea rbx,[rdi+4] / cmp dword ptr [rbx], 0` and then
resolves it through `0x142284870`.  Handing it the CPdxArray header made it
resolve the array's *data pointer* as a handle and take the process down.

The handle lives at **`CArmy+0x18`** (verified: `type = 51, id = 1`), which is what
`armyops_win._mkhandlearray` already uses.  `CArmy+0x08` is **not** a handle --
it read `{0, 0}`.

### 48.3 Result

Called as `(cmd, d+0x18, og, child_order_id)` it posts cleanly for every division:

    OG 0x15f276fcba0 child id=6  posted 4/4    child +0x210 = 4
    OG 0x15f759b4d30 child id=9  posted 26/26  child +0x210 = 26

and `CArmy+0xD8` does not move, the `+0x210` array does not grow, and over 8 s at
speed 5 **0 of 30 divisions changed province**.  So the divisions were already
assigned to the child order; re-assigning is a no-op and assignment is not what
is missing.

The ctor's own owner check compares `*(i32*)(division+0x1C8)` with `og+0x3C`
(the country tag, 1 for GER); `division+0x1C8` holds `0x29C77F8`, the scope magic
that `cmdwin.SCOPE_MAGIC` already names.

### 48.4 Where that leaves the offensive

Known state of a synthetic offensive child order:

* created by `0x18C9840`, `CanExecute` 1, `Post` 1
* `+0x70` province line `[6622, 9363]` -- one German and one Dutch province
* `+0x210` holds the army's divisions
* `+0x248` = 1 after `0x10CD510(order, 1, 1)`
* divisions' `+0xD8` = the child

and the army still stands still.  The one field never observed non-empty is the
*movement path* a division needs; the next experiment should be a differential
one -- draw the arrow **with the mouse** once, dump that order, and diff it
against the synthetic one field by field, instead of guessing another ctor.

## 49. Differential against a real AI offensive order

### 49.1 The comparison partner

The AI draws its own battle plans, so a real type-1 "offensive line" order can be
read straight out of memory -- no UI dragging needed.  The Netherlands starts the
1936 scenario with one:

    HOL OG 0x245fa8c1110  divs=5  orders=[(4, type 2)]
       child 0x245fa967960  type=1  id=9   <- offensive line
       child 0x245fa963c60  type=2  id=7
       +0x70 -> CPdxArray<i32> cap=3 size=3  provinces [9363, 11346, 6535]

### 49.2 The two orders side by side

| offset | AI `0x245fa967960` | OURS `0x243de93fb50` |
|---|---|---|
| 0x00 vtable | `0x7ff658b826e8` | same |
| 0x08 | `0x7ff658b58b80` | same |
| 0x18 | 0 | **`0x100000000`** |
| 0x30 type | 1 | 1 |
| 0x38 owner OG | `0x245fa8c1110` | `0x245ade494e0` |
| 0x40/0x50/0x58/0x68 | module ptrs | same |
| 0x48 | `0x39f22cb` | `0x39f22eb` |
| 0x60 | `0x29c77f8` (scope magic) | same |
| 0x70 provinces | 3 items | 2 items `[6622, 9363]` |
| 0xa0 | -1 | -1 |
| 0xb0 | `0x100000000` | same |
| 0xb8 | 0 | **`0x4300000000`** (= handle {67,0}, CTheatre!) |
| 0xf8 | 0 | **`0x24500000000`** |
| 0x118 | `0x10000` | **`0x24300000000`** |

**Every meaningful field matches.**  The order is not the wrong type, it has the
right owner, the right scope magic, the right province array shape.  What differs
is that ours carries **uninitialised heap** in the fields the engine leaves alone.

### 49.3 The heap is recycled, and the engine does not zero it

The root order is worse.  `words()` on it shows float32 leftovers in the high
half of a dozen 8-byte slots (`0x3e218000`, `0x44f0682b`, `0x3d792c60`, ...) --
1876-ish values that look like coordinates -- and at `+0x120`:

    0x120 = "o_fallba"   0x128 = "nura"    0x130 = 0xc

that is a **leftover loc key from a fallback-line order** that previously owned
the same heap block.  This is exactly the defect the command layer already fixed
for itself in `commands.py` ("operator new hands back uninitialised heap ... the
game's CanExecute/Execute then acted on it"), but it applies to the *order
instances the engine allocates from our command* as well.

### 49.4 First movement ever observed

`child+0x248` read **0** -- the executing flag was not set on this child.  After
zeroing the four stale fields (`+0x18`, `+0xb8`, `+0xf8`, `+0x118`) and writing
the pause byte, **1 of the 4 divisions changed province within 10 s**
(6334 -> 6282), the first movement this project has seen from a synthetic order.
Not yet proof of causality -- it could have been an ordinary front-line shuffle --
but it is the only change the differential pointed at.

### 49.5 Next

1. Zero the stale fields **at creation time** (right after the child order appears,
   before the first tick) and measure movement over a controlled window with the
   game paused/resumed by clicking the pause button, not by writing the byte.
2. Same for the root order (the float leftovers and the leftover string at +0x120).
3. A `--dump` helper (`stage/wdumpai.py`) can now read any AI country's plan orders,
   so any future question about order layout can be answered by comparison instead
   of guessing a constructor.

## 50. M3: the Netherlands capitulates -- the whole chain works

### 50.1 What was run

On the loaded `GER_1936_03_02_12.hoi4`, entirely through `CCommand::Post`:

1. `armyops_win.create_army(g, 26 unassigned)` -> a second orders group
2. `draw_front_line` on front `{66,7}` section 1 for **both** groups
3. offensive line for each group: ctor `0x18C9840` over the provinces where Dutch
   divisions stand
4. `COrderExecuteCommand` (`0x18C8C50`, `order_id = 0`) for each group -- the engine
   then fills the child order's `+0x210` array with the army's divisions
5. `0x10CD510(child, 1, 1)` on every child
6. speed 5 (four `CIncreaseGameSpeedCommand` posts)

### 50.2 Result

```
1945.04.02.00  HOL=8  inHOL=0/30  war=True
1945.04.02.00  HOL=0  inHOL=0/30  war=True
*** Netherlands is out ***
```

and the game itself then raised its own screens:

* `阿姆斯特丹条约` -- the peace conference, Germany 2840 points, defeated
  `荷兰王国` and `荷属东印度`
* World News `缴获了荷兰的装备` -- *"荷兰王国已经停止抵抗，我们从他们的库存里得到了50%的装备"*

All Dutch states were selected in the conference (`全选`, 285 points).  **The
Netherlands is conquered by the automation, not by a console `annex`.**

### 50.3 The "divisions do not move" result was a measurement artifact

At speed 1 the clock runs 0.5 game-hours per real second, so a 10 s probe covers
**5 game hours** -- no division can march in that.  At speed 5 the same probe
measures 384 game-hours per 6 s (16 game-days/s):

```
rate after speed up: 384 hours / 6 s
moved 4/4   a [9363, 9309, 6622, 587] -> b [3444, 9281, 11562, 9363]
```

All four divisions moved, and `inHOL` went 0 -> 2 -> 0 as they fought in and out
of Dutch provinces.  Section 48's "the army stands still" conclusion was taken at
speed 1 and is **wrong**; the chain was working.

### 50.4 The real limiter is force size

A single 4-division army could not break 8 entrenched Dutch divisions: over nine
game years the front never moved.  Adding the 26-division second army produced
capitulation within one poll interval.  So `create_army` + a second front/arrow is
not optional.

### 50.5 Open ends

* `COrderInstance+0x248` is reset to 0 by the engine at some point; it has to be
  re-set with `0x10CD510(child,1,1)` after the plan exists.
* The peace conference is fully modal and **windows-mcp clicks do not register in
  it** (three separate attempts, with the window forced to the foreground).  The
  conference has to be finished by hand, or `CSelectEventOptionCommand`
  (vtable `0x29B6CE0`) has to be tamed.
* The 30-day figure is **not** measured: this war had already run since 1936.03 by
  the time the second army arrived.  A clean timed run needs a reload and the
  whole chain from turn one.

## 51. Clean timed run: 30 days is not enough, and the arrows evaporate

### 51.1 The measurement

Fresh load of `GER_1936_03_02_12.hoi4` (already at war with HOL), whole chain
built at speed 1, then clock started and speed raised to 5:

    T0 = 1936.03.02.19
      1936.03.02.19  HOL=8   (+0.00 days)
      1936.04.07.19  HOL=7   (+36.00 days)
      deadline 90 days, HOL=7

**Inside 30 game days the Dutch lose nothing.**  One division is destroyed by day
36 and nothing more by day 90.  The 30-day acceptance criterion is **not met**.

### 51.2 The offensive does advance, then stops

The front did move into the Netherlands -- `{66,7}` went from the German border to
`[11456, 8622, 3314, 9498, 6241]`, all Dutch provinces -- so the arrows work.  What
kills the attack is what happens next:

    OG 0x1c4424c4240 orders=[(1,2),(2,2),(18,2 front {66,1271} sec 2),(17,2 front {66,1271} sec 2)]
       children=[]                      <- every type-1 offensive order is GONE
       div order ptrs {root id 2: 3, front 1271 id 18: 1}

When the front advances the engine **creates a new front (`{66,1271}`)** and deletes
the child offensive orders; the divisions are handed back to the front line, which
only holds.  A real offensive therefore has to be re-issued every few game days --
a player redraws the arrow, our driver has to do the same.

### 51.3 What that means for M3

Two independent gaps, both real:

1. **Force quality/size.** 30 German 1936-pattern infantry divisions (no artillery)
   cannot break 8 entrenched Dutch divisions in three weeks.  Options: better
   templates (`templedit.py` / `duplicate_division_template` already exist), more
   divisions, or air support.
2. **Plan upkeep.** The arrow must be re-posted whenever the front moves.

Neither is a command-layer problem any more: `create_army`, `draw_front_line`,
the offensive ctor, `COrderExecuteCommand` and the division link all work.

## 52. How hard is driving the peace conference?

### 52.1 What exists

RTTI carries the whole conference, and three **commands**:

    CDonePeaceConferenceCommand      vtable 0x2AFC800     <- "submit demands / confirm"
    CEndTurnPeaceConferenceCommand   vtable 0x2AFC8C0     <- "skip this round"
    CPassPeaceConferenceCommand      vtable 0x2AFC740     <- "pass"

plus `CPeaceConference` (0x29CDA28), `CPeaceConferenceManager` (0x279EE58),
`CPeaceWinnerAi`, and the UI entry classes `CPeaceClickAllEntry`,
`CPeaceAvailableActionItem`, `CPeaceBiddingItem`.

### 52.2 Difficulty

| goal | mechanism | effort |
|---|---|---|
| close the conference | `CDonePeaceConferenceCommand`, same vtable+Post pattern as `SetGameSpeed` | **low** -- find the ctor with `wfindvt.py`, disassemble for the fields, post |
| skip a round / pass | `CEndTurnPeaceConferenceCommand`, `CPassPeaceConferenceCommand` | **low** |
| take all Dutch states (the 全选 we did by hand) | no command exists; the demands are UI list entries (`CPeaceAvailableActionItem`, `CPeaceClickAllEntry`) driving `CPeaceAction` objects | **medium-high** -- would need to walk the conference object's action list and invoke each item's handler |
| let the AI do it | `CPeaceWinnerAi` | unknown; it is the AI's own bidder, not a player-side shortcut |

### 52.3 What was actually observed with the mouse

* clicking a defeated country in the list **works**
* clicking `全选` **works** -- all Dutch states got the red X, 285 points
* clicking the stacked `World News` OK and `提交要求` **did not register**, in three
  attempts, with the window forced to the foreground

So the cheapest route is not the UI at all: post `CDonePeaceConferenceCommand` after
selecting demands, or accept that the conference has to be finished by hand once per
war.  Since capitulation only happens once per enemy, "by hand" is a viable stopgap
and does not block anything else in the pipeline.

## 53. The offensive never attacked because `COrderExecuteCommand` leaves it dormant

This is the real reason the arrows "did nothing" and then "evaporated", and it
supersedes the 30-day conclusion in §51: the plan was never executing.

### 53.1 The missing byte

`COrderExecuteCommand` (0x18C8C50) does everything it says: the engine creates
the type-1 child offensive, hangs it off the front order, and puts the army's
divisions into the child (`child+0x210` = `CPdxArray<CArmy*>`).  What it does
**not** do is set the order's own executing byte:

    COrderInstance + 0x248   u8   0 = dormant, 1 = executing

The engine sets it through

    0x10CD510   (order, u8 executing, u8)      ; [rcx+0x248] = dl, then
                                               ; rebuilds the order's caches

With `+0x248` left at 0 the order is drawn on the map (that is the arrow the
player sees), the divisions are listed as assigned, and nothing moves -- for
years.  Verified live 2026-11-14: same orders, only this byte changed, and
within one game day the divisions crossed into Dutch territory.

### 53.2 Result

With the byte set, from `GER_1936_03_02_12.hoi4`:

* 30 German divisions on front `{66,7}` section 1 (the 8 German border provinces)
* offensive drawn on the 7 Dutch provinces at BFS depth 2 from the border
  (the far side: Amsterdam / Den Haag / Utrecht / Arnhem belt)
* the Dutch army went from 8 divisions to 1, six provinces were taken and the
  **Netherlands capitulated** -- conference `阿姆斯特丹条约`, victor GER with
  2800 points, notification `荷兰王国已经停止抵抗`

So the whole M3 chain is confirmed working end to end.  The 30-day *number* is
still not measured: the run above was interrupted by the modal windows, and the
engine's `CDate` copies cannot be trusted for timing (see §53.4).

### 53.3 Two more defects this exposed

* `m3run.children()` accepts any 8-byte slot that looks like a
  `CPdxArray<COrderInstance*>`.  Once a group carries a few extra orders it
  returns heap noise, and `set_offensive_flags()` then wrote `+0x248 = 1` into
  arbitrary heap addresses.  That killed the game three times today.  The fixed
  version (`armyops_win.order_children`) requires the child's first qword to be
  the `COrderInstance` vtable `0x2A026E8`.
* re-issuing an offensive every 20 s (the naive "keep the arrows fresh" loop)
  piles up children: nine extra type-1 orders per group in three minutes, most
  of them `assigned=0`.  Re-issue only when the province count has actually
  stalled **and** delete or ignore the dormant ones.

### 53.4 The clock: do not trust any `CDate` copy

`api._win_date_addr()` keeps the first candidate it finds and accepts
`0 <= new - last <= 400 days`, which is true for a frozen copy, so a save-preview
string can pin the clock for a session (it read `1945.04.02` while the UI said
`1936.03.02`).  The value is an hour counter -- `(year*365 + doy)*24 + hour`
-- so a "plausible date" test must decode it, not compare it to `YYYYMMDD`.

`clock.py` in the stage folder scans for candidates that both decode into
1935..1965 **and** advance between two reads.  Even that is not enough while the
game is paused, and the live clock is not always among the copies that pass the
signature check.  For measurements, read the UI: the date strip is at
`x 2260..2420, y 10..30` of the 2560x1440 frame and the game renders at
2048x1152, so any coordinate measured on a `PrintWindow` image needs `x1.25`.

### 53.5 Geometry from the game's own map files

`mapgraph.py` rebuilds province adjacency and centroids from
`map/definition.csv` + `map/provinces.bmp` (HOI4 land adjacency is implicit in
the bitmap; `adjacencies.csv` only carries straits and sea crossings).  This is
what makes a correct offensive line possible at all: the Dutch provinces are
exactly three BFS layers from the German border --

    depth 0   6500 9309 9363 9403 11318 11456 11562
    depth 1   391 6241 6286 6496 9335 9498
    depth 2   68 3211 3262 3314 6336 8622 9431
    (480 is cut off by water)

and front `{66,7}` section 1 is exactly the German side of that border
(`3444 3512 6622 9281 9522 11264 11346 11360`), which the adjacency graph
confirms independently of the engine.

### 53.6 Manual push, if it is ever needed

`CMassMoveCommand` is the "select divisions, right-click a province" command:
vtable `0x2A2CEA0`, in-place ctor `0x13F6690`, size `0x60`:

    +0x28  CPdxArray<MoveTarget>   0x20 bytes per division
    +0x40  CPdxArray<handle>       the divisions
    +0x58  int32                   (= 3 at the UI call site)

Not built yet -- the plan orders are enough once `+0x248` is set.

## 54. Clean 30-day measurement: the Netherlands capitulates in ~13 game days

Recipe (all verified, no console):

1. load `GER_1936_03_02_12.hoi4` (war with HOL already declared)
2. dismiss the opening event with a mouse click at (1382,646) -- see 54.3
3. `wplan.py --setup --deep 2`:
   * `create_army` the 26 unassigned divisions
   * `draw_front_line` on front `{66,7}` section 1 for both groups
   * offensive over the 7 Dutch provinces at BFS depth 2
   * `COrderExecuteCommand` for both groups
   * `start_offensives()` sets `+0x248 = 1` on the two accepting children
4. mouse-click `+` three times (speed 4)

Result, read off the UI date strip (the only trustworthy clock, see 53.4):

    plan posted        1936.03.02 20:00
    first recorded     1936.03.04 12:00
    last before capit. 1936.03.15 11:00
    conference opens   ~1936.03.15 15:00

    -> 12.6 game days from order to capitulation
    -> Dutch army 8 divisions -> 1, six provinces occupied, 30/30 German
       divisions intact, conference 2800 points

That is well inside the 30-day target.  The earlier "the Dutch lose nothing
in 30 days" result stands as a measurement of a plan that was never executing
(53.1), not of a plan that was too weak.

### 54.1 Why the previous attempts stalled

Two independent mistakes, both now fixed:

* the offensive child was dormant (`+0x248 = 0`), so no division ever attacked;
* the offensive line was drawn on the provinces the Dutch divisions stood in,
  i.e. one province deep.  A battle plan advances **to** the line and stops
  there, so the line has to be the far side of the country.  `mapgraph.py`
  gives the BFS layers needed to place it.

### 54.2 What the run does not prove

The divisions start scattered across Germany and need ~6 days to reach the
border, so "order to capitulation" includes a march that a prepared player
would not pay.  Measured from the first contact it is under a week.

### 54.3 UI coordinates on this machine (2560x1440)

The game renders at 2048x1152 and is stretched to 2560x1440; every coordinate
below is in the 2560 frame, i.e. what a screen capture shows.

    event option 1     (1382, 646)
    event option 2     (1382, 694)
    speed -            (2271, 17)
    pause / play       (2298, 24)
    speed +            (2430, 24)
    load-screen save row 2   (150, 1022)
    load-screen 开始        (2470, 1414)

A previous session concluded that event and peace-conference windows "do not
accept clicks".  They do; the y coordinate was 50 px off.  `提交要求`,
`确认并退出` and the conference country list all respond as well.

## 55. The peace conference is command-drivable

### 55.1 The three commands

All three carry exactly **one int** at `+0x28` -- the same value the UI copies
out of the conference object before posting:

    CDonePeaceConferenceCommand      vtable 0x2AFC800   ctor 0x1ED1D70   size 0x30
    CEndTurnPeaceConferenceCommand   vtable 0x2AFC8C0                    size 0x38
    CPassPeaceConferenceCommand      vtable 0x2AFC740   ctor 0x1ED1E50   size 0x30

The in-place ctor pattern is the standard command prologue (`+0x08` byte 0,
`+0x0C` = -1, `+0x10` = 0, `+0x18` = 0xFFFF, `+0x1C` = 0) followed by
`[rcx+0x28] = *(int32*)rdx`.  `Execute` (slot 10) is two instructions: fetch the
conference manager, then tail-call the handler with `this+0x28`.  `CanExecute`
(slot 9) only checks that the manager exists.

### 55.2 Where the int comes from

The UI handler is `0x18F29E0`:

    0x1418f2b18  mov rcx, [rbx+0x58f8]     ; the conference object
    0x1418f2b49  mov rdx, [rcx]
    0x1418f2b4c  call qword ptr [rdx+0xa0] ; -> int*  (the value to submit)
    0x1418f2b58  call 0x141ed1d70          ; ctor(this, int*)
    0x1418f2b67  call 0x1422c4250          ; CCommand::Post

So the int is whatever vtable slot 0xA0 of the conference returns a pointer to.
In a single-winner war it is the player's country index: **1 for Germany**.

### 55.3 Verified

Posted `CDonePeaceConferenceCommand(this, 1)` from our own code while the
conference for the Netherlands was open.  The conference closed and the game
produced the treaty window

    「阿姆斯特丹条约 -- 荷兰王国与德意志第三国达成了无条件和平」

That is a white peace, which is exactly what "done" means when the bid list is
empty: the command submits *the bids you have*, and we had none.

`calling/peacewin.py` wraps all three: `peacewin.done(g, 1)`,
`peacewin.passed(g, 1)`, `peacewin.end_turn(g, 1)`.

### 55.4 What is still not a command

Choosing **what to take**.  The demands are data, not commands:

    CPeaceAction            0x2A4C570    one demand
    CStatePeaceAction       0x2A4C690    "take this state"
    CTakeNavyPeaceAction    0x2A4CC30    "take these ships"
    CPeaceBiddingTurn       0x29EAEC0    the turn that owns the action list
    CPeaceAvailableActionItem 0x2AFB688  the UI entry that appends one

There is no `CAddPeaceDemandCommand`; the window appends `CPeaceAction` objects
directly to the turn.  Two ways forward:

* **UI (cheap, already works)**: click the war loser in the list, click `全选`,
  then post `CDonePeaceConferenceCommand`.  Verified that the list and `全选`
  respond to clicks (54.3) -- the only thing that never worked was the stacked
  notification, and that was a coordinate error too.
* **Data (needs work)**: build one `CStatePeaceAction` per Dutch state, append
  them to the live `CPeaceBiddingTurn`, then post the done command.  Costs a
  ctor and an append; worth doing only if the whole pipeline must be headless.

### 55.5 Practical consequence

A war can now be *closed* without a single mouse click, and the whole M3 chain
(declare war -> front -> offensive -> execute -> capitulate -> close the
conference) is reachable from code.  Only the *contents* of the treaty still
need either a click on `全选` or a hand-built action list.

## 56. Peace demands: what a headless "take the states" would need

Option B (build the demands in code instead of clicking `全选`) needs one layer
more than the commands do.  What is known:

### 56.1 The demand object

    CStatePeaceAction   vtable 0x2A4C690   ctor 0x15B43A0   size 0x58
    CPeaceAction        vtable 0x2A4C570   ctor 0x15B43F0   (base, type in edx)

`CStatePeaceAction(this, int* a, int* b, int* c, int stateId)`:

    +0x08  0x30D0            type id
    +0x10  std::string       localised name, built from the type id
    +0x30  *a
    +0x34  *b
    +0x38  *a
    +0x48  stateId           <- the 5th argument
    +0x4C  0
    +0x50  *c

The only caller is the factory at `0x14F2870`, which builds the whole available
list.  At `0x1414F2E71` it allocates 0x58, reads the state id from
`[CState+0x58]` (`ST_ID`), and passes `&node+0x1c`, `&CState+0x10` and a value
from `0x140C07100` for the other three slots.  Those three are **not yet
mapped to a meaning** -- do not guess them.

`peacewin.py` therefore only wraps the lifecycle commands.  A
`add_state_demand()` that fills `+0x30/+0x34/+0x50` by guesswork would post
garbage into a live conference.

### 56.2 The layer that is missing

The bid structures are created **lazily**.  With a conference open and the
Netherlands defeated, a full heap scan found:

    CPeaceConference         1
    CPeaceBiddingTurn        0      <- does not exist yet
    CStatePeaceAction        0
    CTakeNavyPeaceAction     2

So the player's bidding turn only comes into existence once a demand is made.
The code that builds the turns is `0xEB9ED0` (it installs the
`CPeaceBiddingTurn` vtable and walks the beneficiary list).  A headless
implementation has to call that builder first, then append
`CStatePeaceAction` objects to the resulting turn's action array.

Until that is done, the practical sequence is: pick the demands in the window
(`要求` -> the province icon -> the states on the map, or `全选` where it is
offered), then post `CDonePeaceConferenceCommand` -- the submit itself does not
need the mouse.

## 57. Peace demands: the whole chain, reversed

Section 56 stopped at "the bid structures are created lazily".  They are -- but
the creation is not a special event: every demand is produced by one of five
per-type **builder** functions that the conference window calls when it fills
the `要求` list.  Calling the builder yourself gives you the engine's own
`CPeaceAction` objects, fully initialised, with no field guessing.

### 57.1 Action type ids (verified at runtime)

The engine's own name lookup is `0xEA9F00(&out, typeId, 0, 0)`; it returns the
localised name as a `std::string`.  Calling it live in 1.19.5.0 gave:

    0x30CF 获得地区      take states          <- the one M3 needs
    0x30D0 解放          liberate
    0x30D1 傀儡          puppet
    0x30D2 改变政府      change government
    0x30EE 夺取海军      take navy
    0x30CE              (the bidding turn itself)

The window maps its five `要求` icon widgets to `[window+0x5968] .. [window+0x59A0]`
and dispatches on exactly these ids (handler at `0x18F1D60`).

Note the trap: `CStatePeaceAction`'s own ctor passes **0x30D0**, so that class is
the *liberate* action, not "take states".  The take-states action is a sibling
ctor that writes the same vtable but the id **0x30CF**.

### 57.2 Where a bid lives

    CPeaceConference +0x1E8   std::map<int countryIndex, CPeaceBiddingTurn*>

`CPeaceConference::EndTurn(int* countryIndex, CPeaceBiddingTurn** turn)`
(0xEB5390) does `find-or-insert` on that map, deletes any previous turn for the
key, and takes ownership of `*turn` (it zeroes the caller's pointer).  Its
callers are `CEndTurnPeaceConferenceCommand::Execute` (0x1ED2300) and the
conference's own round handling -- so this is the sanctioned way to install a
turn.

    CPeaceBiddingTurn   0x40 bytes
        +0x00  vtable 0x29EAEC0
        +0x08  int
        +0x10  CPdxArray<CPeaceAction*>   {data, cap, size, alloc}   (0x18)
        +0x28  CPdxArray<...>                                        (0x18)

A conference that has not had a demand made yet has `size == 0` on the map and
no `CPeaceBiddingTurn` anywhere in the heap -- confirmed again on 2026-11-14
with a fresh conference (map head valid, size 0).

### 57.3 The five builders

All five take one argument in `rcx` -- a small scratch struct -- and append to
the `CPdxArray` at **`arg+0x38`**, reading the *losing* country index from
**`arg+0x10`**:

    0x14F26C0   change government   0x30D2
    0x14F2870   liberate            0x30D0
    0x14F3070   puppet              0x30D1
    0x14F3560   take navy           0x30EE
    0x14F3950   take states         0x30CF
    0x14F2680   calls all five in order

`0x14F2680` is what the window's `全选` entry ends up using: it produces every
demand the loser can be made to give, in one array.

Calling `0x14F3950` with `arg+0x10 = 8` (Netherlands) in 1.19.5.0 returned
exactly five actions -- type 0x30CF, state ids **7, 35, 36, 309, 695**, `b = 8`
-- i.e. the five rows the window shows under `荷兰王国 (5)`.  That is the
strongest possible confirmation that the builder is the right entry point.

### 57.4 Object layouts (from the ctors)

    CTakeStatePeaceAction   0x50   0x15B46D0(this, int* a, int* b, CState*)
        +0x08 0x30CF   +0x30 *a   +0x34 *b   +0x38 *a   +0x48 stateId
        (+0x4C written 0; the ctor takes a CState* and reads [state+0x58])

    CPuppetPeaceAction      0x60   0x15B4530(this, int* a, int* b, int* c, int stateId)
        +0x08 0x30D1   +0x30 *a   +0x34 *b   +0x38 *a   +0x48 stateId
        +0x50 *c   +0x58 qword 0

    CTakeNavyPeaceAction           0x15B4600(this, int* a, int* b, void* r9)
        +0x08 0x30EE   +0x48 r9 (qword)   +0x50 5th arg (qword)
        (this ctor builds the `夺取海军` name itself)

`a` is the int behind `0xC07100(&local, 0)`; measured live it is **0**, and the
builders pass the same value for `+0x30` and `+0x38`.  `b` is the country index
of the loser whose assets are being taken.

### 57.5 What does not work

`CPdxArray::push_back` (0x1403371D0) must **not** be called by hand.  Passing a
hand-built turn with `cap == size == 0` and `alloc = base+0x30FD000` made the
game die instantly inside the injected call (the injector came back with
`ReadProcessMemory ... GetLastError=299`).  Three earlier crashes came from the
same habit of writing engine structures by hand.

Instead: let the builder allocate the array, then copy its **0x18-byte array
header** into `turn+0x10`.  The header already carries the engine's own
allocator, so nothing has to be grown and no allocator has to be guessed:

    arg = zeros(0x80)
    arg+0x10 = loser index (int)
    arg+0x38 = {0, 0, 0, base+0x30FD000}
    call 0x14F3950(arg)
    header = read(arg+0x38, 0x18)
    write(turn+0x10, header)
    call 0xEB5390(conference, &winnerIndex, &turnPtr)

### 57.6 The working recipe, end to end

    conf   = the live CPeaceConference
    turn   = new 0x40 bytes, [turn] = base+0x29EAEC0, +0x10 and +0x28 zeroed
    acts   = [a for a in builder(loserIndex) if a.type == 0x30CF]
    write(turn+0x10, {data: acts, cap: n, size: n, alloc: engine allocator})
    0xEB5390(conf, &winnerIndex, &turn)     # conference now owns it
    CDonePeaceConferenceCommand(winnerIndex) -> Post

`calling/peacewin.py` implements exactly this as
`add_demands(g, conf, winner_index, loser_index, kinds)` with
`kinds` in `{"state", "puppet", "navy", "liberate", "gov"}`.

### 57.7 Driving the game without the mouse

While testing this, the review machine ended up with the game drawing windows
that no longer accepted input (the conference panel and a `World News` popup
were both pure ghosts: hovering worked, clicks fell through to the map, and the
clock only advanced after the pause byte was cleared by hand).  Two consequences
worth writing down:

* `idler + 0x6C1 = 0` is enough to unpause a save that loads paused, and it
  also works when an event window is stuck on screen.
* `g.date()` is unreliable in this build -- different calls return different
  `CDate` copies, one of them a 1945 save preview.  The strip at
  `(2255,4)-(2450,40)` is still the only trustworthy clock.
### 57.8 First end-to-end attempt (2026-11-15)

`0xEB5390` cannot be called by hand.  It reads its `find` result out of a stack
slot the *caller* has to set up, so an injected call reports success (it does
zero the caller's pointer) but writes the value into the tree's head sentinel
and leaves `_Mysize` at 0.  Installing the turn therefore links the node by
hand instead -- with exactly one element the red-black shape is trivial:

    head = conf[map+0]                     ; the nil sentinel
    node = new 0x30
    node+0x00/08/10 = head                 ; _Left, _Parent, _Right
    node+0x18 = 0, node+0x19 = 0           ; _Color black, _Isnil false
    node+0x20 = countryIndex               ; _Myval.first
    node+0x28 = turn                       ; _Myval.second
    head+0x00/08/10 = node
    conf[map+8] = 1                        ; _Mysize

After that all three maps (`+0x1C8`, `+0x1D8`, `+0x1E8`) read `size = 1` with
the five `0x30CF` actions for states 7/35/36/309/695 inside, as intended.

Posting `CDonePeaceConferenceCommand` then closed the conference -- but the
treaty was

    阿姆斯特丹条约 -- 荷兰王国与德意志第三帝国达成了无条件和平

i.e. a **white peace**, and no state changed hands (`GER` stayed at 236
provinces, `HOL` kept all five).  That test was **inconclusive rather than
negative**: in that run the Netherlands never actually collapsed (it still had
8 divisions and all of its provinces when the conference opened), so there was
no won warscore to spend and the conference had nothing to give.  The demand
side has to be re-tested against a genuine victory conference -- the earlier
measured run reached one (HOL down to 1 division, 7 provinces occupied) but
that was before the builders were understood.

Open question for that re-test: whether `done` reads the bid from the map at
`+0x1C8`/`+0x1D8`/`+0x1E8` at all, or from the per-country context objects in
the array at `conf+0xF8` that the builders actually receive.  `EB50E0` looks
the player's index up in `+0x1C8` and sets `conf+0xDC` from the lookup result,
which is why `+0x1C8` was the first guess.

## 58. Peace conference -- the bidding really lives in `+0x1F8`, and `Done` does not apply

Measured live on a genuine victory conference (Netherlands capitulated, treaty
window "阿姆斯特丹条约" open, round 1/1, Germany 2800 score).

### 58.1 Conference field map (corrected)

    +0x060  CPdxArray<CCountry*>   cap 64  size 1   -- the losing side
    +0x078  CPdxArray<CCountry*>   cap 64  size 2   -- all participants
    +0x0F8  CPdxArray<...>         cap 1   size 1   -- per-country contexts
    +0x104  i32                                  count for +0xF8
    +0x110  CPdxArray<...>         cap 2   size 2
    +0x0DC  u8    "player has no bid"          (set by EB50E0)
    +0x1B8  std::map   _Mysize at +0x1C0   cleared in EB FD50
    +0x1C8  std::map   _Mysize at +0x1D0   <-- the bids that get APPLIED
    +0x1D8  std::map   _Mysize at +0x1E0   merged into +0x1C8 by EB FD50
    +0x1E8  std::map   _Mysize at +0x1F0   cleared by EB FD50
    +0x1F8  std::map   _Mysize at +0x200   <-- the live bid, per country

### 58.2 Two different map node layouts -- this is what broke the first test

    map<int, CPeaceBiddingTurn*>            node 0x30
        +0x20 key (int)   +0x28 value (pointer)

    map<int, CPdxArray<CPeaceBiddingTurn*>> node 0x40
        +0x20 key (int)
        +0x28 array.data     +0x30 array.cap
        +0x34 array.size     +0x38 array.alloc

Verified from `std::map::operator[]` at `0xEA69B0`: it allocates **0x40** bytes,
writes the key at `+0x20`, zeroes `+0x28/+0x30`, then stores the default
allocator at `+0x38`, and returns **`node+0x28`**.  `0xEB9AE0` reads the bid as
`data = [node+0x28]`, `size = [node+0x34]` -- i.e. the inline array.

The hand-linked 0x30-byte node used in the first attempt is the layout of the
*pointer* maps; writing it into `+0x1F8` makes the engine see an array with
`cap = 0, size = 0`, so the bid is silently empty.

### 58.3 `0xEB5390` is `CPeaceConference::InstallBidding` and it works

    EB5390(rcx = conf, rdx = int* countryIndex, r8 = CPeaceBiddingTurn** turn)

    1. steals *r8 and zeroes the slot
    2. find-or-insert on conf+0x1E8 ...
    3. **inserts into conf+0x1F8** as map<int, CPdxArray<CPeaceBiddingTurn*>>
    4. calls EB50E0(conf, countryIndex)

Step 3 is measured, not read: after the call `+0x1F8` held

    node key=1  arr[data=..., cap=1, size=1, alloc=base+0x30FD000]
      [0] -> the turn we built

and the turn itself held the five `0x30CF` actions for states 7/35/36/309/695
with vtable `0x29EAEC0`.  **The conference window immediately showed the bid**
(right panel "当前要求" -> 德意志第三帝国, 5, 获得地区) -- so this is the path
the UI itself uses, and the earlier note that "EB5390 does nothing" was wrong:
it was inserting into a map we were not looking at.

The earlier statement that it "reads its find result from the caller's stack"
was a misreading; the three-argument call works and returns with the slot
zeroed.

### 58.4 `Done` only logs -- it never applies anything

    CDonePeaceConferenceCommand::Execute  0x1ED22E0
        conf = 0x1ED1EE0()
        return 0xEB9AE0(conf, cmd+0x28)

`0xEB9AE0` = sum the cost of the player's turns in `+0x1F8`
(`0x15BB1B0(action, conf)` per action via `0x1A86C60`), emit a log line, then
tail-call `0xEB50E0`.

`0xEB50E0(conf, idx)`:

    find(conf+0x1C8, idx)
    conf+0xDC = (not found)
    if (0xEA9B10(conf)) return 0xEB0CE0(conf, 0xEB4260(conf+0x60))
    for each participant p in conf+0xF8:
        if find(conf+0x1C8, *(int*)(0x140BCF3F0(p)+8)) != end: return
    return 0xEBFD50(conf, 0)          ; nobody bid -> finalise

So a bid that is only in `+0x1F8` is *displayed* but never *applied*: the apply
path looks at `+0x1C8` (and `+0x1D8`, which `0xEBFD50` merges into `+0x1C8`
before distributing scores into `participant+0x4C`).

That is exactly what the live test produced: bid visible in the window, then
`done` -> conference gone, `GER` 236 -> 236 provinces, `HOL` kept states
7/35/36/309/695.  A second white peace, this time with a correct-looking bid.

### 58.5 `EndTurn` is the command that installs a bid

    CEndTurnPeaceConferenceCommand::Execute  0x1ED2300
        conf = 0x1ED1EE0()
        0x1A86C60(*(cmd+0x30), conf)          ; turn->Attach(conf) per action
        return 0xEB5390(conf, cmd+0x28, cmd+0x30)

`cmd+0x28` = country index, `cmd+0x30` = `CPeaceBiddingTurn*`.  The old
`end_turn()` helper wrote the vtable and the index but left `+0x30` null, which
is why it was never usable.  It now takes the turn pointer.

**Next test**: open a conference, build the turn with the five `0x30CF`
actions, post `EndTurn` with that turn, and watch `+0x1C8` -- if the engine
copies it there, the next `done` should apply the demands.

### 58.6 `CSetGameSpeedCommand` -- the old RVA was garbage

`cmdwin.VT_SET_GAME_SPEED` was `0x4474C40`, which is past the end of the image
(size `0x38A3000`); posting it killed the game.  RTTI (`rtti_win.pkl`) gives

    CSetGameSpeedCommand       vtable 0x29F3A80
    CIncreaseGameSpeedCommand  vtable 0x29F3900

`set_game_speed(g, 5)` with the corrected vtable posts cleanly.  Note the UI
`+`/`-` circles beside the date are **not** the speed control -- clicking them
opened the exit dialog.

## 59. Live conference, second run: the bid is rendered but `Done` is the white-peace exit

Same save, same plan: 30 German divisions, war declared 1936-03-02, the
Netherlands capitulated on **1936-03-14 15:00** (12.6 game days) and
"阿姆斯特丹条约" opened -- the army line is reproducible.

Sequence measured with the conference open (`conf` = 0x2da5b4a9cc0):

1. Baseline: `+0x1C8/+0x1D8/+0x1E8/+0x1F8` all size 0, `+0xDC = 1`.
2. `0x14F3950(loser=8)` -> five `0x30CF` actions, states 7/35/36/309/695.
3. Build a `CPeaceBiddingTurn` (0x40, vtable 0x29EAEC0, array at +0x10, 5
   action pointers) and hand it to the conference.
4. `CEndTurnPeaceConferenceCommand::Execute` called **directly**
   (`g.call(base+0x1ED2300, (cmd,))`, cmd+0x28 = 1, cmd+0x30 = turn):
   result 2560, and `+0x1F8` went 0 -> 1 while `+0x1C8` stayed 0.
   The conference counter went **和谈回合 1 -> 2**, so EndTurn is genuinely the
   "submit my demands" step.
   Note: `cmdwin.post()` also returned 1 and the command's CanExecute
   (0x1ED2590) returned 1, but nothing happened -- the queue does not drain
   while the conference is open.  Call Execute directly.
5. `CDonePeaceConferenceCommand::Execute` (0x1ED22E0) -> conference closed,
   `GER` 236 -> 236 provinces, `HOL` kept 7/35/36/309/695.  **Third white
   peace.**  With `+0x1C8` empty, `0xEB50E0` sets `+0xDC = 1` and the finalise
   path has nothing to give.

So the remaining gap is exactly one field: what the *apply* path reads is
`conf+0x1C8` (merged from `+0x1D8` by `0xEBFD50`), and neither `EB5390` nor
`EndTurn` nor `Done` ever writes it.  `0xEAFDB0(conf, &country)` is the
*withdraw* counterpart -- it erases the country from `+0x1E8`, `+0x1D8` and
`+0x1C8` in one go, which confirms all three are per-country bidding maps.

**Next probe (needs one more conference)**: let the *window* add a single
demand -- click the state category and a Dutch province -- then dump `+0x1C8`
and `+0x1D8`.  That reveals the node layout of the two maps the engine actually
applies, which is the only unknown left.  The window is the ground truth: it
already proved that a programmatic bid in `+0x1F8` is displayed.

## 60. Cross-tag control works; "dual drive" has to be single-owner

Probe: build `CDeclareWarAction` with `from = HOL (8)`, `to = BEL (7)` and post
it while the player is Germany.  The engine accepted it and the game raised

    来自荷兰王国的消息 -- 荷兰王国对比利时王国宣战

so the sender in a command object is authoritative: the engine does **not**
require the command to come from the player tag.  That makes "one agent per
country" possible in principle -- every command already carries its own
`CCountryTag` (`cmd+0x28` for the focus/research/war family).

The hard constraint is elsewhere: the game has one tick thread and no locking
around the objects we mutate, and our writes are injected calls that run on a
foreign thread.  Two agents injecting at the same time race inside the engine --
that is exactly what killed the process twice on 2026-11-14/15.  So parallelism
belongs outside the game:

    agent A --\
    agent B ---> gamehost.py (single owner, serialised) --> hoi4.exe
    agent C --/

`calling/gamehost.py` is that owner: a line-JSON socket server on
127.0.0.1:8765 with a whitelist of ops (`state`, `speed`, `pause`,
`declare_war`, `peace_demands`, `peace_done`), one request at a time under a
lock.  `calling/gh.py` is the three-line client.  Verified end to end:
`state` returned both countries, `speed` and `pause` round-tripped.

Three limits that will not go away:

1. A country that is not the player is run by the AI.  Orders you inject are
   re-planned on the next tick, so cross-tag control is "keep pushing", not
   "set once" -- the same pattern as `COrderInstance+0x248`.
2. There is one window and one cursor.  Two agents clicking is a race the game
   cannot arbitrate; all UI work must go through the same owner as the memory
   work.
3. A peace conference or an event window holds the whole game.  Nothing else
   can progress until it closes.

## 61. The player's selection lives in the window, not in the conference

Third live conference (`conf = 0x1d8b122f010`), same save, same 12-day war.

Clicking a Dutch state on the map, with 获得地区 active:

    tooltip  弗里斯兰 (◇27) / 德意志第三帝国占领 -> 荷兰王国的弗里斯兰
    header  分数: 2800 -> 2773 -> 2749

so each click really buys a state and is priced immediately.  **But all four
conference maps stayed at `size = 0` through every click** -- `+0x1C8`,
`+0x1D8`, `+0x1E8` and `+0x1F8` never moved.

The right panel shows why: the selection appears under

    未提交的要求 (unsubmitted demands)  1
        获得地区                       1
            荷兰王国                   1

i.e. it is held by `CPeaceConferenceWindow` -- the two arrays of
`CPeaceConferenceWindow::SetBiddingsInWinnerItem(const CPdxArray<CPeaceAction*>&,
CPdxArray<CPeaceBiddingTurn*>&, CPeaceWinnerBiddingsItem&)` -- and is only
copied into the conference when the player submits.  The bottom-right button
changes from 跳过本轮 to **提交要求 ◈51** as soon as something is selected; that
button is the submit.

The action objects the *window* creates are the same class as ours:

    UI      action vt 0x2A4C7B0   type 0x30CF   state 7 / 695
    builder action vt 0x2A4C7B0   type 0x30CF   state 7 / 35 / 36

so `0x14F3950` produces exactly what a click produces.  What is missing is the
window's half of the handshake, not the action.

Window object located by its two RTTI vtables `0x2A87468` / `0x2A87490`
(multiple inheritance, second vtable at `+0x28`) at `0x1d88cc25a80`.  Its first
0x300 bytes show no `CPdxArray` header, so the arrays belong to a child item
(`CPeaceWinnerBiddingsItem`) -- that is the next thing to walk.

Next probe, in order:

1. click one state, then find the `CPeaceWinnerBiddingsItem` under the window
   and read its `CPdxArray<CPeaceAction*>` -- that array is the selection.
2. call the button's handler (or reproduce whatever copies those arrays into
   `conf+0x1C8`) and confirm the maps move.

## 62. 提交要求落在 `+0x1F8`；`+0x1E8` 是 EndTurn 的槽；应用在 `0xEBFD50`

这一节是拿**活着的会议**做实验换来的（德国打赢荷兰，会议开着，手点一次"提交要求"），
不是读文档推的。

### 62.1 手点一次按钮，只有 `+0x1F8` 变

点之前四张表全空；点完 `+0x1B8/+0x1C8/+0x1D8/+0x1E8` 仍是 0，`+0x1F8` 变成 size=1：

```
node 0x1d8ba0dbe40              # 0x40 字节节点
  +00/08/10 = 头节点(空树形状)
  +18       = 01 00            # _Color=1(黑) _Isnil=0
  +20       = 00000001         # key = 国家序号 1 (GER)
  +28       = 000001d8017eb970 # CPdxArray<CPeaceBiddingTurn*> 内联:
  +30       = 0000000100000001 #   cap=1 size=1
  +38       = 00007ff664f3d000 #   allocator (base+0x30FD000)
turn 0x1d8ba0dc570 vt=base+0x29EAEC0
  +10 arr data=0x1d8017eb910 cap=1 size=1
   [0] type=0x30CF +30=1(winner) +34=8(HOL) +38=1 +48=35(弗里斯兰)
```

也就是说 **`+0x1F8` 是 `map<int, CPdxArray<CPeaceBiddingTurn*>>`，节点 0x40 字节、
数组内联在 +0x28**；turn 里的 action 与 `0x14F3950` 自己造的一模一样。
UI 造的对象和我们造的对象没有区别，缺的从来只是"往哪张表里放"。

### 62.2 `0xEB5390` 写的是 `+0x1E8`（反汇编为证）

```
0xEB5390(rcx=conf, rdx=&idx, r8=&turn)
  0xEB53AD  mov r12,[r8] ; mov qword[r8],0        ; 偷走 turn
  0xEB53BB  lea r14,[rcx+0x1E8]
  0xEB53CC  call 0x7684F0                         ; map::find(conf+0x1E8, key)
  0xEB5435  mov ecx,0x30 ; call 0x25A01DC         ; operator new(0x30)
  0xEB5442  mov [rax+0x20], edx                   ; key
  0xEB5445  mov qword[rax+0x28],0                 ; value = turn*
  0xEB547B  rcx=[rbx+0x28]; if(rcx) rcx->vt[0](rcx,1)   ; 释放旧 turn
  0xEB5492  mov [rbx+0x28], r12                   ; 存新 turn
  0xEB54B4  jmp 0xEB50E0
```
0x30 字节节点、turn 指针在 +0x28——和 `+0x1F8` 的 0x40 内联数组**不是同一个形状**。
之前把 0xEB5390 的结果拿去读 `+0x1F8`，自然什么都看不到。

### 62.3 `Done` 什么时候才真的应用

`CDonePeaceConferenceCommand::Execute 0x1ED22E0` -> `0xEB9AE0(conf, cmd+0x28)`：

```
0xEB9AE0  0xD95CB0(conf+0x1D8, &it, idx)   ; find
          0xD95CB0(conf+0x1C8, &it, idx)   ; find
          jmp 0xEB50E0(conf, idx)
```

`0xEB50E0`（0xEB50E0..0xEB5369）：

```
conf+0xDC = (find(conf+0x1C8, idx) == end)      ; "我没有提交"
if (0xEA9B10(conf)) { 0xEB4260(conf+0x60); return 0xEB0CE0(conf, al); }
for (每个 participant in [conf+0xF8, +0x104 个]) {
    0x692180(conf+0x1C8, &it, participant+8)    ; find by country index
    if (it == end) goto 0xEB536A -> 只打日志, 返回
}
0xEBFD50(conf, 0)                               ; <-- 真正的收尾/应用
```

所以 **`Done` 只在"每个参与者都在 `+0x1C8` 里有条目"时才走 `0xEBFD50`**；
否则就是那条只算分数、打日志、白和平的路径（三次白和平都出在这里）。

`0xEBFD50`（0xEBFD50..0xEC01EB，1179 字节）在 0xEBFF1D 调 `0xEA52E0`（合并
`+0x1D8` -> `+0x1C8`），随后 0xEC015E 调 `0xEA9B10`、0xEC0176 调 `0xEB0CE0`。
`0xEB0CE0` 被 `conf+0xDD`（已开始）挡着，里面遍历 `conf+0x140`，是收尾执行段。

### 62.4 顺带做出来的三件工具

| 文件 | 作用 |
|---|---|
| `stage/wfuns.py` | 读 `.pdata`，任意 RVA -> 函数起止（`0xEB50E0..0xEB5369` 这种边界就是它给的） |
| `stage/wdis.py` | capstone 反汇编指定 RVA，自动标注 call/jmp 目标 RVA |
| `stage/wcalls.py` | 全 `.text` 扫 `E8 rel32`，列出所有调用某 RVA 的站点 |
| `stage/wlea.py` | 扫 `8D/8B modrm disp32`，找"谁碰了 +0x1F8 / +0x1C8 这个字段" |
| `stage/wvt.py` | 按 RVA 打印虚表项 |

### 62.5 `conference()` 的堆扫描太慢，用引擎自己的 getter

`peacewin.conference()` 扫全部可写内存：**29 秒一次**，没法当轮询用。
`0x1ED1EE0` 是引擎自己的取会议指针函数（`EndTurn::Execute` 第一句就是它），
直接 `call` 它 **9 毫秒**返回 0 或会议地址。轮询循环一律用它（`stage/wfast.py`）。

### 62.6 `0x14F3070`（傀儡 builder）会先清 `arg+0x68`

```
0x14F308D  lea r12,[rcx+0x68]
0x14F30A2  mov rcx,r12 ; call 0x1AE000     ; 对 arg+0x68 那个 CPdxArray 做 clear
```
只把 `arg+0x38` 初始化成空数组、`+0x68` 留零，会在这里把游戏打死
（2026-11-15，`ReadProcessMemory ... GetLastError=299`）。两个数组都要写
`{0,0,0,default_alloc}`。傀儡 ctor 本身是
`0x15B4530(this, int* a, int* b, int* c, int state)`（第 5 个参数走栈，
`winject.call` 已经支持），`+0x48=state`、`+0x50=*c`、vtable `0x2A8C9F0`。

## 63. 割地成功：`+0x1F8` 就是全部，差的是 action 的 winner 字段

2026-11-15 这一轮完整跑通，**德国 11 天拿下荷兰，五个州全部易主，全程没用控制台**。

### 63.1 一条命令做完"全选 + 提交"

```python
turn, acts = peacewin.submit_demands(g, conf, winner=1, loser=8, kinds=("state",))
```

内部就是：`0x14F3950` 造出 5 个 `0x30CF`（states 7/35/36/309/695）→ `new_turn()`
→ `install_turns(g, conf, 1, [turn], 0x1F8)`（0x40 节点 + 内联数组，见 §62.1）。

界面上立刻变成"已提交"：右下角按钮从 `提交要求` 变成 `跳过本轮`，
右侧 `当前要求 → 德意志第三帝国 → 获得地区 1`，左侧计数 `5`。

### 63.2 关键坑：builder 把 winner 写成 0

```
0x14F3A2F  lea rcx,[rsp+0x90]
0x14F3A37  call 0xC07100          ; 返回一个 int*
0x14F3A3C  mov r9, rdi            ; state*
0x14F3A3F  mov r8, r13            ; arg+0x10 = 战败国
0x14F3A42  mov rdx, rax           ; a*  <- 这个
0x14F3A48  call 0x15B46D0         ; CTakeStatePeaceAction(this, a*, b*, state*)
```
`a` 指向的那个 int 在窗口里是**玩家国家序号**（实测 action `+0x30=1 +0x34=8 +0x38=1`），
但从注入线程里 `0xC07100(&local,0)` 给的是 **0**，于是我们造出来的 action 是
`+0x30=0 +0x38=0`。这种要求界面照样列出来、回合照样推进，但**一分点数都不扣**
（实测 `◈0`、分数一直 2800），等于废票。

修法：造完把每个 action 的 `+0x30`/`+0x38` 都写成 winner 序号
（`build_actions(..., winner_idx=1)`）。改完界面计数从 `0` 变 `5`。

### 63.3 收尾

`跳过本轮` 每点一次 `和谈回合 +1`，`+0x1F8` 里的 turn 一直保留。
最后点右上角 `确认并退出` → 弹 `退出和谈 / 你确定要退出和谈吗?` → `OK`，
条约当场生效，弹 `阿姆斯特丹条约` 新闻。

实测结果：

```
GER 24 州 236 省 -> 29 州 261 省     (正好 +5 州 +25 省)
HOL  5 州  25 省 ->  0
date 1936.03.13        (1936-03-02 开战, 11 天)
```

### 63.4 参与者结构（`conf+0xF8`，size 在 `+0x104`）

荷兰被完全占领后 size 从 2 掉到 1，只剩德国：

```
participant 0x18bad0e4eb0
  +00 vtable 0x2A8CC48
  +08 国家序号 = 1
  +10 conf
  +18 CPdxArray  data, cap=6, size=5     <- 5 条要求
  +40 内嵌子对象 vtable 0x2A8CC60
  +48 2798      +4C 2800                 <- 分数（当前/上限）
  +58 第二个数组 cap=6 size=5
  +70/+78 100000
```

### 63.5 还差的最后两下

`确认并退出` 和它的 `OK` 对话框还是手点的。命令侧已经有全部材料：
`CDonePeaceConferenceCommand::Execute 0x1ED22E0` = `getter(); 0xEB9AE0(conf, cmd+0x28)`。
下一轮会议直接调它试，若等价就把这两个点击也去掉。
（`CPassPeaceConferenceCommand::Execute = 0x1ED2350`，ctor `0x1ED2290`，size 0x30；
`CEndTurnPeaceConferenceCommand::Execute = 0x1ED2300`，ctor `0x1ED2230`，size 0x38。）

## 64. 傀儡/夺取海军：builder 会打死进程，只能走构造器

`0x14F3950`（获得地区）可以放心调；**`0x14F3070`（傀儡）不行**。
2026-11-15 两次实测，`arg+0x68` 也给成合法空数组之后仍然当场崩
（`ReadProcessMemory ... GetLastError=299`，进程消失）。`0x14F3560`（海军）同样没验成。

它前几行是：

```
0x14F308D  lea r12,[rcx+0x68]
0x14F3091  lea rsi,[rcx+0x10]
0x14F309C  call 0xEB67D0          ; (out=&rbp-0x30, &arg+0x10) 收集该国的州
0x14F30A5  call 0x1AE000          ; 对 arg+0x68 做事
0x14F30AD  call 0xC07BF0          ; 国家对象
0x14F30B2  mov rcx,[rax+0xF88] ; call 0xDA4CD0   ; 校验
```
`0xEB67D0` 很可能要求真正的会议/窗口上下文，注入线程里给不出，于是拿到的
`[rbp-0x30]` 不是数组头，后面 `mov rbx,[rdi]` 直接读野指针。

**构造器链（实际参数已从反汇编确认）：**

```
基类 0x15B43F0(this, edx=type, r8=a*, r9=b*, 栈上=c*)
    [this+0x00] vtable
    [this+0x08] = type
    [this+0x10] std::string 名字（SSO，容量 0xF）
                 名字来自 0x253ECD0(type)，即该要求类型自己的本地化名
    [this+0x30] = *a      [this+0x34] = *b      [this+0x38] = *a
    [this+0x3c] = 0       [this+0x40] = 0

CPuppetPeaceAction   0x15B4530(this, a*, b*, c*, int state)
    基类(type=0x30D1) -> [this+0x48]=state, [this+0x50]=*c, vtable 0x2A8C9F0

CTakeNavyPeaceAction 0x15B4600(this, a*, b*, void* r9, void* arg5)
    基类(type=0x30EE) -> [this+0x48]=r9（是**指针**，不解引用）, [this+0x50]=arg5
```

`a` = 得主（必须在注入时自己写，builder 那个 0xC07100 返回 0，见 §63.2），
`b` = 战败国。`c`/`r9`/`arg5` 的含义还没定；`winject.call` 支持 >4 个参数，
所以下一步直接用构造器造，别再用那两个 builder。

## 65. 全自动割地跑通：零鼠标，`done` 一条命令收尾

2026-11-15 03:17，同一个进程里从"会议刚开"到"五个州易主"全部由代码完成，
**没有点任何一次鼠标**。日志（`stage/wauto2.py`）：

```
CONFERENCE 0x2471b71ca70 at t+625s
status  maps +1b8/+1c8/+1d8/+1e8/+1f8 = 0 0 0 0 0
submitted 5 actions on turn 0x24738140b70
detail  [(0x30cf, winner=1, loser=8, state=7), (…35), (…36), (…309), (…695)]
pass  -> maps 0 0 0 0 1
done  -> maps 0 1 1 0 0      started=True  no_bid=False
 2s    conf=none   {'GER': [7,35,36,309,695], 'HOL': []}
```

也就是：

| 步骤 | 调用 | 结果 |
|---|---|---|
| 提交要求 | `peacewin.submit_demands(g, conf, 1, 8, ("state",))` | `conf+0x1F8` size 1，5 条 `0x30CF` |
| 跳过本轮 | `CPassPeaceConferenceCommand::Execute 0x1ED2350` | 回合推进，`+0x1F8` 保留 |
| 确认退出 | `CDonePeaceConferenceCommand::Execute 0x1ED22E0` | `+0x1C8`/`+0x1D8` 各出现 1 条 → 会议关闭 → 州易主 |

`done` 执行后 `+0x1C8` 与 `+0x1D8` 同时出现条目，说明合并确实发生在
`0xEB9AE0 -> 0xEB50E0 -> 0xEBFD50` 这条链里（§62.3），不是 UI 特供。

**M3 至此闭环**：宣战 → 画前线 → 进攻 → 会议割地，全部是代码；控制台一次没用。
'MCP 侧对应 `hoi4_peace_status` / `hoi4_peace_demands` / `hoi4_peace_finish`（`mcp/server.py`）。

### 65.1 一个反面教训：不要周期性重发进攻计划

`wauto2.py` 第一版每 90 秒重发一次计划（`wplan.offensive` + `execute_plan`），
第四次就把进程打死了。`wchain.drive` 的注释早就写着这句话，这次又验证了一遍：
**每重发一次就多一个 type-1 子订单，旧的在脚下被释放**。
正确做法是 `wchain.py --setup` 设一次 flag（`COrderInstance+0x248 = 1`），
然后只轮询，让引擎自己跑；荷兰 1936-03-02 开战、03-13 投降。

## 66. 傀儡/夺取海军：构造器能造出合法对象（端到端还差一个前提）

按 §64 的构造器链直接造，**不再碰那两个 builder**。实测（比利时，游戏里活着）：

```
puppet  vt=base+0x2A8C9F0  type=0x30D1  a=1(得主) b=7(战败) a2=1 state=6 extra=7
navy    vt=base+0x2A8CC30  type=0x30EE  a=1        b=7         a2=1 +0x48=0 +0x50=100000
```

字段、虚表、类型 id 全对，对象稳定没有被引擎拒掉。`peacewin.make_action()` /
`make_puppet_demands()` / `make_navy_demand()` 就是这三个构造器的封装。

**没做完的一步**：要在真会议里验证"比利时变成傀儡、舰队被夺"，
得先让比利时投降；而 `wplan.py` 整条链路写死了荷兰：

- `border_provinces()` 第一行 `H = set(hol[7] + hol[35] + hol[36])` → `KeyError: 7`
- `--setup` 里 `f['handle'] == HOL_FRONT` → 荷兰打完就没有这个 handle 了

也就是 **"打第二个国家"这件事本身还没通用化**，跟傀儡/海军无关。
下一步把 `wplan` 的敌国从常量换成"当前交战国 + 按省重叠挑前线"（原型脚本 `wbel2.py`，未随仓库发布
里已经有按重叠挑 front 的写法），傀儡/海军就能在同一次会议里验掉。

## 67. M1/M2 读数缺陷的根因（2026-11-15 夜）

五条已知缺陷逐条定位，全部是"Linux 偏移/路径直接搬到 PE"造成的：

**67.1 日期（M1.1 回归）**：`api._log_paths()` 在没有 `-userdir=` 时回落到
`~/.local/share/Paradox Interactive/...`（XDG 路径），Windows 上根本不存在，
于是 `_log_date()` 恒为 `None`——所有"这个 CDate 像不像真的"的校验全部失明。
`_win_date_addr()` 在**暂停**时按"值最大"挑候选，正好挑中一个冻结的 1945 副本
（同一局里新起的进程读 `1936.03.02`，另一个进程读 `1945.04.02`）。
修法三步：`config.user_dir()`（Windows → `Documents/Paradox Interactive/Hearts of Iron IV`）；
`_log_paths()` 用它；`_win_date_addr()` 暂停时以日志戳为下界、在窗口内取**最大值**
（陈旧副本永远落后，取最大才是真时钟）。修后两个独立进程读 `1936.03.10.13` / `.22`。

**67.2 建造队列**：`PS_CONSTRUCTION = 0x70` 和 `BL_BUILDING = 0x78` 本来就是对的
（`CProductionStatus+0x70` 是 `CPdxArray<CBuildingProductionLine*>`），错的是进度：
`BL_PROGRESS` 原来写 `0x28`，那是**总造价**（民用工厂 10800 IC-日）。1.19.5 的
**累计进度在 `+0x38`**，靠对一条在建产线做 25 秒差分确认（`1466.56 → 1486.36`，
只有这个字段在动）。现在返回 `progress`（0..1）、`progress_ic`、`cost_ic`。

**67.3 战争目标类型**：`diplomacy.wargoal_types()` 把 `CWarGoalType+0x10` 当 `char*`
解引用，全成乱码→空表→`justify_wargoal` 报 "unknown wargoal (options: )"。
1.19.5 里名字是 **MSVC `std::string` 在 `+0x18`**（size `+0x28`、cap `+0x30`）。
修后 10 个类型全部读出：`annex_everything civil_war independence_wargoal
liberate_wargoal puppet_wargoal_focus take_claimed_state take_core_state
take_state take_state_focus topple_government`。

**67.4 `front_changes()` 恒 `provinces_seen: 0`**：`mapdata.province_objects()` 用固定
Linux vtable 地址扫 CProvince，PE 上扫不到；即使扫到，1.19.5 的 CProvince 也没有
`+0xE0 CState*`/`+0xF0 CControllerArea`——它是 **`+0xD0 CControllerArea*` +
`+0xD8 COwnerArea*`**。新增 `_win_controller_map()`：读 `CState+0xCC`（控制者）展开到
该州各省，得到 10272 个省的控制器地图。`front_changes()` 从此能看到推进。

**67.5 研究槽**：`TS_SLOTS = 0xA0`、`RS_CURRENT = 0x18`、`RS_SPEED = 0x38` 在 1.19.5
上是对的（`CResearchSlot` 4 个，`+0x18` 指向 `CTechnology`，55 项已研究可读）。
`start_research()` 命令能下发并回读成功。**仍未解决**：槽里的 `progress/required`
（`+0x20/+0x28`）恒为 0，且 `tech_cost_days()`（`TT_RESEARCH_COST = 0x3D0`）在 PE 上
返回 -1/0——研究进度百分比暂时不可用，`base_days` 会是 0。

**67.6 顺带**：`peacewin.describe()` 读名字时不再因某个 action 类的 string 布局不同
把调用方打死；`wnavy.py` 给出海军读数——`CCountry+0x278` 是 `CPdxArray<CFleet*>`，
`CFleet+0xB8`（不是 Linux 的 `+0xB0`）是 `CPdxArray<CTaskForce*>`，`CTaskForce+0xA0`
是舰船数。实测 GER 3 舰队/6 特遣/40 舰，HOL 3/4/34。

这五条在开发期做成了可重跑的 PASS/FAIL 自检，当前全部通过。

## 68. 傀儡：头一次是真的生效了（§66 的结论要更正）

§66 说"端到端还差一个前提"，是因为验证方法错了。**傀儡生效后战败国保留自己的
全部州**，所以拿"德国州数变多"当判据必然看起来"什么都没发生"。正确的判据是
游戏自己吐出来的两条：

- 会议散会后弹出世界新闻：**《阿姆斯特丹条约》／"荷兰专员辖区成为傀儡。"**
- 国家 8（HOL）的名字从"荷兰"变成 **"荷兰专员辖区"**（TNO 的 Reichskommissariat），
  它的 5 个州（7/35/36/309/695，25 个省）一个不少地留在自己名下。

提交方式与 §63 完全相同：`install_turns(conf, 1, [turn], 0x1F8)` → `pass` → `done`。
五个 `CPuppetPeaceAction`（type `0x30D1`，`+0x48`=州 id，`+0x50`=战败国索引）
一次装进同一个 turn，引擎照单全收，进程没有崩。

**代价**：这一局的 `score_now` 只从 2800 掉到 2798，也就是傀儡在计数器上几乎不花点数；
真正的限制不是预算（原来的猜测不成立）。

**同时被证伪的还有"wplan 只能打荷兰"**：`border_provinces(g, enemy)` 现在按邻接算，
`--setup --enemy DEN` 能挑到丹麦的前线（handle `[66,3]`，2 个省）。

**坑**：`wplan.offensive()` 默认把进攻挂到"省数最多的根订单"上，打完荷兰后旧订单还在，
于是丹麦的进攻被挂到了旧的荷兰前线（返回 id 13/14 而不是新的 23）。正确做法是
先 `create_army()` 把师全部搬到一个新 OG，再按"新增的 type-2 订单"取 root_id
（`stage/wden.py` 就是这么写的）。


## 69. 夺取海军：端到端成立（`0x30EE` 只吃一个舰队）

§66 说"海军对象能造出来但端到端还差一个前提"，§68 之后只剩它没有真跑过。2026-11-15 补上，
并且把上次"`done` 打死进程"的事故分离掉了：**只提海军、不提傀儡**，进程活着，条约生效。

**开会**。`debug_force_capitulate DEN`（控制台，仅测试用）让丹麦在下一个日跳投降，
会议对象出现；`conf+0x34 = 25`（DEN 的国家索引），而 `conf+0xF8` 的参与国列表是
`[1, 8]` —— 德国和**荷兰专员辖区**。这不是错：傀儡自动跟随宗主国参战，
`game.log` 里就有 `荷兰专员辖区 is declaring war on 丹麦王国`，所以它也是战胜方成员。

**提交**。只造一个 `CPuppetPeaceAction`（`type 0x30EE` = 12526）：

    a=1  b=25  a2=1  state(+0x48)=0  extra(+0x50)=100000

`install_turns(conf, 1, [turn], 0x1F8)` → `pass`（`+0x1F8` 2）→ `done`
（`+0x1C8=2 +0x1D8=2`，`started=True`）。**没有崩**。

**结果**（同一进程读数，前后对比）：

| | GER | DEN |
|---|---|---|
| 舰船 | 42 → **48** | 7 → **1** |
| 舰队 | 3 → **4** | 2 → **1** |
| 特遣队 | 6 → **7** | 2 → **1** |

德国多出来的 `fleet[3]` 正好是 1 个特遣队 / 6 艘船 —— 和丹麦少掉的那一份形状一致。
丹麦保留 7 个州、1 艘船（另一个舰队的 1 艘）。

**所以 `extra=100000`（100%）是"一个舰队"的比例，不是"整支海军"**：builder 里 `r9 = 0`
表示默认舰队，拿到 6/7。要全拿，就得每支敌方舰队一条 action（`make_action(..., fleet=…)`
现在把这个值透出来了；舰队指针/索引的语义还没在引擎里定死，先只记录观测）。

**收尾**：会议关闭后 `combat.war_relations(refresh=True)` 为空 —— 对丹麦的战争真的结束了，
不是"割完地还在打"。会议对象会一直挂到游戏跑起来的第一帧才关，所以脚本里 `done` 之后
必须解暂停再等（`stage/wafter.py`）。

顺带把"上次为什么崩"缩小了一半：那次是 **5 个傀儡 + 1 个海军装在同一个 turn** 里，
`done` 时进程消失（`ReadProcessMemory ... GetLastError=299`）。这次 1 个海军单独走完全程，
说明海军 action 本身没问题；要重现就只剩"同一 turn 里混装多种 action"这一条线索。

## 70. M4 整合：工具层的 Windows 路径（陆军是最后一块）

原计划的 M4 是"把 focus/research/build/army 暴露成 MCP 工具"。前三个家族在 `game.py`
里早有 `sys.platform == "win32"` 分支（研究、建造、宣战、速度都走 `cmdwin`），**陆军没有**：
`create_army` / `draw_front_line` / `orders_of` / `execute_plan` / `border_fronts` 全部
`import armyops` —— 那是 Linux 实现，在 PE 上要么报错要么直接打死进程。

**70.1 现在走哪条路**。`calling/armyops_win.py` 新增了工具层需要的全部件，并且把
`stage/wplan.py` 里验证过的两段（进攻线、执行）搬了进来，因为工具层不能 import 舞台脚本：

| 函数 | 作用 |
|---|---|
| `owned_provinces(g, tag)` | `GS_STATES` + `CS_OWNER` + `ST_PROVINCES`，州→省（无外部脚本依赖） |
| `fronts_towards(g, enemy, own)` | 用 `A.fronts()` 的 CFront 列表 + 静态邻接图，挑真正贴着敌人的段 |
| `target_provinces(g, enemy, depth=2)` | BFS，进攻线的目标（跳过第一排） |
| `divisions_of(g, og)` | `og+0x50` 的 30 个师 |
| `offensive_line(g, og, parent_id, provs, divs)` | `COrderNewFrontCommand` `0x18C9840` |
| `execute_plan(g, og, order_id)` | `COrderExecuteCommand` `0x18C8C50`（绿勾） |

`game.py` 里对应的方法全部按 `win32` 分派；`hoi4_army_battleplans` 现在落到
`Game._win_battleplans()`：建军团 → 画前线 → 在同一根订单下画进攻线 → 执行 → 置 `+0x248`。
每一步都轮询回读，不把"Post 成功"当成"订单存在"。

**70.2 两个真 bug**

* `order_children()` 把 `COrderInstance+0x248` 当 **i32** 读。它其实是一个 **u8**，
  后面三个字节是别的字段，于是读出来是 `-1716106239` 这种垃圾值，`start_offensives()`
  每次都以为"还没执行"，一遍遍重设。改成 `p.u8` 后 `executing` 正常显示 0/1。
* **暂停时建的计划是死的**：在暂停的游戏里跑同一条链，`COrderExecuteCommand` 造出的
  type-1 子订单 `assigned` 永远是 0（30 个师一个都没进去）；解暂停后再建就正常
  （`assigned=30`）。所以 `_win_battleplans` 会临时解暂停、建完再恢复原来的暂停状态，
  返回值里用 `resumed_for_plan` 标出来。

**70.3 自检**。开发期的只读自检脚本直接调用 MCP server 的 `HANDLERS[name](args)`
（和传输层同一个入口），29 个只读工具 **全绿**：状态/日期/总览/国家、国策四件、
研究四件、生产/建造/建筑类型/国家统计、师/军团/战区/边境前线/订单/陆军状态、
舰队/特遣队/空军、州/州详情/前线变化。`--write` 再加一条"占一个空科研槽"。

**70.4 还没证的**：进攻线画出来之后**没有推进**。比利时那次：`hoi4_declare_war` 成功
（`at_war_verified: true`），`hoi4_army_battleplans` 返回 `ok: true`，订单树齐全
（根 31 / 进攻线 32 带 14 个目标省 / 执行子订单 34 `assigned=30, executing=1`），
但 6 个游戏日里比利时 0 个省易手，30 个师里只有 4 个真正站到前线省，其余还在行军。
§53 的荷兰战例是"师已经在边境线上蹲了几天"之后才画箭头的，所以最可能的解释是
**师没到位就不会进攻**，但这只是解释，不是证据 —— 需要在师到位后重测，或者直接比对
引擎自己的 AI 进攻订单（§49 的做法）。

**70.4.1 第二个数据点（波兰，同一晚）**：`hoi4_declare_war POL` 之后，同一份计划
（根 35 / 进攻线 36 / 执行子订单 38 `assigned=32, executing=1`）跑了 **5 个游戏日**：
4 个师终于站到波兰边境省，**0 个省易手**，而且师的位置每个采样点都在变 —— 引擎一直在
沿前线重排（典型的"前线上兵太多，来回摊"），但没有一次跨过边界。

两个候选原因，都还没排除：

1. **军团没有指挥官**（`hoi4_army_status` 里 `leader_ptr: null`）。§53 的荷兰战例没记
   这一条，所以不排除当时是有将的；
2. 游戏速度被压在 1-2（`cmdwin.set_game_speed(g, 5)` 回读仍是 1），行军以天计，
   观察窗口（5 天）短于"走到位 + 组织度回满"所需。

下一步应该先 `hoi4_assign_commander`（或 `hoi4_generals` 挑一个）再重画一次，然后
才谈"进攻线不推进"是不是引擎层的 bug。**在拿到结论之前，不要对外说 M3 的"30 天拿下"
在工具链上成立** —— 手动链（§53/§54）成立，工具链的推进没证。

**70.4.2 在"已知能赢"的存档上复现，还是不推进。** 为了排除"比利时/波兰本来就不该赢"，
重开进程加载 `GER_1936_03_02_12.hoi4`（对 HOL 的战争在打，§53/§54 就是从这个存档赢的），
然后**只**调 `hoi4_army_battleplans(enemy_tag="HOL")`：

    fronts   [66,7] section 1, border 8          <- 和 §53.2 的前线一致
    targets  68 3211 3262 3314 6336 8622 9431    <- 和 §53.5 的 depth-2 列表逐项一致
    create_army 30 divs -> OG 0x265fd9b7090
    front_line  ok  新订单 id 2
    offensive   ok  child 5
    execute     ok  flags [[5,1]]  children [[5,30,1],[3,0,0]]
    ok: true, resumed_for_plan: true

结构上这就是 §53.2 那份计划，一个字段都不差。然后解暂停跑了 **7 个游戏日**：
8 个师已经站在荷兰边境省（`on-border=8`），HP 平均 204.7、组织度平均 37.9，
`+0x248` 一直是 1（两个子订单都试过置 1），**荷兰 0 个省易手，`active_combats` 一直是 0**。

结论：**§53 的"把 `+0x248` 置 1 就会进攻"在当前进程上不成立**。剩下的差别只可能在
订单是**怎么造出来的**：§53 那次是同一套直接构造函数，但当时的前线订单已经存在了
若干天；现在工具是"建军团 → 立刻画前线 → 立刻画进攻线 → 立刻执行"一气呵成。
最可能的原因是直接 `call` 构造函数造出来的 `COrderInstance` 没有走引擎命令路径的
登记逻辑（`0x10CD510` 只重建订单自己的缓存，不重建军团级的战线分配）。
下一步应该对比"UI/命令路径造出来的进攻订单"和"直接 ctor 造出来的"在
`COrdersGroup` 里的登记差异（§49 的 AI 订单差分就是干这个的）。

在此之前，M4 只能算"工具通了、订单树对、推进未证"。

另外记一笔：`hoi4_declare_war` 绕过保证条约，对象是比利时的时候把整个同盟（ENG/FRA/CAN/
RAJ/SAF/NZL… 共 17 国）一起拉进了战争，会议根本不会开（战争领袖不是比利时）。
测试收尾用 `whitepeace BEL` / `whitepeace POL` 一次结束全部战争。

## 70.5 国策写入：`set_focus` 通了，`drop_focus` 打死进程

M4 的四个家族里，国策的**写**路径也是 Linux 的：`game.set_focus()` 走
`commands.py` 的 `self.cmd.send("SetNationalFocus", ...)`，在 PE 上直接
`RuntimeError: commands.new() is Linux-only`。而 `cmdwin` 里早就有
`make_national_focus` / `set_national_focus`（构造函数 `0x1C0…`、vtable `0x2A128C8`），
只是没人接线。

接上之后实测（`hoi4_set_focus`）：

    before {"current": null}
    set    {"ok": true, "posted": 1, "focus": "GER_remilitarize_the_rhineland",
            "current": "0x1bda6e67d10"}
    after  {"current": {"key": "GER_remilitarize_the_rhineland",
                        "name": "莱茵兰重新军事化", ...}}

也就是**国策能开始**（`+0x18 CO_FOCUS_PROGRESS` 的 current 指针变过去了）。`set_focus_ptr`
走同一条路。

**取消就不行**。`CDropCurrentNationalFocusCommand` 在 `cmdvts.json` 里只有一条
"arg1 → +0x24, deref"，我按 `(cmd, _tag_block(tag))` 调它的构造函数（`29376048`），
对象是造出来了，`Post` 也过了，然后 **hoi4.exe 一秒内消失**
（下一次读内存 `GetLastError=299`）。所以 `game.drop_focus()` 在 Windows 上现在
诚实地返回

    {"ok": false, "error": "dropping a focus is not wired on hoi4.exe: ..."}

正确做法是把 `0x1C03F30` 附近那个构造函数反汇编出来看它到底读几个参数
（`cmdvts` 的推断只覆盖了 `+0x24` 一个字段，显然少了东西），在那之前别再试。

**代价**：这一条把进程打死了，当时那局的比利时/波兰收尾和刚点的国策全丢，
重开只能从 `GER_1936_03_02_12.hoi4` 起。教训和 §4 的风险 1 一样：
**构造函数签名没反汇编清楚之前，任何 `game.call(ctor, ...)` 都是拿进程做赌注**。

## 70.6 推进问题结案：不是订单错了，是**等得不够久**

§70.4 说的"订单树全对但不推进"，最后证明是**观测窗口太短**。同一份计划从建好到第一个省
易手要**几分钟的墙钟时间**，到荷兰投降约 **17 分钟**——之前的每一次判断都停在 3~5 分钟。
（这一局的日期读数不可靠：§67.1 说过 `CDate` 有好几份副本，`_win_date_addr()` 在暂停/慢速时
会挑中冻结的那份，所以这里按墙钟记，不按"游戏日"记。）

**干净复现（2026-11-15，重开进程加载 `GER_1936_03_02_12.hoi4`，只调 MCP 工具）**：

    hoi4_army_battleplans(enemy_tag="HOL")
      前线 [66,7]/8 省  ->  进攻线 7 个目标省  ->  30 师  ->  箭头 executing=1
      （调用时就解暂停建计划，建完恢复暂停）

    t+  0s  建好计划（箭头 executing=1）      荷兰境内 GER 控制 0 省
    t+244s  wait_seconds=240 到期返回          0 省   <- 240 秒还不够，别在这里下结论
    t+~300s 第一个省易手                       9 个省
    t+~600s 继续推进                          14 个省
    t+~17min                                  21 个省 -> 荷兰投降 -> 会议打开

同样的时间线在"手动"那一轮也成立（计划 03.02 建好，03.13 前后开始掉省），只是当时正好
在同一分钟里点了一次 `execute_plan(og, 3)`，看起来像那个调用起的作用，其实是时间到了。

**那 `execute_plan(og, order_id)` 到底是什么？** 顺带纠正一个坑（反汇编
`0x18C8C50` 构造函数、`0x18D64A0` Execute）：

    0x18C8D24  mov [rbx+0x30], esi    ; arg3 = order id
    0x18C8D27  mov [rbx+0x34], dil    ; arg4 = flag
    0x18D64CD  mov edx,[rdi+0x30]
    0x18D64D5  je  0x18D64FA           ; id == 0 -> 造订单树
    0x18D64F5  jmp 0x10CD510           ; id != 0 -> [order+0x248] = flag

我们的 `armyops_win.execute_plan()` 把 arg4 写死成 0，所以 `execute_plan(og, 3)` 实际是
**把 3 号订单的 executing 关掉**（"再点一次绿勾 = 取消"）。真正的"点绿勾"是把 type-2
进攻线订单的 `+0x248` 置 1，走 `set_order_executing`（引擎的 `0x10CD510`，参数
`(order, 1, 1)`）。`_win_battleplans` 现在建完树就顺手把箭头置 1，并且不再用
`execute_plan(id)` 去"激活"。

**工具改动**：`hoi4_army_battleplans` 新增 `wait_seconds`。给 0（默认）时建完就返回；
给 600 时它会解暂停一直等到第一个敌方省易手，并把
`steps.advance = {waited, enemy_provinces_taken, date}` 写进返回值 —— 这样调用方不用
猜"是不是没生效"。**240 秒实测不够**（这一局一台机器上大约 5 分钟才出第一个省），
所以默认建议值写的是 600。

**M3 整条链（全部 MCP 工具，零鼠标）**：

    hoi4_army_battleplans(enemy_tag="HOL", wait_seconds=240)
    hoi4_peace_status / hoi4_peace_demands(loser="HOL", kinds=["state"])
    hoi4_peace_finish("pass") / ("done")
    -> GER 24 -> 29 州, HOL 0 州（实测一次，见 §69 同一套会议代码）

教训写在这里：**在速度 1 的游戏里，任何"几分钟没动静"都要先换算成游戏日**。这个坑已经
吃掉两轮结论（§70.4、§70.4.2 的"不推进"都不成立）。

## 71. 和平会议收尾会**偶发卡死**（`done` 计算条约时）

2026-11-15 13:37，荷兰那次 5 条割地 action 的会议在 `done` 之后**卡住**（不是崩溃）：

* Windows 事件日志：`AppHangTransient`，`hoi4.exe`，13:43:18，无异常码 —— 是挂起。
* `logs/error.log` 最后一行：

      13:37:29  [1936.03.16.01]  History for GER is invalid.
                                Number of turn is 2 when it should be 1. Will resize.

  和 §69 那次"5 傀儡 + 1 海军"崩掉前的最后一行**完全相同**。

今天的会议一共跑了 5 次，2 次死在这一步：

| # | 内容 | 结果 |
|---|---|---|
| 1 | 5×傀儡（HOL） | ok |
| 2 | 5×傀儡 + 1×海军 | `done` 时进程消失（GetLastError=299） |
| 3 | 1×海军（DEN） | ok |
| 4 | 5×割地（HOL，工具链） | ok |
| 5 | 5×割地（HOL，工具链） | **卡死**（AppHangTransient） |

所以不是"海军 action"或"割地 action"的问题，而是**多个 action 的 turn 在 `done`
把 `+0x1F8` 合并进 `+0x1D8 → +0x1C8` 时，历史轮数对不上（2 vs 1）**，引擎的
"Will resize" 路径有时能扛住、有时挂死。

已知的间接因素：
* 那 5 条 action 是同一个 `CPeaceBiddingTurn`（size 1），所以 `+0x1F8` 的 size 是 1；
  引擎自己（AI 战胜方/自动出价）可能也放了一个 turn，合并后就成了 2。
* `pass` 和 `done` 之间只有 3~4 秒，而且比赛是用 `Execute` **直接调用**（队列在会议期间
  不抽），没有等 `+0x1F8` 被消费就发 `done`，存在竞态。

**根因**：`pass` 会给 `+0x1F8` **追加第二个 turn**。实测（2026-11-15）：

    装完 1 个 turn              +0x1F8 = 1
    pass 之后                   +0x1F8 = 2      <- 多出来的那一个
    done 之后                   +0x1C8 = 2, +0x1D8 = 2
    error.log                   History for GER ... Number of turn is 2 when it should be 1

引擎随后走 "Will resize" 把 2 压回 1，这条路有时能扛住、有时挂死（5 次里 2 次）。
我们提交的 turn 本来就已经在 `+0x1F8` 里了，**根本不需要 `pass`**。

**修法**（`calling/peacewin.py: finish()`）：提交完直接 `done`，不 `pass`。
`hoi4_peace_finish(action="done")` 现在走这条路；`pass` 仍单独保留（UI 的"跳过本轮"）。

**验证**（同一进程连跑 3 次会议，每次都看 maps 和 error.log）：

| 对象 | 动作数 | 装完 +0x1F8 | done 后 | started | 结果 |
|---|---|---|---|---|---|
| DEN | 7 | 1 | +0x1C8=1 +0x1D8=1 +0x1F8=0 | true | GER 24→31，DEN 0 州，活着 |
| LUX | 1 | 1 | 1 / 1 / 0 | true | GER 31→32，LUX 0 州，活着 |
| CZE | 10 | 1 | 1 / 1 / 0 | true | GER 32→42，CZE 0 州，活着 |

三次的 `error.log` 里**没有**任何 `History for ...` 行 —— resize 路径根本没被触发。
对比旧流程（pass→done）5 次里 2 次死：一次进程消失、一次 AppHangTransient。

**残留风险**：如果会议里有多个战胜方（例如傀儡跟着参战），`done` 只提交自己那一方，
round 可能等其他人。`finish()` 会把 `after_done` 的 maps 和 `started` 一起返回，调用方
看到 `+0x1C8` 还是 0、`started` 还是 false 时，应改用 `endturn`，不要退回 `pass`。

## 72. M4 最后两个洞：`drop_focus` 与研究进度百分比（2026-11-15 下午）

### 72.1 `CDropCurrentNationalFocusCommand`：从 vtable 反推，不用表里的地址

旧代码里的 `29376048`（`0x1C03F30`）是错的：拿 `(cmd, tagp)` 去调它在几秒内就把
进程打死了（下一次读 `GetLastError=299`）。这次改成**从 RTTI 往下查**：

```
.?AVCDropCurrentNationalFocusCommand@@  -> vtable 0x2A12988
.text 里 lea reg,[rip+0x2A12988] 只有 3 处：
    0x11CE6F0   ctor          (rcx=cmd, rdx=CCountryTag*)       size 0x30
    0x11D56A0   clone         (复制 +0x0C..+0x28)
    0x11D8D20   factory       operator new(0x30) + 默认值
```

ctor 自己写出来的布局（和 `CSetNationalFocusCommand` 0x11D0DC0 同形，只少一个
`CNationalFocus*` 参数）：

```
+0x08 byte 0    +0x0C -1    +0x10 0    +0x18 0xFFFF    +0x1C 0
+0x28 int32 = *tag          [rcx] = 0x2A12988
```

也就是说这条命令只带"谁"，执行时清掉该国的当前国策。vtable 槽位也和 set-focus
一一对应（0x00 析构 / 0x48 CanExecute / 0x50 Execute 0x11E0740 / 0x68 clone），
所以不需要再猜参数。

`calling/cmdwin.py`：`make_drop_national_focus()` 调 ctor 并**校验 vptr 和 +0x28**，
`drop_national_focus()` 在没有国策时直接返回 `no focus in progress`；`game.drop_focus()`
的 win32 分支改成走它并回读 `FP_CURRENT == 0`。

**实测**（一次进程内两轮，每轮 set → 读 → drop → 读）：

| 轮 | set 后 `FP_CURRENT` | drop 后 | 进程 |
|---|---|---|---|
| GER_remilitarize_the_rhineland | 该指针 | 0 | 活 |
| GER_the_four_year_plan | 该指针 | 0 | 活 |
| 无事可取消时再 drop | — | 0，返回 `ok:false, reason:no focus in progress` | 活 |

顺带一个教训：`selectable_focuses()` 那 427 条是**静态可选项**，不是"现在能点"的。
`GER_autarky_efforts` / `GER_reorganize_the_wehrmacht` 拿指针去 set 会被引擎拒绝
（`posted:1` 但 `FP_CURRENT` 不变），真正能点的只有 `focuses(only_startable=True)`
那 8 条。drop 本身没有问题。

### 72.2 研究进度百分比：进度在 `CTechnology` 上，不在 `CResearchSlot` 上

`+0x20 / +0x28` 一直读 0 的原因不是偏移错了，而是**它们不是当前科技进度**。
`CResearchSlot::Save`（0xF61BF0）把三个字段按引擎自己的成员号存盘：

```
slot+0x20 -> 0x31DE      slot+0x28 -> 0x34BB      slot+0x38 -> 0x3684
```

而 `CResearchSlot::SetTechnology`（0xF60170）里 `+0x20` 只在**换科技时**承接旧进度、
`+0x28` 是那一刻算出来的"还需多少"，跑科技的过程中两者都不动（实测一直是 0 和
3 000 000 = 30.0）。真正的进度在科技对象上：

```
CTechnology+0x198          int64，研究天数 x1e5（按速度加权）
CTechnology+0x160          -> CTechnologyTemplate
CTechnologyTemplate+0x3E0  int32，总天数 x1000（150000 = 150 天）
CTechnologyTemplate+0x3D8  int32，起始年份（1943 的科技读出 1943）
CResearchSlot+0x38         研究速度 x1e5（1.01 = 每天 +1.01 天）
```

两条独立验证：

1. **已完成的 56 个科技，`+0x198 / (cost/1000)` 全部正好 1.1**（165/150、220/200、
   330/300 …）—— 开局研究速度 110%，进度停在 `cost × speed` 上，一个不多一个不少。
2. **跑着的科技每天正好涨一个 `speed`**：`basic_machine_tools` 的 `+0x198`
   3 535 000 → 3 636 000 → 3 737 000（每次 +101 000，就是 `slot+0x38`），
   期间游戏时钟 1936.03.26.18 → 27.17。

于是 `hoi4_research` 现在给的是：

```
progress       = tech[+0x198] / 1e5        （已研究天数，速度加权）
required       = template[+0x3E0] / 1000   （总天数）
percent        = 100 * progress / required （夹在 100 以内）
days_remaining = (required - progress) / speed
```

实测同一进程内：basic_machine_tools 44.44/150 = 29.63%、剩 104.5 天；construction1
35.35/200 = 17.68%、剩 163 天；两分钟后分别涨到 31.65% / 19.19% —— 和面板上
"130 日"那类读数同量级（150−19.4)/1.01 ≈ 129 ✓。

`tech_cost_days()` 一并改成读 `+0x3E0/1000`，不再是 `None`。

**新科技开局自带约 30 天进度**（`+0x198` 起步 3 030 000）：这是引擎的"存档研究点"
（`BASE_RESEARCH_POINTS_SAVED` 一类），不是我们写进去的，所以 1 级科技刚点开就有
~20% 是正常的。

### 72.3 顺手修掉的两个真 bug

* `calling/session.py` 的 `G_IDLER` 还是 1.19.4.1 的 `0x459E908`。在 PE 上读它会
  直接 `ReadProcessMemory ... GetLastError=299`，被误判成"进程死了"（其实进程好好的，
  299 只是地址无效）。现在 win32 下从 `offsets_win.G_IDLER`（`0x33DDB08`）取，
  `hoi4_pause` 的 `is_paused` 也终于读对了；`pause()` 在 win32 下改成只写
  `idler+0x6C1` 并校验，不再用 ELF 的 `CPauseGame` vtable（那会打死进程）。
* `_load_tech_db()` 在数据库对象存在但还没填充时会缓存一个**空表**，之后整个进程
  的 `hoi4_research_available` 都返回 0。现在空表不缓存（和"太早加载"同样处理）。

### 72.4 现象记录：弹窗会停表，暂停字节会骗人

测研究进度时被卡了很久：时钟不走，但 `idler+0x6C1` 读 0。原因是**世界新闻弹窗**
（丹麦/卢森堡/捷克斯洛伐克的投降公告）把游戏停住了——弹窗不写暂停字节。按 ENTER
或点"令人满意的消息"关掉后时钟才继续。另外把游戏窗口最小化也会停表。

所以"游戏在不在跑"只能看**屏幕右上角的日期**，不能信 `+0x6C1`，也不能信
`date_raw()`（§67.1 已经记过它不可靠）。这次的三次"0 小时"假象全是这个原因。
