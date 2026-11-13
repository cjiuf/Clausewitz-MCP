"""Turn loop: let the model sleep and wake at the right moment.

Checking the game every second burns tokens; never checking misses openings. This
lets the caller say "do not wake me until one of these happens, but look again
after N days at the latest", and tells it why it woke up.

ALL PURE READS. The one exception is pausing on wake (`pause_on_wake`), which goes
through the same command channel as the player's space key. No injected call is
ever made inside the loop, and paths like `country_stats()` / `g.wars()` that stop
every game thread are avoided, one of those inside a tick loop froze a live game.

Signals, all taken from game state rather than the UI:
  focus         CCountry+0x1348         -> focus finished, or none selected
  research      research_slots()        -> research done, a slot freed
  decisions     decisions()             -> a new decision became available
  production    CProductionStatus+0x58  -> requested factories < available (idle)
  construction  CProductionStatus+0x70  -> construction queue emptied
  deployment    CDeploymentStatus+0x80  -> a division finished training
  war           CWarRelation / battles  -> war declared, new battle, battle lost
  political_power                       -> threshold reached (advisor or decision)
"""
import time

import combat

CO_PRODUCTION  = 0xF40        # CProductionStatus
CO_DEPLOYMENT  = 0xF48        # CDeploymentStatus
PROD_MIL_LINES = 0x58         # CPdxArray<CMilitaryProductionLine*>
PROD_BLD_LINES = 0x70         # CPdxArray<CBuildingProductionLine*>, construction queue
DEP_QUEUE      = 0x80         # CPdxArray<CMilitaryDeployment*>
DEP_PROGRESS   = 0x60         # training progress (fixed point)
DEP_TARGET     = 0x68         # target (fixed point, usually 1.0)

ALL_SIGNALS = ("focus", "research", "decisions", "production",
               "construction", "deployment", "war", "political_power",
               "housekeep")


def _fx(p, a):
    return p.i64(a) / 1e5


def deployment_status(g):
    """Deployment queue: how many divisions are queued, how many are ready."""
    from game import _arr
    p = g.p
    try:
        dep = p.u64(g.player().ptr + CO_DEPLOYMENT)
        q = _arr(p, dep, DEP_QUEUE)
    except Exception:
        return {"queued": 0, "ready": 0}
    ready = 0
    for d in q:
        try:
            prog, target = _fx(p, d + DEP_PROGRESS), _fx(p, d + DEP_TARGET)
            if target > 0 and prog >= target * 0.995:
                ready += 1
        except Exception:
            pass
    return {"queued": len(q), "ready": ready}


def factory_status(g):
    """Detect idle factories.

    Military: if the total REQUESTED by production lines is below what is
    available, the difference is idle. (HOI4 caps requests at what exists, so
    requested > available means nothing is idle.)
    Civilian: an empty construction queue means the civilian factories are idle.
    """
    from game import _arr
    p = g.p
    c = g.player()
    try:
        prod = p.u64(c.ptr + CO_PRODUCTION)
        mil_lines = len(_arr(p, prod, PROD_MIL_LINES))
        bld_lines = len(_arr(p, prod, PROD_BLD_LINES))
    except Exception:
        mil_lines = bld_lines = 0
    try:
        lines = g.production_lines()
        requested = sum(int(l.get("factories") or 0) for l in lines
                        if l.get("class") == "CMilitaryProductionLine")
        nav_requested = sum(int(l.get("factories") or 0) for l in lines
                            if l.get("class") == "CNavalProductionLine")
    except Exception:
        requested = nav_requested = 0
    # 2026-11-12: dockyards were added to military_available, so a finished ship
    # queue looked like idle ARMY factories and the agent's next
    # set_production_amount was refused with "not enough spare factories".
    avail = c.military_factories
    return {"military_available": avail, "military_requested": requested,
            "military_idle": max(0, avail - requested),
            "naval_available": c.naval_dockyards, "naval_requested": nav_requested,
            "naval_idle": max(0, c.naval_dockyards - nav_requested),
            "production_lines": mil_lines,
            "civilian_factories": c.civilian_factories,
            "construction_queue": bld_lines,
            "civilian_idle": c.civilian_factories if bld_lines == 0 else 0}


def snapshot(g, signals=ALL_SIGNALS):
    """Current value of everything being watched. PURE READS."""
    s = {"date": None, "tag": None}
    try:
        s["date"] = g.date_raw()
    except Exception:
        pass
    try:
        s["tag"] = g.player_tag()
    except Exception:
        # If the played country cannot be resolved, no signal can be trusted.
        return s
    if "focus" in signals:
        try:
            f = g.current_focus()
            s["focus"] = f.get("key") if f else None
        except Exception:
            s["focus"] = None
        try:
            s["focus_progress"] = round(g.focus_progress().get("progress", 0.0), 3)
        except Exception:
            pass
    if "research" in signals:
        try:
            slots = g.research_slots()
            s["research"] = tuple(sl.get("researching") for sl in slots)
            s["research_free"] = sum(1 for sl in slots if not sl.get("researching"))
        except Exception:
            pass
    if "decisions" in signals:
        try:
            s["decisions"] = frozenset(d.get("key") for d in g.decisions(which="visible"))
        except Exception:
            pass
    if "production" in signals or "construction" in signals:
        try:
            s["factories"] = factory_status(g)
        except Exception:
            pass
    if "deployment" in signals:
        try:
            s["deployment"] = deployment_status(g)
        except Exception:
            pass
    if "war" in signals:
        try:
            s["enemies"] = frozenset(combat.enemy_tags(g))
            recs = combat.combats(g, only_mine=True)
            s["combats"] = len(recs)
            s["combat_provinces"] = frozenset(r["province"] for r in recs)
        except Exception:
            pass
    if "political_power" in signals:
        try:
            s["political_power"] = g.player().political_power
        except Exception:
            pass
    if "housekeep" in signals:
        try:
            slots = g.research_slots()
            fs = factory_status(g)
            pp = g.player().political_power
            s["housekeep"] = {
                "empty_research": sum(1 for x in slots if not x.get("researching")),
                "focus": (g.current_focus() or {}).get("key"),
                "pp": pp,
                "military_idle": fs.get("military_idle", 0),
                "construction_queue": fs.get("construction_queue", 0),
                "unassigned": len(g.unassigned_divisions()),
            }
        except Exception:
            pass
    return s


def wake_reasons(prev, cur, pp_threshold=None):
    """Changes between two snapshots that the caller should know about."""
    out = []
    if not prev or not cur or prev.get("tag") != cur.get("tag"):
        if prev and cur and prev.get("tag") != cur.get("tag"):
            out.append("played country changed: %s -> %s" % (prev.get("tag"), cur.get("tag")))
        return out

    if "focus" in prev and "focus" in cur and prev["focus"] != cur["focus"]:
        out.append("focus finished/changed: %s -> %s" % (prev["focus"], cur["focus"]))
    if cur.get("focus") is None and prev.get("focus") is None and "focus" in cur:
        pass  # an empty focus should not fire every tick; only the transition does

    if "research" in prev and "research" in cur:
        done = [t for t in prev["research"] if t and t not in cur["research"]]
        if done:
            out.append("research finished: %s" % ", ".join(done))
        if cur.get("research_free", 0) > prev.get("research_free", 0):
            out.append("research slot freed (%d free)" % cur["research_free"])

    if "decisions" in prev and "decisions" in cur:
        fresh = cur["decisions"] - prev["decisions"]
        if fresh:
            out.append("new decision: %s" % ", ".join(sorted(fresh)[:5]))

    pf, cf = prev.get("factories"), cur.get("factories")
    if pf and cf:
        if cf["military_idle"] > 0 and cf["military_idle"] > pf["military_idle"]:
            out.append("idle military factories: %d" % cf["military_idle"])
        if cf["construction_queue"] == 0 and pf["construction_queue"] > 0:
            out.append("construction queue empty (%d civilian factories idle)" % cf["civilian_factories"])

    pd, cd = prev.get("deployment"), cur.get("deployment")
    if pd and cd and cd["ready"] > pd["ready"]:
        out.append("divisions ready to deploy: %d" % cd["ready"])

    if "enemies" in prev and "enemies" in cur:
        fresh = cur["enemies"] - prev["enemies"]
        ended = prev["enemies"] - cur["enemies"]
        if fresh:
            out.append("WAR: %s" % ", ".join(sorted(fresh)))
        if ended:
            out.append("war ended: %s" % ", ".join(sorted(ended)))
    if "combat_provinces" in prev and "combat_provinces" in cur:
        fresh = cur["combat_provinces"] - prev["combat_provinces"]
        if fresh:
            out.append("new battle: %s" % ", ".join(str(x) for x in sorted(fresh)[:5]))

    if pp_threshold is not None and cur.get("political_power") is not None:
        if cur["political_power"] >= pp_threshold > prev.get("political_power", 0):
            out.append("political power reached %d" % cur["political_power"])
    return out


class TurnLoop:
    """The sleep and wake loop.

    wait() returns when a watched signal changes, when the requested date arrives,
    or when `max_days` runs out, whichever comes first. It ALWAYS returns; there
    is no path that waits forever.
    """

    def __init__(self, game, poll=4.0, pause_on_wake=False):
        self.g = game
        self.poll = float(poll)
        self.pause_on_wake = bool(pause_on_wake)
        self.last = None

    def snapshot(self, signals=ALL_SIGNALS):
        return snapshot(self.g, signals)

    def wait(self, max_days=7, until_date=None, signals=ALL_SIGNALS,
             pp_threshold=None, poll=None, timeout_seconds=900,
             pause_on_wake=None, wartime_micro=False, wartime_enemy=None,
             wartime_interval_days=3, wartime_report_days=15):
        """Wait for a signal, for the date, or for max_days: whichever first.

        max_days       : in-game days. Even with "do not disturb me", it ALWAYS
                         wakes at the end of this window.
        until_date     : (year, month, day). Sleep until this date.
        timeout_seconds: real-time safety brake. While the game is paused the game
                         clock never moves, and we must not wait forever.
        wartime_micro  : while at war, run one local wartime_step every
                         `wartime_interval_days` game days; do not wake the model
                         unless the front moved >=4 provinces or 15 days passed.
        """
        import api
        g = self.g
        poll = float(poll if poll is not None else self.poll)
        signals = tuple(signals)
        if wartime_micro:
            signals = tuple(sorted(set(signals) | {"war"}))
        base = snapshot(g, signals)
        start_hours = base.get("date")
        target_hours = None
        if until_date:
            target_hours = api.parts_to_date(*until_date)
        deadline_hours = (start_hours + int(max_days) * 24) if start_hours is not None else None
        if target_hours is not None and deadline_hours is not None:
            deadline_hours = min(deadline_hours, target_hours)
        # In war the game can run at speed 5, which is ~4.5 game days per real
        # second, so a "max_days" window can expire in a couple of seconds and
        # the 3-day micro tick would only ever fire ONCE. When wartime_micro is
        # on, the window is counted in MICRO TICKS instead: the loop keeps
        # ticking every `wartime_interval_days` game days until it has done
        # `max_days` of them.
        war_ticks_target = int(max_days) if wartime_micro else None

        t_start = time.time()
        prev = base
        last_housekeep_date = start_hours
        war_enemy = (wartime_enemy or "").upper() or None
        last_war_tick = start_hours
        last_war_report = start_hours
        war_changed = 0
        war_steps = 0
        war_last = None
        war_baseline = False
        # 2026-11-11: the game pauses itself on every "NATIONAL FOCUS COMPLETED"
        # / research-finished popup. wait() then slept until the 900 s real-time
        # brake and reported "the game may be paused", which is exactly how the
        # previous run deadlocked for hours. Clear it here instead: three
        # consecutive polls with a frozen clock while paused means a popup is
        # holding the game, not that the caller wanted a pause.
        stuck_paused = 0
        auto_unpaused = 0
        last_stamp = base.get("date")
        while True:
            time.sleep(poll)
            cur = snapshot(g, signals)
            try:
                import session as _sess
                if _sess.is_paused(g):
                    if cur.get("date") == last_stamp:
                        stuck_paused += 1
                    else:
                        stuck_paused = 0
                    last_stamp = cur.get("date")
                    if stuck_paused >= 3:
                        g.dismiss_popups_and_unpause()
                        auto_unpaused += 1
                        stuck_paused = 0
                else:
                    stuck_paused = 0
                    last_stamp = cur.get("date")
            except Exception:
                pass
            reasons = wake_reasons(prev, cur, pp_threshold)
            if "housekeep" in signals and cur.get("housekeep"):
                h = cur["housekeep"]
                due = (last_housekeep_date is None or
                       (cur.get("date") is not None and cur["date"] - last_housekeep_date >= 240))
                hot = (h.get("empty_research", 0) > 0 or not h.get("focus") or
                       h.get("pp", 0) >= 180 or h.get("military_idle", 0) >= 2 or
                       h.get("construction_queue", 0) <= 1 or h.get("unassigned", 0) >= 3)
                if hot and due:
                    reasons.append("housekeep: empty_research=%s focus=%s pp=%s mil_idle=%s "
                                   "construction=%s unassigned=%s" %
                                   (h.get("empty_research"), h.get("focus"), h.get("pp"),
                                    h.get("military_idle"), h.get("construction_queue"),
                                    h.get("unassigned")))
                    last_housekeep_date = cur.get("date")
            now_h = cur.get("date")
            if wartime_micro and now_h is not None:
                enemies = sorted(cur.get("enemies") or [])
                enemy = war_enemy or (enemies[0] if enemies else None)
                if enemy:
                    if not war_baseline:
                        try:
                            # Do not wipe a baseline the caller just reset; front_changes
                            # itself rebuilds on a new pid / older date.
                            g.front_changes(enemy, reset=False)
                        except Exception:
                            pass
                        war_baseline = True
                    due = (last_war_tick is None or
                           now_h - last_war_tick >= int(wartime_interval_days) * 24)
                    if due:
                        try:
                            fc = g.front_changes(enemy, reset=False)
                            war_changed += int(fc.get("changed_count") or 0)
                        except Exception:
                            pass
                        try:
                            war_last = g.wartime_step(enemy, max_attacks=2)
                            war_steps += 1
                        except Exception as e:
                            war_last = {"ok": False, "error": str(e)}
                        last_war_tick = now_h
                        if war_changed >= 4:
                            reasons.append("wartime front changed: %d provinces (step %s)" %
                                           (war_changed, war_steps))
                    # In wartime mode the window is counted in MICRO TICKS, not in
                    # game days: at speed 5 the game advances ~4.5 days per real
                    # second, so a game-day window would fire on every tick and
                    # end the wait after one step.
                    if war_ticks_target is not None:
                        report_every = max(1, int(wartime_report_days))
                        if war_steps and war_steps % report_every == 0:
                            reasons.append("wartime report: %d ticks, %d province changes" %
                                           (war_steps, war_changed))
                            last_war_report = now_h
                            war_changed = 0
                    elif (last_war_report is not None and war_steps and
                            now_h - last_war_report >= int(wartime_report_days) * 24):
                        reasons.append("wartime report: %d days, %d steps, %d province changes" %
                                       (int((now_h - last_war_report) / 24), war_steps, war_changed))
                        last_war_report = now_h
                        war_changed = 0
            if war_ticks_target is not None:
                # Wartime mode: the window is a number of micro ticks.
                if war_steps >= war_ticks_target:
                    reasons.append("wartime window done (%d micro ticks)" % war_steps)
            elif now_h is not None and deadline_hours is not None and now_h >= deadline_hours:
                reasons.append("time is up (%s)" % ("target date" if target_hours is not None
                                                    and now_h >= target_hours else
                                                    "%d days" % max_days))
            if time.time() - t_start > timeout_seconds:
                reasons.append("real-time safety brake (%ds), the game may be paused"
                               % int(timeout_seconds))
            if reasons:
                self.last = cur
                paused = None
                want_pause = self.pause_on_wake if pause_on_wake is None else pause_on_wake
                if want_pause:
                    try:
                        import session
                        paused = session.pause(g, True)[0]
                    except Exception as e:
                        paused = "hata: %s" % e
                return {
                    "woke_because": reasons,
                    "waited_game_days": round(((now_h or start_hours or 0)
                                               - (start_hours or 0)) / 24.0, 2),
                    "waited_real_seconds": round(time.time() - t_start, 1),
                    "date": g.date()["text"],
                    "paused": paused,
                    "auto_unpaused": auto_unpaused,
                    "wartime": {"steps": war_steps, "changed_since_report": war_changed,
                                "last_tick": war_last} if wartime_micro else None,
                    "state": _public(cur),
                }
            prev = cur


def _public(s):
    """Make a snapshot JSON-friendly (frozenset -> list)."""
    out = {}
    for k, v in s.items():
        if isinstance(v, (frozenset, set)):
            out[k] = sorted(v)[:20]
            out[k + "_count"] = len(v)
        elif isinstance(v, tuple):
            out[k] = list(v)
        else:
            out[k] = v
    return out
