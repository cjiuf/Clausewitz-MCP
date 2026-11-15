"""Diplomacy: justifying war goals, declaring war, and related actions.

1.19.4.1 layout, re-derived 2026-11-04 from the binary itself
(rtti.pkl + .eh_frame_hdr function starts + disassembly):

  CDeclareWarAction          vtable 0x4359288  size 0x78  default ctor 0xA77450
  CGenerateWarGoalAction     vtable 0x4359718  size 0x90  default ctor 0xA7DB30
  CDiplomaticActionCommand   vtable 0x4358EB0  size 0x30  +0x24 tag, +0x28 action*
  CDiplomaticAction (base)   vtable 0x4359040
      +0x08 action type      +0x10/+0x14 sender tag {id,0}
      +0x18/+0x1c target tag +0x20 scope (embedded)  +0x6C CWarGoal handle
      +0x88 CWarGoalType* (GenerateWarGoal only)

The old constants (VT_DECLAREWAR 0x41AED30, FN_DECLAREWAR_CTOR 0xA4FB80) were
1.19.2 values: 0x41AED30 does not exist in this image at all and 0xA4FB80 lands in
the middle of an unrelated function, which is why every declare_war call died with
sig=11. Both constructors here take ONLY `this`; the sides and the war goal are
plain fields, so they are written directly (no guessed varargs).
"""
import struct

import offsets as O

VT_DIPCMD           = 0x4358EB0   # CDiplomaticActionCommand
VT_DECLAREWAR       = 0x4359288   # CDeclareWarAction
VT_GENERATE_WARGOAL = 0x4359718   # CGenerateWarGoalAction
CTOR_DECLAREWAR     = 0xA77450    # void CDeclareWarAction(this) — DESERIALISATION ctor
CTOR_WARGOAL        = 0xA7DB30    # void CGenerateWarGoalAction(this) — ditto
# The real (parameterised) constructors. The zero-arg ones above only call the
# DEFAULT base ctor 0x3602650, which leaves from/to and the whole Scope block
# (+0x20..+0x48) empty; CanExecute then always returns false. These call the full
# base ctor 0xA6C2F0(this, from, to, scope, flag, -1) instead.
CTOR_DECLAREWAR_PARAM = 0xA77580  # (this, from*, to*, CWarGoal*, u8 flag, scope*)
CTOR_WARGOAL_PARAM    = 0xA7DD10  # (this, from*, to*, CWarGoalType*, scope*)
SCOPE_MAGIC = 0x29C77F8           # the 4-byte value every Scope block is built with
SZ_DECLAREWAR       = 0x78
SZ_WARGOAL          = 0x90
SZ_DIPCMD           = 0x30
G_GAMESTATE         = 0x459E9F0   # 1.19.4.1
RUN_CMD             = 0x20AE0A0   # bool CCommand::Run(cmd, std::string* err)

ACT_TYPE   = 0x08
ACT_FROM   = 0x10
ACT_TO     = 0x18
ACT_WARGOAL = 0x6C
WG_STATES  = 0x70
WG_TYPE    = 0x88

CO_TAG_INDEX = 0x08     # the CCountryTag inside CCountry (GER=1, POL=10 ...)


def wargoal_types(g):
    """War goal types in the game: name -> CWarGoalType*"""
    p = g.p
    out = {}
    hits = list(g.oi.instances("CWarGoalType"))
    if not hits:
        # A freshly re-attached process has no instances indexed yet, and the
        # empty list made every justify call report "unknown wargoal (options: )".
        try:
            hits = list(g.oi.deep_instances("CWarGoalType"))
        except Exception:
            hits = []
    for w in hits:
        try:
            import sys as _sys
            if _sys.platform == "win32":
                # 1.19.5: the name is a MSVC std::string at +0x18 (buffer),
                # size at +0x28, capacity at +0x30 -- not a char* at +0x10.
                # Reading +0x10 as a pointer produced garbage for every entry,
                # so the map came back empty and justify_wargoal rejected every
                # name ("unknown wargoal (options: )").
                sz = p.i64(w + 0x28)
                cap = p.i64(w + 0x30)
                if cap >= 16:
                    n = (p.read(p.u64(w + 0x18), sz).decode("utf-8", "replace")
                         if 0 < sz < 200 else "")
                else:
                    n = p.read(w + 0x18, max(0, min(sz, 15))).decode("utf-8", "replace")
            else:
                n = p.cstr(p.u64(w + 0x10))
        except Exception:
            continue
        if n and n.isprintable():
            out[n] = w
    return out


def tag_index(g, tag):
    return g.p.i32(g.country(tag).ptr + CO_TAG_INDEX)


def _post_action(g, act_ptr, tag_id):
    """Wrap a CDiplomaticAction* in a CDiplomaticActionCommand and Run it.

    The command object is a flat 0x30 bytes; CCommand::Run performs CanExecute and
    hands it to the queue — exactly what the diplomacy dialog does on "Send".
    No 1.19.2 constructor is called (FN_DIPCMD_CTOR/FN_DIPCMD_CANEXE are gone).
    """
    from commands import BASE_INIT
    p = g.p
    cmd = g.malloc(SZ_DIPCMD)
    blob = bytearray(SZ_DIPCMD)
    struct.pack_into("<Q", blob, 0x00, p.base + VT_DIPCMD)
    for off, fmt, val in BASE_INIT:
        struct.pack_into(fmt, blob, off, val)
    struct.pack_into("<i", blob, 0x24, int(tag_id))
    struct.pack_into("<Q", blob, 0x28, int(act_ptr))
    p.write(cmd, bytes(blob))
    rv = g.cmd.post(cmd)
    return bool(rv & 0xFF), ("" if (rv & 0xFF) else
                             (getattr(g.cmd, "last_error", "") or "CanExecute refused the action"))


def _dipcmd(g, act_ptr, tag_id):
    """A hand-built CDiplomaticActionCommand wrapping `act_ptr` (0x30 bytes, flat)."""
    from commands import BASE_INIT
    p = g.p
    cmd = g.malloc(SZ_DIPCMD)
    blob = bytearray(SZ_DIPCMD)
    struct.pack_into("<Q", blob, 0x00, p.base + VT_DIPCMD)
    for off, fmt, val in BASE_INIT:
        struct.pack_into(fmt, blob, off, val)
    struct.pack_into("<i", blob, 0x24, int(tag_id))
    struct.pack_into("<Q", blob, 0x28, int(act_ptr))
    p.write(cmd, bytes(blob))
    return cmd


def _can_execute_cmd(g, cmd):
    """Read-only CanExecute probe (vtable+0x50) on an already built command."""
    p = g.p
    fn = p.u64(p.base + VT_DIPCMD + 0x50)
    # CDiplomaticActionCommand::CanExecute (0xA68750, disassembled 2026-11-04:
    # reads [cmd+0x28]=action, [action+0x10]=sender id, returns a bool; no writes).
    import api
    api.allow_call(0xA68750)   # must be the IMAGE OFFSET, not the runtime address
    return bool(g.call(fn, (cmd, 0), absolute=True) & 0xFF)


def _can_execute(g, act_ptr, tag_id):
    """Read-only CanExecute probe (vtable+0x50) on a hand-built command."""
    cmd = _dipcmd(g, act_ptr, tag_id)
    try:
        return _can_execute_cmd(g, cmd)
    finally:
        try:
            g.free(cmd)
        except Exception:
            pass


def _i32buf(g, v):
    a = g.malloc(4)
    g.p.write(a, struct.pack("<i", int(v)))
    return a


def _scope_obj(g):
    """The 0x18-byte Scope block the parameterised constructors expect.

    Both of them read `[scope+0x08]` and copy it into the local scope struct
    (`{vtable 0x445CD48+0x10, <that value>, vtable 0x445CD48+0x60}`). The default
    constructors hard-code the same value (0x29C77F8) with a 32-bit immediate, so a
    fresh 0x18 buffer carrying it is enough to let the real ctor build the block.
    """
    s = g.malloc(0x18)
    g.p.write(s, struct.pack("<QQQ", 0, SCOPE_MAGIC, 0))
    return s


def _new_action_param(g, size, ctor, vtable, args):
    """Build an action with its PARAMETERISED constructor (the one the UI uses)."""
    p = g.p
    act = g.malloc(size)
    p.write(act, b"\0" * size)
    import api
    api.allow_call(ctor)
    g.call(ctor, [act] + list(args))
    got = p.u64(act)
    if got != p.base + vtable:
        raise RuntimeError("constructor %#x did not build the expected action "
                           "(vptr=%#x, want %#x) — refusing to post"
                           % (ctor, got, p.base + vtable))
    return act


def _new_action(g, size, ctor, vtable):
    p = g.p
    act = g.malloc(size)
    p.write(act, b"\0" * size)
    import api
    api.allow_call(ctor)
    g.call(ctor, [act])
    got = p.u64(act)
    if got != p.base + vtable:
        raise RuntimeError("constructor %#x did not build the expected action "
                           "(vptr=%#x, want %#x) — refusing to post"
                           % (ctor, got, p.base + vtable))
    return act


def _set_sides(g, act, src_tag, dst_tag):
    p = g.p
    i_from, i_to = tag_index(g, src_tag), tag_index(g, dst_tag)
    p.write(act + ACT_FROM, struct.pack("<ii", i_from, 0))
    p.write(act + ACT_TO, struct.pack("<ii", i_to, 0))
    return i_from, i_to


# ------------------------------------------------------------------ war goal
WARGOAL_NEEDS_STATES = ("take_state", "take_core_state", "take_claimed_state",
                        "take_state_focus")


def _state_array(g, act, state_ptrs):
    """Write CPdxArray<int32> at +0x70: data, cap, size, allocator.

    The elements are STATE IDS (4 bytes each), not CState* and not SHandles.
    Proof: the serializer for field 0x2E3B (0x84CA40) walks the array with
    `mov esi, DWORD PTR [r14 + rbx*4]` — a 4-byte stride. Writing 8-byte pointers
    or handles made the game crash (minidump) on the first real justification.
    """
    p = g.p
    n = len(state_ptrs)
    buf = g.malloc(max(1, n) * 4)
    if n:
        p.write(buf, b"".join(struct.pack("<i", p.i32(int(x) + 0x48)) for x in state_ptrs))
    p.write(act + WG_STATES, struct.pack("<QiiQ", buf, n, n, p.base + 0x4505128))


def justify_wargoal(g, target_tag, wargoal="take_state", states=(), source_tag=None,
                    dry_run=False):
    """Start justifying a war goal (the same command as the diplomacy dialog).

    `states` may be CState pointers (Game.justify_wargoal resolves ids) or ids.
    With no states and a state-based wargoal, every state of the target is tried
    in turn and the first one CanExecute accepts is used — that is exactly what a
    player does by clicking through the list.
    """
    p = g.p
    wgs = wargoal_types(g)
    if wargoal not in wgs:
        return False, "unknown wargoal: %s (options: %s)" % (
            wargoal, ", ".join(sorted(wgs)))
    own = g._inj is None
    if own:
        g.attach()
    try:
        src = source_tag or g.player().tag
        act = _new_action_param(g, SZ_WARGOAL, CTOR_WARGOAL_PARAM, VT_GENERATE_WARGOAL,
                                [_i32buf(g, tag_index(g, src)),
                                 _i32buf(g, tag_index(g, target_tag)),
                                 wgs[wargoal], _scope_obj(g)])
        i_from = tag_index(g, src)

        if states:
            cands = [list(states)]
        elif wargoal in WARGOAL_NEEDS_STATES:
            try:
                sts = [s["ptr"] for s in g.states(target_tag)]
            except Exception:
                sts = []
            # Danzig first (85) — the historical German claim — then the rest.
            sts.sort(key=lambda x: 0 if p.i32(x + 0x48) == 85 else 1)
            cands = [[x] for x in sts]
        else:
            cands = [[]]
        if not cands:
            return False, "the target owns no state to take"

        tried = []
        for st in cands:
            _state_array(g, act, st)
            # ALWAYS pre-check CanExecute. Posting an action whose fields the game
            # does not fully understand faults the process (observed: sig=11 rip=0x21
            # when the state array held pointers). A refused pre-check costs nothing.
            ok = _can_execute(g, act, i_from)
            err = "" if ok else "CanExecute false"
            if ok and not dry_run:
                ok, err = _post_action(g, act, i_from)
            ids = [p.i32(x + 0x48) for x in st]
            tried.append({"states": ids, "ok": ok})
            if ok:
                return True, ""
            if len(tried) >= 12:
                break
        return False, "CanExecute refused every attempt: %s" % tried
    finally:
        if own:
            g.detach()


# ------------------------------------------------------------------ declare war
# ---------------------------------------------------------------- CWarGoal
# Offsets inside CWarGoal, re-derived 2026-11-13 from the ctor at 0x258C880.
#
# sizeof(CWarGoal) = 0x90 (the only `new` feeding the ctor allocates 0x90). The
# ctor itself writes only +0x08 (an id copied from a global), +0x10 (a flag
# byte), the embedded Scope at +0x18..+0x2F (vtables 0x445CD48+0x10 / +0x60,
# magic 0x29C77F8), +0x30, and three CPdxArrays at +0x48/+0x60/+0x78.
# owner/target/type are filled in afterwards by CAddWarGoalAction / the
# deserialiser:
#
#     +0x38  i32  owner  country index   (GER = 1)
#     +0x3C  i32  target country index   (DEN = 25)
#     +0x40  u64  CWarGoalType*          (0x560A45390660 == take_state)
#
# Verified live: the CWarGoal produced by a GER->DEN take_state justification
# read exactly (owner=1, target=25, type=take_state). The +0x10/+0x14 pair the
# old code used read (1, 1) on that same object, i.e. it was never owner/target
# and every "owner_tag" this tool printed before 2026-11-13 was noise.
WG_OWNER = 0x38
WG_TARGET = 0x3C
WG_TYPE = 0x40
WG_ID = 0x08


def wargoals(g, tag=None, deep=None, valid_only=False):
    """Live CWarGoal objects, decoded.

    2026-11-08: `ObjectIndex.live_instances` only rescans the heap regions that
    already held an instance when the index was built, so a CWarGoal created
    later in a *different* arena is invisible. `deep=True` scans every writable
    region (4-6 s) and unions the result, which is what a "did my justification
    actually produce a casus belli?" query needs.

    2026-11-13: `valid` is the reliable predicate - a registered id (the qword at
    +0x08, non-zero), a country pair in range and a +0x40 pointer that is one of
    the live CWarGoalType objects. Freed/recycled blocks keep the vtable and fail
    all three (the 1937 save had 6 vtable hits, of which 1 was a real CB).
    """
    p = g.p
    types = wargoal_types(g)
    inv = {v: k for k, v in types.items()}
    out = []
    seen = set()
    tags = getattr(g, "_tags", None) or []
    ns = len(tags)

    def _tag(i):
        return tags[i] if 0 <= i < ns else None

    want_owner = tag_index(g, tag) if tag else None
    auto = deep is None
    hits = list(g.oi.live_instances("CWarGoal"))
    if deep:
        try:
            for a in g.oi.deep_instances("CWarGoal"):
                if a not in hits:
                    hits.append(a)
        except Exception:
            pass
    for w in hits:
        if w in seen:
            continue
        seen.add(w)
        try:
            wid = p.u64(w + WG_ID)
            owner = p.i32(w + WG_OWNER)
            target = p.i32(w + WG_TARGET)
            tptr = p.u64(w + WG_TYPE)
            tname = inv.get(tptr)
            if want_owner is not None and owner != want_owner:
                continue
            rec = {"ptr": hex(w), "id": wid,
                   "owner_index": owner, "target_index": target,
                   "owner_tag": _tag(owner), "target_tag": _tag(target),
                   "wargoal": tname}
            rec["valid"] = (bool(wid) and tname is not None
                            and 0 <= owner < 4096 and 0 <= target < 4096)
            rec["tags_resolved"] = bool(rec["owner_tag"] and rec["target_tag"])
            if valid_only and not rec["valid"]:
                continue
            out.append(rec)
        except Exception:
            pass
    if auto and not any(x["valid"] for x in out):
        # The fast arena scan found nothing usable; a CB created in a fresh arena
        # is invisible to it, so pay for the full writable-memory walk once.
        return wargoals(g, tag=tag, deep=True, valid_only=valid_only)
    return out


def find_wargoal(g, target_tag, source_tag=None, deep=None):
    """The player's live casus belli against `target_tag`, or None."""
    src = source_tag or g.player().tag
    i_src, i_tgt = tag_index(g, src), tag_index(g, target_tag)
    for w in wargoals(g, deep=deep, valid_only=True):
        if w["owner_index"] == i_src and w["target_index"] == i_tgt:
            return w
    return None


def declare_war(g, target_tag, wargoal_ptr=None, flag=0, dry_run=False):
    """Declare war. Requires a valid war goal (justify one first)."""
    p = g.p
    own = g._inj is None
    if own:
        g.attach()
    try:
        src = g.player().tag
        i_from = tag_index(g, src)
        # 2026-11-13: CWarGoal DOES exist (vtable 0x4477D88, sizeof 0x90) — the old
        # "no RTTI class" note was wrong, it just has no `C*Action` name so the
        # diploscan cache never listed it. `find_wargoal` returns the one CB whose
        # (+0x38 owner, +0x3C target) matches, which is exactly the pointer the
        # diplomacy dialog hands to CDeclareWarAction. Passing the OTHER vtable hits
        # (freed blocks) is what produced the sig=11 crashes.
        cb = find_wargoal(g, target_tag, src) if not wargoal_ptr else None
        cands = ([int(wargoal_ptr)] if wargoal_ptr
                 else ([int(cb["ptr"], 16)] if cb else []))
        # A NULL war goal is only meaningful while the `no_wargoal` cheat is on, and
        # only POSTING it is safe: CDeclareWarAction::CanExecute dereferences the
        # pointer (sig=11 rip=+0x... with rax=0), so a dry run must never probe 0.
        if not dry_run and 0 not in cands:
            cands.append(0)
        if dry_run and not cands:
            live = [w for w in wargoals(g, deep=False) if w["valid"]]
            return False, ("no live casus belli against %s (the game refuses a war "
                           "without a wargoal). Valid war goals on the board: %s"
                           % (target_tag, [(w["owner_tag"], w["target_tag"], w["wargoal"])
                                           for w in live] or "none"))
        tried = []
        for wg in cands:
            try:
                act = _new_action_param(
                    g, SZ_DECLAREWAR, CTOR_DECLAREWAR_PARAM, VT_DECLAREWAR,
                    [_i32buf(g, i_from), _i32buf(g, tag_index(g, target_tag)),
                     wg, int(flag), _scope_obj(g)])
            except Exception as e:
                tried.append({"wargoal": hex(wg), "error": str(e)})
                continue
            if dry_run:
                ok = _can_execute(g, act, i_from)
                tried.append({"wargoal": hex(wg), "applicable": ok})
                if ok:
                    return True, "applicable (dry_run: nothing was sent)"
                continue
            ok, err = _post_action(g, act, i_from)
            tried.append({"wargoal": hex(wg), "ok": ok, "error": err})
            if ok:
                return True, ""
        if dry_run:
            return False, "no war goal was applicable: %s" % tried
        if not cb and not wargoal_ptr:
            live = [w for w in wargoals(g, deep=False) if w["valid"]]
            return False, ("no live casus belli against %s (the game refuses a war "
                           "without a wargoal). Valid war goals on the board: %s"
                           % (target_tag, [(w["owner_tag"], w["target_tag"], w["wargoal"])
                                           for w in live] or "none"))
        return False, ("the game refused every war goal (%d tried). Last: %s"
                       % (len(tried), tried[-1] if tried else ""))
    finally:
        if own:
            g.detach()


def action_catalog():
    """All diplomatic action classes: {name: {vtable, ctor, size}}"""
    import diploscan
    raw = diploscan.build()
    out = {}
    allmap = raw.get("_all") if isinstance(raw, dict) else None
    if isinstance(allmap, dict):
        for name, info in allmap.items():
            ctors = info.get("ctors") or []
            pick = None
            for c in ctors:
                sizes = c.get("sizes") or []
                if sizes:
                    pick = (c.get("addr"), sizes[0])
                    break
            if not pick and ctors:
                pick = (ctors[0].get("addr"), 112)
            if pick and pick[0]:
                out[name] = {"vtable": info.get("vtable"), "ctor": pick[0], "size": pick[1]}
    for name, info in (raw or {}).items():
        if name == "_all" or not isinstance(info, dict):
            continue
        if "ctor" in info and "size" in info:
            out[name] = {"vtable": info.get("vtable"), "ctor": info["ctor"], "size": info["size"]}
    return out


def build_action(g, action_class, target_tag, source_tag=None):
    """Build an action object with its default ctor, then write the two sides."""
    cat = action_catalog()
    if action_class not in cat:
        return None, "unknown action: %s" % action_class
    info = cat[action_class]
    if action_class == "CDeclareWarAction":
        info = {"vtable": VT_DECLAREWAR, "ctor": CTOR_DECLAREWAR, "size": SZ_DECLAREWAR}
    elif action_class == "CGenerateWarGoalAction":
        info = {"vtable": VT_GENERATE_WARGOAL, "ctor": CTOR_WARGOAL, "size": SZ_WARGOAL}
    p = g.p
    try:
        act = _new_action(g, info["size"], info["ctor"], info["vtable"])
    except Exception as e:
        return None, str(e)
    i_from, i_to = _set_sides(g, act, source_tag or g.player().tag, target_tag)
    if p.i32(act + ACT_FROM) != i_from or p.i32(act + ACT_TO) != i_to:
        return None, "%s: sides were not written" % action_class
    return act, None


def send_action(g, action_class, target_tag, source_tag=None, dry_run=False):
    own = g._inj is None
    if own:
        g.attach()
    try:
        act, err = build_action(g, action_class, target_tag, source_tag)
        if err:
            return False, err
        tag_id = tag_index(g, g.player().tag)
        if dry_run:
            ok = _can_execute(g, act, tag_id)
            return ok, ("applicable (dry_run: nothing was sent)" if ok
                        else "not applicable (conditions are not met)")
        return _post_action(g, act, tag_id)
    finally:
        if own:
            g.detach()


def _add_game_days(g, days):
    """当前游戏日期 + days 天 -> 'YYYY.MM.DD'。"""
    try:
        from api import date_to_parts
        y, m, d, _h = date_to_parts(int(g.date_raw() + float(days) * 24))
        return "%04d.%02d.%02d" % (y, m, d)
    except Exception:
        return None


# ---------------------------------------------------------------- 工期模型
# 1.19.4.1 游戏自己的数据：common/wargoals/00_invasion.txt
#   take_state = { generate_base_cost = 200  generate_per_state_cost = 50 }
# 加上 NDiplomacy 的常数：
#   WARGOAL_JUSTIFY_TENSION_FROM_PRODUCTION = 30.0   （生产规模满额时 +30 天）
#   WARGOAL_PER_JUSTIFY_AND_WAR_COST_FACTOR = 1.5    （每多一个同存正当化/交战对象 x1.5）
#   BASE_GENERATE_WARGOAL_DAILY_PP          = 0.2    （每天烧 0.2 PP）
#   POLITICAL_POWER_UPPER_CAP               = 2000.0
# 这个模型在 2026-11-08 的两个实测点上完全吻合：HOL[35] = 230 天、BEL[6] = 345 天。
WARGOAL_BASE_COST = 200.0
WARGOAL_PER_STATE_COST = 50.0
WARGOAL_PRODUCTION_TERM = 30.0
WARGOAL_PER_COUNTRY = 1.5
WARGOAL_DAILY_PP = 0.2
POLITICAL_POWER_CAP = 2000.0


def _estimate_days(n_states, n_concurrent):
    base = (WARGOAL_BASE_COST + WARGOAL_PRODUCTION_TERM
            + WARGOAL_PER_STATE_COST * max(0, n_states - 1))
    return round(base * (WARGOAL_PER_COUNTRY ** max(0, n_concurrent)), 1)


def _country_timed_activities(g):
    """CTimedWargoalActivity* read straight from CPoliticalStatus+0x08.

    fieldmaps.pkl: CPoliticalStatus {8: ('timed_activities', 'object')} and the
    array uses the same CPdxArray layout as PL_PARTIES (+0x20): data +0, cap +8,
    size +0xC. vtable 0x4398978 is 21CTimedWargoalActivity in rtti.pkl.
    """
    from game import _arr, CO_POLITICS
    p = g.p
    try:
        vt = p.va(0x4398978)
    except Exception:
        try:
            vt = g.base + 0x4398978
        except Exception:
            return []
    out, seen = [], set()
    try:
        tags = list(g.tags())
    except Exception:
        return out
    for t in tags:
        try:
            pol = p.u64(g.country(t).ptr + CO_POLITICS)
            if not pol:
                continue
            for a in _arr(p, pol, 0x08, limit=64):
                if not a or a in seen or a < 0x10000:
                    continue
                try:
                    if p.u64(a) == vt:
                        seen.add(a)
                        out.append((t, a))     # (owner tag, activity ptr)
                except Exception:
                    continue
        except Exception:
            continue
    return out


def justifying(g):
    """所有正在进行的正当化（读引擎真实对象，不是猜的）。

    1.19.4.1 `CTimedWargoalActivity` 布局（2026-11-12 用活进程重新互证）：
      +0x08 int32 活动 id
      +0x0C int32 发起国 tag id         （实测 GER=1）
      +0x10 int32 目标国 tag id         （实测 HOL=8 / BEL=7）
      +0x14 int32 未知/变体字段（HOL take_state=0，BEL take_state=1，不是类型也不是发起国）
      +0x18 int64 总时长，定点 x1e5，单位=天
      +0x20 int64 每日进度，定点 x1e5，单位=天/天
      +0x28 int64 已过天数，定点 x1e5
    旧注释把 +0x14 当成“发起国”，于是 take_state 的活动读出 from=0，被
    `src not in idx2tag` 直接丢掉——正当化明明在跑却报 not_started。
    旧实现逐 8 字节猜 (t1,t2) 并把 +0x10 当发起国，所以把 GER->HOL 印成了
    HOL->GER（方向反了），也没有 days_left / eta_date。
    """
    p = g.p
    idx2tag = {}
    for t in g.tags():
        try:
            idx2tag[tag_index(g, t)] = t
        except Exception:
            pass
    out = []

    def _bust_negative(cache_name):
        """Drop an EMPTY cache entry only: live=2s, deep=5s TTL. The old code
        cached [] before the engine had built the activity and every later poll
        in the same 3s window read that []; a stale negative outlived the event."""
        c = getattr(g.oi, cache_name, None)
        if c and "CTimedWargoalActivity" in c and not c["CTimedWargoalActivity"][1]:
            c.pop("CTimedWargoalActivity", None)

    def _instances():
        """live_instances misses objects created after the index was built."""
        direct = _country_timed_activities(g)
        if direct:
            return direct
        _bust_negative("_live_cache")
        seen = list(g.oi.live_instances("CTimedWargoalActivity"))
        if seen:
            return seen
        _bust_negative("_deep_cache")
        try:
            # build(force=True) here re-scanned ~3.8 GB on every miss; the direct
            # read above already covers objects the index has never seen.
            return list(g.oi.deep_instances("CTimedWargoalActivity"))
        except Exception:
            return []

    for item in _instances():
        # _country_timed_activities() yields (owner_tag, ptr): the owner of the
        # CPoliticalStatus list IS the country that started the justification.
        owner, a = item if isinstance(item, tuple) else (None, item)
        try:
            tgt = p.i32(a + 0x10)
            src_tag = owner if owner else idx2tag.get(p.i32(a + 0x0C))
            if tgt not in idx2tag or not src_tag or src_tag == idx2tag.get(tgt):
                continue
            total = p.i64(a + 0x18) / O.FIXED
            per = p.i64(a + 0x20) / O.FIXED
            # 2026-11-11: +0x18 is the TOTAL duration, not the remainder (it never
            # moved: 250.0 on 1936.01.26 and again on 1936.03.30). Elapsed days live
            # at +0x28 (67.0 on 03.30, 72.0 on 04.08). Reporting total as days_left
            # made a running justification look frozen forever.
            done = p.i64(a + 0x28) / O.FIXED
            days = max(0.0, round(total - done, 2))
            out.append({
                "ptr": hex(a),
                "from": src_tag,
                "target": idx2tag[tgt],
                "field_0x14": p.i32(a + 0x14),
                "source_class": "CTimedWargoalActivity",
                "days_left": days,
                "eta_date": _add_game_days(g, days) if days is not None else None,
                "progress_days": round(done, 2),
                "total_days": round(total, 2),
                "per_day": round(per, 3),
                "progress": round(min(1.0, done / total), 3) if total > 0 else None,
                "remaining_pp_drain": round(days * WARGOAL_DAILY_PP, 1) if days is not None else None,
            })
        except Exception:
            pass
    if not out:
        # 命令已入队、引擎还没建出 activity 的那几帧
        try:
            _left = g.oi.deep_instances("CGenerateWarGoalAction")
        except Exception:
            _left = g.oi.live_instances("CGenerateWarGoalAction")
        for act in _left:
            try:
                src, tgt = p.i32(act + 0x10), p.i32(act + 0x18)
                if src in idx2tag and tgt in idx2tag and src != tgt:
                    out.append({"ptr": hex(act), "from": idx2tag[src],
                                "target": idx2tag[tgt],
                                "source_class": "CGenerateWarGoalAction",
                                "days_left": None, "eta_date": None,
                                "note": "queued, engine has not created the timed activity yet"})
            except Exception:
                pass
    return out


def wargoal_quote(g, target_tag, wargoal="take_state", state_ptrs=(), state_ids=(),
                  source_tag=None):
    """询价：正当化 target_tag 的指定州，现在够不够 PP、差多少、大概多少天。

    返回的 `required_pp` 是引擎自己算出来的整数门槛，不是猜的：进入 `with g:`
    后全部游戏线程被 ptrace 冻结，把 CCountryPolitics+0xE0 的 PP 临时写成探测值，
    直接调 CDiplomaticActionCommand::CanExecute（0xA68750），在 0..2000 之间二分，
    求出 CanExecute 第一次放行的整数 PP；`finally` 里把 8 字节原始值写回，
    游戏线程全程冻结，看不到任何中间值，也不发任何命令。
    """
    p = g.p
    wgs = wargoal_types(g)
    if wargoal not in wgs:
        return {"ok": False, "error": "unknown wargoal: %s (options: %s)"
                % (wargoal, ", ".join(sorted(wgs)))}
    src = source_tag or g.player().tag
    src_c = g.country(src)
    tgt_c = g.country(target_tag)
    pol = src_c.politics + O.PO_POLITICAL_POWER
    cur_pp = src_c.political_power
    own = g._inj is None
    if own:
        g.attach()
    try:
        act = _new_action_param(g, SZ_WARGOAL, CTOR_WARGOAL_PARAM, VT_GENERATE_WARGOAL,
                                [_i32buf(g, tag_index(g, src)),
                                 _i32buf(g, tag_index(g, target_tag)),
                                 wgs[wargoal], _scope_obj(g)])
        _state_array(g, act, list(state_ptrs))
        cmd = _dipcmd(g, act, tag_index(g, src))
        saved = p.i64(pol)
        if not state_ptrs and wargoal in WARGOAL_NEEDS_STATES:
            # A take_state quote over an EMPTY state array can never be allowed.
            # Pick the first state that passes at max PP (Danzig first), exactly the
            # order justify_wargoal uses, so the quote describes the same attempt.
            try:
                _cands = [s["ptr"] for s in g.states(target_tag)]
            except Exception:
                _cands = []
            _cands.sort(key=lambda x: 0 if p.i32(x + 0x48) == 85 else 1)
            _picked = None
            _cap = int(POLITICAL_POWER_CAP * O.FIXED)
            for _st in _cands[:12]:
                _state_array(g, act, [_st])
                p.write(pol, struct.pack("<q", _cap))
                if _can_execute_cmd(g, cmd):
                    _picked = _st
                    break
            p.write(pol, struct.pack("<q", saved))
            if _picked is not None:
                _state_array(g, act, [_picked])
                if not state_ids:
                    state_ids = [p.i32(_picked + 0x48)]
        try:
            can_now = _can_execute_cmd(g, cmd)
            p.write(pol, struct.pack("<q", int(POLITICAL_POWER_CAP * O.FIXED)))
            allowed = _can_execute_cmd(g, cmd)
            required = None
            if allowed:
                lo, hi = 0, int(POLITICAL_POWER_CAP)
                while lo < hi:
                    mid = (lo + hi) // 2
                    p.write(pol, struct.pack("<q", mid * O.FIXED))
                    if _can_execute_cmd(g, cmd):
                        hi = mid
                    else:
                        lo = mid + 1
                required = lo
        finally:
            p.write(pol, struct.pack("<q", saved))
            try:
                g.free(cmd)
            except Exception:
                pass
    finally:
        if own:
            g.detach()

    n_states = max(1, len(state_ptrs))
    # 2026-11-11: queued CGenerateWarGoalAction leftovers are not concurrent
    # justifications; counting them turned a 230-day quote into 13,263 days.
    concurrent = len([x for x in justifying(g)
                      if x.get("from") == src
                      and x.get("source_class") == "CTimedWargoalActivity"])
    est = _estimate_days(n_states, concurrent)
    shortfall = None if required is None else max(0, required - cur_pp)
    if required is None:
        note = "blocked by diplomatic rules (already justifying/war with this country, truce, NAP...)"
    elif shortfall:
        note = "need %d more PP (have %s, engine wants %d)" % (shortfall, cur_pp, required)
    else:
        note = "ready to justify now"
    return {
        "ok": allowed,
        "source": src,
        "target": target_tag,
        "wargoal": wargoal,
        "states": list(state_ids),
        "can_justify_now": can_now,
        "allowed_by_rules": allowed,
        "current_pp": cur_pp,
        "required_pp": required,
        "pp_shortfall": shortfall,
        "daily_pp_drain": WARGOAL_DAILY_PP,
        "concurrent_justifications": concurrent,
        "estimated_days": est,
        "estimated_completion_date": _add_game_days(g, est),
        "estimated_total_pp": round(est * WARGOAL_DAILY_PP, 1),
        "estimate_source": ("200 + 30(生产项) + 50/额外州，x1.5/同存正当化；"
                            "实测校验 HOL[35]=230、BEL[6]=345。精确值在开跑后用 "
                            "hoi4_justifying 读 CTimedWargoalActivity"),
        "note": note,
    }

