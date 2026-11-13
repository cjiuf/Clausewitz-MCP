"""
Scans live memory for RTTI vtable pointers and builds a class -> objects index.

Every game object (CNationalFocusDatabase, CTechnologyDatabase, ...) starts with
a vtable pointer. RTTI gives us the vtable address of all 18,882 classes in the
binary, so scanning memory for those values tells us where each object lives.

A single pass scans ~3.8 GB, which takes a few seconds with numpy.

MEMORY (this file used to OOM-kill the machine):
  * The addresses are stored RELATIVE to the image base, so the cache survives
    a game restart (ASLR moves the base every launch). Keying the cache on the
    pid or on the absolute base threw it away on every restart and forced a
    live rescan.
  * The per-class address lists are numpy/array('q') arrays, not Python lists.
    5.7 M addresses in Python lists cost ~2.6 GB of RSS; in int64 arrays they
    cost ~46 MB. The live scan therefore peaks at a few hundred MB instead of
    several GB, which is what used to OOM-kill the MCP child (10.2 GB) and,
    with it, the ptrace-attached game.
"""
from __future__ import annotations
import os, pickle, re, struct
from array import array
import numpy as np


class ObjectIndex:
    def __init__(self, proc, rtti_map, cache_path=None):
        self.p = proc
        self.rtti = rtti_map                 # {class_name: [vaddr,...]}
        self.cache_path = cache_path
        self._byvptr = None                  # {vptr_runtime: array('q') of addr_runtime}
        self._vptr_class = {}                # {vptr_runtime: class_name}
        for cls, vps in rtti_map.items():
            for v in vps:
                self._vptr_class[proc.va(v)] = cls
        # Signature of the binary the cache belongs to: the RTTI vtable set.
        flat = sorted(v for vps in rtti_map.values() for v in vps)
        self.sig = (len(flat), sum(flat))

    # ------------------------------------------------------------------ cache
    def _to_rel(self, m):
        base = self.p.base
        return {int(k) - base: array("q", (a - base for a in v)) for k, v in m.items()}

    def _from_rel(self, rel):
        base = self.p.base
        return {int(k) + base: array("q", (a + base for a in v)) for k, v in rel.items()}

    def _load_cache(self):
        """Load the cached index, but only if it belongs to THIS game process.

        The index maps vtable -> object addresses. Those addresses are only
        valid for the memory layout of the process they were scanned in, so a
        cached index from another run (or from before the game restarted) points
        at the wrong objects. The addresses are stored RELATIVE to the image
        base, which makes them survive ASLR, but the CONTENT still has to be
        re-scanned when the process changes.
        """
        if not self.cache_path or not os.path.exists(self.cache_path):
            return None
        try:
            with open(self.cache_path, "rb") as f:
                data = pickle.load(f)
        except Exception:
            return None
        if not isinstance(data, dict):
            return None
        sig = data.get("sig")
        if sig is not None and tuple(sig) != tuple(self.sig):
            return None                      # different binary build
        if data.get("pid") != self.p.pid:
            return None                      # different game process
        rel = data.get("rel")
        if rel is None:
            return None
        return self._from_rel(rel)

    def _save_cache(self):
        if not self.cache_path:
            return
        try:
            tmp = self.cache_path + ".tmp"
            with open(tmp, "wb") as f:
                pickle.dump({"sig": self.sig, "pid": self.p.pid,
                             "rel": self._to_rel(self._byvptr)}, f)
            os.replace(tmp, self.cache_path)
        except Exception:
            pass

    # ------------------------------------------------------------------ scanning
    def _rss_mb(self):
        try:
            with open("/proc/self/status") as fh:
                for line in fh:
                    if line.startswith("VmRSS"):
                        return int(line.split()[1]) // 1024
        except Exception:
            pass
        return 0

    def build(self, force=False, max_per_class=200000, progress=None, mem_limit_mb=1200):
        if self._byvptr is not None and not force:
            return self._byvptr
        if not force:
            cached = self._load_cache()
            if cached is not None:
                self._byvptr = cached
                return self._byvptr

        # A live scan reads the whole address space. Keep the working set small:
        # 4 MB chunks, and hand freed pages back to the OS between chunks. If the
        # resident size still climbs past the limit, stop and use what we have
        # instead of being OOM-killed (which used to take the game down with it).
        targets = np.array(sorted(self._vptr_class), dtype=np.uint64)
        lo, hi = int(targets[0]), int(targets[-1])
        out = {}
        regions = self.p.regions(writable=True)
        CH = 4 << 20
        aborted = False
        for ri, m in enumerate(regions):
            if aborted:
                break
            a = m["start"]
            if progress:
                progress(ri, len(regions))
            while a < m["end"]:
                n = min(CH, m["end"] - a)
                n -= n % 8
                if n <= 0:
                    break
                buf = self.p.try_read(a, n)
                if buf and len(buf) >= 8:
                    arr = np.frombuffer(buf[: len(buf) - len(buf) % 8], dtype=np.uint64)
                    del buf
                    cand = np.nonzero((arr >= lo) & (arr <= hi))[0]
                    if cand.size:
                        vals = arr[cand]
                        idx = np.searchsorted(targets, vals)
                        idx = np.clip(idx, 0, targets.size - 1)
                        good = targets[idx] == vals
                        for off, v in zip(cand[good], vals[good]):
                            lst = out.get(int(v))
                            if lst is None:
                                lst = out[int(v)] = array("q")
                            if len(lst) < max_per_class:
                                lst.append(a + int(off) * 8)
                        del vals, idx, good, cand
                    del arr
                a += n
                self._trim()
                if self._rss_mb() > mem_limit_mb:
                    aborted = True
                    break
        self._byvptr = out
        if not aborted:
            self._save_cache()
        return self._byvptr

    @staticmethod
    def _trim():
        """Return freed pages to the OS so the RSS does not creep up."""
        try:
            import ctypes
            ctypes.CDLL("libc.so.6").malloc_trim(0)
        except Exception:
            pass

    # ------------------------------------------------------------------ queries
    # Classes whose instances are CREATED MID-GAME. The index is a snapshot taken
    # when the world loads, so a CWarGoal created by "Danzig or War" in 1939, a new
    # CFront after a conquest, etc. is invisible to it. For these we re-read memory.
    DYNAMIC_CLASSES = {"CWarGoal", "CTimedWargoalActivity", "CFront", "CAirWing",
                       "CMilitaryDeploymentConveyor", "COrdersGroup", "CWar"}

    def live_instances(self, class_name: str, max_hits: int = 5000):
        """Real-time scan for ONE class's vtable.

        Scans the heap regions that already hold instances of this class first (new
        objects are allocated in the same arena), and only falls back to the whole
        writable address space when that finds nothing. Returns object addresses.
        """
        import time as _t
        cache = getattr(self, "_live_cache", None)
        if cache is None:
            cache = self._live_cache = {}
        hit = cache.get(class_name)
        if hit and _t.time() - hit[0] < 2.0:
            return list(hit[1])
        out = self._live_scan(class_name, max_hits)
        cache[class_name] = (_t.time(), list(out))
        return out

    def _live_scan(self, class_name: str, max_hits: int = 5000):
        key = class_name
        if not re.match(r"^\d", key):
            cands = [c for c in self.rtti if re.fullmatch(r"\d+" + re.escape(key), c)]
            if not cands:
                return []
            key = cands[0]
        vptrs = [np.uint64(self.p.va(v)) for v in self.rtti.get(key, [])]
        if not vptrs:
            return []
        want = set(int(v) for v in vptrs)
        known = []
        m = self.build()
        for vp in want:
            known.extend(m.get(vp, []))
        regs = self.p.regions(writable=True)

        def scan(region_list):
            hits = []
            CH = 4 << 20
            for rg in region_list:
                a = rg["start"]
                while a < rg["end"]:
                    n = min(CH, rg["end"] - a)
                    n -= n % 8
                    if n <= 0:
                        break
                    buf = self.p.try_read(a, n)
                    if buf and len(buf) >= 8:
                        arr = np.frombuffer(buf[: len(buf) - len(buf) % 8], dtype=np.uint64)
                        for t in want:
                            idx = np.nonzero(arr == t)[0]
                            for off in idx:
                                hits.append(a + int(off) * 8)
                                if len(hits) >= max_hits:
                                    return hits
                        del arr
                    del buf
                    a += n
            return hits

        hot = []
        if known:
            # Only the regions that already contain an instance of this class. New
            # objects are allocated from the same heap arena, so this is both exact
            # and fast (a min/max span would cover the whole heap).
            hot = [r for r in regs
                   if any(r["start"] <= a < r["end"] for a in known)]
        hits = scan(hot) if hot else []
        if not hits:
            # B4 (review 2026-11-07): `self.maps` is read once at attach time, so a
            # heap arena mmapped later in the campaign was never scanned and new
            # objects were invisible. Re-read the map before the full fallback.
            try:
                self.p.refresh_maps()
                regs = self.p.regions(writable=True)
            except Exception:
                pass
            hits = scan(regs)
        return sorted(set(hits))

    def deep_instances(self, class_name: str, max_hits: int = 20000, ttl: float = 5.0):
        """Scan EVERY writable region for this class's vtable.

        `live_instances` restricts itself to the arenas that already held an
        instance when the index was built; an object allocated later in a
        different arena is invisible to it. This walks all ~6 GB of writable
        memory with numpy (2-5 s) and caches the result for `ttl` seconds.
        """
        import time as _t
        cache = getattr(self, "_deep_cache", None)
        if cache is None:
            cache = self._deep_cache = {}
        hit = cache.get(class_name)
        if hit and _t.time() - hit[0] < ttl:
            return list(hit[1])
        key = class_name
        if not re.match(r"^\d", key):
            cands = [c for c in self.rtti if re.fullmatch(r"\d+" + re.escape(key), c)]
            if not cands:
                return []
            key = cands[0]
        want = [np.uint64(self.p.va(v)) for v in self.rtti.get(key, [])]
        if not want:
            return []
        try:
            self.p.refresh_maps()
        except Exception:
            pass
        hits = []
        stop = False
        for rg in self.p.regions(writable=True):
            a = rg["start"]
            while a < rg["end"] and not stop:
                n = min(4 << 20, rg["end"] - a)
                n -= n % 8
                if n <= 0:
                    break
                buf = self.p.try_read(a, n)
                if buf and len(buf) >= 8:
                    arr = np.frombuffer(buf[: len(buf) - len(buf) % 8], dtype=np.uint64)
                    for t in want:
                        idx = np.nonzero(arr == t)[0]
                        for off in idx:
                            hits.append(a + int(off) * 8)
                            if len(hits) >= max_hits:
                                stop = True
                                break
                    del arr
                a += n
        out = sorted(set(hits))
        cache[class_name] = (_t.time(), list(out))
        return out

    def _sanitised(self):
        """The index, rebuilt once if the cached scan is a half-built one.

        `hoi4-guard` starts scanning as soon as the process exists, which can be
        before the campaign world is loaded: that scan is saved with the right pid
        but contains only ~6k vtables and no CCountry/CTechnologyDatabase at all.
        Every later call reuses it and every database-driven tool fails with
        "NoneType + int". A loaded 1936 world has ~9.1k vtables and 6.6M objects,
        so a small index is treated as incomplete exactly once.
        """
        m = self.build()
        if getattr(self, "_sane", False):
            return m
        self._sane = True
        if len(m) < 8000:
            m = self.build(force=True)
        return m

    def instances(self, class_name: str):
        """Either the mangled name ('22CNationalFocusDatabase') or the plain one."""
        plain = re.sub(r"^\d+", "", class_name)
        if plain in self.DYNAMIC_CLASSES:
            return self.live_instances(plain)
        m = self._sanitised()
        key = class_name
        if not re.match(r"^\d", key):
            cands = [c for c in self.rtti if re.fullmatch(r"\d+" + re.escape(key), c)]
            if not cands:
                return []
            key = cands[0]
        res = []
        for v in self.rtti.get(key, []):
            res.extend(m.get(self.p.va(v), []))
        return sorted(set(res))

    def singleton(self, class_name: str):
        inst = self.instances(class_name)
        return inst[0] if inst else None

    def class_of(self, addr: int):
        """Resolve an object's class name from its vptr."""
        v = self.p.try_read(addr, 8)
        if not v or len(v) < 8:
            return None
        vp = struct.unpack("<Q", v)[0]
        c = self._vptr_class.get(vp)
        return re.sub(r"^\d+", "", c) if c else None

    def counts(self, pattern=""):
        m = self.build()
        out = {}
        for vp, addrs in m.items():
            c = self._vptr_class.get(vp)
            if not c:
                continue
            name = re.sub(r"^\d+", "", c)
            if pattern and pattern.lower() not in name.lower():
                continue
            out[name] = out.get(name, 0) + len(addrs)
        return dict(sorted(out.items(), key=lambda kv: -kv[1]))
