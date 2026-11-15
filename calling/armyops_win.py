"""Army orders on the Windows build (1.19.5.0), by calling the game's own routines.

Everything here was read out of the binary, not copied from the Linux table:

  * `.?AVCOrdersGroup@@` has two vtables, 0x29CEE58 / 0x29CEED0.  The two
    functions that install them (0x00C3F480, 0x00C3F800) are its constructor and
    copy constructor; COrderGroupCommand::Execute resolves handles and calls
    0x00F702D0, which `new`s 0x230 bytes and runs 0x00C3F480 -- i.e. 0x00F702D0
    is "make one COrdersGroup under this theatre".

  * 0x00C417A0 is what COrderGroupCommand::Execute actually runs for the
    "divisions -> new army" case:

        0x1418d65d4  movzx r9d, byte ptr [rbx + 0x28]   ; flag
        0x1418d65d9  lea r8, [rcx + 0x30]               ; CPdxArray<handle>*
        0x1418d65dd  lea rdx, [rcx + 0x48]              ; CCountryTag* (theatre)
        0x1418d65e1  add rcx, 0x50                      ; CCountryTag* (parent, may be 0)
        0x1418d65e5  call 0x140c417a0

    so its signature is (out_parent_handle, theatre_handle, divisions, flag).
    Inside, it resolves the theatre, calls 0x00F702D0(theatre,0,0,0,0), stores
    the flag at +0x198, then walks the array, resolves each 8-byte handle and
    attaches the division (the resolved pointer is a CArmy sub-object; the base
    is `ptr - 0x10`).

Calling 0x00C417A0 directly is what the command does on its accepted path, so
no command object, no queue and no game tick are needed -- which matters,
because a paused game never drains the command queue.

Verified live 2026-11-14 on PID 32884: 4 German divisions in, one COrdersGroup
back (handle {53,505}, +0x39 = 0 => army, +0x40 = the theatre, +0x50 = the four
divisions, +0x1B8 = 0 => no parent group), and CArmy+0xC0 on all four pointed at
it.  The COrdersGroup field offsets below are the Linux ones and every one of
them still holds on Windows.
"""
import struct
import sys

import cmdwin

# --- the routines -----------------------------------------------------------
FN_CREATE_ARMY = 0x00C417A0   # (handle* out_parent, handle* theatre, CPdxArray<handle>*, u8) -> COrdersGroup*
FN_NEW_OG      = 0x00F702D0   # (theatre,0,0,0,parent) -> COrdersGroup* -- one group, no divisions
FN_RESOLVE     = 0x142284870  # handle* -> object*

# --- fields (Linux table, all re-verified on 1.19.5.0) ----------------------
CO_THEATRES = 0x168           # CCountry -> CPdxArray<CTheatre*>
CARMY_HANDLE = 0x18           # CArmy -> {51, id}
CARMY_OG = 0xC0               # CArmy -> COrdersGroup*

OG_HANDLE      = 0x08
OG_IS_GROUP    = 0x39
OG_THEATRE     = 0x40
OG_DIVISIONS   = 0x50         # CPdxArray<CArmy*>
OG_LEADER      = 0x88
OG_ORDERS      = 0x98         # CPdxArray<COrderInstance*>
OG_NAME        = 0x160        # std::string
OG_PARENT      = 0x1B8
OG_MEMBERS     = 0x228

G_DEFAULT_ALLOC = 0x30FD000   # the game's CPdxNewDeleteAllocator instance
HANDLE_ORDER_GROUP = 53


def theatre(g, country=None):
    """The country's first CTheatre* (every country has exactly one in 1936)."""
    p = g.p
    co = country if country is not None else g.player()
    if not isinstance(co, int):
        co = co.ptr
    arr = p.u64(co + CO_THEATRES)
    if not arr or p.i32(co + CO_THEATRES + 0xC) < 1:
        return 0
    return p.u64(arr)


def _mkhandlearray(g, divisions):
    """CPdxArray<handle> of the given CArmy pointers.

    The array element is the division's own 8-byte handle (CArmy+0x18), not the
    pointer: 0x00C417A0 resolves every element through 0x142284870.
    """
    p = g.p
    n = len(divisions)
    data = cmdwin.alloc(g, 8 * n)
    p.write(data, b"".join(p.read(int(d) + CARMY_HANDLE, 8) for d in divisions))
    hdr = cmdwin.alloc(g, 0x18)
    p.write(hdr, struct.pack("<QiiQ", data, n, n, p.base + G_DEFAULT_ALLOC))
    return hdr


def _mkptrarray(g, objs):
    """CPdxArray<T*> of raw object pointers (division lists want pointers, not handles)."""
    p = g.p
    n = len(objs)
    data = cmdwin.alloc(g, 8 * n)
    p.write(data, b"".join(struct.pack("<Q", int(o)) for o in objs))
    hdr = cmdwin.alloc(g, 0x18)
    p.write(hdr, struct.pack("<QiiQ", data, n, n, p.base + G_DEFAULT_ALLOC))
    return hdr


def _tagbuf(g, handle_bytes):
    buf = cmdwin.alloc(g, 8)
    g.p.write(buf, handle_bytes)
    return buf


def create_army(g, divisions, parent=None):
    """Collect the given divisions (CArmy*) into a new army. -> (ok, err)."""
    p = g.p
    divs = [int(d) for d in divisions]
    if not divs:
        return False, "no divisions to assign"
    own = g._inj is None
    if own:
        g.attach()
    try:
        th = theatre(g)
        if not th:
            return False, "this country has no theatre"
        arr = _mkhandlearray(g, divs)
        thh = _tagbuf(g, p.read(th + OG_HANDLE, 8))
        out = _tagbuf(g, b"\0" * 8)
        og = g.call(p.base + FN_CREATE_ARMY, (out, thh, arr, 0),
                    absolute=True, force=True)
        if not og:
            return False, "the game returned no army object"
        bad = [d for d in divs if p.u64(d + CARMY_OG) != og]
        if bad:
            return False, ("%d of %d divisions were not attached (first %#x -> %#x)"
                           % (len(bad), len(divs), bad[0], p.u64(bad[0] + CARMY_OG)))
        g._last_army = og
        return True, ""
    finally:
        if own:
            g.detach()


def armies(g, country=None):
    """Every COrdersGroup reachable from the country's divisions (armies + groups)."""
    from game import _arr
    p = g.p
    co = country if country is not None else g.player()
    if not isinstance(co, int):
        co = co.ptr
    seen, out = set(), []
    for d in _arr(p, co, 0x290):
        og = p.u64(d + CARMY_OG)
        if og and og not in seen:
            seen.add(og)
            out.append(og)
            parent = p.u64(og + OG_PARENT)
            while parent and parent not in seen:
                seen.add(parent)
                out.append(parent)
                parent = p.u64(parent + OG_PARENT)
    return out


# --- battle plans: CFront and the front-line order ---------------------------
# The front-line order is COrderNewRootCommand, NOT COrderNewFrontCommand: the
# latter is a CHILD front (offensive line) that needs an existing root order.
#
#   CFront        theatre+0x50 (CPdxArray<CFront*>), handle {66,id} at +0x08,
#                 border provinces at +0x20, sections at +0x58;
#                 a section carries an id at +0x08 and provinces at +0x30.
#   0x18C9B20     the 8-arg ctor: (cmd, og, handle_buf, section, from, to,
#                 CPdxArray<CArmy*>*|0, u8 flag) -- verified by the store pattern
#                 `[r14+0x48] = [rdi]`, `[r14+0x50] = ebx`, `[r14+0x58]/[r14+0x60]
#                 = from/to`, `[r14+0x6c] = al`, and by the UI call site 0xFD08F8.
#   Post          what the UI uses (0xFD0880 calls 0x1422C4250 with flag 0).
# Verified live 2026-11-14 (PID 26368): CanExecute 1, Post 1, and the engine
# appended a type-2 order with root_front {66,7} to og+0x98 by itself.
FN_NEW_ROOT_CMD = 0x18C9B20
NR_CANEXE       = 0x18DF5E0
NR_EXECUTE      = 0x18D9A30
NR_SIZE         = 0x70

CO_FRONTS      = 0x50
FRONT_HANDLE   = 0x08
FRONT_PROVS    = 0x20
FRONT_SECTIONS = 0x58
SEC_PROVS      = 0x30
SEC_ID         = 0x08
HANDLE_FRONT   = 66
FULL           = 100000


def _ptrs(p, obj, off, limit=4096):
    """Read a CPdxArray<T*> as a list of pointers (empty on a bogus header)."""
    data = p.u64(obj + off)
    cap = p.i32(obj + off + 8)
    size = p.i32(obj + off + 12)
    if not data or not (0 <= size <= cap < limit):
        return []
    return [p.u64(data + 8 * i) for i in range(size)]


def _prov_ids(p, obj, off):
    out = []
    for pv in _ptrs(p, obj, off):
        try:
            out.append(p.i32(pv + 0xA4))
        except Exception:
            pass
    return out


def fronts(g, country=None):
    """Every CFront of the country's theatre, with its sections and provinces."""
    p = g.p
    th = theatre(g, country)
    out = []
    for f in _ptrs(p, th, CO_FRONTS):
        handle = list(struct.unpack("<ii", p.read(f + FRONT_HANDLE, 8)))
        secs = []
        for s in _ptrs(p, f, FRONT_SECTIONS):
            secs.append({"ptr": s, "id": p.i32(s + SEC_ID),
                         "provinces": _prov_ids(p, s, SEC_PROVS)})
        out.append({"ptr": f, "handle": handle,
                    "provinces": _prov_ids(p, f, FRONT_PROVS),
                    "sections": secs})
    return out


def draw_front_line(g, og, front, section=None, frm=0, to=FULL, divisions=None,
                    submit=True):
    """Give an army a front-line order along one CFront section.

    `front` is the CFront pointer (or a dict from fronts()); `section` defaults to
    the first section's id.  Returns (ok, err, cmd).
    """
    p = g.p
    if isinstance(front, dict):
        front = front["ptr"]
    if not front:
        return False, "no CFront", 0
    if section is None:
        secs = _ptrs(p, front, FRONT_SECTIONS)
        if not secs:
            return False, "the front has no sections", 0
        section = p.i32(secs[0] + SEC_ID)
    own = g._inj is None
    if own:
        g.attach()
    try:
        hp = _tagbuf(g, p.read(front + FRONT_HANDLE, 8))
        darr = _mkptrarray(g, [int(d) for d in divisions]) if divisions else 0
        cmd = cmdwin.alloc(g, NR_SIZE)
        p.write(cmd, b"\0" * NR_SIZE)
        g.call(p.base + FN_NEW_ROOT_CMD,
               (cmd, og, hp, int(section), int(frm), int(to), darr, 0),
               absolute=True, force=True)
        if p.i32(cmd + 0x50) != int(section):
            return False, ("section field +0x50 = %d, expected %d"
                           % (p.i32(cmd + 0x50), int(section))), cmd
        if p.read(cmd + 0x48, 8) != p.read(front + FRONT_HANDLE, 8):
            return False, "front handle was not copied into +0x48", cmd
        if not g.call(p.base + NR_CANEXE, (cmd, 0), absolute=True, force=True) & 0xFF:
            return False, "CanExecute false", cmd
        if submit:
            if not cmdwin.post(g, cmd):
                return False, "Post refused the command", cmd
        g._last_front_cmd = cmd
        return True, "", cmd
    finally:
        if own:
            g.detach()


def orders(g, og):
    """The army's COrderInstance list as dicts."""
    p = g.p
    out = []
    for o in _ptrs(p, og, OG_ORDERS):
        out.append({
            "ptr": hex(o),
            "id": p.i32(o + 0x244),
            "type": p.i32(o + 0x30),
            "root_front": list(struct.unpack("<ii", p.read(o + 0x258, 8))),
            "root_section": p.i32(o + 0x260),
            "from": p.i64(o + 0x268),
            "to": p.i64(o + 0x270),
        })
    return out


# --- the offensive child orders -------------------------------------------
# A front order (type 2) owns its offensive children (type 1) in a
# CPdxArray<COrderInstance*> somewhere inside its own body.  The array offset
# is not fixed, so the scan below walks the first 0x3c0 bytes.  It MUST check
# the child's vtable: without that check every 8-byte slot that happens to look
# like {ptr, cap, size} yields heap noise, and writing to that noise corrupts
# the process (three crashes on 2026-11-14).
VT_ORDER_INSTANCE = 0x2A026E8     # .?AVCOrderInstance@@ vtable
ORDER_TYPE_OFFENSIVE = 1
OI_ASSIGNED = 0x210               # CPdxArray<CArmy*> of divisions
OI_EXECUTING = 0x248              # u8; 0 = the divisions never attack
OI_ID = 0x244
OI_TYPE = 0x30
FN_ORDER_SET_EXECUTING = 0x10CD510   # (order, u8 executing, u8) -> void


def _heap(x):
    return 0x10000000000 < x < 0x7F0000000000


def order_children(g, og):
    """Type-1 offensive children of every front order of this group."""
    p = g.p
    out = []
    for o in orders(g, og):
        if o["type"] != 2:
            continue
        root = int(o["ptr"], 16)
        for off in range(0, 0x3C0, 8):
            data = p.u64(root + off)
            cap = p.i32(root + off + 8)
            size = p.i32(root + off + 12)
            if not _heap(data) or not (0 < cap < 400) or not (0 < size <= cap):
                continue
            for k in range(size):
                child = p.u64(data + 8 * k)
                if not _heap(child) or child & 7:
                    continue
                try:
                    if p.u64(child) - p.base != VT_ORDER_INSTANCE:
                        continue
                except Exception:
                    continue
                out.append({
                    "root": o["id"],
                    "ptr": child,
                    "id": p.i32(child + OI_ID),
                    "type": p.i32(child + OI_TYPE),
                    "assigned": p.i32(child + OI_ASSIGNED + 0xC),
                    # +0x248 is a BYTE: the three bytes after it are unrelated
                    # fields, so an i32 read returns garbage and start_offensives
                    # would "re-set" a flag that is already 1 every single time.
                    "executing": p.u8(child + OI_EXECUTING),
                })
    return out


def set_order_executing(g, child, on=True):
    """Set +0x248 through the engine's own routine.  Returns the byte read back."""
    p = g.p
    g.call(p.base + FN_ORDER_SET_EXECUTING, (child, 1 if on else 0, 1),
           absolute=True, force=True)
    return p.u8(child + OI_EXECUTING)


def start_offensives(g, og):
    """Mark every accepting offensive child of this group as executing.

    COrderExecuteCommand (0x18C8C50) creates the children and hands the
    divisions to them, but leaves +0x248 at 0: the plan draws on the map and
    nothing moves.  Verified 2026-11-14 -- with the flag set the same orders
    took six Dutch provinces and the Netherlands capitulated.
    """
    out = []
    for c in order_children(g, og):
        if c["type"] != ORDER_TYPE_OFFENSIVE or c["assigned"] <= 0:
            continue
        if c["executing"] == 1:
            continue
        out.append((c["id"], set_order_executing(g, c["ptr"], True)))
    return out


def army_info(g, og):
    """One COrdersGroup as a dict, with only fields verified on Windows."""
    p = g.p
    parent = p.u64(og + OG_PARENT)
    return {
        "ptr": hex(og),
        "handle": list(struct.unpack("<ii", p.read(og + OG_HANDLE, 8))),
        "is_army_group": bool(p.u8(og + OG_IS_GROUP)),
        "theatre": hex(p.u64(og + OG_THEATRE)) if p.u64(og + OG_THEATRE) else None,
        "divisions": p.i32(og + OG_DIVISIONS + 0xC),
        "orders": p.i32(og + OG_ORDERS + 0xC),
        "parent": hex(parent) if parent else None,
        "members": p.i32(og + OG_MEMBERS + 0xC) if p.u8(og + OG_IS_GROUP) else None,
    }


# =====================================================================
# M4: the pieces the MCP tool layer needs, moved out of stage/wplan.py
# =====================================================================
# `stage/wplan.py` proved these live (2026-11-14: six Dutch provinces and a
# capitulation; 2026-11-15: the Danish front + offensive).  The tool layer
# cannot import a stage script, so the same calls live here now.

FN_NEW_OFFENSIVE_CMD = 0x18C9840   # COrderNewFrontCommand ctor (the offensive line)
FN_CANEXE_OFFENSIVE  = 0x18DF550
OE_CTOR, OE_VT, OE_SIZE = 0x18C8C50, 0x2A85D60, 0x38   # COrderExecuteCommand

ST_PROV_ID = 0xA4                  # CProvince -> int32 province id


def divisions_of(g, og):
    """The CArmy* this group owns (og+0x50, CPdxArray<CArmy*>)."""
    return [d for d in _ptrs(g.p, og, OG_DIVISIONS) if _heap(d)]


def owned_provinces(g, tag):
    """state id -> [province ids] for the provinces this country OWNS.

    Reads the global state table (GS_STATES) and each state's owner.  Lives here
    so the package needs nothing outside `calling/`.
    """
    import offsets_win as W
    p = g.p
    idx = g.country(tag).tag_id
    arr = p.u64(g.gamestate + W.GS_STATES)
    n = p.i32(g.gamestate + W.GS_STATES + W.PDXARRAY_SIZE)
    out = {}
    for i in range(max(0, min(n, 4096))):
        st = p.u64(arr + 8 * i)
        if not _heap(st):
            continue
        if p.i32(st + W.CS_OWNER) != idx:
            continue
        sid = p.i32(st + W.ST_ID)
        parr = p.u64(st + W.ST_PROVINCES)
        pn = p.i32(st + W.ST_PROVINCES + 0xC)
        ids = []
        for k in range(max(0, min(pn, 512))):
            pr = p.u64(parr + 8 * k)
            if _heap(pr):
                ids.append(p.i32(pr + ST_PROV_ID))
        out[sid] = ids
    return out


def fronts_towards(g, enemy_tag, own_tag=None):
    """Front sections that actually touch `enemy_tag`.

    Same contract as armyops.fronts_towards: a section is only returned when
    some of its provinces belong to us and border the enemy.  The Linux version
    also counts which country most of the section faces; here the CFront list
    comes from the theatre (verified offsets) instead of an RTTI scan, so the
    enemy test is done by walking the static adjacency graph.
    """
    import mapdata
    own_tag = own_tag or g.player_tag()
    mine = set(sum(owned_provinces(g, own_tag).values(), []))
    theirs = set(sum(owned_provinces(g, enemy_tag).values(), []))
    adj = mapdata.build_adjacency()
    out = []
    for f in fronts(g, g.country(own_tag)):
        for si, s in enumerate(f["sections"]):
            ids = [x for x in s["provinces"] if x in mine]
            if not ids:
                continue
            touch = [x for x in ids if any(n in theirs for n in adj.get(x, ()))]
            if not touch:
                continue
            out.append({"front": f["ptr"], "front_handle": f["handle"],
                        "section": s["id"], "section_index": si,
                        "border_provinces": sorted(touch)})
    return out


def offensive_line(g, og, parent_id, provinces, divisions=None, can_execute=True):
    """Draw the offensive line (the child front) under one front order.

    `parent_id` is the type-2 order id the line hangs off; pass the id of the
    front order just drawn -- picking "the longest root" silently attaches to a
    leftover order from an earlier war (that is how the Danish run first
    pointed at the Dutch front).
    """
    p = g.p
    if not provinces:
        return False, "no target provinces", 0
    root = None
    for o in orders(g, og):
        if o["type"] == 2 and o["id"] == parent_id:
            root = int(o["ptr"], 16)
            break
    if root is None:
        return False, "no type-2 order with id %s" % parent_id, 0
    if divisions is None:
        divisions = divisions_of(g, og)
    own = g._inj is None
    if own:
        g.attach()
    try:
        path = cmdwin.alloc(g, 4 * len(provinces))
        p.write(path, b"".join(struct.pack("<i", int(x)) for x in provinces))
        pa = cmdwin.alloc(g, 0x18)
        p.write(pa, struct.pack("<QiiQ", path, len(provinces), len(provinces),
                               p.base + G_DEFAULT_ALLOC))
        darr = _mkptrarray(g, [int(d) for d in divisions]) if divisions else 0
        cmd = cmdwin.alloc(g, NR_SIZE)
        p.write(cmd, b"\0" * NR_SIZE)
        g.call(p.base + FN_NEW_OFFENSIVE_CMD,
               (cmd, og, pa, root, darr, 0, 0, 0, 0), absolute=True, force=True)
        if can_execute and not (g.call(p.base + FN_CANEXE_OFFENSIVE, (cmd, 0),
                                       absolute=True, force=True) & 0xFF):
            return False, "CanExecute false", cmd
        if not cmdwin.post(g, cmd):
            return False, "Post rejected", cmd
        return True, "", cmd
    finally:
        if own:
            g.detach()


def execute_plan(g, og, order_id=0):
    """The green tick: COrderExecuteCommand hands the divisions to the children."""
    p = g.p
    own = g._inj is None
    if own:
        g.attach()
    try:
        cmd = cmdwin.alloc(g, OE_SIZE)
        p.write(cmd, b"\0" * OE_SIZE)
        g.call(p.base + OE_CTOR, (cmd, og, int(order_id), 0, 0),
               absolute=True, force=True)
        if p.u64(cmd) != p.base + OE_VT:
            return False, "ctor wrote no vtable", 0
        if not cmdwin.post(g, cmd):
            return False, "Post rejected", cmd
        return True, "", cmd
    finally:
        if own:
            g.detach()


def target_provinces(g, enemy_tag, depth=2, own_tag=None):
    """Enemy provinces at BFS `depth` from our border -- the offensive line's targets.

    Same rule as stage/wplan.py: start at the enemy provinces that touch us and
    walk inwards, keeping everything at least `depth` steps in (depth=2 skips the
    first row, which the front line already covers).
    """
    import collections
    import mapdata
    own_tag = own_tag or g.player_tag()
    mine = set(sum(owned_provinces(g, own_tag).values(), []))
    theirs = set(sum(owned_provinces(g, enemy_tag).values(), []))
    adj = mapdata.build_adjacency()
    dist = {x: 0 for x in theirs if any(n in mine for n in adj.get(x, ()))}
    q = collections.deque(dist)
    while q:
        u = q.popleft()
        for v in adj.get(u, ()):
            if v in theirs and v not in dist:
                dist[v] = dist[u] + 1
                q.append(v)
    return sorted(x for x, d in dist.items() if d >= depth)
