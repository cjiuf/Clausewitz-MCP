"""Combat analysis, using the numbers the game computes itself.

We do not re-implement HOI4's combat formula; any copy would drift from the real
game within a patch. The game already computes, every combat tick, how much damage
each side is dealing, and stores it on the object. We read that and predict the
outcome, which means terrain, equipment, doctrine, air support, the commander,
entrenchment and supply are all already accounted for.

  CLandCombat      +0x28/+0x30  CLandCombatant* (the two sides)
                   +0x38        CProvince   +0x48 CTerrainType (+0x10 name)
  CLandCombatant   +0xC8  front-line divisions (CPdxArray<CArmy*>)
                   +0xE0  reserves          +0xF8 retreating
                   +0x158 ground_damage_str +0x160 ground_damage_org
                   +0x148 air_damage_str    +0x150 air_damage_org
                   +0x138 anti_air_attack   +0x140 air_kills
  CArmy            +0x420 strength(HP) +0x428 organisation +0x430 experience
                   +0x498 days out of supply  +0x1D8 country  +0x3D0 manpower
"""
import struct
import sys

try:
    import offsets as O
except Exception:                      # direct script use
    O = None

# BUG-18 (playtest 2026-11-15): this was the ELF constant, so every read on
# hoi4.exe went to an unmapped address (`OSError [Errno 299] read ...+8`).
# `offsets.py` already resolves to the Windows table on win32.
G_GAMESTATE = getattr(O, "G_GAMESTATE", None) or 0x459E9F0
GS_COMBATS = getattr(O, "GS_COMBATS", None) or 0x268

LC_SIDE_A, LC_SIDE_B = 0x28, 0x30
LC_PROVINCE, LC_TERRAIN = 0x38, 0x48
TERRAIN_NAME = 0x10

CB_FRONT, CB_RESERVES, CB_RETREAT = 0xC8, 0xE0, 0xF8
CB_GROUND_STR, CB_GROUND_ORG = 0x158, 0x160
CB_AIR_STR, CB_AIR_ORG = 0x148, 0x150

A_HANDLE, A_STR, A_ORG, A_XP, A_OOS, A_TAG, A_MANPOWER = 0x18, 0x420, 0x428, 0x430, 0x498, 0x1D8, 0x3D0
PROV_ID = 0xA4


def _fx(p, a):
    return p.i64(a) / 1e5


def _divs(g, combatant, off):
    from game import _arr
    p = g.p
    try:
        return [d for d in _arr(p, combatant, off) if p.i32(d + A_HANDLE) == 51]
    except Exception:
        return []


def _side(g, combatant, tagof):
    p = g.p
    front = _divs(g, combatant, CB_FRONT)
    res = _divs(g, combatant, CB_RESERVES)
    ret = _divs(g, combatant, CB_RETREAT)
    allv = front + res
    org = sum(_fx(p, d + A_ORG) for d in allv)
    hp = sum(_fx(p, d + A_STR) for d in allv)
    return {
        "tags": sorted({tagof.get(p.i32(d + A_TAG)) for d in allv} - {None}),
        "front": len(front), "reserves": len(res), "retreating": len(ret),
        "org": round(org, 1), "hp": round(hp, 1),
        "org_damage_taken_rate": round(_fx(p, combatant + CB_GROUND_ORG), 3),
        "str_damage_taken_rate": round(_fx(p, combatant + CB_GROUND_STR), 3),
        "air_org_damage": round(_fx(p, combatant + CB_AIR_ORG), 3),
        "avg_org": round(org / len(allv), 1) if allv else 0.0,
        "_front_divs": front, "_reserve_divs": res, "_all": allv,
    }


def combats(g, only_mine=True):
    """Active land battles, plus a statistical prediction of the outcome.

    NOTE: `ground_damage_*` accumulates on a side's object as the damage that side
    is TAKING (dealt by the opponent). "Who is winning" is therefore answered by
    asking whose organisation pool runs out first.
    """
    from game import _arr
    p = g.p
    gs = p.u64(p.base + G_GAMESTATE)
    if sys.platform == "win32":
        # `GS_COMBATS` is still a Linux-derived guess on the PE build.  Only
        # believe it when the CPdxArray header at that offset is coherent;
        # otherwise report "no battles" instead of walking a bogus array.
        try:
            data = p.u64(gs + GS_COMBATS)
            cap = p.i32(gs + GS_COMBATS + 8)
            size = p.i32(gs + GS_COMBATS + 12)
            if not (0 <= size <= cap <= 4096) or (size and not data):
                return []
        except Exception:
            return []
    me = g.player_tag()
    tagof = {}
    for t in g.tags():
        try:
            tagof[p.i32(g.country(t).ptr + 8)] = t
        except Exception:
            pass
    out = []
    for c in _arr(p, gs, GS_COMBATS):
        try:
            a = _side(g, p.u64(c + LC_SIDE_A), tagof)
            b = _side(g, p.u64(c + LC_SIDE_B), tagof)
        except Exception:
            continue
        if not a["_all"] and not b["_all"]:
            continue
        mine = me in a["tags"] or me in b["tags"]
        if only_mine and not mine:
            continue
        us, them = (a, b) if me in a["tags"] else (b, a)
        # in battles we are not in, "us/them" just means side A/side B
        pr = p.u64(c + LC_PROVINCE)
        tt = p.u64(c + LC_TERRAIN)
        from game import item_name
        rec = {
            "combat_ptr": hex(c),
            "province": p.i32(pr + PROV_ID) if pr else None,
            "terrain": item_name(p, tt + TERRAIN_NAME) if tt else None,
            "we_are_in_it": mine,
            ("us" if mine else "side_a"): {k: v for k, v in us.items() if not k.startswith("_")},
            ("them" if mine else "side_b"): {k: v for k, v in them.items() if not k.startswith("_")},
        }
        rec.update(verdict(us, them))
        rec["_us"], rec["_them"] = us, them
        out.append(rec)
    return out


MIN_RATE = 0.05      # below this, treat the damage rate as "no data yet"


def verdict(us, them, margin=1.25, our_rate=None, their_rate=None, samples=None,
            min_samples=1, decisive_within=None):
    """Who is winning the organisation race? Time to break, in ticks.

    If `our_rate`/`their_rate` are given, the smoothed (EMA) rates are used; otherwise
    the instantaneous ones. When the damage rates are very low, or there is too little
    with too little evidence the verdict is "unknown" and the battle is LEFT ALONE.
    """
    our_loss_raw = our_rate if our_rate is not None else us["org_damage_taken_rate"]
    their_loss_raw = their_rate if their_rate is not None else them["org_damage_taken_rate"]
    if (our_loss_raw < MIN_RATE or their_loss_raw < MIN_RATE or
            (samples is not None and samples < min_samples)):
        return {"ticks_until_we_break": None, "ticks_until_they_break": None,
                "org_edge": None, "hp_edge": None,
                "width_saturated": us["reserves"] > 0,
                "verdict": "unknown",
                "why_unknown": ("damage rate too low (no combat data yet)"
                                if min(our_loss_raw, their_loss_raw) < MIN_RATE
                                else "not enough samples (%s)" % samples)}
    our_loss = max(our_loss_raw, 1e-6)
    their_loss = max(their_loss_raw, 1e-6)
    ttb_us = us["org"] / our_loss          # how many ticks until we break
    ttb_them = them["org"] / their_loss    # how many ticks until they break
    edge = ttb_us / ttb_them if ttb_them > 0 else float("inf")
    our_hp_ticks = us["hp"] / max(us["str_damage_taken_rate"], 1e-6)
    their_hp_ticks = them["hp"] / max(them["str_damage_taken_rate"], 1e-6)
    if edge >= margin:
        v = "winning"
    elif edge <= 1.0 / margin:
        v = "losing"
    else:
        v = "even"
    if decisive_within and ttb_us > decisive_within and ttb_them > decisive_within:
        v = "stalemate"     # neither side breaks soon -> no hasty decision
    return {
        "ticks_until_we_break": round(ttb_us, 1),
        "ticks_until_they_break": round(ttb_them, 1),
        "org_edge": round(edge, 2),
        "hp_edge": round((our_hp_ticks / their_hp_ticks) if their_hp_ticks else float("inf"), 2),
        "width_saturated": us["reserves"] > 0,     # reserves present means width is full
        "verdict": v,
    }


def report(g):
    """Readable summary; internal pointers are dropped."""
    return [{k: v for k, v in c.items() if not k.startswith("_")} for c in combats(g)]


A_TMPL = 0x3B8
A_STR_DAMAGE, A_ORG_DAMAGE, A_KILLED = 0x448, 0x450, 0x678


def active_division_combats(g, tags=None, limit=80):
    """Fallback for `combats()` returning 0 in civil wars / fresh saves.

    The CGameState+combats pointer is not reliable there. Every division that has
    taken strength or organisation damage is still visible on the CArmy object,
    so report those instead of claiming "no combat".
    """
    from game import CO_ARMIES, _arr, item_name
    p = g.p
    if tags is None:
        tags = [g.player_tag()] + [t for t in enemy_tags(g) if t != g.player_tag()]
    out = []
    for t in tags:
        try:
            c = g.country(t)
        except Exception:
            continue
        for d in _arr(p, c.ptr, CO_ARMIES):
            try:
                sd = _fx(p, d + A_STR_DAMAGE)
                od = _fx(p, d + A_ORG_DAMAGE)
                if sd <= 0.01 and od <= 0.01:
                    continue
                tmpl = p.u64(d + A_TMPL)
                out.append({
                    "tag": t,
                    "division": hex(d),
                    "province": p.i32(p.u64(d + ATTACK_PROV) + PROV_ID)
                                if p.u64(d + ATTACK_PROV) else None,
                    "template": item_name(p, tmpl + 0x20) if tmpl else None,
                    "str": round(_fx(p, d + A_STR), 3),
                    "org": round(_fx(p, d + A_ORG), 1),
                    "str_damage": round(sd, 3),
                    "org_damage": round(od, 3),
                    "killed": p.i32(d + A_KILLED),
                    "out_of_supply_days": p.i32(d + A_OOS),
                })
            except Exception:
                continue
    out.sort(key=lambda r: -(r["org_damage"] + r["str_damage"] * 100))
    return {"count": len(out), "tags": tags, "divisions": out[:limit]}


ATTACK_PROV = 0x1F0     # CArmy -> the CProvince it is in


def we_attack(g, rec):
    """Are we the ATTACKER here? True when our division is not in the battle province."""
    p = g.p
    prov = rec.get("province")
    for d in rec["_us"]["_all"]:
        pr = p.u64(d + ATTACK_PROV)
        if pr and p.i32(pr + PROV_ID) != prov:
            return True
    return False


def neighbours_of(g, province_id, enemy_tags):
    """ENEMY provinces adjacent to the battle province, as pinning targets."""
    import mapdata
    owners, _ = mapdata.province_owner_map(g)
    adj = mapdata.build_adjacency()
    _, ptype = mapdata.load_definitions()
    out = []
    for n in adj.get(province_id, ()):
        if ptype.get(n) == "land" and owners.get(n) in enemy_tags:
            out.append(n)
    return sorted(out)


CO_ARMIES_IN_COMBAT = 0x12F0     # CCountry (from the serializer: num_armies_in_combat)


CO_DIPLOMACY_STATUS = 0xF60      # CCountry -> CDiplomacyStatus
DS_WAR_RELATIONS    = 0x20       # CPdxArray<CWarRelation*>
WR_FIRST, WR_SECOND = 0x0C, 0x10  # country ids of the two sides
WR_INSTIGATOR       = 0x198       # first_was_instigator (bool)
WR_THREAT           = 0x190


def _country_id_map(g):
    """country_id -> TAG. Pure read."""
    p = g.p
    out = {}
    for t in g.tags():
        try:
            out[p.i32(g.country(t).ptr + 8)] = t
        except Exception:
            pass
    return out


# CWarRelation on the 1.19.5 PE build (vtable 0x29DCC08, sizeof 0x1D0).
# Verified live on both wars present in a 1936 save: an Italy/Ethiopia war read
# (16, 44) and a fresh German/Dominican war read (1, 73), with the second copy of
# the pair at +0x78/+0x7C agreeing on both.
WR_WIN_VTABLE = 0x29DCC08
WR_WIN_A, WR_WIN_B = 0x78, 0x7C
WR_WIN_A2, WR_WIN_B2 = 0x10, 0x14


def _war_relations_win(g, me_tag, refresh=False):
    """Windows: read the war table by scanning live CWarRelation objects.

    The object index is a memory snapshot, so it cannot see a war declared after
    it was built -- callers that just declared one must pass `refresh=True`
    (a full rescan costs ~25 s).
    """
    p = g.p
    oi = g.oi
    oi.build(force=refresh)
    try:
        me_id = p.i32(g.country(me_tag).ptr + 8)
    except Exception:
        return []
    id2tag = _country_id_map(g)
    out = []
    for a in oi.instances("CWarRelation"):
        try:
            atk = p.i32(a + WR_WIN_A) or p.i32(a + WR_WIN_A2)
            dfd = p.i32(a + WR_WIN_B) or p.i32(a + WR_WIN_B2)
        except Exception:
            continue
        if not atk or not dfd or atk == dfd:
            continue
        if atk == me_id:
            enemy_id = dfd
        elif dfd == me_id:
            enemy_id = atk
        else:
            continue
        enemy = id2tag.get(enemy_id)
        if not enemy or enemy == me_tag:
            continue
        out.append({"enemy": enemy, "me": me_tag, "threat": None, "_ptr": a})
    return out


def war_relations(g, tag=None, refresh=False):
    """Wars the country is IN: PURE READS, no injected calls.

    On Windows this scans live `CWarRelation` objects (see `_war_relations_win`);
    `refresh=True` forces the object index to be rebuilt, which is required to
    see a war declared since the last scan.

    On the ELF build: CCountry+0xF60 (CDiplomacyStatus) +0x20 -> array of
    CWarRelation*.

    The enemy is NOT read from an offset. The side fields inside CWarRelation mix
    two different id spaces (a CCountry+0x08 id in one place, an index into the
    country array in another). For countries created mid-game by a civil war the
    two diverge, which produced a wrong or empty enemy list. Object identity is
    used instead: whichever countries have the SAME CWarRelation pointer in their
    war list are the two sides. Offset-independent and exact.

    This also sees wars that have been declared but have not yet made contact,
    which `combats()` cannot.
    """
    from game import _arr
    p = g.p
    me_tag = tag or g.player_tag()
    if sys.platform == "win32":
        return _war_relations_win(g, me_tag, refresh=refresh)
    try:
        c = g.country(me_tag)
        mine = _arr(p, p.u64(c.ptr + CO_DIPLOMACY_STATUS), DS_WAR_RELATIONS)
    except Exception:
        return []
    if not mine:
        return []

    # 2026-11-04: CDiplomacyStatus+0x20 also holds non-war relations in 1.19.4.1
    # (trade agreements, opinion modifiers). Germany and Romania share a Romanian
    # oil-trade object, which is exactly how `hoi4_wartime_status` reported
    # "at war with ROM" for four years of peace. Only accept an element whose RTTI
    # class actually is a war relation. If RTTI cannot name any of them, the list
    # is only believed when the battle counter proves that fighting is happening.
    keep, unknown = [], 0
    for wr in mine:
        try:
            cls = g.oi.class_of(wr) or ""
        except Exception:
            cls = ""
        if not cls:
            unknown += 1
            keep.append(wr)
        elif "War" in cls:
            keep.append(wr)
    if unknown == len(mine):
        try:
            fighting = p.i32(c.ptr + CO_ARMIES_IN_COMBAT) > 0 or bool(combats(g, only_mine=True))
        except Exception:
            fighting = False
        if not fighting:
            return []
    mine = keep
    if not mine:
        return []
    want = set(mine)
    sides = {}
    for t in g.tags():
        try:
            d = p.u64(g.country(t).ptr + CO_DIPLOMACY_STATUS)
            for wr in _arr(p, d, DS_WAR_RELATIONS):
                if wr in want:
                    sides.setdefault(wr, set()).add(t)
        except Exception:
            continue
    out = []
    for wr in mine:
        others = sorted(sides.get(wr, set()) - {me_tag})
        if not others:
            continue
        try:
            threat = round(p.i64(wr + WR_THREAT) / 1e5, 3)
        except Exception:
            threat = None
        for enemy in others:
            out.append({"enemy": enemy, "me": me_tag, "threat": threat, "_ptr": wr})
    if out:
        return out
    # FALLBACK: in 1.19.4.1 the diplomacy records of a country that is AT WAR can
    # be empty, so the enemy cannot be found that way. Find it from the armies
    # instead: every CArmy carries its owner's country pointer, and the armies
    # that are in combat belong to the enemy.
    return _enemies_from_armies(g, me_tag)


def _enemies_from_armies(g, me_tag):
    """Enemy tags of the countries whose armies are FIGHTING OURS.

    `num_armies_in_combat` is a WORLD counter, so it cannot be used on its own.
    The reliable signal is the battle list: a CLandCombat has two sides, and
    each side's divisions carry their owner's country pointer. A battle with one
    of our divisions on one side and theirs on the other names the enemy.
    """
    p = g.p
    A_COUNTRY = 0x1D8
    try:
        from game import _arr
        from game import item_name
    except Exception:
        return []
    # tag for a country pointer
    ptr2tag = {}
    for t in g.tags():
        try:
            ptr2tag[g.country(t).ptr] = t
        except Exception:
            continue
    enemies = []
    try:
        recs = combats(g, only_mine=True)
    except Exception:
        recs = []
    for rec in recs:
        try:
            sides = rec.get("_us", {}), rec.get("_them", {})
        except Exception:
            continue
        for side in sides:
            for d in side.get("_all", []):
                cp = p.u64(d + A_COUNTRY)
                tag = ptr2tag.get(cp)
                if tag and tag != me_tag and tag not in enemies:
                    enemies.append(tag)
    if enemies:
        return [{"enemy": t, "me": me_tag, "threat": None, "_ptr": None} for t in enemies]
    # The battle list can be empty (CGameState+combats is unreliable in some
    # saves), so go by geography: the enemy is the country that owns armies in
    # the provinces where OUR armies are.
    try:
        from game import _arr
        our_provs = {}
        for a in _arr(p, g.country(me_tag).ptr, 0x290):
            try:
                pr = p.u64(a + 0x1F0)
                if pr:
                    our_provs.setdefault(p.i32(pr + 0xA4), []).append(a)
            except Exception:
                continue
        if not our_provs:
            return []
        for t in g.tags():
            if t == me_tag:
                continue
            try:
                c = g.country(t)
                armies = _arr(p, c.ptr, 0x290)
            except Exception:
                continue
            if not armies:
                continue
            for a in armies:
                try:
                    pr = p.u64(a + 0x1F0)
                    if pr and p.i32(pr + 0xA4) in our_provs:
                        enemies.append(t)
                        break
                except Exception:
                    continue
    except Exception:
        pass
    if enemies:
        return [{"enemy": t, "me": me_tag, "threat": None, "_ptr": None} for t in enemies]
    # Fall back to the world counter: any country with armies in combat.
    try:
        from game import _arr
        mine = {p.u64(a + A_COUNTRY)
                for a in _arr(p, g.country(me_tag).ptr, 0x290)}
    except Exception:
        return []
    for t in g.tags():
        if t == me_tag:
            continue
        try:
            c = g.country(t)
            if p.i32(c.ptr + 0x12F0) <= 0:
                continue
            armies = _arr(p, c.ptr, 0x290)
        except Exception:
            continue
        for a in armies:
            if p.u64(a + A_COUNTRY) in mine:
                enemies.append(t)
                break
    return [{"enemy": t, "me": me_tag, "threat": None, "_ptr": None} for t in enemies]


def at_war(g, tag=None):
    """PURE-READ war detection.

    In order: (1) war relations from the diplomacy records, which also catch wars
    declared but not yet fought; (2) num_armies_in_combat; (3) the active battle list.

    DO NOT use `g.wars()` here: it goes through `country_stats()`, which stops every
    game thread and runs ~45 functions inside the process. Called from a loop it
    FREEZES the game (see FINDINGS, the freeze post-mortem).
    """
    p = g.p
    try:
        if war_relations(g, tag):
            return True
    except Exception:
        pass
    try:
        c = g.country(tag) if tag else g.player()
        if p.i32(c.ptr + CO_ARMIES_IN_COMBAT) > 0:
            return True
    except Exception:
        pass
    try:
        return bool(combats(g, only_mine=True))
    except Exception:
        return False


def enemy_tags(g, tag=None, refresh=False):
    """Tags of the countries we are at war with: pure reads."""
    return sorted({w["enemy"] for w in war_relations(g, tag, refresh=refresh)})
