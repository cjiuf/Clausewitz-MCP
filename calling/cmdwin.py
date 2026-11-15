"""The player command channel on the Windows build (1.19.5.0).

Linux posts commands through `CCommandQueue::Post`, found by pattern-scanning
`CCommand::Run`. On the PE build the same pieces exist but at different places,
and they were recovered from the *call sites of the command constructors*
instead -- every command creation site does

    new(size) -> ctor(cmd, &tag, args...) -> Post(session, cmd, 0)

so the call that follows the constructor is `Post`, and the object it is called
on comes from the idler's vtable slot +0x88 (a `CSession*`, not the command
queue the Linux build returns).

    FN_POST         0x22C4250   Post(session, cmd, flag)  -> 1 on success
    IDLER_VT_SESSION 0x88       CSession* (GetPlayerTag is +0xA0)
    ALLOC           0x30FD000   CPdxNewDeleteAllocator object; Allocate at
                                vtable+0x08, Deallocate at vtable+0x10
    OP_NEW          0x25A01DC   CRT operator new(size_t)
    OP_DELETE       0x25CEC00   CRT operator delete(void*)

`Post` owns the command on every path: the success path hands it to
`[session+0x40]->vt[0x28](cmd)`, and each of the four drop paths ends with
`call [cmd->vt[0]](cmd, 1)` -- the deleting destructor, which releases through
CRT `operator delete`. Commands therefore have to come from `operator new`,
not from the game's `IAllocator`: mixing the two aborts the process whenever a
command is refused (the 7/7 SetResearch crashes).

The drop reasons, read from the string constants `Post` builds:

    flag != 0                        "Dropped command: "
    [session+0x725] != 0             "Dropped command: "
    cmd->vt[0x80] / vt[0x60] != 0    "Dropped command: "   (both are 0x11D530,
                                                           a `xor al,al; ret`,
                                                           so they never fire)
    ... later re-checks              "Dropped (posted during command
                                      execution): "
    [session+0x3c] != 0
    [session+0x6cc] == 0             "Dropped (sender not authorized): "
    [session+0x726] != 0
    cmd->vt[0x98] failed             "Dropped (sender not authorized): "

Command constructors are the authoritative description of a command's layout,
so we call them instead of hand-filling a struct:

    CSetNationalFocusCommand  0x11D0DC0   (cmd, CCountryTag*, CNationalFocus*)
        +0x28 int32 tag, +0x30 CNationalFocus*, min size 0x38
    CSetResearchCommand       0x11D1980   (layout not verified yet)
    CAddConstructionCommand   0x11CC8C0   (layout not verified yet)

Verified live 2026-11-14: building + posting a SetNationalFocus command changed
`current_focus()` from None to the requested focus and it survived day ticks.
"""
from __future__ import annotations
import struct
import sys

import offsets as O
from commands import BASE_INIT

FN_POST = 0x22C4250
IDLER_VT_SESSION = 0x88
IDLER_VT_TAG = 0xA0
CTOR_SET_NATIONAL_FOCUS = 0x11D0DC0
CTOR_SET_RESEARCH = 0x11D1980
CTOR_ADD_CONSTRUCTION = 0x11CC8C0
VT_ADD_CONSTRUCTION = 0x2A10B88
VT_BUILDING_REF = 0x29EE730
OP_NEW = 0x25A01DC
OP_DELETE = 0x25CEC00
# `xor al, al; ret` -- the shared do-nothing stub every command vtable already
# uses for its unimplemented slots.
FN_NOOP_DTOR = 0x11D530


def _alloc_obj(p):
    return p.base + O.ALLOC_OFF


def alloc(game, size):
    """Allocate from the game's own heap (so the game can free it later)."""
    p = game.p
    obj = _alloc_obj(p)
    allocate = p.u64(p.u64(obj) + 0x08)
    return game.call(allocate, (obj, size), absolute=True, force=True)


def free(game, ptr):
    p = game.p
    obj = _alloc_obj(p)
    deallocate = p.u64(p.u64(obj) + 0x10)
    return game.call(deallocate, (obj, ptr), absolute=True, force=True)


def _i32buf(game, value):
    """A scratch int32 the game can read an id out of (ctors take int*)."""
    a = alloc(game, 8)
    game.p.write(a, struct.pack("<i", int(value)))
    return a


def new_cmd(game, size):
    """Allocate a command object the way the game allocates its own.

    Only used for probing: `Post` releases commands through the CRT, which is
    why objects it takes have to come from the CRT heap too.  Calling the CRT
    allocator from a hijacked thread was measured to take the process down, so
    real commands go through `alloc` + `attach_fake_vtable` instead.
    """
    return game.call(game.p.base + OP_NEW, (size,), absolute=True, force=True)


def delete_cmd(game, ptr):
    """Release a command we allocated but never posted."""
    return game.call(game.p.base + OP_DELETE, (ptr,), absolute=True, force=True)


def fake_vtable(game, vt_rva, size=0xB0):
    """A copy of a command vtable whose slot 0 (the deleting destructor) is a no-op.

    `Post` reaches `call [cmd->vt[0]](cmd, 1)` on the immediate path and the
    command queue reaches the same slot when it later discards the object.  Both
    would call the real deleting destructor, which frees through the CRT -- fatal
    for an object that came from the game's own allocator.  Giving the object a
    private vtable whose slot 0 returns immediately makes every release path a
    leak instead of a crash, and leaves every other virtual method real.
    """
    cache = getattr(game, "_fake_vts", None)
    if cache is None:
        cache = {}
        game._fake_vts = cache
    key = int(vt_rva)
    if key in cache:
        return cache[key]
    p = game.p
    blob = bytearray(p.read(p.base + vt_rva, size))
    struct.pack_into("<Q", blob, 0, p.base + FN_NOOP_DTOR)
    fake = alloc(game, size)
    p.write(fake, bytes(blob))
    cache[key] = fake
    return fake


def attach_fake_vtable(game, cmd, vt_rva):
    """Point a command object at its no-free vtable.  Call after the ctor ran."""
    fake = fake_vtable(game, vt_rva)
    game.p.write(cmd, struct.pack("<Q", fake))
    return fake


def session_flags(game):
    """The CSession bytes `Post` gates on, before anything is posted."""
    p = game.p
    s = session(game)
    return {
        "session": s,
        "busy_0x725": p.u8(s + 0x725),
        "b_0x3c": p.u8(s + 0x3C),
        "b_0x6cc": p.u8(s + 0x6CC),
        "b_0x726": p.u8(s + 0x726),
        "tick_0x84": p.i32(s + 0x84),
        "seq_0x60": p.i32(s + 0x60),
    }


def idler(game):
    p = game.p
    v = p.u64(p.base + O.G_IDLER)
    if not v:
        raise RuntimeError("no CInGameIdler (not inside a game)")
    return v


def session(game):
    """The CSession the command queue belongs to."""
    p = game.p
    i = idler(game)
    return game.call(p.u64(p.u64(i) + IDLER_VT_SESSION), (i,),
                     absolute=True, force=True)


def player_tag_ptr(game):
    p = game.p
    i = idler(game)
    return game.call(p.u64(p.u64(i) + IDLER_VT_TAG), (i,),
                     absolute=True, force=True)


def player_tag_id(game):
    return game.p.i32(player_tag_ptr(game))


def post(game, cmd, force=0):
    """Queue a command object. force=1 skips the CanExecute check."""
    return game.call(game.p.base + FN_POST, (session(game), cmd, force),
                     absolute=True, force=True)


def command_queue(game):
    """The CCommandQueue `Post` hands accepted commands to (`session+0x40`)."""
    p = game.p
    q = p.u64(session(game) + 0x40)
    if not q:
        raise RuntimeError("no command queue at session+0x40")
    return q


def enqueue(game, cmd, tick=None, seq=None):
    """Push a command straight into the queue, skipping `Post`'s gauntlet.

    `Post`'s accepted path ends in `[session+0x40]->vt[0x28](cmd)` after stamping
    the object with the session tick (`cmd+0xC`) and a sequence number
    (`cmd+0x18`, clamped to 0x7fff).  Every other path builds a "Dropped ..."
    message and then calls the command's deleting destructor, which is where the
    earlier experiments died.  Stamp and enqueue directly instead.
    """
    p = game.p
    s = session(game)
    if tick is None:
        tick = p.i32(s + 0x84)
    if seq is None:
        seq = p.i32(s + 0x60)
    p.write(cmd + 0xC, struct.pack("<i", int(tick) & 0xFFFFFFFF))
    p.write(cmd + 0x18, struct.pack("<H", min(int(seq) & 0xFFFF, 0x7FFF)))
    q = command_queue(game)
    fn = p.u64(p.u64(q) + 0x28)
    return game.call(fn, (q, cmd), absolute=True, force=True)


def _tag_block(game, tag_id):
    """A 16-byte block holding an int32 CCountryTag the ctor can read."""
    b = alloc(game, 0x10)
    game.p.write(b, struct.pack("<i", tag_id))
    return b


def make_national_focus(game, focus_ptr, tag_id):
    p = game.p
    cmd = alloc(game, 0x40)
    tagp = _tag_block(game, tag_id)
    game.call(p.base + CTOR_SET_NATIONAL_FOCUS, (cmd, tagp, focus_ptr),
              absolute=True, force=True)
    if p.u64(cmd) != p.base + 0x2A128C8 or p.i32(cmd + 0x28) != tag_id:
        raise RuntimeError("CSetNationalFocusCommand ctor did not produce the "
                           "expected object (vptr=%#x tag=%d)"
                           % (p.u64(cmd), p.i32(cmd + 0x28)))
    attach_fake_vtable(game, cmd, 0x2A128C8)
    return cmd


def set_national_focus(game, focus_ptr, tag_id=None):
    try:
        cur = game.current_focus()
    except Exception:
        cur = None
    if isinstance(cur, dict) and cur.get("ptr") == focus_ptr:
        return {"ok": False, "posted": 0, "reason": "focus already selected"}
    if tag_id is None:
        tag_id = int(game.player_tag_id())
    cmd = make_national_focus(game, focus_ptr, tag_id)
    ok = enqueue(game, cmd)
    return {"ok": bool(ok), "cmd": cmd, "posted": ok}

def make_research(game, tech_ptr, slot, tag_id, flags=(0, 0, 0)):
    """CSetResearchCommand, built by hand from its ctor's own stores.

    ctor 0x11D1980 = (cmd, CCountryTag*, CTechnology*, int slot,
                      bool, bool, bool) and writes
        +0x08 byte 0   +0x0C -1   +0x10 0   +0x18 0xFFFF   +0x1C 0
        +0x28 int32 tag   +0x30 CTechnology*   +0x38 int32 slot
        +0x3C/+0x3D/+0x3E bools
    The three bools live on the stack, which the injector cannot pass, so the
    object is filled directly instead of calling the ctor.
    """
    p = game.p
    cmd = alloc(game, 0x40)
    blob = bytearray(0x40)
    struct.pack_into("<Q", blob, 0x00, p.base + 0x2A11548)
    blob[0x08] = 0
    struct.pack_into("<i", blob, 0x0C, -1)
    struct.pack_into("<Q", blob, 0x10, 0)
    struct.pack_into("<I", blob, 0x18, 0xFFFF)
    struct.pack_into("<Q", blob, 0x1C, 0)
    struct.pack_into("<i", blob, 0x28, int(tag_id))
    struct.pack_into("<Q", blob, 0x30, int(tech_ptr))
    struct.pack_into("<i", blob, 0x38, int(slot))
    blob[0x3C] = flags[0] & 0xFF
    blob[0x3D] = flags[1] & 0xFF
    blob[0x3E] = flags[2] & 0xFF
    p.write(cmd, bytes(blob))
    attach_fake_vtable(game, cmd, 0x2A11548)
    return cmd


def set_research(game, tech_ptr, slot, tag_id=None):
    """Start researching a technology in the given slot."""
    if tag_id is None:
        tag_id = int(game.player_tag_id())
    cmd = make_research(game, tech_ptr, slot, tag_id)
    ok = enqueue(game, cmd)
    return {"ok": bool(ok), "cmd": cmd, "posted": ok}


# CAddConstructionCommand is 0x50 bytes.  Layout came from a live capture of the
# real constructor (Frida hook on 0x11CC8C0 while clicking the build UI):
#
#   {"typeId": 1, "level": 1, "dl": 2,
#    "ref": {"f08": 59, "f0C": 20013, "f10": 0}}
#
# so the two fields that matter are NOT what the ctor's parameter order suggests:
#   ref+0x08 = state id,      ref+0x0C = BUILDING TYPE id,  ref+0x10 = 0
#   cmd+0x28 = 1 (not the type id), cmd+0x4C = 2 (the bool/priority)
# Getting either wrong made the queue accept the command and silently drop it,
# which is why three earlier layouts produced no building.
def make_add_construction(game, state_id, building_type_id, level=1,
                          priority=2, type_field=1):
    p = game.p
    ref = alloc(game, 0x20)
    b = bytearray(0x20)
    struct.pack_into("<Q", b, 0x00, p.base + VT_BUILDING_REF)
    struct.pack_into("<i", b, 0x08, int(state_id))
    struct.pack_into("<i", b, 0x0C, int(building_type_id))
    struct.pack_into("<i", b, 0x10, 0)
    p.write(ref, bytes(b))

    cmd = alloc(game, 0x50)
    c = bytearray(0x50)
    struct.pack_into("<Q", c, 0x00, p.base + VT_ADD_CONSTRUCTION)
    struct.pack_into("<i", c, 0x0C, -1)
    struct.pack_into("<I", c, 0x18, 0xFFFF)
    struct.pack_into("<i", c, 0x28, int(type_field))
    struct.pack_into("<Q", c, 0x30, p.base + VT_BUILDING_REF)
    struct.pack_into("<i", c, 0x38, int(state_id))
    struct.pack_into("<i", c, 0x3C, int(building_type_id))
    struct.pack_into("<i", c, 0x40, 0)
    struct.pack_into("<i", c, 0x48, int(level))
    c[0x4C] = int(priority) & 0xFF
    p.write(cmd, bytes(c))
    attach_fake_vtable(game, cmd, VT_ADD_CONSTRUCTION)
    return cmd


def add_construction(game, state_id, building_type_id, level=1, priority=2):
    """Queue a building.  Verified live: state 5 gained a (20013, level 0) entry."""
    cmd = make_add_construction(game, state_id, building_type_id, level, priority)
    ok = enqueue(game, cmd)
    return {"ok": bool(ok), "cmd": cmd, "posted": ok}


# --- diplomacy -------------------------------------------------------------
#
# Declaring war is an action object wrapped in a command, not a plain command:
#
#   CDeclareWarAction        0x1180B70(this, int* from, int* to, CWarGoal*, bool, void* scopeOut)
#   CDiplomaticActionCommand 0x2A073C8, action pointer at +0x28, sender at +0x24
#   validator               0x11C2420(cmd, std::string* msgOut) -> bool
#
# Both objects come from the game allocator but the command's execute ends with
# `action->vt[0](action, 1)` -- the deleting destructor, which frees through the
# CRT.  That is the same failure that killed seven SetResearch attempts (§25), so
# both objects get a private vtable whose slot 0 does nothing.

CTOR_DECLARE_WAR = 0x1180B70
VT_DECLARE_WAR = 0x2A07548
VT_DIPLO_CMD = 0x2A073C8
FN_DIPLO_VALIDATE = 0x11C2420
# Filled into the scope struct the ctor forwards to CDiplomaticAction; the
# default ctor 0x1180CD0 uses exactly this triple.
SCOPE_MAGIC = 0x29C77F8
# `CDeclareWarAction::IsAvailable` (0x11B9C40) can only return true through the
# early-out in 0x11B8460, which tests this byte.  Its real meaning is not
# established; it is exposed as an explicit knob and defaults to on because the
# validator rejects every action without it.
DECLARE_WAR_FLAG_OFF = 0x70
# CSetGameSpeedCommand -- vtable confirmed through RTTI (1.19.5.0).  The old
# 0x4474C40 value is past the image (size 0x38A3000) and crashed the game.
VT_SET_GAME_SPEED = 0x29F3A80   # size 0x28, i32 speed at +0x24
VT_INCREASE_GAME_SPEED = 0x29F3900


def make_declare_war(game, to_id, from_id=None, wargoal=0, force=True):
    p = game.p
    if from_id is None:
        from_id = player_tag_id(game)
    if not from_id:
        raise RuntimeError("not in a game yet (player tag is 0)")
    scope = alloc(game, 0x18)
    p.write(scope, struct.pack("<QQQ", 0, SCOPE_MAGIC, 0))
    act = alloc(game, 0x88)
    p.write(act, b"\0" * 0x88)
    game.call(p.base + CTOR_DECLARE_WAR,
              (act, _i32buf(game, from_id), _i32buf(game, to_id), wargoal, 1, scope),
              absolute=True, force=True)
    if force:
        p.write(act + DECLARE_WAR_FLAG_OFF, b"\x01")
    fake_vtable(game, VT_DECLARE_WAR, size=0x1D8)
    attach_fake_vtable(game, act, VT_DECLARE_WAR)

    cmd = alloc(game, 0x30)
    blob = bytearray(0x30)
    struct.pack_into("<Q", blob, 0x00, p.base + VT_DIPLO_CMD)
    for off, fmt, val in BASE_INIT:
        struct.pack_into(fmt, blob, off, val)
    struct.pack_into("<i", blob, 0x24, int(from_id))
    struct.pack_into("<Q", blob, 0x28, int(act))
    p.write(cmd, bytes(blob))
    attach_fake_vtable(game, cmd, VT_DIPLO_CMD)
    return {"act": act, "cmd": cmd, "from": from_id, "to": to_id}


def build_by_vt(game, vt_rva, size, fields=()):
    """Build a CCommand that has no ctor we trust: vtable + BASE_INIT + fields.

    `fields` is a sequence of (offset, struct-format, value).  The object is
    pointed at the no-free vtable copy so any drop path leaks instead of
    aborting; see `fake_vtable`.
    """
    p = game.p
    size = int(size)
    blob = bytearray(size)
    struct.pack_into("<Q", blob, 0x00, p.base + int(vt_rva))
    for off, fmt, val in BASE_INIT:
        if off + struct.calcsize(fmt) <= size:
            struct.pack_into(fmt, blob, off, val)
    for off, fmt, val in fields:
        struct.pack_into(fmt, blob, int(off), val)
    cmd = alloc(game, size)
    p.write(cmd, bytes(blob))
    attach_fake_vtable(game, cmd, int(vt_rva))
    return cmd


# --- generic command construction from the PE catalogue -------------------
#
# `pecmd` recovers {vtable, ctor, size, fields} for every C*Command class in
# hoi4.exe out of MSVC RTTI + the ctor itself.  That is what lets a command be
# built by name instead of by hand: the ctor is the authoritative description of
# the layout, and calling it also runs whatever base-class setup / embedded
# sub-object construction the class does (which a hand-filled struct misses --
# the conveyor and production-line commands are all of that shape).

ARG_TAGPTR = "tagptr"      # pointer to an int32 CCountryTag
ARG_PTR = "ptr"            # a plain pointer
ARG_INT = "int"            # an int32 value
ARG_HANDLEPTR = "handleptr"  # pointer to {0, u64 handle} -- these ctors read [rdx+8]

# How each command's ctor args map onto the names the tool layer uses.
# Only the classes the tools actually call need an entry; anything else falls
# back to filling the recovered field offsets directly.
ARGSPEC = {
    "CSetNationalFocusCommand": {"tag": 1, "focus_ptr": 2},
    "CDropCurrentNationalFocusCommand": {"tag": 1},
    "CSetContinuousFocusCommand": {"tag": 1, "focus_ptr": 2},
    "CDropContinuousFocusCommand": {"tag": 1},
    "CSetResearchCommand": {"tag": 1, "tech_ptr": 2, "slot": 3, "flag": 4},
    "CSelectDecisionCommand": {"tag": 1, "decision_ptr": 2},
    "CAddIdeaCommand": {"tag": 1, "idea_ptr": 2},
    "CRemoveIdeaCommand": {"tag": 1, "idea_ptr": 2},
    "CReplaceIdeaCommand": {"tag": 1, "old_ptr": 2, "new_ptr": 3},
    "CRemoveConstructionCommand": {"tag": 1, "index": 2},
    "CCreateConveyorCommand": {"template_handle": 1, "priority": 2},
    "CCreateConveyorExtendedCommand": {"template_handle": 1, "priority": 2,
                                       "lines": 3, "state_id": 4},
    "CSetConveyorLocationCommand": {"conveyor_handle": 1, "province_id": 2},
    "CSetConveyorPriorityCommand": {"conveyor_handle": 1, "priority": 2},
    "CSetConveyorSeriesCommand": {"conveyor_handle": 1, "series": 2},
    "CAddConveyorLineCommand": {"conveyor_handle": 1, "count": 2},
    "CDeployConveyorCommand": {"conveyor_handle": 1},
    "CRemoveConveyorCommand": {"conveyor_handle": 1},
    "CSetProductionLineAmountToProduceCommand": {"line": 1, "amount": 2},
    "CAddProductionLineFactoriesCommand": {"line_id": 1, "amount": 2},
    "CAddProductionLineCommand": {"tag": 1, "arg0": 2, "arg1": 3},
    "CRemoveProductionLineCommand": {"tag": 1, "line": 2},
    "CSetArmyLeaderCommand": {"arg0": 1, "arg1": 2, "arg2": 3},
    "CUpdateDivisionTemplateCommand": {"arg0": 1, "arg1": 2},
    "CUnlockGrandDoctrineCommand": {"tag": 1, "doctrine_ptr": 2},
}
# per-argument kind, when it is not simply "matches the recovered field kind"
ARG_KIND = {
    "CSetNationalFocusCommand": {1: ARG_TAGPTR, 2: ARG_PTR},
    "CDropCurrentNationalFocusCommand": {1: ARG_TAGPTR},
    "CSetContinuousFocusCommand": {1: ARG_TAGPTR, 2: ARG_PTR},
    "CDropContinuousFocusCommand": {1: ARG_TAGPTR},
    "CSetResearchCommand": {1: ARG_TAGPTR, 2: ARG_PTR, 3: ARG_INT, 4: ARG_INT},
    "CSelectDecisionCommand": {1: ARG_TAGPTR, 2: ARG_PTR},
    "CAddIdeaCommand": {1: ARG_TAGPTR, 2: ARG_PTR},
    "CRemoveIdeaCommand": {1: ARG_TAGPTR, 2: ARG_PTR},
    "CReplaceIdeaCommand": {1: ARG_TAGPTR, 2: ARG_PTR, 3: ARG_PTR},
    "CRemoveConstructionCommand": {1: ARG_TAGPTR, 2: ARG_INT},
    "CCreateConveyorCommand": {1: ARG_HANDLEPTR, 2: ARG_INT},
    "CCreateConveyorExtendedCommand": {1: ARG_HANDLEPTR, 2: ARG_INT,
                                       3: ARG_INT, 4: ARG_INT},
    "CSetConveyorLocationCommand": {1: ARG_HANDLEPTR, 2: ARG_INT},
    "CSetConveyorPriorityCommand": {1: ARG_HANDLEPTR, 2: ARG_INT},
    "CSetConveyorSeriesCommand": {1: ARG_HANDLEPTR, 2: ARG_INT},
    "CAddConveyorLineCommand": {1: ARG_HANDLEPTR, 2: ARG_INT},
    "CDeployConveyorCommand": {1: ARG_HANDLEPTR},
    "CRemoveConveyorCommand": {1: ARG_HANDLEPTR},
    "CSetProductionLineAmountToProduceCommand": {1: ARG_HANDLEPTR, 2: ARG_INT},
    "CAddProductionLineFactoriesCommand": {1: ARG_HANDLEPTR, 2: ARG_INT},
    "CUnlockGrandDoctrineCommand": {1: ARG_TAGPTR, 2: ARG_PTR},
}


def spec_of(cls):
    """The PE catalogue entry for a command class (or short name)."""
    import pecmd
    return pecmd.get(cls)


def _handle_block(game, packed):
    """A 16-byte block whose +0x08 is the game's 8-byte {type,id} handle."""
    b = alloc(game, 0x10)
    game.p.write(b, struct.pack("<QQ", 0, int(packed) & 0xFFFFFFFFFFFFFFFF))
    return b


def build_command(game, cls, fields):
    """Allocate + construct + vtable-patch one command, by class name.

    Uses the class's real ctor whenever the argument list is known
    (`ARGSPEC`), and otherwise writes the fields `pecmd` recovered.
    """
    p = game.p
    spec = spec_of(cls)
    name = spec["class"]
    amap = ARGSPEC.get(name)
    kinds = ARG_KIND.get(name, {})
    if amap:
        n = max(list(amap.values()) + [spec["args"]])
        vals = [None] * (n + 1)
        for kw, idx in amap.items():
            if kw in fields:
                vals[idx] = fields[kw]
        args = []
        for i in range(1, n + 1):
            v = vals[i]
            kind = kinds.get(i, ARG_INT)
            if kind == ARG_TAGPTR:
                if v is None:
                    v = player_tag_id(game)
                args.append(_i32buf(game, int(v)))
            elif kind == ARG_HANDLEPTR:
                if v is None:
                    raise ValueError("%s needs %s" % (name, amap and
                                                      [k for k, j in amap.items() if j == i]))
                args.append(_handle_block(game, v))
            elif kind == ARG_PTR:
                args.append(int(v or 0))
            else:
                args.append(int(v or 0))
        size = spec["size"]
        cmd = alloc(game, size)
        game.call(p.base + spec["ctor"], tuple([cmd] + args),
                  absolute=True, force=True)
        got = p.u64(cmd)
        if got != p.base + spec["vt"]:
            raise RuntimeError("%s ctor produced vptr %#x, expected %#x"
                               % (name, got, p.base + spec["vt"]))
        attach_fake_vtable(game, cmd, spec["vt"])
        return cmd
    # no argument map: write the recovered offsets
    out = []
    for f in spec["fields"]:
        if f["arg"] is None:
            continue
        out.append((f["offset"], {8: "<Q", 4: "<i", 2: "<h", 1: "<B"}[f["size"]],
                    int(fields.get("arg%d" % f["arg"], 0))))
    return build_by_vt(game, spec["vt"], spec["size"], out)


def dispatch(game, cls, fields):
    """Build a command by class name and put it on the game's queue."""
    cmd = build_command(game, cls, fields)
    return {"ok": bool(enqueue(game, cmd)), "cmd": cmd}


# CDropCurrentNationalFocusCommand -- recovered 2026-11-15 from the vtable, not
# from a table lookup: RTTI says .?AVCDropCurrentNationalFocusCommand@@ ->
# 0x2A12988, and scanning .text for `lea reg,[rip+vtable]` gives exactly three
# hits: the ctor 0x11CE6F0, the clone 0x11D56A0 and a size-0x30 factory 0x11D8D20.
# The old 29376048 (0x1C03F30) was a stale Linux-derived number; calling it with
# (cmd, tagp) is what took the process down.
#
#     0x11CE6F0  (rcx = cmd, rdx = CCountryTag*)        size 0x30
#       +0x08 byte 0   +0x0C -1   +0x10 0   +0x18 0xFFFF   +0x1C 0
#       +0x28 int32 = *tag
#       [rcx] = 0x2A12988
#
# Same 12-byte shape as CSetNationalFocusCommand (0x11D0DC0 -> 0x2A128C8) minus
# the CNationalFocus* argument: the command carries only "who", so dropping is
# "clear whatever that country has selected".
CTOR_DROP_NATIONAL_FOCUS = 0x11CE6F0   # (cmd, CCountryTag*) -> CDropCurrentNationalFocusCommand
VT_DROP_NATIONAL_FOCUS = 0x2A12988     # .?AVCDropCurrentNationalFocusCommand@@
SIZE_DROP_NATIONAL_FOCUS = 0x30


def make_drop_national_focus(game, tag_id):
    p = game.p
    cmd = alloc(game, SIZE_DROP_NATIONAL_FOCUS)
    tagp = _tag_block(game, tag_id)
    game.call(p.base + CTOR_DROP_NATIONAL_FOCUS, (cmd, tagp),
              absolute=True, force=True)
    got_vt, got_tag = p.u64(cmd), p.i32(cmd + 0x28)
    if got_vt != p.base + VT_DROP_NATIONAL_FOCUS or got_tag != tag_id:
        raise RuntimeError("CDropCurrentNationalFocusCommand ctor did not "
                           "produce the expected object (vptr=%#x tag=%d)"
                           % (got_vt, got_tag))
    attach_fake_vtable(game, cmd, VT_DROP_NATIONAL_FOCUS)
    return cmd


def drop_national_focus(game, tag_id=None):
    """Cancel the focus in progress (`CDropCurrentNationalFocusCommand`)."""
    if tag_id is None:
        tag_id = int(game.player_tag_id())
    if not tag_id:
        raise RuntimeError("not in a game yet (player tag is 0)")
    cur = game.current_focus()
    if not cur:
        return {"ok": False, "posted": 0, "reason": "no focus in progress"}
    cmd = make_drop_national_focus(game, tag_id)
    ok = enqueue(game, cmd)
    return {"ok": bool(ok), "cmd": cmd, "posted": ok, "was": cur.get("key")}


def set_game_speed(game, speed):
    """The engine's own SetGameSpeed command (vtable 0x4474C40, speed at +0x24)."""
    speed = int(speed)
    if not 0 <= speed <= 5:
        raise ValueError("speed must be between 0 and 5")
    cmd = build_by_vt(game, VT_SET_GAME_SPEED, 0x28, [(0x24, "<i", speed)])
    return post(game, cmd)


def validate_declare_war(game, cmd):
    """Ask the game's own validator.  msgOut must be NULL, exactly as Execute calls it."""
    return game.call(game.p.base + FN_DIPLO_VALIDATE, (cmd, 0),
                     absolute=True, force=True) & 0xFF


def declare_war(game, to_id, from_id=None, wargoal=0, force=True, validate=False):
    """Declare war.  Verified live: the game raised its own war notification.

    `validate=True` additionally calls the game's checker first.  It is off by
    default because that call has been observed to take the process down when the
    action carries the patched vtable; the command's execute runs the same check
    internally anyway, so skipping it changes nothing about the outcome.
    """
    objs = make_declare_war(game, to_id, from_id=from_id, wargoal=wargoal, force=force)
    valid = validate_declare_war(game, objs["cmd"]) if validate else None
    if validate and not valid:
        return {"ok": False, "valid": False, "posted": 0, **objs}
    posted = enqueue(game, objs["cmd"])
    return {"ok": bool(posted), "valid": valid, "posted": posted, **objs}
