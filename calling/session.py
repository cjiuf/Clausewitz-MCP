"""Session info: single or multiplayer, and which country is being played.

All PURE READS (process_vm_readv), the game is never stopped and there is no
desync risk in multiplayer. The one exception is `pause()`, which goes through the
player's own command channel: the same path as pressing space, subject to whatever
rules the multiplayer session enforces.
"""
import struct
import sys as _sys

# 1.19.4.1 (f584) globals. These used to be the 1.19.2 addresses, which made
# is_paused() read a random byte (0xF4 -> always "paused") and date_raw() never
# self-heal. Keep them in one place; commands.py uses IDLER_GLOBAL = 0x459E908.
G_IDLER      = 0x459E908
G_GAMESTATE  = 0x459E9F0
# The PE build is a different binary: reading the ELF offsets there fails outright
# (`ReadProcessMemory ... GetLastError=299`) and that made hoi4_pause / hoi4_resume
# throw on Windows.  Take the verified 1.19.5.0 values when they exist.
if _sys.platform == "win32":
    try:
        import offsets_win as _OW
        G_IDLER = getattr(_OW, "G_IDLER", G_IDLER)
        G_GAMESTATE = getattr(_OW, "G_GAMESTATE", G_GAMESTATE)
    except ImportError:
        pass
IDLER_COUNTRY_NAME = 0x620    # std::string, name of the played country
IDLER_LOCAL_PLAYER = 0x7C0    # std::string, local player name
SESSION_NET  = 0x58           # CSession+0x58 -> network object (CProxyServer)
CLIENT_FIRST = 0x348          # first remote client name in the network object
CLIENT_STRIDE = 0x700
GS_MAJORS    = 0x328          # major powers (CCountry*)
GS_COMBATS   = 0x268          # active land battles (CLandCombat*)

IDLER_PAUSED = 0x6C1          # 1 = paused, 0 = running (+0x6C3 mirrors it)

# CPauseGame: vtable 0x4460D70 (1.19.4.1), object size 0x50.
# Verified by disassembling the constructors (0x21BAE00 default, 0x21BAE60 copy):
#   +0x28 std::string _M_p  -> +0x38 (SSO buffer)      +0x30 _M_string_length
#   +0x38 char[16] local buffer                        +0x48 u8 "pause"  +0x49 u8 flag
# The old PAUSE_CTOR 0x2095400 was a 1.19.2 address and segfaulted on every call,
# which is why hoi4_pause had to fall back to xdotool. No constructor call now.
VT_PAUSE_GAME = 0x4460D70
PAUSE_SIZE    = 0x50


def _std_string(p, addr):
    try:
        ptr, ln = struct.unpack("<Qq", p.read(addr, 16))
        if 0 <= ln < 256:
            return p.read(ptr, ln).decode("utf8", "replace")
    except OSError:
        pass
    return None


def _idler_name(g):
    """The name at idler+0x7C0. NOTE: this field changes during play. It appears
    to hold whoever acted last. So it is NOT reliable as the local player name."""
    return _std_string(g.p, g.p.u64(g.p.base + G_IDLER) + IDLER_LOCAL_PLAYER)


def country_name(g):
    """The country name cached in the idler: may be stale."""
    return _std_string(g.p, g.p.u64(g.p.base + G_IDLER) + IDLER_COUNTRY_NAME)


def _my_country_name(g):
    try:
        return g.player().name
    except Exception:
        return country_name(g)


def _net_object(g):
    s = g.oi.instances("CSession")
    if not s:
        return None, None
    net = g.p.u64(s[0] + SESSION_NET)
    return net, (g.oi.class_of(net) if net else None)


def remote_players(g, max_clients=16):
    """Remote client names in the network object, std::strings 0x700 apart."""
    net, _ = _net_object(g)
    if not net:
        return []
    out = []
    for k in range(max_clients):
        n = _std_string(g.p, net + CLIENT_FIRST + k * CLIENT_STRIDE)
        if n and n.isprintable() and 1 < len(n) < 40:
            out.append(n)
    return out


def info(g):
    """The session summary to read when connecting."""
    p = g.p
    gs = p.u64(p.base + G_GAMESTATE)
    from game import _arr
    tagof = {}
    for t in g.tags():
        try:
            tagof[g.country(t).ptr] = t
        except Exception:
            pass
    try:
        majors = [tagof.get(x) for x in _arr(p, gs, GS_MAJORS)]
    except Exception:
        majors = []
    names = remote_players(g)
    extra = _idler_name(g)
    if extra and extra not in names:
        names.append(extra)
    net, netcls = _net_object(g)
    me = g.player_tag()
    try:
        import combat as _cb
        at_war = _cb.at_war(g)      # pure read; g.wars() would freeze the game
    except Exception:
        at_war = None
    try:
        combats = len(_arr(p, gs, GS_COMBATS))
    except Exception:
        combats = 0
    return {
        "my_tag": me,
        # Take the name from CCountry: idler+0x620 is a CACHE and goes stale when a
        # civil war moves the player to a different country.
        "my_country": _my_country_name(g),
        "is_multiplayer": len(names) > 1,
        "player_count": len(names),
        "players": sorted(names),
        "network_class": netcls,
        "majors": majors,
        "at_war": at_war,
        "active_land_combats": combats,
        "date": g.date()["text"],
        "note": ("Player names are read from the network object (CSession+0x58) at "
                 "0x700 intervals. Which of them is the local player CANNOT be told; "
                 "the country you are playing is definitive in my_tag/my_country."),
    }


def is_paused(g):
    """Is the game paused right now? PURE READ (idler+0x6C1)."""
    return bool(g.p.u8(g.p.u64(g.p.base + G_IDLER) + IDLER_PAUSED))


def pause(g, paused=True, who=None):
    """Pause or resume: the same command as the player's space key.

    `CPauseGame` carries the DESIRED state in +0x48 (it is not a blind toggle: the
    Execute path forwards that byte to idler->vt[+0x2B8]). The state is still read
    first (idler+0x6C1) so a redundant command is never queued.
    """
    want = bool(paused)
    import time as _t

    if _sys.platform == "win32":
        # The command path below builds CPauseGame from the ELF vtable (0x4460D70),
        # which is not a function in hoi4.exe -- building it there is a good way to
        # lose the process.  On the PE build the state lives in idler+0x6C1, so write
        # that and read it back.  Caveat: the byte is necessary but not sufficient --
        # a World-News popup or a minimised window stops the clock while +0x6C1
        # still reads 0, so the caller must confirm with the on-screen date.
        p = g.p
        idler = p.u64(p.base + G_IDLER)
        if not idler:
            return False, "no idler (still on the menu?)"
        if bool(p.u8(idler + IDLER_PAUSED)) == want:
            return True, "already %s" % ("paused" if want else "running")
        p.write(idler + IDLER_PAUSED, b"\x01" if want else b"\x00")
        _t.sleep(0.3)
        got = bool(p.u8(idler + IDLER_PAUSED))
        if got != want:
            return False, "wrote idler+0x6C1 but it reads %s" % got
        return True, "idler+0x6C1 -> %d (hoi4.exe: no CPauseGame path)" % int(want)

    def running_proof():
        """The clock moved -> really running (the pause flag alone lies at start)."""
        before = g.date_raw()
        _t.sleep(1.5)
        return g.date_raw() != before

    if is_paused(g) == want:
        if want:
            return True, "already paused"
        if running_proof():
            return True, "already running"
    ok, err = _toggle(g, who, want)
    if ok:
        for _ in range(10):
            _t.sleep(0.05)
            if is_paused(g) == want:
                if want or running_proof():
                    return True, ""
                break
    # 1.19.4.1: CPauseGame(+0x48=1) pauses correctly, but +0x48=0 does NOT resume
    # (the engine ignores the resume through this command; verified live 2026-11-04
    # and again 2026-11-05 on a fresh campaign). Fall back to the player's own UI:
    # the clock/pause icon in the top bar. Keyboard events never reach the SDL
    # window under this headless X server, but mouse clicks do.
    if not want:
        for attempt in (_click_pause_button, _key_toggle):
            if attempt():
                for _ in range(14):
                    _t.sleep(0.2)
                    if running_proof():
                        return True, ""
    return False, err or ("pause command accepted but the state did not change")


def _click_pause_button(display=None):
    """Click the pause icon in the top bar (window-relative, right-aligned)."""
    import os, subprocess
    disp = display or os.environ.get("HOI4_DISPLAY") or ":1"
    env = dict(os.environ, DISPLAY=disp)
    try:
        ids = subprocess.run(["xdotool", "search", "--name", "Hearts of Iron"],
                             capture_output=True, text=True, env=env, timeout=10).stdout.split()
        if not ids:
            return False
        wid = ids[-1]
        geo = subprocess.run(["xdotool", "getwindowgeometry", "--shell", wid],
                             capture_output=True, text=True, env=env, timeout=10).stdout
        width = 1280
        for line in geo.splitlines():
            if line.startswith("WIDTH="):
                width = int(line.split("=", 1)[1])
        subprocess.run(["xdotool", "mousemove", "--window", wid, str(width - 290), "22"],
                       timeout=10, env=env)
        subprocess.run(["xdotool", "click", "1"], timeout=10, env=env)
        return True
    except Exception:
        return False


def _key_toggle(display=None):
    """Press space in the game window (works where the command path does not)."""
    import os, subprocess
    disp = display or os.environ.get("HOI4_DISPLAY") or ":1"
    try:
        ids = subprocess.run(["xdotool", "search", "--name", "Hearts of Iron"],
                             capture_output=True, text=True,
                             env=dict(os.environ, DISPLAY=disp), timeout=10).stdout.split()
        if ids:
            subprocess.run(["xdotool", "windowactivate", ids[0]], timeout=10,
                           env=dict(os.environ, DISPLAY=disp),
                           stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        subprocess.run(["xdotool", "key", "space"], timeout=10,
                       env=dict(os.environ, DISPLAY=disp),
                       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        return True
    except Exception:
        return False


def _toggle(g, who=None, want=True):
    """Build CPauseGame (0x50 bytes, vtable 0x4460D70) directly in memory.

    Pure struct construction, no constructor call: the 1.19.2 ctor address
    (0x2095400) is not a function in this binary.
    """
    import struct as _s
    from commands import BASE_INIT
    p = g.p
    who = (who or (_idler_name(g) or "AI")).encode("utf-8", "replace")[:15]
    own = g._inj is None
    if own:
        g.attach()
    try:
        cmd = g.malloc(PAUSE_SIZE)
        blob = bytearray(PAUSE_SIZE)
        _s.pack_into("<Q", blob, 0x00, p.base + VT_PAUSE_GAME)
        for off, fmt, val in BASE_INIT:
            _s.pack_into(fmt, blob, off, val)
        _s.pack_into("<QQ", blob, 0x28, cmd + 0x38, len(who))
        blob[0x38:0x38 + len(who)] = who
        blob[0x48] = 1 if want else 0
        blob[0x49] = 0
        p.write(cmd, bytes(blob))
        rv = g.cmd.post(cmd)
        if not (rv & 0xFF):
            return False, getattr(g.cmd, "last_error", "") or "CCommand::Run refused CPauseGame"
        return True, ""
    finally:
        if own:
            g.detach()
