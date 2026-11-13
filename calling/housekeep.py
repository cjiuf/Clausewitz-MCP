"""One-click housekeeping: research slots, construction queue, idle military
factories, political power, and unassigned divisions.

Everything goes through the same command path as a player click (RUN_CMD).
Returns an honest per-section result; a failed sub-step never aborts the rest.
"""
from __future__ import annotations

# Set once `recruit_unit_leader` has faulted inside the game (sig=11): the call
# is retried every housekeep otherwise, and one fault per 8 minutes is a crash
# waiting to happen. Process-scoped, never persisted.
_LEADER_FAULTED = [False]


class HousekeepMixin:
    def _construction_target(self, tag=None):
        return 6

    def _pick_building(self, tag=None):
        """CA industrial timeline (work sheet task 2).

        Before 1937.06: 100% civilian factories, in the highest-infrastructure
        states (the queue order is handled by _state_priority).
        From 1937.06, at war, or when rubber/fuel is short: military factories,
        with a synthetic refinery inserted when the resource balance is negative.
        """
        d = self.date()
        at_war = False
        try:
            import combat
            at_war = bool(combat.at_war(self))
        except Exception:
            pass
        late = (d["year"], d["month"]) >= (1937, 6) or at_war
        if not late:
            return "industrial_complex"
        res = {}
        try:
            res = self.resources(tag) or {}
        except Exception:
            pass
        fuel_days = None
        try:
            fuel_days = float(self.player().summary().get("fuel") or 0)
        except Exception:
            pass
        want_synth = (res.get("rubber", 0) < 0 or
                      (fuel_days is not None and fuel_days < 500))
        # 2026-11-11: 1936 Germany has NO synthetic-refinery slot at all until
        # `synth_oil_experiments` (and the Four-Year-Plan rubber branch) unlock
        # it. housekeep_construction used to queue 8 refineries anyway; each one
        # sat at progress == base_cost forever because the state's level cap was
        # 0, which ate the whole civilian queue and left rubber at 0 -> 0 aircraft.
        if want_synth and self._can_build_synth_refinery(tag):
            return "synthetic_refinery"
        return "arms_factory"

    def _can_build_synth_refinery(self, tag=None):
        """True only when the tech is in and some state still has a free slot."""
        try:
            res = {t.lower() for t in (self.researched(tag) or [])}
        except Exception:
            res = set()
        if not any("synth_oil" in t for t in res):
            return False
        try:
            for s in self.states(tag):
                d = self.state_detail(s["id"], tag)
                b = d.get("buildings") or {}
                if int(b.get("synthetic_refinery") or 0) < 2:
                    return True
        except Exception:
            return False
        return False

    def _state_priority(self, tag=None):
        """Own states, highest infrastructure first (generic; no hard-coded GER ids)."""
        try:
            key = (self.p.pid, self._c(tag).tag)
        except Exception:
            key = None
        if key is not None and getattr(self, "_hk_state_key", None) == key:
            return list(self._hk_state_rank)
        rank = []
        for s in self.states(tag):
            try:
                d = self.state_detail(s["id"], tag)
                infra = int((d.get("buildings") or {}).get("infrastructure") or 0)
            except Exception:
                infra = 0
            rank.append((-infra, s["id"]))
        rank.sort()
        out = [sid for _, sid in rank]
        self._hk_state_key = key
        self._hk_state_rank = out
        return out

    def housekeep_research(self, tag=None, techs=None):
        return self.fill_all_research_slots(techs or [], tag)

    def housekeep_construction(self, tag=None, target=None):
        target = int(target or self._construction_target(tag))
        before = len(self.construction_queue(tag))
        building = self._pick_building()
        added, errors = [], []
        for _pass in range(3):
            if len(self.construction_queue(tag)) >= target:
                break
            for sid in self._state_priority(tag):
                if len(self.construction_queue(tag)) >= target:
                    break
                try:
                    res = self.add_construction(sid, building)
                except Exception as e:
                    errors.append({"state": sid, "error": str(e)})
                    continue
                if res.get("ok"):
                    added.append({"state": sid, "building": building})
                elif res.get("error"):
                    errors.append({"state": sid, "error": res["error"]})
        after = len(self.construction_queue(tag))
        return {"ok": after >= target, "before": before, "after": after,
                "target": target, "building": building, "added": added,
                "errors": errors[:12]}

    # CA standard factory split when nothing is critically short.
    # 2026-11-04: air was 0.20 and the allocator could never reach it (see the
    # deficit allocator below); the historical German build wants ~25% fighters+CAS.
    PROD_RATIO = {"inf": 0.34, "art": 0.21, "armor": 0.20, "air": 0.25}

    @staticmethod
    def _prod_group(equipment):
        eq = (equipment or "").lower()
        if "infantry_equipment" in eq:
            return "inf"
        if "artillery" in eq or "support_equipment" in eq:
            return "art"
        if any(k in eq for k in ("tank", "motorized", "mechanized", "armored_car")):
            return "armor"
        if any(k in eq for k in ("fighter", "cas", "bomber", "naval_bomber")):
            return "air"
        return "other"

    def _eq_family(self, name):
        """'infantry_equipment_2' -> ('infantry_equipment_', 2). ('x', -1) if no tier."""
        import re
        m = re.match(r"^(.*_)(\d+)$", str(name or ""))
        if m:
            return m.group(1), int(m.group(2))
        return str(name or ""), -1

    def _upgrade_production_lines(self, tag=None):
        """Swap obsolete production lines to the newest unlocked variant.

        Four years of the 1936 campaign kept building infantry_equipment_1 and
        light_tank_1 because `housekeep_production` only ever moved factory COUNT;
        nothing ever called set_production_line_equipment(). The newest unlocked
        variant in each family is now picked from the country's own active-variant
        list and swapped in (the same command as clicking it in the production tab).
        """
        out = {"upgraded": [], "errors": []}
        try:
            stock = self.equipment_stockpile(tag)
            avail = [v["equipment"] for v in (stock.get("available_variants") or [])
                     if v.get("equipment")]
            lines = self.production_lines(tag)
        except Exception as e:
            out["errors"].append(str(e))
            return out
        best = {}
        for eq in avail:
            fam, tier = self._eq_family(eq)
            if tier >= 0 and (fam not in best or tier > best[fam][0]):
                best[fam] = (tier, eq)
        medium = None
        for fam, (tier, eq) in best.items():
            if "medium_tank" in fam and (medium is None or tier > medium[0]):
                medium = (tier, eq)
        for l in lines:
            if l.get("class") != "CMilitaryProductionLine":
                continue
            cur = l.get("equipment") or ""
            fam, tier = self._eq_family(cur)
            target = None
            if tier >= 0 and fam in best and best[fam][0] > tier:
                target = best[fam][1]
            elif "light_tank" in cur and medium:
                target = medium[1]
            if target and target != cur:
                try:
                    r = self.set_production_line_equipment(l["index"], target, tag=tag)
                except Exception as e:
                    out["errors"].append({"line": l["index"], "error": str(e)})
                    continue
                rec = {"line": l["index"], "from": cur, "to": target,
                       "ok": bool(r.get("ok"))}
                if not rec["ok"]:
                    rec["error"] = r.get("error")
                out["upgraded"].append(rec)
        return out

    def _stockpile_counts(self, tag=None):
        out = {}
        try:
            for row in self.equipment_stockpile(tag).get("stockpile") or []:
                out[row["equipment"]] = float(row.get("count") or 0)
        except Exception:
            pass
        return out

    def _cap_deprioritized_lines(self, tag, rules):
        """Veteran rule: tactical/strategic bombers are a trap.

        Keep at most ONE factory on them; the freed factories go back to the idle
        pool and are handed to fighters/CAS on the next allocation pass.
        """
        want = tuple((rules or {}).get("deprioritize_equipment")
                     or ("tac_bomber", "strategic_bomber", "heavy_fighter"))
        out = []
        try:
            for l in self.production_lines(tag):
                if l.get("class") != "CMilitaryProductionLine":
                    continue
                eq = str(l.get("equipment") or "").lower()
                if not any(w in eq for w in want):
                    continue
                before = int(l.get("factories") or 0)
                if before <= 1:
                    continue
                res = self.set_production_amount(l["index"], 1, tag)
                out.append({"line": l["index"], "equipment": l.get("equipment"),
                            "before": before, "after": res.get("after"),
                            "ok": bool(res.get("ok"))})
        except Exception as e:
            out.append({"error": str(e)})
        return out

    def housekeep_production(self, tag=None):
        """Put every idle military factory to work.

        Priority order (work sheet task 2):
          1. a negative / critically low stockpile of rifles, support or artillery
             gets the next factory (the biggest shortfall first);
          2. otherwise allocate toward the CA ratio 30% infantry / 25% artillery+
             support / 25% tanks+motorised / 20% air.
        """
        upgraded = self._upgrade_production_lines(tag)
        try:
            import playbooks as _pbk0
            _rules0 = (_pbk0.get_playbook(self._c(tag).tag) or {}).get("production_rules") or {}
        except Exception:
            _rules0 = {}
        # Runs even when there is no idle factory: the freed factories show up on
        # the NEXT allocation pass, so the cap must not be gated behind `idle`.
        capped_lines = self._cap_deprioritized_lines(tag, _rules0)
        lines = self.production_lines(tag)      # re-read: a swap changes the variant
        summary = self.player().summary() if tag in (None, "", "player") else self.country(tag).summary()
        total_mil = int(summary.get("military_factories") or 0)
        mil = [l for l in lines if l.get("class") == "CMilitaryProductionLine"]
        used = sum(int(l.get("factories") or 0) for l in mil)
        idle = max(0, total_mil - used)

        # No air line at all? Create one before handing out factories, otherwise the
        # 25% air share can never be reached (the 1940 run ended with a single
        # fighter factory for four years).
        created_line = None
        if idle > 0 and not any(self._prod_group(l.get("equipment")) == "air" for l in mil):
            try:
                avail = [v["equipment"] for v in
                         (self.equipment_stockpile(tag).get("available_variants") or [])
                         if v.get("equipment")]
                air = sorted(e for e in avail
                             if "fighter" in e or "cas" in e or "small_plane" in e)
                if air:
                    res = self.create_production_line(air[-1], tag=tag)
                    created_line = {"equipment": air[-1], "ok": bool(res.get("ok")),
                                    "error": res.get("error")}
                    lines = self.production_lines(tag)
                    mil = [l for l in lines if l.get("class") == "CMilitaryProductionLine"]
                    used = sum(int(l.get("factories") or 0) for l in mil)
                    idle = max(0, total_mil - used)
            except Exception as e:
                created_line = {"ok": False, "error": str(e)}

        if idle <= 0:
            return {"ok": True, "idle": 0, "total": total_mil, "used": used,
                    "changes": [], "upgraded": upgraded, "created_line": created_line,
                    "capped": capped_lines,
                    "note": "no idle military factories"}
        if not mil:
            return {"ok": False, "idle": idle, "total": total_mil, "used": used,
                    "changes": [], "error": "no military production line",
                    "capped": capped_lines}

        # One main line per group: the one that already has the most factories.
        groups = {}
        for l in mil:
            g = self._prod_group(l.get("equipment"))
            if g == "other":
                continue
            if g not in groups or int(l.get("factories") or 0) > int(groups[g].get("factories") or 0):
                groups[g] = l
        if not groups:
            groups = {"inf": mil[0]}

        stock = self._stockpile_counts(tag)
        rifles = sum(v for k, v in stock.items()
                     if k and "infantry_equipment" in k.lower())
        try:
            import playbooks as _pbk
            rules = (_pbk.get_playbook(self._c(tag).tag) or {}).get("production_rules") or {}
        except Exception:
            rules = {}
        ratio_map = dict(self.PROD_RATIO)
        rifle_cap = float(rules.get("rifle_stockpile_soft_cap") or 25000)
        if rules and rifles >= rifle_cap:
            # Veteran rule: 25k rifles in the warehouse means the infantry line is
            # done — shift the next factories into fighters/CAS and armour.
            ratio_map = dict(rules.get("ratios_rifle_surplus") or ratio_map)
        lines = self.production_lines(tag)
        mil = [l for l in lines if l.get("class") == "CMilitaryProductionLine"]
        used = sum(int(l.get("factories") or 0) for l in mil)
        idle = max(0, total_mil - used)
        low = {}
        need = (("inf", ("infantry_equipment",), 3000),
                ("art", ("artillery_equipment", "support_equipment"), 1200))
        for g, keys, floor in need:
            if g not in groups:
                continue
            have = sum(stock.get(k, 0) for k in stock if any(x in k.lower() for x in keys))
            if have < floor:
                low[g] = (have, floor)

        total_target = max(total_mil, 1)
        # Deficit allocation over EVERY line, not one line per group.
        # The old loop handed the first factory to rifles (weight .45), set `left`
        # to 0 and broke out, so a finished aircraft factory was never assigned to
        # air: the 1940 save still had 1 fighter line after four years.
        target = {}
        for g, ratio in ratio_map.items():
            glines = [l for l in mil if self._prod_group(l.get("equipment")) == g]
            if not glines:
                continue
            cur = [int(l.get("factories") or 0) for l in glines]
            tot = sum(cur)
            for l, c in zip(glines, cur):
                share = (c / tot) if tot else (1.0 / len(glines))
                target[id(l)] = total_target * ratio * share
        cur_counts = {}
        for l in mil:
            n = int(l.get("factories") or 0)
            cur_counts[id(l)] = n
            target.setdefault(id(l), float(n))

        def shortfall(l):
            base = target[id(l)] - cur_counts[id(l)]
            if low and self._prod_group(l.get("equipment")) in low:
                base += max(1.0, total_target * 0.25)   # immediate shortage first
            return base

        desired = dict(cur_counts)
        left = idle
        while left > 0:
            line = max(mil, key=shortfall)
            if shortfall(line) <= -0.5:
                break
            desired[id(line)] = desired[id(line)] + 1
            cur_counts[id(line)] = cur_counts[id(line)] + 1
            left -= 1

        changes = []
        for l in sorted(mil, key=lambda x: desired[id(x)] - int(x.get("factories") or 0),
                        reverse=True):
            before = int(l.get("factories") or 0)
            want = desired[id(l)]
            if want <= before:
                continue
            res = self.set_production_amount(l["index"], want, tag)
            after = int(res.get("after", before))
            changes.append({"line": l["index"], "group": self._prod_group(l.get("equipment")),
                            "equipment": l.get("equipment"),
                            "before": before, "after": after, "requested": want,
                            "granted": after - before, "ok": bool(res.get("ok"))})
        used_after = sum(int(l.get("factories") or 0) for l in self.production_lines(tag)
                         if l.get("class") == "CMilitaryProductionLine")
        return {"ok": used_after >= total_mil or used_after > used,
                "idle": idle, "total": total_mil, "used": used,
                "used_after": used_after,
                "low_stock": sorted(low),
                "ratio": dict(ratio_map), "capped": capped_lines,
                "upgraded": upgraded, "created_line": created_line,
                "changes": changes}

    # Each chain is ordered best -> worst; the first one that CanExecute accepts wins.
    LAW_CHAINS = (
        ("war_economy", "partial_economic_mobilisation", "low_economic_mobilisation"),
        ("extensive_conscription", "limited_conscription", "volunteer_only"),
        ("export_focus", "limited_exports", "free_trade"),
    )
    SAFE_DECISIONS = (
        # promises_of_peace removed: it is a -10% war-support decision and the
        # economic-law gates (partial needs 25%) are the thing that keeps a
        # country out of total mobilisation.
        "improved_worker_conditions", "war_bonds",
        "infrastructure_building_slot", "GER_mefo_bills_extension",
        "anti_fascist_raids", "anti_communist_raids", "anti_democratic_raids",
    )
    _DANGER = ("civil_war", "surrender", "abdicate", "concede", "abandon", "oppose",
               "capitulate", "join_war")

    # Industrial concerns, weapon/aircraft designers and theorists. They are ordinary
    # CIdea objects in PL_ALL_IDEAS, so the already-verified AddIdea/ReplaceIdea path
    # applies — 150 PP each. This is how a player spends PP after the laws are maxed.
    PREF_CONCERNS = ("ig_farben", "krupp", "siemens", "rheinmetall", "mauser", "opel",
                     "henschel", "messerschmitt", "junkers", "porsche", "man",
                     "bofors", "skoda", "porsche")

    def _law_keys(self):
        keys = set()
        for chain in self.LAW_CHAINS:
            keys.update(chain)
        keys.update({"undisturbed_isolation", "isolation", "civilian_economy",
                     "tot_economic_mobilisation", "disarmed_nation", "volunteer_only",
                     "service_by_requirement", "all_adults_serve", "scraping_the_barrel",
                     "closed_economy", "free_trade", "export_focus", "limited_exports"})
        return keys

    def _fill_idea_slots(self, tag=None, max_buys=8):
        """Buy industrial concerns / designers / advisors in empty idea categories.

        Only categories that are currently EMPTY are touched: replacing a minister
        the country already has would need a political-power comparison the tool
        does not have, and replacing a law here would fight LAW_CHAINS.
        """
        p = self.p
        c = self._c(tag)
        active = self._idea_entries(tag, "active")
        active_keys = {a["key"] for a in active}
        active_cats = {a["category"] for a in active if a.get("category")}
        laws = self._law_keys()
        all_ideas = self._idea_entries(tag, "all")

        def rank(e):
            k = (e.get("key") or "").lower()
            for i, pref in enumerate(self.PREF_CONCERNS):
                if k == pref:
                    return (0, i, k)
            for i, pref in enumerate(self.PREF_CONCERNS):
                if k.startswith(pref) or pref in k:
                    return (1, i, k)
            return (2, 0, k)

        bought = []
        for e in sorted(all_ideas, key=rank):
            if len(bought) >= max_buys:
                break
            if float(c.political_power) < 150:
                break
            k, cat = e.get("key"), e.get("category")
            if not k or not cat or cat in active_cats or k in active_keys or k in laws:
                continue
            try:
                res = self.add_idea(k, tag=tag)
            except Exception as exc:
                bought.append({"idea": k, "ok": False, "error": str(exc)})
                continue
            if res.get("ok"):
                bought.append({"idea": k, "ok": True,
                               "pp_before": res.get("pp_before"),
                               "pp_after": res.get("pp_after")})
                active_cats.add(cat)
                active_keys.add(k)
        return bought

    def housekeep_politics(self, tag=None):
        """Spend political power: law upgrades (with fallback), then decisions.

        The 1936 run ended with 2000 PP glued to the cap. Two reasons:
          * `war_economy` is refused while at peace and the old loop then gave up on
            the whole category instead of trying partial_economic_mobilisation;
          * only two decisions were ever attempted.
        """
        pol = self.politics(tag)
        pp = float(pol["political_power"])
        active = set(pol.get("active_ideas") or [])
        actions = []

        for chain in self.LAW_CHAINS:
            if pp < 150:
                break
            cur = next((x for x in chain if x in active), None)
            if cur == chain[0]:
                continue
            for want in chain:
                if want == cur:
                    break
                if want in active:
                    continue
                try:
                    res = self.add_idea(want, tag=tag)
                except Exception as e:
                    res = {"ok": False, "error": str(e)}
                actions.append({"law": want, "from": cur, "ok": bool(res.get("ok")),
                                "error": res.get("error") or res.get("last_error")})
                if res.get("ok"):
                    pp = float(self._c(tag).political_power)
                    active.add(want)
                    break

        if pp >= 150:
            try:
                vis = [d["key"] for d in self.decisions(tag, "visible")]
            except Exception:
                vis = []
            cands = [d for d in self.SAFE_DECISIONS if d in vis]
            if pp >= 400:
                for d in vis:
                    if d not in cands and not any(b in d.lower() for b in self._DANGER):
                        cands.append(d)
            for dec in cands[:4]:
                if pp < 150:
                    break
                try:
                    res = self.select_decision(dec, tag=tag)
                except Exception as e:
                    res = {"ok": False, "error": str(e)}
                actions.append({"decision": dec, "ok": bool(res.get("ok")),
                                "error": res.get("error")})
                if res.get("ok"):
                    pp = float(self._c(tag).political_power)

        # Buy the non-law idea slots last: they are a pure PP sink and must not
        # compete with a law upgrade for the same 150 PP.
        if pp >= 150:
            try:
                ideas = self._fill_idea_slots(tag=tag)
            except Exception as e:
                ideas = [{"ok": False, "error": str(e)}]
            for row in ideas:
                actions.append({"idea_slot": row.get("idea"), "ok": bool(row.get("ok")),
                                "error": row.get("error")})
            if ideas:
                pp = float(self._c(tag).political_power)

        # Last resort when every idea slot is full: a new corps commander costs PP
        # and is always available. Without this the 1939.12+ game sat on the 2000 PP
        # cap for eight months (measured 2026-11-05).
        if pp >= 600 and not _LEADER_FAULTED[0]:
            for _ in range(2):
                if pp < 600:
                    break
                try:
                    res = self.recruit_unit_leader(0, tag=tag)
                except Exception as e:
                    res = {"ok": False, "error": str(e)}
                if isinstance(res, dict) and "sig=11" in str(res.get("error") or ""):
                    # 2026-11-06: this injected call faults inside the game every
                    # time; one fault per 8-minute housekeep is a crash waiting to
                    # happen, so stop trying for the rest of the process.
                    _LEADER_FAULTED[0] = True
                actions.append({"recruit_leader": "general", "ok": bool(res.get("ok")
                                if isinstance(res, dict) else False),
                                "error": (res or {}).get("error") if isinstance(res, dict) else None})
                pp = float(self._c(tag).political_power)
                if not isinstance(res, dict) or not res.get("ok"):
                    break

        # Run 8 note: an automatic replace_idea() sweep here was tried and removed.
        # It re-proposed laws already handled by LAW_CHAINS (free_trade vs
        # limited_exports) and every replace_idea is an injected CanExecute, i.e.
        # exactly the call shape that has been faulting with sig=11. The existing
        # law/decision/idea-slot/leader sinks already spend the PP (measured:
        # 999 -> 882 in one call), so nothing is added here.

        return {"ok": True, "pp_before": pol["political_power"], "pp": pp,
                "actions": actions,
                "note": "" if any(a.get("ok") for a in actions) else "no applicable law or decision"}

    def _conveyor_progress(self, conv):
        """(finished_lines, max_progress) of a conveyor's training lines.

        CMilitaryDeploymentConveyor+0x70 is CPdxArray<CMilitaryDeploymentLine*>;
        each line's CMilitaryDeployment* is at +0x28 and its completion fraction is
        +0x60 (fixed 1e5). A dead conveyor sits at 1.0 forever: the division is
        trained but there is no deployment location, so it never lands.
        """
        p = self.p
        from game import _arr
        # `conveyors()` returns dicts; older call sites passed the raw pointer.
        ptr = int(conv["ptr"], 16) if isinstance(conv, dict) else int(conv)
        done, best = 0, 0.0
        for ln in _arr(p, ptr, 0x70):
            md = p.u64(ln + 0x28)
            if not md:
                continue
            prog = p.i64(md + 0x60) / 1e5
            best = max(best, prog)
            if prog >= 0.999:
                done += 1
        return done, round(best, 3)

    def _repair_conveyors(self, tag=None):
        """Point every existing conveyor at the capital province; delete the dead ones.

        `SetConveyorLocation` takes a PROVINCE id and the engine only accepts it on
        a conveyor that has not started training. A conveyor from an older save has
        lines already, so the call is refused and it stays at "No location set"
        forever — its finished divisions can never deploy (verified: 26./33./30.
        Infanterie-Division stuck at 1/inf for a year).

        Such a conveyor is removed (CRemoveConveyorCommand) and recreated with a
        valid location. The removal is deliberately conservative: it needs a line
        that is 100 % trained in TWO consecutive housekeep calls and no growth in
        the division count, so a healthy conveyor that happens to be at 1.0 for one
        tick (a division about to deploy) is never touched.
        """
        c = self._c(tag)
        from commands import COMMANDS as _CMDS
        want_state = self._homeland_state_id(c.tag)
        want_prov = self.deployment_province(want_state, tag=c.tag)
        hist = getattr(self, "_stuck_hist", None)
        if hist is None:
            hist = self._stuck_hist = {}
        divs = int(self.divisions(c.tag).get("count") or 0)
        out = []
        for conv in self.conveyors(c.tag):
            key = (c.tag, conv["packed_handle"])
            row = {"conveyor": conv["ptr"], "state_id": want_state,
                   "deploy_province": want_prov}
            try:
                with self:
                    rv = self._run_cmd(self.cmd.make("SetConveyorLocation",
                                                     conveyor_handle=conv["packed_handle"],
                                                     province_id=int(want_prov)))
                    row["location_ok"] = bool(rv & 0xFF)
                    row["location_error"] = getattr(self, "_last_cmd_error", "")
                    if "SetConveyorPriority" in _CMDS:
                        rv2 = self._run_cmd(self.cmd.make("SetConveyorPriority",
                                                          conveyor_handle=conv["packed_handle"],
                                                          priority=2))
                        row["priority_ok"] = bool(rv2 & 0xFF)
                done, best = self._conveyor_progress(conv)
                row["finished_lines"] = done
                row["max_progress"] = best
                if not row.get("location_ok") and done:
                    prev = hist.get(key)
                    if prev and prev[0] == divs:
                        with self:
                            rv3 = self._run_cmd(self.cmd.make(
                                "RemoveConveyor", conveyor_handle=conv["packed_handle"]))
                        row["removed"] = bool(rv3 & 0xFF)
                        row["remove_error"] = getattr(self, "_last_cmd_error", "")
                        hist.pop(key, None)
                    else:
                        hist[key] = (divs, done)
                        row["stuck_strike"] = 1
            except Exception as e:
                row["error"] = str(e)
            out.append(row)
        return out

    def housekeep_recruitment(self, tag=None):
        """Keep ONE infantry conveyor at 2-3 lines and push trained divisions to the map.

        Germany's historical focuses gate on army size (Anschluss ~500k men under
        arms, Sudetenland ~725k). The previous run opened 2 conveyors x 5 lines =
        10 divisions training at once against a rifle output of ~26/day: every one
        of them sat at ~10 % equipment for 17 months and not a single division
        finished. One conveyor, 2-3 lines, highest equipment priority and a
        capital-connected deployment state is what actually produces divisions.
        """
        c = self._c(tag)
        out = {"ok": True, "opened": None, "deploy": None}
        try:
            count = int(self.divisions(c.tag).get("count") or 0)
            manpower = float(c.manpower() or 0)
            stock = self._stockpile_counts(tag)
            rifles = sum(v for k, v in stock.items()
                         if k and "infantry_equipment" in k.lower())
            conv = self.conveyors(c.tag)
            out.update(divisions=count, manpower=manpower, rifles=rifles,
                       conveyors=len(conv),
                       homeland_state=self._homeland_state_id(c.tag))
            if conv:
                out["repaired"] = self._repair_conveyors(tag)
                # a dead conveyor is removed by _repair_conveyors; rebuild it now
                conv = self.conveyors(c.tag)
                out["conveyors_after_repair"] = len(conv)
                out["removed"] = [r for r in out["repaired"] if r.get("removed")]
            # lines: 2 while rifles are scarce, 3 once the stockpile can feed them.
            lines = 2 if rifles < 4000 else 3
            # How many parallel training lines the rifle stockpile can actually feed.
            # The old "only after 45 divisions" condition kept ONE 2-line conveyor for
            # 2.5 years while 20k rifles sat in the warehouse: 52 divisions on the eve
            # of the Anschluss manpower gate (54). Scale with the stockpile instead.
            if count >= 95:
                max_convs, lines = 1, 2
            elif rifles >= 12000:
                max_convs, lines = 5, 3
            elif rifles >= 4000:
                max_convs, lines = 4, 3
            elif rifles >= 1200:
                max_convs, lines = 3, 2
            else:
                max_convs, lines = 2, 2
            # Veteran ceiling: 90-100 fully equipped divisions beat 174 shells at
            # 40 % equipment. Stop opening lines at the cap and trim the surplus.
            try:
                import playbooks as _pbk
                _pb = _pbk.get_playbook(c.tag)
            except Exception:
                _pb = {}
            army_cap = int(_pb.get("army_cap") or 0)
            # B8 (review 2026-11-07): `army_cap` is a flat 110, so recruitment
            # stopped dead in 1939 while the playbook's own year curve asks for
            # 135. Use the curve, never below the flat value.
            try:
                _yr = int(self.date().get("year") or 0)   # WO-009: was g.date() -> NameError swallowed, cap stuck at 110
                _td = int(_pbk.get_target_divisions(c.tag, _yr) or 0)
                if _td > army_cap:
                    army_cap = _td
                    out["army_cap_target"] = _td
            except Exception:
                pass
            per_div = float(_pb.get("rifle_per_division") or 500)
            if army_cap and count >= army_cap:
                max_convs, lines = 0, 2
                out["army_cap"] = count
            # Equipment guard: a division needs ~1,000 rifles. Once the army is
            # already large, more training lines only dilute the stockpile further
            # (174 divisions on 76k rifles = ~40 % equipment could not attack).
            # Early game (fewer than 60 divisions) the guard stays out of the way —
            # the opening army is SUPPOSED to be under-equipped.
            if count >= 60 and rifles < count * per_div:
                max_convs = min(max_convs, max(1, len(conv)))
                out["equipment_guard"] = ("rifles %.0f < divs %d x %.0f -> no new conveyor"
                                          % (rifles, count, per_div))
            if max_convs == 0 and conv:
                removed = []
                for extra in conv:
                    try:
                        with self:
                            rv = self._run_cmd(self.cmd.make(
                                "RemoveConveyor", conveyor_handle=extra["packed_handle"]))
                        removed.append({"conveyor": extra.get("ptr"),
                                        "ok": bool(rv & 0xFF),
                                        "error": getattr(self, "_last_cmd_error", "")})
                    except Exception as e:
                        removed.append({"conveyor": extra.get("ptr"), "ok": False,
                                        "error": str(e)})
                out["trimmed"] = removed
                conv = self.conveyors(c.tag)
            # 2026-11-06: `count < 95` was a leftover from the pre-army_cap era and
            # meant that once the army passed 95 divisions a trimmed/battle-worn
            # army could NEVER open a conveyor again (end of run 4: 102 divisions,
            # 0 conveyors, no replacement stream).
            under_cap = (count < army_cap) if army_cap else (count < 95)
            if under_cap and manpower > 100000 and len(conv) < max_convs:
                out["opened"] = self.train_divisions(lines=lines, tag=tag)
            else:
                out["skipped"] = ("count=%d manpower=%.0f rifles=%.0f conveyors=%d/%d lines=%d"
                                  % (count, manpower, rifles, len(conv), max_convs, lines))
        except Exception as e:
            out.update(ok=False, error=str(e))
        try:
            out["deploy"] = self.deploy_ready_conveyors(tag, min_progress=0.995)
        except Exception as e:
            out["deploy"] = {"error": str(e)}
        return out

    def housekeep_divisions(self, tag=None):
        # Re-enabled 2026-11-04: the heap corruption was caused by CPdxArray's
        # allocator field pointing at a VTABLE (0x43591B8) instead of the static
        # CPdxNewDeleteAllocator object (0x4505128) in armyops.ALLOC.
        created = []
        # 2026-11-10: a paratrooper is idle by design. Never collect it into an
        # automatic army -- the paradrop order owns it.
        import divreserve as _dr
        _para = [d for d in self.unassigned_divisions(tag)
                 if _dr.is_paradrop_division(self, d)[0]]
        if _para:
            created.append({"ok": True, "skipped_paratroopers": len(_para)})
        for _round in range(3):
            unassigned = [d for d in self.unassigned_divisions(tag)
                          if not _dr.is_paradrop_division(self, d)[0]]
            if not unassigned:
                break
            before = {g["ptr"] for g in self.order_groups(tag)["groups"]}
            res = self.create_army(unassigned[:24], tag=tag)
            created.append(res)
            for _ in range(12):
                if [g for g in self.order_groups(tag)["groups"] if g["ptr"] not in before]:
                    break
                self._settle(0.5)
        groups = self.order_groups(tag)["groups"]
        gens = [g for g in self.available_generals(tag).get("generals", [])
                if g.get("rank") == "corps_commander"]
        used = {g.get("leader_ptr") for g in groups if g.get("leader_ptr")}
        assigned = []
        for g in groups:
            if g.get("leader_ptr"):
                continue
            cand = next((x for x in gens if x.get("leader_ptr") not in used), None)
            if not cand:
                break
            try:
                cr = self.assign_commander(int(g["ptr"], 16),
                                           leader_ptr=int(cand["leader_ptr"], 16),
                                           character_key=cand["key"])
                ok = bool(cr.get("ok"))
                assigned.append({"army": g["ptr"], "commander": cand["key"],
                                 "ok": ok, "error": cr.get("error")})
                if ok:
                    used.add(cand["leader_ptr"])
            except Exception as e:
                assigned.append({"army": g["ptr"], "commander": cand.get("key"),
                                 "ok": False, "error": str(e)})
        remaining = len([d for d in self.unassigned_divisions(tag)
                         if not _dr.is_paradrop_division(self, d)[0]])
        unled = [g["ptr"] for g in self.order_groups(tag)["groups"] if not g.get("leader_ptr")]
        # The stated goal of this pass is "no unassigned divisions". A new campaign can
        # expose zero parsable corps commanders (CCharacterManager starts empty), so an
        # unled army is a warning, not a failed housekeep; otherwise every call reports
        # false even though all divisions were collected into armies.
        warning = None
        if unled:
            warning = ("%d army(s) have no commander: no available corps commander was "
                       "found (CCharacterManager may still be empty at campaign start)" % len(unled))
        # 2026-11-08 (work order 1108(1) §2.2): a freshly created army has NO front
        # line, so its divisions sit in Berlin until the player opens the army panel.
        # Re-apply the native front line to every army that has none, as soon as
        # there is an enemy to face. `activate_army_battleplans` is idempotent: it
        # only draws on armies whose `orders_of` has no type==2 order.
        battle = None
        try:
            import movemicro
            enemy = None
            for d in (movemicro._load_front_state().get("directives") or {}).values():
                enemy = (d or {}).get("enemy_tag")
                if enemy:
                    break
            if not enemy:
                ws = self.wartime_status()
                enemy = (ws.get("enemies") or [None])[0]
            if enemy:
                battle = self.activate_army_battleplans(enemy, tag)
        except Exception as e:
            battle = {"ok": False, "error": str(e)}
        return {"ok": remaining == 0,
                "unassigned": len(self.unassigned_divisions(tag)) if remaining else 0,
                "remaining": remaining, "created": created,
                "assigned": assigned, "unled": unled, "warning": warning,
                "battleplans": battle,
                "exempt_paratroopers": len(_para)}

    def housekeep_doctrines(self, tag=None):
        """Unlock the grand doctrines the playbook asks for (once per process/tag)."""
        done = getattr(self, "_doctrine_done", None)
        if done is None:
            done = self._doctrine_done = set()
        try:
            import playbooks
            docs = (playbooks.get_playbook(self._c(tag).tag) or {}).get("grand_doctrines") or []
        except Exception:
            docs = []
        out = []
        for d in docs:
            key = (self._c(tag).tag, d)
            if key in done:
                out.append({"doctrine": d, "skipped": "already unlocked"})
                continue
            try:
                r = self.unlock_doctrine(d, tag=tag) or {}
            except Exception as e:
                r = {"ok": False, "error": str(e)}
            out.append({"doctrine": d, "ok": bool(r.get("ok")),
                        "stored": r.get("stored"), "note": r.get("note"),
                        "error": r.get("error")})
            if r.get("ok"):
                done.add(key)
        return {"ok": True, "attempts": out}

    def housekeep(self, tag=None, target_construction=None, techs=None):
        out = {"ok": True}
        for name, fn in (("research", lambda: self.housekeep_research(tag, techs)),
                         ("construction", lambda: self.housekeep_construction(tag, target_construction)),
                         ("production", lambda: self.housekeep_production(tag)),
                         ("doctrines", lambda: self.housekeep_doctrines(tag)),
                         ("politics", lambda: self.housekeep_politics(tag)),
                         ("recruitment", lambda: self.housekeep_recruitment(tag)),
                         ("divisions", lambda: self.housekeep_divisions(tag))):
            try:
                r = fn()
            except Exception as e:
                import traceback
                r = {"ok": False, "error": str(e),
                     "trace": traceback.format_exc()[-1200:]}
            out[name] = r
            out["ok"] = out["ok"] and bool(r.get("ok", False))
        return out

    def candidates(self, tag=None):
        """Structured IDs the model can use directly, one overview call."""
        out = {}
        try:
            sel = self.selectable_focuses(tag)
            out["recommended_focus"] = sel.get("recommended")
            out["selectable_focuses_top5"] = [x["id"] for x in sel.get("selectable", [])[:5]]
        except Exception:
            out["recommended_focus"] = None
            out["selectable_focuses_top5"] = []
        try:
            slots = self.research_slots(tag)
            out["empty_research_slots"] = [s["slot"] for s in slots if s.get("researching") is None]
            out["recommended_techs"] = self.available_research(tag)[:12]
        except Exception:
            out["empty_research_slots"] = []
            out["recommended_techs"] = []
        try:
            out["recommended_build_states"] = self._state_priority(tag)[:4]
        except Exception:
            out["recommended_build_states"] = []
        try:
            out["production_lines"] = [
                {"line_index": l["index"], "equipment": l.get("equipment"),
                 "factories": l.get("factories")}
                for l in self.production_lines(tag)
                if l.get("class") == "CMilitaryProductionLine"][:8]
        except Exception:
            out["production_lines"] = []
        try:
            out["available_decisions"] = [d["key"] for d in self.decisions(tag, "visible")][:12]
        except Exception:
            out["available_decisions"] = []
        try:
            out["recommended_ideas"] = [i["id"] for i in self.recommended_ideas(tag)["ideas"]][:10]
        except Exception:
            out["recommended_ideas"] = []
        # 2026-11-06: expose the unconventional-breakout arsenal so the model knows
        # what exists when a front stalls or a minor nation is being played.
        try:
            import playbooks
            enemies = []
            try:
                import combat
                enemies = list(combat.enemy_tags(self) or [])
            except Exception:
                enemies = []
            out["breakout_tactics_guide"] = playbooks.get_breakout_tactics(
                self._c(tag).tag, year=int(self.date().get("year") or 1936),
                active_enemy_tags=enemies)
        except Exception as e:
            out["breakout_tactics_guide"] = {"error": str(e)}
        return out

    def alerts(self, tag=None):
        """Human-readable red-dot summary for hoi4_overview."""
        out = []
        try:
            if not self.current_focus(tag):
                sel = self.selectable_focuses(tag)
                if sel.get("recommended"):
                    out.append("FOCUS_IDLE: 国策槽空置，推荐 %s (%s)" %
                               (sel["recommended"], sel.get("recommended_reason") or ""))
        except Exception:
            pass
        try:
            slots = self.research_slots(tag)
            empty = [s["slot"] for s in slots if s.get("researching") is None]
            if empty:
                cands = self.available_research(tag)[:3]
                out.append("RESEARCH_IDLE: %d 个科研槽空置 (slot %s)，推荐 %s" %
                           (len(empty), ", ".join(map(str, empty)), ", ".join(cands)))
        except Exception:
            pass
        try:
            pp = self.politics(tag)["political_power"]
            if pp >= 160:
                out.append("PP_HIGH: 政治点数 %d，可切换法案/任命顾问或点决议" % pp)
        except Exception:
            pass
        try:
            q = len(self.construction_queue(tag))
            if q < 6:
                out.append("CONSTRUCTION_LOW: 建造队列仅 %d 项，民工即将闲置" % q)
        except Exception:
            pass
        try:
            n = len(self.unassigned_divisions(tag))
            if n:
                out.append("UNASSIGNED_DIVS: %d 个师未编入集团军" % n)
        except Exception:
            pass
        try:
            lines = self.production_lines(tag)
            total = self.player().summary().get("military_factories") or 0
            used = sum(int(l.get("factories") or 0) for l in lines
                       if l.get("class") == "CMilitaryProductionLine")
            if total - used >= 2:
                out.append("MIL_FACTORY_IDLE: %d 座军工厂闲置未分配" % (total - used))
        except Exception:
            pass
        return out


