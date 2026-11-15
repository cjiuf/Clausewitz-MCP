"""Offsets recovered by reverse engineering for HOI4 **1.19.5.0 on Windows**.

This is the MSVC/PE counterpart of `offsets.py` (which describes the Linux ELF
build 1.19.4.1.99a5).  The two builds are *not* the same binary and their
version numbers differ, so nothing here may be mixed with the Linux table.

All values are RVAs relative to the image base: at runtime `base + X`.
Classes are found through MSVC RTTI (`research/windows/rtti_win.py` builds the
full `. ?AV<Class>@@` -> vtable map for the installed exe).

Verified live against the game (see research/WINDOWS_1.19.5.md):

    g_gameState -> CCurrentGameState (2 vptrs), GS_COUNTRIES @0x310 gives 440
    CCountry objects, all with the CCountry vtable; CState owner/controller
    fields verified against GER/ENG/SOV state lists; console `pp` executed
    through thread hijack and really granted political power.
"""

VERSION = "1.19.5.0"
BUILD_ID = "Operation Postern v1.19.5.0.7c6c (c184)"
PE_IMAGE_BASE = 0x140000000
PE_SIZE_OF_IMAGE = 0x38A3000

# ---------------------------------------------------------------- globals
# .data starts at base+0x30FD000; that address is also the CPdxNewDeleteAllocator
# singleton used by every CPdxArray.
G_DEFAULT_ALLOC = 0x30FD000

G_GAMESTATE = 0x33DD6B0          # u64 -> CCurrentGameState*
G_CONSOLE_MANAGER = 0x33DD968    # CPdxArray<CConsoleCmd> *inline* (not a pointer!)
G_COUNTRYTAG_DB = None           # no image global; the live DB is inline in CGameState, see GS_TAG_DB
G_MODIFIER_DEFS = 0x33DD1D0      # u64 -> SModifierDef[] (heap, 666 entries).
                                 #   Found by scanning writable memory for 24
                                 #   consecutive records whose int32 at +0x6C
                                 #   equals their index: exactly one candidate.
                                 #   +0x00 std::string "MODIFIER_<NAME>",
                                 #   +0x6C int32 id == index (0..665)
G_STABILITY_BASE = None          # UNVERIFIED

# ------------------------------------------------------------- CGameState
GS_COUNTRIES = 0x310             # CPdxArray<CCountry*>   cap/size 440
GS_TAG2INDEX = 0x340             # CPdxArray<int32>       cap 474 size 443 (identity)
GS_TAG_DB = 0x358                # CPdxArray<std::string> cap 474 size 443, INLINE
                                 #   (0x340 + 0x18: the tag index array is immediately
                                 #    followed by the tag-name array -- verified live,
                                 #    entry 440..442 are the dynamic tags D01..D03)
GS_STATES = 0x2C8                # CPdxArray<CState*>     cap/size 1082
GS_PROVINCES = 0x2B0             # cap/size 13414
GS_MAJORS = 0x328                # cap 9 size 7
GS_ARMIES = 0x640                # CPdxArray<CArmy*>      42 / 35
GS_DIV_TEMPLATES = 0x6F0         # CReferencedDivisionTemplate 711 / 705
GS_EQUIPMENT = 0x708             # CEquipmentVariant      5395 / 4513
GS_PAUSE_HOURS = None            # UNVERIFIED
GS_PLAYER_TAG = 0x520            # u32 player country TAG ID. Verified live by
                                 #   switching tag: 1 (GER) -> 16 (ITA) -> 1 (GER).
                                 #   (0x518 reads 15 = SPR and does NOT follow the
                                 #    player -- do not use it.)

# --------------------------------------------------------------- CCountry
CO_SIZE = 0x1710                 # stride between CCountry allocations
CO_TAG_ID = 0x08                 # u32 tag id == country index in this build
CO_NAME = 0x10                   # std::string, LOCALISED ("德意志第三帝国")
CO_ADJECTIVE = 0x30              # std::string
CO_PLAYER = 0x120                # u32 1 == human player (also set on dummy country 0)
CO_STATES = 0x460                # CPdxArray<CState*> owned states (GER 24, ENG 94, SOV 152)
CO_STATES_B = 0x478              # second state list
CO_STATES_C = 0x4A8              # third state list
CO_ARMIES = 0x290                # CPdxArray<CArmy*> 42 / 30
CO_MODIFIERS = 0x5D0             # CPdxArray<CModifier*>
CO_STABILITY_BASE = 0x10D0       # int64 fixed(1e5): BASE stability only.
                                 #   GER 0.70 / FRA 0.45 / ENG 0.596 / SOV 0.50.
                                 #   The UI showed 81% for GER, so modifiers add
                                 #   +11 points; the computed value is not stored.
CO_WARSUPPORT_BASE = 0x10D8      # int64 fixed(1e5): BASE war support.
                                 #   GER 0.30 (UI 35%), FRA 0.10, ENG 0.10, SOV 0.30.

# status sub-objects (all RTTI-verified pointers)
CO_TECHNOLOGY_STATUS = 0xF60     # CTechnologyStatus*
CO_PRODUCTION_STATUS = 0xF68     # CProductionStatus*
CO_DEPLOYMENT_STATUS = 0xF70     # CDeploymentStatus*
CO_REINFORCEMENT_STATUS = 0xF78
CO_ARMY_UPGRADES_STATUS = 0xF80
CO_DIPLOMACY_STATUS = 0xF88      # CDiplomacyStatus*
CO_POLITICAL_STATUS = 0xF90      # CPoliticalStatus*  <-- political power lives here
CO_LOGISTICS_STATUS = 0xF98
CO_DECISION_STATUS = 0xFA0
CO_OCCUPATION_STATUS = 0xFD0
CO_COLLABORATION_STATUS = 0xFD8
CO_INTEL = 0xFE8
CO_CHARACTERS = 0xFF0
CO_RESOURCES = 0x11F8            # CCountryResources*
CO_NUKES = 0x1310                # CPdxArray<CNuke*> 2 / 2

CO_ECONOMY = None                # UNVERIFIED - 1.19.5 moved factories into status blocks
CO_POLITICS = 0xF90              # alias of CO_POLITICAL_STATUS (api.Country.politics)
CO_FOCUS_TREE = 0x1370            # CNationalFocusTree*    (vtable-scan verified 2026-11-13)
CO_CONT_FOCUS_PAL = 0x1378        # CContinuousFocusPalette*
CO_FOCUS_PROGRESS = 0x1380        # CNationalFocusProgress*
CO_FUEL_STATUS = 0x1580           # CCountryFuelStatus*
CO_EXPERIENCE_STATUS = 0x1588     # CCountryExperienceStatus*
CO_BUILDING_STATUS = 0x1350       # CBuildingStatus*
CO_MANPOWER_SUB = 0x328          # embedded CCountryManpower (RTTI-verified: 440
                                 #   instances, exactly one per CCountry)
CO_MANPOWER_CUR = None           # not a single field on this build -- see CSM_*
# CStateManpower is embedded in CState at +0x830 (verified: 1081 instances, every
# one exactly 0x830 past a CState).  Summed over the owned states these reproduce
# the save's manpower_pool exactly.
CSM_OFF = 0x830
CSM_AVAILABLE = 0x18             # int32, the number the top bar shows
CSM_LOCKED = 0x1C                # int32, population that cannot be recruited
CSM_POPULATION = 0x20            # int32
MP_RECRUIT_RATIO = 0x10          # int64 fixed(1e5) on CCountryManpower
CO_FUEL = None                   # UNVERIFIED

# ------------------------------------------------------- CPoliticalStatus
PO_POLITICAL_POWER = 0xE0        # int64 fixed(1e5); verified by differential scan
PO_COMMAND_POWER = 0x560         # int64 fixed(1e5); second accumulator
PO_RULING_SUB = None             # UNVERIFIED

# ----------------------------------------------------------------- CState
CS_SIZE = None                   # UNVERIFIED
CS_ID = 0x10                     # u32 (stale)
ST_ID = 0x58                     # int32 state id (verified against GER state list)
ST_PROVINCES = 0x18              # CPdxArray<CProvince*> (verified: state 5 -> 11)
ST_BUILDINGS = 0x158             # CPdxArray<CBuilding*> (verified by the factory
                                 #   sums: GER 35+28+10 = 73 on the top bar)
ST_RESISTANCE = None             # UNVERIFIED on this build (Linux: +0x278)
ST_COMPLIANCE = None             # UNVERIFIED on this build (Linux: +0x2A8)
CS_OWNER = 0xC8                  # u32 country index
CS_CONTROLLER = 0xCC             # u32 country index

# ------------------------------------------------------- database items
# These are what `game._load_buildings()` needs.  The database itself is found
# through the MSVC RTTI map (`ObjectIndex.singleton("CBuildingDatabase")`); its
# CPdxArray<CBuildingTemplate*> of 51 entries sits at +0x160 (Linux: +0x40).
# Verified live 2026-11-13: every entry carries an int32 type id at +0x08
# (20002 arms_factory, 20013 industrial_complex, 19446 dockyard, ...) and a
# std::string key at +0x270.
BT_ID = 0x08                     # int32 building type id
BT_KEY = 0x270                   # std::string key ("industrial_complex")
BT_NAME = None                   # UNVERIFIED: the template carries no plain
                                 #   display string on this build; callers fall
                                 #   back to the key. (+0x230 holds "<key>_plural".)
BT_NAME_PLURAL = 0x230           # std::string loc key, present on some entries
BLD_DB_ITEMS = 0x160             # CPdxArray<CBuildingTemplate*> inside the db
# CBuilding type ids that are NOT in CBuildingDatabase.  `infrastructure` (12203)
# has no template entry of its own, yet every state carries a CBuilding of that
# type whose level is the state's infrastructure.  Verified against the campaign
# autosave: summing type 12203 over GER's 24 states gives 81, and the save's
# `buildings.infrastructure` total is also 81 (state 5: memory 3, save 3).
BT_EXTRA = {12203: "infrastructure"}
BG_LEVEL = 0x40                  # int16 level on CBuilding


# ------------------------------------------------------- CTechnologyTemplate
# The database (CTechnologyDatabase) keeps its templates at +0x48
# (CPdxArray<CTechnologyTemplate*>, cap 711 / size 553; empty slots are
# TNullObject).  The template's script key is a std::string at +0x10
# ("improved_ship_hull_light"), not the Linux +0x08.  Verified live 2026-11-14.
TT_NAME = 0x10
TT_ID = 0x3C

# ------------------------------------------------------ focus / economy

FP_CURRENT = None
FP_ROOT_FOCUSES = None
EC_MIL_FACTORIES = None
EC_NAVAL_DOCKYARDS = None
EC_CIV_FACTORIES = None
EC_CIV_RESERVED_A = None
EC_CIV_RESERVED_B = None

FIXED = 100000

# ------------------------------------------------------------- functions
# Only addresses that were actually used are listed.  Everything else still
# needs to be located on the Windows build.
FN_CONSOLE_EXECUTE = None
FN_CONSOLE_DISPATCH = None
FN_TAG_GET_COUNTRY = None
FN_GET_POLITICS = None
FN_GET_STABILITY = None
FN_GET_WAR_SUPPORT = None
FN_GET_MANPOWER = None
FN_GET_COUNTRY_NAME = None
FN_GET_MODIFIER = None
FN_OPERATOR_NEW = None
FN_OPERATOR_DELETE = None

# console commands that were confirmed by name (see research/windows/wconsole3.py)
CMD_RVA = {
    "test": 0x2A5070,
    "deironman": 0x25FE10,
    "pp": 0x285FF0,
    "manpower": 0x281850,
    "tag": 0x2A4570,
    "annex": 0x256100,
    "whitepeace": 0x2AFEF0,
    "instant_wargoal": 0x27A8B0,
    "observe": 0x285120,
    "research": 0x294310,
}

# ------------------------------------------------------------------ vtables
VT_COUNTRY = 0x2864730
VT_GAMESTATE = (0x279F0F0, 0x279F148)              # base class, no live object
VT_CURRENT_GAMESTATE = (0x279F168, 0x279F1C0)      # the real one
VT_STATE = (0x29B4D58, 0x29B4DA8)
VT_WARGOAL = 0x2A59250

# --------------------------------------------------------------- structures
PDXARRAY_DATA = 0x00
PDXARRAY_CAP = 0x08
PDXARRAY_SIZE = 0x0C
PDXARRAY_ALLOC = 0x10
PDXARRAY_SZ = 0x18

STDSTRING_SZ = 0x20              # MSVC: {char buf[16]; size_t size; size_t cap;}

# ------------------------------------------------------------- command layer
# CInGameIdler instance (a u64 global). The object is real -- reading [idler]
# gives a CInGameIdler vtable -- but the Linux slot numbers do NOT carry over:
# see research/WINDOWS_1.19.5.md §24. vtable+0xB8 returns idler+0x6B8, which
# holds UI views (CTopBar/CAchievementsView), not the command queue.
G_IDLER = 0x33DDB08
IDLER_VT_QUEUE = None            # UNVERIFIED on this build (Linux: 0x90)
IDLER_VT_TAG = None              # UNVERIFIED on this build (Linux: 0xC0)
# vtable+0x08 = Allocate(this, size), vtable+0x10 = Deallocate(this, ptr).
# Verified round-trip through the injector (0x40 bytes alloc + free).
ALLOC_VTABLE = 0x2794550
ALLOC_OFF = 0x30FD000           # the allocator OBJECT lives at base + this

CMD_STRIDE = 0x1C8               # identical to the Linux build
CMD_NAME = 0x08
CMD_ALIAS_COUNT = 0x28
CMD_ALIASES = 0x30
CMD_FUNC = 0x68
CMD_CTX = None                   # UNVERIFIED

SRESULT_SZ = 0x28                # {bool ok; std::string message;}  (size at +0x18, cap at +0x20)
MGR_CMDS = 0x00

MODDEF_STRIDE = 0x78             # same as the Linux build
MODDEF_NAME = 0x00               # std::string, e.g. "MODIFIER_STABILITY_FACTOR"
MODDEF_ID = 0x6C                 # int32; equals the entry's index

DAYS_PER_YEAR = 365
MONTH_LENGTHS = [31, 28, 31, 30, 31, 30, 31, 31, 30, 31, 30, 31]

DATE_SIGNATURE_FLOAT = None
DATE_SIG_TO_VALUE = None
