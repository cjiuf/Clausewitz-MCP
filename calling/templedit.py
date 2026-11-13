import os
"""Division-template editing through CUpdateDivisionTemplateCommand.

Reverse-engineered 2026-11-09 on 1.19.4.1.99a5:

  CUpdateDivisionTemplateCommand vtable 0x4457E68 (one reference: 0x766D1B, which
  writes `vt+0x10` into [obj] — the command is stored at +0x10 of the object).
  Its parameterless factory sits at 0x766CE0: `new(0x288)`, zeroes +0x08..+0x22,
  stores the vtable, then a global at +0x270 and 0 at +0x278/+0x280.  The embedded
  CDivisionTemplate lives at +0x28 (its own ctor is called at 0x766D29).

  The 648-byte payload therefore is: [vtable + 0x28-byte command header]
  [embedded CDivisionTemplate][tag at +0x270][+0x278 int][+0x280 ptr].

  The template body is filled with the engine's own deep-copy helper 0x761410
  (the same one CCreateDivisionTemplateCommand uses), so we never hand-build a
  CDivisionTemplate.

Editing = copy the live template in, then rewrite the line-battalion array at
body+0x68 (a CPdxArray<CSubUnitDefinition*> {data, cap, size}) so it contains the
requested number of each battalion type.
"""
import struct

FACTORY = 0x766CE0          # parameterless: returns a fresh CUpdateDivisionTemplateCommand*
DEEPCOPY = 0x761410         # (dst CDivisionTemplate*, src CDivisionTemplate*)
RUN_CMD = 0x20AE0A0
CMD_SIZE = 0x288


def _u64(b, o=0):
    return struct.unpack_from("<Q", b, o)[0]


def _read_cstr(p, addr, n=48):
    try:
        return p.read(addr, n).split(b"\0")[0].decode("utf-8", "replace")
    except Exception:
        return ""


def subunit_name(p, entry):
    """CSubUnitDefinition: std::string at +0x10 (_M_p) / +0x18 (_M_string_length).

    2026-11-10 fix (review WO-001): the old code read the heap pointer from
    entry+0x20, which is the SSO buffer for n<=15 but _M_allocated_capacity for
    n>15 — so every subunit whose key is longer than 15 chars ("artillery_brigade",
    "logistics_company", ...) came back as an unreadable 0x11-sized "pointer" and
    silently vanished from every scan. The pool held 78 names, max length 15,
    and 51 of the 100 active definitions were missing.
    """
    try:
        n = _u64(p.read(entry + 0x18, 8), 0)
        if not (0 < n < 40):
            return ""
        if n <= 15:
            s = p.read(entry + 0x20, n)
        else:
            q = _u64(p.read(entry + 0x10, 8), 0)
            if q < 0x10000:
                return ""
            s = p.read(q, n)
        return s.decode("utf-8", "replace")
    except Exception:
        return ""


_LIVE_POOL = {"pid": None, "by_name": {}}


def live_pool(g, max_span=0x10000000):
    """Rebuild {subunit_key: CSubUnitDefinition*} for the live process.

    Anchors on a subunit pointer found in a real division template, reads its
    vtable, then scans only the writable pages within +/-256 MB of that anchor
    for the same vtable (all CSubUnitDefinition instances share it). Cheaper and
    far more reliable than the static subunit_pointers.json, which is bound to
    one game process.
    """
    p = g.p
    cached = _LIVE_POOL["by_name"]
    if _LIVE_POOL["pid"] == p.pid and cached:
        k0 = next(iter(cached))
        if subunit_name(p, cached[k0]) == k0:
            return dict(cached)

    anchor = None
    for t in g.division_templates(None):
        try:
            tp = int(t["ptr"], 16)
        except Exception:
            continue
        for off in (0x68, 0x88):
            try:
                data, cap, size = struct.unpack("<Qii", p.read(tp + off, 16))
                if not (data > 0x10000 and 0 < size < 40):
                    continue
                sp = _u64(p.read(data, 8), 0)
                if sp > 0x10000 and subunit_name(p, sp):
                    anchor = sp
                    break
            except Exception:
                continue
        if anchor:
            break
    if not anchor:
        return dict(cached)

    vt = _u64(p.read(anchor, 8), 0)
    needle = struct.pack("<Q", vt)
    lo = max(0x10000, anchor - max_span)
    hi = anchor + max_span
    found = {}
    for m in p.regions(writable=True):
        a, b = max(lo, m["start"]), min(hi, m["end"])
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
                    cand = cur + i
                    nm = subunit_name(p, cand)
                    if nm and nm not in found:
                        found[nm] = cand
                    i += 8
            cur += n
    if len(found) < len(cached):
        found.update({k: v for k, v in cached.items() if k not in found})
    _LIVE_POOL["pid"] = p.pid
    _LIVE_POOL["by_name"] = dict(found)
    return dict(found)


def template_arrays(g, tmpl_ptr):
    p = g.p
    out = {}
    for label, off in (("line", 0x68), ("support", 0x88)):
        hdr = p.read(tmpl_ptr + off, 16)
        data, cap, size = struct.unpack("<Qii", hdr)
        items = []
        for k in range(min(size, 40)):
            e = _u64(p.read(data + 8 * k, 8), 0)
            items.append({"ptr": e, "name": subunit_name(p, e)})
        out[label] = {"data": data, "cap": cap, "size": size, "items": items}
    return out


def build_array(g, keep, add):
    """Allocate a fresh CPdxArray payload: keep=[ptr...] then add=[(ptr,name)...]."""
    p = g.p
    total = len(keep) + len(add)
    buf = g.malloc(8 * total + 16)
    for i, ptr in enumerate(keep):
        p.write(buf + 8 * i, struct.pack("<Q", ptr))
    for j, (ptr, _n) in enumerate(add):
        p.write(buf + 8 * (len(keep) + j), struct.pack("<Q", ptr))
    return buf, total


def _looks_like_template(p, addr):
    """Guard: CDivisionTemplate starts with a vtable then a std::string name at +0x08."""
    try:
        vt = _u64(p.read(addr, 8), 0)
        rel = vt - p.base if vt > p.base else vt
        if not (0x4000000 < rel < 0x6000000):
            return False, "vtable %#x out of range" % vt
        n = _u64(p.read(addr + 0x10, 8), 0)
        if not (0 < n <= 64):
            return False, "name length %d" % n
        q = _u64(p.read(addr + 0x08, 8), 0)
        txt = p.read(q, n) if q else b""
        if not txt or not all(32 <= c < 127 for c in txt):
            return False, "name bytes unreadable"
        return True, txt.decode()
    except Exception as e:
        return False, str(e)




def mem_edit(g, template_ptr, line_counts, support_counts=None, pool_path=None):
    """ROUTE A: rewrite the live CDivisionTemplate arrays in place.

    NOTE (2026-11-09): bypasses the 50-army-experience cost the editor charges;
    the run that uses it records the cost as spent.

    line_counts / support_counts: {"infantry": 9, "artillery": 3, ...}
    Subunits already in this template are reused; anything else comes from the
    global pointer pool (subunit_pointers.json, harvested from every country).
    """
    import json as _json
    p = g.p
    try:
        _d, _c, _s = struct.unpack("<Qii", p.read(template_ptr + 0x68, 16))
        if not (_d and 1 <= _c <= 60 and 1 <= _s <= 40):
            return {"ok": False, "error": "line array header looks wrong: data=%#x cap=%d size=%d"
                    % (_d, _c, _s)}
    except Exception as e:
        return {"ok": False, "error": "cannot read template header: %s" % e}

    before = template_arrays(g, template_ptr)
    by_name = {}
    for it in before["line"]["items"] + before["support"]["items"]:
        by_name.setdefault(it["name"], []).append(it["ptr"])

    pool = {}
    _here = os.path.dirname(os.path.abspath(__file__))
    _root = os.path.dirname(_here)
    for cand in (pool_path, os.path.join(_root, "subunit_pointers.json")):
        if cand and os.path.exists(cand):
            try:
                pool = {k: int(v, 16) for k, v in _json.load(open(cand)).items()}
                break
            except Exception:
                pool = {}

    meta = {}
    for cand in (os.path.join(_root, "subunit_catalog.json"),):
        if os.path.exists(cand):
            try:
                meta = {r["key"]: r for r in _json.load(open(cand, encoding="utf-8"))}
                break
            except Exception:
                meta = {}

    live = {}
    try:
        live = live_pool(g)
        res_pool_src = "live"
    except Exception:
        res_pool_src = "json"
    if not live:
        res_pool_src = "json"

    def ptr_for(nm):
        if by_name.get(nm):
            return by_name[nm][0]
        if live.get(nm):
            return live[nm]
        return pool.get(nm)

    def write_arr(off, counts, want_support):
        want = []
        for nm, cnt in counts.items():
            # "artillery" is the SUPPORT company and "artillery_brigade" the line
            # battalion. Callers (and LLMs) mix them up constantly; map the obvious
            # one instead of failing with "not a line subunit".
            if not want_support and nm == "artillery":
                nm = "artillery_brigade"
            elif want_support and nm == "artillery_brigade":
                nm = "artillery"
            if meta and nm in meta and bool(meta[nm].get("support")) != want_support:
                kind = "support" if want_support else "line"
                return None, ("%r is not a %s subunit (catalog says support=%s)"
                              % (nm, kind, meta[nm].get("support")))
            ptr = ptr_for(nm)
            if not ptr:
                return None, "no subunit named %r (not in template, not in pool)" % nm
            for _ in range(int(cnt)):
                want.append(ptr)
        if not want:
            return None, "empty array"
        buf = g.malloc(8 * len(want) + 16)
        for i2, ptr in enumerate(want):
            p.write(buf + 8 * i2, struct.pack("<Q", ptr))
        p.write(template_ptr + off, struct.pack("<Qii", buf, len(want), len(want)))
        return len(want), None

    res = {"ok": True, "mode": "memory-write", "template": hex(template_ptr),
           "before": [i3["name"] for i3 in before["line"]["items"]],
           "before_support": [i3["name"] for i3 in before["support"]["items"]],
           "xp_note": "50 army XP treated as spent (not deducted by the engine)"}

    if line_counts:
        n, err = write_arr(0x68, line_counts, False)
        if err:
            return {"ok": False, "error": err, "stage": "line"}
        res["line_size"] = n

    if support_counts:
        n, err = write_arr(0x88, support_counts, True)
        if err:
            return {"ok": False, "error": err, "stage": "support"}
        res["support_size"] = n

    after = template_arrays(g, template_ptr)
    res["after"] = [i3["name"] for i3 in after["line"]["items"]]
    res["support"] = [i3["name"] for i3 in after["support"]["items"]]
    return res


def edit_template(g, template_ptr, line_counts=None, dry_run=True, support_counts=None):
    """line_counts: {"infantry": 9, "artillery": 3, ...} -> new line array."""
    import api
    p = g.p
    if (line_counts or support_counts) and not dry_run:
        return mem_edit(g, template_ptr, line_counts, support_counts)
    api.allow_call(FACTORY)
    api.allow_call(DEEPCOPY)
    api.allow_call(RUN_CMD)

    src_ptr = template_ptr
    okp, why = _looks_like_template(p, src_ptr)
    if not okp:
        # CReferencedDivisionTemplate: the real CDivisionTemplate is embedded at +0x18
        alt = template_ptr + 0x18
        ok2, why2 = _looks_like_template(p, alt)
        if ok2:
            src_ptr, okp, why = alt, True, why2
        else:
            return {"ok": False, "error": "template pointer %#x rejected: %s / +0x18: %s"
                    % (template_ptr, why, why2)}
    cmd = g.call(FACTORY, [])
    if not cmd:
        return {"ok": False, "error": "factory returned NULL"}

    # deep-copy the live template into the command body
    g.call(DEEPCOPY, [cmd + 0x28, src_ptr])
    # The command object's `this` is alloc+0x10 (the factory stores vt+0x10), so the
    # embedded CDivisionTemplate sits at this+0x18.  Probe a few offsets and keep the
    # first one whose line array header looks sane.
    body = None
    # V2-01 (reviewed 2026-11-10): DEEPCOPY puts the CDivisionTemplate at cmd+0x28;
    # its line array sits at +0x50, i.e. cmd+0x78 == (cmd+0x10)+0x68. The old list
    # probed 0x80/0x90/0xA0/0x70 and never the right one -> dry_run always failed.
    for _off in (0x10, 0x18, 0x28, 0x38, 0x08):
        _cand = cmd + _off
        try:
            _d, _c, _s = struct.unpack("<Qii", p.read(_cand + 0x68, 16))
            if 0 < _s <= 40 and 1 <= _c <= 60 and _d:
                body = _cand
                break
        except Exception:
            pass
    if body is None:
        blk = b""
        try:
            blk = p.read(cmd, 0x288)
        except Exception:
            pass
        hits = []
        for needle in (b"Infanterie", b"Panzer", b"Kavallerie"):
            i = blk.find(needle)
            if i >= 0:
                hits.append({"needle": needle.decode(), "off": hex(i)})
        return {"ok": False, "error": "no valid embedded template header found",
                "name_scan": hits, "cmd": hex(cmd),
                "head": blk[:64].hex()}
    # V2-01: body is probed as a CReferencedDivisionTemplate-shaped base, so the
    # template name std::string sits at +0x20 (== inner+0x08), not +0x08.
    try:
        name = p.stdstring(body + 0x20) or "(copied)"
    except Exception:
        name = "(copied)"
    arr = template_arrays(g, body)
    if arr["line"]["size"] <= 0:
        return {"ok": False, "error": "copy produced an empty line array", "cmd": hex(cmd)}

    result = {
        "ok": True, "cmd": hex(cmd), "template_copy": hex(body), "src": hex(src_ptr),
        "name_probe": name,
        "before": {k: [i["name"] for i in v["items"]] for k, v in arr.items()},
        "dry_run": dry_run,
    }
    if dry_run:
        result["note"] = "stopped before patching / Run"
        return result

    if line_counts:
        by_name = {}
        for it in arr["line"]["items"]:
            by_name.setdefault(it["name"], []).append(it["ptr"])
        want = []
        for nm, cnt in line_counts.items():
            pool = by_name.get(nm, [])
            if not pool:
                return {"ok": False, "error": "no subunit named %r in the template" % nm,
                        "have": sorted(by_name)}
            for i in range(int(cnt)):
                want.append(pool[i % len(pool)])
        buf, total = build_array(g, want, [])
        p.write(body + 0x68, struct.pack("<Qii", buf, total, total))

    p.write(cmd + 0x270, struct.pack("<i", 1))   # CCountryTag id for GER
    p.write(cmd + 0x278, struct.pack("<i", 0))
    p.write(cmd + 0x280, struct.pack("<Q", 0))

    s = g.malloc(0x28)
    p.write(s, struct.pack("<QQB", s + 0x10, 0, 0))
    rv = g.call(RUN_CMD, [cmd, s])
    err = ""
    try:
        q, n = struct.unpack("<QQ", p.read(s, 16))
        err = p.read(q, min(n, 200)).decode("utf-8", "replace") if q else ""
    except Exception:
        pass
    result["run"] = bool(rv & 0xFF)
    result["error"] = err
    return result
