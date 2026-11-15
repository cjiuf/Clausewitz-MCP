"""Drive the peace conference -- lifecycle commands *and* the demands themselves.

Everything here was read out of hoi4.exe 1.19.5.0; addresses are PE RVAs.

Lifecycle commands (all one int at +0x28 = the country index the UI would send)

    CDonePeaceConferenceCommand      vtable 0x2AFC800  ctor 0x1ED1D70  size 0x30
    CEndTurnPeaceConferenceCommand   vtable 0x2AFC8C0                  size 0x38
    CPassPeaceConferenceCommand      vtable 0x2AFC740  ctor 0x1ED1E50  size 0x30

Demands are data, not commands.  The chain is

    CPeaceConference +0x1E8 : std::map<countryIndex, CPeaceBiddingTurn*>
    CPeaceBiddingTurn       : 0x40 bytes
        +0x00 vtable 0x29EAEC0
        +0x08 int
        +0x10 CPdxArray<CPeaceAction*>   {data,cap,size,alloc}  (0x18)
        +0x28 CPdxArray<...>                                     (0x18)

`CPeaceConference::EndTurn(int* countryIndex, CPeaceBiddingTurn** turn)`
(0xEB5390) finds-or-inserts the map slot and takes ownership of the turn, so it
is the correct way to install one.

The action objects are built by five per-type builders that all append to
`arg+0x38` (a CPdxArray), keyed only by `arg+0x10` = the *losing* country index:

    0x14F26C0   change government   0x30D2
    0x14F2870   liberate            0x30D0
    0x14F3070   puppet              0x30D1
    0x14F3560   take navy           0x30EE
    0x14F3950   take states         0x30CF
    0x14F2680   all five in one call

Type ids were confirmed by calling the engine's own name lookup (0xEA9F00):
0x30CF -> 获得地区, 0x30D0 -> 解放, 0x30D1 -> 傀儡, 0x30D2 -> 改变政府,
0x30EE -> 夺取海军.

Object layouts (from the ctors):

    CTakeStatePeaceAction  0x50   0x15B46D0(this, int* a, int* b, CState*)
        +0x08 0x30CF   +0x30 *a   +0x34 *b   +0x38 *a   +0x48 state
    CPuppetPeaceAction     0x60   0x15B4530(this, int* a, int* b, int* c, int state)
        +0x08 0x30D1   +0x30 *a   +0x34 *b   +0x38 *a   +0x48 state  +0x50 *c
    CTakeNavyPeaceAction          0x15B4600(this, int* a, int* b, void* r9)
        +0x08 0x30EE   +0x48 r9   +0x50 5th arg   (name is built in the ctor)

`a` is the int returned by 0xC07100(&local, 0) -- measured as 0 -- and `b` is
the country index of the loser whose assets are being taken.
"""
import struct

import cmdwin

DONE_VT, DONE_CTOR, DONE_SIZE = 0x2AFC800, 0x1ED1D70, 0x30
ENDTURN_VT, ENDTURN_SIZE = 0x2AFC8C0, 0x38
PASS_VT, PASS_CTOR, PASS_SIZE = 0x2AFC740, 0x1ED1E50, 0x30
CANEXEC_DONE = 0x1ED23A0
CANEXEC_PASS = 0x1ED2A00

CONF_TURN_MAP = 0x1E8
CONF_BID_MAP = 0x1C8
VT_TURN = 0x29EAEC0
TURN_SIZE = 0x40
FN_INSTALL_TURN = 0xEB5390
FN_PUSH_BACK = 0x1403371D0

BUILD_CHANGE_GOV = 0x14F26C0
BUILD_LIBERATE = 0x14F2870
BUILD_PUPPET = 0x14F3070
BUILD_NAVY = 0x14F3560
BUILD_STATES = 0x14F3950
BUILD_ALL = 0x14F2680

BUILDERS = {
    'state': BUILD_STATES,
    'puppet': BUILD_PUPPET,
    'navy': BUILD_NAVY,
    'liberate': BUILD_LIBERATE,
    'gov': BUILD_CHANGE_GOV,
}

ACTION_SIZE = {'state': 0x50, 'puppet': 0x60}


def conference(g):
    """Every live CPeaceConference (normally zero or one)."""
    p = g.p
    tgt = p.base + 0x29CDA28
    out = []
    for m in p.regions(writable=True, min_size=4096):
        a = m["start"]
        while a < m["end"]:
            n = min(8 << 20, m["end"] - a)
            buf = p.try_read(a, n)
            if buf:
                i = buf.find(struct.pack("<Q", tgt))
                while i >= 0 and len(out) < 8:
                    out.append(a + i)
                    i = buf.find(struct.pack("<Q", tgt), i + 1)
            a += n
    return out


GET_CONF = 0x1ED1EE0


def live_conference(g):
    """The open conference in O(1) through the engine's own getter.

    `conference()` above costs ~29 s (it walks every writable region), which is
    useless for polling; 0x1ED1EE0 is the function CEndTurn/Done::Execute call
    first and returns 0 when no conference is running.
    """
    try:
        v = g.call(g.p.base + GET_CONF, (), absolute=True, force=True)
    except Exception:
        v = 0
    if v and 0x10000000000 < v < 0x40000000000:
        return v
    return None


def summary(g):
    """Small dict describing the live conference (for MCP tools / logging)."""
    p = g.p
    conf = live_conference(g)
    if not conf:
        return {"open": False}
    maps = {}
    for off in (0x1B8, 0x1C8, 0x1D8, 0x1E8, 0x1F8):
        maps["%#x" % off] = int(p.u64(conf + off + 8))
    parts = []
    data = p.u64(conf + 0xF8)
    n = p.i32(conf + 0x104)
    if 0x10000000000 < data < 0x40000000000:
        for i in range(max(0, min(n, 8))):
            o = p.u64(data + 8 * i)
            if not (0x10000000000 < o < 0x40000000000):
                continue
            parts.append({"country": p.i32(o + 0x08),
                          "score": p.i32(o + 0x4C),
                          "score_now": p.i32(o + 0x48)})
    return {"open": True, "conf": conf, "maps": maps,
            "participants": parts,
            "started": bool(p.read(conf + 0xDD, 1)[0]),
            "no_bid": bool(p.read(conf + 0xDC, 1)[0])}


EXEC = {"done": 0x1ED22E0, "endturn": 0x1ED2300, "pass": 0x1ED2350}


def maps(g, conf=None):
    """The five inline turn maps of the conference, as sizes."""
    p = g.p
    conf = conf or live_conference(g)
    if not conf:
        return {}
    return {hex(off): map_size(g, conf, off) for off in (0x1B8, 0x1C8, 0x1D8, 0x1E8, 0x1F8)}


def finish(g, winner_idx, do_pass=False, wait=20.0):
    """Close the conference without the pass -> done race.

    `pass` APPENDS a second turn to +0x1F8 (measured: 1 -> 2).  `done` then merges
    both into +0x1D8/+0x1C8 and the engine logs

        History for <TAG> is invalid. Number of turn is 2 when it should be 1.
        Will resize.

    -- and sometimes hangs while applying the treaty (2026-11-15: 2 of 5 runs).
    The submitted turn is already sitting in +0x1F8, so Done on its own is enough
    and keeps the count at 1.
    """
    import time
    p = g.p
    conf = live_conference(g)
    if not conf:
        raise RuntimeError("no peace conference is open")
    out = {"before": maps(g, conf)}
    n = out["before"].get("0x1f8")
    out["submitted_turns"] = n
    if n and int(n) > 1:
        out["warning"] = ("+0x1F8 already holds %s turns; install_turns should have "
                          "left exactly one" % n)
    if do_pass:
        out["pass"] = execute(g, "pass", winner_idx)
        out["after_pass"] = maps(g)
        t0 = time.time()
        while time.time() - t0 < wait:
            if map_size(g, conf, 0x1F8) <= 1:
                break
            time.sleep(0.25)
        out["waited_after_pass"] = round(time.time() - t0, 2)
    out["done"] = execute(g, "done", winner_idx)
    out["after_done"] = maps(g)
    out["started"] = bool(p.read(conf + 0xDD, 1)[0])
    return out


def execute(g, what, country_idx):
    """Run one of the three conference commands directly (the queue does not drain)."""
    p = g.p
    conf = live_conference(g)
    if not conf:
        raise RuntimeError("no peace conference is open")
    size = 0x38 if what == "endturn" else 0x30
    cmd = cmdwin.alloc(g, size)
    p.write(cmd, b"\0" * size)
    p.write(cmd + 0x28, struct.pack("<i", int(country_idx)))
    p.write(cmd + 0x30, struct.pack("<Q", 0))
    r = g.call(p.base + EXEC[what], (cmd,), absolute=True, force=True)
    return {"ran": what, "result": r, "summary": summary(g)}


def _post(g, vt, ctor, size, idx):
    p = g.p
    cmd = cmdwin.alloc(g, size)
    p.write(cmd, b"\0" * size)
    buf = cmdwin.alloc(g, 4)
    p.write(buf, struct.pack("<i", int(idx)))
    if ctor:
        g.call(p.base + ctor, (cmd, buf), absolute=True, force=True)
        if p.u64(cmd) != p.base + vt:
            return False, "ctor did not install the vtable", cmd
    else:
        p.write(cmd, struct.pack("<Q", p.base + vt))
        p.write(cmd + 0x28, struct.pack("<i", int(idx)))
    if not cmdwin.post(g, cmd):
        return False, "Post refused", cmd
    return True, "", cmd


def done(g, idx=1):
    """Submit my demands and close my side of the conference."""
    return _post(g, DONE_VT, DONE_CTOR, DONE_SIZE, idx)


def passed(g, idx=1):
    """Pass (give up my remaining bids)."""
    return _post(g, PASS_VT, PASS_CTOR, PASS_SIZE, idx)


def end_turn(g, idx=1, turn=0):
    """Submit this country's demands and close the round.

    CEndTurnPeaceConferenceCommand::Execute (0x1ED2300) is

        conf = 0x1ED1EE0()
        0x1A86C60(*(cmd+0x30), conf)        # CPeaceBiddingTurn::Attach(conf)
        0xEB5390(conf, &cmd+0x28, &cmd+0x30)  # steal the turn into the map

    so +0x28 is the country index and +0x30 the CPeaceBiddingTurn* -- not zero.
    Passing 0 (the old behaviour) hands the engine a null turn.
    """
    p = g.p
    cmd = cmdwin.alloc(g, ENDTURN_SIZE)
    p.write(cmd, b"\0" * ENDTURN_SIZE)
    p.write(cmd, struct.pack("<Q", p.base + ENDTURN_VT))
    p.write(cmd + 0x28, struct.pack("<i", int(idx)))
    p.write(cmd + 0x30, struct.pack("<Q", int(turn)))
    if not cmdwin.post(g, cmd):
        return False, "Post refused", cmd
    return True, "", cmd


def _allocator(g):
    import offsets_win as W
    return g.p.base + W.G_DEFAULT_ALLOC


def new_turn(g):
    """A zeroed CPeaceBiddingTurn with two empty arrays and a valid allocator."""
    p = g.p
    turn = cmdwin.alloc(g, TURN_SIZE)
    p.write(turn, b"\0" * TURN_SIZE)
    p.write(turn, struct.pack("<Q", p.base + VT_TURN))
    empty = struct.pack("<QiiQ", 0, 0, 0, _allocator(g))
    p.write(turn + 0x10, empty)
    p.write(turn + 0x28, empty)
    return turn


def install_turn(g, conf, country_idx, turn, map_off=CONF_BID_MAP):
    """Hand the turn to the conference.  Returns the turn.

    +0x1C8 is the map `done` consults: EB50E0 looks the player's index up there
    and sets conf+0xDC when the entry is missing.  +0x1E8 is what EndTurn fills.

    Calling 0xEB5390 by hand does not work: the function reads its `find` result
    out of a stack slot its own caller is expected to have set up, so an
    injected call silently writes the value into the tree's head sentinel and
    leaves the size at 0.  The map node is therefore linked by hand -- with a
    single element that is just the standard red-black shape.
    """
    p = g.p
    head = p.u64(conf + map_off)
    if not (0x10000000000 < head < 0x40000000000):
        raise RuntimeError("conference %#x has no tree head at %#x" % (conf, map_off))
    node = cmdwin.alloc(g, 0x30)
    p.write(node, b"\0" * 0x30)
    # _Left, _Parent, _Right -> head ; _Color = black ; _Isnil = 0
    p.write(node, struct.pack("<QQQ", head, head, head))
    p.write(node + 0x18, struct.pack("<BB", 0, 0))
    p.write(node + 0x20, struct.pack("<i", int(country_idx)))
    p.write(node + 0x28, struct.pack("<Q", turn))
    # the head sentinel is a nil node; point it at the only real node
    p.write(head, struct.pack("<QQQ", node, node, node))
    p.write(conf + map_off + 8, struct.pack("<Q", 1))
    return turn


def build_actions(g, loser_idx, kind='state', winner_idx=None):
    """Run one of the engine's own per-type builders.

    Returns a list of CPeaceAction* that the engine just allocated for the given
    losing country -- exactly what the conference window would list.

    The builder fills `+0x30`/`+0x38` from a stack local produced by
    ``0xC07100(&local, 0)`` (0x14F3A2F..0x14F3A48).  Called from the window that
    local holds the player's country index, but an injected call gets 0 -- and a
    demand whose winner is 0 is silently worth nothing (round 2 came back with
    cost 0).  Pass `winner_idx` to write the real one.
    """
    p = g.p
    arg = cmdwin.alloc(g, 0x100)
    p.write(arg, b"\0" * 0x100)
    p.write(arg + 0x10, struct.pack("<i", int(loser_idx)))
    # Every builder appends to arg+0x38; the puppet one also *clears* arg+0x68
    # first (0x14F30A5 calls 0x1AE000 on it), so that array needs a real
    # allocator too -- a zeroed one took the game down.
    empty = struct.pack("<QiiQ", 0, 0, 0, _allocator(g))
    p.write(arg + 0x38, empty)
    p.write(arg + 0x68, empty)
    g.call(p.base + BUILDERS[kind], (arg,), absolute=True, force=True)
    data = p.u64(arg + 0x38)
    cap = p.i32(arg + 0x40)
    size = p.i32(arg + 0x44)
    if not (0x10000000000 < data < 0x40000000000) or not (0 <= size <= 4096):
        return []
    acts = [p.u64(data + 8 * i) for i in range(size)]
    if winner_idx:
        for a in acts:
            if 0x10000000000 < a < 0x40000000000:
                p.write(a + 0x30, struct.pack("<i", int(winner_idx)))
                p.write(a + 0x38, struct.pack("<i", int(winner_idx)))
    return acts


# --------------------------------------------------------------------------
# Puppet / take-navy: the per-type builders 0x14F3070 / 0x14F3560 crash the
# process when called from an injected thread (0xEB67D0 inside them wants the
# real window context; two attempts on 2026-11-15 took hoi4.exe down).  The
# constructors are plain field writers, so build the action objects directly.
#
#   base 0x15B43F0(this, type, int* a, int* b, int* c)
#       +0x08 type, +0x10 std::string name (from 0x253ECD0(type)), +0x30 = *a,
#       +0x34 = *b, +0x38 = *a, +0x3c/+0x40 = 0
#   puppet 0x15B4530(this, a*, b*, c*, int state)   size 0x60
#       +0x48 state, +0x50 = *c ; builder passes b == c == &loser (0x14F30FE)
#   navy   0x15B4600(this, a*, b*, void* r9, void* arg5)  size 0x58
#       +0x48 = r9, +0x50 = arg5 ; the "whole fleet" call is r9 = 0,
#       arg5 = 0x186A0 (100000 = 100%, see 0x14F3849..0x14F385E)
# --------------------------------------------------------------------------
PUPPET_CTOR = 0x15B4530
NAVY_CTOR = 0x15B4600
ACTION_SIZE_CTOR = {'puppet': 0x60, 'navy': 0x58}


def make_action(g, kind, winner_idx, loser_idx, state=0, share=100000, fleet=0):
    """Allocate one puppet / take-navy action through its constructor.

    For `navy`, `fleet` is the value the builder puts in r9 (see 0x14F3849:
    "whole fleet" is 0 and `share` is 100000 = 100%).  Live result 2026-11-15:
    with fleet=0 against Denmark's two fleets the treaty moved 6 of 7 ships --
    one fleet's worth -- so a "take the whole navy" demand is one action per
    fleet, not one action in total.
    """
    p = g.p
    size = ACTION_SIZE_CTOR[kind]
    obj = cmdwin.alloc(g, size)
    p.write(obj, b"\0" * size)
    a = cmdwin.alloc(g, 4)
    b = cmdwin.alloc(g, 4)
    p.write(a, struct.pack("<i", int(winner_idx)))
    p.write(b, struct.pack("<i", int(loser_idx)))
    if kind == 'puppet':
        g.call(p.base + PUPPET_CTOR, (obj, a, b, b, int(state)),
               absolute=True, force=True)
    elif kind == 'navy':
        g.call(p.base + NAVY_CTOR, (obj, a, b, int(fleet), int(share)),
               absolute=True, force=True)
    else:
        raise ValueError(kind)
    return obj


def describe(g, act):
    """Fields of a CPeaceAction, including the name string it built."""
    p = g.p
    # The name is a std::string at +0x10, but its size/capacity words differ
    # per action class (CPuppetPeaceAction is 0x60 while the state action is
    # 0x50), so a bad read must never take the caller down.
    name = ""
    try:
        ln = p.i64(act + 0x18)
        if 0 < ln <= 0x40:
            if ln > 0x20:
                name = p.read(p.u64(act + 0x10), min(ln, 64)).decode('utf-8', 'replace')
            else:
                name = p.read(act + 0x10, min(ln, 15)).decode('utf-8', 'replace')
    except Exception:
        name = "<unreadable>" 
    return {"act": act, "type": p.i32(act + 8), "name": name,
            "a": p.i32(act + 0x30), "b": p.i32(act + 0x34), "a2": p.i32(act + 0x38),
            "state": p.i32(act + 0x48), "extra": p.i32(act + 0x50)}


def make_puppet_demands(g, winner_idx, loser_idx, state_ids):
    return [make_action(g, 'puppet', winner_idx, loser_idx, st) for st in state_ids]


def make_navy_demand(g, winner_idx, loser_idx):
    return make_action(g, 'navy', winner_idx, loser_idx)


def map_size(g, conf, off):
    return g.p.u64(conf + off + 8)


# --------------------------------------------------------------------------
# conf+0x1F8 is where the *window's* 提交要求 button puts a turn.
#
# Verified live 2026-11-15 (Germany, HOL beaten, one state selected by hand):
# pressing the button left +0x1B8/+0x1C8/+0x1D8/+0x1E8 at size 0 and made
# +0x1F8 size 1.  The node is 0x40 bytes -- key at +0x20 and an *inline*
# CPdxArray<CPeaceBiddingTurn*> at +0x28 (data +0x28, cap +0x30, size +0x34,
# alloc +0x38) -- not the 0x30-byte pointer node that 0xEB5390 builds in
# +0x1E8.  The turn it held was byte-identical to what new_turn()+builders
# produce (vtable 0x29EAEC0, one 0x30CF action for state 35).
#
# `done` (CDonePeaceConferenceCommand::Execute 0x1ED22E0 -> 0xEB9AE0) only
# looks at +0x1D8 and +0x1C8, so the merge +0x1F8 -> +0x1D8 -> +0x1C8 has to
# happen in the engine's own end-of-round/apply path; installing the turn is
# therefore the whole job on our side.
# --------------------------------------------------------------------------
CONF_SUBMIT_MAP = 0x1F8


def _map_find(p, conf, off, key):
    head = p.u64(conf + off)
    if not head:
        return None
    cur = p.u64(head + 8)
    while cur and cur != head:
        k = p.i32(cur + 0x20)
        if k == key:
            return cur
        cur = p.u64(cur) if key < k else p.u64(cur + 0x10)
    return None


def install_turns(g, conf, country_idx, turns, map_off=CONF_SUBMIT_MAP):
    """Put `turns` (a list of CPeaceBiddingTurn*) in conf+map_off for a country.

    Creates the 0x40-byte inline-array node when the key is absent and rewrites
    the array header when it is already there, so calling it twice is safe.
    """
    p = g.p
    head = p.u64(conf + map_off)
    if not (0x10000000000 < head < 0x40000000000):
        raise RuntimeError("conference %#x has no tree head at %#x" % (conf, map_off))
    data = cmdwin.alloc(g, 8 * max(1, len(turns)))
    p.write(data, b"".join(struct.pack("<Q", t) for t in turns))
    arr = struct.pack("<QiiQ", data, len(turns), len(turns), _allocator(g))

    node = _map_find(p, conf, map_off, country_idx)
    if node:
        p.write(node + 0x28, arr)
        return node
    node = cmdwin.alloc(g, 0x40)
    p.write(node, b"\0" * 0x40)
    p.write(node, struct.pack("<QQQ", head, head, head))
    p.write(node + 0x18, struct.pack("<BB", 0, 0))
    p.write(node + 0x20, struct.pack("<i", int(country_idx)))
    p.write(node + 0x28, arr)
    p.write(head, struct.pack("<QQQ", node, node, node))
    p.write(conf + map_off + 8, struct.pack("<Q", 1))
    return node


def submit_demands(g, conf, winner_idx, loser_idx, kinds=("state",),
                   map_off=CONF_SUBMIT_MAP):
    """Build one turn from the engine's builders and submit it like the button."""
    p = g.p
    acts = []
    for kind in kinds:
        for act in build_actions(g, loser_idx, kind, winner_idx=winner_idx):
            if 0x10000000000 < act < 0x40000000000 and not act & 7:
                acts.append(act)
    turn = new_turn(g)
    if acts:
        data = cmdwin.alloc(g, 8 * len(acts))
        p.write(data, b"".join(struct.pack("<Q", a) for a in acts))
        p.write(turn + 0x10, struct.pack("<QiiQ", data, len(acts), len(acts), _allocator(g)))
    install_turns(g, conf, winner_idx, [turn], map_off)
    return turn, acts


def show_submitted(g, conf, country_idx, map_off=CONF_SUBMIT_MAP):
    p = g.p
    node = _map_find(p, conf, map_off, country_idx)
    if not node:
        return None
    data = p.u64(node + 0x28)
    size = p.i32(node + 0x34)
    out = []
    for i in range(min(size, 16)):
        t = p.u64(data + 8 * i)
        adata = p.u64(t + 0x10)
        asize = p.i32(t + 0x1C)
        acts = []
        for j in range(min(asize, 32)):
            a = p.u64(adata + 8 * j)
            acts.append((p.i32(a + 8), p.i32(a + 0x34), p.i32(a + 0x48)))
        out.append((t, acts))
    return out


def add_demands(g, conf, winner_idx, loser_idx, kinds=('state',), map_off=None):
    """Give the winner's bid turn the engine's own demands for `loser_idx`.

    The builders already return a finished CPdxArray at arg+0x38, so the array
    header is written into the turn instead of growing it with push_back: that
    keeps the engine's own allocator and avoids calling 0x1403371D0 by hand
    (doing so crashed the game on 2026-11-15).

    `map_off` may be one offset or a list; a separate turn (and a separate set
    of action objects) is built for each, so nothing is ever owned twice.
    """
    p = g.p
    alloc = _allocator(g)
    offs = map_off or [CONF_BID_MAP]
    if isinstance(offs, int):
        offs = [offs]
    turns, acts = [], []
    for off in offs:
        got = []
        for kind in kinds:
            for act in build_actions(g, loser_idx, kind):
                if not (0x10000000000 < act < 0x40000000000) or act & 7:
                    continue
                got.append(act)
        turn = new_turn(g)
        if got:
            data = cmdwin.alloc(g, 8 * len(got))
            p.write(data, b"".join(struct.pack("<Q", a) for a in got))
            p.write(turn + 0x10, struct.pack("<QiiQ", data, len(got), len(got), alloc))
        install_turn(g, conf, winner_idx, turn, off)
        turns.append(turn)
        acts.extend(got)
    return turns, acts


if __name__ == "__main__":
    import os
    import sys
    sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
    from game import Game
    g = Game()
    cs = conference(g)
    print("conferences", [hex(c) for c in cs])
    for c in cs:
        p = g.p
        print("  turn map head=%#x size=%d" % (p.u64(c + CONF_TURN_MAP), p.u64(c + CONF_TURN_MAP + 8)))
        print("  ints", [p.i32(c + o) for o in range(0, 0x40, 4)])
