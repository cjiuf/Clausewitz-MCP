"""
The macro game layer. What a player actually interacts with.

No cheats: reads come straight from memory, and actions go through the game's own
CCommand queue, the same path a mouse click takes. Eligibility is checked by
calling the game's own functions.
"""
from __future__ import annotations
import os, struct, sys, time
from typing import Optional

from api import HOI4, Country
from commands import CommandLayer
from explore import cstring
import offsets as O
if sys.platform == "win32":
    # MSVC RTTI (PE) instead of Itanium RTTI (ELF); same {"vtables": ...} shape.
    import rtti_win as R
else:
    import rtti as R
from objindex import ObjectIndex

HERE = os.path.dirname(os.path.abspath(__file__))

# ------------------------------------------------------------------ offsets
# CCountry subsystems
# Every one of these is a pointer to a status sub-object, and the Windows build
# moved them (0xF38 -> 0xF60 etc), so prefer the verified Windows table.
CO_TECH_STATUS      = getattr(O, "CO_TECHNOLOGY_STATUS", 0x0F38)   # CTechnologyStatus*
CO_PRODUCTION       = getattr(O, "CO_PRODUCTION_STATUS", 0x0F40)   # CProductionStatus*
CO_ARMY_UPGRADES    = getattr(O, "CO_ARMY_UPGRADES_STATUS", 0x0F58)
CO_DIPLOMACY        = getattr(O, "CO_DIPLOMACY_STATUS", 0x0F60)    # CDiplomacyStatus*
CO_POLITICS         = getattr(O, "CO_POLITICAL_STATUS", 0x0F68)    # CPoliticalStatus*
CO_DECISIONS        = getattr(O, "CO_DECISION_STATUS", 0x0F78)     # CDecisionStatus*
CO_INTEL_AGENCY     = getattr(O, "CO_INTELLIGENCE_AGENCY", 0x0F98)
CO_COUNTRY_INTEL    = getattr(O, "CO_COUNTRY_INTEL", 0x0FC0)
CO_FOCUS_TREE       = getattr(O, "CO_FOCUS_TREE", 0x1338)   # CNationalFocusTree*
CO_CONT_FOCUS_PAL   = getattr(O, "CO_CONT_FOCUS_PAL", 0x1340)
CO_FOCUS_PROGRESS   = getattr(O, "CO_FOCUS_PROGRESS", 0x1348)   # CNationalFocusProgress*
CO_FACTION          = 0x2B00
CO_STATES           = 0x460   # CPdxArray<CState*>, owned states
CO_CAPITAL_STATE    = 0xFF0   # int32 state id of the capital (NOT a province id)

# CNationalFocusTree
FT_FOCUSES          = 0x30     # CPdxArray<CNationalFocus*>

# CNationalFocus
NF_ID               = 0x08
NF_KEY              = 0x18     # CString
NF_LOCKEY           = 0x38
NF_NAME             = 0x60     # CString (localised)
NF_ICON             = 0x88
NF_AVAIL_TRIGGER_N  = 0x2DC
NF_PREREQ           = 0x568    # CPdxArray<CNationalFocusDependency*>
NF_MUTEX            = 0x580
NF_FILTERS          = 0x5D8

# CNationalFocusProgress
FP_COUNTRY          = 0x08
FP_CURRENT          = 0x10     # CNationalFocus* (0 = no focus selected)
FP_COMPLETED        = 0x40     # CPdxArray (1.19.4.1; was 0x20)
FP_ROOT_FOCUSES     = 0x70     # CPdxArray, the tree's root focuses (not 'selectable')
FP_DAILY            = 0x38

# CTechnologyStatus
TS_RESEARCHED       = 0x40     # CPdxArray<CTechnology*>
TS_ALL_TECHS        = 0x88     # CPdxArray<CTechnology*>
TS_SLOTS            = 0xA0     # CPdxArray<CResearchSlot*>
TS_PRODUCTION_LINES = 0x2B8

# CResearchSlot  (0x48 bytes; ctor 0xF4F4F0, dtor frees 0x48)
# The three fields below are exactly the ones `CResearchSlot::Save` (0xF61BF0)
# writes, with the engine's own member tags: +0x20 -> 0x31DE, +0x28 -> 0x34BB,
# +0x38 -> 0x3684.  They do NOT hold the live progress of the current
# technology: +0x20 is the progress carried over when a slot switches tech and
# +0x28 is the required amount computed at that moment (both stay 0/constant
# while a tech runs).  The bar the UI draws comes from the CTechnology object
# (TE_PROGRESS) against the template cost.
RS_OWNER            = 0x10
RS_CURRENT          = 0x18     # CTechnology* (0 = empty)
RS_PROGRESS         = 0x20     # carried-over progress, 1e5 fixed
RS_REQUIRED         = 0x28     # required for the carried-over progress, 1e5
RS_SPEED            = 0x38     # research speed, 1e5 fixed (1.01 = +1%/day)
RS_INDEX            = 0x40     # int32 slot index

# CTechnology (per-country state object)
TE_ID               = 0x08
TE_TEMPLATE         = 0x160    # CTechnologyTemplate* (vtable 0x29B9F10)
TE_PROGRESS         = 0x198    # int64 research days x1e5, speed-weighted
# CTechnologyTemplate (global definition)
TT_NAME             = getattr(O, "TT_NAME", 0x08)   # CString / std::string
TT_ID               = getattr(O, "TT_ID", 0x34)
TT_YEAR             = 0x3D8    # int32 start year (advanced_ship_hull_light -> 1943)
TT_RESEARCH_COST    = 0x3E0    # int32, days x1000 (150000 = 150 days)

# CProductionStatus
PS_LINES            = 0x58     # CPdxArray<CMilitaryProductionLine*/CNavalProductionLine*>
PS_MIL_LINES        = 0x58     # CPdxArray<CMilitaryProductionLine*>
PS_CONSTRUCTION     = 0x70     # CPdxArray<CBuildingProductionLine*>, construction queue
PS_VARIANTS_ALL     = 0xA0
PS_VARIANTS_ACTIVE  = 0xB8

# CMilitaryProductionLine / CNavalProductionLine
ML_OWNER            = 0x20
ML_PRIORITY         = 0x10     # int32
ML_FACTORIES        = 0x14     # int32, assigned factory count
ML_UNIT_COST        = 0x28     # int64 x1e5, unit production cost (IC)
ML_OUTPUT           = 0x30     # int64 x1e5, daily output
ML_EFF_COST         = 0x38     # int64 x1e5, modified cost
ML_MAX_EFFICIENCY   = 0xC8     # int32, maximum efficiency (%)
ML_VARIANT          = 0x88     # CEquipmentVariant*
ML_TOTAL_PRODUCED   = 0xF8

# CBuildingProductionLine (construction)
BL_OWNER            = 0x20
BL_PROGRESS         = 0x38     # int64 accumulated IC-days (1.19.5). Verified
                              # 2026-11-15 by diffing a live line over 25 s: only
                              # this field moved (1466.56 -> 1486.36 of 10800).
BL_COST             = 0x28     # int64 total cost in IC-days (10800 for a civ factory)
BL_BUILDING         = 0x78     # CBuilding* (1.19.4.1; was 0x70 in 1.19.2)

# CEquipmentVariant
EV_TYPE_ID          = 0x14     # int32 -> CEquipmentType id
# CEquipmentType
ET_ID               = 0x08
ET_NAME             = 0x10     # CString
# CBuildingTemplate
BT_ID               = 0x08
BT_NAME             = getattr(O, "BT_NAME", 0x1E8)   # display name (None if the build has none)
BT_KEY              = getattr(O, "BT_KEY", 0x248)    # key ("industrial_complex")
# CBuilding (example)
BG_TYPE_ID          = 0x08
# CState
ST_ID               = getattr(O, "ST_ID", 0x48)   # int32, state id
# CBuildingReference { vptr; int32 state; int32 building_type; int32 province; }
BUILDING_REF_VTABLE = 0x4348A90   # CBuildingReference vtable, 1.19.4.1

# CPoliticalStatus
PL_PARTIES          = 0x20     # CPdxArray<CPoliticalParty*>
PL_ALL_IDEAS        = 0x38     # CPdxArray<CIdea*>  (every idea valid for the country)
PL_ACTIVE_IDEAS     = 0x50     # CPdxArray<CIdea*>, active spirits, laws and advisors
PL_RULING           = 0xD0
PL_PP               = 0xE0

# CDecisionStatus
DS_ALL              = 0x10     # CPdxArray<CDecision*>
DS_ACTIVE           = 0x88
DS_VISIBLE          = 0xD0
DS_TARGETED         = 0xE8

# CDiplomacyStatus
DP_RELATIONS        = 0x08     # CPdxArray<CRelationStatus*>  (one per country)

# item name offsets
IDEA_NAME           = 0x18
DECISION_NAME       = 0x118

# the game's own check functions
FN_GET_TECH_STATUS  = 0xD1FE30   # CTechnologyStatus* CCountry::GetTechnologyStatus()
FN_CAN_RESEARCH     = None  # 1.19.2 adresi (0x11CBF10) bu build'de gecersiz:
                            # cagirmak oyunu SIGSEGV ile dusuruyor. Yeniden bulunana kadar None.


def item_name(p, addr):
    """Name fields are sometimes CString and sometimes std::string. Try both."""
    return cstring(p, addr) or p.stdstring(addr) or None


def _arr(p, obj, off, limit=100000):
    """CPdxArray<T*> -> list of pointers.

    `n` comes straight out of game memory. If the caller passes the wrong offset
    (or a stale object after a reload) n can be a garbage int32 and `p.read(d, 8*n)`
    asks for gigabytes: that is what OOM-killed the MCP child with 11 GB RSS during
    a long housekeep loop. Anything larger than `limit` is treated as "not an array
    we understand" and ignored.
    """
    try:
        n = p.i32(obj + off + O.PDXARRAY_SIZE)
        d = p.u64(obj + off + O.PDXARRAY_DATA)
        if n <= 0 or not d or n > limit or d < 0x10000:
            return []
        return list(struct.unpack("<%dQ" % n, p.read(d, 8 * n)))
    except OSError:
        # wrong offset / not an array of pointers: report empty instead of dying
        return []


def _count(p, obj, off):
    return p.i32(obj + off + O.PDXARRAY_SIZE)


import legality


class Game(HOI4):
    """The HOI4 handle plus the macro game mechanics."""

    def __init__(self, pid=None):
        super().__init__(pid)
        self.cmd = CommandLayer(self)
        self._oi = None
        self._tech_by_name = None
        self._tech_by_id = None
        self._idea_by_name = None
        self._decision_by_name = None

    # ------------------------------------------------------------ plumbing
    @property
    def oi(self) -> ObjectIndex:
        # 2026-11-06: the index holds absolute heap addresses. When the game is
        # restarted (crash reload, fresh campaign) and the MCP process survives, the
        # cached index still points at the OLD process: CTechnologyDatabase /
        # CBuildingDatabase resolved to None and every research/construction call
        # died with `TypeError: unsupported operand type(s) for +: 'NoneType' and 'int'`.
        pid = None
        try:
            pid = self.p.pid
        except Exception:
            pass
        if self._oi is None or getattr(self, "_oi_pid", None) != pid:
            self._oi = ObjectIndex(self.p, R.build()["vtables"],
                                   os.path.join(HERE, "objindex.pkl"))
            self._oi.build()
            self._oi_pid = pid
            # per-process caches derived from the index
            self._tech_by_name = None
            self._tech_by_id = None
        return self._oi

    def rescan_objects(self):
        self.oi.build(force=True)

    @staticmethod
    def _settle(seconds=0.25):
        """Short wait so the game can process a command after it is queued."""
        time.sleep(seconds)

    def _c(self, tag) -> Country:
        return self.player() if tag in (None, "", "player") else self.country(tag)

    # ============================================================= FOCUSES
    def focus_tree(self, tag=None):
        c = self._c(tag)
        tree = self.p.u64(c.ptr + CO_FOCUS_TREE)
        return _arr(self.p, tree, FT_FOCUSES) if tree else []

    def _focus_info(self, f):
        p = self.p
        return {
            "key": item_name(p, f + NF_KEY),
            "name": item_name(p, f + NF_NAME),
            "id": p.i32(f + NF_ID),
            "ptr": f,
            "prerequisites": _count(p, f, NF_PREREQ),
            "mutually_exclusive": _count(p, f, NF_MUTEX),
        }

    def _focus_prereq_groups(self, f):
        """Each prerequisite block is an OR-group of focus keys; all blocks are AND."""
        p = self.p
        out = []
        for d in _arr(p, f, NF_PREREQ):
            group = []
            for ff in _arr(p, d, 0x08):
                k = item_name(p, ff + NF_KEY)
                if k:
                    group.append(k)
            out.append(group)
        return out

    def _focus_mutex_keys(self, f):
        p = self.p
        out = []
        for x in _arr(p, f, NF_MUTEX):
            k = item_name(p, x + NF_KEY)
            if k:
                out.append(k)
            else:
                for y in _arr(p, x, 0x08):
                    kk = item_name(p, y + NF_KEY)
                    if kk:
                        out.append(kk)
        return out

    def focuses(self, tag=None, only_startable=False):
        out = []
        for f in self.focus_tree(tag):
            info = self._focus_info(f)
            if only_startable and info["prerequisites"]:
                continue
            out.append(info)
        return out

    def _wait_until(self, fn, timeout=4.0, step=0.25):
        """Komut kuyruga dusup islenene kadar bekle (Post asenkron)."""
        import time as _t
        end = _t.time() + timeout
        while _t.time() < end:
            try:
                if fn():
                    return True
            except Exception:
                pass
            _t.sleep(step)
        return False

    def current_focus(self, tag=None):
        c = self._c(tag)
        prog = self.p.u64(c.ptr + CO_FOCUS_PROGRESS)
        if not prog:
            return None
        cur = self.p.u64(prog + FP_CURRENT)
        if not cur:
            return None
        info = self._focus_info(cur)
        info["completed_count"] = _count(self.p, prog, FP_COMPLETED)
        return info

    def find_focus(self, key, tag=None):
        for f in self.focus_tree(tag):
            if item_name(self.p, f + NF_KEY) == key:
                return f
        return None

    def set_focus(self, key, tag=None):
        c = self._c(tag)
        f = self.find_focus(key, tag)
        if not f:
            raise KeyError("focus not found: %s" % key)
        if sys.platform == "win32":
            import cmdwin
            r = cmdwin.set_national_focus(self, f, c.tag_id)
            self._settle()
            prog = self.p.u64(c.ptr + CO_FOCUS_PROGRESS)
            got = self.p.u64(prog + FP_CURRENT) if prog else 0
            r.update({"focus": key, "current": hex(got) if got else None,
                      "ok": bool(r.get("ok")) and got == f})
            return r
        with self:
            self.cmd.send("SetNationalFocus", tag=c.tag_id, focus_ptr=f)
        self._settle()

        def _ok():
            prog = self.p.u64(c.ptr + CO_FOCUS_PROGRESS)
            return bool(prog) and self.p.u64(prog + FP_CURRENT) == f
        ok = self._wait_until(_ok)
        return {"ok": ok, "focus": key}

    def set_focus_ptr(self, focus_ptr, tag=None):
        """Start a national focus by its LIVE tree pointer.

        hoi4_selectable_focuses returns pointers read straight out of the running focus
        tree, while hoi4_set_focus resolves the key through a cached lookup that goes
        stale after a game restart. For focuses the game lists as selectable but
        set_focus refuses (Anschluss / inner-circle focuses), post the pointer directly.
        """
        c = self._c(tag)
        f = int(focus_ptr)
        if sys.platform == "win32":
            import cmdwin
            r = cmdwin.set_national_focus(self, f, c.tag_id)
            self._settle()
            prog = self.p.u64(c.ptr + CO_FOCUS_PROGRESS)
            got = self.p.u64(prog + FP_CURRENT) if prog else 0
            r.update({"focus_ptr": hex(f), "current": hex(got) if got else None,
                      "ok": bool(r.get("ok")) and got == f})
            return r
        with self:
            self.cmd.send("SetNationalFocus", tag=c.tag_id, focus_ptr=f)
        self._settle()

        def _ok():
            prog = self.p.u64(c.ptr + CO_FOCUS_PROGRESS)
            return bool(prog) and self.p.u64(prog + FP_CURRENT) == f
        ok = self._wait_until(_ok)
        return {"ok": ok, "focus_ptr": hex(f), "focus": self.p.u64(self.p.u64(c.ptr + CO_FOCUS_PROGRESS) + FP_CURRENT) == f}

    def drop_focus(self, tag=None):
        c = self._c(tag)
        if sys.platform == "win32":
            import cmdwin
            r = cmdwin.drop_national_focus(self, c.tag_id)
            self._settle()
            prog = self.p.u64(c.ptr + CO_FOCUS_PROGRESS)
            got = self.p.u64(prog + FP_CURRENT) if prog else 0
            r.update({"current": hex(got) if got else None,
                      "ok": bool(r.get("ok")) and got == 0})
            return r
        with self:
            self.cmd.send("DropCurrentNationalFocus", tag=c.tag_id)
        self._settle()

        def _ok():
            prog = self.p.u64(c.ptr + CO_FOCUS_PROGRESS)
            return bool(prog) and self.p.u64(prog + FP_CURRENT) == 0
        return {"ok": self._wait_until(_ok)}

    # ========================================================== RESEARCH
    def _load_tech_db(self):
        if self._tech_by_name is not None:
            return
        p = self.p
        db = self.oi.singleton("CTechnologyDatabase")
        if not db:
            self._oi = None
            db = self.oi.singleton("CTechnologyDatabase")
        if not db:
            # Built too early (main menu / campaign still loading): do NOT cache an
            # empty table, otherwise research stays dead for the whole process.
            self._tech_by_name = None
            self._tech_by_id = {}
            self._oi = None
            return
        byname, byid = {}, {}
        for off in (0x48, 0x78, 0xD8):
            for t in _arr(p, db, off):
                nm = item_name(p, t + TT_NAME)
                if nm:
                    byname[nm] = t
                    byid[p.i32(t + TT_ID)] = nm
        if not byname:
            # The database object exists but is still empty (it is filled after the
            # campaign finishes loading).  Caching {} here made every later lookup
            # come back empty for the rest of the process -- hoi4_research_available
            # then reported count 0 for good.
            self._tech_by_name = None
            self._tech_by_id = {}
            self._oi = None
            return
        self._tech_by_name, self._tech_by_id = byname, byid

    def tech_name(self, tech_obj):
        """CTechnology* (country object) -> technology name"""
        self._load_tech_db()
        return self._tech_by_id.get(self.p.i32(tech_obj + TE_ID))

    def tech_status(self, tag=None):
        return self.p.u64(self._c(tag).ptr + CO_TECH_STATUS)

    def tech_cost_days(self, tech_name):
        """Base research time of a technology, in days.

        `CTechnologyTemplate+0x3E0` holds days x1000 on the PE build (verified:
        basic_machine_tools 150000 -> "130日"-class values, synth_oil_experiments
        300000, improved_light_tank_chassis 200000).  The old 0x3D0 is not the
        cost: it reads -1 for most techs (0x3D8 is the start year).
        """
        self._load_tech_db()
        t = self._tech_by_name.get(tech_name)
        if not t:
            return None
        raw = self.p.i32(t + TT_RESEARCH_COST)
        if not (0 < raw < 100000 * 1000):
            return None
        return round(raw / 1000.0, 1)

    def tech_progress(self, tech_obj):
        """Research days already invested in a CTechnology, speed-weighted."""
        if not tech_obj:
            return 0.0
        return self.p.i64(tech_obj + TE_PROGRESS) / float(O.FIXED)

    def research_slots(self, tag=None):
        """What each research slot is doing, with the UI's own numbers.

        Progress lives on the technology, not on the slot:
            progress_days = CTechnology+0x198 / 1e5
            cost_days     = CTechnologyTemplate+0x3E0 / 1000
            speed         = CResearchSlot+0x38 / 1e5   (days gained per day)
        Verified two ways on 1.19.5.0 (2026-11-15):
          * every tech the country starts with shows progress == cost * 1.1
            (56/56 at exactly 1.1, the start-of-game research speed);
          * a running tech gains exactly `speed` x 1e5 per daily tick
            (basic_machine_tools 3535000 -> 3636000 -> ... , speed 101000).
        """
        p = self.p
        ts = self.tech_status(tag)
        out = []
        for i, s in enumerate(_arr(p, ts, TS_SLOTS)):
            cur = p.u64(s + RS_CURRENT)
            nm = self.tech_name(cur) if cur else None
            speed = p.i64(s + RS_SPEED) / float(O.FIXED) or 1.0
            prog_days = self.tech_progress(cur) if cur else 0.0
            cost_days = self.tech_cost_days(nm) if nm else None
            pct = round(100.0 * prog_days / cost_days, 2) if cost_days else 0.0
            rec = {
                "slot": i,
                "researching": nm,
                "progress": round(prog_days, 3),          # days of research done
                "required": round(cost_days, 3) if cost_days else 0.0,  # days to do
                "percent": min(pct, 100.0),
                "speed": speed,
                "base_days": cost_days,
            }
            if cost_days:
                rec["days_remaining"] = round(max(cost_days - prog_days, 0.0) / speed, 1)
            out.append(rec)
        return out

    def researched(self, tag=None):
        return sorted(filter(None, (self.tech_name(t)
                                    for t in _arr(self.p, self.tech_status(tag), TS_RESEARCHED))))

    def available_research(self, tag=None):
        """Technologies researchable by the static tree, with ZERO injected calls.

        Pure memory read for the live state (researched set + occupied slots) and
        common/technologies/*.txt for start_year / prerequisites.  The final
        legality check is still done by the game's own CanExecute inside RUN_CMD
        when hoi4_start_research posts SetResearch.
        """
        import re, techdata
        defs = techdata.load()
        current_year = self.date()["year"]
        max_year = current_year + 1
        researched = set(self.researched(tag))
        active = {s["researching"] for s in self.research_slots(tag) if s.get("researching")}
        my_tag = self._c(tag).tag
        order = {"infantry": 0, "industry": 1, "computing": 2,
                 "armor": 3, "air": 4, "artillery": 5,
                 "support": 6, "naval": 7}
        rows = []
        for nm in self._tech_by_name:
            d = defs.get(nm)
            if not d or d.get("category") not in order:
                continue
            if nm in researched or nm in active:
                continue
            if d.get("always_no"):
                continue
            # Focus-granted modifier techs have no folder; they are not in the tree.
            if not d.get("folder"):
                continue
            allow_tags = d.get("allow_tags") or []
            if allow_tags and my_tag not in allow_tags:
                continue
            # GER_xxx is ours; any other uppercase tag prefix is foreign content.
            m = re.match(r"^([A-Z][A-Za-z0-9]{1,})_", nm)
            if m and m.group(1) != my_tag:
                continue
            if int(d.get("year") or 1936) > max_year:
                continue
            parents = d.get("parents") or []
            if parents and not any(par in researched for par in parents):
                continue
            year = int(d.get("year") or 1936)
            score = techdata.score_tech(nm, d.get("folder", ""), d.get("categories", []),
                                        year, current_year, tag=my_tag)
            rows.append((score, year, nm))
        rows.sort(key=lambda r: (-r[0], r[1], r[2]))
        return [nm for _, _, nm in rows]

    def start_research(self, tech_name, slot=None, tag=None):
        self._load_tech_db()
        c = self._c(tag)
        tmpl = self._tech_by_name.get(tech_name)
        if not tmpl:
            raise KeyError("no such technology: %s" % tech_name)
        if slot is None:
            slots = self.research_slots(tag)
            free = [s["slot"] for s in slots if s["researching"] is None]
            if not free:
                return {"ok": False, "error": "no free research slot"}
            slot = free[0]
        if sys.platform == "win32":
            # Real command channel on the PE build: hand-built CSetResearchCommand
            # + Post(session, cmd). See calling/cmdwin.py.
            import cmdwin
            cmdwin.set_research(self, tmpl, slot, c.tag_id)
            self._settle()
            after = self.research_slots(tag)
            return {"ok": after[slot]["researching"] == tech_name, "slot": slot,
                    "researching": after[slot]["researching"]}
        with self:
            if FN_CAN_RESEARCH is not None and \
                    not self.call(FN_CAN_RESEARCH, (self.tech_status(tag), tmpl, 0)) & 0xFF:
                return {"ok": False, "error": "%s su an arastirilamaz" % tech_name}
            self.cmd.send("SetResearch", tag=c.tag_id, tech_ptr=tmpl, slot=slot, flag=0)
        self._settle()

        def _ok():
            return self.research_slots(tag)[slot]["researching"] == tech_name
        ok = self._wait_until(_ok)
        after = self.research_slots(tag)
        return {"ok": ok, "slot": slot, "researching": after[slot]["researching"]}

    def fill_all_research_slots(self, preferred_techs=None, tag=None, max_attempts=30):
        """Fill EVERY empty research slot, trying preferred techs first then the
        score-sorted available list.  After each command the slot memory is read
        back; a refused tech is skipped instead of being reported as success."""
        slots = self.research_slots(tag)
        empty = [s["slot"] for s in slots if s.get("researching") is None]
        empty_before = list(empty)
        attempts = []
        preferred = [str(x) for x in (preferred_techs or []) if x]
        attempted = set()
        if not empty:
            return {"ok": True, "empty_before": [], "empty_after": [],
                    "filled": 0, "slots": slots, "attempts": attempts}
        candidates = preferred + [x for x in self.available_research(tag) if x not in preferred]
        for slot in list(empty):
            for tech in candidates:
                if tech in attempted:
                    continue
                attempted.add(tech)
                try:
                    res = self.start_research(tech, slot=slot, tag=tag)
                except Exception as e:
                    res = {"ok": False, "error": str(e)}
                attempts.append({"slot": slot, "tech": tech, "ok": bool(res.get("ok")),
                                 "error": res.get("error")})
                if res.get("ok"):
                    break
                if len(attempts) >= max_attempts:
                    break
            if len(attempts) >= max_attempts:
                break
        after = self.research_slots(tag)
        empty_after = [s["slot"] for s in after if s.get("researching") is None]
        return {"ok": not empty_after, "empty_before": empty_before,
                "empty_after": empty_after, "filled": len(empty_before) - len(empty_after),
                "slots": after, "attempts": attempts}

    # =========================================================== DECISIONS
    def _run_cmd(self, cmd):
        """CCommand::Run (CanExecute -> Post) with a valid libstdc++ std::string sink.

        Five call sites used to do `errbuf = malloc(0x20); write(b"\\0"*0x20)`. That
        leaves _M_p = NULL, so the moment CanExecute wrote a rejection message the
        game either dereferenced NULL or freed a wild pointer — the "law/decision
        randomly refused or crashed" bug. One helper, one correct SSO string.
        """
        if sys.platform == "win32":
            # PE: 0x20AE0A0 is a Linux VA and the error sink is a libstdc++
            # std::string.  `cmdwin.enqueue` stamps the session tick/seq and hands
            # the object to the queue, which is the path every working Windows
            # command (focus / research / construction / war) takes.
            import cmdwin
            rv = cmdwin.enqueue(self, cmd)
            self._last_cmd_error = ""
            return rv
        s = self.malloc(0x20)
        self.p.write(s, struct.pack("<QQ", s + 0x10, 0) + b"\0" * 16)
        try:
            rv = self.call(0x20AE0A0, (cmd, s))
            self._last_cmd_error = self._read_stdstring_at(s)
        finally:
            try:
                ptr = self.p.u64(s)
                if ptr and ptr != s + 0x10:
                    self.free(ptr)
                self.free(s)
            except Exception:
                pass
        return rv

    def _read_stdstring_at(self, s):
        try:
            ptr, ln = self.p.u64(s), self.p.u64(s + 8)
            if ptr and 0 < ln < 512:
                return (self.p.try_read(ptr, ln) or b"").decode("utf-8", "replace")
        except Exception:
            pass
        return ""

    def _decision_name(self, d):
        return item_name(self.p, d + DECISION_NAME)

    def decisions(self, tag=None, which="visible"):
        off = {"all": DS_ALL, "active": DS_ACTIVE, "visible": DS_VISIBLE}[which]
        ds = self.p.u64(self._c(tag).ptr + CO_DECISIONS)
        out = []
        for d in _arr(self.p, ds, off):
            nm = self._decision_name(d)
            if nm:
                out.append({"key": nm, "ptr": d})
        return out

    def decision_definitions(self):
        """key -> CDecision* from the game's own CDecisionDatabase (4187 entries)."""
        if getattr(self, "_dec_defs", None) is not None:
            return self._dec_defs
        p = self.p
        db = self.oi.singleton("CDecisionDatabase")
        out = {}
        for it in _arr(p, db, 0x28):
            nm = item_name(p, it + DECISION_NAME)
            if nm:
                out[nm] = it
        self._dec_defs = out
        return out

    def select_decision(self, key, tag=None):
        c = self._c(tag)
        target = None
        source = None
        for which in ("visible", "active", "all"):
            for d in self.decisions(c.tag, which):
                if d["key"] == key:
                    target = d["ptr"]; source = which; break
            if target:
                break
        if not target:
            # Generic decisions (improved_worker_conditions, infrastructure_building_slot,
            # ...) are in the database but not in the country's "visible" array until a
            # category becomes visible. The command takes the definition pointer.
            target = self.decision_definitions().get(key)
            source = "database"
        if not target:
            raise KeyError("decision not found: %s" % key)
        pp0 = c.political_power
        with self:
            cmd = self.cmd.make("SelectDecision", tag=c.tag_id, decision_ptr=target)
            # CCommand::Run: CanExecute -> Post(queue, cmd, 1), the click path.
            rv = self._run_cmd(cmd)
        for _ in range(10):
            if c.political_power != pp0:
                break
            self._settle(0.4)
        return {"ok": bool(rv & 0xFF), "decision": key, "source": source,
                "error": getattr(self, "_last_cmd_error", ""),
                "political_power_before": pp0, "political_power_after": c.political_power}

    # ==================================================== POLITICS AND IDEAS
    def _idea_entries(self, tag=None, which="all"):
        """Country idea instances: key, pointer, and the law-category object at +0xA78.

        PL_ALL_IDEAS and PL_ACTIVE_IDEAS point at per-country CIdea instances. They are
        the correct operands for CReplaceIdeaCommand; CIdeaDatabase entries are only
        definitions and are not accepted as a law replacement operand.
        """
        p = self.p
        c = self._c(tag)
        pol = p.u64(c.ptr + CO_POLITICS)
        off = PL_ALL_IDEAS if which == "all" else PL_ACTIVE_IDEAS
        out = []
        for i in _arr(p, pol, off):
            nm = item_name(p, i + IDEA_NAME)
            if nm:
                out.append({"key": nm, "ptr": i, "category": p.u64(i + 0xA78)})
        return out

    def politics(self, tag=None):
        p = self.p
        c = self._c(tag)
        pol = p.u64(c.ptr + CO_POLITICS)
        active = self._idea_entries(tag, "active")
        return {
            "political_power": c.political_power,
            "ruling_party_popularity": c.ruling_party_popularity,
            "active_ideas": [a["key"] for a in active],
            "active_idea_ptrs": active,
            "available_idea_count": _count(p, pol, PL_ALL_IDEAS),
            "party_count": _count(p, pol, PL_PARTIES),
        }

    def available_ideas(self, tag=None, filter_text=""):
        out = []
        for e in self._idea_entries(tag, "all"):
            if not filter_text or filter_text.lower() in e["key"].lower():
                out.append(e)
        return out

    def _find_idea(self, key, tag=None):
        for e in self._idea_entries(tag, "all"):
            if e["key"] == key:
                return e
        return None

    def _active_idea_in_category(self, category, tag=None):
        if not category:
            return None
        for e in self._idea_entries(tag, "active"):
            if e["category"] == category:
                return e
        return None

    def _replace_cmd(self, old, new, tag=None):
        c = self._c(tag)
        return self.cmd.make("CReplaceIdea", tag=c.tag_id,
                             old_ptr=old["ptr"], new_ptr=new["ptr"])

    def _free_cmd(self, cmd_ptr):
        """Return a command object to the game's heap.

        `cmd.make()` allocates with the game's operator new. A command that is
        only used for a dry-run CanExecute is never posted, so the game never
        frees it. Leaking one per probe used to add ~500 MB of game memory per
        hoi4_overview call, which OOM-killed the whole box (the MCP child hit
        7.8 GB and the attached game died with it).
        """
        if not cmd_ptr:
            return
        try:
            import offsets as O
            self.call(O.FN_OPERATOR_DELETE, (cmd_ptr,), absolute=False)
        except Exception:
            pass

    def _replace_can_execute(self, old, new, tag=None):
        """Call CReplaceIdeaCommand::CanExecute (vtable +0x50) without posting.

        Safe as a dry run: it only reads the command, country and both idea objects.
        The command object is freed again right after the check.
        """
        import api
        api.allow_call(0x1CED690)  # CReplaceIdeaCommand::CanExecute, disassembled above
        cmd = self._replace_cmd(old, new, tag)
        try:
            fn = self.p.u64(self.va(0x4415D70) + 0x50)
            return bool(self.call(fn, (cmd, 0), absolute=True) & 0xFF)
        finally:
            self._free_cmd(cmd)

    def replace_idea(self, key, tag=None):
        """Replace the current law in the same category, using the game's own command."""
        c = self._c(tag)
        new = self._find_idea(key, tag)
        if not new:
            raise KeyError("idea not found: %s" % key)
        old = self._active_idea_in_category(new["category"], tag)
        if not old:
            raise KeyError("no active idea in category for: %s" % key)
        before = c.political_power
        with self:
            cmd = self._replace_cmd(old, new, tag)
            rv = self._run_cmd(cmd)
        # The law change lands on a later tick: poll instead of one 0.25s nap.
        active = set()
        for _ in range(10):
            self._settle(0.3)
            active = set(self.politics(c.tag)["active_ideas"])
            if key in active:
                break
        return {"ok": key in active, "idea": key, "old": old["key"],
                "method": "replace", "run": bool(rv & 0xFF),
                "error": getattr(self, "_last_cmd_error", ""),
                "pp_before": before, "pp_after": c.political_power,
                "active": key in active}

    def can_replace_idea(self, key, tag=None):
        new = self._find_idea(key, tag)
        if not new:
            return {"id": key, "legal": False, "error": "idea not found"}
        old = self._active_idea_in_category(new["category"], tag)
        if not old:
            return {"id": key, "legal": False, "error": "no active idea in category"}
        with self:
            ok = self._replace_can_execute(old, new, tag)
        return {"id": key, "legal": ok, "current": old["key"],
                "method": "replace", "ptr": new["ptr"]}

    # Law pre-filter table: law -> (min war support, needs war).  PURE PYTHON.
    # The game's own CanExecute still runs exactly once, inside CCommand::Run,
    # when hoi4_add_idea / hoi4_replace_idea actually posts the command.  This
    # table only decides what to ADVERTISE, so no ptrace injection is needed to
    # build the candidate list.
    # Values read from this build's own script files (2026-11-12):
    #   common/ideas/_economic.txt  low 0.15 / partial 0.25 / war 0.50 / tot 0.80
    #   common/ideas/_manpower.txt  limited 0.10 / extensive 0.20 / service 0.60
    #                               all_adults 0.70 / scraping 0.85
    # The old table advertised low/partial at 0.0, so `hoi4_available_ideas` called
    # a law legal that CReplaceIdeaCommand::CanExecute then refused.
    _LAW_GATE = {
        "free_trade": (0.0, False),
        "export_focus": (0.0, False),
        "limited_exports": (0.0, False),
        "civilian_economy": (0.0, False),
        "low_economic_mobilisation": (0.15, False),
        "partial_economic_mobilisation": (0.25, False),
        "war_economy": (0.50, False),
        "extensive_conscription": (0.20, False),
        "autarkic_economy": (0.50, False),
        "service_by_requirement": (0.60, True),
        "closed_economy": (0.0, True),
        "tot_economic_mobilisation": (0.80, True),
        "totaler_krieg_economy": (0.80, True),
        "all_adults_serve": (0.70, True),
        "all_men_serve": (0.80, True),
        "scraping_the_barrel": (0.85, True),
        "total_mobilization": (0.80, True),
    }

    # Law hierarchy. `recommended_ideas` used to advertise ANY law of the same
    # category that was not active -- including civilian_economy while the country
    # was already on partial_economic_mobilisation. An agent that followed the
    # recommendation downgraded the country's economy.
    _LAW_RANK = {
        "undisturbed_isolation": 0, "isolation": 1,
        "civilian_economy": 2, "low_economic_mobilisation": 3,
        "partial_economic_mobilisation": 4, "war_economy": 5,
        "autarkic_economy": 5, "tot_economic_mobilisation": 6,
        "totaler_krieg_economy": 7,
        "disarmed_nation": 0, "volunteer_only": 1, "limited_conscription": 2,
        "extensive_conscription": 3, "service_by_requirement": 4,
        "all_adults_serve": 5, "all_men_serve": 5, "scraping_the_barrel": 6,
    }

    def recommended_ideas(self, tag=None, force=False):
        """Law candidates, pre-filtered IN PYTHON -- zero game calls, zero ptrace.

        `hoi4_overview` used to probe every candidate law with a real
        `CReplaceIdeaCommand::CanExecute` injection (~15 command allocations per
        cold call, dozens of stop/start cycles on the main thread).  That was the
        "overview takes several seconds" complaint AND the biggest source of
        `free(): invalid pointer`: every probe allocated a command in the game's
        heap.  The list below is now built from memory reads only:

          * free_trade / export_focus / limited_exports / partial_economic_mobilisation
            are always advertised when they are not the active law;
          * war_economy / extensive_conscription need war_support >= 0.50 or a war;
          * the late-war tiers additionally require a war and higher war support.

        `legal` is therefore a PRE-FILTER verdict, not a verdict from the game.
        The authoritative check is the single CanExecute that CCommand::Run does
        when the law is actually changed; a refusal shows up as ok=false there.
        """
        c = self._c(tag)
        active = {a["key"] for a in self._idea_entries(tag, "active")}
        summary = c.summary()
        ws = float(summary.get("war_support") or 0.0)
        try:
            import combat
            at_war = bool(combat.at_war(self, c.tag))
        except Exception:
            at_war = False
        order = ["free_trade", "export_focus", "limited_exports", "autarkic_economy",
                 "closed_economy", "civilian_economy", "low_economic_mobilisation",
                 "partial_economic_mobilisation", "war_economy",
                 "tot_economic_mobilisation", "totaler_krieg_economy",
                 "extensive_conscription", "service_by_requirement", "all_adults_serve",
                 "scraping_the_barrel", "all_men_serve", "total_mobilization"]
        have = {i["key"]: i for i in self._idea_entries(tag, "all")}
        out, blocked = [], []
        for k in order:
            if k not in have or k in active:
                continue
            new = have[k]
            old = self._active_idea_in_category(new["category"], tag)
            if not old:
                continue
            cur_rank = self._LAW_RANK.get(old["key"])
            new_rank = self._LAW_RANK.get(k)
            if cur_rank is not None and new_rank is not None and new_rank <= cur_rank:
                blocked.append({"id": k, "why": "would downgrade the current law %s"
                                                % old["key"]})
                continue
            need_ws, need_war = self._LAW_GATE.get(k, (0.50, False))
            if need_war and not at_war:
                blocked.append({"id": k, "why": "needs a war"})
                continue
            if ws < need_ws:
                blocked.append({"id": k, "why": "needs war_support >= %.2f (have %.2f)"
                                                   % (need_ws, ws)})
                continue
            out.append({"id": k, "ptr": new["ptr"], "cost_pp": 150,
                        "affordable": c.political_power >= 150,
                        "legal": True, "current": old["key"],
                        "method": "replace", "prefilter": True})
        return {"political_power": c.political_power, "ideas": out,
                "blocked": blocked, "war_support": ws, "at_war": at_war,
                "cached": False, "source": "python_prefilter"}

    def idea_definitions(self):
        """CIdeaDatabase definition pointers (the global idea/law/advisor definitions)."""
        if getattr(self, "_idea_defs", None) is not None:
            return self._idea_defs
        db = self.oi.singleton("CIdeaDatabase")
        out = {}
        if db:
            for x in _arr(self.p, db, 0x68):
                k = item_name(self.p, x + IDEA_NAME)
                if k:
                    out[k] = x
        self._idea_defs = out
        return out

    def add_idea(self, key, tag=None):
        """Apply an idea through the same command a politics-tab click uses.

        Laws and mutually-exclusive advisors are CReplaceIdeaCommand: the country
        already owns one idea in that category, so AddIdea would fail the category
        check. National spirits are CAddIdeaCommand. The branch is chosen from the
        country's own idea list, not guessed from the key.
        """
        c = self._c(tag)
        active = {a["key"] for a in self._idea_entries(tag, "active")}
        if key in active:
            return {"ok": True, "idea": key, "method": "already_active",
                    "run": True, "active": True,
                    "pp_before": c.political_power, "pp_after": c.political_power}
        new = self._find_idea(key, tag)
        if new and new["category"] and self._active_idea_in_category(new["category"], tag):
            return self.replace_idea(key, tag)

        # National spirits / first advisor in an empty category: CAddIdeaCommand.
        ptr = self.idea_definitions().get(key) or (new["ptr"] if new else None)
        if not ptr:
            # V2-04: advisors are not CIdeas at all — route them to CAddAdvisorCommand.
            try:
                hired = self.hire_advisor(key, tag)
            except Exception as e:
                hired = {"ok": False, "attempted": False, "error": repr(e)}
            if hired.get("attempted"):
                return hired
            raise KeyError("idea not found: %s" % key)
        before = c.political_power
        with self:
            cmd = self.cmd.make("AddIdea", tag=c.tag_id,
                                idea_ptr=ptr, flag=1)
            rv = self._run_cmd(cmd)
        self._settle()
        active2 = set(self.politics(c.tag)["active_ideas"])
        return {"ok": key in active2, "idea": key, "method": "add",
                "run": bool(rv & 0xFF), "error": getattr(self, "_last_cmd_error", ""),
                "pp_before": before,
                "pp_after": c.political_power, "active": key in active2}

    def remove_idea(self, key, tag=None):
        p = self.p
        c = self._c(tag)
        pol = p.u64(c.ptr + CO_POLITICS)
        for i in _arr(p, pol, PL_ACTIVE_IDEAS):
            if item_name(p, i + IDEA_NAME) == key:
                with self:
                    self.cmd.send("RemoveIdea", tag=c.tag_id, idea_ptr=i, flag=1)
                self._settle()
                return {"ok": key not in self.politics(c.tag)["active_ideas"], "idea": key}
        raise KeyError("not an active idea: %s" % key)

    # ============================================================= PRODUCTION
    def _load_equipment(self):
        if getattr(self, "_eq_by_id", None) is not None:
            return
        p = self.p
        db = self.oi.singleton("CEquipmentDatabase")
        byid, byname = {}, {}
        for off in (0x60, 0x78, 0xF0, 0x120):
            for t in _arr(p, db, off):
                nm = item_name(p, t + ET_NAME)
                if nm:
                    byid[p.i32(t + ET_ID)] = nm
                    byname[nm] = t
        self._eq_by_id, self._eq_by_name = byid, byname

    def equipment_types(self, filter_text=""):
        self._load_equipment()
        f = filter_text.lower()
        return sorted(n for n in self._eq_by_name if not f or f in n.lower())

    def production_lines(self, tag=None):
        p = self.p
        self._load_equipment()
        ps = p.u64(self._c(tag).ptr + CO_PRODUCTION)
        out = []
        for i, l in enumerate(_arr(p, ps, PS_LINES)):
            v = p.u64(l + ML_VARIANT)
            eid = p.i32(v + EV_TYPE_ID) if v else None
            out.append({
                "index": i,
                "equipment": self._eq_by_id.get(eid),
                "factories": p.i32(l + ML_FACTORIES),
                "unit_cost": round(p.i64(l + ML_UNIT_COST) / O.FIXED, 3),
                "effective_cost": round(p.i64(l + ML_EFF_COST) / O.FIXED, 3),
                "output_per_day": round(p.i64(l + ML_OUTPUT) / O.FIXED, 3),
                "max_efficiency": p.i32(l + ML_MAX_EFFICIENCY),
                "priority": p.i32(l + ML_PRIORITY),
                "class": self.oi.class_of(l),
            })
        return out

    def set_production_amount(self, line_index: int, amount: int, tag=None):
        """Set how many FACTORIES a production line gets (the +/- buttons in the UI).

        NOTE. This used to silently do nothing: there is no command called
        "SetProductionLineAmount", and the result was never checked before returning
        {"ok": True}. The right command is `CAddProductionLineFactoriesCommand`:
        arg0 is the line's HANDLE (type 56) and arg1 is a DELTA, not an absolute
        count. The delta is computed here, and afterwards the line's factory field
        (CMilitaryProductionLine+0x14) is read back to report whether it really
        changed.
        """
        p = self.p
        c = self._c(tag)
        prod = p.u64(c.ptr + CO_PRODUCTION)
        lines = _arr(p, prod, PS_MIL_LINES)
        if not (0 <= line_index < len(lines)):
            return {"ok": False, "error": "no such line: %d (of %d)" % (line_index, len(lines))}
        line = lines[line_index]
        before = p.i32(line + ML_FACTORIES)
        delta = int(amount) - before
        if delta == 0:
            return {"ok": True, "line": line_index, "factories": before,
                    "note": "already set"}
        h = self.handle_of(line)
        # 56 = land/air production line, 57 = NAVAL production line. Both are valid.
        if h["type"] not in (56, 57):
            return {"ok": False, "error": "expected a production line handle (56/57), got %d" % h["type"]}
        # 2026-11-11: one command does NOT move the line by the whole delta. The
        # engine applies at most a few factories per command (measured: asking for
        # +12 moved the line by 1), so a single post reported ok=False while the
        # line really did move. Step the command until the read-back matches, and
        # report the whole trajectory instead of one sample.
        steps = []
        tray = []
        cur = before
        for _ in range(24):
            if cur == int(amount):
                break
            step = int(amount) - cur
            step = max(-5, min(5, step))
            self.send_command("AddProductionLineFactories",
                              {"arg0": (h["id"] << 32) | (h["type"] & 0xFFFFFFFF),
                               "arg1": step}, tag)
            self._settle(0.18)
            nxt = p.i32(line + ML_FACTORIES)
            steps.append(step)
            tray.append(nxt)
            if nxt == cur:
                break          # the engine stopped taking factories (none idle)
            cur = nxt
        after = p.i32(line + ML_FACTORIES)
        paused = None
        try:
            import session as _s
            paused = bool(_s.is_paused(self))
        except Exception:
            pass
        note = ""
        if after != int(amount):
            if paused:
                note = ("the game is PAUSED, so its command queue is not consumed; "
                        "unpause and retry")
            else:
                note = ("only reached %d of %d — not enough spare factories (the "
                        "engine only hands out factories the country actually has)"
                        % (after, int(amount)))
        return {"ok": after == int(amount), "line": line_index,
                "before": before, "after": after, "requested": int(amount),
                "steps": steps, "readback": tray, "paused": paused, "note": note}

    def _variant_for(self, equipment, tag=None):
        """(variant_ptr, handle) of an ACTIVE equipment variant we can produce."""
        p = self.p
        self._load_equipment()
        prod = p.u64(self._c(tag).ptr + CO_PRODUCTION)
        for v in _arr(p, prod, PS_VARIANTS_ACTIVE):
            if self._eq_by_id.get(p.i32(v + EV_TYPE_ID)) == equipment:
                return v, self.handle_of(v)
        return None, None

    def set_production_line_equipment(self, line_index: int, equipment: str, tag=None,
                                      dry_run=False):
        """SWAP an existing production line to another equipment variant (CA换装).

        Uses CSetProductionLineCommand (line handle type 56, variant handle type 70).
        This is exactly what clicking a different equipment in the production tab
        does. The line keeps its factories and efficiency is reset by the game.
        """
        p = self.p
        c = self._c(tag)
        self._load_equipment()
        prod = p.u64(c.ptr + CO_PRODUCTION)
        lines = _arr(p, prod, PS_MIL_LINES)
        if not (0 <= line_index < len(lines)):
            return {"ok": False, "error": "no such line: %d (of %d)" % (line_index, len(lines))}
        line = lines[line_index]
        before_name = self._eq_by_id.get(p.i32(p.u64(line + ML_VARIANT) + EV_TYPE_ID))
        var, vh = self._variant_for(equipment, tag)
        if var is None:
            avail = sorted({self._eq_by_id.get(p.i32(v + EV_TYPE_ID))
                            for v in _arr(p, prod, PS_VARIANTS_ACTIVE)} - {None})
            return {"ok": False, "error": "no active variant for %s" % equipment,
                    "available": avail[:40]}
        lh = self.handle_of(line)
        if lh["type"] != 56:
            return {"ok": False, "error": "expected military line handle 56, got %d" % lh["type"]}
        if vh["type"] != 70:
            return {"ok": False, "error": "expected equipment variant handle 70, got %d" % vh["type"]}
        packed_line = (lh["id"] << 32) | (lh["type"] & 0xFFFFFFFF)
        packed_var = (vh["id"] << 32) | (vh["type"] & 0xFFFFFFFF)
        out = {"line": line_index, "before": before_name, "requested": equipment,
               "line_handle": [lh["type"], lh["id"]],
               "variant_handle": [vh["type"], vh["id"]],
               "dry_run": bool(dry_run)}
        if dry_run:
            out.update(ok=True, after=before_name)
            return out
        self.send_command("SetProductionLine",
                          {"0x24": packed_line, "0x2C": packed_var}, tag)
        self._settle(0.4)
        after_name = self._eq_by_id.get(p.i32(p.u64(line + ML_VARIANT) + EV_TYPE_ID))
        out["after"] = after_name
        out["ok"] = after_name == equipment
        if not out["ok"]:
            out["error"] = "the game did not switch the line (CanExecute refused or the handle was wrong)"
        return out

    def create_production_line(self, equipment: str, tag=None, dry_run=False):
        """EXPERIMENTAL: create a brand-new production line for an equipment variant.

        CAddProductionLineCommand takes (country tag, handle, handle). Which handle
        belongs where is not yet verified, so this tries the variant in slot 1 and
        reports exactly what the game did. If the game refuses, nothing is written.
        """
        p = self.p
        c = self._c(tag)
        self._load_equipment()
        prod = p.u64(c.ptr + CO_PRODUCTION)
        before_ptrs = set(_arr(p, prod, PS_MIL_LINES))
        before = len(before_ptrs)
        var, vh = self._variant_for(equipment, tag)
        if var is None:
            return {"ok": False, "error": "no active variant for %s" % equipment}
        packed = (vh["id"] << 32) | (vh["type"] & 0xFFFFFFFF)
        out = {"before": before, "requested": equipment, "dry_run": bool(dry_run)}
        if dry_run:
            out.update(ok=True, variant_handle=[vh["type"], vh["id"]])
            return out
        attempts = []
        for arg0, arg1 in ((packed, 0), (0, packed)):
            try:
                res = self.send_command("AddProductionLine",
                                        {"0x30": arg1, "0x28": arg0}, tag)
                attempts.append({"arg0": arg0, "arg1": arg1, "result": res})
            except Exception as e:
                attempts.append({"arg0": arg0, "arg1": arg1, "error": str(e)})
            self._settle(0.4)
            after_lines = _arr(p, prod, PS_MIL_LINES)
            new = [x for x in after_lines if x not in before_ptrs]
            if new:
                # The game inserts the new line at the TOP of the list, not at the
                # end, so find it by pointer identity rather than by [-1].
                name = self._eq_by_id.get(p.i32(p.u64(new[0] + ML_VARIANT) + EV_TYPE_ID))
                out.update(ok=name == equipment, after=len(after_lines),
                           equipment=name, line_index=after_lines.index(new[0]),
                           attempts=attempts)
                return out
        out.update(ok=False, after=len(_arr(p, prod, PS_MIL_LINES)),
                   error="the game refused both handle arrangements",
                   attempts=attempts)
        return out

    def remove_production_line(self, line_index: int, tag=None):
        """Remove a production line entirely.

        The same bug lived here as in `set_production_amount`: there is no command
        called "RemoveProductionLine". The right one is `CRemoveProductionLineCommand`,
        with arg0 being the line's HANDLE (type 56).
        """
        p = self.p
        c = self._c(tag)
        prod = p.u64(c.ptr + CO_PRODUCTION)
        lines = _arr(p, prod, PS_MIL_LINES)
        if not (0 <= line_index < len(lines)):
            return {"ok": False, "error": "no such line: %d" % line_index}
        h = self.handle_of(lines[line_index])
        # 56 = land/air production line, 57 = NAVAL production line. Both are valid.
        if h["type"] not in (56, 57):
            return {"ok": False, "error": "expected a production line handle (56/57), got %d" % h["type"]}
        before = len(lines)
        # Real CRemoveProductionLineCommand ctor: one line handle at 0x24.
        self.send_command("RemoveProductionLineByHandle",
                          {"0x24": (h["id"] << 32) | (h["type"] & 0xFFFFFFFF)}, tag)
        self._settle()
        after = len(_arr(p, p.u64(self._c(tag).ptr + CO_PRODUCTION), PS_MIL_LINES))
        return {"ok": after < before, "before": before, "after": after}

    # =========================================================== GRAND DOCTRINES
    # 1.19 stores grand doctrines outside the CCommand catalog as
    # NDoctrines::CGrandDoctrineTemplate objects (vtable 0x447EFA0), and the
    # database is lazily created, so ObjectIndex (writable-only) sees none of
    # them. Scan every readable region for the vtable once per process.
    DOCTRINE_LOC = {
        "new_mobile_warfare": "GRAND_DOCTRINE_MOBILE_WARFARE",
        "mobile_warfare": "GRAND_DOCTRINE_MOBILE_WARFARE",
        "superior_firepower": "GRAND_DOCTRINE_SUPERIOR_FIREPOWER",
        "grand_battleplan": "GRAND_DOCTRINE_GRAND_BATTLEPLAN",
        "mass_assault": "GRAND_DOCTRINE_MASS_ASSAULT",
        "fleet_in_being": "GRAND_DOCTRINE_FLEET_IN_BEING",
        "convoy_raiding": "GRAND_DOCTRINE_CONVOY_RAIDING",
        "base_strike": "GRAND_DOCTRINE_BASE_STRIKE",
        "battlefield_support": "GRAND_DOCTRINE_BATTLEFIELD_SUPPORT",
        "operational_integrity": "GRAND_DOCTRINE_OPERATIONAL_INTEGRITY",
        "strategic_destruction": "GRAND_DOCTRINE_STRATEGIC_DESTRUCTION",
        "special_forces_quantity": "GRAND_DOCTRINE_SPECIAL_FORCES_QUANTITY",
        "special_forces_quality": "GRAND_DOCTRINE_SPECIAL_FORCES_QUALITY",
    }

    def _doctrine_templates(self):
        key = (self.p.pid, self.p.base)
        if getattr(self, "_doc_cache_key", None) == key:
            return self._doc_cache
        target = self.p.va(0x447EFA0)
        found = {}
        for m in self.p.regions(writable=False):
            a = m["start"]
            while a < m["end"]:
                n = min(4 << 20, m["end"] - a)
                n -= n % 8
                if n <= 0:
                    break
                buf = self.p.try_read(a, n)
                if buf:
                    for i in range(0, len(buf) - 7, 8):
                        if struct.unpack_from("<Q", buf, i)[0] == target:
                            obj = a + i
                            nm = item_name(self.p, obj + 0x20)
                            if nm:
                                found[nm] = obj
                a += n
            if len(found) >= 12:
                break
        self._doc_cache_key = key
        self._doc_cache = found
        return found

    def unlock_doctrine(self, doctrine, tag=None):
        """Unlock a grand doctrine (mobile_warfare / superior_firepower / ...).

        The command is NDoctrines::CUnlockGrandDoctrineCommand (vtable 0x4360C28,
        +0x28 = CGrandDoctrineTemplate*, +0x30 = 0). Success is verified by
        finding the template pointer in the country's doctrine store at
        CCountry+0x15E0, not by trusting the command's return value.
        """
        loc = self.DOCTRINE_LOC.get(str(doctrine or "").lower())
        if not loc:
            return {"ok": False, "error": "unknown doctrine: %s" % doctrine,
                    "available": sorted(self.DOCTRINE_LOC)}
        templates = self._doctrine_templates()
        tpl = templates.get(loc)
        if not tpl:
            return {"ok": False, "error": "template not found: %s" % loc,
                    "templates": sorted(templates)}
        res = self.send_command("UnlockGrandDoctrine", {"0x28": tpl}, tag)
        self._settle(0.6)
        stored = False
        try:
            sub = self.p.u64(self._c(tag).ptr + 0x15E0)
            if sub:
                data = self.p.read(sub, 0x2000)
                stored = any(struct.unpack_from("<Q", data, i)[0] == tpl
                             for i in range(0, len(data) - 7, 8))
        except Exception:
            pass
        rv = int((res or {}).get("run_result") or 0)
        accepted = bool(rv & 0xFF)
        return {"ok": bool(stored) or accepted, "doctrine": doctrine,
                "localization": loc, "template": hex(tpl), "stored": stored,
                "accepted": accepted, "command": res,
                "note": ("command accepted by CanExecute; the +0x15E0 store is not the "
                         "authoritative UI status, so `stored` may stay false"
                         if accepted and not stored else
                         ("already unlocked or command refused" if not accepted else ""))}

    def duplicate_division_template(self, template, tag=None):
        """Create a new division template from an existing one (copy its composition).

        CCreateDivisionTemplateCommand ctor takes (cmd, source CDivisionTemplate*,
        CCountryTag*). We pass the template pointer from the country's template
        array. Success is verified by the template count going up.
        """
        rows = self.division_templates(tag)
        src = None
        for i, r in enumerate(rows):
            if str(template) in (str(i), str(r.get("name"))):
                src = int(r["ptr"], 16)
                break
        if src is None:
            return {"ok": False, "error": "no such template: %s" % template,
                    "templates": [r.get("name") for r in rows]}
        before = len(rows)
        res = self.send_command("CreateDivisionTemplateFrom", {"0x278": src}, tag)
        self._settle(0.5)
        after = len(self.division_templates(tag))
        return {"ok": after > before, "source": template, "before": before,
                "after": after, "command": res}

    # =========================================================== CONSTRUCTION
    def _load_buildings(self):
        if getattr(self, "_bld_by_key", None) is not None:
            return
        p = self.p
        db = self.oi.singleton("CBuildingDatabase")
        if not db:
            self._oi = None
            db = self.oi.singleton("CBuildingDatabase")
        if not db:
            self._bld_by_key = None
            self._bld_by_id = {}
            self._oi = None
            return
        bykey, byid = {}, {}
        for t in _arr(p, db, getattr(O, "BLD_DB_ITEMS", 0x40)):
            key = item_name(p, t + BT_KEY)
            if key:
                bid = p.i32(t + BT_ID)
                name = item_name(p, t + BT_NAME) if BT_NAME else None
                bykey[key] = {"id": bid, "name": name or key, "ptr": t,
                              "province_level": p.i32(t + 0x294) > 0}
                byid[bid] = key
        # Type ids the database does not describe (Windows: `infrastructure`).
        for bid, key in getattr(O, "BT_EXTRA", {}).items():
            bykey.setdefault(key, {"id": bid, "name": key, "ptr": 0,
                                   "province_level": True})
            byid[bid] = key
        self._bld_by_key, self._bld_by_id = bykey, byid

    def building_types(self):
        self._load_buildings()
        return {k: {"id": v["id"], "name": v["name"], "province_level": v["province_level"]}
                for k, v in sorted(self._bld_by_key.items())}

    def construction_queue(self, tag=None):
        p = self.p
        self._load_buildings()
        ps = p.u64(self._c(tag).ptr + CO_PRODUCTION)
        out = []
        # NOTE: this array is MIXED, alongside CBuildingProductionLine it also holds
        # CRailwayProductionLine, where +0x70 is NOT a building pointer (reading it
        # produced garbage and an EFAULT). Separate each element by its class.
        for i, l in enumerate(_arr(p, ps, PS_CONSTRUCTION)):
            cls = self.oi.class_of(l)
            prog = p.i64(l + BL_PROGRESS) / O.FIXED
            cost = p.i64(l + BL_COST) / O.FIXED
            row = {"index": i, "class": cls,
                   "progress": (prog / cost) if cost > 0 else 0.0,
                   "progress_ic": prog, "cost_ic": cost}
            if cls == "CBuildingProductionLine":
                b = p.u64(l + BL_BUILDING)
                row["building"] = (self._bld_by_id.get(p.i32(b + BG_TYPE_ID))
                                   if self.oi.class_of(b) == "CBuilding" else None)
            else:
                row["building"] = "railway" if cls == "CRailwayProductionLine" else None
            out.append(row)
        return out

    def states(self, tag=None):
        p = self.p
        return [{"id": p.i32(s + ST_ID), "ptr": s} for s in _arr(p, self._c(tag).ptr, CO_STATES)]

    def add_construction(self, state_id: int, building: str, level: int = 1,
                         province: int = 0, tag=None):
        """Queue a building: the same as using the construction tab."""
        self._load_buildings()
        c = self._c(tag)
        b = self._bld_by_key.get(building)
        if not b:
            raise KeyError("no such building type: %s (see building_types)" % building)
        if sys.platform == "win32":
            # Real command channel on the PE build.  `construction_queue()` cannot
            # see the result yet (its array offset is still wrong on Windows), so
            # success is reported from the enqueue itself; read the state's
            # buildings to confirm.
            import cmdwin
            r = cmdwin.add_construction(self, int(state_id), b["id"], level)
            self._settle()
            return {"ok": bool(r.get("ok")), "state": int(state_id),
                    "building": building, "type_id": b["id"], "level": level,
                    "cmd": r.get("cmd")}
        before = len(self.construction_queue(c.tag))
        # CBuildingReference (1.19.4.1, verified by disassembly and live test):
        #   +0x08 = location id   (state id for a state-level building, province id
        #                          for a province-level one)
        #   +0x0C = building type id
        #   +0x10 = 0
        # The AddConstruction ctor copies ref+8..ref+0x10 into cmd+0x30..+0x3C.
        loc = int(province or 0)
        if loc <= 0:
            loc = int(state_id)
        with self:
            cmd = self.cmd.make("AddConstruction", tag=c.tag_id,
                                ref_vptr=self.va(BUILDING_REF_VTABLE),
                                province=loc, building_type=b["id"], state=0,
                                level=level, flag=1)
            # CCommand::Run: CanExecute -> Post(queue, cmd, 1), the click path.
            rv = self._run_cmd(cmd)
        if not (rv & 0xFF):
            return {"ok": False, "before": before, "after": before,
                    "error": "the game refused the construction order (CanExecute false)"}
        # The command is asynchronous: the construction line appears on the next tick.
        q = self.construction_queue(c.tag)
        for _ in range(12):
            if len(q) > before:
                break
            self._settle(0.5)
            q = self.construction_queue(c.tag)
        return {"ok": len(q) > before, "before": before, "after": len(q), "queue": q}

    def remove_construction(self, index: int, tag=None):
        c = self._c(tag)
        before = len(self.construction_queue(c.tag))
        with self:
            self.cmd.send("RemoveConstruction", tag=c.tag_id, index=index)
        self._settle()
        return {"ok": len(self.construction_queue(c.tag)) < before}

    # ========================================================== DIPLOMACY
    def relation_count(self, tag=None):
        return _count(self.p, self.p.u64(self._c(tag).ptr + CO_DIPLOMACY), DP_RELATIONS)

    # ============================================================== HIZ
    def set_game_speed(self, speed: int):
        # The engine's own SetGameSpeed takes 0..5 (5 = fastest); the old guard
        # capped at 4 and silently ran the campaign one notch slow.
        if not 0 <= speed <= 5:
            raise ValueError("speed must be between 0 and 5")
        if sys.platform == "win32":
            # commands.py is the Linux build: FN_NEW (0x510950) is a Linux VA and
            # calling it on the PE takes the process down. Windows goes through
            # cmdwin, which allocates from the game's own allocator.
            import cmdwin
            return {"ok": True, "speed": speed, "posted": cmdwin.set_game_speed(self, speed)}
        with self:
            self.cmd.send("SetGameSpeed", speed=speed)
        return {"ok": True, "speed": speed}

    # ============================================================= SUMMARY
    def overview(self, tag=None):
        c = self._c(tag)
        cur = self.current_focus(c.tag)
        summary = c.summary()
        lines = self.production_lines(c.tag)
        used_mil = sum(int(l.get("factories") or 0) for l in lines
                       if l.get("class") == "CMilitaryProductionLine")
        return {
            "date": self.date()["text"],
            "alerts": self.alerts(c.tag),
            "factories": {"civ_total": summary.get("civilian_factories"),
                          "mil_total": summary.get("military_factories"),
                          "mil_free": max(0, (summary.get("military_factories") or 0) - used_mil),
                          "dock_total": summary.get("naval_dockyards"),
                          "construction_queue_len": len(self.construction_queue(c.tag))},
            "army_summary": {"total_divisions": len(self.player().armies().get("armies", []))
                             if False else len(_arr(self.p, c.ptr, CO_ARMIES)),
                             "unassigned_divisions": len(self.unassigned_divisions(c.tag))},
            "candidates": self.candidates(c.tag),
            "country": summary,
            "focus": {"current": cur["key"] if cur else None,
                      "name": cur["name"] if cur else None,
                      "tree_size": len(self.focus_tree(c.tag))},
            "research": self.research_slots(c.tag),
            "politics": self.politics(c.tag),
            "decisions_visible": len(self.decisions(c.tag, "visible")),
            "production": self.production_lines(c.tag),
            "construction": self.construction_queue(c.tag),
            "states": len(self.states(c.tag)),
        }

# ============================================================================
#  EXTENDED MACRO LAYER
#  Resources, armies, air, navy, diplomacy, characters, projects, statistics,
#  and generic access to all 370 player commands.
# ============================================================================

# --- extra CCountry offsets: arrays living directly inside CCountry ----------
CO_THEATRES         = 0x0168   # CPdxArray<CTheatre*>
CO_DIV_TEMPLATES    = 0x01B8   # CPdxArray<CReferencedDivisionTemplate*>
CO_FLEETS           = 0x0278   # CPdxArray<CFleet*>
CO_ARMIES           = 0x0290   # CPdxArray<CArmy*>
CO_COMMAND_POWER    = 0x01F0   # int64 x1e5
CO_RESOURCES        = 0x11C0   # CCountryResources*
CO_FOCUS_COMPLETED  = 0x1C10   # CPdxArray<CNationalFocus*>
CO_SUBUNITS         = 0x26C0   # CPdxArray<CSubUnitDefinition*>
CO_RELATIONS_ARR    = 0x2888   # CPdxArray<CRelationStatus*>  (one per country)
CO_PROGRAM_STATUS   = 0x0F80   # NProject::CProgramStatus*
CO_OCCUPATION       = 0x0FA8   # CCountryOccupationStatus*
CO_OPERATIONS       = 0x1548   # CCountryOperationManager*
CO_PROJECT_POOL     = 0x15D8   # NProject::CProjectPool*
CO_EQUIPMENT_TYPES  = 0x11E0   # CPdxArray<CEquipmentType*>

# CCountryResources: seven 16-byte {int64 amount; int32 id; ...} records from +0x60
RES_ARRAY           = 0x60
RES_STRIDE          = 0x10
RESOURCE_NAMES = ["oil", "aluminium", "rubber", "tungsten", "steel", "chromium", "coal"]

# CReferencedDivisionTemplate
DT_NAME             = 0x20
# CSubUnitDefinition
SU_KEY              = 0x10
SU_CATEGORY         = 0x60
# CFleet
FL_NAME             = 0xD8
# CCharacter
CH_NAME             = 0x20
CH_KEY              = 0x40
# CRelationStatus
REL_INDEX           = 0x50

FN_DYNAMIC_VAR      = 0x2D8EE60   # void GetVariable(out, const char* NAME, scope, int)


class MacroMixin:
    """Extra read and action capabilities mixed into Game."""

    # ---------------------------------------------------------- helpers
    def _scope_call(self, fn, tag=None, extra_this=True, signed=True):
        """Call the game's country-scoped getters: f(this, scope) -> int64"""
        c = self._c(tag)
        p = self.p

        def w(scr):
            p.write(scr, b"\0" * 0x400)
            p.write(scr + 0x208, struct.pack("<i", c.tag_id))   # scope = scr+0x200
            return (scr, scr + 0x200)
        import api
        # C2 (review 2026-11-07): `stats.pkl` was scanned against 1.19.2 and its
        # 45 addresses are WILD in 1.19.4.1. `api.allow_call(fn)` here re-opened
        # exactly the hole `api._load_stat_calls()` closed on 2026-11-04: the
        # injected call ran past the end of .text and SIGSEGV'd the game (that is
        # how hoi4_wars / hoi4_full_report kept killing the process). A getter
        # must be re-derived AND re-verified against this binary before it is
        # added to api.SCOPE_CALL_ALLOW.
        if fn not in api.SCOPE_CALL_ALLOW:
            raise api.CallError(
                "Refusing unverified 1.19.2 country-scope address %#x in 1.19.4.1" % fn)
        return self.call(fn, scratch_writer=w, signed=signed)

    # ======================================================== ISTATISTIKLER
    def country_stats(self, tag=None, raw=False):
        """~44 country statistics gathered from the game's own trigger functions."""
        import stats as _st
        table = _st.build()
        FIXED_KEYS = {"command_power", "command_power_daily", "political_power_daily",
                      "surrender_progress", "convoy_threat", "naval_mine_danger"}
        # Bool triggers return "actual == expected", and the expected field is zero, so
        # the result comes back inverted. The right value is not (rax & 1). Checked
        # against is_major, is_in_faction, has_war and has_capitulated; all agree.
        BOOL_PREFIX = ("has_", "is_", "exists", "country_has")
        # These want an extra parameter (id/tag), so they are not reliable here:
        SKIP = ("owns_", "days_since", "has_decision", "has_rule", "has_terrain",
                "has_opinion_modifier", "has_country_leader_ideology",
                "has_completed_national_focus", "has_active_timed_decision",
                "country_has_cosmetic_tag", "has_attache")
        out = {}
        with self:
            for name, (fn, cls) in sorted(table.items()):
                if not raw and name.startswith(SKIP):
                    continue
                try:
                    v = self._scope_call(fn, tag)
                except Exception:
                    continue
                if raw:
                    out[name] = v
                elif name in FIXED_KEYS:
                    out[name] = round(v / O.FIXED, 4)
                elif name.startswith(BOOL_PREFIX):
                    out[name] = not bool(v & 1)
                elif -10 ** 9 < v < 10 ** 9:
                    out[name] = v
        if not out:
            # 2026-11-11: stats.build() points at 1.19.2 trigger addresses and every
            # call is refused, so this tool returned {} for EVERY country — the agent
            # had no way to size up an opponent (it never knew France had 127
            # divisions). Fall back to the memory-only country summary plus the live
            # division count, and say so.
            try:
                out = dict(self._c(tag).summary() or {})
            except Exception:
                out = {}
            try:
                out["divisions"] = len(_arr(self.p, self._c(tag).ptr, CO_ARMIES))
            except Exception:
                pass
            out["source"] = ("memory-only fallback: stats.pkl holds 1.19.2 trigger "
                             "addresses and is disabled; manpower is only reliable "
                             "for the player country")
        return out

    def script_variable(self, name: str, tag=None):
        """Read a value through the game's dynamic variable resolver (e.g. 'ARMY_EXPERIENCE')."""
        c = self._c(tag)
        p = self.p
        hold = {}

        def w(scr):
            hold["s"] = scr
            p.write(scr, b"\0" * 0x200)
            p.write(scr + 0x208, struct.pack("<i", c.tag_id))
            p.write(scr + 0x100, name.encode() + b"\0")
            return (scr + 0x180, scr + 0x100, scr + 0x200, 0)
        r = self.call(FN_DYNAMIC_VAR, scratch_writer=w, signed=True)
        raw = p.read(hold["s"] + 0x180, 16)
        return {"rax": r, "out": struct.unpack("<qq", raw)}

    # ============================================================ RESOURCES
    def resources(self, tag=None):
        """Strategic resources: production and available amount."""
        p = self.p
        r = p.u64(self._c(tag).ptr + CO_RESOURCES)
        if not r:
            return {}
        out = {}
        for i, nm in enumerate(RESOURCE_NAMES):
            a = r + RES_ARRAY + i * RES_STRIDE
            out[nm] = round(p.i64(a) / O.FIXED, 2)
        return out

    # =============================================================== ARMIES
    def armies(self, tag=None):
        p = self.p
        c = self._c(tag)
        out = []
        for i, a in enumerate(_arr(p, c.ptr, CO_ARMIES)):
            out.append({"index": i, "ptr": hex(a), "class": self.oi.class_of(a)})
        return {"count": len(out), "armies": out}

    def division_templates(self, tag=None):
        p = self.p
        c = self._c(tag)
        out = []
        for i, t in enumerate(_arr(p, c.ptr, CO_DIV_TEMPLATES)):
            out.append({"index": i, "name": item_name(p, t + DT_NAME), "ptr": hex(t)})
        return out

    # ============================================ DIVISION TRAINING (CONVEYORS)
    def _template_handle(self, t_ptr):
        """Packed {type,id} handle of a CDivisionTemplate (it lives at +0x08)."""
        raw = self.p.read(t_ptr + 0x08, 8)
        t, i = struct.unpack("<ii", raw)
        return raw, t, i, (i << 32) | (t & 0xFFFFFFFF)

    def conveyors(self, tag=None):
        """Live division-training conveyors.

        CCountry+0xF48 -> CDeploymentStatus, its CPdxArray<CMilitaryDeploymentConveyor*>
        at +0x68. Verified live 2026-11-04: after CreateConveyor the array went from
        0 to 1 and the element's class resolves to CMilitaryDeploymentConveyor.
        (An `oi.instances()` scan cannot be used here — the index is a snapshot taken
        before the conveyor existed.)
        """
        p = self.p
        c = self._c(tag)
        out = []
        dep = p.u64(c.ptr + 0xF48)
        if not dep:
            return out
        for conv in _arr(p, dep, 0x68):
            try:
                h_type, h_id = struct.unpack("<ii", p.read(conv + 0x08, 8))
            except Exception:
                continue
            row = {"ptr": hex(conv), "handle": [h_type, h_id],
                   "packed_handle": (h_id << 32) | (h_type & 0xFFFFFFFF)}
            # CMilitaryDeploymentConveyor (1.19.4.1, from the ctor at 0xfcc3e0):
            #   +0x18 CReferencedDivisionTemplate*  +0x20 name (std::string)
            #   +0x48 line count  +0x70 CPdxArray<line>  +0x88 equipment priority
            try:
                row["lines"] = p.i32(conv + 0x48)
                row["priority"] = p.i32(conv + 0x88)
                row["template"] = item_name(p, p.u64(conv + 0x18) + DT_NAME)
            except Exception:
                pass
            out.append(row)
        return out

    _VP_CACHE = {}

    def deployment_province(self, state_id=None, tag=None) -> int:
        """A province inside `state_id` that can host a deployment (VP province first).

        CSetConveyorLocationCommand does NOT take a state id: its Execute bounds-checks
        the value against the game's PROVINCE count (13414), and a state id such as 64
        is refused while Berlin's province 6521 is accepted. That single wrong unit is
        why every conveyor stayed at "No location set" and 100 %-trained divisions
        never appeared on the map.
        """
        import mapdata
        st = int(state_id or self._homeland_state_id(tag))
        key = (mapdata.GAME, st)
        if key in self._VP_CACHE:
            return self._VP_CACHE[key]
        import glob, os as _os, re
        provs = sorted((mapdata._load_state_provinces_raw()[0] or {}).get(st) or [])
        best = None
        for fn in glob.glob(_os.path.join(mapdata.GAME, "history", "states", "%d-*.txt" % st)):
            try:
                txt = open(fn, encoding="utf-8", errors="replace").read()
            except OSError:
                continue
            m = re.search(r"victory_points\s*=\s*\{([^}]*)\}", txt)
            if not m:
                continue
            pairs = []
            for line in m.group(1).splitlines():
                line = line.split("#")[0].strip()
                parts = line.split()
                if len(parts) >= 2 and parts[0].isdigit() and parts[1].isdigit():
                    pairs.append((int(parts[1]), int(parts[0])))
            if pairs:
                best = max(pairs)[1]
        if best is None:
            best = provs[0] if provs else 6521
        self._VP_CACHE[key] = best
        return best

    def _homeland_state_id(self, tag=None) -> int:
        """A deployment state that is connected to the capital by land.

        HOI4 refuses to deploy a finished division into a state that has no land
        connection to the capital: East Prussia (state 5 / 763, behind the Polish
        corridor in 1936) is the classic trap — the training conveyor fills to 100 %
        and nothing ever appears on the map. `CCountry+0xFF0` is the capital STATE
        id, so the capital's own state is always the safe answer. The hard-coded
        German list is only a fallback for the case where the capital is occupied.
        """
        p = self.p
        c = self._c(tag)
        try:
            own = {int(s["id"]) for s in self.states(c.tag)}
        except Exception:
            own = set()
        try:
            cap = p.i32(c.ptr + CO_CAPITAL_STATE)
        except Exception:
            cap = 0
        if cap and (not own or cap in own):
            return cap
        if c.tag == "GER":
            # Brandenburg 64 = Berlin; then the other core states west of the corridor.
            for pref in (64, 59, 60, 52, 51, 55, 42, 57, 65):
                if pref in own:
                    return pref
        # last resort: an owned state that is not one of the two East Prussian ids
        rest = sorted(x for x in own if x not in (5, 763))
        if rest:
            return rest[0]
        return 64

    def train_divisions(self, template_name=None, lines=2, state_id=None, tag=None):
        """Open a division-training conveyor and push its first line onto the map.

        Three things killed the 1936 run here (2 conveyors x 5 lines, 17 months,
        0 divisions):
          * the deployment state was `states("GER")[0]` = East Prussia, an exclave
            behind the Polish corridor, so a finished division could never deploy;
          * 10 parallel lines competed for ~26 rifles/day, so no single division ever
            reached 100 % equipment;
          * the conveyor ran at normal equipment priority.
        The defaults are therefore: capital state, 2 lines, priority 2 (highest),
        unlimited series. Raise `lines` only when the rifle stockpile is large.
        """
        p = self.p
        c = self._c(tag)
        from commands import COMMANDS as _CMDS
        tmpls = self.division_templates(c.tag)
        if not tmpls:
            return {"ok": False, "error": "no division templates found"}
        pick = None
        for t in tmpls:
            if template_name and (t.get("name") == template_name
                                  or str(t.get("index")) == str(template_name)):
                pick = t
                break
        if not pick:
            pick = next((t for t in tmpls if "Infanterie" in (t.get("name") or "")
                         or "Infantry" in (t.get("name") or "")), tmpls[0])
        t_ptr = int(pick["ptr"], 16)
        _, h_type, h_id, packed = self._template_handle(t_ptr)
        if h_type <= 0:
            return {"ok": False, "error": "template handle looks invalid: %d" % h_type}
        before = {x["ptr"] for x in self.conveyors(c.tag)}
        with self:
            cmd = self.cmd.make("CreateConveyor", template_handle=packed, priority=2)
            rv = self._run_cmd(cmd)
        self._settle(0.6)
        new = [x for x in self.conveyors(c.tag) if x["ptr"] not in before]
        if not new:
            return {"ok": False, "run": bool(rv & 0xFF), "template": pick.get("name"),
                    "template_handle": [h_type, h_id],
                    "error": getattr(self, "_last_cmd_error", "") or "no conveyor appeared"}
        conv = new[0]
        ch = conv["packed_handle"]
        if state_id is None or int(state_id) in (5, 763):
            state_id = self._homeland_state_id(c.tag)
        prov = self.deployment_province(state_id, tag=c.tag)
        errors = []
        with self:
            rv_loc = self._run_cmd(self.cmd.make("SetConveyorLocation",
                                                 conveyor_handle=ch, province_id=int(prov)))
            if not (rv_loc & 0xFF):
                errors.append("location: %s" % (getattr(self, "_last_cmd_error", "") or "refused"))
            if "SetConveyorPriority" in _CMDS:
                rv_pri = self._run_cmd(self.cmd.make("SetConveyorPriority",
                                                     conveyor_handle=ch, priority=2))
                if not (rv_pri & 0xFF):
                    errors.append("priority: %s" % (getattr(self, "_last_cmd_error", "") or "refused"))
            if "SetConveyorSeries" in _CMDS:
                self._run_cmd(self.cmd.make("SetConveyorSeries",
                                            conveyor_handle=ch, series=0))
            if int(lines) > 1:
                self._run_cmd(self.cmd.make("AddConveyorLine",
                                            conveyor_handle=ch, count=int(lines) - 1))
        self._settle(0.4)
        return {"ok": True, "template": pick.get("name"), "template_handle": [h_type, h_id],
                "conveyor": conv["ptr"], "conveyor_handle": conv["handle"],
                "lines": int(lines), "state_id": int(state_id),
                "deploy_province": int(prov),
                "errors": errors,
                "deploy_error": getattr(self, "_last_cmd_error", "")}

    def deploy_ready_conveyors(self, tag=None, min_progress=0.0):
        """Push a conveyor's finished division onto the map.

        `min_progress` is the 2026-11-05 fix for the 174-shell army: calling
        CDeployConveyorCommand on a 20 %-trained line yanks a half-equipped
        division out of training (the in-game "deploy now" button). With a valid
        deployment province the engine lands 100 % divisions by itself, so the
        command is only a rescue for a line that is finished and still waiting.
        """
        deployed, attempts = 0, []
        for conv in self.conveyors(tag):
            prog = None
            if min_progress > 0:
                try:
                    _, prog = self._conveyor_progress(conv)
                except Exception:
                    prog = None
                if prog is None or prog < min_progress:
                    attempts.append({"conveyor": conv["ptr"], "skipped": True,
                                     "progress": prog, "need": min_progress})
                    continue
            with self:
                rv = self._run_cmd(self.cmd.make("DeployConveyor",
                                                 conveyor_handle=conv["packed_handle"]))
            attempts.append({"conveyor": conv["ptr"], "run": bool(rv & 0xFF),
                             "error": getattr(self, "_last_cmd_error", "")})
            if rv & 0xFF:
                deployed += 1
        if deployed:
            self._settle(0.5)
        return {"conveyors": len(attempts), "deployed": deployed, "attempts": attempts}

    def front_changes(self, enemy_tag=None, reset=False):
        """Province CONTROL changes since the previous call: the actual front movement.

        Uses mapdata.province_controller_map(), not state ownership. In a civil war
        (and any war before a peace treaty) occupation only changes the province
        controller; the old owner-map version reported changed_count=0 while a
        state was being overrun (2026-11-02 UK test).
        """
        import json, os, time
        import mapdata
        control = mapdata.province_controller_map(self)
        cache = os.path.join(os.path.dirname(os.path.abspath(__file__)), "front_snapshot.json")
        try:
            now_date = self.date_raw()
        except Exception:
            now_date = None
        try:
            pid = self.p.pid
        except Exception:
            pid = None
        et = enemy_tag.upper() if enemy_tag else None
        prev = None
        if os.path.exists(cache) and not reset:
            try:
                with open(cache) as fh:
                    prev = json.load(fh)
            except Exception:
                prev = None
        reset_reason = None
        if prev:
            if prev.get("schema") != 2:
                reset_reason = "legacy_baseline"
            elif prev.get("pid") is None or prev.get("date") is None:
                reset_reason = "legacy_baseline"
            elif pid is not None and prev.get("pid") != pid:
                reset_reason = "pid_changed"
            elif now_date is not None and prev.get("date") is not None and now_date < prev["date"]:
                reset_reason = "date_went_backwards"
            if reset_reason:
                prev = None
        now = {"schema": 2, "ts": time.time(), "pid": pid, "date": now_date,
               "enemy_tag": et,
               "control": {str(k): v for k, v in control.items()}}
        changed = []
        if prev and prev.get("control"):
            po = prev["control"]
            for k, v in now["control"].items():
                old = po.get(k)
                if old != v:
                    changed.append({"province": int(k), "from": old, "to": v})
        try:
            with open(cache, "w") as fh:
                json.dump(now, fh)
        except Exception:
            pass
        if et:
            changed = [c for c in changed if et in ((c.get("from") or ""), (c.get("to") or ""))]
        return {
            "changed_count": len(changed),
            "changed": changed[:300],
            "provinces_seen": len(control),
            "first_run": prev is None,
            "reset_reason": reset_reason,
            "pid": pid,
            "date": now_date,
            "age_seconds": round(time.time() - prev["ts"], 1) if prev else None,
        }

    def duplicate_division_template(self, source_name, tag=None, new_name=None):
        """Task 2 (2026-11-02 work order): borrow/copy an existing CDivisionTemplate.

        CCreateDivisionTemplateCommand ctor 0x1FDC190 deep-copies the source template
        (embedded at CReferencedDivisionTemplate+0x18) into the command and posts it
        through CCommand::Run -- the exact path the UI 'Duplicate' button uses.
        """
        import struct as _s
        c = self._c(tag)
        arr = c.ptr + 0x1B8                      # CPdxArray<CReferencedDivisionTemplate*>
        d = self.p.u64(arr)
        n = min(self.p.i32(arr + 0x0C), 64)
        src_ref = None
        for i in range(n):
            t = self.p.u64(d + i * 8)
            nm = item_name(self.p, t + 0x20)
            if nm == source_name:
                src_ref = t
                break
        if src_ref is None:
            return {"ok": False, "error": "template not found: %s" % source_name,
                    "have": [item_name(self.p, self.p.u64(d + i * 8) + 0x20)
                             for i in range(n)]}
        # 2026-11-10 test: src_ref+0x18 crashed the ctor (sig=6 at 0x1FDC190);
        # reverted until the real CDivisionTemplate/CReferencedDivisionTemplate
        # layering is confirmed. CCreateDivisionTemplateCommand stays disabled.
        src_inner = src_ref + 0x18
        target_name = new_name or (source_name + " Copy")
        import api
        api.allow_call(0x1FDC190)
        with self:
            cmd = self.cmd.new(0x290)
            self.p.write(cmd, b"\0" * 0x290)
            tagbuf = self.malloc(16)
            try:
                self.p.write(tagbuf, _s.pack("<i", c.tag_id) + b"\0" * 12)
                self.call(0x1FDC190, (cmd, src_inner, 0, tagbuf, 0))
                # V2-01: the deep-copied template keeps the source name, so
                # CanExecute rejects the duplicate. Rewrite the embedded
                # std::string at (cmd+0x28)+0x08 before running.
                na = cmd + 0x28 + 0x08
                nb = target_name.encode("utf-8")
                if len(nb) <= 15:
                    self.p.write(na, _s.pack("<QQ", na + 0x10, len(nb)) + nb.ljust(16, b"\0"))
                else:
                    sb = self.malloc(len(nb) + 1)
                    self.p.write(sb, nb + b"\0")
                    self.p.write(na, _s.pack("<QQQ", sb, len(nb), len(nb) + 1))
                rv = self._run_cmd(cmd)
            finally:
                self.free(tagbuf)
        self._settle()
        arr2 = c.ptr + 0x1B8
        n2 = self.p.i32(arr2 + 0x0C)
        names = [item_name(self.p, self.p.u64(self.p.u64(arr2) + i * 8) + 0x20)
                 for i in range(min(n2, 64))]
        return {"ok": bool(rv & 0xFF) or n2 > n, "run": bool(rv & 0xFF),
                "new_name": target_name,
                "templates_before": n, "templates_after": n2,
                "templates": names,
                "error": getattr(self, "_last_cmd_error", "")}

    def template_composition(self, tag=None, only=None):
        """Division templates with their real battalion/company makeup."""
        p = self.p
        c = self._c(tag)
        out = []

        def key_at(addr):
            for off in range(0, 0x80, 8):
                s = p.try_read(addr + off, 32)
                if not s:
                    continue
                q, n = struct.unpack("<QQ", s[:16])
                if 3 <= n <= 40:
                    txt = s[16:16 + n] if q == addr + off + 16 else (p.try_read(q, n) or b"")
                    try:
                        txt = txt.decode()
                    except Exception:
                        continue
                    if txt.replace("_", "").isalpha() and txt.islower():
                        return txt
            return None

        def counts(tp, arr_off):
            hdr = p.try_read(tp + arr_off, 16)
            if not hdr:
                return {}
            data, cap, size = struct.unpack("<Qii", hdr)
            acc = {}
            for k in range(min(size, 40)):
                e = p.try_read(data + 8 * k, 8)
                if not e:
                    break
                ep = struct.unpack("<Q", e)[0]
                kk = key_at(ep)
                if not kk:
                    continue
                acc[kk] = acc.get(kk, 0) + 1
            return acc

        for i, t in enumerate(_arr(p, c.ptr, CO_DIV_TEMPLATES)):
            name = item_name(p, t + DT_NAME)
            if only is not None and str(only) not in (str(i), str(name)):
                continue
            out.append({
                "index": i,
                "name": name,
                "line": counts(t, 0x68),
                "support": counts(t, 0x88),
                "ptr": hex(t),
            })
        return out

    def subunit_types(self, tag=None, filter_text=""):
        """Battalion types, used when building a division template.

        2026-11-10: CCountry+0x26C0 (CO_SUBUNITS) is an EMPTY array in 1.19.4.1 —
        this tool returned count 0 for every country. Fall back to the live
        CSubUnitDefinition pool (vtable scan anchored on a real template).
        """
        p = self.p
        c = self._c(tag)
        out = []
        for s in _arr(p, c.ptr, CO_SUBUNITS):
            k = item_name(p, s + SU_KEY)
            if k and (not filter_text or filter_text.lower() in k.lower()):
                out.append({"key": k, "category": item_name(p, s + SU_CATEGORY), "ptr": hex(s)})
        if not out:
            try:
                import templedit
                pool = templedit.live_pool(self)
                for k, ptr in sorted(pool.items()):
                    if k and (not filter_text or filter_text.lower() in k.lower()):
                        out.append({"key": k, "category": None, "ptr": hex(ptr),
                                    "source": "live_pool"})
            except Exception:
                pass
        return out

    def theatres(self, tag=None):
        p = self.p
        return [{"ptr": hex(t), "class": self.oi.class_of(t)}
                for t in _arr(p, self._c(tag).ptr, CO_THEATRES)]

    # ============================================================ DONANMA
    def fleets(self, tag=None):
        p = self.p
        out = []
        for i, f in enumerate(_arr(p, self._c(tag).ptr, CO_FLEETS)):
            out.append({"index": i, "name": item_name(p, f + FL_NAME), "ptr": hex(f)})
        return out

    # ============================================================ AIR
    def air_wings(self, tag=None, refresh=False):
        """All CAirWing instances in live memory.

        Run 11 (work order 1107): the old version kept only the wings whose first
        0x200 bytes contained the raw CCountry* pointer. CAirWing does not store
        that pointer, so every wing in the game was filtered out and the air force
        never took part in a single run. Ownership is now left to the command
        layer: CAttachAirWingToArmyCommand::CanExecute refuses a foreign wing in
        C++ and returns false instead of corrupting anything.
        """
        if refresh:
            self.oi.build(force=True)
        wings = self.oi.instances("CAirWing")
        # 2026-11-08: the vtable scan returned every object TWICE, 0x10 apart
        # (434 hits for ~217 wings) because a CAirWing carries the same vtable
        # at +0x00 and at +0x10. Keep only the lower address of each pair, and
        # require a readable handle at +0x18 so a stray vtable word in data
        # cannot be mistaken for a wing.
        import struct as _st
        raw = sorted(set(wings))
        hs = set(raw)
        wings = [w for w in raw if (w - 0x10) not in hs]
        good = []
        for w in wings:
            try:
                _t, _i = _st.unpack("<ii", self.p.read(w + 0x18, 8))
            except Exception:
                continue
            if 0 < _i < 100000:
                good.append(w)
        # 2026-11-11: callers cannot do anything with a bare pointer list, and
        # the run that lost the air war had wings but never knew their type. Use
        # the full scanner (id / type / pool / strategic-air group).
        try:
            import airops
            rich = airops.scan_wings(self)
            types = {}
            for w in rich:
                types[w["type"]] = types.get(w["type"], 0) + 1
            return {"count": len(rich), "raw_hits": len(raw), "types": types,
                    "wings": [{"ptr": hex(w["ptr"]), "id": w["id"],
                               "type": w["type"], "pool": hex(w["pool"]),
                               "sa": hex(w["sa"])} for w in rich[:300]]}
        except Exception as e:
            return {"count": len(good), "raw_hits": len(raw), "scan_error": repr(e),
                    "wings": [{"ptr": hex(w)} for w in good[:300]]}

    def attach_air_wings_to_armies(self, tag=None, refresh_oi=False,
                                   allow_unverified=False):
        """Attach air wings to one of the country's armies (COrdersGroup).

        Verified 1.19.4.1 objects: CAttachAirWingToArmyCommand vtable 0x4470E70,
        ctor 0x241F840, size 0x48, +0x28 = CAirWing*, +0x40 = COrdersGroup handle.
        The ctor is called with (this, wing, og, 0) and the command is posted
        through CCommand::Run (0x20AE0A0).

        NOT WIRED INTO THE TICK. The 2026-11-07 canary on a live 1936 game shows
        the command returns "posted" for wings taken from the END of the global
        CAirWing list (9 of the last 24), i.e. it does NOT filter by owner the way
        the work order assumed: CAirWing has no CCountry* anywhere in its first
        0x180 bytes and neither does its CAirWingPool, so there is no cheap way to
        tell a German wing from a Polish one before posting. Attaching 434 wings
        blind could hijack the whole world's air force, so the default refuses and
        only an explicit `allow_unverified=True` runs the loop.
        """
        import api, armyops
        if getattr(self, "_air_attach_broken", False):
            return {"ok": False, "skipped": "circuit_breaker"}
        c = self._c(tag)
        ogs = [og for og in armyops.orders_groups(self, c)
               if not self.p.u8(og + armyops.OG_IS_GROUP)]
        if not ogs:
            return {"ok": False, "error": "no armies (COrdersGroup) to attach air wings to"}
        wings = self.oi.instances("CAirWing")
        if not wings and refresh_oi:
            self.oi.build(force=True)
            wings = self.oi.instances("CAirWing")
        if not wings:
            return {"ok": False, "error": "no CAirWing in the object index",
                    "scanned_wings": 0, "attached_count": 0}
        if not allow_unverified:
            return {"ok": False, "skipped": "wing ownership unverified",
                    "scanned_wings": len(wings), "attached_count": 0,
                    "note": "see the 2026-11-07 canary: no owner field found in "
                            "CAirWing/CAirWingPool; the command accepts foreign wings"}
        already = getattr(self, "_attached_wings_set", None)
        if already is None or getattr(self, "_attached_wings_pid", None) != self.p.pid:
            already = set()
            self._attached_wings_set = already
            self._attached_wings_pid = self.p.pid
        api.allow_call(0x241F840)
        api.allow_call(0x20AE0A0)
        attached, refused = [], 0
        try:
            with self:
                errbuf = self.malloc(0x28)
                for w in wings:
                    if w in already:
                        continue
                    if self.oi.class_of(w) != "CAirWing":
                        continue
                    og = ogs[len(attached) % len(ogs)]
                    cmd = self.cmd.new(0x48)
                    self.p.write(cmd, b"\0" * 0x48)
                    self.call(0x241F840, (cmd, w, og, 0))
                    # C1 (review 2026-11-07): errbuf is a libstdc++ std::string.
                    # Writing 0x20 zero bytes set _M_p = NULL, so the moment
                    # CCommand::Run wrote a refusal reason libstdc++ dereferenced
                    # NULL -> SIGSEGV. A valid SSO empty string is
                    # _M_p = this+0x10, _M_string_length = 0, _M_local_buf[0] = 0.
                    self.p.write(errbuf, struct.pack("<QQB", errbuf + 0x10, 0, 0))
                    rv = self.call(0x20AE0A0, (cmd, errbuf))
                    if rv & 0xFF:
                        already.add(w)
                        attached.append({"wing": hex(w), "army": hex(og)})
                    else:
                        refused += 1
        except Exception as e:
            self._air_attach_broken = True
            return {"ok": False, "error": str(e), "attached_count": len(attached),
                    "scanned_wings": len(wings), "refused": refused}
        return {"ok": bool(attached) or bool(already),
                "scanned_wings": len(wings),
                "attached_count": len(attached),
                "refused": refused,
                "total_attached": len(already),
                "attached": attached[:40]}

    def _supply_front_guard(self, enemy_tag, tag=None, ratio=1.25):
        """How many divisions this front can feed vs how many we would commit.

        Called BEFORE the battle plan is drawn. A plan commits a whole army to a
        front line, and the engine then distributes it over the border provinces
        of that front; if that is more than the front can supply, every division
        on it degrades at once. Returns the numbers either way; the caller
        decides (force=True).
        """
        try:
            import staff_supply as _ss
            fb = _ss.front_budget(self, enemy_tag, tag)
            need = self.divisions(tag, limit=1)["count"]
            cap = fb.get("capacity") or 0
            ok = (not cap) or (need <= cap * ratio)
            return {"ok": ok, "needed": need, "capacity": cap,
                    "ratio": round(need / cap, 2) if cap else None,
                    "per_province": fb.get("per_province"),
                    "border_provinces": len(fb.get("border_provinces") or [])}
        except Exception as e:
            return {"ok": True, "error": repr(e)}

    def _win_battleplans(self, enemy_tag, tag=None, force=False, wait_seconds=0):
        """activate_army_battleplans for hoi4.exe: the chain stage/wden.py proved live.

        create_army -> front line (CFront section facing the enemy) -> offensive
        line under THAT order -> execute -> set the executing flag.

        Two things this learned the hard way:
          * the engine only hands the divisions to the offensive child while the
            simulation is RUNNING.  Building the plan on a paused game produces a
            child with `assigned == 0` and nothing ever moves, so the method
            unpauses for the duration and restores the pause flag afterwards;
          * "posted" is not "created".  Every step is polled and read back.
        """
        import time
        import armyops_win as A
        p = self.p
        enemy_tag = str(enemy_tag).upper()
        out = {"enemy_tag": enemy_tag, "backend": "windows", "steps": {}}

        if not force:
            try:
                enemies = (self.wars(tag) or {}).get("enemies") or []
            except Exception:
                enemies = []
            if enemy_tag not in enemies:
                out.update({"ok": False,
                            "error": "not at war with %s "
                                     "(pass force=true to draw the plan anyway)"
                                     % enemy_tag,
                            "enemies": enemies})
                return out

        idler = p.u64(p.base + 0x33DDB08)
        was_paused = bool(idler and p.u8(idler + 0x6C1))
        out["resumed_for_plan"] = was_paused
        if was_paused:
            p.write(idler + 0x6C1, b"\x00")
        try:
            segs = A.fronts_towards(self, enemy_tag, tag)
            out["steps"]["fronts"] = [{"handle": s["front_handle"], "section": s["section"],
                                       "border": len(s["border_provinces"])} for s in segs]
            if not segs:
                out.update({"ok": False, "error": "no front section faces %s" % enemy_tag})
                return out
            best = max(segs, key=lambda s: len(s["border_provinces"]))
            targets = A.target_provinces(self, enemy_tag, depth=2, own_tag=tag)
            out["steps"]["targets"] = targets[:20]
            if not targets:
                out.update({"ok": False, "error": "no enemy province two steps in"})
                return out

            divs = [int(d["ptr"], 16) for d in (self.divisions(tag) or {}).get("divisions", [])]
            if not divs:
                out.update({"ok": False, "error": "no divisions"})
                return out
            # One group only if every division already lives in the same one;
            # otherwise make a new army (this is what stage/wden.py did -- the
            # 1936 save has 4 divisions in an army and 26 floating).
            ogs = {p.u64(d + A.CARMY_OG) for d in divs}
            og = next(iter(ogs)) if len(ogs) == 1 and 0 not in ogs else 0
            if not og:
                ok, err = A.create_army(self, divs)
                out["steps"]["create_army"] = {"ok": ok, "error": err}
                if not ok:
                    out.update({"ok": False, "error": err})
                    return out
                og = p.u64(divs[0] + A.CARMY_OG)
            out["steps"]["orders_group"] = hex(og)
            out["steps"]["divisions"] = len(A.divisions_of(self, og))

            def wait(fn, seconds=6.0):
                t0 = time.time()
                while time.time() - t0 < seconds:
                    v = fn()
                    if v:
                        return v
                    time.sleep(0.25)
                return None

            handle = tuple(best["front_handle"])
            roots = [o for o in A.orders(self, og)
                     if o["type"] == 2 and tuple(o.get("root_front") or ()) == handle]
            if roots:
                root_id = roots[-1]["id"]
                out["steps"]["front_line"] = {"ok": True, "reused": root_id}
            else:
                before = {o["id"] for o in A.orders(self, og)}
                ok, err, _cmd = A.draw_front_line(self, og, best["front"], best["section"],
                                                  0, A.FULL, divisions=divs)
                fresh = wait(lambda: [o for o in A.orders(self, og)
                                      if o["id"] not in before and o["type"] == 2])
                out["steps"]["front_line"] = {
                    "ok": ok and bool(fresh), "error": err,
                    "new_orders": [o["id"] for o in (fresh or [])]}
                if not fresh:
                    out.update({"ok": False, "error": "no front order appeared"})
                    return out
                root_id = fresh[-1]["id"]

            kids = [c for c in A.order_children(self, og)
                    if c["root"] == root_id and c["type"] == A.ORDER_TYPE_OFFENSIVE]
            if kids and any(k["assigned"] for k in kids):
                out["steps"]["offensive_line"] = {"ok": True, "reused": [k["id"] for k in kids]}
            else:
                ok, err, _cmd = A.offensive_line(self, og, root_id, targets, divs)
                kids = wait(lambda: [c for c in A.order_children(self, og)
                                     if c["root"] == root_id
                                     and c["type"] == A.ORDER_TYPE_OFFENSIVE
                                     and c["assigned"] > 0])
                out["steps"]["offensive_line"] = {
                    "ok": ok and bool(kids), "error": err,
                    "children": [k["id"] for k in (kids or [])]}
                if not kids:
                    out.update({"ok": False,
                                "error": err or "no offensive child with divisions"})
                    return out

            ok, err, _cmd = A.execute_plan(self, og, 0)
            time.sleep(0.5)
            out["steps"]["execute"] = {"ok": ok, "error": err}

            # THE GREEN TICK is the ARROW: the type-2 offensive line carries its
            # own +0x248.  COrderExecuteCommand only sets that byte when it is
            # given an order id AND flag=1 (ctor: [+0x30]=id, [+0x34]=flag,
            # 0x18C8D24), and our helper passes flag=0 -- i.e. `execute(id)` is
            # the toggle OFF.  So set it through the engine's setter instead.
            # Note the delay: the divisions need ~2 game days of planning before
            # the first province falls (measured 2026-11-15, research 70.6).
            arrows = [c for c in A.order_children(self, og)
                      if c["root"] == root_id and c["type"] == 2]
            if arrows:
                arrow_id = arrows[-1]["id"]
                byte = A.set_order_executing(self, arrows[-1]["ptr"], True)
                out["steps"]["activate"] = {"ok": byte == 1, "order": arrow_id}
            out["steps"]["flags"] = A.start_offensives(self, og)
            out["steps"]["children"] = [(c["id"], c["assigned"], c["executing"])
                                        for c in A.order_children(self, og)]
            out["ok"] = all(c[2] == 1 for c in out["steps"]["children"]
                            if c[1] > 0) and bool(out["steps"]["children"])

            if wait_seconds:
                # How long the plan takes to bite.  Measured 2026-11-15 at speed 1:
                # the first enemy province changes hands ~10-11 GAME DAYS after the
                # plan is built -- the divisions have to redeploy onto the front
                # line first.  Every "it never advances" report before that was a
                # window that was simply too short (research 70.6).
                import mapdata
                enemy_provs = set(sum(A.owned_provinces(self, enemy_tag).values(), []))
                t0 = time.time()
                start = None
                gains = 0
                while time.time() - t0 < float(wait_seconds):
                    cm = mapdata.province_controller_map(self)
                    gains = sum(1 for x in enemy_provs if cm.get(x) == (tag or self.player_tag()))
                    if start is None:
                        start = time.time()
                    if gains:
                        break
                    time.sleep(5)
                out["steps"]["advance"] = {
                    "waited": round(time.time() - t0, 1),
                    "enemy_provinces_taken": gains,
                    "date": self.date()["text"],
                }
            return out
        finally:
            if was_paused and idler:
                p.write(idler + 0x6C1, b"\x01")

    def activate_army_battleplans(self, enemy_tag, tag=None, force=False,
                                  wait_seconds=0):
        """One-shot: unassigned divisions -> armies -> front line -> offensive line -> execute.

        2026-11-08 (work order 1108 §3.2). Replaces the run-13 version, which
        assumed an army (COrdersGroup) already existed. A fresh 1936 Germany has
        30 divisions and ZERO armies (`CArmy+0xC0 == 0` everywhere), so the plan
        could never be drawn. This version builds the armies first.

        The Clausewitz command queue is asynchronous: `RUN_CMD` only posts, the
        game main thread executes on its next frame. Therefore every step must
        `detach` (which `create_army`/`draw_front_line`/... already do) and then
        be POLLED from outside the attach session. Never sleep inside `with self:`.
        """
        import time
        if sys.platform == "win32":
            return self._win_battleplans(enemy_tag, tag, force, wait_seconds)
        import armyops
        p = self.p
        co = self._c(tag)
        enemy_tag = str(enemy_tag).upper()
        out = {"enemy_tag": enemy_tag, "steps": {}}
        DRAW_ARROW = True   # 2026-11-08: ctor OE2 fixed (0x2332310) + non-NULL darr

        # ---- step -1: supply guard -------------------------------------------
        guard = self._supply_front_guard(enemy_tag, tag)
        out["supply_guard"] = guard
        if not guard.get("ok") and not force:
            out.update({"ok": False, "blocked_by": "supply",
                        "error": ("this front can feed about %s divisions but the plan would "
                                  "commit %s. A battle plan puts a whole army on the front and "
                                  "the engine spreads it over only those border provinces, so "
                                  "everything on it would degrade together. Narrow the front, "
                                  "deploy to a different border, or pass force=true."
                                  % (guard.get("capacity"), guard.get("needed")))})
            return out

        # ---- step 0: build armies out of the unassigned pool -------------------
        # 2026-11-10: paratroopers are exempt from every automatic land order.
        # A parachute division waiting at its air base looked exactly like an
        # idle division, was collected into a new army and given a front line.
        import divreserve as _dr
        unassigned = [d for d in self.unassigned_divisions(tag)
                      if not _dr.is_paradrop_division(self, d)[0]]
        ogs = [og for og in armyops.orders_groups(self, co)
               if not p.u8(og + armyops.OG_IS_GROUP)]
        _keep, _skip = [], []
        for og in ogs:
            try:
                _pd, _why = _dr.army_is_paradrop(self, og)
            except Exception:
                _pd, _why = False, None
            if _pd:
                _skip.append({"army": hex(og), "reason": _why})
            else:
                _keep.append(og)
        ogs = _keep
        out["steps"]["skipped_paradrop_armies"] = _skip
        out["steps"]["before"] = {"unassigned": len(unassigned), "armies": len(ogs)}

        if not ogs and unassigned:
            created = []
            while unassigned:
                batch, unassigned = unassigned[:24], unassigned[24:]
                before_set = set(armyops.orders_groups(self, co))
                r = self.create_army(batch, tag=tag)
                got = None
                for _ in range(20):
                    time.sleep(0.15)
                    now = set(armyops.orders_groups(self, co))
                    if len(now) > len(before_set):
                        got = (now - before_set).pop()
                        break
                created.append({"divisions": len(batch), "ok": r.get("ok"),
                                "error": r.get("error"), "army": hex(got) if got else None})
            out["steps"]["create_army"] = created
            ogs = [og for og in armyops.orders_groups(self, co)
                   if not p.u8(og + armyops.OG_IS_GROUP)]

            # commanders: one per new army, never the same man twice
            try:
                gens = [x for x in self.available_generals(tag).get("generals", [])
                        if x.get("leader_ptr")]
                corps = [x for x in gens if x.get("rank") == "corps_commander"] or gens
                used = {p.u64(o + armyops.OG_LEADER) for o in ogs
                        if p.u64(o + armyops.OG_LEADER)}
                led = []
                for og in ogs:
                    if p.u64(og + armyops.OG_LEADER):
                        continue
                    cand = next((x for x in corps
                                 if int(x["leader_ptr"], 16) not in used), None)
                    if not cand:
                        break
                    lp = int(cand["leader_ptr"], 16)
                    ok_l, err_l = armyops.set_army_leader(self, og, lp)
                    time.sleep(0.15)
                    if ok_l:
                        used.add(lp)
                        led.append({"army": hex(og), "leader": cand.get("name")})
                out["steps"]["leaders"] = led
            except Exception as e:
                out["steps"]["leaders"] = {"error": str(e)}

        if not ogs:
            out.update({"ok": False, "error": "no armies after create_army attempt",
                        "unassigned_count": len(self.unassigned_divisions(tag)),
                        "hint": "the game must be UNPAUSED so CCommandQueue drains"})
            return out

        out["steps"]["after"] = {"armies": len(ogs),
                                 "sizes": [p.i32(o + armyops.OG_DIVISIONS + 0xC) for o in ogs]}

        # ---- step 1: the CFront facing the enemy -------------------------------
        segs = armyops.fronts_towards(self, enemy_tag, co.tag)
        if not segs:
            out.update({"ok": False, "error": "no CFront facing %s" % enemy_tag,
                        "armies": len(ogs)})
            return out
        uniq, seen = [], set()
        for s in segs:
            if s["front"] not in seen:
                seen.add(s["front"])
                uniq.append(s)
        target_provs = armyops.target_line(self, enemy_tag, depth="full", own_tag=co.tag)
        out["steps"]["fronts"] = {"sections": len(segs), "unique_fronts": len(uniq),
                                  "target_provinces": len(target_provs)}
        if not target_provs:
            out.update({"ok": False, "error": "empty offensive target line"})
            return out

        results = []
        for idx, og in enumerate(ogs):
            s = uniq[idx % len(uniq)]
            root_orders = [o for o in armyops.orders_of(self, og) if o["type"] == 2]
            if not root_orders:
                try:
                    # 2026-11-08 (run 17): the divisions MUST be handed to the root
                    # front order AT CREATION. Drawing with `None` produces an order
                    # with assigned_divisions == 0, and COrderAssignCommand cannot
                    # top it up afterwards (verified live: assign_to_order returns
                    # True and the order keeps 0). With the army's divisions in the
                    # ctor argument the same call produced assigned_divisions == 24.
                    _divs = armyops.group_divisions(self, og)
                    ok_f, err_f = armyops.draw_front_line(
                        self, og, s["front"], s["section"], 0, armyops.FULL, _divs, 0)
                except Exception as e:
                    ok_f, err_f = False, "%s: %s" % (type(e).__name__, e)
                if not ok_f:
                    results.append({"army": hex(og), "step": "draw_front_line",
                                    "ok": False, "error": err_f})
                    continue
                for _ in range(20):
                    time.sleep(0.15)
                    root_orders = [o for o in armyops.orders_of(self, og)
                                   if o["type"] == 2]
                    if root_orders:
                        break
            if not root_orders:
                results.append({"army": hex(og), "step": "wait_root_order", "ok": False,
                                "error": "root order never appeared in OG_ORDERS"})
                continue
            parent_id = root_orders[0]["id"]

            # top up to 24 with any divisions still unassigned
            cur = p.i32(og + armyops.OG_DIVISIONS + 0xC)
            spare = [d for d in self.unassigned_divisions(tag)
                     if not _dr.is_paradrop_division(self, d)[0]]
            if cur < 24 and spare:
                try:
                    armyops.assign_to_order(self, og, parent_id, spare[:24 - cur])
                except Exception:
                    pass
                time.sleep(0.15)

            # 2026-11-08 (work order 1108(1) §2.1): the offensive arrow and the
            # green tick are NOT drawn here any more. `COrderNewFrontCommand`
            # (0x232CC20) still SIGSEGVs on argument semantics and
            # `COrderExecuteCommand` dies inside RUN_CMD at 0x20AE17E because the
            # command's vtable slot is still NULL (see `cmd_ok` below) — one bad
            # command poisons the rest of the pipeline. A front line alone is
            # enough for the engine to rail every division of that army to the
            # border and dig in, which is what we actually need before attacking.
            if DRAW_ARROW:
                off = [o for o in armyops.orders_of(self, og) if o["type"] == 1]
                ok_o, err_o = True, ""
                if not off:
                    try:
                        ok_o, err_o = armyops.offensive_line(self, og, parent_id, target_provs)
                    except Exception as e:
                        ok_o, err_o = False, "%s: %s" % (type(e).__name__, e)
                try:
                    ok_e, err_e = armyops.execute_plan(self, og, 0, 0)
                except Exception as e:
                    ok_e, err_e = False, "%s: %s" % (type(e).__name__, e)
            else:
                ok_o, err_o, ok_e, err_e = None, "disabled (arrow not implemented)", \
                                           None, "disabled (arrow not implemented)"
            results.append({"army": hex(og), "root_order_id": parent_id,
                            "divisions": p.i32(og + armyops.OG_DIVISIONS + 0xC),
                            "front_line_ok": True,
                            "offensive_ok": ok_o, "offensive_err": err_o,
                            "execute_ok": ok_e, "execute_err": err_e})

        out["results"] = results
        out["ok"] = bool(results) and all(r.get("front_line_ok") for r in results)
        return out

    # ========================================================== DIPLOMACY
    def relations(self, tag=None, limit=60):
        """Relation records with other countries (array index = country index)."""
        p = self.p
        c = self._c(tag)
        self._load_tags()
        ptrs = self.country_ptrs()
        idx_to_tag = {}
        for tid, t in enumerate(self._tags):
            if t and t != "---":
                idx_to_tag[self._tag2idx[tid]] = t
        out = []
        for i, r in enumerate(_arr(p, c.ptr, CO_RELATIONS_ARR)):
            t = idx_to_tag.get(i)
            if not t:
                continue
            out.append({"tag": t, "ptr": hex(r), "index": p.i32(r + REL_INDEX)})
            if len(out) >= limit:
                break
        return out

    FACTION_VT_RVA = 0x4353758      # NFactions::CFaction vtable, 1.19.4.1

    def _faction_objects(self):
        """Every live NFactions::CFaction (vtable scan; cached per game pid).

        oi.instances("CFaction") finds nothing — the real RTTI name is
        `N9NFactions8CFactionE`, which the object index does not map — so the
        first call walks the writable regions for the vtable pointer (~12 s,
        once per process) and caches the result.
        """
        pid = self.p.pid
        cache = getattr(self, "_faction_objects_cache", None)
        if cache and cache[0] == pid:
            return cache[1]
        needle = struct.pack("<Q", self.p.base + self.FACTION_VT_RVA)
        hits = []
        for m in self.p.regions(writable=True):
            a, b = m["start"], m["end"]
            pos = a
            while pos < b:
                n = min(8 << 20, b - pos)
                try:
                    data = self.p.read(pos, n)
                except Exception:
                    pos += n
                    continue
                st = 0
                while True:
                    i = data.find(needle, st)
                    if i < 0:
                        break
                    hits.append(pos + i)
                    st = i + 8
                pos += n
        hits = sorted(set(hits))
        self._faction_objects_cache = (pid, hits)
        return hits

    def _faction_ptr(self, tag=None):
        """The CFaction this country belongs to, or 0.

        2026-11-11: CO_FACTION (+0x2B00) is a 1.19.2 offset; in 1.19.4.1 that
        word points at a shared membership object, not at the faction (GER and
        ITA both read the same pointer, whose vtable is not CFaction), which is
        why hoi4_faction returned garbage. Membership decided which of the three
        real factions (Axis / Allies / Comintern) owns the country.
        """
        p = self.p
        c = self._c(tag)
        cache = getattr(self, "_faction_ptr_cache", None)
        key = (p.pid, c.tag)
        if cache and cache[0] == key:
            return cache[1]
        found = 0
        for f in self._faction_objects():
            try:
                if c.ptr in _arr(p, f, FACTION_MEMBERS):
                    found = f
                    break
            except Exception:
                continue
        self._faction_ptr_cache = (key, found)
        return found

    def faction(self, tag=None):
        p = self.p
        f = self._faction_ptr(tag)
        if not f:
            return None
        nm = None
        for off in range(0, 0x200, 8):
            s = item_name(p, f + off)
            if s and s.isprintable() and 2 < len(s) < 48 and " " not in s[:1]:
                nm = s
                break
        members = []
        for m in _arr(p, f, FACTION_MEMBERS):
            cc = self.country_by_ptr(m)
            members.append(cc.tag if cc else hex(m))
        leader = 0
        try:
            leader = p.u64(f + FACTION_MEMBERS - 8)
        except Exception:
            pass
        lc = self.country_by_ptr(leader) if leader else None
        return {"ptr": hex(f), "name": nm, "class": self.oi.class_of(f),
                "members": members,
                "leader": lc.tag if lc else (members[0] if members else None)}

    # ========================================================== CHARACTERS
    VT_ARMY_LEADER = 0x43D5890   # 11CArmyLeader vtable, 1.19.4.1 (rtti_index.csv)

    def _extract_char_key_and_name(self, ch: int):
        """(key, name) of a CCharacter, tolerant of the 1.19.x layout shifts.

        CH_KEY/CH_NAME are correct for 1.19.4.1 (checked live), but the fallback scan
        keeps this working if a future patch inserts a field before them — the key is
        always a `TAG_something` token and the name a printable string.
        """
        p = self.p
        key = item_name(p, ch + CH_KEY)
        name = item_name(p, ch + CH_NAME)
        if key and 2 < len(key) < 64 and "_" in key:
            return key, (name or key)
        found_key, found_name = None, None
        for off in range(0x10, 0x78, 8):
            s = item_name(p, ch + off)
            if not s or not s.isprintable() or not (3 <= len(s) <= 64):
                continue
            head = s.split("_", 1)[0]
            if "_" in s and 2 <= len(head) <= 4 and head.isalpha() and head.isupper():
                found_key = s
            elif found_name is None:
                found_name = s
        return found_key, (found_name or found_key or key)

    def _find_army_leader_ptr(self, ch: int) -> int:
        """CArmyLeader* inside a CCharacter, found by its vtable, not by an offset.

        `CH_ARMY_LEADER = 0xC0` is where 1.19.4.1 keeps it, but that is exactly the
        kind of offset that moves when a class grows (CScientist was added in this
        version). Matching the CArmyLeader vtable over +0x50..+0x150 is immune to it.
        """
        p = self.p
        target = p.va(self.VT_ARMY_LEADER)
        raw = p.try_read(ch + 0x50, 0x100)
        if not raw:
            return 0
        for i in range(0, len(raw) - 7, 8):
            ptr = struct.unpack_from("<Q", raw, i)[0]
            if not (0x10000 < ptr < 0x7FFFFFFFFFFF):
                continue
            head = p.try_read(ptr, 8)
            if head and struct.unpack("<Q", head)[0] == target:
                return ptr
        return 0


    VT_ADVISOR = 0x4349AD8   # 8CAdvisor vtable, 1.19.4.1 (rtti.pkl)

    def _advisor_pool(self):
        """{character_key: CAdvisor*} for the live process.

        Probed 2026-11-10: a CAdvisor is NOT stored inside its CCharacter (a
        +0x000..+0x220 dump only shows CCharacterTemplate / CUnitMedalStore).
        Advisors live in their own array; each one carries its character key as a
        std::string at +0x30 (e.g. "GER_werner_von_blomberg"). So collect every
        object whose vtable is 8CAdvisor and index it by that string.
        """
        p = self.p
        cache = getattr(self, "_adv_pool_cache", None)
        if cache and cache[0] == p.pid and cache[1]:
            return cache[1]
        target = p.va(self.VT_ADVISOR)
        needle = struct.pack("<Q", target)
        found = {}
        for m in p.regions(writable=True):
            a, b = m["start"], m["end"]
            cur = a
            while cur < b:
                n = min(4 << 20, b - cur)
                try:
                    buf = p.try_read(cur, n)
                except Exception:
                    buf = None
                if buf:
                    k = 0
                    while True:
                        k = buf.find(needle, k)
                        if k < 0:
                            break
                        addr = cur + k
                        try:
                            key = p.stdstring(addr + 0x30)
                        except Exception:
                            key = None
                        if key and 3 < len(key) < 64 and key not in found:
                            found[key] = addr
                        k += 8
                cur += n
        if found:
            self._adv_pool_cache = (p.pid, found)
        return found

    def find_advisor(self, key, tag=None):
        """CAdvisor* for a character key / idea token / bare name."""
        pool = self._advisor_pool()
        if not pool:
            return 0
        k = (key or "").lower()
        for cand in (k, k.upper()):
            if pool.get(cand):
                return pool[cand]
        for name, addr in pool.items():
            nl = name.lower()
            if nl == k or nl.endswith("_" + k):
                return addr
        return 0

    def _find_advisor_ptr(self, ch: int) -> int:
        """Kept for compatibility: advisors are not inside the character."""
        return 0

    def _find_advisor_ptr(self, ch: int) -> int:
        """CAdvisor* inside a CCharacter, found by vtable instead of a fixed offset.

        V2-04: advisors hang off the character the same way CArmyLeader does at
        +0xC0; matching the 8CAdvisor vtable over +0x50..+0x160 is immune to the
        field moving between patches.
        """
        p = self.p
        target = p.va(self.VT_ADVISOR)
        raw = p.try_read(ch + 0x50, 0x120)
        if not raw:
            return 0
        for i in range(0, len(raw) - 7, 8):
            ptr = struct.unpack_from("<Q", raw, i)[0]
            if not (0x10000 < ptr < 0x7FFFFFFFFFFF):
                continue
            head = p.try_read(ptr, 8)
            if head and struct.unpack("<Q", head)[0] == target:
                return ptr
        return 0

    VT_CCHARACTER = 0x4349390   # 10CCharacter vtable, 1.19.4.1 (rtti.pkl)

    G_PROVINCE_MANAGER = 0x459E9F0   # global -> manager singleton (vtable+0x50 = GetProvince(id))
    VT_PROVINCE_MGR    = 0x50        # slot offset inside the manager vtable

    def province_ptr(self, province_id):
        """CProvince* for a province id, via the engine's own GetProvince(id).

        Found 2026-11-10 by disassembling 0xda7410:
            rax = [0x459e9f0]            ; manager singleton
            fn  = [[rax]+0x50]           ; virtual GetProvince(int)
        This is exactly the object COrderSetParadropTargetCommand wants as arg1.
        """
        p = self.p
        mgr = p.u64(self.va(self.G_PROVINCE_MANAGER))
        if not mgr:
            return 0
        vt = p.u64(mgr)
        if not vt:
            return 0
        fn = p.u64(vt + self.VT_PROVINCE_MGR)
        if not fn:
            return 0
        import api
        api.allow_call(fn - self.base)
        # thiscall: rdi = manager singleton, esi = province id
        return self.call(fn - self.base, (mgr, int(province_id)))

    def _scan_vtable(self, vt_off):
        """All live objects whose first qword is `va(vt_off)`, cached per pid.

        2026-11-10: objindex.pkl is bound to ONE game process; after a restart
        every address in it is dead, which is why hoi4_generals returned 0 here.
        Scanning the writable heap for the class vtable is pid-proof.
        """
        p = self.p
        cache = getattr(self, "_vt_scan_cache", None)
        if cache and cache[0] == p.pid and vt_off in cache[1]:
            return cache[1][vt_off]
        target = p.va(vt_off)
        needle = struct.pack("<Q", target)
        found = []
        for m in p.regions(writable=True):
            a, b = m["start"], m["end"]
            cur = a
            while cur < b:
                n = min(4 << 20, b - cur)
                try:
                    buf = p.try_read(cur, n)
                except Exception:
                    buf = None
                if buf:
                    i = 0
                    while True:
                        i = buf.find(needle, i)
                        if i < 0:
                            break
                        found.append(cur + i)
                        i += 8
                cur += n
        if not hasattr(self, "_vt_scan_cache") or self._vt_scan_cache[0] != p.pid:
            self._vt_scan_cache = (p.pid, {})
        self._vt_scan_cache[1][vt_off] = found
        return found

    def characters(self, tag=None, limit=5000):
        """The country's characters: generals, admirals, advisors, leaders."""
        p = self.p
        c = self._c(tag)
        pref = (c.tag + "_").lower()
        out = []
        try:
            chars = self.all_characters(c.tag)
        except Exception:
            chars = []
        if not chars:
            # objindex is process-bound: after any restart it yields nothing.
            chars = self._scan_vtable(self.VT_CCHARACTER)
        for ch in chars:
            k, nm = self._extract_char_key_and_name(ch)
            if k and k.lower().startswith(pref):
                out.append({"key": k, "name": nm, "ptr": hex(ch)})
                if len(out) >= limit:
                    break
        return {"count": len(out), "characters": out}

    # ============================================================ GENERALS
    CH_ARMY_LEADER = 0xC0   # CArmyLeader*, set for generals and admirals
    AL_SKILL       = 0x28
    AL_ATTACK      = 0x30
    AL_DEFENSE     = 0x48
    AL_PLANNING    = 0x50
    AL_LOGISTICS   = 0xF8

    def _character_manager(self):
        return self.oi.singleton("CCharacterManager")

    def all_characters(self, tag=None):
        p = self.p
        c = self._c(tag)
        mgr = self._character_manager()
        pref = (c.tag + "_").lower()
        out = []
        for ch in _arr(p, mgr, 0x10):
            k = item_name(p, ch + CH_KEY)
            if not k:
                k, _nm = self._extract_char_key_and_name(ch)
            if k and k.lower().startswith(pref):
                out.append(ch)
        return out

    def generals(self, tag=None):
        """The country's generals and admirals: name, skills, pointer."""
        p = self.p
        out = []
        try:
            chars = self.all_characters(tag)
        except Exception:
            chars = []
        if not chars:
            # CCharacterManager may still be empty right after a campaign starts;
            # fall back to every CCharacter in the object index (no 80-row cap).
            chars = [int(row["ptr"], 16)
                     for row in self.characters(tag, limit=5000)["characters"]]
        for ch in chars:
            al = p.u64(ch + self.CH_ARMY_LEADER)
            if not al:
                al = self._find_army_leader_ptr(ch)
            if not al:
                continue
            out.append({
                "key": item_name(p, ch + CH_KEY),
                "name": item_name(p, ch + CH_NAME),
                "character_ptr": hex(ch),
                "leader_ptr": hex(al),
                # Skills: CArmyLeader +0xE58/+0xE68/+0xE78/+0xE88.
                # checked one-to-one against common/characters/GER.txt for 42 German
                # verified, and these are EFFECTIVE values with trait bonuses included.
                "attack": p.i32(al + 0xE58),
                "defense": p.i32(al + 0xE68),
                "planning": p.i32(al + 0xE78),
                "logistics": p.i32(al + 0xE88),
                "rank": legality.RANKS.get(p.i32(al + legality.AL_RANK), "?"),
            })
        # mark the commanders the player can actually use right now
        for row in out:
            ok, why = legality.leader_available(self, row["key"], tag)
            row["available"] = ok
            if not ok:
                row["unavailable_reason"] = why
        avail = [r for r in out if r["available"]]
        return {"count": len(out), "available_count": len(avail), "generals": out}

    def available_generals(self, tag=None, rank=None):
        """Only commanders that can be assigned right now, as the player sees them."""
        rows = [r for r in self.generals(tag)["generals"] if r["available"]]
        if rank:
            rows = [r for r in rows if r["rank"] == rank]
        rows.sort(key=lambda r: -(r["attack"] + r["defense"] + r["planning"] + r["logistics"]))
        return {"count": len(rows), "generals": rows}

    # ---------------------------------------------------------- armies and orders
    def order_groups(self, tag=None):
        """The country's ARMIES and ARMY GROUPS (COrdersGroup)."""
        import armyops
        p = self.p
        co = self.country(tag) if tag else self.player()
        out, seen = [], set()
        for og in armyops.orders_groups(self, co):
            seen.add(og)
            out.append(armyops.og_info(self, og))
            parent = p.u64(og + armyops.OG_PARENT)
            if parent and parent not in seen:
                seen.add(parent)
                out.append(armyops.og_info(self, parent))
        return {"count": len(out), "groups": out}

    def unassigned_divisions(self, tag=None):
        """Divisions not attached to any army (CArmy*)."""
        co = self.country(tag) if tag else self.player()
        return [d for d in _arr(self.p, co.ptr, 0x290)
                if self.p.u64(d + 0xC0) == 0]

    def create_army(self, divisions=None, count=None, tag=None):
        """Collect divisions into a new army (the '+' button in the UI).

        The command is QUEUED (CCommand::Run) and only takes effect when the game
        advances a tick. In a paused game the old code reported success while no
        army existed, and a second call in the same paused tick took the SAME
        divisions again and silently did nothing. Both are reported honestly now.
        """
        if sys.platform == "win32":
            import armyops_win as armyops
        else:
            import armyops
        def _n():
            try:
                return (self.order_groups(tag) or {}).get("count")
            except Exception:
                return None
        before = _n()
        divs = divisions or self.unassigned_divisions(tag)
        if count:
            divs = divs[:count]
        if not divs:
            return {"ok": False, "error": "no divisions to assign"}
        ok, err = armyops.create_army(self, divs)
        after = _n()
        res = {"ok": ok, "error": err, "divisions": len(divs),
               "armies_before": before, "armies_after": after}
        if sys.platform == "win32":
            # armyops_win calls 0xC417A0 directly, so the army exists the moment
            # the call returns -- no queue, no tick, nothing to wait for.
            return res
        if ok and before is not None and after is not None and after <= before:
            res["queued"] = True
            res["note"] = ("army count %s -> %s: the command is queued and lands on the "
                           "next game tick; do not issue a second create_army in the "
                           "same paused tick, it would take the same divisions again"
                           % (before, after))
        return res

    def create_army_group(self, armies=None, tag=None):
        """Collect armies into a new army group (field marshal command)."""
        import armyops
        p = self.p
        if armies is None:
            armies = [og for og in armyops.orders_groups(self, self.country(tag) if tag else None)
                      if not p.u8(og + armyops.OG_IS_GROUP) and not p.u64(og + armyops.OG_PARENT)]
        if len(armies) < 1:
            return {"ok": False, "error": "no armies to group"}
        ok, err = armyops.create_army_group(self, armies)
        return {"ok": ok, "error": err, "armies": len(armies)}

    def assign_commander(self, orders_group, character_key=None, leader_ptr=None,
                         allow_unverified=False):
        """Assign a commander to an army or army group.

        Player rules are enforced: only a FIELD MARSHAL may lead an army group, only a
        corps commander. If the character's `visible` condition is not met,
        For example a general not visible under the current government is REFUSED.
        """
        import armyops
        key = character_key
        if leader_ptr is None:
            for gen in self.generals()["generals"]:
                if gen["key"] == character_key or gen["name"] == character_key:
                    leader_ptr = int(gen["leader_ptr"], 16)
                    key = gen["key"]
                    break
            else:
                return {"ok": False, "error": "general bulunamadi: %s" % character_key}
        ok, why = legality.check_commander(self, orders_group, leader_ptr, key, allow_unverified)
        if not ok:
            return {"ok": False, "error": why, "rejected_by": "player_rules"}
        ok, err = armyops.set_army_leader(self, orders_group, leader_ptr)
        return {"ok": ok, "error": err}

    def border_fronts(self, enemy_tag, tag=None):
        """Front sections facing the target country, for drawing a line."""
        if sys.platform == "win32":
            import armyops_win
            segs = armyops_win.fronts_towards(self, enemy_tag, tag)
            return [{"front_ptr": hex(s["front"]), "front_handle": s["front_handle"],
                     "section": s["section"],
                     "border_provinces": s["border_provinces"]}
                    for s in segs]
        import armyops
        segs = armyops.fronts_towards(self, enemy_tag, tag)
        return [{"front_ptr": hex(s["front"]), "front_handle": s["front_handle"],
                 "section": s["section"], "border_provinces": s["border_provinces"]}
                for s in segs]

    def draw_front_line(self, orders_group, enemy_tag=None, front_ptr=None,
                        section=None, frm=0, to=100000, assign="proportional"):
        """Give an army a front line order. With enemy_tag, the WHOLE border is covered."""
        if sys.platform == "win32":
            # The PE build draws a front line by calling COrderNewRootCommand with
            # a CFront* and a CPdxArray<CArmy*>; the Linux arm-then-verify dance
            # (settle + poll for the queued order) does not apply, because the
            # engine appends the order synchronously inside Post.
            import armyops_win
            divs = armyops_win.divisions_of(self, orders_group)
            if front_ptr is not None:
                fp = int(front_ptr, 16) if isinstance(front_ptr, str) else int(front_ptr)
                ok, err, _cmd = armyops_win.draw_front_line(
                    self, orders_group, fp, section, frm, to, divisions=divs)
                return {"ok": ok, "error": err, "divisions": len(divs)}
            if not enemy_tag:
                return {"ok": False, "error": "enemy_tag or front_ptr is required"}
            seen, res, new_ids = set(), [], []
            for s in armyops_win.fronts_towards(self, enemy_tag):
                fk = tuple(s["front_handle"])
                if fk in seen:
                    continue
                seen.add(fk)
                before = {o["id"] for o in armyops_win.orders(self, orders_group)}
                ok, err, _cmd = armyops_win.draw_front_line(
                    self, orders_group, s["front"], s["section"], frm, to,
                    divisions=divs)
                fresh = [o["id"] for o in armyops_win.orders(self, orders_group)
                         if o["id"] not in before and o["id"] not in new_ids]
                if ok and not fresh:
                    ok = False
                    err = "the command posted but no order appeared"
                new_ids += fresh
                res.append({"front": s["front_handle"], "section": s["section"],
                            "ok": ok, "error": err,
                            "weight": len(s["border_provinces"]),
                            "new_orders": fresh})
            return {"ok": all(r["ok"] for r in res) if res else True,
                    "segments": res, "new_order_ids": new_ids,
                    "divisions": len(divs)}
        import armyops
        res = []
        if front_ptr is not None:
            fp = int(front_ptr, 16) if isinstance(front_ptr, str) else int(front_ptr)
            ok, err = armyops.draw_front_line(self, orders_group, fp, section, frm, to)
            return {"ok": ok, "error": err}
        if not enemy_tag:
            return {"ok": False, "error": "enemy_tag or front_ptr is required"}
        have = {(tuple(o["root_front"]), o["root_section"])
                for o in armyops.orders_of(self, orders_group)}
        segs = armyops.fronts_towards(self, enemy_tag)
        # 2026-11-08: the 7-arg COrderNewRootCommand ctor takes a CFront* and
        # covers the WHOLE front, so one order per CFront is enough. Drawing one
        # per section would stack duplicate orders on the same front.
        seen = set()
        known = {o["id"] for o in armyops.orders_of(self, orders_group)}
        new_ids = []
        for s in segs:
            fk = tuple(s["front_handle"])
            if fk in seen:
                continue
            seen.add(fk)
            key = (fk, s["section"])
            if key in have:
                res.append({"front": s["front_handle"], "section": s["section"],
                            "ok": True, "error": "already exists"})
                continue
            ok, err = armyops.draw_front_line(self, orders_group, s["front"], None, frm, to)
            # 2026-11-11: CCommand::Run returning true only means "queued for this frame".
            # Round 3 got 31 x ok:true here and the army still had zero orders, so the
            # next call died with "no front order to assign to" — a fake success.
            # Poll the army's order array and refuse to report an order that the
            # engine never created.
            fresh = []
            if ok:
                for _ in range(6):
                    self._settle(0.25)
                    now = armyops.orders_of(self, orders_group)
                    fresh = [o for o in now
                             if o["id"] not in known and o["id"] not in new_ids]
                    if fresh:
                        break
                if not fresh:
                    ok = False
                    err = ((err + "; ") if err else "") + (
                        "the command was accepted but the engine created no order")
            for o in fresh:
                new_ids.append(o["id"])
            res.append({"front": s["front_handle"], "section": s["section"],
                        "ok": ok, "error": err, "weight": len(s["border_provinces"]),
                        "new_orders": [o["id"] for o in fresh]})
        out = {"ok": all(r["ok"] for r in res) if res else True, "segments": res,
               "new_order_ids": new_ids}
        if assign and assign != "none":
            out["assignment"] = self.assign_divisions_to_front(
                orders_group, enemy_tag, mode=assign, order_ids=new_ids or None)
        return out

    def assign_divisions_to_front(self, orders_group, enemy_tag=None, mode="proportional",
                                  divisions=None, order_ids=None):
        """Distribute an army's divisions across its front orders.

        mode: "proportional" (by front section length, the default),
              "even", "all" (every division to every order), or "none".
        Use mode="none" to leave divisions unassigned for manual placement later.
        """
        import armyops
        p = self.p
        self._assign_note = None
        if mode == "none":
            return {"ok": True, "note": "divisions left unassigned"}
        orders = armyops.orders_of(self, orders_group)
        if order_ids:
            # 2026-11-11: match by the ids the draw call actually produced. Matching by
            # `(root_front, section)` handle silently dropped every order (the engine
            # stores a different root handle than the CFront we drew on) and produced
            # the misleading "no front order to assign to".
            want_ids = {int(x) for x in order_ids}
            orders = [o for o in orders if o["id"] in want_ids]
            weights = [1] * len(orders)
        elif enemy_tag:
            want = {(tuple(s["front_handle"]), s["section"]): len(s["border_provinces"])
                    for s in armyops.fronts_towards(self, enemy_tag)}
            matched = [o for o in orders
                       if (tuple(o["root_front"]), o["root_section"]) in want]
            if matched:
                orders = matched
                weights = [want[(tuple(o["root_front"]), o["root_section"])] for o in orders]
            else:
                # 2026-11-11: the engine re-sections a front when it is drawn, so the
                # handle/section pair recorded at draw time no longer matches the
                # surviving order and every division was refused with the misleading
                # "no front order to assign to". An army's own orders are the right
                # target anyway; keep them and say so.
                self._assign_note = ("front handle filter matched nothing; used all %d "
                                     "order(s) of this army" % len(orders))
                weights = [1] * len(orders)
        else:
            weights = [1] * len(orders)
        if not orders:
            return {"ok": False, "error": "no front order to assign to"}
        divs = list(divisions) if divisions else armyops.group_divisions(self, orders_group)
        if not divs:
            return {"ok": False, "error": "the army has no divisions"}
        n = len(divs)
        if mode == "all":
            shares = [divs] * len(orders)
        else:
            if mode == "even":
                weights = [1] * len(orders)
            tot = sum(weights) or 1
            counts, used = [], 0
            for i, w in enumerate(weights):
                k = n - used if i == len(weights) - 1 else max(1, round(n * w / tot))
                k = min(k, n - used - (len(weights) - 1 - i))
                counts.append(max(0, k)); used += counts[-1]
            shares, at = [], 0
            for k in counts:
                shares.append(divs[at:at + k]); at += k
        out = []
        for o, share in zip(orders, shares):
            if not share:
                continue
            def _count():
                for x in armyops.orders_of(self, orders_group):
                    if x["id"] == o["id"]:
                        return x.get("assigned_divisions")
                return None
            before = _count()
            ok, err = armyops.assign_to_order(self, orders_group, o["id"], share)
            self._settle(0.4)
            after = _count()
            # Only a completely empty order is proof of a fake success; re-assigning
            # the same divisions legitimately keeps the count unchanged.
            if ok and not after:
                ok = False
                err = "the command was accepted but the order still holds 0 divisions"
            out.append({"order_id": o["id"], "divisions": len(share), "ok": ok,
                        "error": err, "assigned_before": before, "assigned_after": after})
        res = {"ok": all(x["ok"] for x in out), "mode": mode, "orders": out}
        note = getattr(self, "_assign_note", None)
        if note:
            res["note"] = note
            self._assign_note = None
        return res

    def division_detail(self, tag=None, limit=200, order_group=None):
        """Divisions one by one, using the game's own field names: organisation, strength,
        experience, entrenchment, fuel, supply, parent army, template, location."""
        import fieldmap, armyops, unitorders
        p = self.p
        fm = fieldmap.field_map(self, "CArmy")
        keep = {"organisation", "strength", "experience", "dig_in", "dig_in_cap",
                "fuel", "max_supply", "army_current_supply_ratio", "out_of_supply_days",
                "str_damage", "org_damage", "killed"}
        if order_group:
            divs = armyops.group_divisions(self, order_group)
        else:
            divs = list(_arr(p, self._c(tag).ptr, CO_ARMIES))
            # Divisions assigned to an army group are no longer in the country
            # array; without this union a fully grouped army reads as empty
            # (measured 1944-06: 225 divisions on the map, 0 returned).
            seen = set(divs)
            try:
                for og in (self.order_groups(tag).get("groups") or []):
                    gptr = og.get("ptr")
                    if isinstance(gptr, str):
                        gptr = int(gptr, 16)
                    if not gptr:
                        continue
                    try:
                        for d in armyops.group_divisions(self, gptr):
                            if d not in seen:
                                seen.add(d)
                                divs.append(d)
                    except Exception:
                        continue
            except Exception:
                pass
        out = []
        lost = 0
        for i, a in enumerate(divs[:limit]):
            # One division that dies / merges mid-read used to abort the whole
            # loop: 225 divisions on the map became 0 and staff_army trained 108
            # phantom divisions on the empty answer.
            try:
                if not a or a < 0x10000 or not p.u64(a):
                    lost += 1
                    continue
                tmpl = p.u64(a + 0x3B8)
                og = p.u64(a + armyops.CARMY_OG)
                row = {"index": i, "ptr": hex(a),
                       "handle": [p.i32(a + 0x18), p.i32(a + 0x1C)],
                       "province": unitorders.province_of(self, a),
                       "moving": bool(unitorders.movement(self, a)),
                       "template": item_name(p, tmpl + DT_NAME) if tmpl else None,
                       "army": (p.stdstring(og + armyops.OG_NAME) if og else None),
                       "army_ptr": hex(og) if og else None}
                row.update(fieldmap.read_fields(self, a, fm, keep))
                out.append(row)
            except Exception:
                lost += 1
                continue
        res = {"count": len(divs), "divisions": out}
        if lost:
            res["skipped"] = lost
        return res

    def orders_of(self, orders_group):
        if sys.platform == "win32":
            import armyops_win
            return armyops_win.orders(self, orders_group)
        import armyops
        return armyops.orders_of(self, orders_group)

    def execute_plan(self, orders_group, order_id=0):
        """Execute the battle plan (the green button in the UI)."""
        if sys.platform == "win32":
            import armyops_win
            ok, err, _cmd = armyops_win.execute_plan(self, orders_group, order_id)
            return {"ok": ok, "error": err}
        import armyops
        ok, err = armyops.execute_plan(self, orders_group, order_id)
        return {"ok": ok, "error": err}

    def offensive_line(self, orders_group, parent_order_id, provinces, divisions=None):
        """Draw an offensive line under one front order (the arrow inside the front)."""
        if sys.platform == "win32":
            import armyops_win
            ok, err, _cmd = armyops_win.offensive_line(
                self, orders_group, parent_order_id, provinces, divisions)
            return {"ok": ok, "error": err}
        raise NotImplementedError("offensive_line is Windows-only for now")

    def delete_order(self, orders_group, order_id):
        import armyops
        ok, err = armyops.delete_order(self, orders_group, order_id)
        return {"ok": ok, "error": err}

    # ---------------------------------------------------------- diplomacy
    def wargoal_types(self):
        import diplomacy
        return sorted(diplomacy.wargoal_types(self))

    # Germany's historical snowball: Anschluss (~12 divisions + 20 factories) and
    # the Sudetenland / end of Czechoslovakia (~40k rifles + 35 factories) are free.
    # Declaring war in 1936 throws that away and fights with 52 rifles.
    GER_SNOWBALL = ("GER_anschluss", "GER_demand_sudetenland", "GER_end_of_czechoslovakia")
    # (置顶分数, 最低师数, 最低年份, 是否要求和平) — the manpower gates are the game's
    # own: Anschluss needs >500k men under arms (~54 divisions), Sudetenland >725k
    # (~74). Below the gate the focus is reported in `blocked`, never recommended.
    GER_SNOWBALL_CHAIN = {
        "GER_anschluss":               (2000, 54, 1937, True),
        "GER_demand_sudetenland":      (1990, 74, 1938, True),
        "GER_first_vienna_award":      (1985,  0, 1938, True),
        # 1.19.4.1 calls it GER_fate_of_czechoslovakia (the 1.19.2 text said
        # GER_end_of_czechoslovakia, which does not exist in this focus tree).
        "GER_fate_of_czechoslovakia":  (1980,  0, 1938, True),
        "GER_end_of_czechoslovakia":   (1980,  0, 1938, True),
        # GER_danzig_for_slovakia is mutually_exclusive with GER_danzig_or_war
        # (germany.txt:11777). Taking it in 1938.09 is exactly what locked the Polish
        # casus belli in every 2026-11-05 run — it is blacklisted, never scored.
        # GER_demand_memel does not exist in 1.19.4.1: the Memel/Danzig claims come
        # from GER_reassert_eastern_claims, whose only prerequisite is GER_anschluss.
        "GER_reassert_eastern_claims": (1975,  0, 1939, True),
        "GER_molotov_ribbentrop_pact": (1965,  0, 1939, True),
        "GER_mol_rib_pact":            (1965,  0, 1939, True),
        # has_army_manpower > 949999 = ~100 divisions in the field.
        "GER_danzig_or_war":           (1955, 100, 1939, True),
    }
    # Repeatable filler focuses that would otherwise win every empty slot.
    GER_LOW_PRIORITY = {"GER_the_triumphant_will": -100}
    GER_OPENING_TOP = {
        "GER_remilitarize_the_rhineland": 1850,
        "GER_rhineland":                  1850,
        # Anschluss is gated on GER_heed_von_neuraths_concerns OR
        # GER_reorganize_the_wehrmacht (focus_detail, checked live 2026-11-05).
        # Without one of them the annexation chain can NEVER become selectable —
        # that is why 28 focuses passed and Anschluss never appeared once.
        "GER_reorganize_the_wehrmacht":   1880,
        "GER_heed_von_neuraths_concerns": 1875,
        "GER_the_four_year_plan":         1840,
        "GER_four_year_plan":             1840,
        "GER_autarky_efforts":            1830,
        "GER_construct_the_reichsautobahn": 1820,
        "GER_kdf_wagen_factories":        1810,
        "GER_extra_tech_slot":            1800,
    }

    def war_readiness_check(self, target_tag, tag=None):
        """Would a human press this button? Divisions, rifles, and the historical clock.

        Returns {"ready": bool, "reasons": [...], ...}. Nothing is written to the game;
        every number comes from the same reads the rest of the tool uses.
        """
        c = self._c(tag)
        me = c.tag
        tgt_tag = str(target_tag).upper()
        my_divs = int((self.divisions(me) or {}).get("count") or 0)
        try:
            enemy_divs = int((self.divisions(tgt_tag) or {}).get("count") or 0)
        except Exception:
            enemy_divs = 0
        rifles = 0.0
        try:
            for row in (self.equipment_stockpile(me) or {}).get("stockpile") or []:
                if "infantry_equipment" in str(row.get("equipment") or "").lower():
                    rifles += float(row.get("count") or 0)
        except Exception:
            pass
        try:
            completed = set(self.completed_focuses(me) or [])
        except Exception:
            completed = set()
        try:
            year = int(self.date().get("year") or 0)
        except Exception:
            year = 0

        reasons = []
        warnings = []
        # 2026-11-11: this used to demand 45 divisions for Germany no matter whom we
        # attacked, and a 1939 focus milestone. A 250-day war goal against a 15-division
        # Netherlands expired unused because of it. The engine's own rule is the CB;
        # this check is advisory. Only a genuinely unarmed army blocks now.
        min_divs = max(12 if me == "GER" else 8, int(enemy_divs * 1.2))
        if my_divs < min_divs:
            reasons.append("insufficient divisions: have %d, need >= %d (%s has %d)"
                           % (my_divs, min_divs, tgt_tag, enemy_divs))
        if rifles < 1500:
            reasons.append("insufficient infantry_equipment stockpile: have %.0f, need >= 1500"
                           % rifles)
        if (me == "GER" and year < 1939
                and "GER_danzig_or_war" not in completed
                and not all(f in completed for f in self.GER_SNOWBALL[:2])):
            have = [f for f in self.GER_SNOWBALL[:2] if f in completed]
            warnings.append("historical timeline: %s not done (year=%d, done=%s) — a minor "
                            "war goal before that is allowed, it just raises world tension"
                            % (" + ".join(self.GER_SNOWBALL[:2]), year, have))
        return {
            "ready": not reasons,
            "warnings": warnings,
            "my_divisions": my_divs,
            "enemy_divisions": enemy_divs,
            "infantry_equipment_stockpile": round(rifles, 1),
            "year": year,
            "completed_snowball": sorted(f for f in self.GER_SNOWBALL if f in completed),
            "reasons": reasons,
        }

    def justify_wargoal(self, target_tag, wargoal="take_state", states=None,
                        dry_run=False, force=False):
        """Start justifying a war goal, gated by war_readiness_check unless force=True.

        We only choose the TARGET, and for types like take_state WHICH STATES;
        the duration and political power cost are decided by the game.
        Default is `take_state` (what the diplomacy dialog opens with): the
        script-only `annex_everything` never passes CanExecute in 1.19.4.1.
        """
        import diplomacy
        readiness = None
        if not force:
            try:
                import playbooks as _pb
                # NOTE: justify_wargoal has no `tag` parameter — using `tag` here
                # raised NameError, the except swallowed it and the whole veteran
                # gate silently disappeared (caught 2026-11-05 by calling it live).
                pbk = _pb.get_playbook(self.player().tag)
            except Exception:
                pbk = {}
            forbid = int(pbk.get("forbid_manual_justify_before_year") or 0)
            fallback = int(pbk.get("justify_fallback_year") or forbid)
            # 2026-11-06: a minor nation, or a major switched to `cheese_breakout`,
            # may justify freely — the historical no-justify rule only applies to
            # the seven majors in "historical" mode.
            try:
                import playbooks as _pbm
                if _pbm.get_playbook_mode(self.player().tag) != "historical":
                    forbid = 0
            except Exception:
                pass
            if forbid and not force:
                year = 0
                try:
                    year = int(self.date().get("year") or 0)
                except Exception:
                    pass
                done = set()
                try:
                    done = set(self.completed_focuses() or [])
                except Exception:
                    pass
                # 1936-1938: never. From the fallback year on: only when the focus
                # chain failed to deliver a casus belli (e.g. Poland accepted Danzig,
                # ai_chance 10/11 -> the ~9% branch that grants no wargoal).
                allowed = (year >= fallback
                           and any(f in done for f in ("GER_danzig_or_war",))
                           ) or year >= forbid
                if not allowed:
                    return {
                        "ok": False,
                        "blocked_by": "veteran_playbook",
                        "error": pbk.get("manual_justify_note", "manual justify forbidden"),
                        "hint": ("Historical line: use the focus tree. For a deliberate "
                                 "1936 opening (e.g. the Dutch/East-Indies rubber plan) pass "
                                 "force=true — the engine accepts it immediately; the "
                                 "playbook gate is advisory, not a game rule."),
                        "year": year,
                        "completed": sorted(f for f in ("GER_danzig_or_war",) if f in done),
                    }
            readiness = self.war_readiness_check(target_tag)
            if not readiness["ready"]:
                return {"ok": False, "blocked_by": "war_readiness_check",
                        "error": "; ".join(readiness["reasons"]),
                        "readiness": readiness,
                        "hint": "Finish the historical annexations, train divisions and "
                                "build a rifle stockpile first — or pass force=true."}
        sp = []
        if states:
            want = {int(x) for x in states}
            for tag in (target_tag,):
                for st in _arr(self.p, self.country(tag).ptr, CO_STATES):
                    if self.p.i32(st + ST_ID) in want:
                        sp.append(st)
            missing = want - {self.p.i32(x + ST_ID) for x in sp}
            if missing:
                return {"ok": False, "error": "these states do not belong to the target: %s" % sorted(missing)}
        # 2026-11-11: CGenerateWarGoalAction::CanExecute refuses silently when the
        # country cannot pay the engine's political-power price, and every caller only
        # saw "CanExecute refused every attempt" (three runs blamed the whole command).
        # Ask the engine for the quote first and report the real blocker.
        if not dry_run:
            try:
                for _j in diplomacy.justifying(self):
                    if _j.get("source_class") != "CTimedWargoalActivity":
                        continue        # a queued CGenerateWarGoalAction is not a justification
                    if str(_j.get("target") or "").upper() == str(target_tag).upper():
                        return {"ok": False, "blocked_by": "already_justifying",
                                "error": "already justifying a war goal against %s" % target_tag,
                                "justifying": _j}
            except Exception:
                pass
            try:
                quote = self.wargoal_quote(target_tag, wargoal, states)
            except Exception as _e:
                quote = None
            if (quote and quote.get("ok") and quote.get("can_justify_now") is False
                    and (quote.get("pp_shortfall") or 0) > 0):
                return {"ok": False, "blocked_by": "political_power",
                        "error": "not enough political power: %s" % (quote.get("note") or ""),
                        "current_pp": quote.get("current_pp"),
                        "required_pp": quote.get("required_pp"),
                        "pp_shortfall": quote.get("pp_shortfall"),
                        "quote": quote,
                        "states": [self.p.i32(x + ST_ID) for x in sp]}
        try:
            _pp0 = self.politics().get("political_power")
        except Exception:
            _pp0 = None
        ok, err = diplomacy.justify_wargoal(self, target_tag, wargoal, sp, dry_run=dry_run)
        _pp_spent = None
        mine = None
        if ok and not dry_run:
            # The engine accepts at most one justification at a time and refuses the
            # second one at execution time, long after CCommand::Run returned true.
            # Round 3 reported ok:true for commands that never became an activity.
            for _ in range(10):
                # A tick must run BEFORE justifying() is asked: the activity only
                # exists once the loop consumed the command, and the first query
                # used to cache an empty answer (objindex negative cache 2s/5s).
                self._settle(0.35)
                for _j in diplomacy.justifying(self):
                    if _j.get("source_class") != "CTimedWargoalActivity":
                        continue
                    if str(_j.get("target") or "").upper() == str(target_tag).upper():
                        mine = _j
                        break
                if mine:
                    break
        # The PP is charged on a later tick too: read it AFTER the poll.
        try:
            _pp1 = self.politics().get("political_power")
            _pp_spent = (round(float(_pp0) - float(_pp1), 1)
                         if (_pp0 is not None and _pp1 is not None) else None)
        except Exception:
            _pp_spent = None
        if ok and not dry_run and not mine:
                try:
                    import session as _sess
                    _paused = _sess.is_paused(self)
                except Exception:
                    _paused = False
                if _paused:
                    # The command sits in the queue; the engine builds the activity on
                    # the next unpaused frame. Failing here made callers retry and leak
                    # one CGenerateWarGoalAction per attempt (round 4: six of them).
                    return {"ok": True, "queued": True,
                            "note": ("the game is PAUSED: the justification is queued and "
                                     "will start on the next unpaused frame - call "
                                     "hoi4_justifying after unpausing to confirm"),
                            "wargoal": wargoal,
                            "states": [self.p.i32(x + ST_ID) for x in sp]}
                # 2026-11-13: with the `instant_wargoal` cheat on there is no timed
                # activity at all - the CB is created on the same tick. A live
                # CWarGoal for (me, target) proves the justification completed, so
                # report success instead of "the engine charged PP and dropped it".
                try:
                    _cb = diplomacy.find_wargoal(self, target_tag)
                except Exception:
                    _cb = None
                if _cb:
                    return {"ok": True, "instant": True, "wargoal": wargoal,
                            "casus_belli": _cb, "pp_spent": _pp_spent,
                            "states": [self.p.i32(x + ST_ID) for x in sp]}
                return {"ok": False,
                        "blocked_by": "engine_dropped" if (_pp_spent or 0) > 0 else "not_started",
                        "pp_spent": _pp_spent,
                        "error": (("the engine charged %.1f PP but created no timed war-goal "
                                   "activity for %s — the justification action was dropped "
                                   "after the cost was taken" % (_pp_spent, target_tag))
                                  if (_pp_spent or 0) > 0 else
                                  ("the command was queued but no timed war-goal activity "
                                   "for %s appeared (the engine allows one justification at "
                                   "a time — check hoi4_justifying)" % target_tag)),
                        "wargoal": wargoal,
                        "states": [self.p.i32(x + ST_ID) for x in sp],
                        "active": diplomacy.justifying(self)}
        return {"ok": ok, "error": err, "wargoal": wargoal, "dry_run": bool(dry_run),
                "pp_spent": _pp_spent,
                "states": [self.p.i32(x + ST_ID) for x in sp],
                "readiness": readiness,
                # live progress straight from CTimedWargoalActivity (days_left/eta_date)
                "justifying": (diplomacy.justifying(self) if ok and not dry_run else [])}

    def wargoal_quote(self, target_tag, wargoal="take_state", states=None, tag=None):
        """对 target_tag 正当化指定州要多少 PP、还差多少、大概多少天（引擎级精确 PP）。"""
        import diplomacy
        sp, sids = [], []
        if states:
            want = {int(x) for x in states}
            for st in _arr(self.p, self.country(target_tag).ptr, CO_STATES):
                sid = self.p.i32(st + ST_ID)
                if sid in want:
                    sp.append(st)
                    sids.append(sid)
            missing = want - set(sids)
            if missing:
                return {"ok": False,
                        "error": "these states do not belong to %s: %s" % (target_tag, sorted(missing))}
        return diplomacy.wargoal_quote(self, target_tag, wargoal, sp, sids, source_tag=tag)

    def justifying(self):
        import diplomacy
        return diplomacy.justifying(self)

    def diplomatic_actions(self):
        import diplomacy, diploscan
        cat = diplomacy.action_catalog()
        # 2026-11-13: action_catalog() already unwraps diploscan's "_all" map, so
        # cat["_all"] raised KeyError on every call. Read the raw build for the
        # complete class list instead.
        raw = diploscan.build()
        allmap = (raw or {}).get("_all") or {}
        return {"sendable": sorted(k for k in cat if k != "_all"),
                "all_classes": sorted(allmap)}

    def send_diplomatic_action(self, action_class, target_tag, dry_run=False):
        import diplomacy
        ok, err = diplomacy.send_action(self, action_class, target_tag, dry_run=dry_run)
        return {"ok": ok, "error": err}

    def declare_war(self, target_tag, dry_run=False, force=False):
        readiness = None
        if not dry_run and not force:
            readiness = self.war_readiness_check(target_tag)
            if not readiness["ready"]:
                return {"ok": False, "blocked_by": "war_readiness_check",
                        "error": "; ".join(readiness["reasons"]),
                        "readiness": readiness}
        import diplomacy
        import combat
        import time as _t
        if sys.platform == "win32":
            if dry_run:
                # BUG-08 (playtest 2026-11-15): dry_run used to fall through to the
                # real declaration -- a "probe" started the Dutch war. A dry run now
                # only evaluates the player-side gate and posts nothing.
                rd = self.war_readiness_check(target_tag)
                return {"ok": bool(rd.get("ready")), "dry_run": True,
                        "posted": 0, "target": target_tag,
                        "readiness": rd,
                        "note": "dry_run: no command was built or posted"}
            # Real action channel on the PE build: CDeclareWarAction wrapped in a
            # CDiplomaticActionCommand.  The availability gate (action+0x70) is
            # bypassed, so war_readiness_check stays the only guard.
            import cmdwin
            c = self._c(target_tag)
            r = cmdwin.declare_war(self, c.tag_id)
            self._settle()
            # combat.war_relations scans live CWarRelation objects, but the object
            # index is a snapshot -- refresh=True is what makes a just-declared war
            # visible (a full rescan costs ~25 s, hence one shot rather than a poll).
            verified = None
            if r.get("ok"):
                try:
                    _t.sleep(2.0)
                    verified = target_tag.upper() in combat.enemy_tags(
                        self, self.player_tag(), refresh=True)
                except Exception:
                    verified = None
            return {"ok": bool(r.get("ok")), "posted": r.get("posted"),
                    "target": target_tag, "target_id": c.tag_id,
                    "action": r.get("act"), "cmd": r.get("cmd"),
                    "at_war_verified": verified}
        cb = None if dry_run else diplomacy.find_wargoal(self, target_tag)
        ok, err = diplomacy.declare_war(self, target_tag, dry_run=dry_run)
        # 2026-11-13: CCommand::Run only queues the action, so a `true` return means
        # "CanExecute accepted and the command was posted", not "war exists". Poll
        # the war-relation table so the caller never has to guess.
        verified = None
        if ok and not dry_run:
            for _ in range(10):
                _t.sleep(1.0)
                try:
                    if target_tag in combat.enemy_tags(self):
                        verified = True
                        break
                except Exception:
                    pass
            if verified is None:
                verified = False
        return {"ok": ok, "error": err, "readiness": readiness,
                "wargoal": cb, "at_war_verified": verified}

    def country_characters(self, tag=None):
        """All of the country's characters: leaders, generals, admirals, advisors."""
        p = self.p
        out = []
        for ch in self.all_characters(tag):
            out.append({"key": item_name(p, ch + CH_KEY),
                        "name": item_name(p, ch + CH_NAME),
                        "is_military_leader": bool(p.u64(ch + self.CH_ARMY_LEADER)),
                        "ptr": hex(ch)})
        return {"count": len(out), "characters": out}

    def divisions(self, tag=None, limit=200):
        """The country's divisions (CArmy in the game's code)."""
        p = self.p
        c = self._c(tag)
        out = []
        for i, a in enumerate(_arr(p, c.ptr, CO_ARMIES)[:limit]):
            tmpl = p.u64(a + 0x3B8)
            out.append({"index": i, "ptr": hex(a),
                        "template": item_name(p, tmpl + DT_NAME) if tmpl else None,
                        "province_ptr": hex(p.u64(a + 0x1F0))})
        return {"count": _count(p, c.ptr, CO_ARMIES), "divisions": out}

    # ============================================================ PROJECTS
    def scientists(self, tag=None, limit=60):
        p = self.p
        c = self._c(tag)
        pref = (c.tag + "_").lower()
        out = []
        for s in self.oi.instances("CScientist"):
            nm = None
            for off in range(0, 0x120, 8):
                v = item_name(p, s + off)
                if v and v.isprintable() and 2 < len(v) < 48:
                    nm = v
                    break
            if nm and (not pref or nm.lower().startswith(pref) or True):
                out.append({"name": nm, "ptr": hex(s)})
            if len(out) >= limit:
                break
        return out

    def special_projects(self, tag=None):
        p = self.p
        c = self._c(tag)
        pool = p.u64(c.ptr + CO_PROJECT_POOL)
        prog = p.u64(c.ptr + CO_PROGRAM_STATUS)
        res = {"pool": hex(pool) if pool else None, "program_status": hex(prog) if prog else None}
        if pool:
            from probe import describe
            res["pool_arrays"] = [r for r in describe(p, pool, self.oi, span=0x200) if r["size"]]
        return res

    # ============================================================ DOCTRINE
    def tech_folders(self):
        """Technology and doctrine folders."""
        p = self.p
        db = self.oi.singleton("CTechnologyDatabase")
        out = []
        for f in _arr(p, db, 0xA8):
            nm = None
            for off in range(0, 0x200, 8):
                v = item_name(p, f + off)
                if v and v.isprintable() and 2 < len(v) < 48:
                    nm = v
                    break
            out.append({"name": nm, "ptr": hex(f)})
        return out

    # ========================================================= FOCUS DETAIL
    def focus_detail(self, key, tag=None):
        """Everything about one focus: prerequisites, mutual exclusions, filters."""
        p = self.p
        f = self.find_focus(key, tag)
        if not f:
            raise KeyError("no such focus: %s" % key)
        info = self._focus_info(f)
        deps = []
        for d in _arr(p, f, NF_PREREQ):
            for ff in _arr(p, d, 0x08):
                k = item_name(p, ff + NF_KEY)
                if k:
                    deps.append(k)
        mut = []
        for x in _arr(p, f, NF_MUTEX):
            k = item_name(p, x + NF_KEY)
            if k:
                mut.append(k)
            else:
                for y in _arr(p, x, 0x08):
                    kk = item_name(p, y + NF_KEY)
                    if kk:
                        mut.append(kk)
        filters = []
        fa = f + NF_FILTERS
        n = p.i32(fa + O.PDXARRAY_SIZE)
        base = p.u64(fa + O.PDXARRAY_DATA)
        for i in range(max(0, min(n, 12))):
            s = item_name(p, base + i * 32)
            if s:
                filters.append(s)
        info["prerequisite_keys"] = [d for d in deps if d]
        info["mutually_exclusive_keys"] = [m for m in mut if m]
        info["filters"] = filters
        info["ptr"] = hex(f)
        return info

    def focus_progress(self, tag=None):
        p = self.p
        prog = p.u64(self._c(tag).ptr + CO_FOCUS_PROGRESS)
        cur = self.current_focus(tag)
        return {"current": cur, "raw_0x38": p.i64(prog + 0x38) / O.FIXED if prog else None,
                "completed": _count(p, prog, FP_COMPLETED) if prog else 0}

    def _can_start_focus_cmd(self, focus_ptr, tag_id):
        """Would the ENGINE let this country start this focus right now?

        CSetNationalFocusCommand::CanExecute is slot +0x50 of its vtable
        (0x4416DD8, 1.19.4.1) — the same entry CCommand::Run calls before it
        posts. Verified 2026-11-11 against the live game: it returned 1 for
        GER_befriend_japan and 0 for GER_legion_condor, and set_focus_ptr()
        then succeeded / failed exactly the same way.
        """
        import api, commands
        try:
            spec = commands.COMMANDS["SetNationalFocus"]
            vt = self.va(spec["vt"])
            if vt < self.p.base or vt > self.p.base + 0x3900000:
                return True          # platform without the Linux command vtable
            can_fn = self.p.u64(vt + 0x50) - self.p.base
        except OSError:
            return True
        api.allow_call(can_fn)
        cmd = self.cmd.make("SetNationalFocus", tag=int(tag_id),
                            focus_ptr=int(focus_ptr))
        try:
            with self:
                return bool(self.call(can_fn, (cmd, 0)) & 0xFF)
        except Exception:
            return True          # never hide a focus because of a probe error
        finally:
            try:
                self._free_cmd(cmd)
            except Exception:
                pass

    def selectable_focuses(self, tag=None, allow_civil_war=False):
        """Selectable focuses, ranked by the country's own historical AI plan.

        `selectable` is still the set that passes the live prerequisite/mutex check;
        the ranking comes from common/ai_strategy_plans (ai_national_focuses) plus
        static reward flags from common/national_focus.  No game calls.
        """
        import focusplan
        p = self.p
        c = self._c(tag)
        prog = p.u64(c.ptr + CO_FOCUS_PROGRESS)
        if not prog:
            return {"recommended": None, "recommended_reason": "", "count": 0,
                    "selectable": [], "focuses": [], "blocked": []}
        completed = set()
        for f in _arr(p, prog, FP_COMPLETED):
            k = item_name(p, f + NF_KEY)
            if k:
                completed.add(k)
        current = p.u64(prog + FP_CURRENT)

        blacklist = set()
        try:
            import playbooks
            blacklist = playbooks.blacklist(c.tag)
        except Exception:
            blacklist = set()
        if c.tag == "GER" and not allow_civil_war:
            blacklist |= {"GER_oppose_hitler", "GER_oppose_hitler_ww"}

        data = focusplan.load()
        meta = data.get("focuses", {})
        plan = data.get("plans", {}).get(c.tag, [])
        plan_pos = {k: i for i, k in enumerate(plan)}
        # The historical AI plan puts GER_anschluss at #25 and GER_demand_sudetenland
        # at #29: following it literally spends 3.6 years on propaganda/industry and
        # never annexes anything (28 focuses, 0 snowball, measured 2026-11-05).
        # These two tables jump the annexation chain to the top as soon as the
        # country can actually pass its manpower/peace conditions.
        my_divs = len(_arr(p, c.ptr, CO_ARMIES))
        try:
            yr = int(self.date().get("year") or 0)
        except Exception:
            yr = 0
        try:
            import combat as _cb
            at_war = bool(_cb.at_war(self, c.tag))
        except Exception:
            at_war = False
        rows, blocked = [], []
        try:
            import playbooks as _pb
            pb_scores = _pb.focus_scores(c.tag, yr, my_divs, at_war)
        except Exception:
            pb_scores = {}
        for f in self.focus_tree(tag):
            k = item_name(p, f + NF_KEY)
            if not k or k in completed or f == current:
                continue
            if k in blacklist:
                blocked.append(k)
                continue
            ok = True
            for group in self._focus_prereq_groups(f):
                if group and not any(g in completed for g in group):
                    ok = False
                    break
            if not ok:
                continue
            if any(m in completed for m in self._focus_mutex_keys(f)):
                continue
            m = meta.get(k, {})
            score, tier, reason = 0, "D_other", ""
            if k in plan_pos:
                score = 1000 - min(plan_pos[k], 100)
                tier = "S_historical"
                reason = "历史主线优先顺位 #%d" % (plan_pos[k] + 1)
            elif m.get("research_slot"):
                score, tier, reason = 300, "A_research_slot", "奖励额外科研槽"
            elif m.get("annex") or m.get("wargoal"):
                score, tier, reason = 300, "A_annex", "政治吞并/宣战目标"
            elif m.get("industry"):
                score, tier, reason = 150, "B_industry", "工业主线（民工/军工）"
            elif m.get("air_naval"):
                score, tier, reason = 0, "C_air_naval", "空军/海军支线，默认排后"
            if c.tag == "GER":
                sb = self.GER_SNOWBALL_CHAIN.get(k)
                if sb:
                    sb_score, min_d, min_y, need_peace = sb
                    if (need_peace and at_war) or my_divs < min_d or yr < min_y:
                        blocked.append("%s (wait: divs %d/%d, year %d/%d, at_war=%s)"
                                       % (k, my_divs, min_d, yr, min_y, at_war))
                        continue
                    score, tier = sb_score, "S0_snowball"
                    reason = "德国史实吞并雪球核心主线 (divs=%d >= %d)" % (my_divs, min_d)
                elif k in self.GER_OPENING_TOP:
                    score, tier, reason = (self.GER_OPENING_TOP[k], "S0_opening",
                                           "德国开局核心工业/政治主线")
                elif k in self.GER_LOW_PRIORITY:
                    score, tier, reason = (self.GER_LOW_PRIORITY[k], "Z_filler",
                                           "可重复填充国策，让位给吞并链")
            if k in pb_scores and pb_scores[k] > score:
                score, tier, reason = (pb_scores[k], "S0_playbook", "老手攻略时间轴")
            rows.append({
                "id": k, "key": k, "name": item_name(p, f + NF_NAME),
                "ptr": hex(f), "tier": tier, "score": score, "days": m.get("days", 70),
                "reason": reason, "_plan": plan_pos.get(k, 999),
            })
        # 2026-11-11: the prerequisite/mutex scan above cannot evaluate the
        # `available` trigger block (GER_anschluss needs 250k manpower and
        # rifles; GER_currency_reforms needs a law), which is why the tool used
        # to advertise focuses the engine then refused — the previous run spent
        # dozens of turns clicking them. Ask the engine instead. Only while no
        # focus is running: with one in progress the engine refuses everything
        # else, so the answer would be a useless wall of "no".
        engine_checked = False
        if rows and not current:
            keep = []
            for r in rows:
                if self._can_start_focus_cmd(int(r["ptr"], 16), c.tag_id):
                    keep.append(r)
                else:
                    blocked.append("%s (engine: available-trigger / manpower / "
                                   "political-power gate not met)" % r["id"])
            rows = keep
            engine_checked = True
        rows.sort(key=lambda r: (-r["score"], r["_plan"], r["id"]))
        for r in rows:
            r.pop("_plan", None)
        recommended = rows[0]["id"] if rows else None
        return {
            "recommended": recommended,
            "recommended_reason": rows[0]["reason"] if rows else "",
            "count": len(rows),
            "engine_checked": engine_checked,
            "focus_in_progress": item_name(p, current + NF_KEY) if current else None,
            "selectable": rows,
            "focuses": rows,
            "blocked": blocked[:40],
        }

    def completed_focuses(self, tag=None):
        p = self.p
        prog = p.u64(self._c(tag).ptr + CO_FOCUS_PROGRESS)
        if not prog:
            return []
        return [item_name(p, f + NF_KEY) for f in _arr(p, prog, FP_COMPLETED)]

    def continuous_focuses(self, tag=None):
        """The 1.19.4.1 continuous-focus catalogue (no completion needed).

        CO_CONT_FOCUS_PAL (+0x1340) is a CContinuousFocusPalette -- a GUI object
        with a std::string at +0x08 and one selection slot at +0x00 -- and holds
        no focus list at all, which is why the old find_arrays() walk returned []
        for every country. The real catalogue is the global
        CContinuousNationalFocus database (14 definitions, key at +0x18, display
        name at +0x38). Verified live 2026-11-11.
        """
        p = self.p
        out = []
        for d in self.oi.instances("CContinuousNationalFocus"):
            k = item_name(p, d + 0x18)
            if not k:
                continue
            out.append({"key": k, "id": item_name(p, d + 0x58) or k,
                        "name": item_name(p, d + 0x38),
                        "ptr": hex(d), "generic": k.startswith("continuous_")})
        out.sort(key=lambda r: (not r["generic"], r["key"]))
        return out

    def set_continuous_focus(self, key, tag=None):
        """Select a continuous focus, and tell the truth about the result.

        2026-11-11: the definition pointer is what the old code passed, and the
        engine refuses it (CanExecute 0) -- the operand has to be the country's
        own CContinuousFocusItem, which this build only materialises while the
        political screen is built, so it cannot be fabricated from Python. The
        tool therefore reports `queued` from CCommand::Run and never claims
        `ok` without it, instead of the old unconditional {"ok": True}.
        """
        c = self._c(tag)
        rows = self.continuous_focuses(c.tag)
        row = next((r for r in rows if key in (r["key"], r["id"])), None)
        if row is None:
            raise KeyError("no such continuous focus: %s (available: %s)"
                           % (key, ", ".join(r["key"] for r in rows)))
        if not row["generic"]:
            return {"ok": False, "focus": key, "queued": False,
                    "error": "%s is a country-specific continuous focus and is "
                             "not offered to %s" % (key, c.tag)}
        cmd = self.cmd.make("SetContinuousFocus", tag=c.tag_id,
                            focus_ptr=int(row["ptr"], 16))
        rv = self.cmd.post(cmd)
        self._settle(0.4)
        ok = bool(rv & 0xFF) if isinstance(rv, int) else False
        return {"ok": ok, "focus": key, "queued": ok, "verified": False,
                "error": "" if ok else
                ("engine refused CSetContinuousFocusCommand::CanExecute: this "
                 "build wants the per-country CContinuousFocusItem, not the "
                 "definition pointer; select it in the UI instead")}

    def drop_continuous_focus(self, tag=None):
        c = self._c(tag)
        cmd = self.cmd.make("DropContinuousFocus", tag=c.tag_id)
        rv = self.cmd.post(cmd)
        self._settle(0.4)
        return {"ok": bool(rv & 0xFF) if isinstance(rv, int) else False,
                "error": getattr(self.cmd, "last_error", "")}

    # ================================================== GENERIC COMMAND ACCESS
    def player_commands(self, filter_text="", with_params=True):
        """All 370 player commands, with their parameter layouts."""
        cat = self.cmd.catalog()
        ctors = self.cmd.ctor_catalog()
        f = filter_text.lower()
        out = {}
        for k, v in sorted(cat.items()):
            if f and f not in k.lower():
                continue
            rec = {"size": v.get("size"),
                   "type_id": hex(v["type_id"]) if v["type_id"] else None,
                   "verified": k in _VERIFIED_NAMES,
                   "has_params": bool(ctors.get(k, {}).get("fields"))}
            if with_params:
                try:
                    rec["parameters"] = self.command_help(k)["parameters"]
                except Exception:
                    rec["parameters"] = []
            out[k] = rec
        return {"count": len(out),
                "note": "use hoi4_send_command to fill in the parameters; "
                        "values can be given by name, such as 'focus:xxx' / 'tech:xxx' / 'charhandle:xxx'",
                "commands": out}

    def send_command(self, name: str, fields: dict = None, tag=None):
        """Send ANY player command.
        field keys are either "tag" or an offset such as "0x28"."""
        c = self._c(tag)
        f = dict(fields or {})
        spec, _src = self.cmd.spec_for(name)
        has_tag = any(fl[2] == "tag" for fl in spec["fields"])
        if has_tag and "tag" not in f and "0x24" not in f:
            f["tag"] = c.tag_id
        with self:
            r = self.cmd.send_raw(name, f)
        self._settle()
        return r

    # ======================================================= EQUIPMENT STOCKPILE
    PS_STOCKPILE = 0x220   # CPdxArray<{CEquipmentVariant*; int64 count x1e5}>

    def equipment_stockpile(self, tag=None, limit=120):
        """Equipment counts in the stockpile, as variant -> count."""
        p = self.p
        self._load_equipment()
        ps = p.u64(self._c(tag).ptr + CO_PRODUCTION)
        n = p.i32(ps + self.PS_STOCKPILE + O.PDXARRAY_SIZE)
        base = p.u64(ps + self.PS_STOCKPILE + O.PDXARRAY_DATA)
        out = []
        if base and n:
            raw = p.try_read(base, 16 * n) or b""
            for i in range(min(n, len(raw) // 16, limit)):
                vp, cnt = struct.unpack_from("<qq", raw, i * 16)
                nm = self._eq_by_id.get(p.i32(vp + EV_TYPE_ID)) if vp else None
                out.append({"equipment": nm, "count": round(cnt / O.FIXED, 1)})
        # variants that can be produced
        variants = []
        for v in _arr(p, ps, PS_VARIANTS_ACTIVE)[:limit]:
            nm = self._eq_by_id.get(p.i32(v + EV_TYPE_ID))
            if nm:
                variants.append({"equipment": nm, "variant_ptr": hex(v)})
        return {"stockpile": out, "available_variants": variants}

    # ================================================ VERITABANI GEZGINI
    DATABASE_ARRAYS = {
        "war_goals": ("CWarGoalDatabase", 0x40, None),
        "occupation_laws": ("COccupationLawDatabase", 0x58, None),
        "buildings": ("CBuildingDatabase", 0x40, BT_KEY),
        "technologies": ("CTechnologyDatabase", 0x48, TT_NAME),
        "ideas": ("CIdeaDatabase", 0x68, IDEA_NAME),
        "decisions": ("CDecisionDatabase", 0x28, DECISION_NAME),
        "equipment": ("CEquipmentDatabase", 0x60, ET_NAME),
        "focuses": ("CNationalFocusDatabase", 0x58, NF_KEY),
        "character_templates": ("CCharacterTemplateDatabase", 0x28, None),
        "scripted_diplomatic_actions": ("CScriptedDiplomaticActionTemplateDatabase", 0x160, None),
    }

    def list_database(self, which: str, filter_text="", limit=200):
        """List any of the game's item databases by name."""
        if which not in self.DATABASE_ARRAYS:
            return {"error": "bilinmeyen veritabani",
                    "available": sorted(self.DATABASE_ARRAYS)}
        cls, off, nameoff = self.DATABASE_ARRAYS[which]
        db = self.oi.singleton(cls)
        if not db:
            return {"error": "%s bellekte bulunamadi" % cls}
        p = self.p
        out = []
        f = filter_text.lower()
        for it in _arr(p, db, off):
            nm = item_name(p, it + nameoff) if nameoff is not None else None
            if nm is None:
                for o in range(0, 0x300, 8):
                    v = item_name(p, it + o)
                    if v and v.isprintable() and 2 < len(v) < 48 and not v.startswith("/"):
                        nm = v
                        break
            if nm and (not f or f in nm.lower()):
                out.append(nm)
            if len(out) >= limit:
                break
        return {"database": which, "count": len(out), "items": out}

    def browse_object(self, ptr, span=0x100):
        """ADVANCED: dump an unknown object's fields, resolving classes and names."""
        p = self.p
        if isinstance(ptr, str):
            ptr = int(ptr, 16)
        rows = []
        d = p.try_read(ptr, span)
        if not d:
            return {"error": "okunamadi"}
        for i in range(0, len(d) - 8, 8):
            v = struct.unpack_from("<Q", d, i)[0]
            row = {"off": hex(i), "u64": hex(v), "i32": struct.unpack_from("<i", d, i)[0]}
            c = self.oi.class_of(v) if 0x10000 < v < 0x7FFFFFFFFFFF else None
            if c:
                row["class"] = c
            s = item_name(p, ptr + i)
            if s and s.isprintable() and 1 < len(s) < 60:
                row["str"] = s
            rows.append(row)
        return {"ptr": hex(ptr), "class": self.oi.class_of(ptr), "fields": rows}

    # ============================================================== FULL REPORT
    def full_report(self, tag=None):
        """The wide status report to read at the start of a turn."""
        c = self._c(tag)
        base = self.overview(c.tag)
        base["resources"] = self.resources(c.tag)
        # B2/C2 (review 2026-11-07): `country_stats` is built on the 1.19.2
        # stats.pkl table and is now refused by `_scope_call`; calling it here
        # killed the process. Report the refusal instead of the numbers.
        try:
            base["stats"] = self.country_stats(c.tag)
        except Exception as e:
            base["stats"] = {}
            base["stats_unavailable"] = str(e)
        base["armies"] = self.armies(c.tag)["count"]
        base["division_templates"] = [t["name"] for t in self.division_templates(c.tag)]
        base["fleets"] = [f["name"] for f in self.fleets(c.tag)]
        base["command_power"] = self.p.i64(c.ptr + CO_COMMAND_POWER) / O.FIXED
        # B2: `selectable_focuses` returns a dict, so `for f in ...` iterated the
        # KEYS and `f["key"]` raised TypeError: string indices must be integers.
        _sf = self.selectable_focuses(c.tag)
        _sel = _sf.get("selectable", []) if isinstance(_sf, dict) else (_sf or [])
        base["selectable_focuses"] = [
            (f["key"] if isinstance(f, dict) else str(f)) for f in _sel
        ]
        base["completed_focuses"] = [f for f in self.completed_focuses(c.tag) if f]
        base["faction"] = self.faction(c.tag)
        base["stockpile"] = self.equipment_stockpile(c.tag)["stockpile"]
        return base


_VERIFIED_NAMES = set()
try:
    from commands import COMMANDS as _CMDS
    _VERIFIED_NAMES = {"C" + k + "Command" for k in _CMDS}
except Exception:
    pass

# mix into Game
for _n in dir(MacroMixin):
    if not _n.startswith("__"):
        setattr(Game, _n, getattr(MacroMixin, _n))


# ============================================================================
#  STATE DETAIL, DECISION AND IDEA DETAIL, AND ACTION WRAPPERS
# ============================================================================
ST_PROVINCES        = getattr(O, "ST_PROVINCES", 0x08)     # CPdxArray<CProvince*>
ST_BUILDING_LEVELS  = 0x128    # 56-entry building level array
ST_BUILDING_MAX     = 0x140
ST_BUILDINGS        = getattr(O, "ST_BUILDINGS", 0x158)    # CPdxArray<CBuilding*>
BG_LEVEL            = getattr(O, "BG_LEVEL", 0x40)         # int16, building level
ST_RESISTANCE       = getattr(O, "ST_RESISTANCE", 0x278)   # int64 x1e5, None = unknown
ST_COMPLIANCE       = getattr(O, "ST_COMPLIANCE", 0x2A8)   # int64 x1e5, None = unknown


class MacroMixin2:
    # ============================================================= STATES
    def state_detail(self, state_id: int, tag=None):
        p = self.p
        self._load_buildings()
        # 2026-11-13: this used to look only in the PLAYER's CO_STATES array, so every
        # foreign state id raised KeyError("state not found") - which is what the
        # Germany planning scripts hit when they asked about Dutch states.
        _found = None
        if tag:
            for s in _arr(p, self._c(tag).ptr, CO_STATES):
                if p.i32(s + ST_ID) == state_id:
                    _found = s
                    break
        else:
            try:
                for c in self.countries():
                    for s in _arr(p, c.ptr, CO_STATES):
                        if p.i32(s + ST_ID) == state_id:
                            _found = s
                            break
                    if _found:
                        break
            except Exception:
                _found = None
        if _found is None:
            raise KeyError("state not found: %s" % state_id)
        for s in [_found]:
            if p.i32(s + ST_ID) != state_id:
                continue
            blds = {}
            for b in _arr(p, s, ST_BUILDINGS):
                nm = self._bld_by_id.get(p.i32(b + BG_TYPE_ID))
                if nm:
                    blds[nm] = struct.unpack("<h", p.read(b + BG_LEVEL, 2))[0]
            return {
                "id": state_id,
                "ptr": hex(s),
                "buildings": blds,
                "provinces": _count(p, s, ST_PROVINCES),
                "resistance": (round(p.i64(s + ST_RESISTANCE) / O.FIXED, 3)
                               if ST_RESISTANCE else None),
                "compliance": (round(p.i64(s + ST_COMPLIANCE) / O.FIXED, 3)
                               if ST_COMPLIANCE else None),
                "building_status_ptr": hex(s + 0x120),
            }
        raise KeyError("state not found: %s" % state_id)

    def states_detail(self, tag=None, limit=40):
        out = []
        for s in self.states(tag)[:limit]:
            try:
                out.append(self.state_detail(s["id"], tag))
            except Exception:
                pass
        return out

    # ======================================================== IDEAS AND DECISIONS
    def idea_categories(self, tag=None):
        """Idea, law and advisor categories: law slots and advisor roles."""
        p = self.p
        db = self.oi.singleton("CIdeaDatabase")
        out = []
        for c in _arr(p, db, 0x98):
            nm = None
            for o in range(0, 0x200, 8):
                v = item_name(p, c + o)
                if v and v.isprintable() and 2 < len(v) < 48:
                    nm = v
                    break
            out.append(nm)
        return [x for x in out if x]

    def decision_detail(self, key, tag=None):
        p = self.p
        for which in ("visible", "active", "all"):
            for d in self.decisions(tag, which):
                if d["key"] == key:
                    return {"key": key, "list": which, "ptr": hex(d["ptr"]),
                            "fields": self.browse_object(d["ptr"], 0x80)["fields"][:16]}
        raise KeyError("no such decision: %s" % key)

    # ======================================================= COMMAND SHORTCUTS
    #  All of these go through the player command queue (not cheats).
    def _sc(self, name, tag=None, **kw):
        return self.send_command(name, kw, tag)

    # --- armies and divisions ---
    def create_division_template(self, tag=None, **kw):
        return self._sc("CCreateDivisionTemplateCommand", tag, **kw)

    def update_division_template(self, tag=None, **kw):
        return self._sc("CUpdateDivisionTemplateCommand", tag, **kw)

    def set_army_template(self, tag=None, **kw):
        return self._sc("CSetArmyTemplateCommand", tag, **kw)

    def queue_unit_action(self, tag=None, **kw):
        return self._sc("CQueueUnitActionCommand", tag, **kw)

    def set_army_leader(self, tag=None, **kw):
        return self._sc("CSetArmyLeaderCommand", tag, **kw)

    def learn_trait(self, tag=None, **kw):
        return self._sc("CLearnTraitCommand", tag, **kw)

    def promote_unit_leader(self, tag=None, **kw):
        return self._sc("CPromoteUnitLeaderCommand", tag, **kw)

    def create_unit_leader(self, tag=None, **kw):
        return self._sc("CCreateUnitLeaderCommand", tag, **kw)

    # --- battle plans (macro orders) ---
    def order_new_front(self, tag=None, **kw):
        return self._sc("COrderNewFrontCommand", tag, **kw)

    def order_new_fallback(self, tag=None, **kw):
        return self._sc("COrderNewFallbackCommand", tag, **kw)

    def order_execute(self, tag=None, **kw):
        return self._sc("COrderExecuteCommand", tag, **kw)

    def order_delete(self, tag=None, **kw):
        return self._sc("COrderDeleteCommand", tag, **kw)

    def order_delete_all(self, tag=None, **kw):
        return self._sc("COrderDeleteAllCommand", tag, **kw)

    def order_set_path(self, tag=None, **kw):
        return self._sc("COrderSetPathCommand", tag, **kw)

    def order_assign(self, tag=None, **kw):
        return self._sc("COrderAssignCommand", tag, **kw)

    def order_add_complete_plan(self, tag=None, **kw):
        return self._sc("COrderAddNewCompletePlanCommand", tag, **kw)

    def army_group(self, tag=None, **kw):
        return self._sc("CArmyGroupCommand", tag, **kw)

    # --- air ---
    def deploy_air_wing(self, tag=None, **kw):
        return self._sc("CDeployAirWingCommand", tag, **kw)

    def air_set_mission(self, tag=None, **kw):
        return self._sc("CStratAirSetMissionCommand", tag, **kw)

    def air_enable_mission(self, tag=None, **kw):
        return self._sc("CStratAirEnableMissionCommand", tag, **kw)

    def air_transfer(self, tag=None, **kw):
        return self._sc("CStratAirTransferCommand", tag, **kw)

    # --- navy ---
    def create_fleet(self, tag=None, **kw):
        return self._sc("CCreateFleetCommand", tag, **kw)

    def naval_mission_set_type(self, tag=None, **kw):
        return self._sc("CNavalMissionSetTypeCommand", tag, **kw)

    def naval_mission_set_regions(self, tag=None, **kw):
        return self._sc("CNavalMissionSetRegionsCommand", tag, **kw)

    def set_fleet_home_base(self, tag=None, **kw):
        return self._sc("CSetFleetHomeBaseCommand", tag, **kw)

    # --- diplomacy and trade ---
    def diplomatic_action(self, tag=None, **kw):
        return self._sc("CDiplomaticActionCommand", tag, **kw)

    def incoming_diplomatic_action(self, tag=None, **kw):
        return self._sc("CIncomingDiplomaticActionActingCommand", tag, **kw)

    def create_faction(self, tag=None, **kw):
        return self._sc("CCreateFactionCommand", tag, **kw)

    def create_trade(self, tag=None, **kw):
        return self._sc("CCreateTradeCommand", tag, **kw)

    def release_country(self, tag=None, **kw):
        return self._sc("CReleaseCountryCommand", tag, **kw)

    def set_occupation_policy(self, tag=None, **kw):
        return self._sc("CSetOccupationPolicyCommand", tag, **kw)

    # --- politics, advisors, companies ---
    def add_advisor(self, tag=None, **kw):
        return self._sc("CAddAdvisorCommand", tag, **kw)

    def hire_advisor(self, key, tag=None, slot_override=None):
        """Recruit a political advisor / chief of staff / theorist.

        1.19.4.1 CAddAdvisorCommand ctor 0x1CEB5C0, disassembled 2026-11-10:
            mov rax,[rsi+0x8] ; mov [rdi+0x24],rax  -> arg1 = CAdvisor* (handle at +0x08)
            mov r12,[rdx] ; mov rbx,[rdx+8] ...     -> arg2 = std::string* (slot name)
        Then CCommand::Run (CanExecute -> Post) through _run_cmd.
        """
        import advisors as _adv, api
        c = self._c(tag)
        rows = _adv.load_advisors(c.tag)
        k = (key or "").lower()
        row = None
        for r in rows:
            names = {r["character"].lower(), r["idea_token"].lower(),
                     r["character"].lower().replace(c.tag.lower() + "_", "")}
            if k in names:
                row = r
                break
        if row is None:
            return {"ok": False, "attempted": False,
                    "error": "no advisor named %r for %s" % (key, c.tag)}
        # 2026-11-12: refuse BEFORE injecting when the character's own script
        # trigger is provably unmet. legality._eval already implements the small
        # trigger subset used in common/characters; anything it cannot resolve
        # lands in `unknown` and we fall through to the engine as before.
        try:
            import legality as _leg
            _st = _leg._state(self, c.tag)
            for _bname in ("visible_block", "available_block"):
                _blk = row.get(_bname)
                if not _blk:
                    continue
                _unk = []
                if not _leg._eval(_blk, _st, _unk) and not _unk:
                    return {"ok": False, "attempted": False,
                            "advisor": row["character"], "slot": row.get("slot"),
                            "error": "%s condition unmet: {%s}"
                                     % (_bname, " ".join(_blk.split())[:160])}
        except Exception:
            pass
        ch = 0
        for r in (self.characters(c.tag) or {}).get("characters", []):
            ck = (r.get("key") or "").lower()
            if ck == row["character"].lower() or ck.endswith(
                    row["character"].lower().replace(c.tag.lower() + "_", "")):
                ch = int(r["ptr"], 16)
                break
        if not ch:
            return {"ok": False, "attempted": True,
                    "error": "CCharacter for %s not found" % row["character"]}
        # CanExecute does __dynamic_cast(resolved_handle, CReferenceObject,
        # CCharacter) — the argument is the PERSON, not the CAdvisor. Confirmed by
        # reading the typeinfo at 0x43493e8 ("10CCharacter") and 0x44d8fa0
        # ("16CReferenceObject") that 0x1CEBC00 passes to __dynamic_cast.
        adv = ch

        slot = slot_override if slot_override is not None else (row.get("slot") or "")
        nb = slot.encode("utf-8")
        sp = self.malloc(0x20)
        if len(nb) <= 15:
            self.p.write(sp, struct.pack("<QQ", sp + 0x10, len(nb)) + nb.ljust(16, b"\0"))
        else:
            b = self.malloc(len(nb) + 1)
            self.p.write(b, nb + b"\0")
            self.p.write(sp, struct.pack("<QQQ", b, len(nb), len(nb) + 1))

        # 2026-11-11 (disassembly of 0x1CEB5C0): the ctor takes EXACTLY three
        # arguments (this, CCharacter*, std::string*). It reads [rsi+0x08] into
        # cmd+0x24 and deep-copies the string at rdx into cmd+0x30; rcx/r8 are
        # never touched, so the "missing regs" theory in the work order was
        # wrong. The real defects were (a) no read-back at all (ok came straight
        # from CanExecute while the effect lands on the NEXT game tick, so a
        # later observer saw PP move and blamed a failed hire for it) and
        # (b) no reason string when CanExecute refused.
        cost = int(row.get("cost") or 0)
        try:
            pp0 = int(self.player().summary().get("political_power") or 0)
        except Exception:
            pp0 = 0
        if cost and pp0 < cost:
            return {"ok": False, "attempted": False, "advisor": row["character"],
                    "slot": slot, "cost": cost, "political_power": pp0,
                    "error": "not enough political power: have %d, need %d"
                             % (pp0, cost)}

        api.allow_call(0x1CEB5C0)
        with self:
            cmd = self.cmd.new(0x50)
            self.p.write(cmd, b"\0" * 0x50)
            self.call(0x1CEB5C0, (cmd, adv, sp))
            rv = self._run_cmd(cmd)
            err = getattr(self, "_last_cmd_error", "")
        queued = bool(rv & 0xFF)

        # Read back: the hire is applied by the game loop, not by the call.
        token = (row.get("idea_token") or "").lower()
        pp1 = pp0
        applied = False
        for _ in range(16):
            self._settle(0.25)
            try:
                pp1 = int(self.player().summary().get("political_power") or 0)
            except Exception:
                pass
            if (cost and pp1 <= pp0 - cost) or (not cost and pp1 != pp0):
                applied = True
                break
            if not queued:
                break
        if not err and not queued:
            # 2026-11-11: PP was checked above, so a refusal here is never about money.
            # The old text sent two agents hunting for political power that they already had.
            err = ("engine refused CAddAdvisorCommand::CanExecute (slot %r). Political power "
                   "was sufficient (%d >= %d). A political_advisor slot holds THREE advisors, "
                   "so 'the slot is full' is not the explanation: this character's own "
                   "available/visible trigger is unmet (Todt/Speer need their focus, Schacht "
                   "loses his after some focuses). Pick another advisor; retrying this one "
                   "fails the same way." % (slot, pp0, cost))
        return {"ok": bool(queued and (applied or not cost)),
                "attempted": True, "queued": queued, "applied": applied,
                "advisor": row["character"], "idea_token": token, "slot": slot,
                "cost": cost, "political_power_before": pp0,
                "political_power_after": pp1, "advisor_ptr": hex(adv),
                "rv": rv & 0xFF, "error": err}

    def remove_advisor(self, tag=None, **kw):
        return self._sc("CRemoveAdvisorCommand", tag, **kw)

    def replace_idea_raw(self, tag=None, **kw):
        return self._sc("CReplaceIdeaCommand", tag, **kw)

    def promote_to_country_leader(self, tag=None, **kw):
        return self._sc("CPromoteToCountryLeaderCommand", tag, **kw)

    def set_industrial_org_task(self, tag=None, **kw):
        return self._sc("CSetIndustrialOrganisationTaskCommand", tag, **kw)

    def set_design_team(self, tag=None, **kw):
        return self._sc("CSetDesignTeamCommand", tag, **kw)

    # --- special projects and science ---
    def recruit_scientist(self, tag=None, **kw):
        return self._sc("CRecruitScientistCommand", tag, **kw)

    def attach_scientist(self, tag=None, **kw):
        return self._sc("CAttachScientistCommand", tag, **kw)

    def start_project(self, tag=None, **kw):
        return self._sc("CStartProjectCommand", tag, **kw)

    def stop_project(self, tag=None, **kw):
        return self._sc("CStopProjectCommand", tag, **kw)

    # --- intelligence and operations ---
    def create_intelligence_agency(self, tag=None, **kw):
        return self._sc("CIntelligenceAgencyCreationCommand", tag, **kw)

    def recruit_operative(self, tag=None, **kw):
        return self._sc("CRecruitOperativeCommand", tag, **kw)

    def launch_operation(self, tag=None, **kw):
        return self._sc("CLaunchOperationCommand", tag, **kw)

    # --- more production ---
    def add_production_line(self, tag=None, **kw):
        return self._sc("CAddProductionLineCommand", tag, **kw)

    def set_production_priority(self, tag=None, **kw):
        return self._sc("CSetProductionLinePriorityCommand", tag, **kw)

    def update_equipment_variant(self, tag=None, **kw):
        return self._sc("CUpdateEquipmentVariantCommand", tag, **kw)

    def convert_factory(self, tag=None, **kw):
        return self._sc("CConvertFactoryCommand", tag, **kw)

    def set_garrison_template(self, tag=None, **kw):
        return self._sc("CSetCountryGarrisonTemplateCommand", tag, **kw)

    def set_supply_capital_node(self, tag=None, **kw):
        return self._sc("CSetSupplyCapitalNodeCommand", tag, **kw)

    def select_event_option(self, tag=None, **kw):
        return self._sc("CSelectEventOptionCommand", tag, **kw)


for _n in dir(MacroMixin2):
    if not _n.startswith("__"):
        setattr(Game, _n, getattr(MacroMixin2, _n))


# ============================================================================
#  RESOLVING VALUES BY NAME: so callers never have to handle raw pointers
#  "focus:GER_rhineland", "tech:interwar_artillery", "country:FRA", "state:51" ...
# ============================================================================
class ResolverMixin:
    REF_TYPES = {
        "focus": "Focus key (hoi4_focus_list / hoi4_selectable_focuses)",
        "tech": "Technology name (hoi4_research_available)",
        "idea": "Idea, law or advisor key (hoi4_ideas_available)",
        "decision": "Decision key (hoi4_decisions)",
        "building": "Building key (hoi4_building_types) -> id",
        "equipment": "Equipment name (hoi4_equipment_types) -> id",
        "country": "Country tag (GER/FRA/...) -> tag id",
        "state": "State id",
        "character": "Character key (hoi4_characters / hoi4_generals)",
        "template": "Division template: index or name (hoi4_division_templates)",
        "army": "Army index (hoi4_armies)",
        "fleet": "Fleet index or name (hoi4_fleets)",
        "subunit": "Battalion type key (hoi4_subunit_types)",
        "wargoal": "War goal name (hoi4_list_database war_goals)",
        "occupationlaw": "Occupation law name",
        "int": "A plain integer",
        "ptr": "Ham pointer (0x...)",
    }

    def resolve(self, spec, tag=None):
        """Turn a 'kind:value' reference into a pointer or id.
        A number, or 0x..., is returned unchanged."""
        if isinstance(spec, int):
            return spec
        s = str(spec).strip()
        if s.startswith("0x"):
            return int(s, 16)
        if ":" not in s:
            return int(s)
        kind, _, val = s.partition(":")
        kind = kind.lower()
        p = self.p
        c = self._c(tag)
        if kind == "int":
            return int(val)
        if kind == "ptr":
            return int(val, 0)
        if kind == "focus":
            f = self.find_focus(val, c.tag)
            if not f:
                for x in self.continuous_focuses(c.tag):
                    if x["key"] == val:
                        return int(x["ptr"], 16)
            if not f:
                raise KeyError("no such focus: %s" % val)
            return f
        if kind == "tech":
            self._load_tech_db()
            t = self._tech_by_name.get(val)
            if not t:
                raise KeyError("no such technology: %s" % val)
            return t
        if kind == "idea":
            for i in self.available_ideas(c.tag):
                if i["key"] == val:
                    return i["ptr"]
            raise KeyError("no such idea: %s" % val)
        if kind == "decision":
            for which in ("visible", "active", "all"):
                for d in self.decisions(c.tag, which):
                    if d["key"] == val:
                        return d["ptr"]
            raise KeyError("no such decision: %s" % val)
        if kind == "building":
            self._load_buildings()
            b = self._bld_by_key.get(val)
            if not b:
                raise KeyError("no such building: %s" % val)
            return b["id"]
        if kind == "equipment":
            self._load_equipment()
            t = self._eq_by_name.get(val)
            if not t:
                raise KeyError("no such equipment: %s" % val)
            return p.i32(t + ET_ID)
        if kind == "country":
            self._load_tags()
            v = val.upper()
            if v not in self._tags:
                raise KeyError("no such tag: %s" % v)
            return self._tags.index(v)
        if kind == "state":
            return int(val)
        if kind == "character":
            for ch in self.characters(c.tag, limit=5000)["characters"]:
                if ch["key"] == val or ch["name"] == val:
                    return int(ch["ptr"], 16)
            raise KeyError("no such character: %s" % val)
        if kind == "template":
            tl = self.division_templates(c.tag)
            if val.isdigit():
                return int(tl[int(val)]["ptr"], 16)
            for t in tl:
                if t["name"] == val:
                    return int(t["ptr"], 16)
            raise KeyError("no such template: %s" % val)
        if kind == "army":
            al = _arr(p, c.ptr, CO_ARMIES)
            return al[int(val)]
        if kind == "fleet":
            fl = self.fleets(c.tag)
            if val.isdigit():
                return int(fl[int(val)]["ptr"], 16)
            for f in fl:
                if f["name"] == val:
                    return int(f["ptr"], 16)
            raise KeyError("no such fleet: %s" % val)
        if kind == "subunit":
            for s in self.subunit_types(c.tag):
                if s["key"] == val:
                    return int(s["ptr"], 16)
            raise KeyError("no such battalion: %s" % val)
        if kind in ("wargoal", "occupationlaw"):
            which = "war_goals" if kind == "wargoal" else "occupation_laws"
            cls, off, _ = self.DATABASE_ARRAYS[which]
            db = self.oi.singleton(cls)
            for it in _arr(p, db, off):
                nm = None
                for o in range(0, 0x300, 8):
                    v2 = item_name(p, it + o)
                    if v2 and v2.isprintable() and 2 < len(v2) < 48:
                        nm = v2
                        break
                if nm == val:
                    return it
            raise KeyError("no such %s: %s" % (kind, val))
        raise KeyError("unknown reference kind: %s (valid: %s)"
                       % (kind, sorted(self.REF_TYPES)))

    def send_command(self, name: str, fields: dict = None, tag=None, force=False):
        """Send ANY player command.
        field values may be 'focus:GER_x', 'tech:y', 'country:FRA', 'state:51',
        'character:GER_z', 'ptr:0x...' or a plain number.

        SAFETY: writing raw offsets into a command whose layout is unverified can
        crash the game, so that case requires force=True."""
        c = self._c(tag)
        f = {}
        for k, v in (fields or {}).items():
            f[k] = self.resolve(v, c.tag)
        spec0, src0 = self.cmd.spec_for(name)
        raw_keys = [k for k in f if "@" in str(k) or str(k).startswith("0x")]
        known_offs = {fl[0] for fl in spec0["fields"]}
        risky = [k for k in raw_keys
                 if (int(str(k).split("@")[-1], 16) if "0x" in str(k) else -1) not in known_offs]
        if risky and not force:
            return {"ok": False, "blocked": True,
                    "error": "This command's field layout is unverified; writing to unknown offsets "
                             "can crash the game: %s" % risky,
                    "hint": "Check the parameters with hoi4_command_help. "
                            "Pass force=true to try anyway.",
                    "known_offsets": [hex(o) for o in sorted(known_offs)]}
        # do the pointer fields actually point at a real object?
        for fl in spec0["fields"]:
            if fl[1] != "<Q":
                continue
            key = fl[2]
            val = f.get(key)
            if val is None:
                continue
            if val and not (0x10000 < val < 0x7FFFFFFFFFFF):
                return {"ok": False, "blocked": True,
                        "error": "field %s is not a valid object pointer: %#x" % (key, val)}
        spec, _src = self.cmd.spec_for(name)
        has_tag = any(fl[2] == "tag" for fl in spec["fields"])
        if has_tag and "tag" not in f and "0x24" not in f:
            f["tag"] = c.tag_id
        with self:
            r = self.cmd.send_raw(name, f)
        self._settle()
        return r

    # ------------------------------------------------------- command help
    OBJECT_BY_NAMEOFF = {
        0x08: "CTechnologyTemplate / CEquipmentType / CBuilding (tech: / equipment: / building:)",
        0x10: "CEquipmentType / CSubUnitDefinition (equipment: / subunit:)",
        0x18: "CNationalFocus / CIdea (focus: / idea:)",
        0x20: "CCharacter / CReferencedDivisionTemplate (character: / template:)",
        0xD8: "CFleet (fleet:)",
        0x118: "CDecision (decision:)",
        0x248: "CBuildingTemplate (building:)",
    }

    KIND_HELP = {
        "handle": "A unit or character HANDLE: 'charhandle:GER_x' or 'divhandle:<index>' "
                  "(or get one with hoi4_handle; written as an i64)",
        "ptr": "An object pointer: 'focus:', 'tech:', 'idea:', 'decision:', 'template:', "
               "a reference such as 'subunit:', 'fleet:', 'character:' or 'wargoal:'",
        "int": "Integer",
        "bool": "0 / 1",
        "deref": "The object's first int32, usually a CCountryTag. Pass 'country:FRA'",
    }

    def command_help(self, name: str):
        """Explain a player command's parameters in a form a caller can act on."""
        spec, src = self.cmd.spec_for(name)
        key = spec.get("name", name)
        ctors = self.cmd.ctor_catalog().get(key, {})
        bykind = {f["offset"]: f for f in ctors.get("fields", [])}
        auto = self.cmd.catalog().get(key, {})
        nameoffs = {o: n for o, k, n in auto.get("fields", [])}
        params = []
        for off, fmt, fname in spec["fields"]:
            if fname == "tag":
                params.append({"name": "tag", "offset": hex(off), "type": "CCountryTag",
                               "note": "filled in automatically (the player's country); "
                                       "use 'country:FRA' for another country"})
                continue
            f = bykind.get(off, {})
            kind = f.get("kind", "")
            if kind.startswith("field"):
                kind = "ptr"
            hint = self.OBJECT_BY_NAMEOFF.get(nameoffs.get(off, 0))
            params.append({
                "name": fname, "offset": hex(off),
                "bytes": {"<q": 8, "<Q": 8, "<i": 4, "<h": 2, "<B": 1}.get(fmt, 8),
                "kind": kind or ("ptr" if fmt in ("<Q", "<q") else "int"),
                "ctor_arg": f.get("arg"),
                "expects": hint or self.KIND_HELP.get(kind, "number"),
            })
        return {"command": key, "source": src, "size": spec.get("size"),
                "parameters": params,
                "usage": "hoi4_send_command(name='%s', fields={'arg0': '<value>', ...})" % key,
                "reference_types": self.REF_TYPES}


for _n in dir(ResolverMixin):
    if not _n.startswith("__"):
        setattr(Game, _n, getattr(ResolverMixin, _n))


# ============================================================================
#  HANDLES, DIPLOMATIC ACTION CLASSES, AND LATER ADDITIONS
#  The game refers to units and characters by a {int32 type, int32 id} "handle"
#  rather than a pointer. In commands these are written as two adjacent int32s.
# ============================================================================
HANDLE_OFFSETS = {          # class -> offset of the handle inside the object
    "CCharacter": 0x08,
    "CArmy": 0x18,          # CArmy is a division
}


class HandleMixin:
    def handle_of(self, ptr):
        """Return an object's (type, id) handle."""
        if isinstance(ptr, str):
            ptr = int(ptr, 16)
        cls = self.oi.class_of(ptr)
        off = HANDLE_OFFSETS.get(cls, 0x08)
        a, b = struct.unpack("<ii", self.p.read(ptr + off, 8))
        return {"class": cls, "type": a, "id": b, "ptr": hex(ptr)}

    def character_handle(self, key, tag=None):
        p = self.p
        for ch in self.all_characters(tag):
            if item_name(p, ch + CH_KEY) == key:
                return self.handle_of(ch)
        raise KeyError("no such character: %s" % key)

    def division_handle(self, index, tag=None):
        d = _arr(self.p, self._c(tag).ptr, CO_ARMIES)[index]
        return self.handle_of(d)

    # ------------------------------------------------------------ diplomacy
    def diplomatic_action_classes(self):
        """The game's 47 diplomatic action classes (CDiplomaticAction subclasses).
        CDiplomaticActionCommand carries one of them at +0x28."""
        if sys.platform == "win32":
            raise NotImplementedError(
                "diplomatic_action_classes() walks Itanium RTTI typeinfo and .rela.dyn "
                "in the Linux ELF. On hoi4.exe the equivalent needs the MSVC "
                "TypeDescriptor table (research/windows/rtti_win.py) plus the base-class "
                "chain, which is not implemented yet.")
        import rtti as _R
        from elf import ELF
        e = ELF(__import__("config").binary_path())
        m = _R.build()
        rel, _s = _R.all_relocs(e)
        ti = m["typeinfo"]
        out = []
        for slot, name in ti.items():
            b = rel.get(slot + 16)
            if b in ti and ti[b] == "17CDiplomaticAction":
                nm = re.sub(r"^\d+", "", name)
                vp = None
                for k, vs in m["vtables"].items():
                    if k == name:
                        vp = vs[0]
                out.append({"class": nm, "vtable": hex(vp) if vp else None})
        return {"count": len(out), "actions": sorted(out, key=lambda x: x["class"])}


import re  # noqa: E402
for _n in dir(HandleMixin):
    if not _n.startswith("__"):
        setattr(Game, _n, getattr(HandleMixin, _n))


# handle: register the reference kind with the resolver
_orig_resolve = Game.resolve


def _resolve2(self, spec, tag=None):
    if isinstance(spec, str) and spec.startswith(("charhandle:", "divhandle:")):
        kind, _, val = spec.partition(":")
        h = (self.character_handle(val, tag) if kind == "charhandle"
             else self.division_handle(int(val), tag))
        return (h["id"] << 32) | (h["type"] & 0xFFFFFFFF)   # i64: [tip|id]
    return _orig_resolve(self, spec, tag)


Game.resolve = _resolve2
Game.REF_TYPES = dict(Game.REF_TYPES,
                      charhandle="Character handle (write as i64: i64@<offset>)",
                      divhandle="Division handle (write as i64)")


# ============================================================================
#  LATER ADDITIONS: faction members, trade, intelligence, occupation, subsystem browser
# ============================================================================
CO_TRADE_ROUTES     = 0x112B08   # CPdxArray<CResourceDeliveryRoute*>
CO_RESOURCE_ORIGINS = 0x112A90
FACTION_MEMBERS     = 0x58       # CPdxArray<CCountry*>


class ExtraMixin:
    def faction_members(self, tag=None):
        info = self.faction(tag)
        if not info:
            return {"faction": None, "members": []}
        return {"faction": info.get("name"), "leader": info.get("leader"),
                "members": info.get("members") or []}

    def trade_routes(self, tag=None, limit=40):
        p = self.p
        out = []
        for r in _arr(p, self._c(tag).ptr, CO_TRADE_ROUTES)[:limit]:
            out.append({"ptr": hex(r), "class": self.oi.class_of(r)})
        return {"count": _count(p, self._c(tag).ptr, CO_TRADE_ROUTES), "routes": out}

    def wars(self, tag=None):
        """Wars we are in. PURE READS ONLY.

        This used to call `country_stats()`, which ptrace-calls ~45 trigger
        functions recovered from a 1.19.2 scan; the game died within 10 s every
        time `hoi4_wars` was invoked. `combat.enemy_tags` reads the CWarRelation
        objects straight out of the country's diplomacy status instead.
        """
        import combat
        c = self._c(tag)
        enemies = combat.enemy_tags(self, c.tag)
        try:
            combats = len(combat.combats(self, only_mine=True))
        except Exception:
            combats = 0
        return {"at_war": bool(enemies), "enemies": enemies,
                "active_combats": combats,
                "armies_in_combat": self.p.i32(c.ptr + 0x12F0)}

    COUNTRY_SUBSYSTEMS = {
        "technology": CO_TECH_STATUS, "production": CO_PRODUCTION,
        "army_upgrades": CO_ARMY_UPGRADES, "diplomacy": CO_DIPLOMACY,
        "politics": CO_POLITICS, "decisions": CO_DECISIONS,
        "intelligence_agency": CO_INTEL_AGENCY, "country_intel": CO_COUNTRY_INTEL,
        "focus_tree": CO_FOCUS_TREE, "focus_progress": CO_FOCUS_PROGRESS,
        "continuous_focus_palette": CO_CONT_FOCUS_PAL, "faction": CO_FACTION,
        "resources": CO_RESOURCES, "occupation": CO_OCCUPATION,
        "operations": CO_OPERATIONS, "project_pool": CO_PROJECT_POOL,
        "program_status": CO_PROGRAM_STATUS,
    }

    def country_subsystems(self, tag=None):
        """Every subsystem of CCountry: class plus the arrays inside it.
        To explore an unwrapped mechanic, continue from here with hoi4_browse_object."""
        from probe import describe
        p = self.p
        c = self._c(tag)
        out = {}
        for name, off in sorted(self.COUNTRY_SUBSYSTEMS.items()):
            ptr = p.u64(c.ptr + off)
            if not ptr:
                out[name] = None
                continue
            try:
                arrs = [{"offset": r["off"], "size": r["size"], "elem": r["elem"]}
                        for r in describe(p, ptr, self.oi, span=0x300) if r["size"]]
            except Exception:
                arrs = []
            out[name] = {"offset": hex(off), "ptr": hex(ptr),
                         "class": self.oi.class_of(ptr), "arrays": arrs[:12]}
        return out

    def promote_general(self, character_key, tag=None):
        """Promote a corps commander to field marshal. Costs command power.

        NOTE: this used to silently do nothing. The command was given a CCharacter
        handle (type 73). The CanExecute of CPromoteUnitLeaderCommand
        (img+0x23F51A0) resolves the handle and does `cmpl $0x1, 0xD7C(obj)`,
        does. +0xD7C is CArmyLeader's RANK field (0=field marshal, 1=corps
        commander, 2=admiral). So the command wants a CArmyLeader (type 4713) and
        with the wrong type the compare never matched. The same mistake crashed the
        crashed the game once before (see FINDINGS).
        """
        p = self.p
        ch = None
        for c in self.all_characters(tag):
            if item_name(p, c + CH_KEY) == character_key:
                ch = c
                break
        if ch is None:
            raise KeyError("no such character: %s" % character_key)
        lp = p.u64(ch + 0xC0)
        if not lp:
            return {"ok": False, "error": "%s is not an army leader (no CArmyLeader)"
                                          % character_key}
        h = self.handle_of(lp)
        if h["type"] != 4713:
            return {"ok": False, "error": "beklenen CArmyLeader handle'i (4713), gelen %d"
                                          % h["type"]}
        rank = p.i32(lp + 0xD7C)
        if rank != 1:
            names = {0: "already a field marshal", 1: "a corps commander", 2: "an admiral"}
            return {"ok": False, "rank": rank,
                    "error": "only a CORPS COMMANDER can be promoted to field marshal; %s %s"
                             % (character_key, names.get(rank, "rank %d" % rank))}
        res = self.send_command("CPromoteUnitLeaderCommand",
                                {"arg0": (h["id"] << 32) | (h["type"] & 0xFFFFFFFF)}, tag)
        try:
            new_rank = p.i32(lp + 0xD7C)
        except Exception:
            new_rank = None
        if isinstance(res, dict):
            res["rank_before"] = rank
            res["rank_after"] = new_rank
            res["promoted"] = (new_rank == 0)
            if new_rank == rank:
                res["note"] = ("Rank did not change, the game refused. The most likely reason "
                               "is insufficient command power, which it rejects silently.")
        return res

    def recruit_unit_leader(self, leader_type=0, tag=None):
        """Recruit a leader: 0=general, 1=field marshal, 2=admiral (costs political power)."""
        return self.send_command("CCreateUnitLeaderCommand", {"arg0": int(leader_type)}, tag)

    def diplomatic_action_info(self):
        """A guide to the diplomatic actions."""
        return {
            "actions": self.diplomatic_action_classes()["actions"],
            "how": "CDiplomaticActionCommand carries a CDiplomaticAction at +0x28. "
                   "These are polymorphic structures of 0x90+ bytes containing embedded sub-objects; "
                   "is NOT wrapped yet. To answer incoming diplomatic offers, "
                   "CIncomingDiplomaticActionActingCommand can be used.",
            "incoming_command": self.command_help("CIncomingDiplomaticActionActingCommand"),
        }


for _n in dir(ExtraMixin):
    if not _n.startswith("__"):
        setattr(Game, _n, getattr(ExtraMixin, _n))


class MoveMixin:
    """Route A: per-division province orders (the player's right click)."""

    def move_divisions(self, divisions=None, province=None, orders_group=None, tag=None,
                       mode="move", wait=12.0, limit=60, force=False):
        import movemicro
        return movemicro.move_divisions(self, divisions, province, orders_group, tag,
                                        mode=mode, wait=wait, limit=limit, force=force)

    def deploy_to_border(self, target_tag, orders_group=None, include_unassigned=True, tag=None):
        import movemicro
        return movemicro.deploy_to_border(self, target_tag, orders_group,
                                          include_unassigned, tag)

    def attack_country(self, target_tag, mode="balanced", max_attacks=2, tag=None):
        import movemicro
        return movemicro.attack_country(self, target_tag, mode, max_attacks, tag)

    def plug_border_gaps(self, target_tag, min_garrison=1, max_gaps=None, dry_run=False):
        import movemicro
        return movemicro.plug_border_gaps(self, target_tag, min_garrison,
                                         max_gaps, dry_run)

    def wartime_status(self):
        import movemicro
        return movemicro.wartime_status(self)

    def wartime_step(self, target_tag=None, max_attacks=2, dry_run=False):
        import movemicro
        return movemicro.wartime_step(self, target_tag, max_attacks, dry_run)

    # --- fifth stage: campaign directive + CA-level tactical tick ---
    def set_front_directive(self, enemy_tag, stance="balanced_attack",
                            priority_provinces=None, priority_states=None,
                            min_garrison=1, deploy=None, tag=None):
        import movemicro
        return movemicro.set_front_directive(self, enemy_tag, stance,
                                             priority_provinces, priority_states,
                                             min_garrison, deploy, tag)

    def front_directives(self):
        import movemicro
        return movemicro.front_directives(self)

    def clear_front_directive(self, enemy_tag=None):
        import movemicro
        return movemicro.clear_front_directive(enemy_tag)

    def tactical_front_tick(self, enemy_tag=None, force=False, dry_run=False):
        import movemicro
        return movemicro.tactical_front_tick(self, enemy_tag, force, dry_run)


# Copy the raw descriptors, not the bound attributes: getattr() unwraps
# staticmethod/classmethod/property, which silently turned HousekeepMixin's
# static `_prod_group(equipment)` into a bound method and made every
# housekeep production pass die with "takes 1 positional argument but 2".
for _n in dir(MoveMixin):
    if not _n.startswith("__"):
        setattr(Game, _n, MoveMixin.__dict__.get(_n, getattr(MoveMixin, _n)))


from housekeep import HousekeepMixin

for _n in dir(HousekeepMixin):
    if not _n.startswith("__"):
        setattr(Game, _n, HousekeepMixin.__dict__.get(_n, getattr(HousekeepMixin, _n)))
