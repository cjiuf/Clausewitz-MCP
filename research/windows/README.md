# Windows probes (HOI4 1.19.5.0, MSVC build)

Everything here was written against the Steam Windows build
`Operation Postern v1.19.5.0.7c6c (c184)` (`hoi4.exe`, PE32+, 56.5 MB) and run
against a live game process.  The Linux build (1.19.4.1.99a5) is a different
binary — do not mix the two tables.

Run order:

| script | what it does |
|---|---|
| `rtti_win.py` | parses the PE, walks MSVC RTTI (`RTTITypeDescriptor` → `RTTICompleteObjectLocator` → vtable) and writes `rtti_win.pkl`: **19 706 types, 16 567 vtables, 15 141 classes**, 0.5 s |
| `win_gs.py` | follows `image_base + 0x33DD6B0` → `CCurrentGameState`, prints the 440-country table with tag ids and localised names |
| `wmap.py` | structural map of one `CCountry`: every pointer field and every `CPdxArray` with its element class |
| `wstate2.py` | allocator-tagged `CPdxArray` enumeration (this is how `CO_STATES = 0x460` was found) |
| `wstate3.py` | pins `CState` owner/controller (`0xC8` / `0xCC`) by requiring a field to equal the right country index for GER, ENG and SOV state lists |
| `wtag.py` | finds the country-tag `std::string[443]` table (stride 0x20) and dumps `---`, `GER`, `ENG`, `SOV`, … |
| `wconsole.py` | scans `.data` for the `CPdxArray<CConsoleCmd>` singleton |
| `wconsole3.py` | decodes all **370** console commands (name / aliases / function RVA) |
| `wdiff.py` | differential scan: samples a struct twice and reports the fields that tick (this is how `CPoliticalStatus+0xE0` = political power was proven) |
| `xdis.py` | tiny capstone disassembler by RVA (used to recover the console command ABI) |
| `wpp3.py` | live end-to-end test: three `pp` calls through the injector, printing the political power before/after |

`rtti_win.pkl` is generated, not committed.

Support scripts the table above leans on:

| script | what it does |
|---|---|
| `wdump.py` | PE section map + a RVA range reader, shared by everything below |
| `xdis.py` | RVA disassembler (capstone) |
| `wcallers.py` | every `E8 rel32` call site that targets a given RVA |
| `wxref.py` | every RIP-relative reference to a given RVA in `.text` |
| `wstr.py` | ASCII/UTF-16 string search in the data sections, with code xrefs |
| `wcmds.py` | catalogue + run console commands through `consolecmd.run` |

Injector diagnostics (crash hunting; each is a single-shot probe):

| script | what it does |
|---|---|
| `wctx_test.py` | round-trips the `CONTEXT` register offsets on a scratch thread — no game involved |
| `wseq.py` | graded injector verification: context read-back, then real calls, printing before each step |
| `wstep.py` | single-steps the first instructions of an injected call and dumps registers |
| `wdbg2.py` | attaches a debugger, runs one injected `pp`, reports the fault address and every thread RIP |
| `wthr.py` | read-only: lists hoi4 threads and their current RIP |
| `wstress.py` | many console calls in one attach, verifying each result |

Prerequisites: `capstone` (only for the disassembly scripts), and a running `hoi4.exe`.
Read `research/WINDOWS_1.19.5.md` for the full findings, the verified offset table and
the command-layer design.
