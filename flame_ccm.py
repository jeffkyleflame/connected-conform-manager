"""
Script Name: Connected Conform Manager
Script Version: 0.9.9
Flame Version: 2027 (verified working on 2026)
Written by: Jeff Kyle
Creation Date: 06.13.26
Description:

    Flame CCM -- a connected-conform lifecycle manager built around the
    CONFORM HUB (the sources/shots sequence) as the organizational hub, where
    a shot that exists as several real sources becomes ONE shot with stacked
    pieces rather than repeated entries. It covers the connected-conform
    workflow that Flame cannot represent natively: heterogeneous per-instance
    versioning (v7 in the EN spots, v5 in the ES spots of the same logical shot,
    live at once) and selective coverage trimming. Flame's segment connections
    are all-or-nothing -- every connected instance gets the same media/version;
    CCM fills exactly that gap.

    STATUS: PUBLIC BETA (0.9.x). The audits and views are used on real jobs;
    some sections are unfinished and say so in the tool:
        - The Hub and Ledger tabs are UNDER DEVELOPMENT: Source Segment
          Connections and Duplicate Connected Segment are not in Flame's
          python API, and the connect -> publish workflow depends on both.
          CCM hands those steps to Flame's native tools and verifies the
          result.
        - Post Publish relink is not built yet.
        - Remove from Hub is experimental and untested.
        - Create Conform Hub's current build (with stacked pieces), stack-aware
          Renumber and Prep Publish Fix mode are built but not yet verified on
          a real job.
    CCM is READ-ONLY by default: nothing on a timeline changes until you allow
    changes in Settings. One shared scan feeds every audit and view, and it
    re-derives almost everything live (placement from connections, identity
    from source attrs, version from the openclip), so the on-disk state is
    thin -- only triage outcomes the scan cannot reconstruct.

    Audits:
        Duplicate    - Tier-1 (shared source_uid) then Tier-2 (same logical shot
                       across imports: source-TC overlap, camera-token tiebreak).
        Over-coverage - per source, union the consumed ranges across sequences;
                       propose minimal covering pieces + handles; shade the dead
                       middle as a trim candidate.
        Off-latest   - flag any openclip whose current version is not its latest.
        Timewarp     - flag retimed segments (conservative source envelope; never
                       auto-split).

    Views:
        Timelines map   - every sequence as a lane on one shared scale (hub lane
                       first, all tracks stacked); click a shot to inspect it,
                       double-click to go there in Flame.
        Coverage drawer - per shot: full source extent with consumed bands +
                       dead zones + per-timeline lanes.
        Connections     - the cut-flow braid (hub + every sequence as lanes,
                       ribbons following each shot) beside the shot list, and
                       the picked shot's family tree (hub piece on top, spots
                       by length, line colour = connection state).
        Shot preview    - one scrubbable, zoomable filmstrip panel shared by
                       Timelines, Connections, Hub and Ledger (one switch).

    Install (single file, its own folder, unique name; full Flame restart):
        /opt/Autodesk/shared/python/flame_ccm/flame_ccm.py

    Tabs:
        Inventory     - every consumed segment in scope, with source / camera /
                        version / dup / timewarp status; Timecode<->Frames toggle.
        Duplicates    - the triage queue; merge / keep-both / dismiss (decisions
                        persist so post-publish version-dupes stop re-flagging).
        Timelines     - the map + the coverage drawer.
        Connections   - the braid + family tree; double-click selects the
                        connected segments in Flame.
        Hub / Ledger  - (WIP) Conform Hub audit + build; publish ledger + prep.
        Settings      - sources-sequence name, handles, camera token, match chars,
                        scope, state folder, display units.

    Built by Jeff Kyle with Claude (Anthropic).

Menus:

    Right-click on a timeline segment  ->  Connected Conform Manager  ->  Open...
    Right-click in the Media Panel     ->  Connected Conform Manager  ->  Open...
    Flame main menu                    ->  Connected Conform Manager  ->  Open Manager...
"""

import os
import re
import json
import time
import shutil
import hashlib
import logging

import flame
from PySide6 import QtWidgets, QtCore, QtGui


log = logging.getLogger("flame_ccm")
if not log.handlers:
    _h = logging.StreamHandler()
    _h.setFormatter(logging.Formatter("%(message)s"))
    log.addHandler(_h)
    log.setLevel(logging.INFO)
    log.propagate = False

# Shown in the window header, the window title and the probe dump. Pre-1.0 on
# purpose: public, but with unfinished sections (BETA_NOTE). Keep "Script
# Version" in the docstring above in step -- the test suite checks it.
__version__ = "0.9.9"

BETA_NOTE = (
    "Public beta. The audits and views are used on real jobs; these sections "
    "are unfinished:\n"
    "  • The Hub and Ledger tabs are UNDER DEVELOPMENT: Source Segment "
    "Connections and Duplicate Connected Segment are not in Flame's python "
    "API, and the connect → publish workflow depends on both. CCM hands "
    "those steps to Flame's native tools and verifies the result.\n"
    "  • Post Publish relink is not built yet.\n"
    "  • Remove from Hub is experimental and untested.\n"
    "  • Create Conform Hub's current build (with stacked pieces), stack-aware "
    "Renumber and Prep Publish Fix mode are built but not yet verified on a "
    "real job.\n\n"
    "Read-only by default: nothing on a timeline changes until you allow "
    "changes in Settings.")

# The banner on the Hub and Ledger tabs, worded so their unfinished state
# is unmistakable. Public text — it says what Flame's API lacks, nothing more.
WIP_NOTE = (
    "<b>UNDER DEVELOPMENT</b> — Flame's python API has no Create Source Segment "
    "Connection and no Duplicate Connected Segment, and the connect → publish "
    "workflow this tab is built for depends on both. Without them CCM hands "
    "those steps to Flame's native tools and checks the result. Use it on a "
    "copy of a job, not on a delivery.")


# ====================================================================
# Settings (per-user) + thin project State (per-project, configurable)
# ====================================================================

# Scope labels. The Sources Sequence is the hub; every other sequence in scope
# is a potential consumer. The hub is scanned (it defines shot identity and the
# coverage extent) but excluded from the consumed-by tally.
SCOPES = ["Selected Sequences", "Current Reel", "Current Reel Group",
          "Current Desktop", "Open Sequences"]

SETTINGS_PATH = os.path.join(os.path.expanduser("~"), "flame", "flame_ccm_settings.json")
DEFAULT_SETTINGS = {
    "scope": "Current Reel",
    "sources_seq_name": "Conform_Hub",  # the hub; excluded from consumer tally
    "state_dir": "",                # blank -> per-project default
    "handle_frames": 8,             # padding added to each minimal covering piece
    "merge_gap_frames": 0,          # consumed ranges within this gap merge into one
    "camera_token": "first_underscore",   # camera-extraction strategy
    "match_chars": 10,              # Tier-2 camera-prefix compare length
    "inv_units": "Timecode",        # Inventory In/Dur/Source display: "Timecode" | "Frames"
    "inv_hidden": [],               # Inventory columns the user has switched off
    # Reference-picture detection (the "95/5" system: heuristics catch most refs,
    # ref_overrides in state nudge the rest). Artist workflows differ wildly, so
    # every knob is a setting, never a hard-coded convention.
    "ref_span_pct": 60,             # segment spanning >= this % of its sequence looks like a ref
    "ref_name_tokens": "ref, reference, offline",   # name/track tokens that mark a ref
    # Master switch: every timeline mutation (Prep Publish, …) stays
    # disabled until the user opts in. The community build ships read-only.
    "enable_mutations": False,
    # Publish snapshot tracks grow from the BOTTOM of the live version by
    # default (a common layout: v000 at the very bottom, live/openclips on top).
    "publish_track_position": "bottom",
    # snapshot container: a real VERSION (the workflow's 'new version track';
    # Flame picks its position) or a plain track (position controllable).
    "publish_snapshot_as": "version",
    # Probe extras that can hang on a real job: the wiretap frame read blocked
    # for two minutes on network/uncached media, and it is only a diagnostic,
    # so it is opt-in.
    "probe_wiretap": False,
    # Slates: sequences start on an hour and the slate sits just before it.
    "detect_slates": True,
    "slate_lead_secs": 60,
    # High-security jobs: replace client names/paths in the probe dump with
    # stable placeholders so the file can be shared for support.
    "probe_anonymize": True,
    # Shot identity: require matching (normalised) source names before two
    # segments count as the same shot. Essential on shot-named jobs, where
    # every source shares the job prefix and the camera token cannot tell
    # JOB_sh0060 from JOB_sh0060_ALT. Turn off only if the same footage
    # arrives under genuinely different names per spot.
    "identity_by_name": True,
}

CAMERA_STRATEGIES = ("first_underscore", "whole_name", "filename_stem")


def _fresh(defaults):
    """A deep copy of a defaults dict. A plain dict() copy shares the nested
    dicts and lists, so an in-place edit (st.setdefault(...)[k] = v) on a
    project with no state file changed the defaults themselves — and the next
    project opened in the same Flame session started out with the first
    one's hub names, track roles, overrides and decisions."""
    return json.loads(json.dumps(defaults))


def load_settings():
    try:
        with open(SETTINGS_PATH) as f:
            return {**_fresh(DEFAULT_SETTINGS), **json.load(f)}
    except Exception:
        return _fresh(DEFAULT_SETTINGS)


def _atomic_write_json(path, obj):
    """Write JSON via temp file + os.replace, so a crash mid-write can never
    leave a half-written (corrupt) file at the real path."""
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(obj, f, indent=2, ensure_ascii=False)
        f.flush()
        os.fsync(f.fileno())
    os.replace(tmp, path)


def save_settings(settings):
    try:
        _atomic_write_json(SETTINGS_PATH, settings)
    except Exception as e:
        log.warning("settings save failed: %s", e)


def _default_state_dir():
    try:
        sf = str(flame.projects.current_project.setups_folder).strip("'\"")
        if sf:
            return os.path.join(sf, "flame_ccm")
    except Exception:
        pass
    return os.path.join(os.path.expanduser("~"), "flame", "flame_ccm")


def state_path():
    d = (load_settings().get("state_dir") or "").strip() or _default_state_dir()
    return os.path.join(d, "conform_state.json")


# Thin state. The live scan re-derives placement, version and identity; state
# persists ONLY what the scan cannot reconstruct:
#   dup_decisions     - conflict-set key -> "merge" | "keep_both" | "dismiss".
#   identity_overrides- source_uid -> hand-corrected camera name.
#   sanctioned_dupes  - conflict-set keys the user has accepted as expected.
#   ref_overrides     - segment key -> "ref" | "source" (the 5% nudge: user
#                       corrections to the heuristic ref-picture detection).
DEFAULT_STATE = {"dup_decisions": {}, "identity_overrides": {},
                 "sanctioned_dupes": [], "ref_overrides": {},
                 # sequences CCM itself created as conform hubs — they
                 # count as hubs regardless of what the user named them
                 "hub_names": [],
                 # Wizard answers: {sequence name: {track_id: role}}. No
                 # heuristic can tell a still that IS a shot from a still
                 # that is a graphic — only the artist knows, so the wizard
                 # asks once per sequence and this is where it lands.
                 "track_roles": {},
                 # {media file path: first frame PAST the end of that media}.
                 # The scan can't see media length; the hub build learns it
                 # when a tail stretch stops at the media end. The Hub tab
                 # then reads use frames past it as HELD (a freeze on the
                 # last frame), not missing.
                 "media_ends": {}, "media_ends_next": {}}

# The hub is called the CONFORM HUB; a new hub is named
# Conform_Hub by default. Detection still accepts the older
# names and Flame's native one so existing projects keep working untouched.
HUB_ALIASES = ("Conform_Hub", "Conform Hub", "Sources Sequence")
# hub names that were only ever DEFAULTS — a Settings value equal to one of
# these is not a custom job-code name, so Create Conform Hub offers Conform_Hub
LEGACY_HUB_DEFAULTS = ("Sources Sequence", "Conform Hub", "Conform_Hub")

# Roles a track can be given in the wizard. 'source' = footage/shots (the
# only thing the conform audits and the hub build care about), 'graphic' =
# GFX/titles (managed separately — a different category entirely),
# 'ref' = reference picture, 'fx' = comp elements living on their own track.
TRACK_ROLES = ("source", "graphic", "ref", "fx", "slate", "published")
ROLE_LABELS = {"source": "Shots / footage", "graphic": "Graphics",
               "ref": "Reference", "fx": "FX / elements", "slate": "Slate",
               # hub-only: tracks holding sources that were published from but
               # live in no timeline — grade orphans kept for the record
               "published": "Published Hub Track (archive)",
               "": "(let CCM decide)"}


def load_state():
    """Read project state, distinguishing 'missing' from 'corrupt': an existing
    but unparseable file is quarantined (renamed *.corrupt) and reported, rather
    than silently treated as empty -- otherwise the next save would clobber every
    triage decision the user has made."""
    p = state_path()
    try:
        with open(p) as f:
            raw = f.read()
    except FileNotFoundError:
        return _fresh(DEFAULT_STATE)
    except Exception as e:
        log.warning("state read failed at %s: %s", p, e)
        return _fresh(DEFAULT_STATE)
    try:
        return {**_fresh(DEFAULT_STATE), **json.loads(raw)}
    except Exception as e:
        quarantine = p + ".corrupt"
        try:
            os.replace(p, quarantine)
            log.warning("state at %s is corrupt (%s) -- moved to %s so a save "
                        "can't overwrite it; starting empty", p, e, quarantine)
        except Exception:
            log.warning("state at %s is corrupt (%s) and could not be "
                        "quarantined -- do NOT save until it's recovered", p, e)
        return _fresh(DEFAULT_STATE)


def save_state(state):
    try:
        _atomic_write_json(state_path(), state)
        return True
    except Exception as e:
        log.warning("state save failed: %s", e)
        return False


# ====================================================================
# Pure time / rate parsing  (no flame dependency -- off-box tested)
# ====================================================================

def parse_rate(value, default=24.0):
    """Frame rate as a float. Accepts a number or a Flame string like
    '25 fps' / '23.976 fps'. Defaults to 24.0."""
    if isinstance(value, (int, float)):
        return float(value)
    if value is None:
        return default
    m = re.search(r"\d+(?:\.\d+)?", str(value))
    return float(m.group(0)) if m else default


def parse_timecode(value, fps):
    """'HH:MM:SS:FF' or 'HH:MM:SS+FF' -> integer frames at round(fps).

    Flame is inconsistent about the frame-field separator: PySegment timecodes
    use ':' while a PyClip's duration uses '+' (e.g. '00:00:07+08'). Both are
    accepted here. Returns None if the string is not a timecode."""
    if value is None:
        return None
    s = str(value).strip().strip("'\"")
    m = re.match(r"^(-?)(\d+):(\d+):(\d+)[:+](\d+)$", s)
    if not m:
        return None
    sign, hh, mm, ss, ff = m.groups()
    fpsi = max(1, int(round(parse_rate(fps))))
    total = ((int(hh) * 60 + int(mm)) * 60 + int(ss)) * fpsi + int(ff)
    return -total if sign == "-" else total


def to_frames(value, fps):
    """Flame time string/int -> integer frame count. Accepts a timecode (either
    separator), a bare frame integer, or an int. None if unparseable."""
    if value is None:
        return None
    if isinstance(value, int):
        return value
    s = str(value).strip().strip("'\"")
    f = parse_timecode(s, fps)
    if f is not None:
        return f
    return int(s) if re.match(r"^-?\d+$", s) else None


def frames_to_tc(frames, fps):
    """Integer frames -> non-drop HH:MM:SS:FF at round(fps)."""
    if frames is None:
        return ""
    fpsi = max(1, int(round(parse_rate(fps))))
    sign = "-" if frames < 0 else ""
    f = abs(int(frames))
    ff = f % fpsi
    s = f // fpsi
    return "%s%02d:%02d:%02d:%02d" % (sign, s // 3600, (s // 60) % 60, s % 60, ff)


# ====================================================================
# Pure identity / camera extraction  (off-box tested)
# ====================================================================

_HUB_SUFFIX_RE = re.compile(r"^[ _\-.]*(publish|published|pub|copy|snapshot|"
                            r"archive|bak|backup|old)\b", re.I)


def is_hub_name(seq_name, hub_name):
    """Is this sequence the hub? EXACT match, or the hub name followed by a
    publish/copy suffix — derived hubs like
    'Sources Sequence_publish' must count as hub.

    NOT a bare prefix match. A real job's hub was simply called 'JOB' and
    every spot was 'JOB_SPOT_30_…', 'JOB_WEB_15_…' — so prefix matching turned
    the ENTIRE job into hubs the moment that name was registered, and no
    amount of unchecking helped."""
    s = (seq_name or "").strip().lower()
    h = (hub_name or "").strip().lower()
    if not h or not s.startswith(h):
        return False
    rest = s[len(h):]
    return rest == "" or bool(_HUB_SUFFIX_RE.match(rest))


def camera_token(source_name, editorial_name="", strategy="first_underscore"):
    """The camera-name token used as the Tier-2 identity tiebreaker.

    'first_underscore' (default): first underscore-delimited token of the source
    name, falling back to the editorial/file name. 'filename_stem': basename
    without extension. 'whole_name': the cleaned name verbatim. Trailing junk is
    tolerated by the prefix compare in duplicate_sets, so over-capturing here is
    safe."""
    raw = (source_name or "").strip().strip("'\"")
    if not raw:
        raw = (editorial_name or "").strip().strip("'\"")
    if not raw:
        return ""
    base = raw.rsplit("/", 1)[-1].rsplit("\\", 1)[-1]
    if strategy == "whole_name":
        return raw
    stem = base[:base.rindex(".")] if "." in base else base
    # per-spot versioned deliveries tag the SAME shot with a version prefix
    # (V01-A003_C002… in one spot, V05-A003_C002… in another). The tag is
    # delivery metadata, not identity — strip it
    # so the copies can meet in a duplicate set at all. DASH form only for
    # now: 'V01_' could be a legitimate camera/roll name; extend from real
    # name pairs, not guesses.
    stem = re.sub(r"^[vV]\d{1,4}-", "", stem) or stem
    if strategy == "filename_stem":
        return stem
    # first_underscore
    return stem.split("_", 1)[0] if "_" in stem else stem


def normalize_source_name(name):
    """A source name reduced to its shot identity: no path, no extension, no
    delivery/version tag, no frame number, lowercase.

    This exists because the camera token cannot carry identity on a
    shot-named job, where every source is named like `JOB_sh0060`,
    `JOB_sh0060_ALT`, `JOB_sh0100`… so `first_underscore` returns "JOB" for
    all of them, every pair reads as camera-compatible, and any two shots
    whose source timecodes happen to overlap merge into one logical shot —
    which is why the _ALT variant of a shot could not be selected
    separately. Normalised names keep `JOB_sh0060` and `JOB_sh0060_ALT`
    apart while still folding `V01-A003_C002` and `V05-A003_C002` together."""
    raw = str(name or "").strip().strip("'\"")
    if not raw:
        return ""
    base = raw.rsplit("/", 1)[-1].rsplit("\\", 1)[-1]
    base = re.sub(r"\.\d+$", "", base)                 # trailing frame number
    if "." in base:
        base = base[:base.rindex(".")]                 # extension
    base = re.sub(r"\.\d+$", "", base)                 # .00001001 before ext
    base = re.sub(r"^[vV]\d{1,4}-", "", base)          # V01- delivery tag
    base = re.sub(r"[._-][vV]\d{1,4}$", "", base)      # _v011 version tag
    return base.strip("._- ").lower()


def _names_compatible(a_key, b_key):
    """Two normalised source names can be the same shot when they are equal,
    or when either is unknown. Unknown never blocks a match — identity then
    falls back to timecode + camera exactly as before."""
    if not a_key or not b_key:
        return True
    return a_key == b_key


def src_end(d):
    """Half-open end of a row's source range. On box seg.source_out is the
    INCLUSIVE last frame a use shows (out - in + 1 == record duration, and a
    timewarp's out is its last displayed frame), while every range CCM
    computes with is half-open [first, end) — so the end is source_out + 1.
    Mixing the two built timewarp-tailed hub pieces one frame long and read
    every straight cut one frame short."""
    b = d.get("src_out")
    return None if b is None else b + 1


def media_ends_of(state):
    """{file path: first frame past the media} from project state. Builds
    before the frame-convention fix stored the LAST media frame under
    "media_ends" — still read, shifted by one — and a newer build writes the
    first frame past under "media_ends_next", a key the old code never
    touches, so an old copy saving state can't be misread."""
    st = state or {}
    ends = {k: v + 1 for k, v in (st.get("media_ends") or {}).items()
            if v is not None}
    ends.update(st.get("media_ends_next") or {})
    return ends


def _ranges_overlap(a_in, a_out, b_in, b_out):
    """Half-open [in,out) overlap. None endpoints never overlap."""
    if None in (a_in, a_out, b_in, b_out):
        return False
    return a_in < b_out and b_in < a_out


def _cam_compatible(a_key, b_key, match_chars):
    """Two camera prefixes are compatible (could be the same shot) when either
    is unknown, or their first match_chars characters match. Two clearly
    different cameras at the same timecode are legitimately different shots."""
    ka = (a_key or "")[:match_chars]
    kb = (b_key or "")[:match_chars]
    if not ka or not kb:
        return True
    return ka.lower() == kb.lower()


def duplicate_sets(instances, match_chars=10, require_name=True):
    """Group consumed instances into duplicate conflict sets.

    Each instance is a dict with at least: src_uid, src_in, src_out, cam_key.
    Tier 1 unions any instances sharing a source_uid (literal shared source).
    Tier 2 unions instances whose source-TC ranges overlap AND whose cameras are
    compatible (same logical shot via different media). Returns a list of
    {'tier': 1|2, 'members': [index, ...]} for every group of 2+, tier-1 groups
    first. Singletons are omitted."""
    n = len(instances)
    parent = list(range(n))

    def find(x):
        while parent[x] != x:
            parent[x] = parent[parent[x]]
            x = parent[x]
        return x

    def union(a, b):
        ra, rb = find(a), find(b)
        if ra != rb:
            parent[ra] = rb

    uid_pair = set()                 # frozensets unioned via a shared uid
    by_uid = {}
    for i, d in enumerate(instances):
        u = d.get("src_uid")
        if u:
            by_uid.setdefault(u, []).append(i)
    for idxs in by_uid.values():
        for j in idxs[1:]:
            union(idxs[0], j)
            uid_pair.add(frozenset((idxs[0], j)))

    for i in range(n):
        a = instances[i]
        for j in range(i + 1, n):
            b = instances[j]
            if find(i) == find(j):
                continue
            if _ranges_overlap(a.get("src_in"), src_end(a),
                               b.get("src_in"), src_end(b)) and \
               _cam_compatible(a.get("cam_key"), b.get("cam_key"), match_chars) and \
               (not require_name
                or _names_compatible(a.get("name_key"), b.get("name_key"))):
                union(i, j)

    comps = {}
    for i in range(n):
        comps.setdefault(find(i), []).append(i)

    groups = []
    for members in comps.values():
        if len(members) < 2:
            continue
        mset = set(members)
        tier = 1 if any(p <= mset for p in uid_pair) else 2
        groups.append({"tier": tier, "members": sorted(members)})
    groups.sort(key=lambda g: (g["tier"], -len(g["members"])))
    return groups


def conflict_key(instances, members):
    """Stable key for a conflict set, used to look up a persisted dup decision.
    Built from the members' source uids (or identity fallbacks) so the same
    real-world conflict keys identically across scans even as table order shifts."""
    parts = []
    for i in members:
        d = instances[i]
        parts.append(str(d.get("src_uid") or d.get("ident") or i))
    return "|".join(sorted(parts))


# ====================================================================
# Pure coverage math  (off-box tested)
# ====================================================================

def merge_ranges(ranges, gap=0):
    """Merge [in,out) intervals that overlap or sit within `gap` frames of each
    other. Drops degenerate (in>=out) and None-bearing ranges. Returns a new
    sorted, merged list."""
    clean = [(a, b) for (a, b) in ranges if a is not None and b is not None and a < b]
    if not clean:
        return []
    clean.sort()
    out = [list(clean[0])]
    for a, b in clean[1:]:
        if a <= out[-1][1] + gap:
            out[-1][1] = max(out[-1][1], b)
        else:
            out.append([a, b])
    return [(a, b) for a, b in out]


def covering_pieces(ranges, handle=0, extent=None, gap=0):
    """Minimal covering pieces for a set of consumed ranges: merge them (within
    gap), pad each side by `handle`, clamp to `extent` (lo, hi) if given, then
    re-merge so handles that now overlap collapse. This is what the conform needs
    to actually cut -- the smallest set of pieces that covers everything used."""
    merged = merge_ranges(ranges, gap)
    if not merged:
        return []
    padded = []
    for a, b in merged:
        a2, b2 = a - handle, b + handle
        if extent is not None:
            lo, hi = extent
            if lo is not None:
                a2 = max(a2, lo)
            if hi is not None:
                b2 = min(b2, hi)
        padded.append((a2, b2))
    return merge_ranges(padded, 0)


def dead_zones(ranges, extent, gap=0):
    """The unused interior of a source: the parts of `extent` (lo, hi) that no
    consumed range covers. These are the trim candidates the Coverage Ruler
    shades. Leading/trailing dead space is included."""
    lo, hi = extent
    if lo is None or hi is None or lo >= hi:
        return []
    merged = [r for r in merge_ranges(ranges, gap) if r[1] > lo and r[0] < hi]
    gaps, cursor = [], lo
    for a, b in merged:
        a = max(a, lo)
        if a > cursor:
            gaps.append((cursor, a))
        cursor = max(cursor, min(b, hi))
    if cursor < hi:
        gaps.append((cursor, hi))
    return gaps


def coverage_fraction(ranges, extent, gap=0):
    """Fraction of `extent` that consumed ranges cover (0.0-1.0). Handy as a
    quick 'how much of this source is actually used' number."""
    lo, hi = extent
    if lo is None or hi is None or hi <= lo:
        return 0.0
    used = sum(b - a for a, b in merge_ranges(
        [(max(a, lo), min(b, hi)) for a, b in ranges if b > lo and a < hi], gap))
    return max(0.0, min(1.0, used / float(hi - lo)))


def piece_membership(instances, members, pieces):
    """For each covering piece, the consumer members (indices into `instances`)
    whose consumed source range intersects it — i.e. which timelines each piece
    serves. Hub/ref members never count. An instance straddling two pieces
    (possible with a small merge gap) appears in both."""
    out = []
    for (a, b) in pieces:
        got = []
        for i in members:
            d = instances[i]
            if d.get("is_hub") or d.get("role", "source") != "source":
                continue
            if _ranges_overlap(a, b, d.get("src_in"), src_end(d)):
                got.append(i)
        out.append(got)
    return out


def split_proposal(instances, members, pieces):
    """The human-readable split analysis for one logical shot: each covering
    piece labeled A, B, C… with the sequences it serves. Two or more pieces =
    a natural split candidate (piece boundaries are where the shot can be cut
    apart so each timeline keeps only what it uses — the mutation itself is
    a separate step; this is the read-only determination)."""
    mem = piece_membership(instances, members, pieces)
    out = []
    for k, ((a, b), got) in enumerate(zip(pieces, mem)):
        label = ""
        n = k + 1
        while n > 0:
            n, r = divmod(n - 1, 26)
            label = chr(65 + r) + label
        out.append({"label": label, "range": (a, b),
                    "seqs": sorted({instances[i]["seq"] for i in got})})
    return out


def subtract_ranges(ranges, cover):
    """Parts of the [in,out) `ranges` NOT covered by `cover` (both merged
    first). The hub audit uses this to find frames consumers cut with that the
    Sources Sequence doesn't actually hold."""
    src = merge_ranges(ranges, 0)
    cov = merge_ranges(cover, 0)
    out = []
    for a, b in src:
        cur = a
        for ca, cb in cov:
            if cb <= cur or ca >= b:
                continue
            if ca > cur:
                out.append((cur, min(ca, b)))
            cur = max(cur, cb)
            if cur >= b:
                break
        if cur < b:
            out.append((cur, b))
    return out


_HUB_NAME_RE = re.compile(r"_\d{2,}0$")


def _is_live_hub(d):
    """A hub row that counts as the LIVE Conform Hub. A hub track the wizard
    has tagged (an archive of published grade orphans, a graphics track) is
    still `is_hub` but must not count as live hub coverage (its pieces are
    not live shots)."""
    return bool(d.get("is_hub")) and d.get("role", "hub") == "hub"


def hub_shot_name_ok(name):
    """The hub naming convention: shots end `_###0` (0010, 0020, 0030 —
    the trailing zero leaves nine insertion slots for late-added shots)."""
    return bool(_HUB_NAME_RE.search((name or "").strip()))


# The publish workflow labels snapshot version tracks "Publish 00",
# "Publish 01", … inside the Sources Sequence. Those segments are hub rows but
# they are the publish LEDGER's domain — never forks/coverage/order material.
PUB_TRACK_RE = re.compile(r"^\s*publish[ _-]?(\d+)\s*$", re.IGNORECASE)


def publish_track_index(track_name):
    """'Publish 00' -> 0, 'publish_01' -> 1; None when the track is not a
    publish snapshot track."""
    m = PUB_TRACK_RE.match(str(track_name or ""))
    return int(m.group(1)) if m else None


def publish_track_name(nn):
    return "Publish %02d" % int(nn)


def plan_hub_rename(names_in_order, base):
    """The renumber convention applied to the hub's record order:
    base_0010, base_0020, … (trailing zero = insertion slots). Returns
    [(old, new)] for every position, including already-correct ones — the
    caller decides what to skip/show."""
    return [(old, "%s_%03d0" % (base, i + 1))
            for i, old in enumerate(names_in_order)]


def _primary_hub_seq(instances, hub_name=""):
    """The ONE hub sequence the audit judges against: the exact settings name
    if present, else the hub sequence with the most segments. Derived hubs
    ('Sources Sequence_publish') are hub for scan purposes but must not count
    as forks or coverage (on box, every shot showed a phantom 2-fork
    because the publish copy doubled the hub instances)."""
    counts = {}
    for d in instances:
        if d.get("is_hub"):
            counts[d["seq"]] = counts.get(d["seq"], 0) + 1
    if not counts:
        return None
    hn = (hub_name or "").strip().lower()
    for s in counts:
        if s.strip().lower() == hn:
            return s
    return max(counts, key=lambda s: counts[s])


def hub_audit(instances, groups, hub_name="", media_ends=None):
    """Read-only Sources Sequence health — the detection side of the hub
    build, judged against the PRIMARY hub sequence only. Per logical
    shot: is it in the (primary) hub, fork stacks (2+ instances IN the primary
    hub — an intentional alt-shot convention), does the hub cover
    every frame the consumers DISPLAY (use_frames — timewarps by their curve,
    checked like any cut; only a timewarp whose curve can't be
    read goes unchecked, `n_unchecked`), how many frames the piece runs past
    the uses (`excess_head`/`excess_tail`, the "not too long" half), how many
    frames a use shows past the END OF ITS MEDIA (`held_f`/`held_idx` — a
    freeze holding the last frame, which no hub piece can hold, so it is not
    counted missing; `media_ends` = {file path: first frame past the media},
    learned by the hub build), whether every
    live piece carries a shot name (`named`; `name_ok` keeps the `_###0`
    convention check for callers that want it), and where any missing frames
    sit (`uncovered_at` head/tail/gap) plus which clean consumers are short
    (`short_idx`). Globally: record-order vs source-TC (C-mode) inversions on
    the primary hub."""
    primary = _primary_hub_seq(instances, hub_name)
    rows = []
    for gi, members in enumerate(groups):
        hub_all = [i for i in members if _is_live_hub(instances[i])
                   and instances[i].get("pub_nn") is None]   # snapshots ≠ live hub
        hub = [i for i in hub_all if instances[i]["seq"] == primary]
        cons = [i for i in members if not instances[i].get("is_hub")
                and instances[i].get("role", "source") == "source"]
        if not hub_all and not cons:
            continue
        hub_ranges = [(instances[i]["src_in"], src_end(instances[i])) for i in hub
                      if instances[i]["src_in"] is not None
                      and instances[i]["src_out"] is not None]
        frames = {i: use_frames(instances[i]) for i in cons}
        checked = [i for i in cons if frames[i] and frames[i][2] != "static"]
        # frames shown past the end of the media are HELD (Flame repeats the
        # last frame), so clip each use there before judging coverage
        held_f, held_idx, use_rng = 0, [], {}
        for i in checked:
            a, b = frames[i][:2]
            end = (media_ends or {}).get(instances[i].get("file_path") or "")
            if end is not None and b > end:
                held_f = max(held_f, b - max(a, end))
                held_idx.append(i)
                b = max(a, end)
            if b > a:
                use_rng[i] = (a, b)
        use_ranges = list(use_rng.values())
        uncovered = subtract_ranges(use_ranges, hub_ranges) if hub else []
        name = (instances[hub[0]].get("shot")
                or instances[hub[0]].get("name", "")) if hub else ""
        # The SHOT NAME column shows only a real shot name. Falling back to
        # the segment name there made unnamed shots look named;
        # the segment name identifies the row in its own column.
        shots = [(instances[i].get("shot") or "").strip() for i in hub]
        seg_name = ""
        for i in (hub or cons or hub_all):
            seg_name = (instances[i].get("name")
                        or instances[i].get("src_name") or "").strip()
            if seg_name:
                break
        # where each missing range sits relative to the hub piece(s), and
        # which clean consumers are short — the Coverage tooltip's evidence
        h_lo = min((a for a, _b in hub_ranges), default=None)
        h_hi = max((b for _a, b in hub_ranges), default=None)
        uncovered_at = ["head" if h_lo is not None and b <= h_lo else
                        "tail" if h_hi is not None and a >= h_hi else "gap"
                        for a, b in uncovered]
        short_idx = [i for i in checked if i in use_rng
                     and subtract_ranges([use_rng[i]], hub_ranges)] if hub else []
        # the "not too long" half: frames the piece holds before the earliest
        # or after the latest use — only meaningful when every use is known
        excess_head = excess_tail = 0
        if hub_ranges and use_ranges and len(checked) == len(cons):
            excess_head = max(0, min(a for a, _b in use_ranges) - h_lo)
            excess_tail = max(0, h_hi - max(b for _a, b in use_ranges))
        rows.append({
            "group": gi,
            "camera": instances[members[0]].get("camera") or "?",
            "in_hub": bool(hub),
            "n_hub": len(hub),                       # forks: primary hub only
            "n_hub_other": len(hub_all) - len(hub),  # e.g. the _publish copy
            "n_consumers": len(cons),
            "n_warp": sum(1 for i in cons if instances[i].get("timewarp")),
            "n_unchecked": len(cons) - len(checked),
            "excess_head": excess_head,
            "excess_tail": excess_tail,
            "held_f": held_f,
            "held_idx": held_idx,
            "hub_name": name,
            "name_ok": hub_shot_name_ok(name) if hub else False,
            "shot_name": next((s for s in shots if s), ""),
            "named": bool(shots) and all(shots),
            "n_unnamed": sum(1 for s in shots if not s),
            "seg_name": seg_name,
            "hub_idx": list(hub),
            "short_idx": short_idx,
            "uncovered": uncovered,
            "uncovered_at": uncovered_at,
            "unused": bool(hub) and not cons,
        })
    hub_rows = sorted((d for d in instances
                       if _is_live_hub(d) and d["seq"] == primary
                       and d.get("pub_nn") is None
                       and d.get("rec_in_f") is not None
                       and d.get("src_in") is not None),
                      key=lambda d: d["rec_in_f"])
    inversions = sum(1 for x in range(len(hub_rows))
                     for y in range(x + 1, len(hub_rows))
                     if hub_rows[x]["src_in"] > hub_rows[y]["src_in"])
    return {"rows": rows,
            "primary": primary,
            "missing": [r for r in rows if not r["in_hub"] and r["n_consumers"]],
            "unused": [r for r in rows if r["unused"]],
            "inversions": inversions,
            "n_hub_segments": len(hub_rows)}


# ====================================================================
# Pure publish ledger + Prep Publish planning  (off-box tested)
# ====================================================================

def publish_ledger(instances, groups, openclips, hub_name=""):
    """One row per logical shot with a live segment in the primary hub: its
    snapshot history (Publish NN tracks), its output openclip (joined by
    essence uid, else by name), and version state. This is the
    at-a-glance 'every shot × what version it's up to × published or
    not' view — and the input Prep Publish plans from."""
    primary = _primary_hub_seq(instances, hub_name)
    by_ess = {}
    by_name = {}
    for k, oc in enumerate(openclips or []):
        e = (oc.get("essence_uid") or "").strip()
        if e:
            by_ess.setdefault(e, k)
        n = (oc.get("name") or "").strip().lower()
        if n:
            by_name.setdefault(n, k)
    rows = []
    for gi, members in enumerate(groups):
        live = [i for i in members if _is_live_hub(instances[i])
                and instances[i]["seq"] == primary
                and instances[i].get("pub_nn") is None]
        if not live:
            continue
        snaps = sorted({instances[i]["pub_nn"] for i in members
                        if _is_live_hub(instances[i])
                        and instances[i]["seq"] == primary
                        and instances[i].get("pub_nn") is not None})
        oc_idx = None
        for i in live:
            e = (instances[i].get("src_uid") or "").strip()
            if e and e in by_ess:
                oc_idx = by_ess[e]
                break
        if oc_idx is None:
            for i in live:
                n = (instances[i].get("name") or "").strip().lower()
                if n and n in by_name:
                    oc_idx = by_name[n]
                    break
        oc = openclips[oc_idx] if oc_idx is not None else None
        published = oc is not None
        if published:
            next_nn = oc["n_versions"]          # a NEW publish adds the next version
        elif snaps:
            next_nn = max(snaps)                # prepped but unpublished -> re-prep replaces
        else:
            next_nn = 0                         # first publish is 00, whenever it happens
        rows.append({
            "group": gi, "camera": instances[live[0]].get("camera") or "?",
            "hub_idx": live[0],
            "hub_name": (instances[live[0]].get("shot")
                         or instances[live[0]].get("name", "")),
            "n_forks": len(live),
            "snaps": snaps, "published": published,
            "clip_name": oc["name"] if oc else "",
            "version_uid": oc["version_uid"] if oc else "",
            "latest": oc["latest"] if oc else "",
            "off_latest": bool(oc and oc["off_latest"]),
            "n_versions": oc["n_versions"] if oc else 0,
            "next_nn": next_nn,
        })
    return {"rows": rows, "primary": primary}


def plan_prep_publish(rows, selected, fix_mode=False):
    """Plan the 'Publish NN' snapshot round over selected ledger rows (pure —
    nothing touches Flame until execute). Modes per the publish workflow:
      - normal: each shot snapshots into ITS OWN next version track — never-
        published shots fill Publish 00 late (sparse publishing), already-
        published shots open the next NN;
      - fix_mode: an erroneous publish is REPLACED in place (same NN as the
        current version) instead of burning a new number.
    Returns {'groups': [{nn, track, create, shots, replaces, hide}], 'warnings'}
    grouped by target track; 'hide' lists prior snapshot shots on that track
    that must be hidden before the native publish (only visible segments of
    the primary track get published); 'replaces' flags shots whose snapshot at
    that NN already exists (fix / re-prep)."""
    existing = sorted({nn for r in rows for nn in r["snaps"]})
    by_nn, warnings = {}, []
    for si in selected:
        r = rows[si]
        if fix_mode:
            if r["published"]:
                nn = max(r["n_versions"] - 1, 0)
            elif r["snaps"]:
                nn = max(r["snaps"])
            else:
                nn = 0
                warnings.append("%s: nothing to fix (never prepped) — snapshots "
                                "into Publish 00" % (r["hub_name"] or r["camera"]))
        else:
            nn = r["next_nn"]
        by_nn.setdefault(nn, []).append(si)
    groups = []
    for nn in sorted(by_nn):
        sel = by_nn[nn]
        selected_shots = [rows[si] for si in sel]
        replaces = [r["hub_name"] or r["camera"] for r in selected_shots
                    if nn in r["snaps"]]
        if replaces and not fix_mode:
            warnings.append("Publish %02d already holds: %s — prepping again "
                            "REPLACES those snapshots" % (nn, ", ".join(replaces)))
        hide = [r["hub_name"] or r["camera"] for r in rows
                if nn in r["snaps"]
                and r not in selected_shots]
        groups.append({
            "nn": nn, "track": publish_track_name(nn),
            "create": nn not in existing,
            "shots": sel, "replaces": replaces, "hide": hide,
        })
    return {"groups": groups, "warnings": warnings}


# ====================================================================
# Pure role / stack / longest analysis  (off-box tested)
# ====================================================================

def _ref_tokens(settings):
    # fallback mirrors DEFAULT_SETTINGS so a bare/None settings dict still
    # detects the obvious names (the GUI always passes merged settings)
    raw = (settings or {}).get("ref_name_tokens", "ref, reference, offline") or ""
    return [t.strip().lower() for t in raw.split(",") if t.strip()]


def classify_roles(rows, settings=None, overrides=None, track_roles=None):
    """Assign every scanned row a role: 'hub' | 'ref' | 'source' | 'graphic' |
    'fx' (in place; returns rows). Only 'source' rows feed the conform audits
    and the hub build — graphics are a different category and are managed
    separately.

    The 95/5 system: heuristics classify the bulk, `overrides` (state
    ref_overrides: seg_key -> 'ref'|'source') pin the rest. ref_conf records
    how the call was made: 'override' | 'wizard' | 'auto' | 'maybe' ('maybe'
    rows STAY role=source — ambiguity never silently drops a segment from the
    audits; the GUI shows 'ref?' and the user nudges).

    `track_roles` ({sequence: {track_id: role}}) carries the WIZARD's answers
    and beats every heuristic below it: no rule can tell a still that is a
    shot from a still that is a graphic, so on a real job the artist declares
    it once per track and CCM stops guessing. Precedence: hub > per-segment
    override > wizard track role > heuristics.

    Heuristics (workflows differ per artist -- all knobs are settings):
      - name tokens (segment / source / track name contains a ref token);
      - span: record duration >= ref_span_pct of the sequence's record extent,
        guarded by 'the sequence has 2+ other segments on other tracks' so a
        one-shot spot's only segment can't self-classify as a ref;
      - track inheritance: any track holding a confident ref pulls its other
        segments in too (catches the small legal-size / graphic-placement
        clips parked on a ref track). Overrides always win.
    """
    settings = settings or {}
    overrides = overrides or {}
    track_roles = track_roles or {}
    span_pct = float(settings.get("ref_span_pct", 60) or 60) / 100.0
    tokens = _ref_tokens(settings)

    by_seq = {}
    for r in rows:
        if r.get("is_hub"):
            # hub rows are hub — EXCEPT where the wizard has tagged the track.
            # The Conform Hub's own tracks carry meaning: the live version
            # track holds trackable clips, while other tracks archive sources
            # that were published from but appear in no timeline.
            # Those must not count as live hub coverage.
            tr = (track_roles.get(r["seq"]) or {}).get(r.get("track_id"))
            if tr in TRACK_ROLES:
                r["role"], r["ref_conf"] = tr, "wizard"
            else:
                r["role"], r["ref_conf"] = "hub", ""
            continue
        r["role"], r["ref_conf"] = "source", ""
        by_seq.setdefault(r["seq"], []).append(r)

    for seq_rows in by_seq.values():
        troles = track_roles.get(seq_rows[0]["seq"]) or {}
        rec = [(r["rec_in_f"], r["rec_in_f"] + r["rec_dur_f"]) for r in seq_rows
               if r.get("rec_in_f") is not None and r.get("rec_dur_f")]
        lo = min((a for a, _b in rec), default=None)
        hi = max((b for _a, b in rec), default=None)
        span_total = (hi - lo) if (lo is not None and hi is not None and hi > lo) else None
        for r in seq_rows:
            # a per-SEGMENT answer beats everything: a slate sitting in line
            # with the programme on a shared track can only be named one
            # segment at a time
            ov = overrides.get(r.get("seg_key"))
            if ov in TRACK_ROLES:
                r["role"], r["ref_conf"] = ov, "override"
                continue
            tr = troles.get(r.get("track_id"))
            if tr in TRACK_ROLES:
                r["role"], r["ref_conf"] = tr, "wizard"
                continue
            names = " ".join([str(r.get("name") or ""), str(r.get("src_name") or ""),
                              str(r.get("track_name") or "")]).lower()
            if tokens and any(t in names for t in tokens):
                r["role"], r["ref_conf"] = "ref", "auto"
                continue
            if span_total and r.get("rec_dur_f"):
                span = r["rec_dur_f"] / float(span_total)
                others = sum(1 for o in seq_rows
                             if o is not r and o.get("track_id") != r.get("track_id"))
                if span >= span_pct:
                    if others >= 2:
                        r["role"], r["ref_conf"] = "ref", "auto"
                    else:
                        r["ref_conf"] = "maybe"       # stays a source; GUI shows 'ref?'
                elif span >= span_pct * 0.75 and others >= 2:
                    r["ref_conf"] = "maybe"

        # track inheritance: a track that holds a confident ref is a ref track
        ref_tracks = {r.get("track_id") for r in seq_rows
                      if r["role"] == "ref"
                      and r["ref_conf"] in ("auto", "override", "wizard")}
        for r in seq_rows:
            if r["role"] != "ref" and r["ref_conf"] not in ("override", "wizard") \
                    and r.get("track_id") in ref_tracks:
                r["role"], r["ref_conf"] = "ref", "auto"
    return rows


def ref_report(rows):
    """Per-sequence reference-picture health checks over classified rows.
    Returns display strings: no ref present (nothing to conform against), ref
    extent differing from the conform extent (the cut moved since editorial
    exported), and N>1 refs as info (picture ref + burn-in TC ref is a
    legitimate pair, not an error)."""
    by_seq = {}
    for r in rows:
        if not r.get("is_hub"):
            by_seq.setdefault(r["seq"], []).append(r)
    out = []
    for seq in sorted(by_seq):
        seq_rows = by_seq[seq]
        refs = [r for r in seq_rows if r["role"] == "ref"]
        srcs = [r for r in seq_rows if r["role"] == "source"]
        if not refs:
            if len(srcs) >= 2:
                out.append("⚠ %s: no reference picture detected" % seq)
            continue
        if len(refs) > 1:
            out.append("ℹ %s: %d refs present (picture + burn-in is normal)"
                       % (seq, len(refs)))
        def extent(rr):
            pts = [(r["rec_in_f"], r["rec_in_f"] + r["rec_dur_f"]) for r in rr
                   if r.get("rec_in_f") is not None and r.get("rec_dur_f")]
            return (min(a for a, _ in pts), max(b for _, b in pts)) if pts else None
        re_, se = extent(refs), extent(srcs)
        if re_ and se and re_ != se:
            out.append("⚠ %s: ref extent %d–%d ≠ conform extent %d–%d "
                       "(cut changed since ref export?)"
                       % (seq, re_[0], re_[1] - 1, se[0], se[1] - 1))
    return out


def stack_groups(rows):
    """Split-screen / multi-element audit: groups of consumer (role=source,
    non-hub) rows in the SAME sequence whose record ranges overlap across
    DIFFERENT tracks (Flame can't overlap within a track). Union-find per
    sequence; returns lists of indices into `rows`, 2+ members each. Refs are
    excluded upstream by role — that exclusion is what keeps every sequence
    from looking 'stacked' under its full-length ref."""
    idxs = [i for i, r in enumerate(rows)
            if not r.get("is_hub") and r.get("role") == "source"
            and r.get("rec_in_f") is not None and r.get("rec_dur_f")]
    by_seq = {}
    for i in idxs:
        by_seq.setdefault(rows[i]["seq"], []).append(i)
    parent = {i: i for i in idxs}

    def find(x):
        while parent[x] != x:
            parent[x] = parent[parent[x]]
            x = parent[x]
        return x

    for members in by_seq.values():
        for ai in range(len(members)):
            a = rows[members[ai]]
            a_in, a_out = a["rec_in_f"], a["rec_in_f"] + a["rec_dur_f"]
            for bi in range(ai + 1, len(members)):
                b = rows[members[bi]]
                if a.get("track_id") == b.get("track_id"):
                    continue
                if _ranges_overlap(a_in, a_out, b["rec_in_f"],
                                   b["rec_in_f"] + b["rec_dur_f"]):
                    ra, rb = find(members[ai]), find(members[bi])
                    if ra != rb:
                        parent[ra] = rb
    comps = {}
    for i in idxs:
        comps.setdefault(find(i), []).append(i)
    return sorted([sorted(m) for m in comps.values() if len(m) > 1])


def longest_members(rows, groups):
    """Per logical shot (each group = list of indices into rows), the consumer
    instance with the longest source duration — the 'longest version of each
    shot'. Only flagged when a shot has 2+ consumer instances (a singleton being
    'longest' is noise). Returns a set of row indices."""
    out = set()
    for members in groups:
        cons = [i for i in members
                if not rows[i].get("is_hub") and rows[i].get("role") == "source"
                and rows[i].get("src_dur_f") is not None]
        if len(cons) >= 2:
            out.add(max(cons, key=lambda i: rows[i]["src_dur_f"]))
    return out


def annotate_dup_sets(instances, groups, decisions, sanctioned):
    """Attach persisted triage decisions to dup groups (members are indices into
    `instances`). Returns rows: {tier, members, key, decision, sanctioned}."""
    sset = set(sanctioned or [])
    rows = []
    for g in groups:
        key = conflict_key(instances, g["members"])
        rows.append({"tier": g["tier"], "members": g["members"], "key": key,
                     "decision": (decisions or {}).get(key, ""),
                     "sanctioned": key in sset})
    return rows


def shared_components(members, shared_of):
    """Partition a duplicate set's members by Flame's UNDER-THE-HOOD source
    sharing: every import event mints a unique real
    source, so the same shot conformed on different days — or delivered
    repeatedly by Resolve prep — exists as SPLIT sources that drift apart
    silently. `shared_of[m]` = the members m shares a real source with
    (from seg.shared_source_segments(), the scriptable twin of right-click
    'Jump to Shared Source Segment', mapped back to scan rows).

    Returns components sorted largest-first (ties: lowest member index).
    ONE component = intended reuse of one real source (good); MORE = split
    sources, and everything outside the first component is a stray copy to
    merge. The relation is made symmetric — a one-sided API read still
    binds the pair."""
    idx = {m: k for k, m in enumerate(members)}
    parent = list(range(len(members)))

    def find(x):
        while parent[x] != x:
            parent[x] = parent[parent[x]]
            x = parent[x]
        return x

    def union(a, b):
        ra, rb = find(a), find(b)
        if ra != rb:
            parent[rb] = ra

    for m in members:
        for other in (shared_of.get(m) or ()):
            if other in idx and other != m:
                union(idx[m], idx[other])
    comps = {}
    for m in members:
        comps.setdefault(find(idx[m]), []).append(m)
    return sorted(comps.values(), key=lambda c: (-len(c), c[0]))


def media_extent(rows, idx):
    """(first, last) source frames, inclusive, that the real source's MEDIA
    holds, from straight uses: first = in - head, last = out + tail (Flame's
    head/tail = frames of media outside the cut). A retime's head/tail
    measure its own footprint, not its cut, so only straight cuts count.
    None when no use gives all four numbers."""
    lo = hi = None
    for i in idx:
        d = rows[i]
        if d.get("timewarp"):
            continue
        a, b, h, t = (d.get("src_in"), d.get("src_out"),
                      d.get("head_f"), d.get("tail_f"))
        if None in (a, b, h, t) or h < 0 or t < 0:
            continue
        lo = a - h if lo is None else min(lo, a - h)
        hi = b + t if hi is None else max(hi, b + t)
    return None if lo is None else (lo, hi)


def pick_merge_master(rows, comp, stray, extra=()):
    """Which use of the primary source a split stray is merged onto.

    The replacement clip is copied from ONE use, so that use's source range
    must contain the stray's. A block's uses often sit on different windows
    of one take — a :60 and a :15 cut of the same shot are not nested — so
    the widest use is not necessarily one that covers: another use of the
    same real source may cover the stray frame for frame.

    Candidates are the members of the primary component `comp` with a
    readable source range. Among those that cover the stray: a straight cut
    before a retimed one (a retime's in/out is only its record length laid
    on the source, not the frames it shows — see use_frames), then the
    widest, then the first in `comp` order. So wherever the widest use is a
    straight cut that covers, the pick is simply that widest use;
    a retimed widest use yields to a straight one that covers.

    A retimed stray whose curve was read must be covered over the frames it
    DISPLAYS as well as its own in/out (a speed-up shows frames past its
    out point).

    `extra` = live Conform Hub pieces that share the primary source. A hub
    piece is built to the union of every use's visible frames, so it covers
    a stray no single spot covers — typically the widest (often retimed)
    use, whose window no other cut contains. Spots are tried first; a hub
    piece only when no spot covers.

    Last, the SOURCE itself: smart_replace_media takes the frames a stray
    needs from the whole media, past the copy's cut (on box, a stray
    needing 2 frames past the master's cut kept its
    range, length and Timewarp). So when the primary source's media
    (media_extent) contains the stray's frames, the widest spot is the
    master even though its own cut doesn't cover — no hub needed.

    Returns (master, None); (None, closest) when nothing covers the stray,
    closest being the candidate that overlaps it most (for the skip
    message); (None, None) when no candidate has a readable range. A stray
    whose own range can't be read gets the widest spot, unchecked."""
    def readable(idx):
        return [i for i in idx if rows[i].get("src_in") is not None
                and rows[i].get("src_out") is not None]

    cands, hubs = readable(comp), readable(extra)
    if not cands and not hubs:
        return None, None

    def width(i):
        return rows[i]["src_out"] - rows[i]["src_in"]

    s_in, s_out = rows[stray].get("src_in"), rows[stray].get("src_out")
    if s_in is None or s_out is None:
        return max(cands or hubs, key=width), None
    shown = use_frames(rows[stray])
    if shown and shown[2] == "curve":           # half-open: last shown = end - 1
        s_in, s_out = min(s_in, shown[0]), max(s_out, shown[1] - 1)
    for pool in (cands, hubs):
        cover = [i for i in pool
                 if rows[i]["src_in"] <= s_in and s_out <= rows[i]["src_out"]]
        if cover:
            straight = [i for i in cover if not rows[i].get("timewarp")]
            return max(straight or cover, key=width), None
    media = media_extent(rows, comp)
    if cands and media and media[0] <= s_in and s_out <= media[1]:
        straight = [i for i in cands if not rows[i].get("timewarp")]
        return max(straight or cands, key=width), None
    return None, max(cands + hubs, key=lambda i: (
        min(s_out, rows[i]["src_out"]) - max(s_in, rows[i]["src_in"])))


def key_index(keys):
    """(by_key, by_base) for a list of match keys: index lists per exact key
    and per key WITHOUT its track part (the identity before 0.9.7)."""
    by_key, by_base = {}, {}
    for i, k in enumerate(keys):
        if k:
            by_key.setdefault(k, []).append(i)
            by_base.setdefault(k.rsplit("|", 1)[0], []).append(i)
    return by_key, by_base


def match_rows(k, by_key, by_base):
    """Rows a key computed from a segment RETURNED BY FLAME belongs to: the
    exact key (track included) first; else every row sharing the key minus
    its track — the matching CCM used before the track was added. The track
    part comes from comparing Flame's wrapper objects, verified on one build
    only: if a build ever reads it on one side but not the other, a
    shared-source read must still find its rows (missing them would read
    every block as split), never do worse than before."""
    if not k:
        return ()
    return by_key.get(k) or by_base.get(k.rsplit("|", 1)[0], ())


def rescan_list(prev, extra=(), alive=None):
    """The sequences a rescan after one of CCM's own changes reads: the ones
    the last scan read, in the same order, then any the change created
    (`extra`), each once, minus any `alive` says are gone. Re-resolving the
    scope instead would anchor on whatever the Timeline shows at that moment,
    and a change's own temp copy/delete in the Media Panel moves the Timeline
    — the rescan could land on an empty scope and blank every tab while the
    project itself was fine."""
    out, seen = [], set()
    for s in list(prev or ()) + list(extra or ()):
        if s is None or id(s) in seen:
            continue
        if alive is not None and not alive(s):
            continue
        seen.add(id(s))
        out.append(s)
    return out


def plan_shot_stack(pieces):
    """Lay out ONE logical shot that exists as several real sources (grades /
    re-imports) as a stack: the LONGEST piece
    sits on the base track in line with every other shot; the others stack on
    tracks above, aligned to it by SOURCE TIMECODE — so a viewer reads the
    same source frame straight up the stack — and the shot reserves enough
    room that a stacked piece never overlaps its neighbours (that reserved
    room is the gap the user sees before/after the base piece).

    `pieces` = [(src_in, src_out), …], one per distinct real source, already
    union-sized. Returns {'footprint': total record frames the shot occupies,
    'base': index of the base piece, 'placements': [{'i', 'track', 'start'}]}
    with `start` measured from the shot's first record frame. Degenerate or
    None-ranged pieces are dropped."""
    good = [(i, a, b) for i, (a, b) in enumerate(pieces)
            if a is not None and b is not None and b > a]
    if not good:
        return {"footprint": 0, "base": None, "placements": []}
    # base = longest; ties resolve to the earliest source in, then input order
    bi, ba, bb = max(good, key=lambda t: (t[2] - t[1], -t[1], -t[0]))
    offs = {i: a - ba for i, a, b in good}
    lead = max(0, -min(offs.values()))
    placed = []                      # per track: list of (start, end)
    out = []
    # base first (owns track 0), then the rest earliest-first so the stack
    # reads left to right
    order = [(bi, ba, bb)] + sorted((t for t in good if t[0] != bi),
                                    key=lambda t: (t[1], t[0]))
    for i, a, b in order:
        start = lead + offs[i]
        end = start + (b - a)
        if i == bi:
            trk = 0
        else:
            trk = 1
            while True:
                while len(placed) <= trk:
                    placed.append([])
                if trk > 0 and all(end <= s or start >= e
                                   for s, e in placed[trk]):
                    break
                trk += 1
        while len(placed) <= trk:
            placed.append([])
        placed[trk].append((start, end))
        out.append({"i": i, "track": trk, "start": start})
    out.sort(key=lambda p: (p["track"], p["start"]))
    footprint = max(p["start"] + (pieces[p["i"]][1] - pieces[p["i"]][0])
                    for p in out)
    return {"footprint": footprint, "base": bi, "placements": out}


def mean_abs_delta(a, b):
    """Mean absolute difference between two equal-length channel-value
    sequences (0-255), as a percentage of full scale. The grade-compare
    statistic: JPEG-thumbnail noise lands near zero, a real grade shift does
    not. Returns None when the inputs can't be compared."""
    if not a or not b or len(a) != len(b):
        return None
    return sum(abs(x - y) for x, y in zip(a, b)) / float(len(a)) / 2.55


def grade_verdict(delta_pct):
    """Plain-language reading of mean_abs_delta. Thresholds are deliberately
    conservative — CCM never decides a grade question, it only says where to
    look. Tune from real footage as data arrives."""
    if delta_pct is None:
        return "not comparable"
    if delta_pct < 0.5:
        return "identical"
    if delta_pct < 2.0:
        return "near-identical (compression noise)"
    return "GRADE DIFFERS"


def anonymize_probe(text, rows=None):
    """Scrub a probe dump so a high-security job can still send one
    for support. Client identity lives in three places: media PATHS,
    SEQUENCE names and SOURCE/segment names. Each distinct value is replaced
    by a stable placeholder — SEQ1, SRC1, /anon/path1 — so the structure a
    probe exists to show (API signatures, counts, relationships) survives
    intact and the same name reads the same everywhere in the file.

    Returns (scrubbed text, number of distinct identifiers replaced)."""
    names, paths = set(), set()
    for r in (rows or []):
        for key in ("seq", "name", "src_name", "shot"):
            v = str(r.get(key) or "").strip()
            if len(v) >= 4:
                names.add(v)
        p = str(r.get("file_path") or "").strip()
        if p:
            paths.add(p)
    # paths first (they contain names), longest first so a prefix can't win
    n = 0
    for i, p in enumerate(sorted(paths, key=len, reverse=True), 1):
        if p in text:
            text = text.replace(p, "/anon/path%d%s" % (i, os.path.splitext(p)[1]))
            n += 1
    # any surviving absolute path, whoever produced it
    text, k = re.subn(r"(/(?:Users|Volumes|mnt|net|Jobs|jobs)/[^\s'\"]+)",
                      "/anon/path", text)
    n += k
    for i, nm in enumerate(sorted(names, key=len, reverse=True), 1):
        if nm in text:
            text = text.replace(nm, "NAME%d" % i)
            n += 1
    # Belt and braces: these probe lines carry a bare client name whatever
    # the scan happened to contain (a media-panel clip is not a scan row —
    # that is exactly how one can leak through). Scrub by SHAPE.
    def _line(m):
        val = (m.group(2) or "").strip().strip("'\"")
        # keep trivia the probe exists to report ('*', None, ''): a client
        # name is never three characters
        if len(val) <= 3 or val.lower() in ("none", "n/a", "(absent)"):
            return m.group(0)
        return m.group(1) + "REDACTED" + (m.group(3) or "")
    for pat in (r"^(clip: )(.+?)( \(Py\w+\))$",
                r"^(segment: )(.*?)()$",
                r"^(seg\.source_name: )(.*?)()$",
                r"^(seg\.shot_name: )(.*?)(\s+\(via get_value\))?$",
                r"^(track\.name: )(.*?)(\s+\(via get_value\))?$"):
        text, k = re.subn(pat, _line, text, flags=re.M)
        n += k
    return text, n


def mark_slates(rows, fps=24.0, lead_seconds=60):
    """Flag pre-roll slates by STRUCTURE, not by name.

    Broadcast sequences start on an hour — 01:00:00:00, 02:00:00:00 — and a
    slate sits in the handful of seconds before it (00:59:53:00 and friends).
    So: per sequence, round the first record frame UP to the next hour; if
    the sequence begins before that hour by less than `lead_seconds`, every
    segment starting before the hour is pre-roll. A sequence that genuinely
    starts at 00:00:00:00 rounds to itself and is left alone.

    Marks role='slate' (ref_conf='auto') in place; never overrides an
    explicit wizard/override call. Returns the count marked."""
    hour = int(round(float(fps or 24.0) * 3600))
    if hour <= 0:
        return 0
    by_seq = {}
    for r in rows:
        if r.get("is_hub") or r.get("rec_in_f") is None:
            continue
        by_seq.setdefault(r["seq"], []).append(r)
    n = 0
    for seq_rows in by_seq.values():
        lo = min(r["rec_in_f"] for r in seq_rows)
        boundary = -(-lo // hour) * hour          # round up to the next hour
        lead = boundary - lo
        if lead <= 0 or lead > lead_seconds * float(fps or 24.0):
            continue
        for r in seq_rows:
            if r.get("ref_conf") in ("override", "wizard"):
                continue
            if r["rec_in_f"] < boundary:
                r["role"], r["ref_conf"] = "slate", "auto"
                n += 1
    return n


def collapse_uses(rows, members):
    """Fold a shot's consumer instances that use the IDENTICAL source range
    into one entry. The point of the per-timeline lanes is to reveal where
    durations DIFFER; on a 52-sequence job a shot cut the same way in 20
    spots drew 20 identical lanes and buried the one that mattered.
    Returns [{'rep', 'members', 'seqs', 'n'}] ordered by source
    in-point, so the odd one out stands out instead of scrolling away."""
    by_range = {}
    for i in members:
        d = rows[i]
        if d.get("is_hub") or d.get("role") != "source":
            continue
        key = (d.get("src_in"), d.get("src_out"), bool(d.get("timewarp")))
        by_range.setdefault(key, []).append(i)
    out = []
    for key, idxs in by_range.items():
        seqs = []
        for i in idxs:
            s = rows[i].get("seq")
            if s not in seqs:
                seqs.append(s)
        out.append({"rep": idxs[0], "members": idxs, "seqs": sorted(seqs),
                    "n": len(idxs),
                    # stable across renders so an opened group stays open
                    "key": "%s|%s|%s" % key})
    out.sort(key=lambda g: (rows[g["rep"]].get("src_in") is None,
                            rows[g["rep"]].get("src_in") or 0,
                            rows[g["rep"]].get("seq") or ""))
    return out


def record_overlap_groups(items):
    """Group hub pieces that belong to ONE shot. `items` = [(track_id,
    rec_in, rec_dur), …]; any two entries on DIFFERENT tracks whose record
    ranges overlap are the same shot (that is exactly what stacking means —
    a piece sitting above another piece of the same shot), unioned
    transitively. Same-track neighbours are separate shots however close
    they sit. Returns groups of indices, each sorted, ordered by earliest
    record position — i.e. the order Renumber walks."""
    n = len(items)
    parent = list(range(n))

    def find(x):
        while parent[x] != x:
            parent[x] = parent[parent[x]]
            x = parent[x]
        return x

    def union(a, b):
        ra, rb = find(a), find(b)
        if ra != rb:
            parent[rb] = ra

    for i in range(n):
        ti, ai, di = items[i]
        for j in range(i + 1, n):
            tj, aj, dj = items[j]
            if ti == tj or None in (ai, aj):
                continue
            if _ranges_overlap(ai, ai + (di or 0), aj, aj + (dj or 0)):
                union(i, j)
    comps = {}
    for i in range(n):
        comps.setdefault(find(i), []).append(i)
    out = [sorted(v) for v in comps.values()]
    out.sort(key=lambda g: min((items[i][1] if items[i][1] is not None
                                else 0) for i in g))
    return out


def hub_orphans(rows, shared_of):
    """Consumer instances whose REAL SOURCE has no representative in the hub —
    the segment plays from media the Sources Sequence never accounts for, so
    every hub-driven operation (publish, relink, version bump) silently skips
    it. On real jobs, orphan segments scattered through several sequences
    are one of the most damaging conform problems.

    Identity here is SOURCE SHARING, not logical shot identity: two grades of
    the same shot are one logical shot but two real sources, and the hub owes
    each of them an entry (Flame's native builder makes two). `shared_of[i]`
    = row indices sharing i's real source, hub rows included. Refs are never
    orphans (they represent the cut, not a source)."""
    out = []
    for i, d in enumerate(rows):
        if d.get("is_hub") or d.get("role") != "source":
            continue
        if not any(_is_live_hub(rows[j]) for j in (shared_of.get(i) or ())):
            out.append(i)
    return out


# ====================================================================
# Pure Timelines-tab layout  (off-box tested)
# ====================================================================

def _track_sort_key(track_id):
    """'v0.t1' -> (0, 1). Junk sorts first rather than raising — track_id is
    positional and scan-generated, so anything else means a synthetic row."""
    m = re.match(r"v(\d+)\.t(\d+)$", str(track_id or ""))
    return (int(m.group(1)), int(m.group(2))) if m else (-1, -1)


def track_label(track_id, track_name=""):
    """Flame's own track naming: V<version>.<track> — V4.2 is version 4,
    track 2. `track.name` reads '*' on unnamed tracks (on box), so
    the positional id is the honest source of a readable label; a real name,
    when the artist set one, is appended."""
    m = re.match(r"v(\d+)\.t(\d+)$", str(track_id or ""))
    base = ("V%d.%d" % (int(m.group(1)) + 1, int(m.group(2)) + 1)) if m \
        else str(track_id or "?")
    nm = str(track_name or "").strip().strip("'\"")
    if nm and nm not in ("*", "None"):
        return "%s  %s" % (base, nm)
    return base


def timeline_lanes(rows, include_refs=False, hidden_roles=(), include_hub=True):
    """The Timelines-tab layout, computed from scan rows alone: one lane per
    sequence with hub lane(s) PINNED FIRST (then A→Z), ALL tracks stacked
    inside each lane (highest track on top, like the Flame timeline), blocks
    as row indices sorted by record position. Every lane is normalized to its
    own record start; `max_dur` is the longest lane's span — the shared scale:
    that span maps to the full widget width and every other lane falls
    proportionally short (ONE scale, never per-lane fit).
    Rows without a record position are skipped (they can't be placed).
    References are HIDDEN unless include_refs — a big ref stack is in the
    way far more often than it is wanted; a track or
    lane that held only refs disappears with them. `hidden_roles` drops more
    categories the same way — Compact view passes graphics and FX, which on
    a real 52-sequence job is most of the clutter. include_hub=False drops
    the hub lane(s) entirely: the hub holds every shot end to end, so it is
    usually the longest lane and squeezes every spot —
    without it the longest SPOT sets the scale and the rest stretch."""
    hidden = set(hidden_roles or ())
    if not include_refs:
        hidden.add("ref")
    by_seq = {}
    for i, d in enumerate(rows):
        if d.get("rec_in_f") is None:
            continue
        if d.get("role") in hidden:
            continue
        if not include_hub and d.get("is_hub"):
            continue
        by_seq.setdefault(d["seq"], []).append(i)
    lanes = []
    for seq, idxs in by_seq.items():
        is_hub = any(rows[i].get("is_hub") for i in idxs)
        lo = min(rows[i]["rec_in_f"] for i in idxs)
        hi = max(rows[i]["rec_in_f"] + (rows[i].get("rec_dur_f") or 0)
                 for i in idxs)
        tracks = {}
        for i in idxs:
            tid = rows[i].get("track_id") or ""
            tracks.setdefault(tid, {
                "track_id": tid,
                "track_name": rows[i].get("track_name") or "",
                "label": track_label(tid, rows[i].get("track_name")),
                # version index drives the visual grouping: tracks inside one
                # version stack tight, versions are separated
                "version": _track_sort_key(tid)[0],
                "blocks": []})
            tracks[tid]["blocks"].append(i)
        ordered = sorted(tracks.values(),
                         key=lambda t: _track_sort_key(t["track_id"]),
                         reverse=True)
        for t in ordered:
            t["blocks"].sort(key=lambda i: rows[i]["rec_in_f"])
        lanes.append({"seq": seq, "is_hub": is_hub, "lo": lo, "hi": hi,
                      "dur": hi - lo, "tracks": ordered})
    lanes.sort(key=lambda ln: (not ln["is_hub"], ln["seq"].lower()))
    return {"lanes": lanes,
            "max_dur": max((ln["dur"] for ln in lanes), default=0)}


# ====================================================================
# Pure Connections-tab layout  (off-box tested)
# ====================================================================

def link_map(groups):
    """{row: set(rows it is linked to)} from groups of mutually linked rows —
    Flame returns a connected set (or a shared-source set) as one list that
    includes the segment itself, so every member links to every other."""
    out = {}
    for g in groups:
        g = set(g)
        for i in g:
            out.setdefault(i, set()).update(g - {i})
    return out


def web_nodes(rows, members, peer_maps=(), primary=None):
    """The nodes of one shot's Connections family tree: (centres, ring).
    Centres = the shot's live piece(s) in the PRIMARY Conform Hub — the top
    of the tree. Ring = every other segment of the shot plus anything
    linked to it through `peer_maps` (connected / shared-source maps), each
    segment its OWN node (no folding), ordered by sequence then track
    then record position so a spot's aspect versions sit together; other hub
    copies go last."""
    base = [i for i in members
            if rows[i].get("is_hub") or rows[i].get("role") == "source"]
    centres = sorted((i for i in base if _is_live_hub(rows[i])
                      and rows[i].get("pub_nn") is None
                      and (primary is None or rows[i].get("seq") == primary)),
                     key=lambda i: (_track_sort_key(rows[i].get("track_id")),
                                    rows[i].get("rec_in_f") or 0))
    linked = set(base)
    for pm in peer_maps:
        for i in base:
            linked.update(pm.get(i) or ())
    cset = set(centres)
    ring = sorted((i for i in linked if i not in cset),
                  key=lambda i: (bool(rows[i].get("is_hub")),
                                 (rows[i].get("seq") or "").lower(),
                                 _track_sort_key(rows[i].get("track_id")),
                                 rows[i].get("rec_in_f") or 0, i))
    return centres, ring


def link_state(i, hubs, conn, orphans=()):
    """One segment's relation to its shot's live hub piece(s) — the colour
    language the braid and the family tree share: 'c' connected, 's' shares
    the hub's source but is NOT connected, 'o' not in the hub (its source has
    no hub piece, or the shot has none), 'u' connections not read yet.
    `conn` = {row: set(rows)}, None = not read."""
    if not hubs or i in orphans:
        return "o"
    if conn is None:
        return "u"
    return "c" if (conn.get(i) or set()) & set(hubs) else "s"


def seq_spans(rows):
    """{sequence: (record span in frames, frame rate)} — how long each
    sequence runs, first segment to last. Pre-roll slates are left out so a
    slate doesn't make a :60 read 1:10. The rate is the sequence's own when
    the scan read it (`seq_rate`), else the segment's — on a mixed-rate job
    the segment rate is the SOURCE rate, which would mislabel the length."""
    ext = {}
    for d in rows:
        if d.get("rec_in_f") is None or d.get("role") == "slate":
            continue
        a = d["rec_in_f"]
        b = a + (d.get("rec_dur_f") or 0)
        lo, hi, rate = ext.get(d["seq"], (a, b, None))
        ext[d["seq"]] = (min(lo, a), max(hi, b),
                         d.get("seq_rate") or rate or d.get("rate"))
    return {s: (hi - lo, rate or 24.0) for s, (lo, hi, rate) in ext.items()}


def tree_tiers(rows, ring, spans=None):
    """The Connections family tree's generations: the hub
    piece on top, then the shot's other segments grouped by how long their
    sequence runs, LONGEST FIRST — a :60's uses sit above the :15's, which
    sit above the :06's. Sequences of one length (a spot's aspect versions)
    share a generation; hub copies (other hubs, Publish snapshots) come last.
    Returns [{'label', 'secs', 'nodes'}], nodes kept in the ring's order."""
    spans = seq_spans(rows) if spans is None else spans
    by_secs, copies = {}, []
    for i in ring:
        d = rows[i]
        if d.get("is_hub"):
            copies.append(i)
            continue
        frames, rate = spans.get(d.get("seq"), (0, 24.0))
        secs = int(round(frames / float(rate or 24.0)))
        by_secs.setdefault(secs, []).append(i)
    out = [{"label": "%d:%02d" % divmod(s, 60), "secs": s, "nodes": by_secs[s]}
           for s in sorted(by_secs, reverse=True)]
    if copies:
        out.append({"label": "hub copies", "secs": None, "nodes": copies})
    return out


def tree_layout(tiers, n_centres, width, node_w=150, node_h=34, centre_w=230,
                centre_h=38, margin=10, label_w=58, hgap=10, bus=14, row_gap=8,
                tier_gap=10):
    """Geometry for the family tree, fitted to the panel WIDTH — it wraps and
    never scrolls sideways. The hub piece(s) sit on top; a trunk runs down
    the left from the first one; every row of segments hangs off the trunk
    on its own bus line, with its generation's length in the left column.
    Wrapping rows keep the trunk in the clear because it never crosses a box.
    Returns {'w', 'h', 'trunk_x', 'trunk_top', 'trunk_bottom', 'centres':
    [(cx, cy)], 'rows': [{'tier', 'label', 'bus_y', 'nodes': [(i, cx, cy)]}],
    'per_row'}."""
    trunk_x = margin + label_w
    x0 = trunk_x + 12
    width = max(width, x0 + node_w + margin)
    per_row = max(1, int((width - x0 - margin + hgap) // (node_w + hgap)))
    n_c = max(1, n_centres)
    cy = margin + centre_h / 2.0
    left = trunk_x - 14
    centres = [(left + centre_w / 2.0 + k * (centre_w + hgap), cy)
               for k in range(n_c)]
    y = margin + centre_h
    rows_out = []
    for t, tier in enumerate(tiers):
        nodes = list(tier["nodes"])
        for r in range(0, len(nodes), per_row) if nodes else ():
            chunk = nodes[r:r + per_row]
            bus_y = y + bus / 2.0 + (tier_gap if r == 0 else 0)
            top = bus_y + bus / 2.0 + 2
            rows_out.append({
                "tier": t, "label": tier["label"] if r == 0 else "",
                "bus_y": bus_y,
                "nodes": [(i, x0 + k * (node_w + hgap) + node_w / 2.0,
                           top + node_h / 2.0) for k, i in enumerate(chunk)]})
            y = top + node_h + row_gap
    h = int(y + margin) if rows_out else int(margin * 2 + centre_h)
    return {"w": int(width), "h": h, "trunk_x": trunk_x,
            "trunk_top": margin + centre_h,
            "trunk_bottom": rows_out[-1]["bus_y"] if rows_out else margin + centre_h,
            "centres": centres, "rows": rows_out, "per_row": per_row}


def preview_pick(rows, members, prefer=None, primary=None):
    """Which segment a shot's preview is baked from: `prefer` when it has a
    readable source range (a tab asking for one exact segment), else the
    shot's widest live piece in the primary hub (it usually spans every use),
    else its widest hub piece, else its widest use. None when nothing has a
    source range."""
    def ok(i):
        return (isinstance(i, int) and 0 <= i < len(rows)
                and rows[i].get("src_in") is not None
                and rows[i].get("src_out") is not None
                and rows[i]["src_out"] >= rows[i]["src_in"])    # out inclusive
    if ok(prefer):
        return prefer
    cand = [i for i in (members or ()) if ok(i)]
    if not cand:
        return None
    hubs = [i for i in cand if rows[i].get("is_hub")]
    live = [i for i in hubs if rows[i].get("pub_nn") is None
            and (primary is None or rows[i].get("seq") == primary)]
    return max(live or hubs or cand,
               key=lambda i: (rows[i]["src_out"] - rows[i]["src_in"], -i))


def strip_uses(rows, members):
    """The source frames the spots actually show, merged — the orange band on
    a preview's scrub bar. Timewarped uses count to their last DISPLAYED
    frame (use_frames); hub pieces, refs and graphics are left out."""
    spans = []
    for i in members or ():
        if not (0 <= i < len(rows)):
            continue
        d = rows[i]
        if d.get("is_hub") or d.get("role") not in (None, "source"):
            continue
        f = use_frames(d)
        if f:
            spans.append((f[0], f[1]))
    return merge_ranges(spans)


BRAID_STATE_RANK = {"o": 0, "s": 1, "u": 2, "c": 3, "hub": 4}


def braid_layout(rows, groups, primary=None, conn=None, shared=None,
                 orphans=(), fold=False):
    """The Connections tab's CUT-FLOW BRAID: the whole
    job's connections at a glance. Lanes = the Conform Hub, then every
    sequence; blocks = shot uses in record order (widths from record
    duration, laid out per lane by the painter); ribbons join the SAME shot
    in consecutive lanes — never hub-to-everything — so aspect versions with
    one cut drop as calm verticals, re-edits cross, and shots a cutdown drops
    simply end: the funnel reads itself.

    Lane order is a greedy chain: the hub first, then repeatedly the sequence
    sharing the most shots with the lane above (more shots, then name, break
    ties), which keeps a family's versions adjacent and makes a shot rarely
    reappear after it drops out (when it does, its ribbon is marked `skip`).
    `fold` merges sequences with the IDENTICAL cut into one lane ("name +N")
    for big jobs; a folded block keeps its worst state.

    Block state against the shot's live hub piece(s): 'hub' (the hub lane),
    'c' connected, 's' shares the hub's source but is NOT connected, 'o' not
    in the hub (orphan, or the shot has no hub piece), 'u' connections not
    read yet. `conn`/`shared` = {row: set(rows)} maps (None = not read).
    Returns {'lanes': [{label, seqs, is_hub, order, blocks: [{g, rows, w,
    state, tw}]}], 'ribbons': [{g, frm: (lane, block), to: (lane, block),
    skip, state}]}."""
    group_of = {}
    for gi, members in enumerate(groups):
        for i in members:
            group_of[i] = gi
    orphans = set(orphans or ())
    hub_rows = {}
    for i, d in enumerate(rows):
        if (i in group_of and _is_live_hub(d) and d.get("pub_nn") is None
                and (primary is None or d.get("seq") == primary)):
            hub_rows.setdefault(group_of[i], []).append(i)

    def state(i):
        return link_state(i, hub_rows.get(group_of.get(i), ()), conn, orphans)

    by_seq = {}
    for i, d in enumerate(rows):
        if (d.get("is_hub") or d.get("role") != "source"
                or d.get("rec_in_f") is None or i not in group_of):
            continue
        by_seq.setdefault(d["seq"], []).append(i)
    seq_lanes = []
    for seq in sorted(by_seq, key=lambda x: x.lower()):
        idxs = sorted(by_seq[seq], key=lambda i: (rows[i]["rec_in_f"],
                                                  _track_sort_key(rows[i].get("track_id"))))
        seq_lanes.append({
            "label": seq, "seqs": [seq], "is_hub": False,
            "order": [group_of[i] for i in idxs],
            "blocks": [{"g": group_of[i], "rows": [i],
                        "w": rows[i].get("rec_dur_f") or 1, "state": state(i),
                        "tw": bool(rows[i].get("timewarp"))} for i in idxs]})
    if fold:
        merged, out = {}, []
        for ln in seq_lanes:
            key = tuple(ln["order"])
            if key not in merged:
                merged[key] = ln
                out.append(ln)
                continue
            m = merged[key]
            m["seqs"].append(ln["label"])
            for b, nb in zip(m["blocks"], ln["blocks"]):
                b["rows"] = b["rows"] + nb["rows"]
                b["tw"] = b["tw"] or nb["tw"]
                if BRAID_STATE_RANK[nb["state"]] < BRAID_STATE_RANK[b["state"]]:
                    b["state"] = nb["state"]
        for ln in out:
            if len(ln["seqs"]) > 1:
                ln["label"] = "%s +%d" % (ln["seqs"][0], len(ln["seqs"]) - 1)
        seq_lanes = out

    lanes, prev = [], set()
    if hub_rows:
        order = sorted(hub_rows, key=lambda g: (
            min(rows[i].get("rec_in_f") or 0 for i in hub_rows[g]), g))
        lanes.append({
            "label": primary or "Conform Hub", "seqs": [primary], "is_hub": True,
            "order": order,
            "blocks": [{"g": g, "rows": sorted(hub_rows[g]),
                        "w": max(rows[i].get("rec_dur_f") or 1 for i in hub_rows[g]),
                        "state": "hub", "tw": False} for g in order]})
        prev = set(order)
    rest = list(seq_lanes)
    while rest:
        rest.sort(key=lambda ln: (-len(set(ln["order"]) & prev),
                                  -len(set(ln["order"])), ln["label"].lower()))
        nxt = rest.pop(0)
        lanes.append(nxt)
        prev = set(nxt["order"])

    appear = {}
    for li, ln in enumerate(lanes):
        for bi, b in enumerate(ln["blocks"]):
            appear.setdefault(b["g"], {}).setdefault(li, []).append(bi)
    ribbons = []
    for g in sorted(appear):
        seen = sorted(appear[g].items())
        for (la, ba), (lb, bb) in zip(seen, seen[1:]):
            for k, b in enumerate(bb):
                ribbons.append({"g": g, "frm": (la, ba[min(k, len(ba) - 1)]),
                                "to": (lb, b), "skip": lb - la > 1,
                                "state": lanes[lb]["blocks"][b]["state"]})
    return {"lanes": lanes, "ribbons": ribbons}


def timeline_block_label(row, prefer_segment=False):
    """Block caption: Shot Name by default, Segment Name when
    the tab checkbox flips it; each falls through to the other, then camera."""
    if prefer_segment:
        return row.get("name") or row.get("shot") or row.get("camera") or ""
    return row.get("shot") or row.get("name") or row.get("camera") or ""


def _floor_int(x):
    i = int(x)
    return i if (x >= 0 or x == i) else i - 1


def _ceil_int(x):
    i = int(x)
    return i if x == i else (i + 1 if x > 0 else i)


def use_frames(row):
    """The source frames ONE consumer use DISPLAYS, as (first, end, how) —
    half-open, whole timecode frames — or None when nothing is readable.
      'cut'    a straight cut: its own source in/out.
      'curve'  a timewarp whose curve was read: every record frame's sample,
               FLOORED to the frame Flame displays, counted from the frame
               shown at record frame 1 (the segment's source in).
      'static' a timewarp whose curve couldn't be read: Flame's own in/out
               (on box a timewarp's source_out read as its last displayed
               frame on both timewarps checked) — kept so the
               build still holds something, never trusted as a check.
    Calibrated on box on a 25 fps source retimed to 104.27% (= 25/23.976)
    in 23.976 spots: source in 13:40:56:18, curve
    26.000 → 66.666 over 40 record frames, and Flame displays 13:40:58:08 on
    the last frame of every :15/:60 use = in + floor(66.666) − floor(26.000).
    A ceil + 1 model (then a +2 pad, plus the static range) builds
    13:40:58:11. source_out is INCLUSIVE on box, so a cut's end here is
    source_out + 1 (src_end) — reading it as the end put every cut one frame
    short, and the hub's timewarp-tailed pieces one frame long.
    The rule: last DISPLAYED timecode, not partial frames (Mix
    interpolation included)."""
    a, b = row.get("src_in"), row.get("src_out")
    if a is None:
        return None
    m = row.get("tw_model") or {}
    if m.get("t1") is not None and m.get("lo") is not None and m.get("hi") is not None:
        base = _floor_int(m["t1"])
        return (a + _floor_int(m["lo"]) - base, a + _floor_int(m["hi"]) - base + 1,
                "curve")
    if b is None or b < a:
        return None
    return (a, b + 1, "static" if row.get("timewarp") else "cut")


def hub_piece_range(rows, members):
    """The source range a hub piece must hold to serve EVERY consumer use of
    the shot: the union of what each use displays (use_frames) — straight
    cuts by their in/out, timewarps by their curve. EDL ground truth:
    picking ONE widest instance left 9/21
    hub pieces 1-4 frames short vs Flame's native builder; the union is what
    native builds. Timewarps use the displayed-frame rule (use_frames), not
    a padded envelope, so pieces are neither short nor long.
    Returns (lo, hi) half-open, or (None, None)."""
    lo = hi = None
    for i in members:
        d = rows[i]
        if d.get("is_hub") or d.get("role") != "source":
            continue
        f = use_frames(d)
        if f is None:
            continue
        lo = f[0] if lo is None else min(lo, f[0])
        hi = f[1] if hi is None else max(hi, f[1])
    return (lo, hi)


def hub_match_pick(rows, members):
    """Which use a hub piece is MATCHED from: the straight cut that STARTS
    EARLIEST (widest breaks ties). The matched clip fixes the piece's head and
    only the tail is widened afterwards, so matching the WIDEST use instead
    would leave the head short whenever a narrower use started earlier.
    Timewarped uses are matched only when nothing else exists. None if no
    member has a source in-point."""
    placed = [i for i in members if rows[i].get("src_in") is not None]
    pool = [i for i in placed if not rows[i].get("timewarp")] or placed
    if not pool:
        return None
    return min(pool, key=lambda i: (
        rows[i]["src_in"],
        -((rows[i].get("src_out") or rows[i]["src_in"]) - rows[i]["src_in"]), i))


def checked_use_range(rows, members):
    """(first, end) half-open extent of every use whose frames are KNOWN —
    straight cuts and timewarps with a readable curve — exactly what the Hub
    tab holds a hub piece to. None when no use is known."""
    rng = []
    for i in members:
        d = rows[i]
        if d.get("is_hub") or d.get("role") != "source":
            continue
        f = use_frames(d)
        if f is not None and f[2] != "static":
            rng.append(f)
    if not rng:
        return None
    return (min(f[0] for f in rng), max(f[1] for f in rng))


def shot_record_end(track_durations, planned_end):
    """Where the NEXT hub shot starts: the furthest END of the tracks this
    shot actually landed pieces on, each end = the sum of that track's segment
    durations (gaps and the newborn frame included). Advancing by the PLANNED
    footprint left a gap wherever a piece came up shorter than planned
    (e.g. a gap after shot 1 and another before the last shot). Summed
    durations, not record_in, because durations read true (EDL-verified)
    while record_in reads 1 low on a gap-headed sequence — and `rec_at +
    duration` clipped shot 1 whenever the newborn frame pushed it to frame 1.
    `track_durations` = [[durations of one track], …]; `planned_end` only
    when nothing landed."""
    ends = [sum(d or 0 for d in durs) for durs in track_durations]
    ends = [e for e in ends if e]
    return max(ends) if ends else planned_end


# ====================================================================
# Pure version / clean-replace logic  (off-box tested)
# ====================================================================

def tw_envelope(samples):
    """Envelope + shape of a sampled retime curve: true min/max timing value,
    monotonic direction, reversal detection. Pure — the sampler
    (tw_sample_model) is on-box. Keyframed non-uniform curves are captured
    exactly because we READ the curve per record frame, never infer it."""
    if not samples:
        return {"lo": None, "hi": None, "monotonic": True, "reversed": False}
    lo, hi = min(samples), max(samples)
    diffs = [b - a for a, b in zip(samples, samples[1:])]
    inc = any(d > 0 for d in diffs)
    dec = any(d < 0 for d in diffs)
    return {"lo": lo, "hi": hi, "monotonic": not (inc and dec),
            "reversed": dec and not inc}


def is_off_latest(version_uid, version_uids):
    """An openclip is off-latest when its current version is not the last entry
    of its ascending version list. Forks are judged against their own openclip,
    so this is always a per-clip comparison. Empty/unknown -> not off-latest."""
    if not version_uids:
        return False
    return version_uid != version_uids[-1]


def clean_replace_check(cut_len, clip_len, cut_rate, clip_rate,
                        cut_start=None, clip_start=None):
    """Read-only gate for a would-be media replace (not yet wired to a replace;
    pure and tested). Returns (ok, level, messages):

        level 'block'   - a hard stop: rate mismatch, or the openclip is shorter
                          than the cut (coverage shortfall).
        level 'verify'  - safe to replace but worth a human glance: extra handle
                          frames, or a different start-frame convention (rebase).
        level 'clean'   - rate matches, coverage holds, no handle/start delta.

    ok is True for 'verify' and 'clean', False for 'block'."""
    msgs = []
    if cut_rate is not None and clip_rate is not None and \
            abs(parse_rate(cut_rate) - parse_rate(clip_rate)) > 1e-6:
        msgs.append("rate mismatch: cut %s vs clip %s"
                    % (parse_rate(cut_rate), parse_rate(clip_rate)))
        return False, "block", msgs
    if cut_len is not None and clip_len is not None and clip_len < cut_len:
        msgs.append("coverage shortfall: clip %d < cut %d frames" % (clip_len, cut_len))
        return False, "block", msgs
    level = "clean"
    if cut_len is not None and clip_len is not None and clip_len > cut_len:
        msgs.append("verify: %d handle frame(s) beyond the cut" % (clip_len - cut_len))
        level = "verify"
    if cut_start is not None and clip_start is not None and cut_start != clip_start:
        msgs.append("verify: start Δ %d (rebase convention differs)"
                    % (clip_start - cut_start))
        level = "verify"
    return True, level, msgs


# ====================================================================
# Flame helpers  (need the box)
# ====================================================================

def _get(obj, name):
    v = getattr(obj, name, None)
    return v() if callable(v) else v


def _safe_name(obj):
    try:
        return str(obj.name)
    except Exception:
        return type(obj).__name__


def _clean_name(obj):
    n = _safe_name(obj)
    if len(n) >= 2 and n[0] == n[-1] and n[0] in ("'", '"'):
        n = n[1:-1]
    return n


def _seg_shot_name(seg):
    """The segment's SHOT NAME — the field Flame's publish tokens use, and the
    one the renumber convention targets (the segment name is the wrong
    field for it)."""
    try:
        v = getattr(seg, "shot_name", None)
        if v is None:
            return ""
        s = str(v.get_value() if hasattr(v, "get_value") else v).strip("'\" ")
        return "" if s.lower() == "none" else s
    except Exception:
        return ""


def _seg_name(seg):
    """Segment's own name, blank if unnamed (no type-name fallback), so the Name
    column stays empty rather than showing 'PySegment'."""
    try:
        n = str(seg.name)
    except Exception:
        return ""
    if len(n) >= 2 and n[0] == n[-1] and n[0] in ("'", '"'):
        n = n[1:-1]
    return n.strip()


def _ancestor(seg, typename):
    p = getattr(seg, "parent", None)
    while p is not None:
        if type(p).__name__ == typename:
            return p
        p = getattr(p, "parent", None)
    return None


def _seqs_in(container):
    out, seen = [], set()

    def walk(o):
        if o is None or id(o) in seen:
            return
        seen.add(id(o))
        if type(o).__name__ == "PySequence":
            out.append(o)
            return
        for attr in ("sequences", "reels", "reel_groups", "libraries", "desktops"):
            children = _get(o, attr)
            if children:
                for c in children:
                    walk(c)

    walk(container)
    return out


def _current_segment():
    try:
        return flame.timeline.current_segment
    except Exception:
        return None


def _current_sequence():
    try:
        return flame.timeline.clip
    except Exception:
        return None


def sequences_for_scope(scope):
    """Resolve a scope label to the list of sequences to scan. The Sources
    Sequence (the hub) is included here -- it is filtered out of the consumer
    tally downstream, not out of the scan."""
    s = (scope or "Current Reel").strip().lower()
    if s == "selected sequences":
        out = []
        for e in (_get(flame.media_panel, "selected_entries") or []):
            out += _seqs_in(e)
        return _dedup_seqs(out)
    seg = _current_segment()
    anchor = seg if seg is not None else _current_sequence()
    if anchor is None:
        return []
    if s == "current reel":
        reel = _ancestor(anchor, "PyReel") or _ancestor(seg, "PyReel")
        return _dedup_seqs(_seqs_in(reel)) if reel else _dedup_seqs(_seqs_in(anchor))
    if s == "current reel group":
        rg = _ancestor(anchor, "PyReelGroup")
        return _dedup_seqs(_seqs_in(rg)) if rg else []
    if s == "current desktop":
        dt = _ancestor(anchor, "PyDesktop")
        if dt is None:
            try:
                dt = flame.projects.current_project.current_workspace.desktop
            except Exception:
                dt = None
        return _dedup_seqs(_seqs_in(dt)) if dt else []
    if s == "open sequences":
        # Best-effort: every sequence on the current desktop. Flame has no clean
        # 'is open in a player' enumerator; refine on-box if needed.
        try:
            dt = flame.projects.current_project.current_workspace.desktop
            return _dedup_seqs(_seqs_in(dt))
        except Exception:
            return _dedup_seqs(_seqs_in(_ancestor(anchor, "PyDesktop")))
    return []


def _seq_alive(seq):
    """A held sequence is still in the project: its name reads and it still
    has a parent (a reel on box)."""
    try:
        seq.name
        return getattr(seq, "parent", None) is not None
    except Exception:
        return False


def _dedup_seqs(seqs):
    out, seen = [], set()
    for s in seqs:
        if id(s) not in seen:
            seen.add(id(s))
            out.append(s)
    return out


def iter_segments(sequence):
    for ver in (_get(sequence, "versions") or []):
        for trk in (_get(ver, "tracks") or []):
            for seg in (_get(trk, "segments") or []):
                yield seg


def iter_segments_with_track(sequence):
    """(segment, track_id, track_name) triples. track_id is positional
    ('v0.t1') — scan-stable, cheap, and never persisted (ref nudges persist per
    SEGMENT via _seg_key; track ref-ness is re-inferred each scan)."""
    for vi, ver in enumerate(_get(sequence, "versions") or []):
        for ti, trk in enumerate(_get(ver, "tracks") or []):
            tname = _clean_name(trk)
            tid = "v%d.t%d" % (vi, ti)
            for seg in (_get(trk, "segments") or []):
                yield seg, tid, tname


def _is_gap(seg):
    try:
        return "gap" in str(seg.type).lower()
    except Exception:
        return False


def version_token(path, src_name=""):
    """The version tag in a media path — 'v011' out of
    …/JOB_sh0060_ALT_v011/JOB_sh0060_ALT_v011.00001001.exr. On a
    version-managed job the whole duplicate question is 'WHICH version am I
    looking at', and the answer is sitting in the path.
    Falls back to a tag in the source name, else ''."""
    for text in (str(path or ""), str(src_name or "")):
        if not text:
            continue
        hits = re.findall(r"(?<![A-Za-z0-9])[vV](\d{1,4})(?![0-9])", text)
        if hits:
            return "v%s" % hits[-1].zfill(3)     # last wins: file over folder
    return ""


def _path_key(path):
    """Short stable tag for a media path — the only reliable discriminator
    between two real sources of the same shot on box (all uids read
    empty). Used to key the per-source filmstrip cache."""
    p = str(path or "").strip().strip("'\"")
    if not p or p.lower() == "none":
        return ""
    return hashlib.md5(p.encode("utf-8", "replace")).hexdigest()[:10]


def _seg_key(seg):
    """Persistent string identity for a segment — the ref_overrides key, so a
    'this is a ref' nudge survives across scans and sessions. seg.uid is exact
    (confirmed on PySegment in gfx work); fall back to sequence + name + a
    position attr."""
    u = getattr(seg, "uid", None)
    if u:
        return str(u).strip("'\"")
    parts = [_clean_name(_ancestor(seg, "PySequence")), _clean_name(seg)]
    for attr in ("record_in", "source_in", "start_frame"):
        v = getattr(seg, attr, None)
        if v is not None:
            parts.append(str(v))
            break
    return "|".join(parts)


def _track_pos(seg):
    """'v<version>.t<track>' of a segment, read from its own parents — the
    same label the scan gives its row. Stacked pieces of one shot can share
    a sequence, a name, a record in and a source in (a stacked Conform Hub
    puts one piece per real source at the same record position; a Publish
    snapshot copies a piece onto another version): only the track tells them
    apart. '' when the parents can't be read or compared."""
    try:
        trk = seg.parent
        ver = trk.parent
        seq = ver.parent
        vi = next((k for k, v in enumerate(_get(seq, "versions") or [])
                   if v == ver), None)
        ti = next((k for k, t in enumerate(_get(ver, "tracks") or [])
                   if t == trk), None)
    except Exception:
        return ""
    if vi is None or ti is None:
        return ""
    return "v%d.t%d" % (vi, ti)


def _match_key(seg):
    """Identity used to match a segment RETURNED BY FLAME (from
    shared_source_segments) back to a scanned row. Deliberately richer than
    `_seg_key`, which stays as-is because persisted ref_overrides are keyed
    on it: on a real job _seg_key produced 67 collisions
    because a shot and the ref or graphic stacked directly above
    it share a sequence, a record position and often a name — and each
    collision silently dropped a member from the shared-source map, making
    duplicate verdicts unreliable. Adding the source in-point and the source
    name separates most stacked segments; the TRACK separates the rest — a
    stacked Conform Hub's pieces of one shot (one per real source) share
    all of the above and collided, so a merge couldn't tell which hub piece
    shares which source."""
    return "|".join(str(x) for x in (
        _clean_name(_ancestor(seg, "PySequence")),
        _clean_name(seg),
        str(getattr(seg, "source_name", "") or "").strip("'\""),
        _frames(getattr(seg, "record_in", None)),
        _frames(getattr(seg, "source_in", None)),
        _track_pos(seg)))


def _count(v):
    """A frame count Flame hands back as an int or a string ('17'); None
    for anything else ('infinite', missing)."""
    try:
        return int(str(v).strip("'\" "))
    except Exception:
        return None


def _frames(t):
    """PyTime -> integer frame count (int(record_duration) is confirmed)."""
    for a in ("frame", "frames"):
        v = getattr(t, a, None)
        if isinstance(v, int):
            return v
    try:
        return int(t)
    except Exception:
        return None


def _seg_rate(seg):
    """Source frame rate of a segment as a float ('25 fps' string tolerated)."""
    return parse_rate(getattr(seg, "source_frame_rate", None))


_TIMEWARP_HINTS = ("timewarp", "retime", "warp")


def is_timewarp(seg):
    """A retimed segment maps source non-linearly, so its source range can't be
    trusted for a literal cut. We flag for manual handling and never auto-split
    by design. On box, timeline FX surface as
    a GENERIC 'PyTimelineFX' class, so the class name alone is not enough — the
    effect's own type/name strings are checked too."""
    try:
        for e in (seg.effects or []):
            parts = [type(e).__name__]
            for attr in ("type", "name"):
                v = getattr(e, attr, None)
                if v is not None:
                    parts.append(str(v))
            joined = " ".join(parts).lower()
            if any(h in joined for h in _TIMEWARP_HINTS):
                return True
    except Exception:
        pass
    return False


def _tw_fx(seg):
    try:
        for e in (seg.effects or []):
            if "timewarp" in type(e).__name__.lower():
                return e
    except Exception:
        pass
    return None


# mode-aware curve getters (probed on box: each TW mode has its own;
# calling the wrong one raises cleanly)
TW_GETTERS = {"Timing": "get_timing", "Speed": "get_speed_timing",
              "Duration": "get_duration_timing"}


def tw_sample_model(seg, rec_dur):
    """The retime model: sample the TW curve at every record frame
    (get_timing(record_frame) -> source timing value, VERIFIED on-box) and
    reduce via tw_envelope. Returns {"lo","hi","monotonic","reversed","mode",
    "n"} or None when no TW / unreadable."""
    fx = _tw_fx(seg)
    if fx is None or not rec_dur:
        return None
    try:
        mode = getattr(fx, "mode", None)
        mode = str(mode.get_value() if hasattr(mode, "get_value") else mode)
        getter = getattr(fx, TW_GETTERS.get(mode, ""), None)
        if getter is None:
            return None
        step = 1 if rec_dur <= 2000 else max(1, rec_dur // 2000)
        frames = list(range(1, rec_dur + 1, step))
        if frames[-1] != rec_dur:
            frames.append(rec_dur)
        vals = [float(getter(float(f))) for f in frames]
        model = tw_envelope(vals)
        model["mode"] = mode
        model["n"] = len(frames)
        # timing values are in the curve's own coordinate space, NOT absolute
        # source-TC frames (mixing bases once produced 900k-frame "head
        # short" warnings). Keep the record-frame-1 sample as the anchor so
        # consumers can work with anchor-free DIFFERENCES.
        model["t1"] = vals[0]
        model["tN"] = vals[-1]
        # exact effective % from the endpoint slope (span/duration was off by
        # edge frames — a curve measured at 198.65 displayed as 197)
        if len(frames) > 1 and frames[-1] != frames[0]:
            model["pct"] = round(abs(vals[-1] - vals[0])
                                 / (frames[-1] - frames[0]) * 100.0, 2)
        return model
    except Exception:
        return None


def _real_source(seg):
    """True when the segment carries actual linked media (not a gap / unlinked /
    titles-only segment). Only real-source segments participate in the conform
    audits."""
    if _is_gap(seg):
        return False
    if getattr(seg, "source_unlinked", False):
        return False
    return getattr(seg, "source_uid", None) is not None


# ====================================================================
# Scan  (the single shared walk that feeds every audit + view)
# ====================================================================

def scan_instances(sequences, settings=None, warnings=None, state=None):
    """Every real-source consumed segment across `sequences` (resolve the scope
    ONCE via sequences_for_scope and pass the list in — every Py-attr access is
    a Flame round-trip, so the tree is walked exactly once per scan). One row
    per timeline instance. The Sources Sequence is flagged (is_hub) but kept —
    it defines shot identity and the coverage extent. Rows are role-classified
    (hub/ref/source) before returning. One throwing segment is skipped and
    reported rather than aborting the whole scan."""
    settings = settings or load_settings()
    state = state or load_state()
    hub_name = (settings.get("sources_seq_name") or "Conform_Hub").strip()
    hub_aliases = ([hub_name] + list(HUB_ALIASES)
                   + [h for h in state.get("hub_names", []) if h])
    strat = settings.get("camera_token", "first_underscore")
    overrides = state.get("identity_overrides", {})
    out = []
    for seq in sequences:
        sname = _clean_name(seq)
        is_hub = any(is_hub_name(sname, h) for h in hub_aliases)
        try:
            seq_rate = parse_rate(getattr(seq, "frame_rate", None), None)
        except Exception:
            seq_rate = None
        for seg, track_id, track_name in iter_segments_with_track(seq):
            try:
                if not _real_source(seg):
                    continue
                rate = _seg_rate(seg)
                src_in = _frames(getattr(seg, "source_in", None))
                src_out = _frames(getattr(seg, "source_out", None))
                # source_uid can be EMPTY on real conformed segments (on-box
                # probe) — fall through the sibling identity uids so
                # Tier-1 dup detection keeps a deterministic key.
                src_uid = ""
                for uid_attr in ("source_uid", "source_essence_uid",
                                 "original_source_uid"):
                    v = getattr(seg, uid_attr, None)
                    if v is not None and str(v).strip("'\" "):
                        src_uid = str(v).strip("'\" ")
                        break
                src_name = _clean_name(seg) if getattr(seg, "source_name", None) is None \
                    else str(getattr(seg, "source_name")).strip("'\"")
                cam = overrides.get(src_uid) or camera_token(
                    src_name, _seg_name(seg), strat)
                rec_in = getattr(seg, "record_in", None)
                rec_dur = getattr(seg, "record_duration", None)
                warp = is_timewarp(seg)
                tw_model = tw_sample_model(seg, _frames(rec_dur)) if warp else None
                out.append({
                    "seq": sname, "seg": seg, "is_hub": is_hub,
                    "seg_key": _seg_key(seg),
                    "match_key": _match_key(seg),
                    "track_id": track_id, "track_name": track_name,
                    "pub_nn": publish_track_index(track_name) if is_hub else None,
                    "name": _seg_name(seg),
                    "shot": _seg_shot_name(seg),
                    "src_uid": src_uid, "src_name": src_name,
                    # shot identity that survives a job-prefix naming scheme
                    "name_key": normalize_source_name(src_name)
                                or normalize_source_name(_seg_name(seg)),
                    # verified on box: file_path EXISTS on PySegment — the
                    # scenario-#1 discriminator (same file imported twice
                    # vs a genuinely separate delivery)
                    "file_path": str(getattr(seg, "file_path", "") or ""),
                    "camera": cam, "cam_key": (cam or "").lower(),
                    "rate": rate, "seq_rate": seq_rate,
                    "src_in": src_in, "src_out": src_out,
                    "src_in_tc": frames_to_tc(src_in, rate),
                    "src_out_tc": frames_to_tc(src_out, rate),
                    "src_dur_f": (src_out - src_in + 1) if (src_in is not None and src_out is not None) else None,
                    "rec_in_f": _frames(rec_in), "rec_dur_f": _frames(rec_dur),
                    "rec_in_tc": frames_to_tc(_frames(rec_in), rate),
                    "rec_dur_tc": frames_to_tc(_frames(rec_dur), rate),
                    "timewarp": warp, "tw_model": tw_model,
                    # frames of media outside the cut (Flame's head/tail):
                    # the real source's extent for a merge. Non-numeric
                    # ('infinite' was read once) gives None.
                    "head_f": _count(getattr(seg, "head", None)),
                    "tail_f": _count(getattr(seg, "tail", None)),
                    # ident keys the filmstrip cache. It MUST separate two
                    # real sources of the same shot: uids read empty on box,
                    # so camera@src_in alone collided and the second grade
                    # displayed the first one's frames (a teal grade showed
                    # the other grade's thumbnail). file_path
                    # is the discriminator; seg_key is the last resort.
                    "ident": src_uid or ("%s@%s|%s" % (
                        (cam or "?"), src_in,
                        _path_key(getattr(seg, "file_path", "")) or _seg_key(seg))),
                })
            except Exception as e:
                msg = "⚠ scan skipped a segment in %s: %s" % (sname, e)
                log.warning(msg)
                if warnings is not None:
                    warnings.append(msg)
    classify_roles(out, settings, state.get("ref_overrides", {}),
                   state.get("track_roles", {}))
    if settings.get("detect_slates", True):
        rate = next((r.get("rate") for r in out if r.get("rate")), 24.0)
        n = mark_slates(out, rate, int(settings.get("slate_lead_secs", 60) or 60))
        if n and warnings is not None:
            warnings.append("ℹ %d pre-roll slate segment(s) flagged (they sit "
                            "before the hour start); they are excluded from "
                            "the conform audits." % n)
    return out


def iter_openclips(sequences):
    """PyClips around `sequences` that expose a version list (the published
    output openclips). Walks the media-panel containers surrounding the already-
    resolved sequence list. Best-effort: returns whatever carries version_uids."""
    seen, out = set(), []
    roots = []
    try:
        for e in (_get(flame.media_panel, "selected_entries") or []):
            roots.append(e)
    except Exception:
        pass
    for seq in sequences:
        rg = _ancestor(seq, "PyReelGroup") or _ancestor(seq, "PyDesktop")
        if rg is not None:
            roots.append(rg)

    def walk(o):
        if o is None or id(o) in seen:
            return
        seen.add(id(o))
        try:
            if getattr(o, "version_uids", None):   # RAISES on non-versioned clips
                out.append(o)
        except Exception:
            pass
        for attr in ("clips", "reels", "reel_groups", "libraries", "folders", "desktops"):
            children = _get(o, attr)
            if children:
                for c in children:
                    walk(c)

    for r in roots:
        walk(r)
    return out


def openclip_inventory(sequences, warnings=None):
    """Off-latest audit data: one row per openclip, with current vs latest
    version and where it sits."""
    out = []
    for clip in iter_openclips(sequences):
        try:
            cur = str(getattr(clip, "version_uid", "") or "")
            uids = [str(u) for u in (getattr(clip, "version_uids", None) or [])]
            out.append({
                "clip": clip, "name": _clean_name(clip),
                # the segment<->openclip join key (probe-verified:
                # seg.source_essence_uid == clip.essence_uid)
                "essence_uid": str(getattr(clip, "essence_uid", "") or "").strip("'\""),
                "version_uid": cur, "version_uids": uids,
                "latest": uids[-1] if uids else "",
                "off_latest": is_off_latest(cur, uids),
                "n_versions": len(uids),
            })
        except Exception as e:
            msg = "⚠ openclip scan skipped one: %s" % e
            log.warning(msg)
            if warnings is not None:
                warnings.append(msg)
    return out


# ---- audits assembled from the pure core --------------------------

def consumer_instances(instances):
    """Instances that count as consumption: not the hub, and not a reference
    picture — a ref represents the cut, it doesn't consume a source."""
    return [d for d in instances
            if not d.get("is_hub") and d.get("role", "source") == "source"]


def source_groups(instances, match_chars=10, require_name=True):
    """Group every instance (hub + consumers) by logical source identity, using
    the same Tier-1/Tier-2 logic as the dup audit but keeping ALL groups,
    singletons included. Returns list of member-index lists. This is the spine of
    the Coverage Ruler and the Connection Map (each group == one logical shot)."""
    groups = duplicate_sets(instances, match_chars, require_name)
    placed = set()
    out = []
    for g in groups:
        out.append(list(g["members"]))
        placed.update(g["members"])
    for i in range(len(instances)):
        if i not in placed:
            out.append([i])
    return out


def source_extent(instances, members):
    """Full source extent (lo, hi) for a logical shot: the min source_in / max
    source_out across its instances. The conform's 'how long is this source'.
    Timewarped members contribute their conservative min->max envelope."""
    ins = [instances[i]["src_in"] for i in members if instances[i]["src_in"] is not None]
    ends = [src_end(instances[i]) for i in members
            if instances[i]["src_out"] is not None]
    if not ins or not ends:
        return (None, None)
    return (min(ins), max(ends))


def coverage_report(instances, members, settings):
    """Over-coverage analysis for one logical shot. Consumed ranges come from the
    CONSUMER instances only (hub placement isn't 'consumption'); extent spans all.
    Returns the merged consumed bands, minimal covering pieces (+handles), and
    the dead zones to propose as trims."""
    handle = int(settings.get("handle_frames", 8) or 0)
    gap = int(settings.get("merge_gap_frames", 0) or 0)
    extent = source_extent(instances, members)
    consumer_members = [i for i in members if not instances[i].get("is_hub")
                        and instances[i].get("role", "source") == "source"]
    ranges = [(instances[i]["src_in"], src_end(instances[i]))
              for i in consumer_members
              if instances[i]["src_in"] is not None and instances[i]["src_out"] is not None]
    merged = merge_ranges(ranges, gap)       # merge once; the helpers re-merging
    return {                                 # pre-merged input at gap=0 is a no-op
        "extent": extent,
        "consumed": merged,
        "pieces": covering_pieces(merged, handle, extent, 0),
        "dead": dead_zones(merged, extent, 0) if None not in extent else [],
        "fraction": coverage_fraction(merged, extent, 0) if None not in extent else 0.0,
        "n_consumers": len(consumer_members),
        "has_timewarp": any(instances[i]["timewarp"] for i in members),
    }


# ====================================================================
# Preview filmstrip  (on-box; PyExporter path — wiretap can't read
# UNCACHED media, which is the normal openclip/conform state, per an
# on-box probe)
# ====================================================================

def _safe_delete(obj):
    """Remove a media-panel entry. Stray-clip bug: flame.delete
    defaults to confirm=True, which cannot be answered from script — the
    delete silently no-ops and temp matched clips pile up in reels. Always
    pass confirm=False first. (ok, error)."""
    try:
        flame.delete(obj, confirm=False)
        return True, None
    except Exception as e1:
        try:
            flame.delete(obj)
            return True, None
        except Exception:
            pass
        try:
            obj.delete()
            return True, None
        except Exception as e2:
            return False, e2 or e1


def _scratch_reel():
    """First reel of the first desktop reel group — the transient landing spot
    for a matched clip (the same pattern a snapshot hook uses)."""
    return flame.projects.current_project.current_workspace.desktop.reel_groups[0].reels[0]


def _preview_cache_dir(ident):
    """Per-shot filmstrip cache INSIDE the project (system /var/folders
    temp dirs are invisible/unfindable). Lives beside the CCM state:
    <setups>/flame_ccm/preview_cache/<shot-ident>/ — so it follows the project,
    survives restarts (re-opening a shot's preview is instant), and is one
    obvious folder to wipe."""
    d = (load_settings().get("state_dir") or "").strip() or _default_state_dir()
    safe = re.sub(r"[^\w.-]+", "_", str(ident)).strip("_")[:80] or "shot"
    return os.path.join(d, "preview_cache", safe)


def _collect_jpegs(out_dir):
    files = []
    if os.path.isdir(out_dir):
        for root, _dirs, fns in os.walk(out_dir):
            for fn in fns:
                if fn.lower() == "mid.jpg":     # pre-0.9.1 ledger single frame
                    continue
                if fn.lower().endswith((".jpg", ".jpeg")):
                    files.append(os.path.join(root, fn))
    files.sort()
    return files


def _find_jpeg_preset():
    """Locate a shipped JPEG image-sequence export preset. On-box probe:
    get_presets_(base_)dir REQUIRE enum args. Search Autodesk visibility first,
    then Shared/Project; match 'jpeg' in the XML filename."""
    ex = flame.PyExporter
    for vis_name in ("Autodesk", "Shared", "Project"):
        vis = getattr(ex.PresetVisibility, vis_name, None)
        if vis is None:
            continue
        dirs = []
        for typ_name in ("Image_Sequence", "ImageSequence", "Image"):
            typ = getattr(ex.PresetType, typ_name, None)
            if typ is None:
                continue
            try:
                dirs.append(ex.get_presets_dir(vis, typ))
            except Exception:
                continue
        try:
            dirs.append(ex.get_presets_base_dir(vis))
        except Exception:
            pass
        for d in dirs:
            d = str(d)
            if not os.path.isdir(d):
                continue
            for root, _dirs, files in os.walk(d):
                for fn in sorted(files):
                    if fn.lower().endswith(".xml") and "jpeg" in fn.lower():
                        return os.path.join(root, fn)
    return None


def _patch_preset_resolution(preset_path, scratch_dir, width=320, height=180):
    """Copy a preset XML with its <resize> block forced to a small fit — a
    5.7K source must not export 250 full-res JPEGs for a thumbnail strip. If
    the preset has no recognizable resize block, return it unmodified (full-res
    export still works, just slower)."""
    try:
        with open(preset_path, encoding="utf-8") as f:
            txt = f.read()
        if "<resize>" not in txt:
            return preset_path
        new = re.sub(r"<width>\d+</width>", "<width>%d</width>" % width, txt)
        new = re.sub(r"<height>\d+</height>", "<height>%d</height>" % height, new)
        new = re.sub(r"<resizeType>[^<]*</resizeType>", "<resizeType>fit</resizeType>", new)
        if new == txt:
            return preset_path
        out = os.path.join(scratch_dir, "ccm_preview_preset.xml")
        with open(out, "w", encoding="utf-8") as f:
            f.write(new)
        return out
    except Exception:
        return preset_path


def export_preview_strip(seg, out_dir, log_fn):
    """Match the segment's media to a scratch reel (preserve_handle=True so the
    clip is EXACTLY the segment's source range — that makes the frame mapping
    exact: file[i] == src_in + i), foreground-export small JPEGs, delete the
    matched clip. Returns (sorted_files, error_or_None). The only mutation is
    the transient media-panel clip; the timeline is never touched."""
    clip = None
    try:
        preset = _find_jpeg_preset()
        if not preset:
            return [], "no JPEG export preset found (get_presets_dir)"
        # 640-wide: the fixed preview panel earns real pixels (320 was for the
        # old in-canvas thumb)
        preset = _patch_preset_resolution(preset, out_dir, width=640, height=360)
        log_fn("preview: preset %s" % preset)
        reel = _scratch_reel()
        t0 = time.time()
        clip = seg.match(reel, preserve_handle=True, include_timeline_fx=False)
        if isinstance(clip, (list, tuple)):
            clip = clip[0] if clip else None
        if clip is None:
            return [], "seg.match returned nothing"
        ex = flame.PyExporter()
        try:
            ex.foreground = True
        except Exception:
            pass
        try:
            ex.export_between_marks = False
        except Exception:
            pass
        ex.export(clip, preset, out_dir)
        files = _collect_jpegs(out_dir)
        log_fn("preview: %d frame(s) exported in %.1fs" % (len(files), time.time() - t0))
        return files, (None if files else "export produced no JPEG files")
    except Exception as e:
        return [], str(e)
    finally:
        if clip is not None:
            ok, err = _safe_delete(clip)
            if not ok:
                log_fn("preview: temp matched clip could not be deleted (%s) — "
                       "remove it from the first desktop reel by hand" % err)


# ====================================================================
# Prep Publish execution  (on-box; every call in here is from the
# probe-verified list: create_version, copy_to_media_panel, overwrite,
# remove_connection, duplicate_source, settable names)
# ====================================================================

def _set_flame_attr(obj, attr, value):
    """Write a Flame attribute that may be a plain value or a PyAttribute
    (probe: some carry set_value and a broken __repr__). Returns True on a
    successful write, False otherwise — callers surface failures as manual
    steps, never silently."""
    try:
        a = getattr(obj, attr, None)
        sv = getattr(a, "set_value", None)
        if callable(sv):
            sv(value)
            return True
    except Exception:
        pass
    try:
        setattr(obj, attr, value)
        return True
    except Exception:
        return False


def _true_record_pos(seg):
    """Absolute record position of a segment on its track, robust to the
    leading-gap skew: on a gap-headed sequence every record_in reads 1 low
    (EDL ground truth — CCM hub seg 1 sits at 00:00:00:01 in the
    exported EDL but record_in reads 0), while durations don't lie. Locate
    the segment on its own track by record_in (both sides read in the same
    skewed basis, so the match is exact) and SUM the durations of everything
    before it, gaps included."""
    target = _frames(getattr(seg, "record_in", None))
    if target is None:
        return None
    try:
        track = _ancestor(seg, "PyTrack")
        pos = 0
        for s in (_get(track, "segments") or []):
            if _frames(getattr(s, "record_in", None)) == target:
                return pos
            pos += _frames(getattr(s, "record_duration", None)) or 0
    except Exception:
        pass
    return target


def _close_leading_gap(sq, say):
    """Newborn sequences are born 1 frame long, so the first overwrite leaves
    a 1-frame leading gap (EDL-proven; it also skews every record_in read 1
    low). On box, detecting the gap by TYPE failed silently — the
    newborn gap's .type does not read as a gap — while the duration-sum jump
    fix proved the object IS enumerated on the track. So detect by ORDER:
    everything on the populated track BEFORE the first real-source segment
    is leading junk. Delete it; if it survives, extend the first real
    segment's head over it (trim_head sign resolved by READBACK) and slip to
    restore source alignment. If everything fails, dump the head of the
    track so a diagnosis starts from facts, not theories."""
    def track_list():
        for ver in (_get(sq, "versions") or []):
            for trk in (_get(ver, "tracks") or []):
                segs = list(_get(trk, "segments") or [])
                if any(_real_source(s) for s in segs):
                    return segs
        return []

    def _looks_real(s):
        # realness for THIS check needs a media mapping, not just a non-None
        # source_uid (real conformed segments read '' here — the newborn gap
        # might too); a gap has no source_in
        return _real_source(s) and getattr(s, "source_in", None) is not None

    def lead_junk():
        # position-based, not list-order-based: the segment list's ordering is
        # unverified, but record_in reads are comparable within one track
        segs = track_list()
        reals = [(_frames(getattr(s, "record_in", None)) or 0, k, s)
                 for k, s in enumerate(segs) if _looks_real(s)]
        if not reals:
            return [], None
        fpos, _k, first = min(reals)
        junk = [s for s in segs if not _looks_real(s)
                and (_frames(getattr(s, "record_in", None)) or 0) <= fpos]
        return junk, first

    junk, first = lead_junk()
    if first is None:
        return False
    if not junk:
        # no junk enumerated — cross-check against the sequence's own duration
        # (covers the theory that the newborn frame hides OUTSIDE the segment
        # list; a mismatch here is the diagnostic to report)
        try:
            total = sum(_frames(getattr(s, "record_duration", None)) or 0
                        for s in track_list())
            seqd = _frames(getattr(sq, "duration", None))
            if seqd and total and seqd != total:
                say("  NOTE: sequence duration %d ≠ segment sum %d — hidden "
                    "%+d fr outside the track list"
                    % (seqd, total, seqd - total))
        except Exception:
            pass
        return True
    glen = sum(_frames(getattr(s, "record_duration", None)) or 0
               for s in junk) or 1
    for s in junk:
        _safe_delete(s)
    junk2, first = lead_junk()
    if first is not None and not junk2:
        say("  leading %d-frame gap deleted" % glen)
        return True
    if first is None:
        return False
    for off in (-glen, glen):
        try:
            first.trim_head(off, False)
        except Exception:
            continue
        j3, _f3 = lead_junk()
        if not j3:
            try:
                first.slip(glen if off < 0 else -glen)
            except Exception:
                say("  gap closed but slip failed — VERIFY shot 1's first frame")
            say("  leading %d-frame gap absorbed (trim_head %+d + slip) — "
                "VERIFY shot 1's first frame" % (glen, off))
            return True
    say("  ⚠ leading %d-frame gap STILL present; track head dump:" % glen)
    for k, s in enumerate(track_list()[:4]):
        say("    [%d] type=%r name=%r rec_in=%r dur=%r real=%s"
            % (k, str(getattr(s, "type", "?")), _clean_name(s),
               _frames(getattr(s, "record_in", None)),
               _frames(getattr(s, "record_duration", None)),
               _real_source(s)))
    return False


def _move_tail(seg, delta, say, label="", info=None):
    """Move a segment's tail by `delta` source frames: +N lengthens (the union
    of uses reaches further), -N shortens (the matched clip runs past every
    use — a timewarp matched out at its record length). trim_tail's
    sign convention is undocumented, so it is learned by READBACK from a
    ONE-FRAME probe and the rest is made in one move; a media-end clamp is
    reported, and a lengthening never leaves the piece shorter than it
    started. Learning the sign from a FULL-SIZE first move failed — on a
    short piece asked to grow ~160 fr, that trimmed it past zero and the
    shot vanished; before that a clamp followed by a
    shrink was misread as an inverted convention. A real log ("+0 of
    +4", "+2 of +3") fits Flame shortening on a POSITIVE offset. Safe here
    because populate calls it on the just-placed LAST segment."""
    if not delta:
        return True

    def out():
        return _frames(getattr(seg, "source_out", None))

    before = out()
    if before is None:
        return False

    def trim(off):
        """trim_tail(off), then the TOTAL displacement from `before`."""
        try:
            seg.trim_tail(off, False)
        except Exception:
            return None
        now = out()
        return (now - before) if now is not None else None

    grow, at = None, 0                  # grow: the offset sign that lengthens
    m1 = trim(1)
    if m1 is not None and m1 != 0:
        grow, at = (1 if m1 > 0 else -1), m1
    else:                               # +1 didn't move: clamped, or refused
        m2 = trim(-1)
        if m2 is not None and m2 != 0:
            grow, at = (-1 if m2 > 0 else 1), m2
    what = "extension" if delta > 0 else "trim"
    if grow is None:
        say("  ⚠ %s tail %s did not move source_out (%+d fr wanted)"
            % (label, what, delta))
        return False
    got = at
    if at != delta:
        r = trim(grow * (delta - at))
        got = r if r is not None else at
    if delta > 0 and got < 0:           # never shorter than it started
        r = trim(grow * -got)
        got = r if r is not None else got
    if got == delta:
        say("  %s tail %s %+d fr (union of uses)"
            % (label, "extended" if delta > 0 else "trimmed", delta))
        return True
    if delta > 0:
        if info is not None:
            info["clamped"] = True       # the media ends here
        say("  ⚠ %s tail extension clamped at %+d of %+d fr (media end?)"
            % (label, got, delta))
    else:
        say("  ⚠ %s tail trim stopped at %+d of %+d fr" % (label, got, delta))
    return False


def _extend_tail(seg, add, say, label=""):
    """Lengthen only (kept for callers that must never shorten)."""
    if not add or add <= 0:
        return True
    return _move_tail(seg, add, say, label)


def _widen_to_union(seg, want, checked, say, label="", info=None):
    """Fit a just-placed hub piece's TAIL to `want` = (lo, hi), the union of
    what every use displays (hub_piece_range), working from what Flame READS
    BACK on the placed piece — lengthening when a use reaches further,
    trimming when the matched clip runs past every use (pieces
    must not be too long either). Then hold it to `checked`
    (checked_use_range, what the Hub tab checks). Returns (head_short,
    tail_short) in frames — (0, 0) when every known use is covered — or None
    when the piece can't be read."""
    w_lo, w_hi = want
    # `want` is half-open; Flame's source_out is the INCLUSIVE last frame, so
    # the piece's out point must land on w_hi - 1 (read as the end, it built
    # every timewarp-tailed piece one frame long)
    last = None if w_hi is None else w_hi - 1
    s_in = _frames(getattr(seg, "source_in", None))
    out = _frames(getattr(seg, "source_out", None))
    if (last is not None and out is not None and last != out
            and (last > out or s_in is None or last >= s_in)):
        _move_tail(seg, last - out, say, label, info)
    s_in = _frames(getattr(seg, "source_in", None))
    s_out = _frames(getattr(seg, "source_out", None))
    if s_in is None or s_out is None:
        return None
    if info is not None:
        info["out"] = s_out + 1          # half-open: first frame past the piece
    if w_lo is not None and s_in > w_lo:
        say("  ⚠ %s starts %d fr after the earliest use (a timewarp reads "
            "before it) — extend the head by hand" % (label, s_in - w_lo))
    if not checked:
        return (0, 0)
    return (max(0, s_in - checked[0]), max(0, checked[1] - (s_out + 1)))


def _segment_at(track, rec_in):
    """Segment on `track` whose record_in matches rec_in (compared as text —
    PyTime equality is unreliable; pattern proven in graphic_sync)."""
    key = str(rec_in)
    try:
        for s in track.segments:
            if str(s.record_in) == key:
                return s
    except Exception:
        pass
    return None


def _ensure_video_track(sq, idx):
    """The PyTrack at video index `idx` on the first version, creating tracks
    until it exists (stacked hub pieces need real tracks above the
    base). create_track's index argument was flaky on box, so the
    append form is tried first."""
    ver = next(iter(_get(sq, "versions") or []), None)
    if ver is None:
        return None
    tracks = list(_get(ver, "tracks") or [])
    while len(tracks) <= idx:
        made = None
        for args in ((-1,), (len(tracks),), ()):
            try:
                made = ver.create_track(*args)
                break
            except Exception:
                continue
        after = list(_get(ver, "tracks") or [])
        if made is None and len(after) <= len(tracks):
            return None
        tracks = after
    return tracks[idx]


def _overwrite_at(sq, clip, rec_at, track, rate):
    """Drop `clip` onto `track` at record position `rec_at`. Record positions
    can carry a known 1-frame skew (the newborn-gap bug), but
    every placement shares that basis so RELATIVE alignment — which is what
    stacking depends on — stays exact. Returns (result, error)."""
    err = None
    mks = (lambda: flame.PyTime(frames_to_tc(rec_at, rate), "%g fps" % (rate or 24.0)),
           lambda: flame.PyTime(int(rec_at)),
           lambda: flame.PyTime(int(rec_at) + 1))
    for mk in mks:
        try:
            t = mk()
        except Exception as e:
            err = e
            continue
        try:
            if track is not None:
                return sq.overwrite(clip, t, track), None
            return sq.overwrite(clip, t), None
        except Exception as e:
            err = e
    return None, err


def _find_on_track(track, src_in):
    """The segment on `track` reading from source frame `src_in` — used to get
    a handle on the piece just placed so its tail can be widened."""
    found = None
    for s in (_get(track, "segments") or []):
        try:
            if _real_source(s) and _frames(getattr(s, "source_in", None)) == src_in:
                found = s
        except Exception:
            continue
    return found


def _real_segments(track):
    return [s for s in (_get(track, "segments") or []) if _real_source(s)]


def _placed_piece(track, src_in, n_real_before):
    """The segment a hub placement just put on `track`: by source in-point,
    else the LAST real segment on the track (list order is record order) —
    but only if the track really gained one since `n_real_before`. That count
    must be taken before anything else touches the sequence: once, shot 1
    was counted AFTER the newborn-gap delete, which cancelled the gain, so
    the shot read as 'added no segment' and was never widened (6 frames
    missing + a gap on the hub)."""
    found = _find_on_track(track, src_in)
    if found is not None:
        return found
    segs = _real_segments(track)
    return segs[-1] if len(segs) > n_real_before else None


def _vanished_pieces(placed):
    """Pieces a hub build placed that are no longer on their track: `placed` =
    [(label, track, source_in read back after widening)]. A trim past zero
    removes a segment while its python handle keeps answering with stale
    values, so the per-piece checks can't see it (a shot was once
    'placed' and then simply wasn't in the hub). Re-reading the track at the
    END of the build can. Returns the labels that are missing."""
    return [label for label, trk, s_in in placed
            if trk is not None and s_in is not None
            and _find_on_track(trk, s_in) is None]


def _find_hub_track(seq, name):
    """Track of `seq` whose name matches (cleaned, case-insensitive)."""
    want = name.strip().lower()
    for ver in (_get(seq, "versions") or []):
        for trk in (_get(ver, "tracks") or []):
            if _clean_name(trk).strip().lower() == want:
                return trk
    return None


def execute_prep_publish(seq, plan, instances, ledger_rows, log,
                         position="bottom", snapshot_as="version"):
    """Run a Prep Publish plan against the live hub sequence. Per target track:
    find-or-create the 'Publish NN' version track, then per shot: copy the live
    segment onto it at the same record position, break the copy's connection
    and duplicate its source so the snapshot is a fully standalone record of
    this moment; finally hide prior snapshots that must not re-publish.
    Every step is verified by readback and logged; failures stop the shot, not
    the run. Returns (done_count, manual_steps, errors)."""
    done, manual, errors = 0, [], []
    for grp in plan["groups"]:
        track = _find_hub_track(seq, grp["track"])
        if track is None:
            if not grp["create"]:
                log("⚠ track '%s' expected but not found — creating it" % grp["track"])
            # create a TRACK in the live version, growing from the bottom:
            # Publish 00 at index 0, 01 above it, live stays on top (default
            # layout; 'top' preference appends instead)
            live_seg = instances[ledger_rows[grp["shots"][0]]["hub_idx"]]["seg"]
            ver = getattr(getattr(live_seg, "parent", None), "parent", None)
            track, errs = None, []
            if snapshot_as == "version":
                # Flame's named action "Insert Version/Track
                # Below Focus" — execute_shortcut drives it by DESCRIPTION
                # (keystroke binding irrelevant). Focus should be on the live
                # track; verified by version-count readback.
                try:
                    before_v = list(_get(seq, "versions") or [])
                    if flame.execute_shortcut("Insert Version/Track Below Focus"):
                        now_v = list(_get(seq, "versions") or [])
                        new_v = [v for v in now_v
                                 if all(v is not b for b in before_v)]
                        if new_v:
                            tks = list(_get(new_v[0], "tracks") or [])
                            track = tks[0] if tks else None
                            if track is not None:
                                log("version inserted BELOW focus via native "
                                    "shortcut for %s" % grp["track"])
                except Exception as e:
                    errs.append("below-focus shortcut: %s" % e)
            if track is None and snapshot_as == "version":
                log("below-focus shortcut did NOT take (focus the live track "
                    "in the open sequence and retry) — falling back to "
                    "create_version (lands ABOVE)")
                # the workflow's real container: a NEW VERSION (a plain
                # track is not a version track)
                try:
                    ver2 = seq.create_version()
                    tks = list(_get(ver2, "tracks") or [])
                    if not tks:
                        vers = list(_get(seq, "versions") or [])
                        tks = list(_get(vers[-1], "tracks") or []) if vers else []
                    track = tks[0] if tks else None
                    if track is not None:
                        log("created a new VERSION for %s (Flame decides where "
                            "versions stack — drag if needed)" % grp["track"])
                except Exception as e:
                    errs.append("create_version: %s" % e)
            if track is None:
                n_pub = sum(1 for t in (_get(ver, "tracks") or [])
                            if publish_track_index(_clean_name(t)) is not None)
                tries = ([n_pub, n_pub + 1, -1] if position == "bottom" else [-1])
                for idx in tries:
                    try:
                        track = ver.create_track(idx)
                        if track is not None:
                            if idx == -1 and position == "bottom":
                                log("⚠ %s landed on TOP (bottom insert "
                                    "rejected) — drag it down" % grp["track"])
                            break
                    except Exception as e:
                        errs.append("idx %s: %s" % (idx, e))
            if track is None:
                errors.append("snapshot container for %s failed (%s)"
                              % (grp["track"], "; ".join(errs)))
                continue
            if _set_flame_attr(track, "name", grp["track"]):
                log("created version track '%s'" % grp["track"])
            else:
                manual.append("rename the new version track to '%s' by hand"
                              % grp["track"])
        reel = _scratch_reel()
        for si in grp["shots"]:
            r = ledger_rows[si]
            seg = instances[r["hub_idx"]]["seg"]
            label = r["hub_name"] or r["camera"]
            clip = None
            try:
                rec_in = seg.record_in
                clip = seg.copy_to_media_panel(reel)
                seq.overwrite(clip, rec_in, track)
                placed = _segment_at(track, rec_in)
                if placed is None:
                    raise RuntimeError("placed segment not found at %s" % rec_in)
                try:
                    placed.remove_connection()
                except Exception:
                    pass                        # not connected -> fine
                placed.duplicate_source()       # 'source no longer shared'
                # readback: the snapshot must match the live cut length
                want = _frames(getattr(seg, "record_duration", None))
                got = _frames(getattr(placed, "record_duration", None))
                if want is not None and got is not None and want != got:
                    errors.append("%s: snapshot length %s ≠ live %s — check by eye"
                                  % (label, got, want))
                else:
                    done += 1
                    log("snapshot %s → %s" % (label, grp["track"]))
            except Exception as e:
                errors.append("%s: %s" % (label, e))
            finally:
                if clip is not None:
                    okd, errd = _safe_delete(clip)
                    if not okd:
                        log("⚠ temp clip for %s left in the reel (%s) — "
                            "delete by hand" % (label, errd))
        for name in grp["hide"]:
            hseg = None
            try:
                for s in (track.segments or []):
                    if _seg_name(s) == name:
                        hseg = s
                        break
            except Exception:
                pass
            if hseg is None:
                manual.append("hide '%s' on %s by hand (not found by name)"
                              % (name, grp["track"]))
            elif _set_flame_attr(hseg, "hidden", True):
                log("hid prior snapshot '%s' on %s" % (name, grp["track"]))
            else:
                manual.append("hide '%s' on %s by hand (hidden not writable)"
                              % (name, grp["track"]))
        if _set_flame_attr(seq, "primary_track", track):
            log("PRIMARY track set to '%s' — run the native publish now, then "
                "restore your primary track" % grp["track"])
        else:
            manual.append("set the PRIMARY track to '%s' before the native "
                          "publish, and restore it after" % grp["track"])
    return done, manual, errors


# ====================================================================
# GUI
# ====================================================================

# Inventory column model: (key, header, toggleable, default_visible, resize).
INV_COLS = (
    ("seq",     "Sequence", False, True,  "stretch"),
    ("name",    "Name",     True,  True,  "fit"),
    ("camera",  "Camera",   True,  True,  "fit"),
    ("source",  "Source",   False, True,  "stretch"),
    ("src_in",  "Src In",   True,  True,  "fit"),
    ("src_out", "Src Out",  True,  True,  "fit"),
    ("dur",     "Dur",      True,  True,  "fit"),
    ("rec_in",  "Rec In",   True,  True,  "fit"),
    ("role",    "Role",     True,  True,  "fit"),
    ("dup",     "Dup",      True,  True,  "fit"),
    ("stack",   "Stack",    True,  True,  "fit"),
    ("warp",    "Warp",     True,  True,  "fit"),
    ("long",    "Long",     True,  True,  "fit"),
)
INV_UNITS = ("Timecode", "Frames")

ACCENT = "#ff7a45"      # CCM accent (warm; distinct from GFX Sync's cyan)
ORPHAN_EDGE = "#ffe14d"  # not in the hub — yellow (red reads as "unlinked" in Flame)

STYLE = """
QDialog, QWidget { background-color: #1e1e1e; color: #cccccc;
    font-family: 'Helvetica Neue', Helvetica, Arial, sans-serif; font-size: 13px; }
QLabel { color: #cccccc; }
QLabel#header { color: #ffffff; font-size: 18px; font-weight: bold; letter-spacing: 2px; }
QLabel#sub { color: #777777; font-size: 11px; letter-spacing: 1px; }
QLabel#ver { color: #ff7a45; font-size: 10px; font-weight: bold; letter-spacing: 1px;
    border: 1px solid #ff7a45; border-radius: 3px; padding: 1px 6px; }
QLabel#wip { color: #ffd27a; background-color: #2e2410; font-size: 12px;
    border: 1px solid #a8741a; border-radius: 4px; padding: 6px 10px; }
QTabWidget::pane { border: 1px solid #333333; border-radius: 6px; top: -1px; }
QTabBar::tab { background: #232323; color: #aaaaaa; padding: 7px 16px;
    border: 1px solid #333333; border-bottom: none;
    border-top-left-radius: 5px; border-top-right-radius: 5px; }
QTabBar::tab:selected { background: #1e1e1e; color: #ffffff; }
QGroupBox { color: #777777; font-size: 10px; font-weight: bold; letter-spacing: 1.5px;
    border: 1px solid #333333; border-radius: 6px; margin-top: 12px; padding-top: 12px; }
QGroupBox::title { subcontrol-origin: margin; subcontrol-position: top left; padding: 0 8px; left: 12px; }
QTableWidget { background-color: #141414; color: #cccccc; border: 1px solid #2a2a2a;
    border-radius: 4px; gridline-color: #2a2a2a; }
QTableWidget::item:selected { background-color: #4a2a14; color: #ffffff; }
QHeaderView::section { background-color: #2a2a2a; color: #aaaaaa; border: none;
    border-right: 1px solid #404040; padding: 5px; font-size: 11px; font-weight: bold; }
QSplitter::handle { background-color: #333333; border-radius: 2px; }
QSplitter::handle:horizontal { width: 10px; }
QSplitter::handle:vertical { height: 10px; }
QSplitter::handle:hover { background-color: #ff7a45; }
QListWidget { background-color: #141414; color: #cccccc; border: 1px solid #2a2a2a; border-radius: 4px; }
QListWidget::item:selected { background-color: #4a2a14; color: #ffffff; }
QPlainTextEdit, QLineEdit, QComboBox { background-color: #141414; color: #cccccc;
    border: 1px solid #2a2a2a; border-radius: 4px; padding: 6px; }
QComboBox QAbstractItemView { background-color: #141414; color: #cccccc; selection-background-color: #4a2a14; }
QSpinBox { background-color: #141414; color: #cccccc; border: 1px solid #2a2a2a; border-radius: 4px; padding: 4px; }
QPushButton { background-color: #2d2d2d; color: #cccccc; border: 1px solid #444444;
    border-radius: 4px; padding: 7px 16px; }
QPushButton:hover { background-color: #383838; border-color: #666666; }
QPushButton:disabled { color: #555555; border-color: #2a2a2a; }
QPushButton#primary { background-color: #ff7a45; color: #000000; border: none; font-weight: bold; }
QPushButton#primary:hover { background-color: #ff9466; }
QPlainTextEdit#logbox { color: #ff7a45; font-family: 'Menlo','Consolas',monospace; font-size: 11px; }
"""


def _init_table_resize(table, stretch_cols=()):
    """EVERY column user-draggable (a mid-table Stretch
    section made whole tables feel locked). All Interactive; the last section
    stretches to absorb slack but stays draggable via its left edge."""
    _ = stretch_cols        # kept for call-site compatibility
    # names end in what tells them apart (…_0140) — cut the middle, not the
    # end, when a column is narrow (the preview panels take width)
    table.setTextElideMode(QtCore.Qt.ElideMiddle)
    h = table.horizontalHeader()
    h.setSectionResizeMode(QtWidgets.QHeaderView.Interactive)
    h.setStretchLastSection(True)
    h.setMinimumSectionSize(28)


def _squish(label):
    """Status labels carry long dynamic text; without this Qt uses their full
    text as the WINDOW's minimum width (the dialog couldn't shrink and
    walked off a MacBook screen). Let them elide/clip instead."""
    label.setSizePolicy(QtWidgets.QSizePolicy.Ignored, QtWidgets.QSizePolicy.Preferred)
    label.setMinimumWidth(0)
    return label


def _autosize_once(table):
    """Fit columns to content on the FIRST render only — later scans must never
    stomp widths the user has dragged."""
    if not table.property("ccm_sized"):
        table.resizeColumnsToContents()
        table.setProperty("ccm_sized", True)


class RulerWidget(QtWidgets.QWidget):
    """A horizontal source-extent bar with shaded bands. Used both for the
    Coverage Ruler (consumed bands + dead zones over one source) and for the
    Connection Map's per-sequence placement rows. Bands are (lo, hi, color, tip)
    in the same frame space as `extent`=(lo, hi)."""

    def __init__(self, parent=None):
        super().__init__(parent)
        self.extent = (0, 0)
        self.bands = []
        self.ticks = True
        self.setMinimumHeight(34)
        self.setSizePolicy(QtWidgets.QSizePolicy.Expanding, QtWidgets.QSizePolicy.Fixed)

    def set_data(self, extent, bands, ticks=True):
        self.extent = extent if (extent and extent[0] is not None) else (0, 0)
        self.bands = bands or []
        self.ticks = ticks
        self.update()

    def _x(self, f, x0, w):
        lo, hi = self.extent
        span = (hi - lo) or 1
        return int(x0 + (f - lo) / float(span) * w)

    def paintEvent(self, _ev):
        p = QtGui.QPainter(self)
        p.setRenderHint(QtGui.QPainter.Antialiasing, False)
        r = self.rect().adjusted(6, 6, -6, -6)
        x0, y0, w, h = r.x(), r.y(), r.width(), r.height()
        p.fillRect(self.rect(), QtGui.QColor("#1e1e1e"))
        # base track
        p.fillRect(x0, y0 + h // 2 - 8, w, 16, QtGui.QColor("#101010"))
        p.setPen(QtGui.QColor("#333333"))
        p.drawRect(x0, y0 + h // 2 - 8, w, 16)
        lo, hi = self.extent
        if hi <= lo:
            p.setPen(QtGui.QColor("#666666"))
            p.drawText(self.rect(), QtCore.Qt.AlignCenter, "no extent")
            p.end()
            return
        for (a, b, color, _tip) in self.bands:
            if a is None or b is None:
                continue
            xa, xb = self._x(a, x0, w), self._x(max(b, a + 1), x0, w)
            p.fillRect(xa, y0 + h // 2 - 8, max(2, xb - xa), 16, QtGui.QColor(color))
        if self.ticks:
            p.setPen(QtGui.QColor("#777777"))
            f = self.font(); f.setPointSize(8); p.setFont(f)
            p.drawText(x0, y0, 80, 12, QtCore.Qt.AlignLeft, str(lo))
            p.drawText(x0 + w - 80, y0, 80, 12, QtCore.Qt.AlignRight, str(hi))
        p.end()


class ShotInspector(QtWidgets.QWidget):
    """The overlap inspector for one logical shot: a shared source-frame axis
    with the merged coverage summary + piece brackets on top and one lane per
    timeline instance below, showing exactly which slice of the source each
    timeline uses. Click/drag anywhere to scrub a cursor; cursorMoved(frame)
    fires — the dialog updates the TC/who-uses readout and the FIXED preview
    panel beside the diagram (pictures live there, not on the canvas, so they
    don't chase the playhead)."""

    cursorMoved = QtCore.Signal(int)
    groupToggled = QtCore.Signal(str)     # twirl a folded "×N" use open/shut

    LABEL_W = 300      # narrower widths truncated names past usefulness
    HEAD_H = 16
    SUM_H = 30
    LANE_H = 24

    def __init__(self, parent=None):
        super().__init__(parent)
        self.extent = (0, 0)
        self.merged, self.dead, self.pieces = [], [], []
        self.lanes = []          # {label, a, b, color, range_text, is_hub, warp}
        self.cursor = None       # source frame under the scrub cursor
        self.setSizePolicy(QtWidgets.QSizePolicy.Expanding, QtWidgets.QSizePolicy.Fixed)

    def set_data(self, extent, merged, dead, pieces, lanes):
        self.extent = extent if (extent and extent[0] is not None) else (0, 0)
        self.merged, self.dead = merged or [], dead or []
        self.pieces, self.lanes = pieces or [], lanes or []
        lo, hi = self.extent
        if self.cursor is not None and not (lo <= self.cursor < hi):
            self.cursor = None
        self.setFixedHeight(self.HEAD_H + self.SUM_H
                            + self.LANE_H * len(self.lanes) + 10)
        self.update()

    def set_cursor(self, f):
        """Move the scrub cursor WITHOUT emitting — the preview panel's own
        scrub bar drives the diagram through this."""
        lo, hi = self.extent
        self.cursor = f if (f is not None and lo <= f < hi) else None
        self.update()

    # ---- geometry ----------------------------------------------------
    def _axis(self):
        x0 = self.LABEL_W
        return x0, max(1, self.rect().width() - x0 - 14)

    def _x(self, f):
        lo, hi = self.extent
        x0, w = self._axis()
        return int(x0 + (f - lo) / float((hi - lo) or 1) * w)

    def _f(self, x):
        lo, hi = self.extent
        x0, w = self._axis()
        return int(round(lo + (x - x0) / float(w) * (hi - lo)))

    # ---- scrub -------------------------------------------------------
    def _lane_at(self, y):
        i = int((y - self.HEAD_H - self.SUM_H) // self.LANE_H)
        return i if 0 <= i < len(self.lanes) else None

    def mousePressEvent(self, ev):
        pos = ev.position() if hasattr(ev, "position") else ev.pos()
        x, y = pos.x(), pos.y()
        if x < self.LABEL_W:
            ln = self._lane_at(y)
            key = self.lanes[ln].get("toggle_key") if ln is not None else None
            if key:
                self.groupToggled.emit(key)
                return
        self._scrub(ev)

    def mouseMoveEvent(self, ev):
        if ev.buttons() & QtCore.Qt.LeftButton:
            self._scrub(ev)

    def _scrub(self, ev):
        lo, hi = self.extent
        if hi <= lo:
            return
        x = ev.position().x() if hasattr(ev, "position") else ev.x()
        f = max(lo, min(hi - 1, self._f(x)))
        if f != self.cursor:
            self.cursor = f
            self.update()
            self.cursorMoved.emit(f)

    # ---- paint -------------------------------------------------------
    def paintEvent(self, _ev):
        p = QtGui.QPainter(self)
        p.fillRect(self.rect(), QtGui.QColor("#1e1e1e"))
        lo, hi = self.extent
        x0, w = self._axis()
        if hi <= lo:
            p.setPen(QtGui.QColor("#666666"))
            p.drawText(self.rect(), QtCore.Qt.AlignCenter,
                       "Select a shot with source ranges.")
            p.end()
            return
        small = self.font(); small.setPointSize(9)
        p.setFont(small)
        # header: extent bounds
        p.setPen(QtGui.QColor("#777777"))
        p.drawText(x0, 0, 120, self.HEAD_H, QtCore.Qt.AlignLeft | QtCore.Qt.AlignVCenter, str(lo))
        p.drawText(x0 + w - 120, 0, 120, self.HEAD_H,        # half-open: last frame
                   QtCore.Qt.AlignRight | QtCore.Qt.AlignVCenter, str(hi - 1))
        y = self.HEAD_H
        # summary track: consumed + dead + piece brackets
        p.fillRect(x0, y + 6, w, 14, QtGui.QColor("#101010"))
        for a, b in self.dead:
            p.fillRect(self._x(a), y + 6, max(2, self._x(b) - self._x(a)), 14,
                       QtGui.QColor("#333333"))
        for a, b in self.merged:
            p.fillRect(self._x(a), y + 6, max(2, self._x(b) - self._x(a)), 14,
                       QtGui.QColor("#ff7a45"))
        p.setPen(QtGui.QColor("#777777"))
        p.drawText(2, y, self.LABEL_W - 10, self.SUM_H,
                   QtCore.Qt.AlignRight | QtCore.Qt.AlignVCenter, "coverage")
        pen = QtGui.QPen(QtGui.QColor("#ffffff")); pen.setWidth(1)
        for k, (a, b) in enumerate(self.pieces):
            xa, xb = self._x(a), self._x(b)
            p.setPen(pen)
            p.drawLine(xa, y + 24, xb, y + 24)
            p.drawLine(xa, y + 21, xa, y + 24)
            p.drawLine(xb, y + 21, xb, y + 24)
            label = ""
            n = k + 1
            while n > 0:
                n, r = divmod(n - 1, 26)
                label = chr(65 + r) + label
            p.drawText(xa + 3, y + 16, 24, 12, QtCore.Qt.AlignLeft, label)
        y += self.SUM_H
        # lanes: one per timeline instance
        for ln in self.lanes:
            a, b = ln.get("a"), ln.get("b")
            hit = (self.cursor is not None and a is not None and b is not None
                   and a <= self.cursor < b)
            p.setPen(QtGui.QColor("#ffffff" if hit else
                                  ("#8fb4cc" if ln.get("is_hub") else "#aaaaaa")))
            fm = QtGui.QFontMetrics(small)
            p.drawText(2, y, self.LABEL_W - 10, self.LANE_H,
                       QtCore.Qt.AlignRight | QtCore.Qt.AlignVCenter,
                       fm.elidedText(ln.get("label", ""), QtCore.Qt.ElideMiddle,
                                     self.LABEL_W - 12))
            p.fillRect(x0, y + self.LANE_H // 2, w, 1, QtGui.QColor("#262626"))
            if a is not None and b is not None:
                xa, xb = self._x(a), self._x(max(b, a + 1))
                color = ln.get("color") or "#ff7a45"
                if hit:
                    color = {"#ff7a45": "#ff9466", "#c080ff": "#d4a6ff",
                             "#6f9fc8": "#8fb4cc"}.get(color, color)
                p.fillRect(xa, y + 6, max(2, xb - xa), self.LANE_H - 12,
                           QtGui.QColor(color))
                rt = ln.get("range_text", "")
                if rt:
                    p.setPen(QtGui.QColor("#999999"))
                    if xb + 6 + fm.horizontalAdvance(rt) < x0 + w:
                        p.drawText(xb + 6, y, x0 + w - xb - 6, self.LANE_H,
                                   QtCore.Qt.AlignLeft | QtCore.Qt.AlignVCenter, rt)
                    else:
                        p.drawText(x0, y, xa - x0 - 6, self.LANE_H,
                                   QtCore.Qt.AlignRight | QtCore.Qt.AlignVCenter, rt)
            y += self.LANE_H
        # scrub cursor over everything
        if self.cursor is not None:
            cx = self._x(self.cursor)
            p.setPen(QtGui.QPen(QtGui.QColor("#ffffff"), 1))
            p.drawLine(cx, self.HEAD_H, cx, self.height() - 4)
        p.end()


class ScrubBar(QtWidgets.QWidget):
    """The strip under every preview picture: the baked source range, the
    frames the spots actually show in orange, a white playhead. Click or drag
    to scrub; the wheel steps a frame (shift = 10)."""

    frameChosen = QtCore.Signal(int)
    H = 18

    def __init__(self, parent=None):
        super().__init__(parent)
        self.lo = self.hi = 0
        self.uses = []
        self.frame = None
        self.setFixedHeight(self.H)
        self.setToolTip("Drag to scrub · wheel = 1 frame (shift = 10) · "
                        "orange = frames the spots use")

    def set_range(self, lo, hi, uses=()):
        self.lo, self.hi = lo, hi
        self.uses = list(uses or ())
        self.update()

    def set_frame(self, f):
        self.frame = f
        self.update()

    def _x(self, f):
        w = max(1, self.width() - 2)
        return 1 + (f - self.lo) / float((self.hi - self.lo) or 1) * w

    def _pick(self, ev):
        if self.hi <= self.lo:
            return
        x = ev.position().x() if hasattr(ev, "position") else ev.x()
        w = max(1, self.width() - 2)
        f = self.lo + int((x - 1) / float(w) * (self.hi - self.lo))
        self.frameChosen.emit(max(self.lo, min(self.hi - 1, f)))

    def mousePressEvent(self, ev):
        self._pick(ev)

    def mouseMoveEvent(self, ev):
        if ev.buttons() & QtCore.Qt.LeftButton:
            self._pick(ev)

    def wheelEvent(self, ev):
        if self.hi <= self.lo or self.frame is None:
            return
        d = ev.angleDelta().y() or ev.angleDelta().x()
        n = 10 if ev.modifiers() & QtCore.Qt.ShiftModifier else 1
        self.frameChosen.emit(max(self.lo, min(self.hi - 1,
                                               self.frame + (n if d < 0 else -n))))
        ev.accept()

    def paintEvent(self, _ev):
        p = QtGui.QPainter(self)
        p.fillRect(self.rect(), QtGui.QColor("#101010"))
        if self.hi > self.lo:
            p.fillRect(QtCore.QRectF(1, 5, self.width() - 2, self.H - 10),
                       QtGui.QColor("#2c2c2c"))
            for a, b in self.uses:
                a, b = max(a, self.lo), min(b, self.hi)
                if b > a:
                    p.fillRect(QtCore.QRectF(self._x(a), 5,
                                             max(1.5, self._x(b) - self._x(a)),
                                             self.H - 10), QtGui.QColor(ACCENT))
            if self.frame is not None:
                x = self._x(self.frame + 0.5)
                p.fillRect(QtCore.QRectF(x - 1, 0, 2, self.H), QtGui.QColor("#ffffff"))
        p.end()


class ShotPreview(QtWidgets.QWidget):
    """The ONE preview panel: Timelines, Connections, Hub
    and Ledger all show a shot with it. A baked filmstrip you can SCRUB — the
    bar under the picture, a drag across the picture, the wheel on the bar,
    ←/→ keys (shift = 10), Home/End — and ZOOM (past Fit the picture pans).
    The dialog owns baking and the per-shot disk cache; this widget only
    shows frames: file[k] is source frame base + k (preserve_handle=True)."""

    frameChanged = QtCore.Signal(int)      # the user scrubbed HERE
    rebakeRequested = QtCore.Signal()
    bakeAllRequested = QtCore.Signal()

    ZOOMS = ("Fit", "150%", "200%", "300%", "400%")

    def __init__(self, parent=None, show_title=True):
        super().__init__(parent)
        self.files, self.base, self.rate = [], 0, 24.0
        self.frame = None
        self._pix = {}
        v = QtWidgets.QVBoxLayout(self)
        v.setContentsMargins(0, 0, 0, 0)
        v.setSpacing(4)
        self.title = _squish(QtWidgets.QLabel(""))
        self.title.setStyleSheet("color:#ffffff; font-weight:bold;")
        self.title.setVisible(show_title)
        v.addWidget(self.title)
        self.img = QtWidgets.QLabel("")
        self.img.setAlignment(QtCore.Qt.AlignCenter)
        self.img.setStyleSheet("color:#666666;")
        self.scroll = QtWidgets.QScrollArea()
        self.scroll.setWidget(self.img)
        self.scroll.setWidgetResizable(False)     # zoom past Fit pans
        self.scroll.setAlignment(QtCore.Qt.AlignCenter)
        self.scroll.setMinimumSize(160, 90)
        self.scroll.setStyleSheet(
            "QScrollArea { background-color:#0d0d0d; border:1px solid #2a2a2a;"
            " border-radius:4px; }")
        self.scroll.viewport().installEventFilter(self)
        self.img.installEventFilter(self)
        v.addWidget(self.scroll, 1)
        self.bar = ScrubBar()
        self.bar.frameChosen.connect(self._user_frame)
        v.addWidget(self.bar)
        row = QtWidgets.QHBoxLayout()
        self.cap = _squish(QtWidgets.QLabel(""))
        self.cap.setStyleSheet("color:#999999; font-family:'Menlo','Consolas',"
                               "monospace; font-size:11px;")
        row.addWidget(self.cap, 1)
        self.zoom = QtWidgets.QComboBox()
        self.zoom.addItems(self.ZOOMS)
        self.zoom.setToolTip("Zoom the picture — past Fit it pans (scroll bars "
                             "or the wheel over the picture).")
        self.zoom.currentTextChanged.connect(lambda _t: self._render())
        row.addWidget(self.zoom)
        small = "padding: 3px 9px;"
        self.b_rebake = QtWidgets.QPushButton("Re-bake")
        self.b_rebake.setStyleSheet(small)
        self.b_rebake.setToolTip(
            "Wipe this shot's cached frames and bake them again — after its "
            "media changed.")
        self.b_rebake.clicked.connect(self.rebakeRequested)
        row.addWidget(self.b_rebake)
        self.b_all = QtWidgets.QPushButton("Bake All")
        self.b_all.setStyleSheet(small)
        self.b_all.setToolTip(
            "Bake a preview for every shot in the scan that doesn't have one "
            "yet — a few seconds each, once. After that every shot opens "
            "instantly, in every tab.")
        self.b_all.clicked.connect(self.bakeAllRequested)
        row.addWidget(self.b_all)
        v.addLayout(row)
        self.setFocusPolicy(QtCore.Qt.ClickFocus)
        self.set_message("Pick a shot.")

    # ---- content -----------------------------------------------------------
    def set_message(self, text, title=None):
        self.files, self._pix, self.frame = [], {}, None
        if title is not None:
            self.title.setText(title)
        self.img.setPixmap(QtGui.QPixmap())
        self.img.setText(text)
        self.img.adjustSize()
        self.bar.set_range(0, 0)
        self.bar.set_frame(None)
        self.cap.setText("")

    def set_strip(self, files, base, rate=24.0, uses=(), title="", frame=None):
        """Load a baked strip; `frame` is kept when it is inside it, else the
        picture opens in the middle of what the spots use."""
        self.files, self.base = list(files or []), int(base)
        self.rate = rate or 24.0
        self._pix = {}
        self.title.setText(title)
        hi = self.base + len(self.files)
        self.bar.set_range(self.base, hi, uses)
        if frame is None or not (self.base <= frame < hi):
            inside = [(max(a, self.base), min(b, hi)) for a, b in (uses or ())
                      if min(b, hi) > max(a, self.base)]
            lo_u = min((a for a, _b in inside), default=self.base)
            hi_u = max((b for _a, b in inside), default=hi)
            frame = (lo_u + hi_u) // 2
        self.show_frame(frame)

    def has_strip(self):
        return bool(self.files)

    def show_frame(self, f):
        """Show source frame f, clamped to the strip. Never emits — outside
        drivers (the Timelines diagram) call this."""
        if not self.files or f is None:
            return
        f = max(self.base, min(self.base + len(self.files) - 1, int(f)))
        self.frame = f
        self.bar.set_frame(f)
        self._render()

    def _user_frame(self, f):
        if not self.files:
            return
        self.setFocus()
        self.show_frame(f)
        self.frameChanged.emit(self.frame)

    def step(self, n):
        if self.frame is not None:
            self._user_frame(self.frame + n)

    def _pixmap(self, k):
        if not (0 <= k < len(self.files)):
            return None
        pm = self._pix.get(k)
        if pm is None:
            pm = QtGui.QPixmap(self.files[k])
            self._pix[k] = pm
        return None if pm.isNull() else pm

    def _render(self):
        if not self.files or self.frame is None:
            return
        k = self.frame - self.base
        self.cap.setText("%s  %d/%d" % (frames_to_tc(self.frame, self.rate),
                                         k + 1, len(self.files)))
        self.cap.setToolTip("source frame %d" % self.frame)
        pix = self._pixmap(k)
        if pix is None:
            self.img.setPixmap(QtGui.QPixmap())
            self.img.setText("frame unreadable")
            self.img.adjustSize()
            return
        z = self.zoom.currentText()
        try:
            scale = 1.0 if z == "Fit" else int(z.rstrip("%")) / 100.0
        except ValueError:
            scale = 1.0
        vp = self.scroll.viewport().size()
        target = QtCore.QSize(max(64, int((vp.width() - 2) * scale)),
                              max(36, int((vp.height() - 2) * scale)))
        scaled = pix.scaled(target, QtCore.Qt.KeepAspectRatio,
                            QtCore.Qt.SmoothTransformation)
        self.img.setText("")
        self.img.setPixmap(scaled)
        self.img.resize(scaled.size())

    # ---- interaction -------------------------------------------------------
    def resizeEvent(self, ev):
        super().resizeEvent(ev)
        self._render()

    def keyPressEvent(self, ev):
        k = ev.key()
        n = 10 if ev.modifiers() & QtCore.Qt.ShiftModifier else 1
        if not self.files:
            super().keyPressEvent(ev)
        elif k == QtCore.Qt.Key_Left:
            self.step(-n)
        elif k == QtCore.Qt.Key_Right:
            self.step(n)
        elif k == QtCore.Qt.Key_Home:
            self._user_frame(self.base)
        elif k == QtCore.Qt.Key_End:
            self._user_frame(self.base + len(self.files) - 1)
        else:
            super().keyPressEvent(ev)

    def eventFilter(self, obj, ev):
        """A drag across the picture scrubs, like a player: the panel's width
        is the whole baked strip. Picture and viewport both report here (the
        picture passes clicks it doesn't use up to the viewport)."""
        vp = self.scroll.viewport()
        if obj in (vp, self.img) and self.files:
            t = ev.type()
            if t == QtCore.QEvent.MouseButtonPress or (
                    t == QtCore.QEvent.MouseMove
                    and ev.buttons() & QtCore.Qt.LeftButton):
                pt = ev.position().toPoint() if hasattr(ev, "position") else ev.pos()
                if obj is self.img:
                    pt = self.img.mapTo(vp, pt)
                w = max(1, vp.width())
                self._user_frame(self.base + int(pt.x() / float(w) * len(self.files)))
                return True
        return super().eventFilter(obj, ev)


class TimelinesView(QtWidgets.QWidget):
    """The Timelines map: every sequence in scope as one lane (hub pinned
    first), ALL of its tracks stacked inside the lane, every segment a named
    block positioned by record time on ONE shared scale — the longest sequence
    spans the full width and every other lane falls proportionally short
    (never per-lane fit). Single-click = blockClicked (the dialog highlights
    that logical shot in every lane and docks the Shot Inspector below);
    double-click = blockDoubleClicked (the dialog moves Flame's actual
    timeline there). Zoom is applied by the dialog via setMinimumWidth — the
    surrounding scroll area grows a horizontal scrollbar."""

    blockClicked = QtCore.Signal(int)
    blockDoubleClicked = QtCore.Signal(int)
    laneToggled = QtCore.Signal(str)             # sequence name
    trackMenuRequested = QtCore.Signal(str, str, QtCore.QPoint)
    blockMenuRequested = QtCore.Signal(int, QtCore.QPoint)

    LABEL_W = 300      # narrower widths truncated names past usefulness
    HEAD_H = 20
    TRACK_H = 24
    LANE_GAP = 10
    VERSION_GAP = 8      # between VERSIONS; tracks inside a version are tight

    _BRIGHT = {"#ff7a45": "#ffa365", "#c080ff": "#d4a6ff",
               "#6f9fc8": "#9cc4e4", "#4a6a85": "#6f9fc8",
               "#3d3a37": "#55504b"}

    def __init__(self, parent=None):
        super().__init__(parent)
        self.rows = []
        self.lanes = []
        self.max_dur = 0
        self.rate = 24.0
        self.strays = set()      # undecided split sources — red
        self.variants = set()    # acknowledged intentional variants — amber
        self.orphans = set()     # source absent from the hub — yellow, dashed
        self.collapsed = set()   # sequences folded to a single summary row
        self.highlight = set()
        self.labels_segment = False
        self._hits = []          # [(QRect, inv index)] rebuilt every paint
        self._track_hits = []    # [(QRect, seq, track_id)] for the role menu
        self._lane_hits = []     # [(QRect, seq)] for fold/unfold
        self.setSizePolicy(QtWidgets.QSizePolicy.Expanding,
                           QtWidgets.QSizePolicy.Fixed)

    def set_data(self, rows, layout, strays=None, rate=24.0,
                 variants=None, orphans=None):
        self.rows = rows or []
        self.lanes = (layout or {}).get("lanes", [])
        self.max_dur = (layout or {}).get("max_dur", 0)
        self.strays = strays or set()
        self.variants = variants or set()
        self.orphans = orphans or set()
        self.rate = rate or 24.0
        self._relayout()

    def _relayout(self):
        h = self.HEAD_H + 8
        for ln in self.lanes:
            h += self._lane_height(ln) + self.LANE_GAP
        self.setFixedHeight(max(80, h))
        self.update()

    def _lane_rows(self, ln):
        """[(track, y offset)] within a lane. Tracks belonging to the same
        VERSION stack tight; a gap opens between versions, because a version
        track and a plain track are different animals in Flame and reading
        them as one wall of rows was misleading."""
        out, y, prev = [], 0, None
        for t in ln["tracks"]:
            v = t.get("version")
            if prev is not None and v != prev:
                y += self.VERSION_GAP
            out.append((t, y))
            y += self.TRACK_H
            prev = v
        return out

    def _lane_height(self, ln):
        if ln["seq"] in self.collapsed:
            return self.TRACK_H
        rows = self._lane_rows(ln)
        return (rows[-1][1] + self.TRACK_H) if rows else self.TRACK_H

    def set_collapsed(self, seqs):
        self.collapsed = set(seqs or ())
        self._relayout()

    def set_highlight(self, indices):
        self.highlight = set(indices or [])
        self.update()

    def set_label_mode(self, prefer_segment):
        self.labels_segment = bool(prefer_segment)
        self.update()

    # ---- geometry ----------------------------------------------------
    def _axis(self):
        x0 = self.LABEL_W
        return x0, max(1, self.rect().width() - x0 - 14)

    def _x(self, f):
        x0, w = self._axis()
        return int(x0 + f / float(self.max_dur or 1) * w)

    def _hit(self, pos):
        for rect, idx in self._hits:
            if rect.contains(pos):
                return idx
        return None

    # ---- interaction ---------------------------------------------------
    def mousePressEvent(self, ev):
        pos = ev.position().toPoint() if hasattr(ev, "position") else ev.pos()
        if ev.button() == QtCore.Qt.RightButton:
            for rect, seq, tid in getattr(self, "_track_hits", []):
                if rect.contains(pos):
                    # map here, not from the event: Qt6 dropped globalPos()
                    self.trackMenuRequested.emit(seq, tid,
                                                 self.mapToGlobal(pos))
                    return
            idx = self._hit(pos)
            if idx is not None:
                self.blockMenuRequested.emit(idx, self.mapToGlobal(pos))
            return
        for rect, seq in getattr(self, "_lane_hits", []):
            if rect.contains(pos):
                self.laneToggled.emit(seq)
                return
        idx = self._hit(pos)
        if idx is not None:
            self.blockClicked.emit(idx)

    def mouseDoubleClickEvent(self, ev):
        pos = ev.position().toPoint() if hasattr(ev, "position") else ev.pos()
        idx = self._hit(pos)
        if idx is not None:
            self.blockDoubleClicked.emit(idx)

    def event(self, ev):
        if ev.type() == QtCore.QEvent.ToolTip:
            idx = self._hit(ev.pos())
            if idx is not None and idx < len(self.rows):
                d = self.rows[idx]
                bits = ["%s  ·  %s" % (d.get("shot") or d.get("name") or "?",
                                       d.get("seq", "")),
                        "track %s" % (d.get("track_name") or d.get("track_id")),
                        "rec %s +%s" % (d.get("rec_in_tc"), d.get("rec_dur_tc")),
                        "src %s–%s" % (d.get("src_in_tc"), d.get("src_out_tc"))]
                if d.get("is_hub"):
                    bits.append("HUB" + ("  (Publish %02d snapshot)" % d["pub_nn"]
                                         if d.get("pub_nn") is not None else ""))
                if d.get("role") == "ref":
                    bits.append("reference picture")
                if d.get("timewarp"):
                    pct = (d.get("tw_model") or {}).get("pct")
                    bits.append("TIMEWARP" + (" %.4g%%" % pct if pct else ""))
                if idx in self.orphans:
                    bits.append("ORPHAN — this source is NOT in the hub "
                                "(publish/relink would skip it)")
                if idx in self.strays:
                    bits.append("SPLIT SOURCE — same shot, separate real "
                                "source (undecided)")
                elif idx in self.variants:
                    bits.append("VARIANT — separate source, acknowledged as "
                                "intentional")
                QtWidgets.QToolTip.showText(ev.globalPos(), "\n".join(bits), self)
            else:
                QtWidgets.QToolTip.hideText()
            return True
        return super().event(ev)

    # ---- paint -------------------------------------------------------
    def _tick_step(self):
        """A round tick interval giving <=12 gridlines across the shared scale."""
        for sec in (1, 2, 5, 10, 15, 30, 60, 120, 300, 600, 1800):
            step = int(round(sec * self.rate))
            if step > 0 and self.max_dur / float(step) <= 12:
                return step
        return max(1, int(self.max_dur / 10) or 1)

    def paintEvent(self, _ev):
        self._hits = []
        self._track_hits = []
        self._lane_hits = []
        p = QtGui.QPainter(self)
        p.fillRect(self.rect(), QtGui.QColor("#1e1e1e"))
        if not self.lanes or self.max_dur <= 0:
            p.setPen(QtGui.QColor("#666666"))
            p.drawText(self.rect(), QtCore.Qt.AlignCenter,
                       "Scan first — nothing to lay out.")
            p.end()
            return
        small = self.font(); small.setPointSize(9)
        tiny = self.font(); tiny.setPointSize(8)
        fm = QtGui.QFontMetrics(small)
        x0, w = self._axis()
        body_h = self.height() - self.HEAD_H - 6
        # shared ruler + grid (one scale for every lane, by design)
        p.setFont(tiny)
        step = self._tick_step()
        f = 0
        while f <= self.max_dur:
            x = self._x(f)
            p.setPen(QtGui.QColor("#2a2a2a"))
            p.drawLine(x, self.HEAD_H, x, self.HEAD_H + body_h)
            p.setPen(QtGui.QColor("#777777"))
            tc = frames_to_tc(f, self.rate)
            if tc.startswith("00:"):
                tc = tc[3:]
            p.drawText(x + 3, 1, 80, self.HEAD_H - 4,
                       QtCore.Qt.AlignLeft | QtCore.Qt.AlignVCenter, tc)
            f += step
        y = self.HEAD_H + 4
        self._lane_hits = []
        for lane_n, ln in enumerate(self.lanes):
            lane_h = self._lane_height(ln)
            folded = ln["seq"] in self.collapsed
            lane_w = self._x(ln["dur"]) - x0
            # lane backdrop: hub tinted, consumers zebra-striped
            bg = "#20262c" if ln["is_hub"] else \
                 ("#212121" if lane_n % 2 else "#1e1e1e")
            p.fillRect(0, y, self.rect().width(), lane_h, QtGui.QColor(bg))
            # sequence name spans the lane; per-track names in the right of
            # the label column. Clicking the name folds the lane — one huge
            # sequence should not push dozens of others off screen
            p.setFont(small)
            p.setPen(QtGui.QColor("#8fb4cc" if ln["is_hub"] else "#cccccc"))
            n_seg = sum(len(t["blocks"]) for t in ln["tracks"])
            head_txt = "%s%s %s" % ("▸ " if folded else "▾ ",
                                    ("[HUB] " if ln["is_hub"] else "") + ln["seq"],
                                    "(%d segs)" % n_seg if folded else "")
            p.drawText(2, y, self.LABEL_W - 78, self.TRACK_H,
                       QtCore.Qt.AlignRight | QtCore.Qt.AlignVCenter,
                       fm.elidedText(head_txt, QtCore.Qt.ElideMiddle,
                                     self.LABEL_W - 80))
            self._lane_hits.append(
                (QtCore.QRect(0, y, self.LABEL_W, self.TRACK_H), ln["seq"]))
            if folded:
                # one summary strip: where this sequence has material at all
                for trk in ln["tracks"]:
                    for i in trk["blocks"]:
                        d = self.rows[i]
                        xa = self._x(d["rec_in_f"] - ln["lo"])
                        xb = self._x(d["rec_in_f"] - ln["lo"]
                                     + (d.get("rec_dur_f") or 0))
                        p.fillRect(xa, y + 9, max(2, xb - xa), self.TRACK_H - 18,
                                   QtGui.QColor("#4a4a4a"))
                y += lane_h + self.LANE_GAP
                continue
            for trk, dy in self._lane_rows(ln):
                ty = y + dy
                p.setFont(tiny)
                p.setPen(QtGui.QColor("#666666"))
                p.drawText(self.LABEL_W - 72, ty, 62, self.TRACK_H,
                           QtCore.Qt.AlignRight | QtCore.Qt.AlignVCenter,
                           trk.get("label") or trk["track_id"])
                self._track_hits.append(
                    (QtCore.QRect(self.LABEL_W - 78, ty, 74, self.TRACK_H),
                     ln["seq"], trk["track_id"]))
                # base strip only as far as THIS lane reaches — shorter
                # sequences visibly fall short of the shared scale
                p.fillRect(x0, ty + self.TRACK_H // 2, max(lane_w, 2), 1,
                           QtGui.QColor("#0f0f0f"))
                for i in trk["blocks"]:
                    d = self.rows[i]
                    xa = self._x(d["rec_in_f"] - ln["lo"])
                    xb = self._x(d["rec_in_f"] - ln["lo"] + (d.get("rec_dur_f") or 0))
                    rect = QtCore.QRect(xa, ty + 3, max(3, xb - xa),
                                        self.TRACK_H - 6)
                    is_ref = d.get("role") == "ref"
                    if is_ref:
                        color = "#3d3a37"
                    elif d.get("is_hub"):
                        color = "#4a6a85" if d.get("pub_nn") is not None else "#6f9fc8"
                    elif d.get("timewarp"):
                        color = "#c080ff"
                    else:
                        color = "#ff7a45"
                    hit = i in self.highlight
                    if hit:
                        color = self._BRIGHT.get(color, color)
                    p.fillRect(rect, QtGui.QColor(color))
                    # not-in-hub is YELLOW and DASHED: a red box reads as
                    # "unlinked" everywhere else in Flame,
                    # and the dash keeps it apart from the amber variant edge
                    edge = (ORPHAN_EDGE if i in self.orphans else
                            "#ff5555" if i in self.strays else
                            "#e0b000" if i in self.variants else None)
                    if edge:
                        pen = QtGui.QPen(QtGui.QColor(edge), 2)
                        if i in self.orphans:
                            pen.setStyle(QtCore.Qt.DashLine)
                        p.setPen(pen)
                        p.drawRect(rect.adjusted(1, 1, -1, -1))
                    elif hit:
                        p.setPen(QtGui.QPen(QtGui.QColor("#ffffff"), 2))
                        p.drawRect(rect.adjusted(1, 1, -1, -1))
                    else:
                        p.setPen(QtGui.QColor("#141414"))
                        p.drawRect(rect)
                    if rect.width() >= 28:
                        p.setFont(tiny)
                        p.setPen(QtGui.QColor("#888888" if is_ref else "#14100d"))
                        tfm = QtGui.QFontMetrics(tiny)
                        p.drawText(rect.adjusted(4, 0, -3, 0),
                                   QtCore.Qt.AlignLeft | QtCore.Qt.AlignVCenter,
                                   tfm.elidedText(
                                       timeline_block_label(d, self.labels_segment),
                                       QtCore.Qt.ElideRight, rect.width() - 7))
                    self._hits.append((rect, i))
            y += lane_h + self.LANE_GAP
        p.end()


class ConnectionsTree(QtWidgets.QWidget):
    """One shot as a FAMILY TREE (an earlier ring web was
    big where it didn't need to be and small where it did, in the text): the
    Conform Hub piece on top, then every other segment of the shot as its own
    box, in generations by sequence length — the longest spots first. The
    line into each box is its link to the hub piece: ORANGE connected, BLUE
    shares the hub's source but NOT connected, dashed YELLOW not in the hub,
    grey not read yet. Click a box to ring what it is linked to (orange =
    connected to it, blue = shares its source); double-click selects its
    connected segments in Flame (the dialog does the selecting)."""

    nodeClicked = QtCore.Signal(int)
    nodeDoubleClicked = QtCore.Signal(int)

    NODE_H, CENTRE_H = 36, 40
    CENTRE_W = 250
    COLOURS = {"c": ACCENT, "s": "#4da3ff", "o": ORPHAN_EDGE, "u": "#777777",
               "hub": "#6f9fc8"}
    STATE_WORD = {"c": "connected", "s": "shared source only",
                  "o": "not in the hub", "u": "not read yet", "hub": ""}

    def __init__(self, parent=None):
        super().__init__(parent)
        self.rows = []
        self.centres, self.tiers = [], []
        self.states = {}
        self.conn, self.shared = {}, {}
        self.orphans = set()
        self.focus = None
        self.missing_label = ""
        self.tip_fn = None
        self.node_w = 150
        self._lay = tree_layout([], 1, 400)
        self._rects = {}
        self.setMouseTracking(True)
        self.setMinimumHeight(120)

    def _fonts(self):
        name = QtGui.QFont(self.font())
        name.setPixelSize(12)
        name.setBold(True)
        sub = QtGui.QFont(self.font())
        sub.setPixelSize(11)
        return name, sub

    def set_data(self, rows, centres, tiers, states, conn=None, shared=None,
                 orphans=(), missing_label=""):
        self.rows = rows or []
        self.centres, self.tiers = list(centres), list(tiers)
        self.states = dict(states or {})
        self.conn, self.shared = conn or {}, shared or {}
        self.orphans = set(orphans or ())
        self.missing_label = missing_label
        self.focus = None
        # boxes as wide as the longest sequence name needs — no wider
        fm = QtGui.QFontMetrics(self._fonts()[0])
        names = [self.rows[i].get("seq", "") for t in self.tiers
                 for i in t["nodes"] if i < len(self.rows)]
        self.node_w = max(120, min(210, max((fm.horizontalAdvance(n) for n in names),
                                            default=0) + 26))
        self._relayout()
        self.update()

    def clear(self, message=""):
        self.set_data([], [], [], {}, None, None, (), "")
        self.missing_label = message
        self.update()

    def _relayout(self):
        self._lay = tree_layout(self.tiers, max(1, len(self.centres)),
                                max(240, self.width()), self.node_w, self.NODE_H,
                                self.CENTRE_W, self.CENTRE_H)
        self._rects = {}
        for i, (cx, cy) in zip(self.centres, self._lay["centres"]):
            self._rects[i] = QtCore.QRectF(cx - self.CENTRE_W / 2.0,
                                           cy - self.CENTRE_H / 2.0,
                                           self.CENTRE_W, self.CENTRE_H)
        for row in self._lay["rows"]:
            for i, cx, cy in row["nodes"]:
                self._rects[i] = QtCore.QRectF(cx - self.node_w / 2.0,
                                               cy - self.NODE_H / 2.0,
                                               self.node_w, self.NODE_H)
        h = self._lay["h"] if (self.centres or self.tiers) else 120
        if self.minimumHeight() != h:
            self.setMinimumHeight(h)

    def resizeEvent(self, ev):
        super().resizeEvent(ev)
        self._relayout()

    def _hit(self, pos):
        pt = QtCore.QPointF(pos)
        return next((i for i, r in self._rects.items() if r.contains(pt)), None)

    # ---- interaction -----------------------------------------------------
    def mousePressEvent(self, ev):
        pos = ev.position().toPoint() if hasattr(ev, "position") else ev.pos()
        i = self._hit(pos)
        self.focus = i
        self.update()
        if i is not None:
            self.nodeClicked.emit(i)

    def mouseDoubleClickEvent(self, ev):
        pos = ev.position().toPoint() if hasattr(ev, "position") else ev.pos()
        i = self._hit(pos)
        if i is not None:
            self.nodeDoubleClicked.emit(i)

    def event(self, ev):
        if ev.type() == QtCore.QEvent.ToolTip:
            i = self._hit(ev.pos())
            if i is not None and self.tip_fn is not None:
                QtWidgets.QToolTip.showText(ev.globalPos(), self.tip_fn(i), self)
            else:
                QtWidgets.QToolTip.hideText()
            return True
        return super().event(ev)

    # ---- paint -----------------------------------------------------------
    def _sub(self, i):
        d = self.rows[i]
        bits = [track_label(d.get("track_id"), d.get("track_name"))]
        if d.get("is_hub") and i not in self.centres:
            bits.append("hub copy" if d.get("pub_nn") is None
                        else "Publish %02d" % d["pub_nn"])
        if d.get("timewarp"):
            pct = (d.get("tw_model") or {}).get("pct")
            bits.append("TW %.4g%%" % pct if pct else "TW")
        word = self.STATE_WORD.get(self.states.get(i), "")
        if word:
            bits.append(word)
        return " · ".join(bits)

    def paintEvent(self, _ev):
        p = QtGui.QPainter(self)
        p.setRenderHint(QtGui.QPainter.Antialiasing, True)
        p.fillRect(self.rect(), QtGui.QColor("#1e1e1e"))
        name_f, sub_f = self._fonts()
        if not self._rects and not self.missing_label:
            p.setPen(QtGui.QColor("#666666"))
            p.drawText(self.rect(), QtCore.Qt.AlignCenter, "Pick a shot.")
            p.end()
            return
        lay = self._lay
        grey = QtGui.QColor("#555555")
        tx = lay["trunk_x"]
        if lay["rows"]:
            p.setPen(QtGui.QPen(grey, 1.5))
            p.drawLine(QtCore.QPointF(tx, lay["trunk_top"]),
                       QtCore.QPointF(tx, lay["trunk_bottom"]))
        lit = None
        if self.focus is not None:
            lit = ({self.focus} | set(self.conn.get(self.focus) or ())
                   | set(self.shared.get(self.focus) or ()))
        # bus + drops: the drop's colour is the box's link to the hub piece
        p.setFont(sub_f)
        for row in lay["rows"]:
            by = row["bus_y"]
            last = row["nodes"][-1][1]
            p.setPen(QtGui.QPen(grey, 1.5))
            p.drawLine(QtCore.QPointF(tx, by), QtCore.QPointF(last, by))
            for i, cx, _cy in row["nodes"]:
                st = self.states.get(i, "u")
                col = QtGui.QColor(self.COLOURS.get(st, "#777777"))
                if lit is not None and i not in lit:
                    col.setAlpha(90)
                pen = QtGui.QPen(col, 2.5)
                if st == "o":
                    pen.setStyle(QtCore.Qt.DashLine)
                p.setPen(pen)
                p.drawLine(QtCore.QPointF(cx, by), QtCore.QPointF(cx, self._rects[i].top()))
                p.setPen(QtCore.Qt.NoPen)
                p.setBrush(col)
                p.drawEllipse(QtCore.QPointF(cx, by), 3, 3)
            if row["label"]:
                p.setPen(QtGui.QColor("#9a9a9a"))
                p.drawText(QtCore.QRectF(2, by - 9, tx - 10, 18),
                           QtCore.Qt.AlignRight | QtCore.Qt.AlignVCenter, row["label"])
        fm_n = QtGui.QFontMetrics(name_f)
        fm_s = QtGui.QFontMetrics(sub_f)
        # the hub piece(s) — or the gap where one should be
        if not self.centres and self.missing_label:
            c = lay["centres"][0]
            rect = QtCore.QRectF(c[0] - self.CENTRE_W / 2.0, c[1] - self.CENTRE_H / 2.0,
                                 self.CENTRE_W, self.CENTRE_H)
            pen = QtGui.QPen(QtGui.QColor(ORPHAN_EDGE), 2)
            pen.setStyle(QtCore.Qt.DashLine)
            p.setPen(pen)
            p.setBrush(QtGui.QColor("#1e1e1e"))
            p.drawRoundedRect(rect, 4, 4)
            p.setPen(QtGui.QColor(ORPHAN_EDGE))
            p.setFont(name_f)
            p.drawText(rect, QtCore.Qt.AlignCenter, self.missing_label)
        for i, rect in self._rects.items():
            if i >= len(self.rows):
                continue
            d = self.rows[i]
            centre = i in self.centres
            p.setPen(QtCore.Qt.NoPen)
            p.setBrush(QtGui.QColor("#24303a" if centre else "#262626"))
            p.drawRoundedRect(rect, 4, 4)
            stripe = ("#6f9fc8" if d.get("is_hub") else
                      "#c080ff" if d.get("timewarp") else ACCENT)
            p.fillRect(QtCore.QRectF(rect.x(), rect.y(), 4, rect.height()),
                       QtGui.QColor(stripe))
            if i in self.orphans:
                pen = QtGui.QPen(QtGui.QColor(ORPHAN_EDGE), 2)
                pen.setStyle(QtCore.Qt.DashLine)
            elif i == self.focus:
                pen = QtGui.QPen(QtGui.QColor("#ffffff"), 2)
            elif lit is not None and i in (self.conn.get(self.focus) or ()):
                pen = QtGui.QPen(QtGui.QColor(self.COLOURS["c"]), 2)
            elif lit is not None and i in (self.shared.get(self.focus) or ()):
                pen = QtGui.QPen(QtGui.QColor(self.COLOURS["s"]), 2)
            else:
                pen = QtGui.QPen(QtGui.QColor("#3c3c3c"), 1)
            p.setPen(pen)
            p.setBrush(QtCore.Qt.NoBrush)
            p.drawRoundedRect(rect, 4, 4)
            inner = rect.adjusted(11, 3, -7, -3)
            half = inner.height() / 2.0
            if centre:
                top = "HUB · %s" % (d.get("shot") or d.get("name") or "")
                sub = "%s · %s" % (d.get("seq", "?"),
                                   track_label(d.get("track_id"), d.get("track_name")))
            else:
                top, sub = d.get("seq", "?"), self._sub(i)
            p.setFont(name_f)
            p.setPen(QtGui.QColor("#e6e6e6"))
            p.drawText(QtCore.QRectF(inner.x(), inner.y(), inner.width(), half),
                       QtCore.Qt.AlignLeft | QtCore.Qt.AlignVCenter,
                       fm_n.elidedText(top, QtCore.Qt.ElideMiddle, int(inner.width())))
            p.setFont(sub_f)
            p.setPen(QtGui.QColor("#9a9a9a"))
            p.drawText(QtCore.QRectF(inner.x(), inner.y() + half, inner.width(), half),
                       QtCore.Qt.AlignLeft | QtCore.Qt.AlignVCenter,
                       fm_s.elidedText(sub, QtCore.Qt.ElideRight, int(inner.width())))
            if lit is not None and i not in lit and not centre:
                p.fillRect(rect, QtGui.QColor(30, 30, 30, 130))
        p.end()


class CutFlowBraid(QtWidgets.QWidget):
    """The Connections tab's whole-job view: the
    Conform Hub on top, every sequence as a lane below it, each shot a block
    in record order, and a ribbon joining the same shot from lane to lane —
    orange = connected to its hub piece, blue = shares the hub's source but
    NOT connected, dashed yellow = not in the hub, grey = connections not
    read yet. Lanes fit the width (order and connection are the point here,
    not timing — Timelines keeps the shared scale). Hovering a block threads
    that shot through every lane; click = open its family tree below;
    double-click = select its connected segments in Flame."""

    blockClicked = QtCore.Signal(int)          # shot group
    blockDoubleClicked = QtCore.Signal(int)    # inventory row

    LABEL_W = 240
    LANE_H = 14
    GAP = 34
    TOP = 8
    COLOURS = {"c": "#ff7a45", "s": "#4da3ff", "o": ORPHAN_EDGE, "u": "#777777",
               "hub": "#6f9fc8"}
    STATE_TEXT = {"c": "connected to its hub piece",
                  "s": "shares the hub piece's source but is NOT connected",
                  "o": "NOT in the hub", "u": "connections not read yet",
                  "hub": "the Conform Hub piece"}

    def __init__(self, parent=None):
        super().__init__(parent)
        self.rows = []
        self.lay = {"lanes": [], "ribbons": []}
        self.names = {}
        self.focus = None       # the selected shot (sticky)
        self.hover = None       # the shot under the mouse
        self._blocks = []       # [(QRectF, lane, block)] rebuilt every paint
        self.setMouseTracking(True)
        self.setSizePolicy(QtWidgets.QSizePolicy.Expanding,
                           QtWidgets.QSizePolicy.Fixed)
        self.setFixedHeight(80)

    def set_data(self, rows, lay, names=None):
        self.rows = rows or []
        self.lay = lay or {"lanes": [], "ribbons": []}
        self.names = names or {}
        self.hover = None
        n = len(self.lay["lanes"])
        self.setFixedHeight(max(80, self.TOP * 2 + n * self.LANE_H
                                + max(0, n - 1) * self.GAP + 4))
        self.update()

    def set_focus(self, group):
        self.focus = group
        self.update()

    # ---- geometry --------------------------------------------------------
    def _lane_y(self, li):
        return self.TOP + li * (self.LANE_H + self.GAP)

    def _label_w(self):
        """Lane-name column: up to LABEL_W, less on a narrow panel (the shot
        list sits beside the braid)."""
        return int(max(120, min(self.LABEL_W, self.width() * 0.2)))

    def _geometry(self):
        x0 = self._label_w()
        width = max(60.0, self.width() - x0 - 14)
        out = []
        for li, ln in enumerate(self.lay["lanes"]):
            blocks = ln["blocks"]
            gaps = 2.0 * max(0, len(blocks) - 1)
            tot = float(sum(b["w"] for b in blocks)) or 1.0
            scale = max(0.0, width - gaps) / tot
            x = float(x0)
            rects = []
            for b in blocks:
                w = max(2.0, b["w"] * scale)
                rects.append(QtCore.QRectF(x, self._lane_y(li), w, self.LANE_H))
                x += w + 2.0
            out.append(rects)
        return out

    def _hit(self, pos):
        pt = QtCore.QPointF(pos)
        for rect, li, bi in self._blocks:
            if rect.adjusted(-1, -3, 1, 3).contains(pt):
                return li, bi
        return None

    def _lit(self):
        return self.hover if self.hover is not None else self.focus

    def _dim(self):
        """How far the rest of the braid fades: hard while HOVERING (tracing
        one shot), gently for the SELECTED shot — the whole job stays
        readable, which is the point of the view."""
        if self.hover is not None:
            return 18, 45
        if self.focus is not None:
            return 70, 150
        return 120, 255

    # ---- interaction -----------------------------------------------------
    def mouseMoveEvent(self, ev):
        pos = ev.position().toPoint() if hasattr(ev, "position") else ev.pos()
        hit = self._hit(pos)
        g = self.lay["lanes"][hit[0]]["blocks"][hit[1]]["g"] if hit else None
        if g != self.hover:
            self.hover = g
            self.update()

    def leaveEvent(self, _ev):
        if self.hover is not None:
            self.hover = None
            self.update()

    def mousePressEvent(self, ev):
        pos = ev.position().toPoint() if hasattr(ev, "position") else ev.pos()
        hit = self._hit(pos)
        if hit is None:
            self.focus = None           # click empty space: back to the whole job
            self.update()
            return
        g = self.lay["lanes"][hit[0]]["blocks"][hit[1]]["g"]
        self.focus = g
        self.update()
        self.blockClicked.emit(g)

    def mouseDoubleClickEvent(self, ev):
        pos = ev.position().toPoint() if hasattr(ev, "position") else ev.pos()
        hit = self._hit(pos)
        if hit is not None:
            b = self.lay["lanes"][hit[0]]["blocks"][hit[1]]
            if b["rows"]:
                self.blockDoubleClicked.emit(b["rows"][0])

    def event(self, ev):
        if ev.type() == QtCore.QEvent.ToolTip:
            hit = self._hit(ev.pos())
            if hit is not None:
                ln = self.lay["lanes"][hit[0]]
                b = ln["blocks"][hit[1]]
                lines = ["%s  ·  %s" % (self.names.get(b["g"], "?"),
                                        ", ".join(s for s in ln["seqs"] if s)),
                         self.STATE_TEXT.get(b["state"], "")]
                if b["tw"]:
                    lines.append("timewarped")
                lines.append("click: open its tree below · double-click: "
                             "select its connected segments in Flame")
                QtWidgets.QToolTip.showText(ev.globalPos(), "\n".join(lines), self)
            else:
                QtWidgets.QToolTip.hideText()
            return True
        return super().event(ev)

    # ---- paint -----------------------------------------------------------
    def paintEvent(self, _ev):
        p = QtGui.QPainter(self)
        p.setRenderHint(QtGui.QPainter.Antialiasing, True)
        p.fillRect(self.rect(), QtGui.QColor("#1e1e1e"))
        lanes = self.lay["lanes"]
        self._blocks = []
        if not lanes:
            p.setPen(QtGui.QColor("#666666"))
            p.drawText(self.rect(), QtCore.Qt.AlignCenter,
                       "Scan to see how every shot flows through every sequence.")
            p.end()
            return
        geo = self._geometry()
        lit = self._lit()
        rib_dim, block_dim = self._dim()
        # ribbons first, under the blocks; the lit shot's ribbons last (on top)
        order = sorted(self.lay["ribbons"], key=lambda r: r["g"] == lit)
        for rb in order:
            (la, ba), (lb, bb) = rb["frm"], rb["to"]
            ra, rbx = geo[la][ba], geo[lb][bb]
            x1, y1 = ra.center().x(), ra.bottom()
            x2, y2 = rbx.center().x(), rbx.top()
            dy = (y2 - y1) * 0.45
            path = QtGui.QPainterPath(QtCore.QPointF(x1, y1))
            path.cubicTo(QtCore.QPointF(x1, y1 + dy), QtCore.QPointF(x2, y2 - dy),
                         QtCore.QPointF(x2, y2))
            col = QtGui.QColor(self.COLOURS.get(rb["state"], "#777777"))
            on = lit is None or rb["g"] == lit
            col.setAlpha(120 if lit is None else (235 if on else rib_dim))
            width = max(1.5, min(7.0, min(ra.width(), rbx.width()) * 0.45))
            pen = QtGui.QPen(col, 1.2 if rb["skip"] else width)
            pen.setCapStyle(QtCore.Qt.FlatCap)
            if rb["skip"] or rb["state"] == "o":
                pen.setStyle(QtCore.Qt.DashLine)
            p.setPen(pen)
            p.setBrush(QtCore.Qt.NoBrush)
            p.drawPath(path)
        small = self.font()
        small.setPointSize(9)
        fm = QtGui.QFontMetrics(small)
        p.setFont(small)
        for li, ln in enumerate(lanes):
            y = self._lane_y(li)
            label = ("[HUB] " if ln["is_hub"] else "") + ln["label"]
            p.setPen(QtGui.QColor("#8fb4cc" if ln["is_hub"] else "#bbbbbb"))
            lw = self._label_w()
            p.drawText(QtCore.QRectF(4, y - 3, lw - 12, self.LANE_H + 6),
                       QtCore.Qt.AlignRight | QtCore.Qt.AlignVCenter,
                       fm.elidedText(label, QtCore.Qt.ElideMiddle, lw - 16))
            for bi, b in enumerate(ln["blocks"]):
                rect = geo[li][bi]
                on = lit is None or b["g"] == lit
                col = QtGui.QColor(self.COLOURS.get(b["state"], "#777777"))
                if b["state"] == "o":
                    fill = QtGui.QColor(col)
                    fill.setAlpha(70 if on else max(20, block_dim // 4))
                    p.fillRect(rect, fill)
                    pen = QtGui.QPen(col if on else QtGui.QColor(90, 90, 60), 1.2)
                    pen.setStyle(QtCore.Qt.DashLine)
                    p.setPen(pen)
                    p.setBrush(QtCore.Qt.NoBrush)
                    p.drawRect(rect.adjusted(0.5, 0.5, -0.5, -0.5))
                else:
                    col.setAlpha(255 if on else block_dim)
                    p.fillRect(rect, col)
                if b["tw"]:
                    tw = QtGui.QColor("#c080ff")
                    tw.setAlpha(255 if on else 60)
                    p.fillRect(QtCore.QRectF(rect.x(), rect.y(), rect.width(), 3), tw)
                if lit is not None and b["g"] == lit:
                    p.setPen(QtGui.QPen(QtGui.QColor("#ffffff"), 1.5))
                    p.setBrush(QtCore.Qt.NoBrush)
                    p.drawRect(rect.adjusted(-1, -1, 1, 1))
                self._blocks.append((rect, li, bi))
        p.end()


class SetupWizard(QtWidgets.QDialog):
    """One sequence at a time: see its tracks laid out, say what each one is.

    No heuristic can separate a still that IS a shot from a still that is a
    graphic, or know which movie on V3 is an FX element — but the artist can,
    in two seconds, per track. On a real 52-sequence job the guessing
    was the difference between a readable tool and noise. Answers persist in
    project state and beat every heuristic from then on."""

    def __init__(self, rows, state, parent=None):
        super().__init__(parent)
        self.setWindowTitle("CCM Setup Wizard — what is on each track?")
        self.setStyleSheet(STYLE)
        scr = QtWidgets.QApplication.primaryScreen()
        avail = scr.availableGeometry() if scr else QtCore.QRect(0, 0, 1280, 800)
        self.resize(min(1180, avail.width() - 80), min(760, avail.height() - 80))
        self.rows = rows
        self.roles = {k: dict(v) for k, v in
                      (state.get("track_roles") or {}).items()}
        self.hubs = set(h for h in (state.get("hub_names") or []) if h)
        self.seqs = []
        for d in rows:
            if d["seq"] not in self.seqs:
                self.seqs.append(d["seq"])
        self.seqs.sort(key=lambda s: (not any(d.get("is_hub") for d in rows
                                              if d["seq"] == s), s.lower()))
        self.idx = 0

        root = QtWidgets.QVBoxLayout(self)
        root.setContentsMargins(14, 14, 14, 14)
        root.setSpacing(8)
        head = QtWidgets.QLabel(
            "Tell CCM what lives on each track. Only <b>Shots / footage</b> "
            "feeds the conform audits and the Conform Hub — graphics, "
            "references and FX are tracked separately and can be hidden with "
            "Compact view. Anything left on “let CCM decide” keeps the old "
            "automatic guess.")
        head.setWordWrap(True)
        head.setStyleSheet("color:#aaaaaa;")
        root.addWidget(head)

        split = QtWidgets.QHBoxLayout()
        self.seq_list = QtWidgets.QListWidget()
        self.seq_list.setFixedWidth(240)
        self.seq_list.currentRowChanged.connect(self._goto)
        split.addWidget(self.seq_list)

        right = QtWidgets.QVBoxLayout()
        self.title = _squish(QtWidgets.QLabel(""))
        self.title.setStyleSheet("color:#ffffff; font-weight:bold;")
        right.addWidget(self.title)
        self.view = TimelinesView()
        vs = QtWidgets.QScrollArea()
        vs.setWidgetResizable(True)
        vs.setWidget(self.view)
        vs.setMinimumHeight(180)
        vs.setStyleSheet("QScrollArea { border:1px solid #2a2a2a; border-radius:4px; }")
        right.addWidget(vs, 1)
        self.hub_cb = QtWidgets.QCheckBox("This sequence is the CONFORM HUB "
                                          "(the sources/shots sequence)")
        self.hub_cb.toggled.connect(self._hub_toggled)
        right.addWidget(self.hub_cb)
        self.track_table = QtWidgets.QTableWidget(0, 5)
        self.track_table.setHorizontalHeaderLabels(
            ["Version", "Track", "Segments", "What CCM guessed",
             "This track is…"])
        self.track_table.verticalHeader().setVisible(False)
        self.track_table.setEditTriggers(QtWidgets.QAbstractItemView.NoEditTriggers)
        _init_table_resize(self.track_table, stretch_cols=(3,))
        right.addWidget(self.track_table, 1)
        rw = QtWidgets.QWidget(); rw.setLayout(right)
        split.addWidget(rw, 1)
        root.addLayout(split, 1)

        btns = QtWidgets.QHBoxLayout()
        self.progress = QtWidgets.QLabel("")
        self.progress.setStyleSheet("color:#777777;")
        btns.addWidget(self.progress, 1)
        for text, fn in (("◀ Previous", lambda: self._step(-1)),
                         ("Next ▶", lambda: self._step(1))):
            b = QtWidgets.QPushButton(text)
            b.clicked.connect(fn)
            btns.addWidget(b)
        b_all = QtWidgets.QPushButton("Apply to all sequences")
        b_all.setToolTip(
            "Copy THIS sequence's track answers to every other sequence that "
            "has a track with the same name. Spot families usually share a "
            "layout, so this turns 52 sequences into one decision.")
        b_all.clicked.connect(self._apply_all)
        btns.addWidget(b_all)
        b_save = QtWidgets.QPushButton("Save + Rescan")
        b_save.setObjectName("primary")
        b_save.clicked.connect(self.accept)
        btns.addWidget(b_save)
        b_cancel = QtWidgets.QPushButton("Cancel")
        b_cancel.clicked.connect(self.reject)
        btns.addWidget(b_cancel)
        root.addLayout(btns)

        self._fill_list()
        if self.seqs:
            self.seq_list.setCurrentRow(0)

    # ---- helpers -----------------------------------------------------
    def _seq(self):
        return self.seqs[self.idx] if 0 <= self.idx < len(self.seqs) else None

    def _tracks(self, seq):
        """(track_id, track_name, n segments, guessed roles) per track."""
        out = {}
        for d in self.rows:
            if d["seq"] != seq:
                continue
            t = out.setdefault(d.get("track_id"), {
                "track_id": d.get("track_id"),
                "name": track_label(d.get("track_id"), d.get("track_name")),
                "version": _track_sort_key(d.get("track_id"))[0] + 1,
                "n": 0, "guess": {}})
            t["n"] += 1
            g = d.get("role") or "source"
            t["guess"][g] = t["guess"].get(g, 0) + 1
        return sorted(out.values(),
                      key=lambda t: _track_sort_key(t["track_id"]), reverse=True)

    def _fill_list(self):
        self.seq_list.blockSignals(True)
        self.seq_list.clear()
        for s in self.seqs:
            done = len(self.roles.get(s) or {})
            mark = "✓ " if done else "   "
            self.seq_list.addItem("%s%s%s" % (mark, s,
                                              "  [HUB]" if s in self.hubs else ""))
        self.seq_list.blockSignals(False)

    def _goto(self, row):
        if row is None or row < 0 or row >= len(self.seqs):
            return
        self.idx = row
        self._render()

    def _step(self, delta):
        n = len(self.seqs)
        if not n:
            return
        self.seq_list.setCurrentRow(max(0, min(n - 1, self.idx + delta)))

    def _hub_toggled(self, on):
        seq = self._seq()
        if seq is None:
            return
        (self.hubs.add if on else self.hubs.discard)(seq)
        self._fill_list()
        self.seq_list.blockSignals(True)
        self.seq_list.setCurrentRow(self.idx)
        self.seq_list.blockSignals(False)

    def _set_role(self, track_id, role):
        seq = self._seq()
        if seq is None:
            return
        slot = self.roles.setdefault(seq, {})
        if role:
            slot[track_id] = role
        else:
            slot.pop(track_id, None)
        if not slot:
            self.roles.pop(seq, None)
        self._fill_list()
        self.seq_list.blockSignals(True)
        self.seq_list.setCurrentRow(self.idx)
        self.seq_list.blockSignals(False)

    def _apply_all(self):
        seq = self._seq()
        mine = self.roles.get(seq) or {}
        if not mine:
            return
        by_name = {t["track_id"]: t["name"] for t in self._tracks(seq)}
        wanted = {by_name.get(tid): role for tid, role in mine.items()}
        n = 0
        for other in self.seqs:
            if other == seq:
                continue
            slot = self.roles.setdefault(other, {})
            for t in self._tracks(other):
                role = wanted.get(t["name"])
                if role:
                    slot[t["track_id"]] = role
                    n += 1
            if not slot:
                self.roles.pop(other, None)
        self._fill_list()
        self.seq_list.blockSignals(True)
        self.seq_list.setCurrentRow(self.idx)
        self.seq_list.blockSignals(False)
        QtWidgets.QMessageBox.information(
            self, "Applied", "Matched %d track(s) by name across %d other "
            "sequence(s). Step through to check the odd ones."
            % (n, len(self.seqs) - 1))

    def _render(self):
        seq = self._seq()
        if seq is None:
            return
        sub = [d for d in self.rows if d["seq"] == seq]
        rate = next((d.get("rate") for d in sub if d.get("rate")), 24.0)
        self.view.set_data(sub, timeline_lanes(sub, include_refs=True), rate=rate)
        self.title.setText("%s — %d segment(s)" % (seq, len(sub)))
        self.progress.setText("Sequence %d of %d · %d answered"
                              % (self.idx + 1, len(self.seqs),
                                 sum(1 for s in self.seqs if self.roles.get(s))))
        self.hub_cb.blockSignals(True)
        self.hub_cb.setChecked(seq in self.hubs
                               or any(d.get("is_hub") for d in sub))
        self.hub_cb.blockSignals(False)
        tracks = self._tracks(seq)
        self.track_table.setRowCount(len(tracks))
        prev_v = None
        for r, t in enumerate(tracks):
            # the table reads top-down exactly like the lanes above it, and
            # only marks the version where it CHANGES — so a version's tracks
            # read as one block
            vi = QtWidgets.QTableWidgetItem(
                "" if t["version"] == prev_v else "V%d" % t["version"])
            vi.setForeground(QtGui.QColor("#8fb4cc"))
            prev_v = t["version"]
            self.track_table.setItem(r, 0, vi)
            self.track_table.setItem(r, 1, QtWidgets.QTableWidgetItem(t["name"]))
            self.track_table.setItem(r, 2, QtWidgets.QTableWidgetItem(str(t["n"])))
            guess = ", ".join("%s×%d" % (ROLE_LABELS.get(k, k), v)
                              for k, v in sorted(t["guess"].items(),
                                                 key=lambda kv: -kv[1]))
            gi = QtWidgets.QTableWidgetItem(guess)
            gi.setForeground(QtGui.QColor("#777777"))
            self.track_table.setItem(r, 3, gi)
            combo = QtWidgets.QComboBox()
            combo.addItem(ROLE_LABELS[""], "")
            for role in TRACK_ROLES:
                combo.addItem(ROLE_LABELS[role], role)
            cur = (self.roles.get(seq) or {}).get(t["track_id"], "")
            combo.setCurrentIndex(max(0, combo.findData(cur)))
            combo.currentIndexChanged.connect(
                lambda _i, tid=t["track_id"], c=combo:
                self._set_role(tid, c.currentData()))
            self.track_table.setCellWidget(r, 4, combo)
        _autosize_once(self.track_table)

    def result_state(self):
        return self.roles, sorted(self.hubs)


class ConformManagerDialog(QtWidgets.QDialog):

    CMP_W, CMP_H = 320, 180        # compare-column thumbnail size (fixed)

    def __init__(self, selection, parent=None):
        super().__init__(parent)
        self.setWindowTitle("Connected Conform Manager %s (beta)" % __version__)
        self.setStyleSheet(STYLE)
        # never open larger than the available screen (small laptops), and
        # allow shrinking well below the default
        scr = QtWidgets.QApplication.primaryScreen()
        avail = scr.availableGeometry() if scr else QtCore.QRect(0, 0, 1280, 800)
        self.resize(min(940, avail.width() - 60), min(820, avail.height() - 80))
        self.setMinimumSize(620, 460)
        self._inv = []           # one scan, shared by every tab
        self._groups = []        # logical shot groups (inv indices; refs excluded)
        self._oc = []            # openclip inventory (off-latest)
        self._s = load_settings()   # per-scan settings snapshot (no per-render disk reads)
        self._st = load_state()     # per-scan state snapshot
        self._dup_rows = []      # consumer dup conflict sets — computed ONCE per scan
        self._dup_tier = {}      # inv index -> dup tier
        self._stacks = []        # split-screen / multi-element groups (inv indices)
        self._stack_label = {}   # inv index -> "S1"…
        self._longest = set()    # inv indices of each shot's longest instance
        self._tips = []          # explanatory labels the Tips button hides
        # ONE preview switch across the tabs — see _pv_show
        self._pv_on = bool(load_settings().get("auto_preview"))
        self._pv_buttons, self._pv_panels, self._pv_vis = [], {}, {}
        self._pv_req, self._pv_loaded, self._pv_token = {}, {}, {}
        self._pv_tabs = {}

        root = QtWidgets.QVBoxLayout(self)
        root.setContentsMargins(16, 16, 16, 16)
        root.setSpacing(10)

        head = QtWidgets.QHBoxLayout()
        hdr = QtWidgets.QLabel("CCM")
        hdr.setObjectName("header")
        head.addWidget(hdr)
        sub = QtWidgets.QLabel("CONNECTED CONFORM MANAGER")
        sub.setObjectName("sub")
        head.addWidget(sub)
        # the version doubles as the beta flag: hover lists what's unfinished
        ver = QtWidgets.QLabel("v%s BETA" % __version__)
        ver.setObjectName("ver")
        ver.setToolTip(BETA_NOTE)
        head.addWidget(ver, 0, QtCore.Qt.AlignVCenter)
        head.addStretch(1)
        root.addLayout(head)

        srow = QtWidgets.QHBoxLayout()
        srow.addWidget(QtWidgets.QLabel("Scope:"))
        self.scope = QtWidgets.QComboBox()
        self.scope.addItems(SCOPES)
        saved = load_settings().get("scope", "Current Reel")
        if saved in SCOPES:
            self.scope.setCurrentText(saved)
        self.scope.currentTextChanged.connect(self._scope_changed)
        srow.addWidget(self.scope)
        srow.addStretch(1)
        self.probe_btn = QtWidgets.QPushButton("On-Box Probe")
        self.probe_btn.setToolTip(
            "READ-ONLY diagnostics: writes what this Flame's python API "
            "exposes (inspector image access, timewarp class names, track "
            "creation, prefs, openclip discovery) to flame_ccm_probe.txt next "
            "to the script (home folder as the fallback). "
            "Best run with an Inventory row selected — ideally a TIMEWARPED "
            "one — and a source clip or openclip selected in the Media Panel.")
        self.probe_btn.clicked.connect(self._run_probe)
        srow.addWidget(self.probe_btn)
        self.wiz_btn = QtWidgets.QPushButton("Setup Wizard…")
        self.wiz_btn.setToolTip(
            "Go sequence by sequence and tell CCM what each track holds — "
            "shots, graphics, reference or FX — and which sequence is the "
            "Conform Hub. Nothing on the timeline is touched; the answers "
            "beat CCM's guesses and drive Compact view and the graphics "
            "filters. On a big job this is the first thing to run.")
        self.wiz_btn.clicked.connect(self._run_wizard)
        srow.addWidget(self.wiz_btn)
        self.tips_btn = QtWidgets.QPushButton("Tips")
        self.tips_btn.setCheckable(True)
        self.tips_btn.setToolTip(
            "Show/hide the explanatory text under each view. Off once you "
            "know the tool — it is a lot of vertical space on a big job.")
        self.tips_btn.toggled.connect(self._tips_toggled)
        srow.addWidget(self.tips_btn)
        self.console_btn = QtWidgets.QPushButton("Console")
        self.console_btn.setCheckable(True)
        self.console_btn.setToolTip(
            "Show/hide the message log. Hidden by default — turn it on for "
            "probe output or to follow a build step.")
        self.console_btn.toggled.connect(self._console_toggled)
        srow.addWidget(self.console_btn)
        self.scan_btn = QtWidgets.QPushButton("Scan")
        self.scan_btn.setObjectName("primary")
        self.scan_btn.clicked.connect(self._scan)
        srow.addWidget(self.scan_btn)
        root.addLayout(srow)

        self.tabs = QtWidgets.QTabWidget()
        self.tabs.addTab(self._build_inventory_tab(), "Inventory")
        self.tabs.addTab(self._build_duplicates_tab(), "Duplicates")
        # no separate Coverage tab: the
        # Timelines tab IS the map, with the coverage drawer docked below
        self._tl_tab_w = self._build_timelines_tab()
        self.tabs.addTab(self._tl_tab_w, "Timelines")
        self._cx_tab_w = self._build_connections_tab()
        self.tabs.addTab(self._cx_tab_w, "Connections")
        # Hub + Ledger wear their state in the tab name
        self._hub_tab_w = self._build_hub_tab()
        k = self.tabs.addTab(self._hub_tab_w, "Hub (WIP)")
        self.tabs.setTabToolTip(k, WIP_NOTE)
        self._led_tab_w = self._build_ledger_tab()
        k = self.tabs.addTab(self._led_tab_w, "Ledger (WIP)")
        self.tabs.setTabToolTip(k, WIP_NOTE)
        self.tabs.addTab(self._build_settings_tab(), "Settings")
        self._pv_tabs = {"tl": self._tl_tab_w, "cx": self._cx_tab_w,
                         "hub": self._hub_tab_w, "led": self._led_tab_w}
        for apply in self._pv_vis.values():
            apply(self._pv_on)
        # connections are read from Flame only when that tab is opened
        self.tabs.currentChanged.connect(self._tabs_changed)
        root.addWidget(self.tabs, 1)

        self.log = QtWidgets.QPlainTextEdit()
        self.log.setObjectName("logbox")
        self.log.setReadOnly(True)
        self.log.setFixedHeight(130)
        root.addWidget(self.log)
        # screen real estate is the scarce resource on a real job (52
        # sequences): the console is for probes and build steps, not for
        # every day, so it starts hidden and remembers the choice
        self.console_btn.setChecked(bool(load_settings().get("show_console")))
        self.log.setVisible(self.console_btn.isChecked())
        self.tips_btn.setChecked(load_settings().get("show_tips", True))

        self._say("Connected Conform Manager %s (beta) — %s" % (
            __version__,
            "changes ALLOWED (Settings)." if self._s.get("enable_mutations")
            else "read-only: nothing on a timeline changes until you allow "
                 "it in Settings."))
        self._scan()

    # ---------------------------------------------------------------- util
    def _say(self, m):
        self.log.appendPlainText(m)

    def _settings(self):
        return load_settings()

    def _tip(self, label):
        """Register an explanatory label so the Tips button can reclaim its
        space. Returns the label so callers can add it inline."""
        self._tips.append(label)
        label.setVisible(self.tips_btn.isChecked())
        return label

    def _tips_toggled(self, on):
        for lb in getattr(self, "_tips", []):
            lb.setVisible(on)
        s = load_settings()
        s["show_tips"] = bool(on)
        save_settings(s)

    def _console_toggled(self, on):
        self.log.setVisible(on)
        s = load_settings()
        s["show_console"] = bool(on)
        save_settings(s)

    def _run_wizard(self):
        """READ-ONLY: the wizard only records what the artist tells us."""
        if not self._inv:
            msg = self._empty_scope_hint()
            self._say("Setup Wizard: " + msg)
            QtWidgets.QMessageBox.information(self, "Setup Wizard", msg)
            return
        wiz = SetupWizard(self._inv, self._st, self)
        if wiz.exec() != QtWidgets.QDialog.Accepted:
            return
        roles, hubs = wiz.result_state()
        st = self._st
        st["track_roles"] = roles
        # The wizard is AUTHORITATIVE for every sequence it just showed, so
        # unchecking actually unmarks. Unioning here meant a hub could never
        # be un-marked — it came straight back on Save + Rescan.
        # Registrations for sequences outside this scan are
        # left alone.
        in_scope = {d["seq"] for d in self._inv}
        keep = {h for h in (st.get("hub_names") or []) if h not in in_scope}
        st["hub_names"] = sorted(keep | set(hubs))
        save_state(st)
        # a sequence can still read as hub because its NAME matches the
        # setting — say so rather than letting it look like the toggle failed
        hub_setting = (self._s.get("sources_seq_name") or "").strip()
        stuck = [s for s in in_scope if s not in hubs
                 and any(is_hub_name(s, h)
                         for h in ([hub_setting] + list(HUB_ALIASES)) if h)]
        if stuck:
            self._say("Note: %s still count as hubs because the name matches "
                      "the Conform Hub name in Settings — rename the sequence "
                      "or change that setting to clear it."
                      % ", ".join(sorted(stuck)[:4]))
        n_tracks = sum(len(v) for v in roles.values())
        self._say("Wizard: %d track answer(s) across %d sequence(s) saved; "
                  "%d hub(s). Rescanning." % (n_tracks, len(roles), len(hubs)))
        self._scan()

    def _empty_scope_hint(self):
        """What to do when the scope holds nothing. Every scope except
        Selected Sequences is measured FROM the sequence open in the Timeline
        (sequences_for_scope anchors on it), so with nothing open a desktop
        full of sequences still scans empty."""
        scope = self.scope.currentText()
        if scope == "Selected Sequences":
            how = ("select the sequences (or the reel holding them) in the "
                   "Media Panel, then press Scan.")
        else:
            how = ("open one of your sequences in the Timeline — the '%s' "
                   "scope is measured from it — then press Scan. Or switch "
                   "Scope to Selected Sequences and select them in the Media "
                   "Panel." % scope)
        return "No sequences found for scope '%s': %s" % (scope, how)

    def _scope_changed(self, text):
        s = load_settings(); s["scope"] = text; save_settings(s)
        self._scan()

    def _units(self):
        return self.inv_units.currentText() if hasattr(self, "inv_units") else "Timecode"

    def _fmt(self, frames, tc):
        """Pick TC or frame display for a value, per the Units toggle."""
        if self._units() == "Frames":
            return "" if frames is None else frames
        return tc or ""

    # ---------------------------------------------------------------- scan
    def _scan(self):
        """Scan button, scope change, startup: resolve the scope from what the
        Timeline shows now, then scan it."""
        try:
            seqs = sequences_for_scope(self.scope.currentText())   # ONE tree walk
        except Exception as e:
            self._say("Scope error: %s" % e)
            seqs = []
        self._scan_list(seqs)

    def _rescan_after_change(self, extra=()):
        """After CCM itself changed the project, rescan the SAME sequences
        (plus any the change created) instead of re-resolving the scope. The
        change's own temp copy/delete in the Media Panel moves the Timeline,
        and a scope measured from it can come back empty and blank every tab
        — the merge looked like it did nothing. Only Scan or a scope change
        re-resolves."""
        self._restore_timeline()
        seqs = rescan_list(getattr(self, "_scan_seqs", None), extra, _seq_alive)
        if not seqs:
            self._scan()
            return
        self._say("Rescanned the same %d sequence(s)." % len(seqs))
        self._scan_list(seqs)

    def _set_scan_enabled(self, on):
        """Scan and Scope off while a change runs: its loop yields to the
        event queue, and a rescan mid-loop would rebind the rows it acts on."""
        for w in (getattr(self, "scan_btn", None), getattr(self, "scope", None)):
            if w is not None:
                w.setEnabled(on)

    def _remember_timeline(self):
        """Note the sequence open in the Timeline, and where its positioner
        sits, before CCM changes the project. The change's temp copy/delete in
        the Media Panel makes Flame load something else into the Timeline;
        _restore_timeline puts the user back."""
        self._tl_home = None
        try:
            clip = flame.timeline.clip
        except Exception:
            clip = None
        if clip is None or type(clip).__name__ != "PySequence":
            return
        t = None
        try:
            a = clip.current_time
            t = a.get_value() if hasattr(a, "get_value") else a
        except Exception:
            t = None
        self._tl_home = (clip, _clean_name(clip), t)

    def _restore_timeline(self, home=None):
        """Re-open the sequence _remember_timeline noted, at the same frame,
        if the Timeline now shows something else. Navigation only: nothing in
        the project changes. Flame may reload the Timeline after this code
        returns, so the check runs once more when events have settled."""
        again = home is None
        if again:
            home, self._tl_home = getattr(self, "_tl_home", None), None
        if not home:
            return
        if again:
            self._tl_recheck = home
            QtCore.QTimer.singleShot(600, self._recheck_timeline)
        seq, name, t = home
        try:
            cur = flame.timeline.clip
        except Exception:
            cur = None
        if cur is not None and type(cur).__name__ == "PySequence" \
                and _clean_name(cur) == name:
            return
        if not _seq_alive(seq):
            return
        try:
            seq.open()
        except Exception as e:
            self._say("Couldn't re-open '%s' in the Timeline (%s)." % (name, e))
            return
        if t is not None:
            _set_flame_attr(seq, "current_time", t)
        self._say("Re-opened '%s' in the Timeline." % name)

    def _recheck_timeline(self):
        home, self._tl_recheck = getattr(self, "_tl_recheck", None), None
        if home:
            self._restore_timeline(home)

    def _scan_list(self, seqs):
        self._s = load_settings()
        self._st = load_state()
        warns = []
        # the sequences a change's rescan reads (_rescan_after_change). An
        # empty scan leaves nothing to change, so it doesn't replace them — a
        # scan that comes back empty mid-change can't strand the rescan
        if seqs:
            self._scan_seqs = list(seqs)
        # an empty scope used to be SILENT — an empty table with no reason
        # (a desktop full of sequences still read "no sequences")
        self._n_seqs = len(seqs)
        if not seqs:
            self._say(self._empty_scope_hint())
        try:
            self._inv = scan_instances(seqs, self._s, warnings=warns, state=self._st)
        except Exception as e:
            self._say("Scan error: %s" % e)
            self._inv = []
        try:
            self._oc = openclip_inventory(seqs, warnings=warns)
        except Exception as e:
            self._say("Openclip scan error: %s" % e)
            self._oc = []
        for w in warns:
            self._say(w)
        for m in ref_report(self._inv):
            self._say(m)
        self._recompute()
        self._refresh_views()

    def _recompute(self):
        """Everything derived from the cached scan rows — computed once, read by
        every renderer (the dup math is O(n²); it must not run per-tab)."""
        inv = self._inv
        mc = int(self._s.get("match_chars", 10) or 10)
        self._shared_all = None      # per-scan cache of the shared-source map
        self._conn_all = None        # per-scan cache of the connected map (lazy)
        # seg_key collisions silently drop a member from the shared-source
        # mapping — one way the same operation can flag one segment and
        # not its twin. seg.uid reads None on
        # box, so the fallback key does the work; say so if it collides.
        seen_keys = {}
        for i, d in enumerate(inv):
            seen_keys.setdefault(d.get("match_key"), []).append(i)
        clashes = [k for k, v in seen_keys.items() if k and len(v) > 1]
        if clashes:
            self._say("⚠ %d segment identity collision(s) (e.g. %r) — duplicate "
                      "verdicts on those rows may be unreliable."
                      % (len(clashes), clashes[0]))
        # logical shot groups over hub + sources (refs excluded), as inv indices
        req_name = bool(self._s.get("identity_by_name", True))
        aidx = [i for i, d in enumerate(inv) if d.get("role") != "ref"]
        self._groups = [[aidx[m] for m in g]
                        for g in source_groups([inv[i] for i in aidx], mc,
                                               req_name)]
        # consumer-side duplicate conflict sets, annotated from state
        # graphics get the same duplicate treatment as shots — they are a
        # separate CATEGORY, not a lesser one — so both are
        # grouped here and the tab filters by category
        cidx = [i for i, d in enumerate(inv)
                if not d.get("is_hub")
                and d.get("role") in ("source", "graphic")]
        raw = duplicate_sets([inv[i] for i in cidx], mc, req_name)
        groups = [{"tier": g["tier"], "members": [cidx[m] for m in g["members"]]}
                  for g in raw]
        self._dup_rows = annotate_dup_sets(inv, groups,
                                           self._st.get("dup_decisions"),
                                           self._st.get("sanctioned_dupes"))
        self._dup_tier = {}
        for r in self._dup_rows:
            for i in r["members"]:
                self._dup_tier[i] = r["tier"]
        self._attach_strays(self._dup_rows)
        # split-screen stacks + longest-instance flags
        self._stacks = stack_groups(inv)
        self._stack_label = {}
        for k, g in enumerate(self._stacks):
            for i in g:
                self._stack_label[i] = "S%d" % (k + 1)
        self._longest = longest_members(inv, self._groups)
        # orphans: consumer segments whose real source has NO hub entry (only
        # meaningful once a hub is in scope, else everything reads orphaned)
        self._orphans = set()
        if any(d.get("is_hub") for d in inv):
            self._orphans = set(hub_orphans(inv, self._shared_map()))
            if self._orphans:
                self._say("⚠ %d ORPHAN segment(s): their source is not in the "
                          "hub — publish/relink would skip them. Dashed yellow edge "
                          "in Timelines; rebuild the hub or merge the "
                          "duplicate that caused it." % len(self._orphans))
        self._cov_gi = None       # group indices are per-scan; selection resets
        self._pv_reset()

    def _refresh_views(self):
        cons = consumer_instances(self._inv)
        refs = sum(1 for d in self._inv if d.get("role") == "ref")
        hub = sum(1 for d in self._inv if d.get("is_hub"))
        warp = sum(1 for d in cons if d["timewarp"])
        off = sum(1 for d in self._oc if d["off_latest"])
        if getattr(self, "_n_seqs", None) == 0:
            self.inv_label.setText(self._empty_scope_hint())
        else:
            self.inv_label.setText(
                "%d source segment(s), %d ref(s) in '%s'  —  %d logical shot(s), "
                "%d hub, %d stack(s), %d timewarped, %d openclip(s) off-latest"
                % (len(cons), refs, self.scope.currentText(), len(self._groups),
                   hub, len(self._stacks), warp, off))
        self._render_inv_table()
        self._render_duplicates()
        self._render_timelines()
        self._render_coverage_detail()
        self._render_hub()
        self._render_connections()
        self._render_ledger()

    def _reclassify(self):
        """Re-run role classification + derived audits on the CACHED scan — a
        ref nudge changes no Flame data, so no tree walk is needed."""
        classify_roles(self._inv, self._s, self._st.get("ref_overrides", {}),
                       self._st.get("track_roles", {}))
        for m in ref_report(self._inv):
            self._say(m)
        self._recompute()
        self._refresh_views()

    # ---------------------------------------------------------------- Inventory tab
    def _build_inventory_tab(self):
        w = QtWidgets.QWidget()
        v = QtWidgets.QVBoxLayout(w)
        s = load_settings()

        toprow = QtWidgets.QHBoxLayout()
        self.inv_label = _squish(QtWidgets.QLabel("Press Scan."))
        toprow.addWidget(self.inv_label, 1)
        self.inv_sources_only = QtWidgets.QCheckBox("Only show sources")
        self.inv_sources_only.setToolTip(
            "Hide everything that isn't shots / footage — references, graphics, "
            "FX / elements, slates and published archive segments.")
        # set before connecting: the table doesn't exist yet
        self.inv_sources_only.setChecked(bool(s.get("inv_sources_only")))
        self.inv_sources_only.toggled.connect(self._inv_sources_only_toggled)
        toprow.addWidget(self.inv_sources_only)
        toprow.addWidget(QtWidgets.QLabel("Units:"))
        self.inv_units = QtWidgets.QComboBox()
        self.inv_units.addItems(list(INV_UNITS))
        if s.get("inv_units") in INV_UNITS:
            self.inv_units.setCurrentText(s["inv_units"])
        self.inv_units.setToolTip("Show source / record columns as timecode or frames.")
        self.inv_units.currentTextChanged.connect(self._inv_units_changed)
        toprow.addWidget(self.inv_units)
        self.inv_cols_btn = QtWidgets.QToolButton()
        self.inv_cols_btn.setText("Columns ▾")
        self.inv_cols_btn.setPopupMode(QtWidgets.QToolButton.InstantPopup)
        self.inv_cols_btn.setStyleSheet(
            "QToolButton { background-color: #2d2d2d; color: #cccccc;"
            " border: 1px solid #444444; border-radius: 4px; padding: 6px 12px; }"
            "QToolButton:hover { background-color: #383838; }"
            "QToolButton::menu-indicator { image: none; }")
        menu = QtWidgets.QMenu(self.inv_cols_btn)
        menu.setStyleSheet(STYLE)
        hidden = set(s.get("inv_hidden") or [])
        self._inv_col_actions = {}
        for key, header, togg, _d, _rz in INV_COLS:
            if not togg:
                continue
            act = menu.addAction(header)
            act.setCheckable(True)
            act.setChecked(key not in hidden)   # inv_hidden is the authority once saved
            act.toggled.connect(lambda on, k=key: self._inv_col_toggled(k, on))
            self._inv_col_actions[key] = act
        self.inv_cols_btn.setMenu(menu)
        toprow.addWidget(self.inv_cols_btn)
        v.addLayout(toprow)

        self.inv_table = QtWidgets.QTableWidget(0, len(INV_COLS))
        self.inv_table.setHorizontalHeaderLabels([c[1] for c in INV_COLS])
        self.inv_table.verticalHeader().setVisible(False)
        self.inv_table.setSelectionBehavior(QtWidgets.QAbstractItemView.SelectRows)
        self.inv_table.setSelectionMode(QtWidgets.QAbstractItemView.ExtendedSelection)
        self.inv_table.setEditTriggers(QtWidgets.QAbstractItemView.NoEditTriggers)
        self.inv_table.setSortingEnabled(True)
        _init_table_resize(self.inv_table, stretch_cols=(0, 3))   # seq + source
        self.inv_table.itemSelectionChanged.connect(self._inv_sel_changed)
        v.addWidget(self.inv_table, 1)
        self._apply_inv_hidden()

        note = QtWidgets.QLabel(
            "Every segment carrying real linked media (hub excluded). Role is "
            "blank for shots / footage; anything else — ref, graphic, FX / "
            "elements, slate, published — shows dimmed and stays out of the "
            "conform audits. '*' = set by you on that segment, '(track)' = set "
            "for its track in the Setup Wizard, 'ref?' (amber) = an ambiguous "
            "call that STAYS a source until you decide. Wrong? Select rows and "
            "pick a role below — it persists per project. 'Dup' = duplicate "
            "conflict set, 'Stack' = split-screen/multi-element overlap, '★' = "
            "the longest instance of a multi-instance shot.")
        note.setWordWrap(True)
        note.setStyleSheet("color:#777777;")
        v.addWidget(self._tip(note))

        # every role the Setup Wizard offers, per segment (not just
        # ref/source), so one segment can differ from its track's role
        actions = QtWidgets.QGroupBox("ROLE  (select rows above)")
        a = QtWidgets.QHBoxLayout(actions)
        a.addWidget(QtWidgets.QLabel("Set role:"))
        self.inv_role = QtWidgets.QComboBox()
        self.inv_role.addItem(ROLE_LABELS[""], "")
        for role in TRACK_ROLES:
            self.inv_role.addItem(ROLE_LABELS[role], role)
        self.inv_role.setToolTip(
            "What the selected segments are. '(let CCM decide)' forgets your "
            "call and falls back to the Setup Wizard's track answer, then "
            "auto-detection. Only Shots / footage feed the conform audits and "
            "the hub build.")
        self.inv_role.activated.connect(self._inv_role_chosen)
        a.addWidget(self.inv_role)
        self.inv_role_note = QtWidgets.QLabel("")
        self.inv_role_note.setStyleSheet("color:#777777;")
        a.addWidget(self.inv_role_note)
        a.addStretch(1)
        v.addWidget(actions)
        self._inv_sel_changed()
        return w

    def _selected_inv(self):
        out = []
        for mi in self.inv_table.selectionModel().selectedRows():
            it = self.inv_table.item(mi.row(), 0)
            if it is None:
                continue
            di = it.data(QtCore.Qt.UserRole)
            if isinstance(di, int) and di < len(self._inv):
                out.append(di)
        return out

    def _inv_sel_changed(self):
        idxs = self._selected_inv()
        self.inv_role.setEnabled(bool(idxs))
        ovs = (self._st or {}).get("ref_overrides") or {}
        calls = {ovs.get(self._inv[i].get("seg_key"), "") for i in idxs}
        roles = {self._inv[i].get("role") or "source" for i in idxs}
        # show the selection's own call; a mixed selection shows no call
        pos = self.inv_role.findData(calls.pop()) if len(calls) == 1 else 0
        self.inv_role.setCurrentIndex(max(0, pos))
        if not idxs:
            self.inv_role_note.setText("")
        elif len(roles) == 1:
            role = roles.pop()
            self.inv_role_note.setText("%d selected · currently %s" % (
                len(idxs), ROLE_LABELS.get(role, role)))
        else:
            self.inv_role_note.setText("%d selected · mixed roles" % len(idxs))

    def _inv_role_chosen(self, index):
        self._set_role_override(self.inv_role.itemData(index) or None)

    def _inv_sources_only_toggled(self, on):
        s = load_settings()
        s["inv_sources_only"] = bool(on)
        save_settings(s)
        self._render_inv_table()

    def _set_role_override(self, value):
        """Persist the artist's role call for the selected segments (state
        ref_overrides, keyed by segment uid) and reclassify from the cached
        scan. State-only — this never touches the timeline."""
        idxs = self._selected_inv()
        if not idxs:
            self._say("Select row(s) first.")
            return
        ov = self._st.setdefault("ref_overrides", {})
        n = 0
        for i in idxs:
            k = self._inv[i].get("seg_key")
            if not k:
                continue
            if value:
                ov[k] = value
            else:
                ov.pop(k, None)
            n += 1
        save_state(self._st)
        label = ("Role set to %s" % ROLE_LABELS.get(value, value)) if value \
            else "Role call cleared (back to the track / auto)"
        self._say("%s on %d segment(s); reclassified from the cached scan." % (label, n))
        self._reclassify()

    def _inv_units_changed(self, text):
        s = load_settings(); s["inv_units"] = text; save_settings(s)
        self._s = s                      # keep the per-scan snapshot current
        self._render_inv_table()
        self._render_coverage_detail()

    def _inv_col_toggled(self, key, on):
        s = load_settings()
        hidden = set(s.get("inv_hidden") or [])
        hidden.discard(key) if on else hidden.add(key)
        s["inv_hidden"] = sorted(hidden)
        save_settings(s)
        self._apply_inv_hidden()

    def _apply_inv_hidden(self):
        for c, (key, _h, togg, _d, _rz) in enumerate(INV_COLS):
            act = self._inv_col_actions.get(key)
            self.inv_table.setColumnHidden(c, bool(togg and act and not act.isChecked()))

    def _inv_cell(self, i, d, key):
        if key == "source":
            return d.get("src_name", "") or d.get("src_uid", "")
        if key == "src_in":
            return self._fmt(d["src_in"], d["src_in_tc"])
        if key == "src_out":
            return self._fmt(d["src_out"], d["src_out_tc"])
        if key == "dur":
            return self._fmt(d["src_dur_f"], frames_to_tc(d["src_dur_f"], d["rate"]))
        if key == "rec_in":
            return self._fmt(d["rec_in_f"], d["rec_in_tc"])
        if key == "role":
            role = d.get("role") or "source"
            if role == "source":
                return "ref?" if d.get("ref_conf") == "maybe" else ""
            short = {"graphic": "gfx", "fx": "fx / elements"}.get(role, role)
            how = {"override": "*", "wizard": " (track)"}.get(d.get("ref_conf"), "")
            return short + how
        if key == "dup":
            t = self._dup_tier.get(i, 0)
            return ("T%d" % t) if t else ""
        if key == "stack":
            return self._stack_label.get(i, "")
        if key == "warp":
            return "warp" if d["timewarp"] else ""
        if key == "long":
            return "★" if i in self._longest else ""
        return d.get(key, "")

    def _render_inv_table(self):
        only_src = self.inv_sources_only.isChecked()
        rows = [(i, d) for i, d in enumerate(self._inv) if not d.get("is_hub")
                and (not only_src or (d.get("role") or "source") == "source")]
        dim = QtGui.QColor("#666666")
        amber = QtGui.QColor("#e0b000")
        self.inv_table.blockSignals(True)
        self.inv_table.setSortingEnabled(False)
        self.inv_table.setRowCount(len(rows))
        for r, (i, d) in enumerate(rows):
            is_ref = (d.get("role") or "source") != "source"   # out of the audits
            for c, col in enumerate(INV_COLS):
                val = self._inv_cell(i, d, col[0])
                it = QtWidgets.QTableWidgetItem()
                if isinstance(val, int):
                    it.setData(QtCore.Qt.DisplayRole, val)
                else:
                    it.setText(str(val))
                if c == 0:
                    it.setData(QtCore.Qt.UserRole, i)
                if is_ref:
                    it.setForeground(dim)      # visible but clearly out of the audits
                elif col[0] == "role" and d.get("ref_conf") == "maybe":
                    it.setForeground(amber)
                elif col[0] == "dup" and self._dup_tier.get(i):
                    it.setForeground(QtGui.QColor(
                        "#ff7a45" if self._dup_tier[i] == 1 else "#e0b000"))
                elif col[0] == "stack" and i in self._stack_label:
                    it.setForeground(amber)
                elif col[0] == "warp" and d["timewarp"]:
                    it.setForeground(QtGui.QColor("#c080ff"))
                elif col[0] == "long" and i in self._longest:
                    it.setForeground(QtGui.QColor("#ff7a45"))
                self.inv_table.setItem(r, c, it)
        self.inv_table.setSortingEnabled(True)
        self.inv_table.blockSignals(False)
        _autosize_once(self.inv_table)
        self._inv_sel_changed()

    # ---------------------------------------------------------------- Duplicates tab
    def _build_duplicates_tab(self):
        w = QtWidgets.QWidget()
        v = QtWidgets.QVBoxLayout(w)
        self.dup_label = _squish(QtWidgets.QLabel("Press Scan."))
        v.addWidget(self.dup_label)

        toprow = QtWidgets.QHBoxLayout()
        toprow.addWidget(QtWidgets.QLabel("Show:"))
        self.dup_cat = QtWidgets.QComboBox()
        self.dup_cat.addItems(["Shots", "Graphics", "Both"])
        self.dup_cat.setToolTip(
            "Graphics duplicate just like footage does, but they are a "
            "different job — keep them apart, or look at everything at once. "
            "Run the Setup Wizard first so CCM knows which tracks are which.")
        self.dup_cat.currentTextChanged.connect(
            lambda _t: self._render_duplicates())
        toprow.addWidget(self.dup_cat)
        self.dup_show_ok = QtWidgets.QCheckBox("Show resolved too")
        self.dup_show_ok.setToolTip(
            "Resolved = copies that already share one real source, and split "
            "sources you have ruled on (merged, or acknowledged as intentional "
            "variants). Off = only what still needs a decision.")
        self.dup_show_ok.toggled.connect(lambda _on: self._render_duplicates())
        toprow.addStretch(1)
        toprow.addWidget(self.dup_show_ok)
        v.addLayout(toprow)
        self.dup_table = QtWidgets.QTableWidget(0, 8)
        self.dup_table.setHorizontalHeaderLabels(
            ["Shot", "Sequence", "Role", "Version", "Source", "Src Range",
             "Rec In", "Status"])
        self.dup_table.verticalHeader().setVisible(False)
        self.dup_table.setSelectionBehavior(QtWidgets.QAbstractItemView.SelectRows)
        self.dup_table.setSelectionMode(QtWidgets.QAbstractItemView.ExtendedSelection)
        self.dup_table.setEditTriggers(QtWidgets.QAbstractItemView.NoEditTriggers)
        _init_table_resize(self.dup_table, stretch_cols=(4,))     # source
        v.addWidget(self.dup_table, 1)

        legend = QtWidgets.QLabel(
            "Each block is one shot that exists as multiple copies. Every import "
            "mints a UNIQUE under-the-hood source, so the same shot conformed on "
            "different days (or re-delivered by color prep) silently splits into "
            "separate real sources that drift apart. The check is Flame's own "
            "shared-source relation (right-click → Jump to Shared Source "
            "Segment): one shared source across the copies = intended reuse ✓. "
            "A SPLIT SOURCE is not automatically wrong — it needs a VERDICT. "
            "Merge into Primary = one of them wins everywhere (each stray "
            "keeps its cut and FX; the merge verifies itself with the same "
            "shared-source check). Keep Both = they differ on purpose (a "
            "separate grade, say) and both stay in the conform as elements of "
            "the same shot — amber, not red. Assess first: use the Timelines "
            "tab's previews to compare the copies frame for frame. Both "
            "sources always get their own hub entry, exactly as Flame's "
            "native builder does.")
        legend.setWordWrap(True)
        legend.setStyleSheet("color:#777777;")
        v.addWidget(self._tip(legend))

        actions = QtWidgets.QGroupBox("ACTIONS  (select a block's rows above)")
        a = QtWidgets.QHBoxLayout(actions)
        self.b_dup_compare = QtWidgets.QPushButton("Compare Sources")
        self.b_dup_compare.setToolTip(
            "Put the copies side by side at the SAME source frame and measure "
            "how different they are, so the grade question gets answered here "
            "rather than by eye across tabs. First bake takes a few seconds "
            "per copy; the per-shot cache makes it instant afterwards.")
        self.b_dup_compare.clicked.connect(self._dup_compare)
        a.addWidget(self.b_dup_compare)
        self.b_dup_merge = QtWidgets.QPushButton("Merge into Primary…")
        self.b_dup_merge.setToolTip(
            "Fix a SPLIT SOURCE block: replace each stray copy's media with "
            "its primary's real source (smart_replace_media — every stray "
            "keeps its own cut and Timeline FX), then VERIFY the source is "
            "truly shared afterwards using the same check that flagged it. "
            "Mutation-gated.")
        self.b_dup_merge.clicked.connect(self._dup_merge)
        a.addWidget(self.b_dup_merge)
        self.b_dup_ok = QtWidgets.QPushButton("Keep Both (Variants)")
        self.b_dup_ok.setToolTip(
            "The other verdict: these copies are DIFFERENT ON PURPOSE (e.g. a "
            "separate grade) and both belong in the conform — same shot, "
            "multiple elements, like a split screen. Each keeps its own hub "
            "piece, stacked under ONE shot name, and the block leaves this "
            "list as resolved. Persists per project.")
        self.b_dup_ok.clicked.connect(lambda: self._mark_dup_ok(True))
        a.addWidget(self.b_dup_ok)
        self.b_dup_notok = QtWidgets.QPushButton("Un-mark")
        self.b_dup_notok.setToolTip("Send an acknowledged block back to undecided.")
        self.b_dup_notok.clicked.connect(lambda: self._mark_dup_ok(False))
        a.addWidget(self.b_dup_notok)
        a.addStretch(1)
        v.addWidget(actions)

        # ---- Compare Sources panel: the grade question, answered
        # in this tab. One column per REAL SOURCE, all showing the same
        # source frame, plus a measured difference against the primary.
        self.dup_cmp_panel = QtWidgets.QWidget()
        cv = QtWidgets.QVBoxLayout(self.dup_cmp_panel)
        cv.setContentsMargins(0, 6, 0, 0)
        self.dup_cmp_title = _squish(QtWidgets.QLabel(""))
        self.dup_cmp_title.setStyleSheet("color:#ffffff; font-weight:bold;")
        cv.addWidget(self.dup_cmp_title)
        self.dup_cmp_holder = QtWidgets.QWidget()
        self.dup_cmp_row = QtWidgets.QHBoxLayout(self.dup_cmp_holder)
        self.dup_cmp_row.setContentsMargins(0, 0, 0, 0)
        cmp_scroll = QtWidgets.QScrollArea()
        cmp_scroll.setWidgetResizable(True)
        cmp_scroll.setWidget(self.dup_cmp_holder)
        cmp_scroll.setMinimumHeight(190)
        cmp_scroll.setStyleSheet(
            "QScrollArea { background-color:#0d0d0d; border:1px solid #2a2a2a;"
            " border-radius:4px; }")
        cv.addWidget(cmp_scroll, 1)
        srow = QtWidgets.QHBoxLayout()
        srow.addWidget(QtWidgets.QLabel("Frame:"))
        self.dup_cmp_slider = QtWidgets.QSlider(QtCore.Qt.Horizontal)
        self.dup_cmp_slider.valueChanged.connect(self._dup_cmp_frame)
        srow.addWidget(self.dup_cmp_slider, 1)
        self.dup_cmp_frame_lbl = QtWidgets.QLabel("")
        self.dup_cmp_frame_lbl.setStyleSheet(
            "color:#ff7a45; font-family:'Menlo','Consolas',monospace; font-size:11px;")
        srow.addWidget(self.dup_cmp_frame_lbl)
        b_close = QtWidgets.QPushButton("Hide")
        b_close.clicked.connect(lambda: self.dup_cmp_panel.setVisible(False))
        srow.addWidget(b_close)
        cv.addLayout(srow)
        self.dup_cmp_panel.setVisible(False)
        v.addWidget(self.dup_cmp_panel, 1)
        self._cmp = None
        return w

    def _dup_compare(self):
        """Bake (or reuse) a filmstrip per REAL SOURCE of the selected block
        and show them at one shared source frame. Frame mapping is the proven
        exact one: export_preview_strip matches with preserve_handle=True, so
        file[i] is source frame src_in+i — which is what makes 'the same
        frame in both copies' meaningful rather than approximate."""
        keys = self._selected_dup_keys()
        if not keys:
            self._say("Compare: select a duplicate block's rows first.")
            return
        rd = next((r for r in self._dup_rows if r["key"] == keys[0]), None)
        comps = (rd or {}).get("components") or []
        if len(comps) < 2:
            self._say("Compare: these copies already share ONE real source — "
                      "there is nothing to compare.")
            return
        reps = []
        for comp in comps:
            cand = [i for i in comp
                    if self._inv[i].get("src_in") is not None
                    and self._inv[i].get("src_out") is not None]
            if cand:
                reps.append(max(cand, key=lambda i: (self._inv[i]["src_out"]
                                                     - self._inv[i]["src_in"])))
        if len(reps) < 2:
            self._say("Compare: need a readable source range on at least two "
                      "of the copies.")
            return
        lo = max(self._inv[i]["src_in"] for i in reps)
        hi = min(src_end(self._inv[i]) for i in reps)
        if hi <= lo:
            self._say("Compare: these copies share no overlapping source "
                      "frames — comparing their middles instead.")
            lo, hi = None, None
        strips = {}
        for i in reps:
            d = self._inv[i]
            out_dir = _preview_cache_dir(d.get("ident") or d.get("camera")
                                         or "shot")
            files = _collect_jpegs(out_dir)
            if not files:
                self._say("Compare: baking %s (%s)…" % (d["seq"],
                                                        d.get("camera") or "?"))
                QtWidgets.QApplication.processEvents()
                os.makedirs(out_dir, exist_ok=True)
                files, err = export_preview_strip(d["seg"], out_dir, self._say)
                if err and not files:
                    self._say("Compare: bake failed for %s (%s)" % (d["seq"], err))
            strips[i] = files
        if not any(strips.values()):
            self._say("Compare: no frames could be baked.")
            return
        self._cmp = {"reps": reps, "strips": strips, "lo": lo, "hi": hi}
        # one column per source
        while self.dup_cmp_row.count():
            it = self.dup_cmp_row.takeAt(0)
            if it.widget():
                it.widget().deleteLater()
        self._cmp_imgs, self._cmp_caps = {}, {}
        for k, i in enumerate(reps):
            d = self._inv[i]
            col = QtWidgets.QWidget()
            cl = QtWidgets.QVBoxLayout(col)
            cl.setContentsMargins(4, 4, 4, 4)
            img = QtWidgets.QLabel("—")
            img.setAlignment(QtCore.Qt.AlignCenter)
            # FIXED, not minimum: scaling to the label's own size made every
            # repaint a little bigger than the last (the thumbnails kept
            # growing — a classic size feedback loop)
            img.setFixedSize(self.CMP_W, self.CMP_H)
            img.setScaledContents(False)
            img.setStyleSheet("background-color:#000000; border:1px solid #2a2a2a;")
            cl.addWidget(img)
            head = QtWidgets.QLabel("PRIMARY · %s" % d["seq"] if k == 0
                                    else "copy %d · %s" % (k, d["seq"]))
            head.setStyleSheet("color:%s; font-weight:bold;"
                               % ("#8fb4cc" if k == 0 else "#ff7a45"))
            cl.addWidget(head)
            path = d.get("file_path") or ""
            sub = QtWidgets.QLabel("%s\n%s" % (
                os.path.basename(path) or d.get("src_name") or "?",
                os.path.dirname(path) or ""))
            sub.setWordWrap(True)
            sub.setStyleSheet("color:#777777; font-size:10px;")
            cl.addWidget(sub)
            cap = QtWidgets.QLabel("")
            cap.setStyleSheet("color:#999999; font-size:11px;")
            cl.addWidget(cap)
            self.dup_cmp_row.addWidget(col)
            self._cmp_imgs[i] = img
            self._cmp_caps[i] = cap
        self.dup_cmp_row.addStretch(1)
        self.dup_cmp_panel.setVisible(True)
        if lo is not None:
            self.dup_cmp_slider.setEnabled(True)
            self.dup_cmp_slider.setRange(lo, max(lo, hi - 1))
            self.dup_cmp_slider.setValue((lo + hi) // 2)
            self._dup_cmp_frame((lo + hi) // 2)
        else:
            self.dup_cmp_slider.setEnabled(False)
            self._dup_cmp_frame(None)

    def _img_channels(self, path, w=96, h=54):
        """Flatten a thumbnail to RGB channel values for mean_abs_delta.
        Both copies are scaled to the same small size, so resolution or
        aspect differences never masquerade as a grade difference."""
        img = QtGui.QImage(path)
        if img.isNull():
            return None
        img = img.scaled(w, h, QtCore.Qt.IgnoreAspectRatio,
                         QtCore.Qt.SmoothTransformation).convertToFormat(
                             QtGui.QImage.Format_RGB888)
        vals = []
        for y in range(img.height()):
            for x in range(img.width()):
                c = img.pixelColor(x, y)
                vals.extend((c.red(), c.green(), c.blue()))
        return vals

    def _dup_cmp_frame(self, f):
        """Show source frame `f` in every column and measure each copy
        against the primary. CCM reports the number and the pictures; the
        grade call stays with the artist."""
        cmp_ = getattr(self, "_cmp", None)
        if not cmp_:
            return
        reps, strips = cmp_["reps"], cmp_["strips"]
        rate = self._inv[reps[0]].get("rate") or 24.0
        base_vals, base_ok = None, None
        for k, i in enumerate(reps):
            d = self._inv[i]
            files = strips.get(i) or []
            if f is None:                      # no overlap — show each middle
                idx = len(files) // 2
                shown = (d["src_in"] or 0) + idx
            else:
                idx = f - (d["src_in"] or 0)
                shown = f
            path = files[idx] if 0 <= idx < len(files) else None
            lbl, cap = self._cmp_imgs[i], self._cmp_caps[i]
            if path is None:
                lbl.setPixmap(QtGui.QPixmap())
                lbl.setText("no frame\nhere")
                cap.setText("")
                continue
            pm = QtGui.QPixmap(path)
            if pm.isNull():
                lbl.setText("unreadable")
                continue
            lbl.setText("")
            lbl.setPixmap(pm.scaled(QtCore.QSize(self.CMP_W, self.CMP_H),
                                    QtCore.Qt.KeepAspectRatio,
                                    QtCore.Qt.SmoothTransformation))
            vals = self._img_channels(path)
            if k == 0:
                base_vals, base_ok = vals, path
                cap.setText("frame %d · %s" % (shown, frames_to_tc(shown, rate)))
            else:
                delta = mean_abs_delta(base_vals, vals) if base_ok else None
                verdict = grade_verdict(delta)
                cap.setText("Δ %s vs primary — %s"
                            % ("—" if delta is None else "%.2f%%" % delta,
                               verdict))
                cap.setStyleSheet(
                    "font-size:11px; color:%s;"
                    % ("#ff5555" if verdict == "GRADE DIFFERS"
                       else "#8fb4cc" if verdict == "identical"
                       else "#e0b000"))
        n = len(reps)
        self.dup_cmp_title.setText(
            "Comparing %d real sources of this shot — same source frame in "
            "every column. Δ is measured, not judged: near 0%% means the "
            "pictures match, a few %% or more means the grades differ." % n)
        if f is None:
            self.dup_cmp_frame_lbl.setText("no shared frames — middles shown")
        else:
            self.dup_cmp_frame_lbl.setText("%d · %s" % (f, frames_to_tc(f, rate)))

    def _mark_dup_ok(self, ok):
        """Record the OTHER verdict: these copies differ on purpose and both
        belong in the conform as elements of one shot."""
        keys = self._selected_dup_keys()
        if not keys:
            self._say("Select the rows of a duplicate block first.")
            return
        st = self._st
        sset = set(st.get("sanctioned_dupes", []))
        for k in keys:
            (sset.add if ok else sset.discard)(k)
        st["sanctioned_dupes"] = sorted(sset)
        save_state(st)
        self._say("%s %d block(s)."
                  % ("Acknowledged as variants" if ok else "Back to undecided",
                     len(keys)))
        self._reannotate_dups()

    def _dup_merge(self):
        """Fix a flagged duplicate: merge a SPLIT SOURCE
        block back onto ONE real source. Mechanics are the proven
        pair — copy_to_media_panel of a primary member, or of the live Conform
        Hub piece sharing the primary source when no spot covers the stray
        (the copy SHARES the primary's real source) + smart_replace_media into
        each stray (keeps
        the stray's own cut and Timeline FX, picture-identical on-box). Then
        SELF-VERIFY: re-read shared_source_segments on the stray — the fix
        is checked by the exact relation that flagged it, so a ✓ here is the
        ✓ the next scan will show."""
        keys = self._selected_dup_keys()
        if not keys:
            self._say("Select the rows of a SPLIT SOURCE block first.")
            return
        jobs = [r for r in self._dup_rows
                if r["key"] in keys and r.get("split") and r.get("strays")]
        if not jobs:
            self._say("Merge: the selected block(s) already share one source.")
            return
        if not self._mutations_ok("Merge into Primary"):
            return
        n_strays = sum(len(r["strays"]) for r in jobs)
        if QtWidgets.QMessageBox.question(
                self, "Merge into Primary",
                "Replace the media of %d stray cop%s across %d shot(s) with "
                "the primary's real source (each stray keeps its own cut and "
                "Timeline FX), then verify the source is truly shared.\n\n"
                "Each stray is merged onto a use of the primary — or the "
                "primary's Conform Hub piece — that contains its frames. "
                "Skipped: unreadable timewarps, and strays nothing of the "
                "primary covers. Undoable in Flame."
                % (n_strays, "y" if n_strays == 1 else "ies", len(jobs)),
                QtWidgets.QMessageBox.Yes | QtWidgets.QMessageBox.No) \
                != QtWidgets.QMessageBox.Yes:
            return
        # the rows this merge planned on: a Scan pressed while the loop yields
        # to the event queue would rebind self._inv and remap the indices onto
        # other segments — so hold the list, and keep Scan off until the end
        inv = self._inv
        smap = self._shared_map()           # the same scan as `inv`
        self._remember_timeline()
        self._set_scan_enabled(False)
        merged = skipped = errors = 0
        try:
            reel = _scratch_reel()
            for r in jobs:
                comp = r["components"][0]
                first = inv[comp[0]]
                label = (first.get("shot") or first.get("name")
                         or first.get("camera") or r["key"])
                prim_keys = {inv[i].get("seg_key") for i in comp}
                # live hub pieces of the PRIMARY source (never one of a stray's
                # source): built to the union of every use's visible frames,
                # they cover a stray that no single spot covers
                comp_set, stray_set = set(comp), set(r["strays"])
                live_hubs = [h for h in range(len(inv)) if _is_live_hub(inv[h])
                             and inv[h].get("pub_nn") is None]
                hubs = [h for h in live_hubs if smap.get(h, set()) & comp_set
                        and not smap.get(h, set()) & stray_set]
                merged_ok = set()
                # the copy comes from ONE use, so each stray gets a use of the
                # primary that covers its own frames (pick_merge_master) — the
                # widest use alone misses cutdowns whose windows aren't nested
                plan = {}
                for si in r["strays"]:
                    d = inv[si]
                    who = "%s in %s" % (label, d["seq"])
                    if d["timewarp"] and not d.get("tw_model"):
                        skipped += 1
                        self._say("  ~ %s skipped (timewarp, curve unreadable)"
                                  % who)
                        continue
                    live = (_frames(getattr(d["seg"], "source_in", None)),
                            _frames(getattr(d["seg"], "source_out", None)))
                    if None not in live and live != (d["src_in"], d["src_out"]):
                        skipped += 1
                        self._say("  ~ %s skipped: it changed since the scan — "
                                  "press Scan and merge again" % who)
                        continue
                    mi, near = pick_merge_master(inv, comp, si, hubs)
                    if mi is None:
                        skipped += 1
                        if near is None:
                            self._say("  ⚠ %s skipped: the primary has no "
                                      "readable source range" % who)
                        else:
                            n = inv[near]
                            self._say("  ⚠ %s skipped: it uses %s → %s, past "
                                      "what the primary source's media holds "
                                      "(closest use: %s, %s → %s) — Keep Both, "
                                      "or merge the other way"
                                      % (who, d["src_in_tc"], d["src_out_tc"],
                                         n["seq"], n["src_in_tc"],
                                         n["src_out_tc"]))
                        continue
                    plan.setdefault(mi, []).append(si)
                for mi, strays in plan.items():
                    m = inv[mi]
                    clip = None
                    try:
                        clip = m["seg"].copy_to_media_panel(reel)
                        if isinstance(clip, (list, tuple)):
                            clip = clip[0] if clip else None
                        if clip is None:
                            raise RuntimeError("copy_to_media_panel returned "
                                               "nothing")
                    except Exception as e:
                        errors += len(strays)
                        self._say("⚠ %s: couldn't copy the use in %s (%s) — "
                                  "not merged: %s"
                                  % (label, m["seq"], e,
                                     ", ".join(inv[si]["seq"] for si in strays)))
                        continue
                    try:
                        for si in strays:
                            d = inv[si]
                            who = "%s in %s" % (label, d["seq"])
                            try:
                                before = _frames(getattr(d["seg"],
                                                         "record_duration", None))
                                in_before = _frames(getattr(d["seg"], "source_in",
                                                            None))
                                d["seg"].smart_replace_media(clip)
                                after = _frames(getattr(d["seg"],
                                                        "record_duration", None))
                                if before is not None and after is not None \
                                        and before != after:
                                    errors += 1
                                    self._say("  ⚠ %s: its duration changed "
                                              "%s→%s in the replace — check it "
                                              "(Undo in Flame restores it)"
                                              % (who, before, after))
                                    continue
                                # the replace lines the new media up by
                                # timecode; a moved first source frame means
                                # the picture moved
                                now_in = _frames(getattr(d["seg"], "source_in",
                                                         None))
                                if None not in (now_in, in_before) \
                                        and now_in != in_before:
                                    errors += 1
                                    rate = d.get("rate") or 24
                                    self._say("  ⚠ %s: its first source frame "
                                              "moved %s → %s in the replace — "
                                              "check the picture (Undo in Flame "
                                              "restores it)"
                                              % (who, frames_to_tc(in_before, rate),
                                                 frames_to_tc(now_in, rate)))
                                    continue
                                try:
                                    now = {_seg_key(x) for x in
                                           (d["seg"].shared_source_segments()
                                            or [])}
                                except Exception:
                                    now = set()
                                if now & prim_keys:
                                    merged += 1
                                    merged_ok.add(si)
                                    past_cut = not (
                                        m["src_in"] <= d["src_in"]
                                        and d["src_out"] <= m["src_out"])
                                    self._say("  ✓ %s — source now shared with "
                                              "the primary (copied from %s%s)"
                                              % (who, m["seq"],
                                                 "; the frames past its cut "
                                                 "come from the source media"
                                                 if past_cut else ""))
                                else:
                                    errors += 1
                                    self._say("  ⚠ %s replaced but STILL NOT "
                                              "SHARED — check it in Flame "
                                              "(Undo restores it)" % who)
                            except Exception as e:
                                errors += 1
                                self._say("  ⚠ %s: %s" % (who, e))
                    finally:
                        ok, err = _safe_delete(clip)
                        if not ok:
                            self._say("  ⚠ the temp copy of %s is still in the "
                                      "Media Panel (%s) — delete it by hand"
                                      % (label, err))
                    QtWidgets.QApplication.processEvents()
                # a hub piece left holding ONLY the merged strays' old source
                # (in the primary hub): nothing uses it any more
                primary = _primary_hub_seq(
                    inv, (self._s or {}).get("sources_seq_name", ""))
                for h in live_hubs:
                    peers = smap.get(h, set())
                    users = {j for j in peers if not inv[j].get("is_hub")}
                    if (merged_ok and inv[h]["seq"] == primary
                            and not peers & comp_set and users
                            and users <= merged_ok):
                        self._say("  ℹ %s: %s %s still holds a piece for the "
                                  "merged duplicate's old source — rebuild "
                                  "the hub to drop it."
                                  % (label, inv[h]["seq"], track_label(
                                      inv[h].get("track_id"),
                                      inv[h].get("track_name"))))
        except Exception as e:
            errors += 1
            self._say("⚠ Merge into Primary stopped: %s" % e)
        finally:
            self._set_scan_enabled(True)
        self._say("Merge into Primary: %d merged+verified, %d skipped, "
                  "%d problem(s). Rescanning." % (merged, skipped, errors))
        self._rescan_after_change()

    def _render_duplicates(self):
        rows_data = self._dup_rows          # computed once per scan in _recompute
        cat = self.dup_cat.currentText() if hasattr(self, "dup_cat") else "Shots"
        if cat != "Both":
            want = "graphic" if cat == "Graphics" else "shot"
            rows_data = [r for r in rows_data
                         if want in r.get("cats", {"shot"})]
        # the list is an INBOX: it holds what still needs a decision. A split
        # source lands here undecided (never hidden by default);
        # recording EITHER verdict — merged, or acknowledged as variants —
        # resolves it and files it away behind the checkbox.
        problem = [r for r in rows_data if r.get("split") and not r["sanctioned"]]
        okset = [r for r in rows_data if r not in problem]
        n_variants = sum(1 for r in okset if r.get("split"))
        n_orph = len(getattr(self, "_orphans", ()))
        head = (
            "%d shot(s) exist as multiple copies — %d NEED A DECISION · "
            "resolved: %d acknowledged variant(s), %d sharing one source%s"
            % (len(rows_data), len(problem), n_variants,
               len(okset) - n_variants,
               "  ·  ⚠ %d ORPHAN segment(s) not in the hub" % n_orph
               if n_orph else ""))
        # an empty scan must not read "0 NEED A DECISION": that looks like
        # everything was resolved
        self.dup_label.setText(self._empty_scope_hint()
                               if getattr(self, "_n_seqs", None) == 0 else head)
        show = (problem + okset) if (hasattr(self, "dup_show_ok")
                                     and self.dup_show_ok.isChecked()) else problem
        # flatten to table rows with spacer separators; row -> block key in UserRole
        flat = []
        for si, rd in enumerate(show):
            for ci in rd["members"]:
                flat.append((rd, self._inv[ci], ci))
            if si != len(show) - 1:
                flat.append((None, None, None))
        self.dup_table.blockSignals(True)
        # the selection is by row index: after a rebuild the same rows hold
        # different blocks, so a kept selection would point Merge / Keep Both
        # at a block the user never picked
        self.dup_table.clearSelection()
        self.dup_table.setRowCount(len(flat))
        red, grey = QtGui.QColor("#ff5555"), QtGui.QColor("#8fb4cc")
        for r, (rd, d, inv_i) in enumerate(flat):
            if rd is None:
                for c in range(self.dup_table.columnCount()):
                    cell = QtWidgets.QTableWidgetItem("")
                    cell.setFlags(QtCore.Qt.NoItemFlags)
                    cell.setBackground(QtGui.QColor("#0d0d0d"))
                    self.dup_table.setItem(r, c, cell)
                self.dup_table.setRowHeight(r, 6)
                continue
            rng = "%s → %s" % (d["src_in_tc"], d["src_out_tc"])
            n_sh = d.get("shared_n_set", 0)
            amber = QtGui.QColor("#e0b000")
            if d.get("split_stray"):
                if rd["sanctioned"]:
                    status = "VARIANT — intentional, kept as its own element"
                    col = amber
                else:
                    status = "SPLIT SOURCE — decide: Merge, or Keep Both"
                    if d.get("dup_hint"):
                        status += "  · " + d["dup_hint"]
                    col = red
            elif rd.get("split"):
                status = "primary" + (
                    " · variants acknowledged" if rd["sanctioned"]
                    else " — merge the split copies in, or Keep Both")
                col = amber if rd["sanctioned"] else grey
            elif rd["sanctioned"]:
                status = "marked OK"
                col = QtGui.QColor("#888888")
            else:
                status = "shared ✓ (%d)" % (n_sh + 1)
                col = grey
            if inv_i in getattr(self, "_orphans", ()):
                status += "  · ORPHAN (not in hub)"
                col = QtGui.QColor(ORPHAN_EDGE)
            if d.get("shared_err"):
                status += "  (shared-source read failed — see probe)"
            vals = [d.get("shot") or d.get("name") or d.get("camera") or "?",
                    d["seq"], ROLE_LABELS.get(d.get("role"), d.get("role") or ""),
                    version_token(d.get("file_path"), d.get("src_name")),
                    d.get("src_name", "") or d.get("src_uid", ""),
                    rng, d["rec_in_tc"], status]
            for c, val in enumerate(vals):
                cell = QtWidgets.QTableWidgetItem(str(val))
                if c == 0:
                    cell.setData(QtCore.Qt.UserRole, rd["key"])
                elif c == 2 and d.get("role") == "graphic":
                    cell.setForeground(QtGui.QColor("#7fd1b9"))
                elif c == 3 and val:
                    cell.setForeground(QtGui.QColor("#e0b000"))
                elif c == 7:
                    cell.setForeground(col)
                self.dup_table.setItem(r, c, cell)
        self.dup_table.blockSignals(False)
        _autosize_once(self.dup_table)

    def _selected_dup_keys(self):
        keys = []
        for mi in self.dup_table.selectionModel().selectedRows():
            it = self.dup_table.item(mi.row(), 0)
            if it is None:
                continue
            k = it.data(QtCore.Qt.UserRole)
            if k and k not in keys:
                keys.append(k)
        return keys

    def _shared_map(self):
        """Row-index → set of row indices sharing the SAME REAL SOURCE, over
        the whole scan (hub rows included). Built once per scan and cached:
        Flame's `shared_source_segments()` is the only truth about
        under-the-hood identity, and three separate features need it —
        duplicate verdicts, the orphan audit, and (most importantly) the
        hub builder, which must place one piece per real SOURCE, not one per
        logical shot. Rows whose read fails carry shared_err and simply
        share with nothing."""
        if getattr(self, "_shared_all", None) is not None:
            return self._shared_all
        by_key, by_base = key_index([d.get("match_key") for d in self._inv])
        out = {}
        for i, d in enumerate(self._inv):
            if "shared_keys" not in d:
                try:
                    d["shared_keys"] = set(
                        _match_key(s) for s in
                        (d["seg"].shared_source_segments() or []))
                except Exception as e:
                    d["shared_keys"] = set()
                    d["shared_err"] = str(e)
            peers = set()
            for k in d["shared_keys"]:
                for j in match_rows(k, by_key, by_base):
                    if j != i:
                        peers.add(j)
            out[i] = peers
        # make it symmetric — a one-sided read still binds the pair
        for i, peers in list(out.items()):
            for j in peers:
                out.setdefault(j, set()).add(i)
        self._shared_all = out
        return out

    def _source_components(self, members):
        """`members` split into groups that share one real source."""
        smap = self._shared_map()
        mset = set(members)
        return shared_components(
            list(members), {i: (smap.get(i, set()) & mset) for i in members})

    def _attach_strays(self, rows):
        """Split each dup set by real source: the discriminator is Flame's
        UNDER-THE-HOOD source sharing, NOT segment connections (an earlier
        conn_n check answered a different question and missed a
        3-shared-+-1-duplicated test outright). Members of each dup set are
        partitioned by seg.shared_source_segments() into components: one
        component = intended reuse; several = SPLIT SOURCES — everything
        outside the largest component is a stray copy to merge (Merge into
        Primary, _dup_merge, does the merge). API note: existence is
        GFX-verified; args/reach are unconfirmed — a member whose
        read fails carries shared_err and counts as unshared."""
        smap = self._shared_map()
        for r in rows:
            # categories live HERE, not in _recompute: recording a verdict
            # rebuilds these rows through annotate_dup_sets, which drops any
            # field it doesn't own — so cats computed upstream vanished and
            # every set fell back to "shot", dragging graphics into the Shots
            # list the moment you hit Keep Both.
            # A set can straddle categories (a track tagged Graphics in one
            # spot but not yet in another); record every category present and
            # let it appear under each.
            r["cats"] = {("graphic" if self._inv[i].get("role") == "graphic"
                          else "shot") for i in r["members"]}
            comps = self._source_components(r["members"])
            r["components"] = comps
            r["split"] = len(comps) > 1
            r["strays"] = [i for c in comps[1:] for i in c]
            # first duplicate-taxonomy classifier step: file_path
            # separates scenario #1 (same file imported twice — safe merge)
            # from #2+ (separate delivery — a HUMAN verifies grade first)
            prim_paths = {self._inv[i].get("file_path") for i in comps[0]
                          if self._inv[i].get("file_path")}
            for i in r["members"]:
                d = self._inv[i]
                d["split_stray"] = i in r["strays"]
                d["shared_n_set"] = len(smap.get(i, ()) & set(r["members"]))
                hint = ""
                if d["split_stray"] and d.get("file_path") and prim_paths:
                    hint = ("same file (scenario #1 — safe merge)"
                            if d["file_path"] in prim_paths else
                            "different file (verify grade first)")
                d["dup_hint"] = hint
        return rows

    def _reannotate_dups(self):
        """Refresh sanction annotations on the cached dup groups — no need to
        re-run the O(n²) grouping for a triage click."""
        self._dup_rows = self._attach_strays(
            annotate_dup_sets(self._inv, self._dup_rows,
                              self._st.get("dup_decisions"),
                              self._st.get("sanctioned_dupes")))
        self._render_duplicates()

    def _set_decision(self, value):
        keys = self._selected_dup_keys()
        if not keys:
            self._say("Select the rows of a conflict set first.")
            return
        st = self._st
        for k in keys:
            if value:
                st.setdefault("dup_decisions", {})[k] = value
            else:
                st.get("dup_decisions", {}).pop(k, None)
        save_state(st)
        self._say("Recorded '%s' on %d conflict set(s)." % (value or "clear", len(keys)))
        self._reannotate_dups()

    def _toggle_sanction(self):
        keys = self._selected_dup_keys()
        if not keys:
            self._say("Select the rows of a conflict set first.")
            return
        st = self._st
        sset = set(st.get("sanctioned_dupes", []))
        for k in keys:
            sset.discard(k) if k in sset else sset.add(k)
        st["sanctioned_dupes"] = sorted(sset)
        save_state(st)
        self._say("Toggled sanction on %d conflict set(s)." % len(keys))
        self._reannotate_dups()

    # ---------------------------------------------------------------- previews
    PV_TIP = ("See the shot. The same picture panel in Timelines, Connections, "
              "Hub and Ledger, and one switch for all of them. Scrub it (drag "
              "the bar or the picture, wheel on the bar, ←/→ keys, shift = 10 "
              "frames) and zoom it. The first look at a shot bakes its frames "
              "(a few seconds); the per-shot cache in the project makes every "
              "later look instant, in every tab. The timeline is never touched.")

    def _pv_button(self):
        """A tab's Preview toggle. Every tab has one in its top row and they
        are ONE switch — persisted as auto_preview."""
        b = QtWidgets.QPushButton("Preview")
        b.setCheckable(True)
        b.setChecked(self._pv_on)
        b.setToolTip(self.PV_TIP)
        b.toggled.connect(self._pv_set_on)
        self._pv_buttons.append(b)
        return b

    def _pv_panel(self, key, show_title=True):
        pv = ShotPreview(show_title=show_title)
        pv.rebakeRequested.connect(lambda k=key: self._pv_rebake(k))
        pv.bakeAllRequested.connect(self._pv_bake_all)
        self._pv_panels[key] = pv
        return pv

    def _pv_set_on(self, on):
        on = bool(on)
        self._pv_on = on
        for b in self._pv_buttons:
            if b.isChecked() != on:
                b.blockSignals(True)
                b.setChecked(on)
                b.blockSignals(False)
        s = load_settings()
        s["auto_preview"] = on
        save_settings(s)
        for apply in self._pv_vis.values():
            apply(on)
        if on:
            self._pv_flush()

    def _pv_current_key(self):
        w = self.tabs.currentWidget() if hasattr(self, "tabs") else None
        return next((k for k, tw in self._pv_tabs.items() if tw is w), None)

    def _pv_reset(self):
        """A scan renumbers the rows — every request and loaded strip is
        stale (the disk cache is not)."""
        self._pv_req, self._pv_loaded, self._pv_token = {}, {}, {}
        for pv in self._pv_panels.values():
            pv.set_message("Pick a shot.", title="")

    def _pv_pick(self, members, prefer=None):
        return preview_pick(self._inv, members, prefer,
                            (getattr(self, "_hub_rep", None) or {}).get("primary"))

    def _pv_show(self, key, members, prefer=None, title=""):
        """A tab's selection changed: remember what its preview should show,
        and load it now if that tab is on screen with previews on — a bake
        only ever runs for the tab being looked at."""
        self._pv_req[key] = (list(members or []), prefer, title)
        if self._pv_on and key == self._pv_current_key():
            self._pv_flush(key)

    def _pv_flush(self, key=None):
        key = key or self._pv_current_key()
        pv = self._pv_panels.get(key)
        if pv is None or not self._pv_on:
            return
        members, prefer, title = self._pv_req.get(key) or ([], None, "")
        if not members and prefer is None:
            pv.set_message("Pick a shot.", title="")
            self._pv_loaded[key] = None
            return
        idx = self._pv_pick(members, prefer)
        if idx is None:
            pv.set_message("Nothing to preview — no readable source range.",
                           title=title)
            self._pv_loaded[key] = None
            return
        if self._pv_loaded.get(key) == (idx, title) and pv.has_strip():
            return
        d = self._inv[idx]
        files = _collect_jpegs(_preview_cache_dir(
            d.get("ident") or d.get("camera") or "shot"))
        if files:
            self._pv_load(key, idx, files, members, title)
            return
        # not cached: bake after a beat, so arrowing down a table doesn't
        # bake every row it passes
        pv.set_message("Baking a preview of %s…\n(a few seconds, once — "
                       "then it's cached)" % (title or d.get("camera") or "the shot"),
                       title=title)
        self._pv_loaded[key] = None
        token = object()
        self._pv_token[key] = token
        QtCore.QTimer.singleShot(300, lambda k=key, t=token: self._pv_bake_pending(k, t))

    def _pv_bake_pending(self, key, token):
        if self._pv_token.get(key) is not token:
            return                          # the selection moved on
        if not self._pv_on or key != self._pv_current_key():
            return
        members, prefer, title = self._pv_req.get(key) or ([], None, "")
        idx = self._pv_pick(members, prefer)
        if idx is None:
            return
        files, err = self._pv_bake(idx)
        if not files:
            self._pv_panels[key].set_message(
                "Preview failed:\n%s" % (err or "no frames came back"), title=title)
            return
        self._pv_load(key, idx, files, members, title)

    def _pv_bake(self, idx):
        """Bake (or reuse) one segment's filmstrip in the per-shot cache.
        Returns (files, error)."""
        d = self._inv[idx]
        out_dir = _preview_cache_dir(d.get("ident") or d.get("camera") or "shot")
        files = _collect_jpegs(out_dir)
        if files:
            return files, None
        if d.get("seg") is None:
            return [], "no live segment — Scan again"
        self._say("Preview: baking %s from %s (%d frames)…" % (
            d.get("camera") or "?", d["seq"], d["src_out"] - d["src_in"] + 1))
        QtWidgets.QApplication.processEvents()      # let the message paint
        os.makedirs(out_dir, exist_ok=True)
        files, err = export_preview_strip(d["seg"], out_dir, self._say)
        if err and not files:
            self._say("Preview failed: %s" % err)
        return files, err

    def _pv_load(self, key, idx, files, members, title):
        d = self._inv[idx]
        pv = self._pv_panels[key]
        was = self._pv_loaded.get(key)
        keep = pv.frame if (was and was[0] == idx) else None
        if key == "tl" and self.cov_inspector.cursor is not None:
            keep = self.cov_inspector.cursor
        pv.set_strip(files, d["src_in"], d.get("rate") or 24.0,
                     strip_uses(self._inv, members), title, keep)
        self._pv_loaded[key] = (idx, title)
        if key == "tl" and pv.frame is not None:
            self.cov_inspector.set_cursor(pv.frame)
            self._cov_readout(pv.frame)

    def _pv_rebake(self, key):
        members, prefer, _title = self._pv_req.get(key) or ([], None, "")
        idx = self._pv_pick(members, prefer)
        if idx is None:
            return
        d = self._inv[idx]
        out_dir = _preview_cache_dir(d.get("ident") or d.get("camera") or "shot")
        if os.path.isdir(out_dir):
            shutil.rmtree(out_dir, ignore_errors=True)
            self._say("Preview cache wiped: %s" % out_dir)
        for k, was in list(self._pv_loaded.items()):
            if was and was[0] == idx:       # other tabs showed the wiped files
                self._pv_loaded[k] = None
        self._pv_flush(key)

    def _pv_bake_all(self):
        """Bake every shot of the scan that has no preview yet — one pass up
        front, then every tab opens every shot instantly."""
        picks, seen = [], set()
        for members in self._groups:
            idx = self._pv_pick(members)
            if idx is None:
                continue
            d = self._inv[idx]
            ident = d.get("ident") or d.get("camera") or "shot"
            if ident not in seen:
                seen.add(ident)
                picks.append(idx)
        todo = [i for i in picks if not _collect_jpegs(_preview_cache_dir(
            self._inv[i].get("ident") or self._inv[i].get("camera") or "shot"))]
        if not todo:
            self._say("Bake All: every shot already has a preview (%d)." % len(picks))
            return
        if QtWidgets.QMessageBox.question(
                self, "Bake All",
                "Bake previews for %d shot(s)? A few seconds each; Flame is busy "
                "until it's done. %d already have one." % (len(todo), len(picks) - len(todo)),
                QtWidgets.QMessageBox.Yes | QtWidgets.QMessageBox.No) != QtWidgets.QMessageBox.Yes:
            return
        done = fail = 0
        for n, i in enumerate(todo, 1):
            self._say("Bake All %d/%d…" % (n, len(todo)))
            files, _err = self._pv_bake(i)
            done, fail = done + bool(files), fail + (not files)
        self._say("Bake All: %d baked, %d already cached, %d failed."
                  % (done, len(picks) - len(todo), fail))
        self._pv_flush()

    # ------------------------------------------------------- Coverage drawer
    def _build_coverage_drawer(self):
        """The retired Coverage tab, reborn as a drawer under the Timelines
        lanes, so Timelines is both the map and the coverage view.
        The source list is gone — clicking a block IS the
        selection; the Coverage button on the tab shows/hides this whole
        area to trade for lane real estate."""
        drawer = QtWidgets.QWidget()
        panel_l = QtWidgets.QVBoxLayout(drawer)
        panel_l.setContentsMargins(0, 4, 0, 0)
        panel_l.setSpacing(6)
        self.cov_title = _squish(QtWidgets.QLabel("Click a shot block above."))
        self.cov_title.setStyleSheet("color:#ffffff; font-weight:bold;")
        panel_l.addWidget(self.cov_title)
        self.cov_inspector = ShotInspector()
        self.cov_inspector.cursorMoved.connect(self._cov_cursor_moved)
        self.cov_inspector.groupToggled.connect(self._cov_group_toggled)
        self._cov_open = set()      # twirled-open use groups
        cov_scroll = QtWidgets.QScrollArea()
        cov_scroll.setWidgetResizable(True)
        cov_scroll.setWidget(self.cov_inspector)
        cov_scroll.setMinimumHeight(120)
        cov_scroll.setStyleSheet("QScrollArea { border: 1px solid #2a2a2a; border-radius: 4px; }")
        # diagram | preview — pictures live in a stable spot beside the
        # timeline (a thumb chasing the playhead is
        # unreadable); the splitter lets the user trade real estate freely.
        # The panel is the SAME one every tab uses — its own
        # scrub bar and the diagram's cursor drive each other.
        self.cov_split = QtWidgets.QSplitter(QtCore.Qt.Horizontal)
        self.cov_split.addWidget(cov_scroll)
        self.tl_pv = self._pv_panel("tl", show_title=False)
        self.tl_pv.frameChanged.connect(self._tl_pv_scrubbed)
        self.cov_split.addWidget(self.tl_pv)
        self.cov_split.setStretchFactor(0, 1)
        self.cov_split.setStretchFactor(1, 1)
        self.cov_split.setChildrenCollapsible(True)
        self.cov_split.setSizes([620, 300])
        panel_l.addWidget(self.cov_split, 1)
        self.cov_cursor_lbl = _squish(QtWidgets.QLabel(
            "Click / drag on the diagram to scrub the source."))
        self.cov_cursor_lbl.setStyleSheet("color:#ff7a45; font-family:'Menlo','Consolas',monospace; font-size:11px;")
        panel_l.addWidget(self.cov_cursor_lbl)
        self.cov_detail = QtWidgets.QTableWidget(0, 5)
        self.cov_detail.setHorizontalHeaderLabels(["Kind", "In", "Out", "Len", "Used by"])
        self.cov_detail.verticalHeader().setVisible(False)
        self.cov_detail.setEditTriggers(QtWidgets.QAbstractItemView.NoEditTriggers)
        _init_table_resize(self.cov_detail, stretch_cols=(4,))    # used-by
        self.cov_detail.setMaximumHeight(160)
        panel_l.addWidget(self.cov_detail, 1)
        # what the Coverage button hides; the Preview button keeps the picture
        self._cov_only = (cov_scroll, self.cov_cursor_lbl, self.cov_detail)
        self._cov_members = []
        self._cov_rep = None
        self._cov_gi = None       # selection = a group index, set by block clicks
        return drawer

    def _selected_group(self):
        gi = getattr(self, "_cov_gi", None)
        if isinstance(gi, int) and 0 <= gi < len(self._groups):
            return self._groups[gi]
        return None

    def _render_coverage_detail(self):
        members = self._selected_group()
        self._cov_members = members or []
        self._cov_rep = None
        if not members:
            self.cov_title.setText("Click a shot block above.")
            self.cov_inspector.set_data((0, 0), [], [], [], [])
            self.cov_detail.setRowCount(0)
            return
        rep = coverage_report(self._inv, members, self._s)
        self._cov_rep = rep
        lo, hi = rep["extent"]
        cam = self._inv[members[0]].get("camera") or "?"
        warp = "  • TIMEWARP present (lane shows its full source span, min→max)" if rep["has_timewarp"] else ""
        if lo is None:
            self.cov_title.setText("%s — no source range%s" % (cam, warp))
            self.cov_inspector.set_data((0, 0), [], [], [], [])
            self.cov_detail.setRowCount(0)
            return
        prop = split_proposal(self._inv, members, rep["pieces"])
        split_txt = ""
        if len(prop) >= 2:
            split_txt = "  •  NATURAL SPLIT: " + "  ·  ".join(
                "%s ← %s" % (pc["label"], ", ".join(pc["seqs"]) or "—") for pc in prop)
        # title wording: the name matters, the extent numbers and the
        # "% used" never did in daily use, and "consumers" is jargon
        name = self._inv[members[0]].get("src_name") or cam
        if len(name) > 60:
            name = name[:59] + "…"
        self.cov_title.setText("%s — %d shared source(s)%s%s"
                               % (name, rep["n_consumers"], warp, split_txt))
        # lanes: hub first, then ONE row per distinct use, twirlable. 20 spots
        # cutting a shot identically drew 20 identical rows and buried the one
        # that differed; folding to "×20" alone lost WHICH spots — so the row
        # folds by default and opens in place.
        rate = self._inv[members[0]]["rate"]
        lanes = []
        order = [i for i in members if self._inv[i].get("is_hub")]
        counts = {}
        for g in collapse_uses(self._inv, members):
            # NB: not `rep` — that name holds the coverage report a few lines
            # up, and shadowing it broke the whole panel
            rep_i = g["rep"]
            counts[rep_i] = g
            order.append(rep_i)
            if g["n"] > 1 and g["key"] in self._cov_open:
                order += [m for m in g["members"] if m != rep_i]
        for i in order:
            d = self._inv[i]
            rng = "%s–%s" % (self._fmt(d["src_in"], d["src_in_tc"]),
                             self._fmt(d["src_out"], d["src_out_tc"]))
            if d["timewarp"]:
                pct = (d.get("tw_model") or {}).get("pct")
                if pct is None and d.get("src_dur_f") and d.get("rec_dur_f"):
                    pct = round(100.0 * d["src_dur_f"] / d["rec_dur_f"], 1)
                if pct:
                    rng += "  · TW %.4g%%" % pct
            label = ("[HUB] %s" % d["seq"]) if d.get("is_hub") else d["seq"]
            lane = {"a": d["src_in"], "b": src_end(d), "range_text": rng,
                    "is_hub": d.get("is_hub", False), "warp": d["timewarp"],
                    "color": "#6f9fc8" if d.get("is_hub")
                             else ("#c080ff" if d["timewarp"] else "#ff7a45")}
            g = counts.get(i)
            if g and g["n"] > 1:
                open_ = g["key"] in self._cov_open
                label = "%s %s  ×%d" % ("▾" if open_ else "▸", g["seqs"][0], g["n"])
                lane["toggle_key"] = g["key"]
                if not open_:
                    rng += "   (%s%s)" % (", ".join(g["seqs"][1:5]),
                                          "…" if len(g["seqs"]) > 5 else "")
                    lane["range_text"] = rng
            if i not in counts and not d.get("is_hub"):
                label = "      " + label          # an opened group's children
            lane["label"] = label
            lanes.append(lane)
        self.cov_inspector.set_data((lo, hi), rep["consumed"], rep["dead"],
                                    rep["pieces"], lanes)
        # detail table: the top row lists EVERY timeline
        # using this shot; each "uses X" row is one covering piece and lists
        # only the timelines reading THOSE frames (2+ pieces = the natural
        # split); "unused" rows are the trim candidates between them
        all_seqs = sorted({self._inv[i]["seq"] for i in members
                           if not self._inv[i].get("is_hub")})
        rowdefs = [("all uses", lo, hi, ", ".join(all_seqs))]
        rowdefs += [("uses %s (+handles)" % pc["label"], pc["range"][0],
                     pc["range"][1], ", ".join(pc["seqs"])) for pc in prop]
        rowdefs += [("unused (trim)", a, b, "") for a, b in rep["dead"]]
        self.cov_detail.setRowCount(len(rowdefs))
        for r, (kind, a, b, used) in enumerate(rowdefs):
            # ranges are half-open: Out shows the LAST frame, like Flame
            cells = [kind, self._fmt(a, frames_to_tc(a, rate)),
                     self._fmt(b - 1, frames_to_tc(b - 1, rate)),
                     self._fmt(b - a, frames_to_tc(b - a, rate)), used]
            for c, val in enumerate(cells):
                it = QtWidgets.QTableWidgetItem(str(val))
                if c == 0 and kind.startswith("unused"):
                    it.setForeground(QtGui.QColor("#888888"))
                self.cov_detail.setItem(r, c, it)
        _autosize_once(self.cov_detail)

    def jump_to_segment(self, seg):
        """Right-click → land on this segment's shot in the Timelines tab
        (highlight everywhere + open the coverage drawer on it)."""
        k = _seg_key(seg)
        idx = next((i for i, d in enumerate(self._inv)
                    if d.get("seg_key") == k), None)
        if idx is None:
            self._scan()
            idx = next((i for i, d in enumerate(self._inv)
                        if d.get("seg_key") == k), None)
        if idx is None:
            self._say("Jump: that segment isn't in the current scope.")
            return
        gi = next((g for g, members in enumerate(self._groups)
                   if idx in members), None)
        if gi is None:
            return
        self.tabs.setCurrentWidget(self._tl_tab_w)
        self.tl_view.set_highlight(self._groups[gi])
        if not self.b_cov_drawer.isChecked():
            self.b_cov_drawer.setChecked(True)   # an explicit jump wants detail
        self._cov_gi = gi
        self._render_coverage_detail()
        d = self._inv[idx]
        self._pv_show("tl", self._groups[gi], None,
                      d.get("shot") or d.get("name") or d.get("camera") or "?")

    def _cov_group_toggled(self, key):
        """Twirl a folded use open (it lists the actual spots) or shut."""
        if key in self._cov_open:
            self._cov_open.discard(key)
        else:
            self._cov_open.add(key)
        self._render_coverage_detail()

    def _cov_cursor_moved(self, f):
        """The diagram was scrubbed: readout + the preview follow."""
        self._cov_readout(f)
        if self._pv_loaded.get("tl"):
            self.tl_pv.show_frame(f)

    def _tl_pv_scrubbed(self, f):
        """The preview was scrubbed: the diagram's cursor + readout follow."""
        self.cov_inspector.set_cursor(f)
        self._cov_readout(f)

    def _cov_readout(self, f):
        """Scrub readout: source frame + TC at the cursor and which timelines
        use that exact frame (dead-zone frames say so — those are the frames a
        split can safely cut through)."""
        members = self._cov_members
        if not members:
            return
        rate = self._inv[members[0]]["rate"]
        users = []
        for i in members:
            d = self._inv[i]
            if d.get("src_in") is None or d.get("src_out") is None:
                continue
            if d["src_in"] <= f <= d["src_out"]:      # out is inclusive
                users.append(("[HUB] %s" % d["seq"]) if d.get("is_hub") else d["seq"])
        who = ", ".join(users) if users else "nobody — dead space (safe split point)"
        self.cov_cursor_lbl.setText("Cursor  frame %d · %s   →   %s"
                                    % (f, frames_to_tc(f, rate), who))

    # ---------------------------------------------------------------- Timelines tab
    def _build_timelines_tab(self):
        w = QtWidgets.QWidget()
        v = QtWidgets.QVBoxLayout(w)
        row = QtWidgets.QHBoxLayout()
        self.tl_info = _squish(QtWidgets.QLabel(
            "Every sequence as a lane on ONE shared scale — click a shot to "
            "light it up in every lane; double-click to go there in Flame."))
        self.tl_info.setStyleSheet("color:#777777;")
        row.addWidget(self.tl_info, 1)
        self.tl_seg_names = QtWidgets.QCheckBox("Segment names")
        self.tl_seg_names.setToolTip(
            "Label the blocks with the segment name instead of the shot name "
            "(when they differ).")
        self.tl_seg_names.toggled.connect(
            lambda on: self.tl_view.set_label_mode(on))
        row.addWidget(self.tl_seg_names)
        self.tl_show_hub = QtWidgets.QCheckBox("Show hub")
        self.tl_show_hub.setToolTip(
            "Show the Conform Hub lane. The hub holds every shot end to end, "
            "so it is usually the longest lane and shrinks every other "
            "sequence — untick to hide it and let the remaining lanes stretch "
            "to fill the width. A shot you clicked stays highlighted.")
        # set before connecting: tl_view doesn't exist yet at this point
        self.tl_show_hub.setChecked(bool(load_settings().get("show_hub_lane", True)))
        self.tl_show_hub.toggled.connect(self._show_hub_toggled)
        row.addWidget(self.tl_show_hub)
        self.tl_show_all = QtWidgets.QCheckBox("Show all elements")
        self.tl_show_all.setToolTip(
            "Off (default): only shots/footage — references, graphics and FX "
            "are hidden so the lanes stay readable on a big job. On: show "
            "everything, which is how you spot a track CCM has mis-labelled. "
            "Right-click any track name to correct it on the spot.")
        self.tl_show_all.toggled.connect(self._show_all_toggled)
        row.addWidget(self.tl_show_all)
        self.b_cov_drawer = QtWidgets.QPushButton("Coverage")
        self.b_cov_drawer.setCheckable(True)
        self.b_cov_drawer.setChecked(True)
        self.b_cov_drawer.setToolTip(
            "Show/hide the coverage inspector below the lanes — hide it for "
            "more lane real estate.")
        row.addWidget(self.b_cov_drawer)
        row.addWidget(self._pv_button())
        row.addWidget(QtWidgets.QLabel("Zoom:"))
        self.tl_zoom = QtWidgets.QComboBox()
        self.tl_zoom.addItems(["Fit", "150%", "200%", "300%", "400%", "600%"])
        self.tl_zoom.setToolTip(
            "Stretch the shared scale; past Fit the map pans horizontally.")
        self.tl_zoom.currentTextChanged.connect(lambda _t: self._tl_apply_zoom())
        row.addWidget(self.tl_zoom)
        v.addLayout(row)

        self.tl_view = TimelinesView()
        self.tl_view.blockClicked.connect(self._tl_block_clicked)
        self.tl_view.blockDoubleClicked.connect(self._tl_block_dclicked)
        self.tl_view.laneToggled.connect(self._tl_lane_toggled)
        self.tl_view.trackMenuRequested.connect(self._tl_track_menu)
        self.tl_view.blockMenuRequested.connect(self._tl_block_menu)
        self._tl_collapsed = set()
        self.tl_scroll = QtWidgets.QScrollArea()
        self.tl_scroll.setWidgetResizable(True)
        self.tl_scroll.setWidget(self.tl_view)
        self.tl_scroll.setStyleSheet(
            "QScrollArea { border: 1px solid #2a2a2a; border-radius: 4px; }")

        # lanes over the coverage drawer (single-click fills it; the Coverage
        # button trades it for lane real estate)
        split = QtWidgets.QSplitter(QtCore.Qt.Vertical)
        split.setHandleWidth(10)
        split.addWidget(self.tl_scroll)
        self.tl_drawer = self._build_coverage_drawer()
        split.addWidget(self.tl_drawer)
        self.b_cov_drawer.toggled.connect(self._tl_drawer_vis)
        self._pv_vis["tl"] = self._tl_drawer_vis
        split.setStretchFactor(0, 3)
        split.setStretchFactor(1, 2)
        split.setSizes([420, 300])
        v.addWidget(split, 1)

        self.tl_show_all.setChecked(bool(load_settings().get("show_all_elements")))
        legend = QtWidgets.QLabel(
            "Orange = source · purple = timewarped · blue = hub (dim blue = "
            "Publish snapshot) · dim grey = reference. Edges: red = split "
            "source awaiting a verdict · amber = acknowledged variant · "
            "dashed YELLOW = not in the hub, its source has no hub piece (publish and "
            "relink would skip it). Hub lane first; the longest sequence "
            "spans the full width and every other lane falls proportionally "
            "short — untick Show hub to take the hub out of that scale.")
        legend.setWordWrap(True)
        legend.setStyleSheet("color:#777777;")
        v.addWidget(self._tip(legend))
        return w

    def _tl_drawer_vis(self, *_a):
        """Coverage and Preview each own part of the drawer under the lanes;
        it shows when either is on (the picture alone scrubs by its own
        bar)."""
        cov, pv = self.b_cov_drawer.isChecked(), self._pv_on
        self.tl_drawer.setVisible(cov or pv)
        for wdg in self._cov_only:
            wdg.setVisible(cov)
        self.tl_pv.setVisible(pv)

    def _show_all_toggled(self, on):
        s = load_settings()
        s["show_all_elements"] = bool(on)
        save_settings(s)
        self._render_timelines(keep_highlight=True)

    def _show_hub_toggled(self, on):
        s = load_settings()
        s["show_hub_lane"] = bool(on)
        save_settings(s)
        # keep the clicked shot lit: "where is this shot used" is the reason
        # to hide the hub
        self._render_timelines(keep_highlight=True)

    def _tl_lane_toggled(self, seq):
        """Fold a sequence to a single summary strip — on a 52-sequence job
        one long timeline shouldn't push every other lane off screen."""
        if seq in self._tl_collapsed:
            self._tl_collapsed.discard(seq)
        else:
            self._tl_collapsed.add(seq)
        self.tl_view.set_collapsed(self._tl_collapsed)

    def _tl_track_menu(self, seq, track_id, gpos):
        """Right-click a track name → set what it holds, without reopening
        the wizard. One mis-labelled track shouldn't cost a full pass."""
        cur = ((self._st.get("track_roles") or {}).get(seq) or {}).get(track_id, "")
        menu = QtWidgets.QMenu(self)
        # QAction lives in QtGui in Qt6 — QtWidgets.QAction raised here and
        # silently killed the whole menu
        menu.addAction("%s · %s" % (seq, track_label(track_id))).setEnabled(False)
        menu.addSeparator()
        acts = {}
        for role in ("",) + TRACK_ROLES:
            a = menu.addAction(("● " if role == cur else "    ")
                               + ROLE_LABELS[role])
            acts[a] = role
        chosen = menu.exec(gpos)
        if chosen is None or chosen not in acts:
            return
        role = acts[chosen]
        st = self._st
        roles = st.setdefault("track_roles", {})
        slot = roles.setdefault(seq, {})
        if role:
            slot[track_id] = role
        else:
            slot.pop(track_id, None)
        if not slot:
            roles.pop(seq, None)
        save_state(st)
        self._say("Track %s in '%s' → %s. Reclassifying."
                  % (track_label(track_id), seq, ROLE_LABELS[role]))
        self._reclassify()

    def _tl_block_menu(self, idx, gpos):
        """Right-click ONE segment → say what it is. Track-level answers
        cover the tidy case; this covers the untidy one — a slate cut in
        line with the programme on a shared track."""
        if idx >= len(self._inv):
            return
        d = self._inv[idx]
        key = d.get("seg_key")
        if not key:
            self._say("This segment has no stable identity — mark its track "
                      "instead.")
            return
        cur = (self._st.get("ref_overrides") or {}).get(key, "")
        menu = QtWidgets.QMenu(self)
        menu.addAction("%s · %s" % (d.get("shot") or d.get("name") or "?",
                                    d.get("seq", ""))).setEnabled(False)
        menu.addSeparator()
        acts = {}
        for role in ("",) + TRACK_ROLES:
            a = menu.addAction(("● " if role == cur else "    ")
                               + ("(follow the track)" if not role
                                  else ROLE_LABELS[role]))
            acts[a] = role
        chosen = menu.exec(gpos)
        if chosen is None or chosen not in acts:
            return
        role = acts[chosen]
        st = self._st
        ovs = st.setdefault("ref_overrides", {})
        if role:
            ovs[key] = role
        else:
            ovs.pop(key, None)
        save_state(st)
        self._say("Segment '%s' in %s → %s. Reclassifying."
                  % (d.get("shot") or d.get("name") or "?", d.get("seq"),
                     ROLE_LABELS[role] if role else "track default"))
        self._reclassify()

    def _render_timelines(self, keep_highlight=False):
        show_all = self.tl_show_all.isChecked()
        lay = timeline_lanes(self._inv, include_refs=show_all,
                             hidden_roles=() if show_all
                             else ("graphic", "fx", "slate"),
                             include_hub=self.tl_show_hub.isChecked())
        # the two tabs must agree (never red here and "OK" there) —
        # an ACKNOWLEDGED variant is amber, only an undecided split is red
        strays, variants = set(), set()
        for r in self._dup_rows:
            (variants if r.get("sanctioned") else strays).update(
                r.get("strays") or [])
        rate = next((d["rate"] for d in self._inv if d.get("rate")), 24.0)
        if not keep_highlight:
            self.tl_view.set_highlight(())    # old highlight indexes the old scan
        self.tl_view.set_data(self._inv, lay, strays, rate,
                              variants=variants,
                              orphans=getattr(self, "_orphans", set()))
        self._tl_apply_zoom()

    def _tl_apply_zoom(self):
        t = self.tl_zoom.currentText()
        if t == "Fit":
            self.tl_view.setMinimumWidth(0)
            return
        try:
            pct = int(t.rstrip("%"))
        except Exception:
            pct = 100
        vw = max(200, self.tl_scroll.viewport().width())
        self.tl_view.setMinimumWidth(int(vw * pct / 100.0))

    def _tl_block_clicked(self, idx):
        """One click lights the logical shot up in EVERY lane and
        fills the coverage drawer below."""
        if idx >= len(self._inv):
            return
        d = self._inv[idx]
        gi = next((g for g, members in enumerate(self._groups)
                   if idx in members), None)
        members = self._groups[gi] if gi is not None else [idx]
        self.tl_view.set_highlight(members)
        label = d.get("shot") or d.get("name") or d.get("camera") or "?"
        self.tl_info.setText(
            "%s — %d instance(s) across %d sequence(s)%s"
            % (label, len(members),
               len({self._inv[m]["seq"] for m in members}),
               "  ·  reference picture" if d.get("role") == "ref" else ""))
        # refs and hub-only shots keep gi=None — no consumer coverage to show
        self._cov_gi = gi
        self._render_coverage_detail()
        self._pv_show("tl", members, None, label)

    def _tl_block_dclicked(self, idx):
        """Double-click moves Flame's ACTUAL timeline there.
        seq.open() is confirmed on box; whether the positioner (current_time)
        is settable from script is not guaranteed — the log states exactly
        what happened either way, and the fallback still leaves the sequence
        open in the player."""
        if idx >= len(self._inv):
            return
        d = self._inv[idx]
        seg = d.get("seg")
        seq = _ancestor(seg, "PySequence") if seg is not None else None
        if seq is None:
            self._say("Timelines: no live handle on '%s' — rescan and retry."
                      % d.get("seq", "?"))
            return
        opened, err = False, None
        for meth in ("open", "open_as_sequence"):
            fn = getattr(seq, meth, None)
            if callable(fn):
                try:
                    fn()
                    opened = True
                    break
                except Exception as e:
                    err = e
        if not opened:
            self._say("Timelines: could not open '%s' (%s)." % (d["seq"], err))
            return
        shot = d.get("shot") or d.get("name") or "?"
        # positioner target: record_in reads 1 low on a gap-headed sequence
        # (on box, every jump landed 1 frame back; the EDL diff
        # proved the skew) — sum the durations before the segment instead
        pos = _true_record_pos(seg)
        if pos is None:
            pos = d.get("rec_in_f") or 0
        if pos != d.get("rec_in_f"):
            self._say("Timelines: leading gap detected — jump corrected %+d fr."
                      % (pos - (d.get("rec_in_f") or 0)))
        tc = frames_to_tc(pos, d["rate"])
        moved = False
        for mk in (lambda: flame.PyTime(tc, d["rate"]),
                   # bare-frame fallback: PyTime(relative_frame) is 1-based
                   lambda: flame.PyTime(int(pos) + 1)):
            try:
                t = mk()
            except Exception:
                continue
            if _set_flame_attr(seq, "current_time", t):
                moved = True
                break
        if moved:
            self._say("Timelines: opened '%s' at %s (%s)."
                      % (d["seq"], tc, shot))
        else:
            self._say("Timelines: opened '%s' — positioner not settable from "
                      "script (VERIFY); jump to %s manually ('%s')."
                      % (d["seq"], tc, shot))

    # ---------------------------------------------------------------- Connections tab
    def _build_connections_tab(self):
        """Connections tab layout: the shot list beside
        the CUT-FLOW BRAID on top; below, the picked
        shot's FAMILY TREE on the left (hub piece on top, longest spots
        first; orange = connected, blue = shared source) and its preview on
        the right. Double-click in the braid or the tree selects the
        connected segments in Flame. Read-only except that selection."""
        w = QtWidgets.QWidget()
        v = QtWidgets.QVBoxLayout(w)
        row = QtWidgets.QHBoxLayout()
        self.cx_info = _squish(QtWidgets.QLabel("Scan, then pick a shot."))
        self.cx_info.setStyleSheet("color:#777777;")
        row.addWidget(self.cx_info, 1)
        self.cx_fold = QtWidgets.QCheckBox("Fold identical cuts")
        self.cx_fold.setToolTip(
            "Merge sequences with exactly the same cut (e.g. the aspect "
            "versions of one spot) into a single lane — for big jobs. A folded "
            "block shows the worst connection state among them.")
        self.cx_fold.setChecked(bool(load_settings().get("cx_fold_cuts")))
        self.cx_fold.toggled.connect(self._cx_fold_toggled)
        row.addWidget(self.cx_fold)
        self.b_cx_web = QtWidgets.QPushButton("Tree")
        self.b_cx_web.setCheckable(True)
        self.b_cx_web.setChecked(True)
        self.b_cx_web.setToolTip("Show/hide the picked shot's family tree under "
                                 "the braid — hide it (and Preview) for more "
                                 "lanes.")
        row.addWidget(self.b_cx_web)
        row.addWidget(self._pv_button())
        self.b_cx_refresh = QtWidgets.QPushButton("Re-read Connections")
        self.b_cx_refresh.setToolTip(
            "Read the connections from Flame again — after you connect or "
            "disconnect segments, without a full Scan.")
        self.b_cx_refresh.clicked.connect(self._cx_refresh)
        row.addWidget(self.b_cx_refresh)
        v.addLayout(row)

        # narrow beside the braid: no "Uses" column — it's the
        # denominator of both counts
        self.cx_table = QtWidgets.QTableWidget(0, 3)
        self.cx_table.setHorizontalHeaderLabels(["Shot", "Conn.", "Shared"])
        for c, tip in enumerate((
                "The hub piece's shot name (its segment name when it has none).",
                "Uses in the spots CONNECTED to the hub piece, of all its uses.",
                "Uses sharing ONE source with the hub piece, of all its uses.")):
            hi = self.cx_table.horizontalHeaderItem(c)
            if hi is not None:
                hi.setToolTip(tip)
        self.cx_table.verticalHeader().setVisible(False)
        self.cx_table.setSelectionBehavior(QtWidgets.QAbstractItemView.SelectRows)
        self.cx_table.setSelectionMode(QtWidgets.QAbstractItemView.SingleSelection)
        self.cx_table.setEditTriggers(QtWidgets.QAbstractItemView.NoEditTriggers)
        self.cx_table.setSortingEnabled(True)
        _init_table_resize(self.cx_table)
        # beside the braid the list is narrow: the name takes the slack
        hh = self.cx_table.horizontalHeader()
        hh.setStretchLastSection(False)
        hh.setSectionResizeMode(0, QtWidgets.QHeaderView.Stretch)
        for c in (1, 2):
            hh.setSectionResizeMode(c, QtWidgets.QHeaderView.ResizeToContents)
        self.cx_table.itemSelectionChanged.connect(self._cx_selection_changed)

        self.cx_braid = CutFlowBraid()
        self.cx_braid.blockClicked.connect(self._cx_braid_clicked)
        self.cx_braid.blockDoubleClicked.connect(self._cx_select_connected)
        braid_scroll = QtWidgets.QScrollArea()
        braid_scroll.setWidgetResizable(True)
        braid_scroll.setHorizontalScrollBarPolicy(QtCore.Qt.ScrollBarAlwaysOff)
        braid_scroll.setWidget(self.cx_braid)
        braid_scroll.setStyleSheet(
            "QScrollArea { border: 1px solid #2a2a2a; border-radius: 4px; }")
        # top: the list gives up braid width, not height
        top = QtWidgets.QSplitter(QtCore.Qt.Horizontal)
        top.addWidget(self.cx_table)
        top.addWidget(braid_scroll)
        top.setStretchFactor(0, 1)
        top.setStretchFactor(1, 4)
        top.setSizes([300, 850])

        self.cx_tree = ConnectionsTree()
        self.cx_tree.tip_fn = self._cx_tip
        self.cx_tree.nodeClicked.connect(self._cx_node_clicked)
        self.cx_tree.nodeDoubleClicked.connect(self._cx_select_connected)
        self.cx_tree_scroll = QtWidgets.QScrollArea()
        self.cx_tree_scroll.setWidgetResizable(True)
        self.cx_tree_scroll.setHorizontalScrollBarPolicy(QtCore.Qt.ScrollBarAlwaysOff)
        self.cx_tree_scroll.setWidget(self.cx_tree)
        self.cx_tree_scroll.setStyleSheet(
            "QScrollArea { border: 1px solid #2a2a2a; border-radius: 4px; }")
        self.cx_pv = self._pv_panel("cx")
        # bottom: tree left, picture right
        self.cx_bottom = QtWidgets.QSplitter(QtCore.Qt.Horizontal)
        self.cx_bottom.addWidget(self.cx_tree_scroll)
        self.cx_bottom.addWidget(self.cx_pv)
        self.cx_bottom.setStretchFactor(0, 3)
        self.cx_bottom.setStretchFactor(1, 2)
        self.cx_bottom.setSizes([640, 420])

        vsplit = QtWidgets.QSplitter(QtCore.Qt.Vertical)
        vsplit.setHandleWidth(10)
        vsplit.addWidget(top)
        vsplit.addWidget(self.cx_bottom)
        vsplit.setStretchFactor(0, 3)
        vsplit.setStretchFactor(1, 2)
        vsplit.setSizes([440, 340])
        self.b_cx_web.toggled.connect(self._cx_bottom_vis)
        self._pv_vis["cx"] = self._cx_bottom_vis
        v.addWidget(vsplit, 1)

        legend = QtWidgets.QLabel(
            "BRAID (top right): the Conform Hub, then every sequence as a lane; "
            "each shot is a block in cut order and a ribbon follows it from "
            "lane to lane — so aspect versions of one cut fall straight down, "
            "re-edits cross, and shots a cutdown drops simply end. Colour = "
            "connection to the shot's hub piece: ORANGE connected · BLUE shares "
            "its source but NOT connected · dashed YELLOW not in the hub · grey "
            "not read yet (purple top edge = timewarped). Lanes fit the width — "
            "order, not timing (Timelines keeps the shared scale). Hover a block "
            "to thread a shot through every lane; click it (or its row in the "
            "list) to open it below; double-click to select its connected "
            "segments in Flame. TREE (bottom left): the hub piece on top, then "
            "every segment of the shot in generations by sequence length, "
            "longest first; the line into each box is its link to the hub "
            "piece, in the same colours. Click a box to ring what it's linked "
            "to and preview that segment.")
        legend.setWordWrap(True)
        legend.setStyleSheet("color:#777777;")
        v.addWidget(self._tip(legend))
        return w

    def _cx_bottom_vis(self, *_a):
        tree, pv = self.b_cx_web.isChecked(), self._pv_on
        self.cx_tree_scroll.setVisible(tree)
        self.cx_pv.setVisible(pv)
        self.cx_bottom.setVisible(tree or pv)

    def _cx_rows(self):
        """Hub audit rows in shot order (hub pieces first, then shots that
        aren't in the hub)."""
        rows = list((getattr(self, "_hub_rep", None) or {}).get("rows") or [])
        return sorted(rows, key=lambda r: (not r["in_hub"],
                                           (r["shot_name"] or r["seg_name"] or "").lower()))

    def _connected_map(self):
        """Row index → rows CONNECTED to it (Flame's connected_segments,
        'all reels' — on box it reaches every sequence and includes
        the segment itself). One call per connected SET, not per segment:
        every member of a set is marked read by the first answer. Cached
        per scan; the Connections tab asks for it when it's opened."""
        if getattr(self, "_conn_all", None) is not None:
            return self._conn_all
        by_key, by_base = key_index([d.get("match_key") for d in self._inv])
        groups, done = [], set()
        todo = [i for i, d in enumerate(self._inv)
                if d.get("is_hub") or d.get("role") == "source"]
        for n, i in enumerate(todo):
            if i in done:
                continue
            if n % 25 == 0:
                self.cx_info.setText("Reading connections from Flame… %d of %d"
                                     % (n, len(todo)))
                QtWidgets.QApplication.processEvents()
            try:
                got = self._inv[i]["seg"].connected_segments(scoping="all reels") or []
            except Exception as e:
                self._inv[i]["conn_err"] = str(e)
                continue
            members = {i}
            for x in got:
                members.update(match_rows(_match_key(x), by_key, by_base))
            done.update(members)
            groups.append(members)
        self._conn_all = link_map(groups)
        return self._conn_all

    def _cx_fold_toggled(self, on):
        s = load_settings()
        s["cx_fold_cuts"] = bool(on)
        save_settings(s)
        self._cx_render_braid()

    def _cx_braid_clicked(self, group):
        """A braid block → the same shot in the list (which opens its web)."""
        for r in range(self.cx_table.rowCount()):
            it = self.cx_table.item(r, 0)
            if it is not None and it.data(QtCore.Qt.UserRole) == group:
                self.cx_table.selectRow(r)
                self.cx_table.scrollToItem(it)
                self._cx_selection_changed()
                return

    def _cx_render_braid(self):
        rows = self._cx_rows()
        names = {d["group"]: (d["shot_name"] or d["seg_name"] or "?") for d in rows}
        lay = braid_layout(
            self._inv, self._groups,
            (getattr(self, "_hub_rep", None) or {}).get("primary"),
            getattr(self, "_conn_all", None),
            self._shared_map() if self._inv else {},
            getattr(self, "_orphans", set()),
            fold=self.cx_fold.isChecked()) if self._inv else None
        self.cx_braid.set_data(self._inv, lay, names)
        self.cx_braid.set_focus(self._cx_current_group())

    def _cx_refresh(self):
        self._conn_all = None
        self._render_connections(read=True)

    def _tabs_changed(self, _index):
        if self.tabs.currentWidget() is getattr(self, "_cx_tab_w", None) \
                and self._inv and getattr(self, "_conn_all", None) is None:
            self._render_connections(read=True)
        self._pv_flush()        # a tab's preview loads when it comes on screen

    def _render_connections(self, read=False):
        if read or (self.tabs.currentWidget() is self._cx_tab_w and self._inv):
            conn = self._connected_map() if self._inv else {}
        else:
            conn = getattr(self, "_conn_all", None)
        shared = self._shared_map() if self._inv else {}
        rows = self._cx_rows()
        # group numbers change per scan — re-open the same shot BY NAME
        prev_sel = self.cx_table.selectionModel().selectedRows() \
            if self.cx_table.rowCount() else []
        prev = (self.cx_table.item(prev_sel[0].row(), 0).text()
                if prev_sel and self.cx_table.item(prev_sel[0].row(), 0) else None)
        self.cx_table.blockSignals(True)
        self.cx_table.setSortingEnabled(False)
        self.cx_table.setRowCount(len(rows))
        grey = QtGui.QColor("#888888")
        for r, d in enumerate(rows):
            hub = set(d["hub_idx"])
            members = self._groups[d["group"]] if d["group"] < len(self._groups) else []
            uses = [i for i in members if not self._inv[i].get("is_hub")
                    and self._inv[i].get("role") == "source"]

            def count(pm):
                if pm is None or not hub:
                    return "—"
                return "%d/%d" % (sum(1 for i in uses if (pm.get(i) or set()) & hub),
                                  len(uses))
            vals = [d["shot_name"] or d["seg_name"] or "?",
                    count(conn), count(shared)]
            for c, val in enumerate(vals):
                it = QtWidgets.QTableWidgetItem()
                if isinstance(val, int):
                    it.setData(QtCore.Qt.DisplayRole, val)
                else:
                    it.setText(str(val))
                if c == 0:
                    it.setData(QtCore.Qt.UserRole, d["group"])
                    if not d["in_hub"]:
                        it.setForeground(QtGui.QColor(ORPHAN_EDGE))
                        it.setToolTip("Not in the Conform Hub.")
                elif "/" in str(val):
                    a, b = val.split("/")
                    if a != b:
                        it.setForeground(QtGui.QColor("#e0b000"))
                else:
                    it.setForeground(grey)
                self.cx_table.setItem(r, c, it)
        self.cx_table.setSortingEnabled(True)
        self.cx_table.blockSignals(False)
        _autosize_once(self.cx_table)
        self._cx_render_braid()
        n_hub = sum(1 for d in rows if d["in_hub"])
        self._cx_summary = (
            "%d shot(s), %d in the hub%s — hover the braid, click a shot for its web." % (
                len(rows), n_hub, "" if conn is not None
                else " · connections are read when this tab opens"))
        self.cx_info.setText(self._cx_summary)
        # keep the same shot open across a re-read
        for r in range(self.cx_table.rowCount()):
            it = self.cx_table.item(r, 0)
            if prev is not None and it is not None and it.text() == prev:
                self.cx_table.selectRow(r)
                self._cx_selection_changed()
                return
        self.cx_tree.clear()
        self._pv_show("cx", [])

    def _cx_current_group(self):
        sel = self.cx_table.selectionModel().selectedRows() if self.cx_table.rowCount() else []
        if not sel:
            return None
        it = self.cx_table.item(sel[0].row(), 0)
        return it.data(QtCore.Qt.UserRole) if it is not None else None

    def _cx_selection_changed(self):
        gi = self._cx_current_group()
        self.cx_braid.set_focus(gi)
        if gi is None or gi >= len(self._groups):
            self.cx_tree.clear()
            self.cx_info.setText(getattr(self, "_cx_summary", ""))
            self._pv_show("cx", [])
            return
        rep = next((r for r in self._cx_rows() if r["group"] == gi), None)
        read = getattr(self, "_conn_all", None)
        conn = read or {}
        shared = self._shared_map() if self._inv else {}
        orphans = getattr(self, "_orphans", set())
        centres, ring = web_nodes(self._inv, self._groups[gi], (conn, shared),
                                  (getattr(self, "_hub_rep", None) or {}).get("primary"))
        states = {i: ("hub" if self._inv[i].get("is_hub")
                      else link_state(i, centres, read, orphans)) for i in ring}
        self.cx_tree.set_data(self._inv, centres, tree_tiers(self._inv, ring),
                              states, conn, shared, orphans,
                              "" if centres else "not in the hub")
        name = (rep or {}).get("shot_name") or (rep or {}).get("seg_name") or "?"
        n_conn = sum(1 for s in states.values() if s == "c")
        n_use = sum(1 for s in states.values() if s != "hub")
        self.cx_info.setText(
            "%s — %d of %d use(s) connected to the hub piece%s · click a box for "
            "its links · double-click selects its connected segments in Flame."
            % (name, n_conn, n_use,
               "" if read is not None else " (connections not read yet)"))
        self._cx_shot_name = name
        self._pv_show("cx", self._groups[gi], None, name)

    def _cx_tip(self, i):
        d = self._inv[i]
        conn = (getattr(self, "_conn_all", None) or {}).get(i) or set()
        shared = (self._shared_map() or {}).get(i) or set()
        bits = ["%s  ·  %s" % (d.get("seq", "?"),
                               track_label(d.get("track_id"), d.get("track_name"))),
                "shot: %s · segment: %s" % (d.get("shot") or "—", d.get("name") or "—"),
                "connected to %d other segment(s) · shares its source with %d"
                % (len(conn), len(shared))]
        if d.get("timewarp"):
            pct = (d.get("tw_model") or {}).get("pct")
            bits.append("timewarped" + (" %.4g%%" % pct if pct else ""))
        if i in getattr(self, "_orphans", set()):
            bits.append("its source is NOT in the hub")
        if d.get("conn_err"):
            bits.append("connections couldn't be read: %s" % d["conn_err"])
        bits.append("click: its links + preview · double-click: open the sequence "
                    "with its connected segments selected")
        return "\n".join(bits)

    def _cx_node_clicked(self, i):
        """A tree box: say what it's linked to and preview THAT segment — a
        split source shows its own picture, not the hub's."""
        d = self._inv[i]
        self.cx_info.setText("%s · %s · connected to %d · shares its source with %d"
                             % (d.get("seq", "?"), d.get("shot") or d.get("name") or "?",
                                len((getattr(self, "_conn_all", None) or {}).get(i) or ()),
                                len((self._shared_map() or {}).get(i) or ())))
        gi = self._cx_current_group()
        members = self._groups[gi] if gi is not None and gi < len(self._groups) else [i]
        self._pv_show("cx", members, i, "%s · %s" % (
            getattr(self, "_cx_shot_name", "") or d.get("shot") or "?", d.get("seq", "?")))

    def _cx_select_connected(self, i):
        """Double-click: open that segment's sequence and SELECT
        its connected segments in it — seg.selected is settable (confirmed
        on box). Every other segment of the sequence is deselected so the
        selection is exactly the connected set. Connected segments in OTHER
        sequences are counted, not touched."""
        if i >= len(self._inv):
            return
        d = self._inv[i]
        seg = d.get("seg")
        seq = _ancestor(seg, "PySequence") if seg is not None else None
        if seq is None:
            self._say("Connections: no live handle on '%s' — Scan and retry."
                      % d.get("seq", "?"))
            return
        self._tl_block_dclicked(i)          # open the sequence + go to the segment
        try:
            linked = seg.connected_segments(scoping="all reels") or []
        except Exception as e:
            self._say("Connections: couldn't read %s's connections (%s)."
                      % (d.get("seq"), e))
            linked = []
        keys = {_match_key(x) for x in linked}
        keys.add(d.get("match_key") or _match_key(seg))
        n_on = n_fail = 0
        for x in iter_segments(seq):
            if _is_gap(x):
                continue
            try:
                want = _match_key(x) in keys
            except Exception:
                continue
            if _set_flame_attr(x, "selected", want):
                n_on += 1 if want else 0
            else:
                n_fail += 1
        elsewhere = max(0, len(keys) - n_on)
        self._say("Connections: selected %d connected segment(s) in '%s'%s%s."
                  % (n_on, d.get("seq"),
                     (" · %d more in other sequences" % elsewhere) if elsewhere else "",
                     (" · %d segment(s) wouldn't take a selection" % n_fail)
                     if n_fail else ""))

    # ---------------------------------------------------------------- Hub tab
    def _wip_banner(self):
        """Always shown — not a Tip: the in-development status must not be missed."""
        lb = QtWidgets.QLabel(WIP_NOTE)
        lb.setObjectName("wip")
        lb.setWordWrap(True)
        return lb

    def _build_hub_tab(self):
        w = QtWidgets.QWidget()
        v = QtWidgets.QVBoxLayout(w)
        v.addWidget(self._wip_banner())
        top = QtWidgets.QHBoxLayout()
        self.hub_label = _squish(QtWidgets.QLabel("Press Scan."))
        top.addWidget(self.hub_label, 1)
        top.addWidget(self._pv_button())
        v.addLayout(top)

        # No `_###0 OK` naming-convention column:
        # a convention check isn't a status, and conventions are settings,
        # never hard-codes. Status answers "does it have a shot name".
        hub_cols = (
            ("Shot Name", "The hub piece's SHOT NAME — the field publish "
                          "tokens use. Blank = no shot name yet."),
            ("Status", "✓ in the live hub with a shot name\n"
                       "✗ in the hub but no shot name yet\n"
                       "✗ MISSING — a spot uses it, the hub doesn't have it\n"
                       "✗ pub only — only on a publish copy of the hub"),
            ("Segment Name", "The hub segment's own name (for a MISSING "
                             "shot, a spot segment's) — tells rows apart "
                             "when there is no shot name."),
            ("Alts", "How many live pieces this shot has in the hub, shown "
                     "when it's more than one: several real sources (grades, "
                     "re-imports) or alt shots stacked under one shot name."),
            ("Consumers", "How many spot segments use this shot, timewarped "
                          "ones included."),
            ("Coverage", "Does the hub hold every frame the spots show — "
                         "timewarps included (their curve is read frame by "
                         "frame, down to the last displayed timecode) — and "
                         "no more? Purple = longer than every use, or a "
                         "timewarp Flame wouldn't read back, or a freeze "
                         "holding past the end of its media (not missing). "
                         "Hover any cell for every use and which one sets "
                         "the tail."),
        )
        self.hub_table = QtWidgets.QTableWidget(0, len(hub_cols))
        self.hub_table.setHorizontalHeaderLabels([c for c, _t in hub_cols])
        for c, (_label, tip) in enumerate(hub_cols):
            hdr_item = self.hub_table.horizontalHeaderItem(c)
            if hdr_item is not None:
                hdr_item.setToolTip(tip)
        self.hub_table.verticalHeader().setVisible(False)
        self.hub_table.setSelectionBehavior(QtWidgets.QAbstractItemView.SelectRows)
        self.hub_table.setEditTriggers(QtWidgets.QAbstractItemView.NoEditTriggers)
        self.hub_table.setSortingEnabled(True)
        _init_table_resize(self.hub_table)
        self.hub_table.itemSelectionChanged.connect(self._hub_pv_selected)
        hub_split = QtWidgets.QSplitter(QtCore.Qt.Horizontal)
        hub_split.addWidget(self.hub_table)
        self.hub_pv = self._pv_panel("hub")
        hub_split.addWidget(self.hub_pv)
        hub_split.setStretchFactor(0, 3)
        hub_split.setStretchFactor(1, 2)
        hub_split.setSizes([620, 380])
        self._pv_vis["hub"] = self.hub_pv.setVisible
        v.addWidget(hub_split, 1)

        actions = QtWidgets.QGroupBox("HUB ACTIONS")
        a = QtWidgets.QHBoxLayout(actions)
        self.b_hub_create = QtWidgets.QPushButton("Create Conform Hub…")
        self.b_hub_create.setToolTip(
            "Build a new Conform Hub from every shot the spots use: one piece "
            "per real source, sized to the union of every use (timewarps "
            "included), in source-TC or record order. A shot with several real "
            "sources stacks them above the base piece under one shot name. "
            "Known: the new sequence starts with a 1-frame gap — delete it by "
            "hand.")
        self.b_hub_create.clicked.connect(self._hub_create_sequence)
        a.addWidget(self.b_hub_create)
        self.b_hub_connect = QtWidgets.QPushButton("Create Source Segment Connections…")
        self.b_hub_connect.setToolTip(
            "Source Segment Connections are not scriptable in Flame's python "
            "API: this walks you through Flame's native step, then "
            "verifies which spot segments actually connected.")
        self.b_hub_connect.clicked.connect(self._hub_connect)
        a.addWidget(self.b_hub_connect)
        self.b_hub_rename = QtWidgets.QPushButton("Renumber / Rename Shots…")
        self.b_hub_rename.setToolTip(
            "Rename every live hub shot to <hub name>_0010/0020/… in record "
            "order — the naming pass done right before connections + publish.")
        self.b_hub_rename.clicked.connect(self._hub_rename)
        a.addWidget(self.b_hub_rename)
        self.b_hub_remove = QtWidgets.QPushButton("Remove from Hub…")
        self.b_hub_remove.setToolTip(
            "Delete the selected shots' live hub segments (e.g. a ref that "
            "slipped in) and close the gap. EXPERIMENTAL: the exact removal "
            "API is being probed — results are verified and reported.")
        self.b_hub_remove.clicked.connect(self._hub_remove)
        a.addWidget(self.b_hub_remove)
        a.addStretch(1)
        v.addWidget(actions)

        legend = QtWidgets.QLabel(
            "Conform Hub health — the pre-publish eyeball pass, computed from "
            "the shared scan: is each consumed shot IN the hub and does it "
            "have a shot name (Status: ✓ named · ✗ no shot name · ✗ MISSING, "
            "a spot uses it but the hub doesn't have it · ✗ pub only, only on "
            "a publish copy), does the hub hold every frame the spots cut with "
            "(Coverage — hover a cell for what sets each piece's length), and how far hub "
            "record order is from source-TC (C-mode) order. Alts = the number "
            "of live pieces a shot has in the hub when it's more than one "
            "(stacked sources or alt shots under one shot name). Hover a "
            "column header for details. The HUB ACTIONS above act on these "
            "rows.")
        legend.setWordWrap(True)
        legend.setStyleSheet("color:#777777;")
        v.addWidget(self._tip(legend))
        return w

    def _render_hub(self):
        rep = hub_audit(self._inv, self._groups,
                        (self._s or {}).get("sources_seq_name", ""),
                        media_ends_of(self._st))
        self._hub_rep = rep
        n_named = sum(1 for r in rep["rows"] if r["in_hub"] and r["named"])
        n_hubbed = sum(1 for r in rep["rows"] if r["in_hub"])
        n_short = sum(1 for r in rep["rows"] if r["in_hub"] and r["uncovered"])
        self.hub_label.setText(
            "%d logical shot(s) vs primary hub '%s' — %d in hub · %d MISSING · "
            "%d unused · %d with missing frames · %d order inversion(s) vs "
            "source-TC · %d/%d named"
            % (len(rep["rows"]), rep.get("primary") or "?", n_hubbed,
               len(rep["missing"]), len(rep["unused"]), n_short,
               rep["inversions"], n_named, n_hubbed))
        orange = QtGui.QColor("#ff7a45")
        amber = QtGui.QColor("#e0b000")
        red = QtGui.QColor("#ff5555")
        grey = QtGui.QColor("#888888")
        rows = rep["rows"]
        self.hub_table.blockSignals(True)
        self.hub_table.clearSelection()     # row indices are stale after a rebuild
        self.hub_table.setSortingEnabled(False)
        self.hub_table.setRowCount(len(rows))
        for r, d in enumerate(rows):
            missing_f = sum(b - a for a, b in d["uncovered"])
            try:
                cov_tip = self._hub_cov_tip(d)
            except Exception as e:
                cov_tip = "(breakdown unavailable: %s)" % e
            if not d["in_hub"]:
                cov = "NOT IN HUB"
            elif missing_f:
                where = [w for w in ("head", "gap", "tail")
                         if w in d["uncovered_at"]]
                cov = "missing %d frame(s) at %s" % (missing_f, " + ".join(where))
                if d["held_f"]:
                    cov += " · %d fr held past the media end" % d["held_f"]
                if d["n_unchecked"]:
                    cov += " · %d TW curve(s) unreadable" % d["n_unchecked"]
            elif d["held_f"]:
                cov = "OK · %d fr held past the media end (freeze)" % d["held_f"]
            elif d["excess_head"] or d["excess_tail"]:
                cov = "OK · %d fr longer than the uses" % (
                    d["excess_head"] + d["excess_tail"])
            elif d["n_unchecked"]:
                cov = "OK · %d TW curve(s) unreadable, not checked" % d["n_unchecked"]
            else:
                cov = "OK"
            if d["in_hub"] and d["named"]:
                status, status_tip = "✓", ""
            elif d["in_hub"] and d["shot_name"]:
                status = "✗"
                status_tip = ("%d of %d stacked pieces have no shot name."
                              % (d["n_unnamed"], len(d["hub_idx"])))
            elif d["in_hub"]:
                status = "✗"
                status_tip = ("No shot name yet — Renumber / Rename Shots… "
                              "gives every hub shot one.")
            elif d["n_hub_other"]:
                status = "✗ pub only"     # only in a derived hub (…_publish)
                status_tip = "Only on a publish copy of the hub, not the live one."
            else:
                status = "✗ MISSING"
                status_tip = ("A spot uses this shot but the hub doesn't have "
                              "it — rebuild the hub or add it.")
            vals = [d["shot_name"], status, d["seg_name"],
                    d["n_hub"] if d["n_hub"] > 1 else "",
                    d["n_consumers"] if d["n_consumers"] else "unused",
                    cov]
            for c, val in enumerate(vals):
                it = QtWidgets.QTableWidgetItem()
                if isinstance(val, int):
                    it.setData(QtCore.Qt.DisplayRole, val)
                else:
                    it.setText(str(val))
                if c == 0:
                    it.setData(QtCore.Qt.UserRole, r)
                if c == 1:
                    if not d["in_hub"]:
                        it.setForeground(amber if d["n_hub_other"] else orange)
                    elif not d["named"]:
                        it.setForeground(amber)
                    if status_tip:
                        it.setToolTip(status_tip)
                elif c == 3 and d["n_hub"] > 1:
                    it.setForeground(orange)      # alt stack (intentional)
                elif c == 4 and not d["n_consumers"]:
                    it.setForeground(grey)
                elif c == 5:
                    if not d["in_hub"] or missing_f:
                        it.setForeground(red if missing_f else orange)
                    elif (d["excess_head"] or d["excess_tail"] or d["n_unchecked"]
                          or d["held_f"]):
                        it.setForeground(QtGui.QColor("#c080ff"))
                    if cov_tip:
                        it.setToolTip(cov_tip)
                self.hub_table.setItem(r, c, it)
        self.hub_table.setSortingEnabled(True)
        self.hub_table.blockSignals(False)
        _autosize_once(self.hub_table)

    def _hub_pv_selected(self):
        """A hub row → its picture: the hub piece, or the widest use when the
        shot is MISSING from the hub."""
        sel = self.hub_table.selectionModel().selectedRows()
        it = self.hub_table.item(sel[0].row(), 0) if sel else None
        r = it.data(QtCore.Qt.UserRole) if it is not None else None
        rows = (getattr(self, "_hub_rep", None) or {}).get("rows") or []
        if not isinstance(r, int) or r >= len(rows):
            self._pv_show("hub", [])
            return
        d = rows[r]
        g = d.get("group")
        members = (self._groups[g] if isinstance(g, int) and g < len(self._groups)
                   else list(d.get("hub_idx") or []))
        self._pv_show("hub", members, self._pv_pick(d.get("hub_idx") or []),
                      d["shot_name"] or d["seg_name"] or "?")

    def _hub_cov_tip(self, d):
        """Where a hub piece's length comes from — the tooltip on EVERY
        Coverage cell. The piece(s) as the scan READ them (source span and
        record duration: for an unretimed hub piece those must agree, so a
        mismatch exposes a readback skew), any missing ranges, the union CCM
        builds to, then every use of the shot with what it asks the piece to
        hold: a straight cut's first/last frame; a timewarp's raw curve (speed,
        first and last sample) beside CCM's computed last frame and Flame's
        static last frame. ◀ marks the use that sets the tail. Frames read
        FIRST → LAST inclusive, the way Flame shows them. The exact
        timewarp rule is calibrated from this readout."""
        inv = self._inv
        rate = next((inv[i].get("rate") for i in (d["hub_idx"] or [])
                     if inv[i].get("rate")), None) or 24.0

        def tc(f, r=None):
            return frames_to_tc(f, r or rate)

        def span(a, b, r=None):
            return "%s → %s (%d fr)" % (tc(a, r), tc(b - 1, r), b - a)

        lines = []
        if d["hub_idx"]:
            lines.append("Hub piece(s), as read:")
            for i in d["hub_idx"]:
                h = inv[i]
                if h.get("src_in") is None or h.get("src_out") is None:
                    continue
                rec = h.get("rec_dur_f")
                lines.append("  %s · record dur %s fr%s" % (
                    span(h["src_in"], src_end(h)),
                    rec if rec is not None else "?",
                    "   ⚠ span ≠ record dur"
                    if rec is not None and rec != src_end(h) - h["src_in"] else ""))
        else:
            lines.append("Not in the hub.")
        if d["uncovered"]:
            lines.append("Missing source frames:")
            for (a, b), at in zip(d["uncovered"], d["uncovered_at"]):
                lines.append("  %s at %s" % (span(a, b), at))
        members = self._groups[d["group"]] if d.get("group") is not None \
            and d["group"] < len(self._groups) else []
        uses = [i for i in members if not inv[i].get("is_hub")
                and inv[i].get("role") == "source"]
        w_lo, w_hi = hub_piece_range(inv, uses)
        if w_lo is not None and w_hi is not None and w_hi > w_lo:
            lines.append("CCM builds to the union of every use: %s" % span(w_lo, w_hi))
        lines.append("Uses — the frames each one displays (◀ sets the tail):")
        short = set(d["short_idx"])
        cap = 14
        for i in sorted(uses, key=lambda k: (inv[k].get("seq") or "",
                                             inv[k].get("rec_in_f") or 0))[:cap]:
            u = inv[i]
            r = u.get("rate") or rate
            f = use_frames(u)
            mark = "  ◀" if f and f[1] == w_hi else ""
            if i in short:
                mark += "  ✗ short"
            m_end = media_ends_of(self._st).get(
                u.get("file_path") or "")
            if f and m_end is not None and f[1] > m_end:
                mark += "  · %d fr past the media end (held)" % (f[1] - max(f[0], m_end))
            m = u.get("tw_model") or {}
            if f is None:
                body = "no readable source range"
            elif f[2] == "cut":
                body = "cut · %s" % span(f[0], f[1], r)
            elif f[2] == "curve":
                t_n = m.get("tN", m["hi"])
                body = "TW %s%% · curve %.3f → %.3f%s · shows %s" % (
                    ("%.4g" % m["pct"]) if m.get("pct") is not None else "?",
                    m["t1"], t_n,
                    (" (max %.3f)" % m["hi"]) if m["hi"] > t_n + 1e-6 else "",
                    span(f[0], f[1], r))
            else:
                body = "TW, curve unreadable · assumed %s — not checked" % span(
                    f[0], f[1], r)
            lines.append("  %s @ %s · %s%s" % (u.get("seq", "?"),
                                                u.get("rec_in_tc") or "?", body, mark))
        if len(uses) > cap:
            lines.append("  … and %d more" % (len(uses) - cap))
        if d.get("held_f"):
            lines.append("Held past the media end: a use runs %d fr beyond the "
                         "last frame of its media, so Flame holds that frame (a "
                         "freeze). No hub piece can hold frames that don't exist "
                         "— not counted as missing. The media end was learned by "
                         "the last Create Conform Hub." % d["held_f"])
        if d.get("excess_head") or d.get("excess_tail"):
            lines.append("The piece is longer than every use: %s — rebuild "
                         "to trim it." % " and ".join("%d fr %s" % (k, w) for k, w in (
                             (d["excess_head"], "before the earliest"),
                             (d["excess_tail"], "after the latest")) if k))
        if d.get("n_unchecked"):
            lines.append("%d use(s) couldn't be checked: a timewarp whose curve "
                         "Flame wouldn't read back." % d["n_unchecked"])
        return "\n".join(lines)

    def _hub_create_sequence(self):
        """Create + name the Conform Hub via reel.create_sequence, then
        populate it: one piece per real source, in source-TC (C-mode) or
        basis record (A-mode) order, widened to cover every use, then read
        back from Flame for a coverage check."""
        if not self._mutations_ok("Create Conform Hub"):
            return
        dlg = QtWidgets.QDialog(self)
        dlg.setWindowTitle("Create Conform Hub")
        dlg.setStyleSheet(STYLE)
        f = QtWidgets.QFormLayout(dlg)
        # default Conform_Hub unless Settings holds a real
        # custom hub name (a job code); never reuse a name already in scope —
        # CCM tells sequences apart by name, so two hubs called Conform_Hub
        # would read as one hub with every shot doubled
        saved = ((self._s or {}).get("sources_seq_name") or "").strip()
        base = saved if saved and saved not in LEGACY_HUB_DEFAULTS else "Conform_Hub"
        taken = {d["seq"] for d in self._inv}
        default = base
        n = 2
        while default in taken:
            default = "%s_%02d" % (base, n)
            n += 1
        e_name = QtWidgets.QLineEdit(default)
        f.addRow("Name:", e_name)
        c_order = QtWidgets.QComboBox()
        c_order.addItems(["Source TC (C-mode)", "Record TC (A-mode)"])
        f.addRow("Order:", c_order)
        c_basis = QtWidgets.QComboBox()
        c_basis.addItems(self._consumer_seq_names())
        c_basis.setEnabled(False)
        c_basis.setToolTip("A-mode: shots ordered by their record position in "
                           "THIS timeline; shots absent from it append in "
                           "source-TC order.")
        c_order.currentTextChanged.connect(
            lambda t: c_basis.setEnabled("A-mode" in t))
        f.addRow("A-mode basis:", c_basis)
        n_shots = n_pieces = 0
        for members in self._groups:
            cons = [i for i in members
                    if not self._inv[i].get("is_hub")
                    and self._inv[i].get("role") == "source"
                    and self._inv[i].get("src_in") is not None]
            if not cons:
                continue
            n_shots += 1
            n_pieces += len(self._source_components(cons))
        f.addRow("", QtWidgets.QLabel(
            "Populates with %d shot(s) / %d piece(s), video only (no audio).%s"
            % (n_shots, n_pieces,
               "\nShots with several real sources stack on tracks above,"
               "\naligned by source timecode under one shot name."
               if n_pieces > n_shots else "")))
        bb = QtWidgets.QDialogButtonBox(QtWidgets.QDialogButtonBox.Ok
                                        | QtWidgets.QDialogButtonBox.Cancel)
        bb.accepted.connect(dlg.accept)
        bb.rejected.connect(dlg.reject)
        f.addRow(bb)
        if dlg.exec() != QtWidgets.QDialog.Accepted:
            return
        name = e_name.text().strip() or "Conform_Hub"
        if name in {d["seq"] for d in self._inv}:
            self._say("⚠ A sequence named '%s' is already in scope — CCM tells "
                      "sequences apart by name, so the two will read as one "
                      "hub until one is renamed." % name)
        amode = "A-mode" in c_order.currentText()
        basis = c_basis.currentText()
        # destination: the reel the scanned sequences actually live in
        # (not the first desktop reel, which was the wrong home)
        reel = None
        for d in self._inv:
            reel = _ancestor(d["seg"], "PyReel")
            if reel is not None:
                break
        if reel is None:
            try:
                reel = _scratch_reel()
            except Exception as e:
                self._say("Create Conform Hub: no destination reel (%s)" % e)
                return
        rate = None
        for d in self._inv:
            if not d.get("is_hub"):
                rate = d.get("rate")
                break
        self._remember_timeline()
        sq, err = None, None
        for call in (lambda: reel.create_sequence(name=name, audio_tracks=0,
                                                  frame_rate="%g fps" % rate)
                     if rate else None,
                     lambda: reel.create_sequence(name=name, audio_tracks=0),
                     lambda: reel.create_sequence(name=name),
                     lambda: reel.create_sequence()):
            if call is None:
                continue
            try:
                sq = call()
                break
            except Exception as e:
                err = e
        if sq is None:
            self._say("create_sequence failed: %s" % err)
            return
        _set_flame_attr(sq, "name", name)
        st = self._st
        aliases = st.setdefault("hub_names", [])
        if name not in aliases:
            aliases.append(name)          # a hub by birth, whatever it's called
            save_state(st)
        # the hub just built is the one the Hub and Ledger tabs audit: they
        # judge the sequence named in Settings first, so an older hub (e.g.
        # 'Sources Sequence') would otherwise keep being audited instead
        s = load_settings()
        if s.get("sources_seq_name") != name:
            s["sources_seq_name"] = name
            save_settings(s)
            self._s = s
            if hasattr(self, "set_hub"):
                self.set_hub.setText(name)
            self._say("Settings → Conform Hub name is now '%s', so the Hub and "
                      "Ledger tabs audit this hub." % name)
        self._say("Created sequence '%s' in reel '%s'%s — registered as a hub."
                  % (name, _clean_name(reel),
                     (" @ %g fps" % rate) if rate else ""))
        # ---- auto-populate: one segment per REAL SOURCE, source-TC or A-mode order.
        # Never place just ONE piece
        # per logical shot group. Logical grouping collapses two grades of
        # the same shot (or any re-import) into one group, so the second
        # real source never enters the hub and every spot using it becomes
        # an ORPHAN — invisible to publish, relink and version bumps.
        # Flame's native builder emits one entry per source; so does CCM,
        # and the doubled entry is itself the duplicate-discovery signal.
        # Stacking: a shot that exists as several real sources is
        # ONE shot with multiple PIECES — not two entries side by side like
        # native. The longest piece sits on the base track in line with every
        # other shot; the rest stack above it ALIGNED BY SOURCE TIMECODE, and
        # the shot reserves lead/tail room so a stack never runs into its
        # neighbours. Work then happens once, at the longest duration, and
        # fans out to multiple renders in batch.
        shots = []
        for members in self._groups:
            cons = [i for i in members
                    if not self._inv[i].get("is_hub")
                    and self._inv[i].get("role") == "source"
                    and self._inv[i].get("src_in") is not None]
            if not cons:
                continue
            comps = self._source_components(cons)
            picks, pieces, straights = [], [], []
            for comp in comps:
                # match the EARLIEST-starting use so the head is exact; only
                # the tail is widened after placement
                pick = hub_match_pick(self._inv, comp)
                # the piece must serve the UNION of uses OF THIS SOURCE, not
                # just the picked instance — EDL diff vs the native builder
                # showed 9/21 pieces 1-4 fr short at the tail
                # (timewarped consumers read deeper than the widest clean one)
                w_lo, w_hi = hub_piece_range(self._inv, comp)
                picks.append(pick)
                pieces.append((w_lo if w_lo is not None else self._inv[pick]["src_in"],
                               w_hi if w_hi is not None else src_end(self._inv[pick])))
                straights.append(checked_use_range(self._inv, comp))
            stack = plan_shot_stack(pieces)
            if not stack["placements"]:
                continue
            if len(comps) > 1:
                d0 = self._inv[cons[0]]
                self._say("  ! %s exists as %d DISTINCT sources — stacking "
                          "them as one shot (longest on the base track, the "
                          "rest aligned above by source TC)"
                          % (d0.get("shot") or d0.get("name")
                             or d0.get("camera") or "?", len(comps)))
            src_key = min(a for a, _b in pieces if a is not None)
            if amode:
                in_basis = [self._inv[i]["rec_in_f"] for i in cons
                            if self._inv[i]["seq"] == basis
                            and self._inv[i]["rec_in_f"] is not None]
                key = (0, min(in_basis)) if in_basis else (1, src_key)
            else:
                key = (0, src_key)
            shots.append((key, picks, pieces, straights, stack))
        shots.sort(key=lambda t: t[0])   # C-mode: source TC · A-mode: basis record order
        if shots:      # count/consent shown IN the create dialog
            done, fails, short, placed_log, held = 0, 0, [], [], []
            media_ends = media_ends_of(self._st)
            cursor = 0            # record head of the next shot
            first_placed = False  # newborn-gap fix runs once, after shot 1
            for _key, picks, pieces, straights, stack in shots:
                landed = []       # (track, placed segment) for this shot
                for p in stack["placements"]:
                    pi = p["i"]
                    i = picks[pi]
                    d = self._inv[i]
                    label = d.get("shot") or d.get("name") or d.get("camera")
                    rec_at = cursor + p["start"]
                    trk_idx = p["track"]
                    if trk_idx:
                        label = "%s [stacked V%d]" % (label, trk_idx + 1)
                    clip = None
                    try:
                        clip = d["seg"].match(reel, preserve_handle=True,
                                              include_timeline_fx=False)
                        if isinstance(clip, (list, tuple)):
                            clip = clip[0] if clip else None
                        if clip is None:
                            raise RuntimeError("match returned nothing")
                        track = _ensure_video_track(sq, trk_idx)
                        if track is None and trk_idx:
                            fails += 1
                            self._say("  ⚠ %s: could not create video track "
                                      "%d — piece skipped" % (label, trk_idx + 1))
                            continue
                        # count BEFORE placing, and before any gap fix
                        n_real = len(_real_segments(track)) if track is not None else 0
                        ok, err = _overwrite_at(sq, clip, rec_at, track, rate)
                        if ok is None and not trk_idx:
                            ok = sq.insert(clip, sq.duration)   # last resort
                        if track is None:
                            track = _ensure_video_track(sq, 0)
                        placed = _placed_piece(track, d["src_in"], n_real) \
                            if track is not None else None
                        if placed is None:
                            fails += 1
                            self._say("  ⚠ %s: placement added no segment "
                                      "(%r / %s)" % (label, ok, err))
                            continue
                        done += 1
                        self._say("  + %s" % label)
                        # widen to the union of every use (timewarp envelopes
                        # included) from the placed piece's own readback, then
                        # hold it to the straight cuts the Hub tab checks
                        info = {}
                        try:
                            cover = _widen_to_union(placed, pieces[pi],
                                                    straights[pi], self._say, label,
                                                    info)
                        except Exception as e:
                            cover = None
                            self._say("  ⚠ %s union widen failed: %s" % (label, e))
                        # the media ENDS where a stretch clamps: remember it,
                        # and read use frames past it as HELD (a freeze on the
                        # last frame), never as missing
                        fpath = d.get("file_path") or ""
                        if fpath and info.get("out") is not None:
                            if info.get("clamped"):
                                media_ends[fpath] = info["out"]
                            elif media_ends.get(fpath, info["out"]) < info["out"]:
                                media_ends.pop(fpath, None)     # media got longer
                        if cover and cover[1] and info.get("clamped"):
                            held.append("%s: %d fr past the end of its media"
                                        % (label, cover[1]))
                            cover = (cover[0], 0)
                        if cover is None:
                            short.append("%s: could not read the placed piece "
                                         "back — check it by hand" % label)
                        elif cover[0] or cover[1]:
                            short.append("%s: %s" % (label, " + ".join(
                                "%d fr at %s" % (n, where) for n, where in
                                ((cover[0], "head"), (cover[1], "tail")) if n)))
                        landed.append((track, placed))
                        try:
                            placed_log.append((label, track, _frames(
                                getattr(placed, "source_in", None))))
                        except Exception:
                            pass
                    except Exception as e:
                        fails += 1
                        self._say("  ⚠ %s: %s" % (label, e))
                    finally:
                        if clip is not None:
                            okd, errd = _safe_delete(clip)
                            if not okd:
                                self._say("  ⚠ temp clip for %s left in the "
                                          "reel (%s) — delete it by hand"
                                          % (label, errd))
                    QtWidgets.QApplication.processEvents()
                # The newborn-gap fix runs only once the first shot is placed
                # AND widened: in the middle of placement it cancelled the
                # segment-count check and shot 1 was never widened.
                if landed and not first_placed:
                    first_placed = True
                    try:
                        _close_leading_gap(sq, self._say)
                    except Exception as e:
                        self._say("  gap fix failed: %s" % e)
                # the next shot starts where this one ACTUALLY ends — never at
                # the planned footprint, which left a gap after any piece that
                # came up short (read after widening + the gap fix)
                used, durs = [], []
                for trk, _seg in landed:
                    if any(trk is u for u in used):
                        continue
                    used.append(trk)
                    try:
                        durs.append([_frames(getattr(x, "record_duration", None))
                                     for x in (_get(trk, "segments") or [])])
                    except Exception:
                        pass
                cursor = shot_record_end(durs, cursor + stack["footprint"])
            # re-read every track at the END: a piece can vanish after it was
            # placed and checked — this is the only check that sees it
            if media_ends != media_ends_of(self._st) or self._st.get("media_ends"):
                self._st["media_ends_next"] = media_ends
                self._st["media_ends"] = {}      # migrated: legacy key emptied
                save_state(self._st)
            vanished = _vanished_pieces(placed_log)
            for label in vanished:
                self._say("   ✗ %s was placed but is NOT in the hub any more" % label)
            self._say("Populate: %d piece(s) placed, %d failed, %d vanished. "
                      "Reorder to taste, then Renumber / Rename."
                      % (done, fails, len(vanished)))
            if short:
                self._say("⚠ Coverage check — %d piece(s) do NOT hold every "
                          "frame the spots show (read back from Flame; a "
                          "clamp line above means the media ends there):"
                          % len(short))
                for line in short:
                    self._say("   ✗ " + line)
            for line in held:
                self._say("   ℹ %s — the spot holds the last frame (a freeze); "
                          "no hub piece can hold frames the media doesn't have, "
                          "so this is not missing." % line)
            if not short and done and not vanished:
                self._say("✓ Coverage check: every placed piece holds every "
                          "frame the spots show — cuts and timewarps, read "
                          "back from Flame.")
            if short or fails or vanished:
                # the console is hidden by default — this must not be missed
                QtWidgets.QMessageBox.warning(
                    self, "Create Conform Hub",
                    "The hub was built, but %s.\n\nOpen the Console for the "
                    "details." % " and ".join(x for x in (
                        ("%d piece(s) failed to place" % fails) if fails else "",
                        ("%d piece(s) don't cover every use" % len(short))
                        if short else "",
                        ("%d piece(s) vanished after placing (%s)"
                         % (len(vanished), ", ".join(vanished[:4])))
                        if vanished else "") if x))
        self._rescan_after_change(extra=[sq])
        # focus settles, then read the same list (now holding the hub) again
        QtCore.QTimer.singleShot(900, self._rescan_after_change)

    def _hub_connect(self):
        """Probe finding: the native scoped 'Source Segment
        Connections' operation is NOT exposed to python (no connect API on
        PySequence/PyReel), and seg.create_connection() only arms a plain
        segment connection — a matched-out hub shares no literal source with
        the spots, so nothing joins. CCM's job here: hand off to the native
        button with the right recipe, then VERIFY the result per shot."""
        primary = self._hub_rep.get("primary")
        box = QtWidgets.QMessageBox(self)
        box.setWindowTitle("Source Segment Connections")
        box.setText(
            "Flame's python API does not expose the scoped Source Segment "
            "Connections operation, so this step runs natively:\n\n"
            "1. In the Conform tab, run Create Source Segment Connections\n"
            "2. Scoping: Affect Sequences Reel Only ✓ · Overwrite Existing ✓ ·\n"
            "    Sync Segments OFF until duplicates/splits are resolved\n"
            "3. Come back and let CCM verify who actually connected.")
        box.setInformativeText(
            "CCM's Unify option makes regular segment connections only, not "
            "Source Segment Connections.")
        b_ccm = box.addButton("Unify media + segment connections (not source)",
                              QtWidgets.QMessageBox.ActionRole)
        b_verify = box.addButton("I ran it — Verify now", QtWidgets.QMessageBox.AcceptRole)
        box.addButton("Close", QtWidgets.QMessageBox.RejectRole)
        box.exec()
        if box.clickedButton() is b_ccm:
            self._ccm_connect(primary)
            return
        if box.clickedButton() is not b_verify:
            return
        lines, tot, tot_ok = [], 0, 0
        for members in self._groups:
            cons = [i for i in members
                    if not self._inv[i].get("is_hub")
                    and self._inv[i].get("role") == "source"]
            hub = [i for i in members
                   if self._inv[i].get("is_hub") and self._inv[i]["seq"] == primary
                   and self._inv[i].get("pub_nn") is None]
            if not hub or not cons:
                continue
            n_ok = 0
            for i in cons:
                d = self._inv[i]
                try:
                    d["conn_n"] = len(d["seg"].connected_segments(scoping="all reels"))
                except Exception:
                    d["conn_n"] = 0
                n_ok += 1 if d["conn_n"] else 0
            tot += len(cons)
            tot_ok += n_ok
            if n_ok < len(cons):
                nm = (self._inv[hub[0]].get("shot")
                      or self._inv[hub[0]].get("name") or "?")
                lines.append("⚠ %s: only %d of %d spot instance(s) connected"
                             % (nm, n_ok, len(cons)))
        self._say("Connection coverage: %d of %d spot instance(s) connected."
                  % (tot_ok, tot))
        for ln in lines:
            self._say(ln)
        if not lines and tot:
            self._say("Every consumed instance is connected — clean.")

    def _hub_selected_rows(self):
        out = []
        for mi in self.hub_table.selectionModel().selectedRows():
            it = self.hub_table.item(mi.row(), 0)
            if it is None:
                continue
            di = it.data(QtCore.Qt.UserRole)
            if isinstance(di, int) and di < len(self._hub_rep.get("rows", [])):
                out.append(di)
        return sorted(set(out))

    def _live_hub_segs(self, hub_row):
        """Live (primary-hub, non-publish-track) instances for a hub row."""
        primary = self._hub_rep.get("primary")
        members = self._groups[hub_row["group"]]
        return [self._inv[i] for i in members
                if self._inv[i].get("is_hub")
                and self._inv[i]["seq"] == primary
                and self._inv[i].get("pub_nn") is None]

    def _hub_rename(self):
        if not self._mutations_ok("Renumber / Rename"):
            return
        primary = self._hub_rep.get("primary")
        live = sorted((d for d in self._inv
                       if d.get("is_hub") and d["seq"] == primary
                       and d.get("pub_nn") is None
                       and d.get("rec_in_f") is not None),
                      key=lambda d: d["rec_in_f"])
        if not live:
            QtWidgets.QMessageBox.information(self, "Renumber",
                                              "No live hub segments in scope.")
            return
        # A stack is ONE shot: pieces on higher tracks overlapping a base
        # piece are the same shot in another grade/range, so they take the
        # SAME number (publish names come off the track,
        # so identical shot names across the stack are correct).
        groups = record_overlap_groups(
            [(d.get("track_id"), d["rec_in_f"], d.get("rec_dur_f")) for d in live])
        order = [live[g[0]] for g in groups]
        stacked = [live[i]["name"] for g in groups for i in g[1:]]
        pairs = plan_hub_rename([(d.get("shot") or d["name"]) for d in order], primary)
        # every piece of a stack gets its leader's new name
        changes = []
        for g, (old, new) in zip(groups, pairs):
            for k, idx in enumerate(g):
                d = live[idx]
                cur = d.get("shot") or d["name"]
                if cur != new:
                    changes.append((d, cur if k == 0 else "%s (stacked)" % cur,
                                    new))
        if not changes:
            self._say("Renumber: every hub shot already matches %s_###0." % primary)
            return
        lines = ["Set the SHOT NAME on %d piece(s) across %d shot(s), record "
                 "order?" % (len(changes), len(order)), ""]
        lines += ["   %s  →  %s" % (old or "(unnamed)", new) for _d, old, new in changes[:30]]
        if len(changes) > 30:
            lines.append("   … and %d more" % (len(changes) - 30))
        if stacked:
            lines += ["", "%d stacked piece(s) take their shot's number too — "
                          "one shot name across the whole stack (publish names "
                          "come off the track, so this stays unique)."
                          % len(stacked)]
        if QtWidgets.QMessageBox.question(
                self, "Renumber / Rename", "\n".join(lines),
                QtWidgets.QMessageBox.Yes | QtWidgets.QMessageBox.No) != QtWidgets.QMessageBox.Yes:
            return
        self._remember_timeline()
        done, failed = 0, []
        for d, _old, new in changes:
            if _set_flame_attr(d["seg"], "shot_name", new):   # the publish-token field
                done += 1
            else:
                failed.append(_old or d["camera"])
        self._say("Renumber: %d renamed, %d failed%s."
                  % (done, len(failed),
                     (" (%s)" % ", ".join(failed)) if failed else ""))
        self._rescan_after_change()

    def _hub_remove(self):
        if not self._mutations_ok("Remove from Hub"):
            return
        sel = self._hub_selected_rows()
        if not sel:
            QtWidgets.QMessageBox.information(
                self, "Remove from Hub", "Select the shot row(s) to remove first.")
            return
        rows = self._hub_rep["rows"]
        targets = []
        for si in sel:
            targets += self._live_hub_segs(rows[si])
        if not targets:
            QtWidgets.QMessageBox.information(
                self, "Remove from Hub", "Selection has no live hub segments.")
            return
        names = ", ".join((d["name"] or d["camera"]) for d in targets)
        if QtWidgets.QMessageBox.question(
                self, "Remove from Hub",
                "Remove %d segment(s) from the hub and close the gap?\n\n%s\n\n"
                "EXPERIMENTAL — tries extract (ripple) first; the result is "
                "verified and reported. Undoable in Flame." % (len(targets), names),
                QtWidgets.QMessageBox.Yes | QtWidgets.QMessageBox.No) != QtWidgets.QMessageBox.Yes:
            return
        self._remember_timeline()
        removed, remain = 0, []
        for d in targets:
            seg, label = d["seg"], d["name"] or d["camera"]
            track = getattr(seg, "parent", None)
            rec = str(getattr(seg, "record_in", ""))
            gone = False
            for meth, ripples in (("extract", True), ("lift", False)):
                fn = getattr(seg, meth, None)
                if not callable(fn):
                    continue
                try:
                    fn()
                except Exception as e:
                    self._say("%s.%s(): %s" % (label, meth, e))
                    continue
                still = _segment_at(track, rec) if track is not None else None
                if still is None or _seg_name(still) != _seg_name(seg):
                    gone = True
                    self._say("removed %s via %s()%s" % (label, meth,
                              "" if ripples else " — GAP left, ripple by hand"))
                    break
            if not gone:
                ok, err = _safe_delete(seg)
                if ok:
                    gone = True
                    self._say("removed %s via flame.delete — check the gap" % label)
                else:
                    remain.append(label)
                    self._say("⚠ could not remove %s (%s) — probe will find "
                              "the right API" % (label, err))
            removed += 1 if gone else 0
        self._say("Remove from Hub: %d removed, %d failed." % (removed, len(remain)))
        self._rescan_after_change()

    def _ccm_connect(self, primary):
        """Unify: a python route to shared-media segment connections. The
        native op isn't exposed, but a similar effect is reachable with verified
        calls: arm the hub segment, copy it to the media panel (the copy
        shares the hub's source — verified on box), then smart_replace_media the
        copy into each spot segment. The spot keeps its cut but now references
        the hub's source → it joins the connection. The replacement is
        picture-identical and keeps each spot's cut; the connection it makes
        is a regular segment connection, not a Source Segment Connection."""
        if not self._mutations_ok("Connect via CCM"):
            return
        shots = []
        for members in self._groups:
            hub = [i for i in members
                   if self._inv[i].get("is_hub") and self._inv[i]["seq"] == primary
                   and self._inv[i].get("pub_nn") is None]
            cons = [i for i in members
                    if not self._inv[i].get("is_hub")
                    and self._inv[i].get("role") == "source"]
            if hub and cons:
                shots.append((hub[0], cons))
        if not shots:
            self._say("Connect via CCM: no hub shots with consumers in scope.")
            return
        if QtWidgets.QMessageBox.question(
                self, "Connect via CCM",
                "Replace the media of every spot instance with its hub shot's "
                "source (smart_replace_media — each keeps its own cut), joining "
                "them to the hub's connection.\n\n%d shot(s). Timewarped and "
                "uncovered instances are skipped and listed. Undoable in Flame."
                "\n\nTIP for the first run: test a copy of ONE spot and check "
                "the picture doesn't shift (that answers the alignment "
                "question)." % len(shots),
                QtWidgets.QMessageBox.Yes | QtWidgets.QMessageBox.No) != QtWidgets.QMessageBox.Yes:
            return
        self._remember_timeline()
        reel = _scratch_reel()
        replaced = skipped = errors = 0
        for hub_i, cons in shots:
            h = self._inv[hub_i]
            label = h.get("shot") or h.get("name") or h.get("camera")
            clip = None
            try:
                try:
                    h["seg"].create_connection()      # arm (already-armed ok)
                except Exception:
                    pass
                clip = h["seg"].copy_to_media_panel(reel)
                if isinstance(clip, (list, tuple)):
                    clip = clip[0] if clip else None
                if clip is None:
                    raise RuntimeError("copy_to_media_panel returned nothing")
                for ci in cons:
                    d = self._inv[ci]
                    who = "%s in %s" % (label, d["seq"])
                    if d["timewarp"] and not d.get("tw_model"):
                        skipped += 1
                        self._say("  ~ %s skipped (timewarp, curve unreadable "
                                  "— handle manually)" % who)
                        continue
                    if None not in (d["src_in"], d["src_out"], h["src_in"], h["src_out"])                             and not (h["src_in"] <= d["src_in"]
                                     and d["src_out"] <= h["src_out"]):
                        skipped += 1
                        self._say("  ⚠ %s skipped (hub piece doesn't cover its "
                                  "range — split/extend first)" % who)
                        continue
                    try:
                        before = _frames(getattr(d["seg"], "record_duration", None))
                        d["seg"].smart_replace_media(clip)
                        after = _frames(getattr(d["seg"], "record_duration", None))
                        if before is not None and after is not None and before != after:
                            self._say("  ⚠ %s: duration changed %s→%s — CHECK"
                                      % (who, before, after))
                        replaced += 1
                    except Exception as e:
                        errors += 1
                        self._say("  ⚠ %s: %s" % (who, e))
                try:
                    n = len(h["seg"].connected_segments(scoping="all reels"))
                    self._say("%s: %d connected instance(s)" % (label, n))
                except Exception:
                    pass
            except Exception as e:
                errors += 1
                self._say("⚠ %s: %s" % (label, e))
            finally:
                if clip is not None:
                    _safe_delete(clip)
            QtWidgets.QApplication.processEvents()
        self._say("Connect via CCM: %d replaced, %d skipped, %d error(s). "
                  "Check a shot's picture before moving on. Rescanning."
                  % (replaced, skipped, errors))
        self._rescan_after_change()

    # ---------------------------------------------------------------- Ledger tab
    def _build_ledger_tab(self):
        w = QtWidgets.QWidget()
        v = QtWidgets.QVBoxLayout(w)
        v.addWidget(self._wip_banner())
        top = QtWidgets.QHBoxLayout()
        self.led_label = _squish(QtWidgets.QLabel("Press Scan."))
        top.addWidget(self.led_label, 1)
        top.addWidget(self._pv_button())
        v.addLayout(top)

        led_cols = (
            ("Shot", "The source's camera roll name (the camera token CCM "
                     "groups by)."),
            ("Hub Name", "The shot's name in the Conform Hub — what publish "
                         "names its output with."),
            ("Forks", "How many live pieces the shot has in the hub, when "
                      "it's more than one (alt versions stacked under one "
                      "shot name)."),
            ("Snapshots", "The Publish NN tracks holding a frozen copy of "
                          "this shot."),
            ("Openclip", "The output openclip its publish writes to — blank "
                         "until it has been published."),
            ("Current", "The openclip version the timeline uses now."),
            ("Latest", "The newest version in that openclip."),
            ("State", "unpublished (next: 00) · prepped NN — not yet "
                      "published · published · OFF-LATEST, a newer version "
                      "exists than the one in use (amber)."),
        )
        self.led_table = QtWidgets.QTableWidget(0, len(led_cols))
        self.led_table.setHorizontalHeaderLabels([c for c, _t in led_cols])
        for c, (_label, tip) in enumerate(led_cols):
            hdr_item = self.led_table.horizontalHeaderItem(c)
            if hdr_item is not None:
                hdr_item.setToolTip(tip)
        self.led_table.verticalHeader().setVisible(False)
        self.led_table.setSelectionBehavior(QtWidgets.QAbstractItemView.SelectRows)
        self.led_table.setSelectionMode(QtWidgets.QAbstractItemView.ExtendedSelection)
        self.led_table.setEditTriggers(QtWidgets.QAbstractItemView.NoEditTriggers)
        self.led_table.setSortingEnabled(True)
        _init_table_resize(self.led_table, stretch_cols=(4,))     # openclip
        self.led_table.itemSelectionChanged.connect(self._led_pv_selected)
        led_split = QtWidgets.QSplitter(QtCore.Qt.Horizontal)
        led_split.addWidget(self.led_table)
        self.led_pv = self._pv_panel("led")
        led_split.addWidget(self.led_pv)
        led_split.setStretchFactor(0, 3)
        led_split.setStretchFactor(1, 2)
        led_split.setSizes([620, 380])
        self._pv_vis["led"] = self.led_pv.setVisible
        v.addWidget(led_split, 1)

        # legend: what the rows are, then
        # how a publish goes, in the order you do it
        legend = QtWidgets.QLabel(
            "One row per shot in the Conform Hub, showing how far it has got "
            "through publishing (hover a column header for what it holds). "
            "To publish: select the shots you've worked on, press Prep "
            "Publish, then run Flame's own publish on the Publish NN track it "
            "fills. Prep Publish puts a frozen copy of each shot on that track "
            "— copied, unlinked and given its own source, so later changes to "
            "the hub can't alter what was published. Only the shots you "
            "select are prepped, and a shot's first publish always goes to "
            "Publish 00, however late it comes. Fix mode re-does a bad publish "
            "on its existing Publish NN instead of starting the next number.")
        legend.setWordWrap(True)
        legend.setStyleSheet("color:#777777;")
        v.addWidget(self._tip(legend))

        actions = QtWidgets.QGroupBox("PREP PUBLISH  (select shots above)")
        a = QtWidgets.QHBoxLayout(actions)
        self.led_fix = QtWidgets.QCheckBox("Fix mode (replace current version)")
        self.led_fix.setToolTip(
            "An erroneous publish is re-prepped INTO its existing Publish NN "
            "instead of incrementing — no need to keep a broken version.")
        a.addWidget(self.led_fix)
        a.addStretch(1)
        self.b_prep = QtWidgets.QPushButton("Prep Publish…")
        self.b_prep.setObjectName("primary")
        self.b_prep.clicked.connect(self._prep_publish)
        a.addWidget(self.b_prep)
        v.addWidget(actions)
        self._ledger = {"rows": [], "primary": None}
        self._update_mutation_gate()
        return w

    def _update_mutation_gate(self):
        on = bool((self._s or {}).get("enable_mutations"))
        if hasattr(self, "b_prep"):
            self.b_prep.setToolTip(
                "Snapshot the selected shots to their Publish NN tracks." if on
                else "Mutations are OFF (Settings) — clicking offers to enable.")

    def _mutations_ok(self, what):
        """A silently disabled button reads as 'broken', so gate
        checks SPEAK — and offer to flip the switch right here."""
        if (self._s or {}).get("enable_mutations"):
            return True
        if QtWidgets.QMessageBox.question(
                self, "Mutations are disabled",
                "%s CHANGES your project. CCM is currently in read-only mode "
                "(the safety default) — nothing it does can alter a timeline.\n\n"
                "Allow changes now?" % what,
                QtWidgets.QMessageBox.Yes | QtWidgets.QMessageBox.No) == QtWidgets.QMessageBox.Yes:
            s = load_settings()
            s["enable_mutations"] = True
            save_settings(s)
            self._s = s
            if hasattr(self, "set_mutations"):
                self.set_mutations.setChecked(True)
            self._update_mutation_gate()
            return True
        return False

    def _render_ledger(self):
        self._ledger = publish_ledger(self._inv, self._groups, self._oc,
                                      (self._s or {}).get("sources_seq_name", ""))
        rows = self._ledger["rows"]
        n_pub = sum(1 for r in rows if r["published"])
        n_off = sum(1 for r in rows if r["off_latest"])
        self.led_label.setText(
            "%d live hub shot(s) in '%s' — %d published · %d unpublished · "
            "%d off-latest" % (len(rows), self._ledger.get("primary") or "?",
                               n_pub, len(rows) - n_pub, n_off))
        amber = QtGui.QColor("#e0b000")
        grey = QtGui.QColor("#888888")
        self.led_table.blockSignals(True)
        self.led_table.clearSelection()     # row indices are stale after a rebuild
        self.led_table.setSortingEnabled(False)
        self.led_table.setRowCount(len(rows))
        for r, d in enumerate(rows):
            if d["published"]:
                state = ("OFF-LATEST %s → %s" % (d["version_uid"], d["latest"])
                         if d["off_latest"] else "published %s" % d["version_uid"])
            elif d["snaps"]:
                state = "prepped %s — not yet published" % \
                        ", ".join("%02d" % n for n in d["snaps"])
            else:
                state = "unpublished (next: 00)"
            vals = [d["camera"], d["hub_name"],
                    d["n_forks"] if d["n_forks"] > 1 else "",
                    ", ".join("%02d" % n for n in d["snaps"]),
                    d["clip_name"],
                    d["version_uid"], d["latest"], state]
            for c, val in enumerate(vals):
                it = QtWidgets.QTableWidgetItem()
                if isinstance(val, int):
                    it.setData(QtCore.Qt.DisplayRole, val)
                else:
                    it.setText(str(val))
                if c == 0:
                    it.setData(QtCore.Qt.UserRole, r)
                if c == 7:
                    if d["off_latest"]:
                        it.setForeground(amber)
                    elif not d["published"]:
                        it.setForeground(grey if not d["snaps"] else amber)
                self.led_table.setItem(r, c, it)
        self.led_table.setSortingEnabled(True)
        self.led_table.blockSignals(False)
        _autosize_once(self.led_table)
        self._update_mutation_gate()

    def _selected_ledger_rows(self):
        out = []
        for mi in self.led_table.selectionModel().selectedRows():
            it = self.led_table.item(mi.row(), 0)
            if it is None:
                continue
            di = it.data(QtCore.Qt.UserRole)
            if isinstance(di, int) and di < len(self._ledger["rows"]):
                out.append(di)
        return sorted(set(out))

    def _led_current_row(self):
        sel = self._selected_ledger_rows()
        return self._ledger["rows"][sel[0]] if sel else None

    def _led_pv_selected(self):
        """A ledger row → its hub piece's picture (the first row of a
        multi-row selection)."""
        r = self._led_current_row()
        if r is None:
            self._pv_show("led", [])
            return
        g = r.get("group")
        members = (self._groups[g] if isinstance(g, int) and g < len(self._groups)
                   else [r["hub_idx"]])
        self._pv_show("led", members, r["hub_idx"], r["hub_name"] or r["camera"])

    def _prep_publish(self):
        if not self._mutations_ok("Prep Publish"):
            return
        rows = self._ledger["rows"]
        if not rows:
            QtWidgets.QMessageBox.information(
                self, "Prep Publish",
                "No live hub shots in the current scope.\n\nMake sure the scope "
                "includes your Conform Hub (its name is set in Settings), "
                "then Scan again.")
            return
        sel = self._selected_ledger_rows()
        if not sel:
            QtWidgets.QMessageBox.information(
                self, "Prep Publish",
                "Select the shot ROWS to prep in the table first — sparse "
                "publishing: only what's being worked on.")
            return
        fix = self.led_fix.isChecked()
        plan = plan_prep_publish(rows, sel, fix_mode=fix)
        # pre-flight: surface unresolved audits — the 'checks are made' step
        undecided = [d for d in self._dup_rows
                     if not d["decision"] and not d["sanctioned"]]
        lines = ["Prep Publish — %d shot(s)%s in '%s'"
                 % (len(sel), " (FIX MODE)" if fix else "",
                    self._ledger.get("primary") or "?"), ""]
        if undecided:
            lines.append("⚠ %d duplicate conflict set(s) still untriaged "
                         "(Duplicates tab)" % len(undecided))
        for wmsg in plan["warnings"]:
            lines.append("⚠ " + wmsg)
        for grp in plan["groups"]:
            lines.append("")
            lines.append("%s %s:" % ("CREATE track" if grp["create"]
                                     else "Fill track", grp["track"]))
            for si in grp["shots"]:
                d = rows[si]
                mark = "  ↻ replace" if (d["hub_name"] or d["camera"]) in grp["replaces"] else ""
                lines.append("   • %s%s" % (d["hub_name"] or d["camera"], mark))
            if grp["hide"]:
                lines.append("   hide (already published): %s" % ", ".join(grp["hide"]))
        lines += ["", "Each snapshot is copied, unlinked and source-duplicated — "
                      "an immutable record. The live track is untouched. "
                      "Undoable in Flame. Proceed?"]
        if QtWidgets.QMessageBox.question(
                self, "Prep Publish", "\n".join(lines),
                QtWidgets.QMessageBox.Yes | QtWidgets.QMessageBox.No) != QtWidgets.QMessageBox.Yes:
            return
        self._remember_timeline()
        seq = _ancestor(self._inv[rows[sel[0]]["hub_idx"]]["seg"], "PySequence")
        if seq is None:
            self._say("Prep Publish: could not resolve the hub sequence object.")
            return
        done, manual, errors = execute_prep_publish(
            seq, plan, self._inv, rows, self._say,
            (self._s or {}).get("publish_track_position", "bottom"),
            (self._s or {}).get("publish_snapshot_as", "version"))
        for e in errors:
            self._say("⚠ " + e)
        self._say("Prep Publish: %d snapshot(s) done, %d error(s)." % (done, len(errors)))
        if manual:
            self._say("MANUAL steps remaining:")
            for m in manual:
                self._say("   • " + m)
        self._rescan_after_change()

    def _consumer_seq_names(self):
        names, seen = [], set()
        for d in self._inv:
            if d.get("is_hub") or d.get("role") == "ref":
                continue
            if d["seq"] not in seen:
                seen.add(d["seq"]); names.append(d["seq"])
        return sorted(names)

    # ---------------------------------------------------------------- probe
    def _run_probe(self):
        """READ-ONLY on-box diagnostics: what this Flame's python API exposes
        for CCM — image access for the inspector preview (wiretap / exporter /
        player control), timewarp FX class names, segment→source-clip mapping,
        track/version creation + primary-track surface, publish/export hooks,
        Conform Sources Merge pref, openclip discovery. Writes
        flame_ccm_probe.txt (next to the script, else the home folder) and
        echoes a summary to the panel. Mutates nothing —
        introspection (dir/getattr/docstrings) and wiretap format reads only."""
        L = ["==== Flame CCM %s on-box probe ====" % __version__]

        def head(t):
            L.append("")
            L.append("---- %s ----" % t)

        def grab(label, fn):
            # PyAttribute wrappers can have a BROKEN __repr__ (seen on box: it
            # returned a raw bool / PyTrack) — normalize through get_value /
            # str / type name instead of trusting repr.
            try:
                v = fn()
            except Exception as e:
                L.append("%s: <err %s>" % (label, e))
                return
            for render in (lambda: repr(v.get_value()) + "  (via get_value)",
                           lambda: repr(v),
                           lambda: "%s  (type %s)" % (str(v), type(v).__name__),
                           lambda: "<unprintable %s>" % type(v).__name__):
                try:
                    L.append("%s: %s" % (label, render()))
                    return
                except Exception:
                    continue

        def hits(label, obj, keys):
            if obj is None:
                L.append("%s: (object is None)" % label)
                return
            try:
                names = [n for n in dir(obj)
                         if any(k in n.lower() for k in keys) and not n.startswith("__")]
                L.append("%s: %s" % (label, ", ".join(sorted(names)) or "(none)"))
            except Exception as e:
                L.append("%s: <err %s>" % (label, e))

        head("flame module")
        grab("version", lambda: flame.get_version())
        grab("current tab", lambda: flame.get_current_tab())
        hits("dir(flame) player/exec/prefs", flame,
             ("player", "view", "shortcut", "execute", "go_to", "export",
              "import", "pref", "batch"))
        # docstrings tell us the callable signatures
        for fn in ("execute_shortcut", "execute_command", "go_to"):
            grab("flame.%s doc" % fn,
                 lambda fn=fn: (getattr(flame, fn).__doc__ or "(no doc)").strip()[:400])
        grab("PyExporter constructible", lambda: type(flame.PyExporter()).__name__)
        try:
            ex = flame.PyExporter()
            hits("dir(PyExporter)", ex, ("export", "preset", "mark", "fore", "hook"))
            grab("PyExporter.export doc",
                 lambda: (ex.export.__doc__ or "(no doc)").strip()[:400])
            grab("presets base dir", lambda: flame.PyExporter.get_presets_base_dir())
        except Exception as e:
            L.append("PyExporter details: <err %s>" % e)

        head("wiretap")
        wt = None
        try:
            import libwiretapPythonClientAPI as wt
            L.append("libwiretapPythonClientAPI: IMPORT OK")
            grab("API surface (Client/Server/Node/Clip)", lambda: sorted(
                n for n in dir(wt) if any(k in n for k in
                ("Client", "Server", "Node", "Clip", "Int")))[:40])
        except Exception as e:
            L.append("libwiretapPythonClientAPI: import failed — %s" % e)

        # segment level: selected Inventory row, else inspected shot, else current
        seg = None
        sel = self._selected_inv()
        if sel:
            seg = self._inv[sel[0]]["seg"]
        if seg is None and self._cov_members:
            seg = self._inv[self._cov_members[0]]["seg"]
        if seg is None:
            seg = _current_segment()
        head("segment")
        if seg is None:
            L.append("(no segment available — Scan, select an Inventory row, re-run)")
        else:
            L.append("segment: %s" % _clean_name(seg))
            # FX are a generic 'PyTimelineFX' class — the effect's own
            # type/name strings are where 'Timewarp' should appear. RUN THIS ON
            # A RETIMED SEGMENT.
            grab("effects deep (class, .type, .name)  [SELECT A TIMEWARPED SEG]",
                 lambda: [(type(e).__name__, str(getattr(e, "type", None)),
                           str(getattr(e, "name", None)))
                          for e in (seg.effects or [])])
            # seg.match is the segment→source-clip bridge (Create Conform Hub uses it)
            grab("seg.match doc", lambda: (seg.match.__doc__ or "(no doc)").strip()[:500])
            grab("seg.container_clip", lambda: getattr(seg, "container_clip", None))
            # the Duplicates tab rides on shared_source_segments —
            # confirm signature, return type, and CROSS-SEQUENCE reach (run
            # with a segment selected that is known-shared into other
            # sequences; a 3-shared-+-1-duplicated test setup is ideal)
            grab("seg.shared_source_segments doc",
                 lambda: (seg.shared_source_segments.__doc__ or "(no doc)").strip()[:400])
            def _shared_probe():
                ss = seg.shared_source_segments() or []
                out = []
                for s in list(ss)[:12]:
                    sqn = _clean_name(_ancestor(s, "PySequence")) if _ancestor(s, "PySequence") else "?"
                    out.append("%s @ %s" % (_clean_name(s), sqn))
                return "%d shared: %s" % (len(ss), "; ".join(out) or "—")
            grab("seg shared-source reach", _shared_probe)
            # signature CONFIRMED no-args, reach CONFIRMED
            # cross-sequence (a scoping kwarg is rejected)
            # scenario-#1 discriminator — a readable FILE PATH
            # separates 'same file imported twice' (safe auto-merge) from
            # 'separate per-spot delivery' (grade must be verified first)
            grab("seg path-ish attrs", lambda: [a for a in dir(seg)
                 if "path" in a.lower() or "file" in a.lower()])
            for a in ("file_path", "source_path", "path", "media_path",
                      "original_source_file"):
                grab("seg.%s" % a, lambda a=a: getattr(seg, a, None))
            # source_uid reads EMPTY on box — what do the sibling uids hold?
            for a in ("uid", "source_uid", "source_essence_uid",
                      "original_source_uid", "source_name", "source_frame_rate",
                      "start_frame", "source_width", "source_height",
                      "source_bit_depth", "source_cached"):
                grab("seg.%s" % a, lambda a=a: getattr(seg, a, None))
            track = getattr(seg, "parent", None)
            ver = getattr(track, "parent", None) if track is not None else None
            sq = _ancestor(seg, "PySequence")
            # dir() misses dynamic attrs (track.name exists anyway) —
            # test known names directly instead
            for a in ("name", "hidden", "visible", "mute", "selected", "primary"):
                grab("track.%s" % a, lambda a=a: getattr(track, a, None))
            grab("track.name has set_value",
                 lambda: hasattr(getattr(track, "name", None), "set_value"))
            for a in ("primary_track", "primary", "current_track"):
                grab("seq.%s" % a, lambda a=a: getattr(sq, a, None))
            grab("seq.create_version doc",
                 lambda: (sq.create_version.__doc__ or "(no doc)").strip()[:400])
            grab("version.create_track doc",
                 lambda: (ver.create_track.__doc__ or "(no doc)").strip()[:400])
            grab("seg.hidden direct", lambda: getattr(seg, "hidden", "(absent)"))
            # publish unknowns: hidden/primary writability, razor/split, overwrite
            grab("seg.hidden has set_value",
                 lambda: hasattr(getattr(seg, "hidden", None), "set_value"))
            grab("seq.primary_track has set_value",
                 lambda: hasattr(getattr(sq, "primary_track", None), "set_value"))
            hits("dir(seg) razor/split", seg, ("split", "cut", "razor", "slice"))
            hits("dir(track) razor/split", track, ("split", "cut", "razor", "slice"))
            hits("dir(seq) razor/split/overwrite", sq,
                 ("split", "cut", "razor", "slice", "overwrite", "insert"))
            grab("seq.overwrite doc",
                 lambda: (sq.overwrite.__doc__ or "(no doc)").strip()[:400])
            grab("seg.copy_to_media_panel doc",
                 lambda: (seg.copy_to_media_panel.__doc__ or "(no doc)").strip()[:400])
            hits("dir(project) create sequence/library", flame.projects.current_project,
                 ("create", "sequence", "library"))
            # hub-edit unknowns: segment removal, razor, rename, marks
            hits("dir(seg) delete/extract/lift", seg,
                 ("delete", "extract", "lift", "remove", "ripple"))
            hits("dir(track) delete/extract", track,
                 ("delete", "extract", "lift", "remove", "ripple"))
            grab("seq.cut doc", lambda: (sq.cut.__doc__ or "(no doc)").strip()[:300])
            grab("track.cut doc",
                 lambda: (track.cut.__doc__ or "(no doc)").strip()[:300])
            grab("seq.insert doc",
                 lambda: (sq.insert.__doc__ or "(no doc)").strip()[:300])
            grab("seg.name has set_value",
                 lambda: hasattr(getattr(seg, "name", None), "set_value"))
            grab("flame.delete doc",
                 lambda: (flame.delete.__doc__ or "(no doc)").strip()[:300])
            grab("reel create candidates", lambda: sorted(
                n for n in dir(_scratch_reel()) if "create" in n.lower()))
            grab("reel.create_sequence doc", lambda: (
                _scratch_reel().create_sequence.__doc__ or "(no doc)").strip()[:400])
            grab("seg.shot_name", lambda: getattr(seg, "shot_name", "(absent)"))
            grab("seg.shot_name has set_value",
                 lambda: hasattr(getattr(seg, "shot_name", None), "set_value"))
            # hunt for the NATIVE 'Source Segment Connections' API (the
            # scoped popup: reel-only / overwrite / sync / harmonise) — plain
            # seg.create_connection() is NOT it
            hits("dir(seq) connect/source", sq, ("connect", "source_segment"))
            hits("dir(version) move/reorder", ver,
                 ("move", "index", "reorder", "swap", "position", "up", "down"))
            hits("dir(seq) move/reorder", sq, ("move", "reorder", "swap"))
            grab("seg.smart_replace_media doc", lambda: (
                seg.smart_replace_media.__doc__ or "(no doc)").strip()[:400])
            hits("dir(seg) trim/slip", seg, ("trim", "slip", "slide"))
            hits("dir(reel) connect/source", _scratch_reel(),
                 ("connect", "source", "sync"))
            hits("dir(flame) time/connect", flame, ("pytime", "time", "connect"))
            grab("flame.PyTime doc",
                 lambda: (flame.PyTime.__doc__ or "(no doc)").strip()[:400])
            grab("seg.create_connection doc",
                 lambda: (seg.create_connection.__doc__ or "(no doc)").strip()[:400])

            # Connections tab readiness (READ-ONLY). The tab colours
            # shared-source segments BLUE (cross-sequence reach confirmed)
            # and connected segments ORANGE, and double-click SELECTs
            # the connected segments in Flame. Checks: connected_
            # segments' scoping values and reach, and any segment-selection
            # API. Run with a CONNECTED segment selected in the Inventory.
            head("connections tab readiness  [select a CONNECTED segment]")
            grab("seg.connected_segments doc", lambda: (
                seg.connected_segments.__doc__ or "(no doc)").strip()[:500])
            for scoping in (None, "all reels", "sequences reel", "current reel",
                            "all", "current sequence"):
                def _conn(scoping=scoping):
                    got = (seg.connected_segments() if scoping is None
                           else seg.connected_segments(scoping=scoping)) or []
                    out = []
                    for x in list(got)[:10]:
                        sqs = _ancestor(x, "PySequence")
                        out.append("%s @ %s" % (_clean_name(x),
                                                _clean_name(sqs) if sqs else "?"))
                    return "%d: %s" % (len(got), "; ".join(out) or "—")
                grab("connected_segments(%s)" % ("" if scoping is None
                                                 else "scoping=%r" % scoping), _conn)
            # if Flame exposes media length on the segment, the Hub
            # tab can know where media ENDS without a hub build (freeze frames
            # held past the end are then recognised on any scan)
            for a in ("head", "tail", "source_duration", "source_out"):
                grab("seg.%s" % a, lambda a=a: getattr(seg, a, "(absent)"))
            grab("seg.remove_connection doc", lambda: (
                seg.remove_connection.__doc__ or "(no doc)").strip()[:300])
            grab("seg.selected", lambda: getattr(seg, "selected", "(absent)"))
            grab("seg.selected has set_value",
                 lambda: hasattr(getattr(seg, "selected", None), "set_value"))
            hits("dir(seg) select", seg, ("select",))
            hits("dir(flame.timeline) select/segment", getattr(flame, "timeline", None),
                 ("select", "segment", "clip", "current"))
            hits("dir(seq) select", sq, ("select",))
            hits("dir(track) select", track, ("select",))

        head("media panel clip  [select the shot's SOURCE or an OPENCLIP first]")
        clip = None
        try:
            ents = _get(flame.media_panel, "selected_entries") or []
            clip = next((e for e in ents
                         if type(e).__name__ in ("PyClip", "PySequence")), None)
        except Exception:
            pass
        if clip is None:
            L.append("(nothing usable selected in the media panel)")
        else:
            L.append("clip: %s (%s)" % (_clean_name(clip), type(clip).__name__))
            hits("dir(clip) image access", clip,
                 ("wiretap", "thumb", "poster", "proxy", "frame", "export",
                  "open", "current_time"))
            for a in ("version_uid", "version_uids", "start_frame", "duration",
                      "frame_rate", "essence_uid", "in_mark", "out_mark"):
                grab("clip.%s" % a, lambda a=a: getattr(clip, a, None))
            nid = None
            for m in ("get_wiretap_node_id", "get_wiretap_storage_id"):
                if hasattr(clip, m):
                    grab("clip.%s()" % m, lambda m=m: getattr(clip, m)())
                    if m == "get_wiretap_node_id" and nid is None:
                        try:
                            nid = str(getattr(clip, m)())
                        except Exception:
                            pass
                else:
                    L.append("clip.%s: (absent)" % m)
            # The wiretap read test is OFF by default: on a
            # real job getClipFormat sat on a network/uncached clip
            # and froze the whole app for two minutes. Its question was
            # already answered — wiretap cannot read Uncached
            # media, which is why the filmstrip exporter exists — so this
            # only runs when someone deliberately turns it back on.
            if wt is not None and nid and load_settings().get("probe_wiretap"):
                head("wiretap read test (format + ONE timed frame read)")
                try:
                    if not wt.WireTapClientInit():
                        L.append("WireTapClientInit: FAILED")
                    else:
                        srv = wt.WireTapServerHandle("localhost:IFFFS")
                        node = wt.WireTapNodeHandle(srv, str(nid).strip("'\""))
                        fmt = wt.WireTapClipFormat()
                        if node.getClipFormat(fmt):
                            grab("format w×h", lambda: (fmt.width(), fmt.height()))
                            grab("format tag", lambda: fmt.formatTag())
                            grab("bits/pixel", lambda: fmt.bitsPerPixel())
                            grab("frame buffer size", lambda: fmt.frameBufferSize())
                            try:
                                n_f = wt.WireTapInt()      # WireTapInt, not Int32
                                if node.getNumFrames(n_f):
                                    L.append("num frames: %d" % int(n_f))
                            except Exception as e:
                                L.append("getNumFrames: <err %s>" % e)
                            # the money test: can we pull ONE frame's pixels,
                            # and how long does it take? (decides live scrub
                            # vs filmstrip; read-only)
                            try:
                                size = int(fmt.frameBufferSize())
                                buf = bytearray(size)
                                t0 = time.time()
                                ok = node.readFrame(0, buf, size)
                                dt = time.time() - t0
                                nz = sum(1 for b in buf[:4096] if b)
                                L.append("readFrame(0): ok=%s in %.3fs "
                                         "(%d bytes; %d/4096 leading bytes non-zero)"
                                         % (bool(ok), dt, size, nz))
                            except Exception as e:
                                L.append("readFrame: <err %s>" % e)
                        else:
                            L.append("getClipFormat failed: %s" % node.lastError())
                except Exception as e:
                    L.append("wiretap test: <err %s>" % e)

        head("CONNECTED-CONFORM API GAP AUDIT  (native operations not found "
             "in Flame's python API)")
        objs = {}
        try:
            objs = {"flame": flame, "project": flame.projects.current_project,
                    "media_panel": flame.media_panel,
                    "timeline": getattr(flame, "timeline", None),
                    "reel": _scratch_reel()}
        except Exception:
            pass
        if seg is not None:
            objs["seg"] = seg
            objs["track"] = getattr(seg, "parent", None)
            objs["version"] = getattr(objs.get("track"), "parent", None)
            objs["seq"] = _ancestor(seg, "PySequence")

        def hunt(*keywords):
            out = []
            for on, o in objs.items():
                if o is None:
                    continue
                try:
                    ms = sorted(n for n in dir(o) if not n.startswith("__")
                                and any(k in n.lower() for k in keywords))
                except Exception:
                    continue
                if ms:
                    out.append("%s: %s" % (on, ",".join(ms)))
            return "; ".join(out)

        def gap(feature, *keywords):
            ev = hunt(*keywords)
            L.append("%-44s %s" % (feature,
                     ("EXPOSED?  " + ev) if ev else "NOT FOUND"))

        L.append("(native op -> python hunt across flame/project/media_panel/"
                 "timeline/reel/seg/track/version/seq)")
        L.append("NOTE: dir() hides some dynamic attrs — seg.hidden and "
                 "seq.primary_track are VERIFIED settable despite NOT FOUND "
                 "lines below.")
        # docs for API calls the gap audit found (behavior not yet mapped)
        for on, mn in (("flame", "duplicate"), ("flame", "duplicate_many"),
                       ("seg", "slip"), ("seg", "trim_head"),
                       ("seg", "set_version_uid"), ("seq", "update_sources"),
                       ("seq", "extract_selection_to_media_panel"),
                       ("seq", "lift_selection_to_media_panel"),
                       ("media_panel", "move")):
            o = objs.get(on)
            fn = getattr(o, mn, None) if o is not None else None
            if fn is not None:
                grab("%s.%s doc" % (on, mn),
                     lambda fn=fn: (fn.__doc__ or "(no doc)").strip()[:400])
        # timewarp CURVE readability: dump the TW FX object itself
        if seg is not None:
            try:
                tw = next((e for e in (seg.effects or [])
                           if "timewarp" in type(e).__name__.lower()), None)
                if tw is not None:
                    grab("dir(PyTimewarpTimelineFX)", lambda: sorted(
                        n for n in dir(tw) if not n.startswith("__"))[:60])
                    # dir() hides dynamic attrs — hit the likely names directly
                    for a in ("mode", "duration_speed", "sample_mode",
                              "frame_interpolation_mode", "bypass"):
                        grab("tw.%s" % a, lambda a=a: getattr(tw, a, "(absent)"))
                    # per-FX sync + setup-transplant (selective FX sync
                    # between instances)
                    grab("fx.sync_connected_segments doc", lambda: (
                        tw.sync_connected_segments.__doc__ or "(no doc)").strip()[:350])
                    grab("fx.load_setup doc", lambda: (
                        tw.load_setup.__doc__ or "(no doc)").strip()[:350])
                    others = [e for e in (seg.effects or []) if e is not tw]
                    if others:
                        grab("other FX (%s) has save/load_setup+sync"
                             % str(getattr(others[0], "type", "?")),
                             lambda: [n for n in
                                      ("save_setup", "load_setup",
                                       "sync_connected_segments")
                                      if hasattr(others[0], n)])
                    # the curve API exists — docs + safe reads
                    for mn in ("get_timing", "get_speed", "get_speed_timing",
                               "get_duration_timing", "set_timing", "set_speed",
                               "save_setup", "get_expression"):
                        fn = getattr(tw, mn, None)
                        if fn is not None:
                            grab("tw.%s doc" % mn,
                                 lambda fn=fn: (fn.__doc__ or "(no doc)").strip()[:350])
                    # read-only sample calls (guarded; results reveal per-frame
                    # semantics — 'source TC at each record frame')
                    for call, lbl in ((lambda: tw.get_speed(), "get_speed()"),
                                      (lambda: tw.get_timing(), "get_timing()"),
                                      (lambda: tw.get_timing(1), "get_timing(1)"),
                                      (lambda: tw.get_timing(10), "get_timing(10)"),
                                      (lambda: tw.get_speed_timing(10),
                                       "get_speed_timing(10)")):
                        grab("tw.%s" % lbl, call)
                    try:
                        p = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                                         "tw_setup_dump")
                        tw.save_setup(p)
                        found = [f for f in os.listdir(os.path.dirname(p))
                                 if f.startswith("tw_setup_dump")]
                        L.append("tw.save_setup -> %s" % (found or "(no file)"))
                        for f in found:
                            fp = os.path.join(os.path.dirname(p), f)
                            with open(fp, encoding="utf-8", errors="replace") as fh:
                                head_txt = fh.read(1200)
                            L.append("-- %s head --\n%s" % (f, head_txt))
                    except Exception as e:
                        L.append("tw.save_setup: <err %s>" % e)
                    # razor-sampling feasibility rides on per-piece envelopes:
                    # manual pre-test = razor a TW seg copy, probe each half
                    L.append("(razor-sampling pre-test: cut a COPY of a TW seg "
                             "by hand, probe each half — different source "
                             "ranges per piece = curve sampling works)")
                else:
                    L.append("(no TW on this segment — select a retimed one "
                             "for the curve dump)")
            except Exception as e:
                L.append("TW dump: <err %s>" % e)
        try:
            grab("PresetType values (sequence/EDL/XML export hunt)",
                 lambda: sorted(n for n in dir(flame.PyExporter.PresetType)
                                if not n.startswith("_")))
        except Exception as e:
            L.append("PresetType dump: <err %s>" % e)
        gap("Source Segment Connections (create/scope)", "source_connection",
            "source_segment", "sourceconnect")
        gap("ANY connection API", "connect")
        gap("Duplicate Connected Segment", "duplicate")
        gap("Create Sources Sequence (native op)", "sources_seq", "conform")
        gap("Unlink / Relink media", "unlink", "relink", "link")
        gap("Sync Segments on Connection (media)", "sync")
        gap("Extract / Lift / ripple delete", "extract", "lift", "ripple")
        gap("Slip / Slide / trim segment", "slip", "slide", "trim")
        gap("Version reorder / move (below live)", "move", "reorder", "swap")
        gap("Timewarp curve read (TW-safe ranges)", "timewarp", "animation",
            "channel", "keyframe")
        gap("Openclip version SET / bump", "select_version", "set_version",
            "update_source", "refresh")
        gap("Segment razor / split", "razor", "split", "cut")
        gap("Match / match-out", "match")
        gap("Hidden / primary track (publish mech)", "hidden", "primary")
        gap("Marks (in/out)", "mark")
        gap("Harmonise groups / storyboard", "harmoni", "storyboard", "group")

        head("openclip discovery")
        grab("openclips found in current scope", lambda: len(
            iter_openclips(sequences_for_scope(self.scope.currentText()))))

        head("prefs (Conform Sources Merge)")
        grab("project attrs conform/merge/pref", lambda: [
            n for n in dir(flame.projects.current_project)
            if any(k in n.lower() for k in ("conform", "merge", "pref"))])

        out = "\n".join(L)
        if load_settings().get("probe_anonymize", True):
            # media-panel clips and openclips are NOT scan rows, so their
            # names would slip past an anonymizer fed scan rows only
            extra = []
            for o in (self._oc or []):
                extra.append({"name": o.get("name")})
            try:
                for e in (_get(flame.media_panel, "selected_entries") or []):
                    extra.append({"name": _clean_name(e)})
            except Exception:
                pass
            out, n_red = anonymize_probe(out, list(self._inv) + extra)
            self._say("Probe anonymized: %d client identifier(s) replaced "
                      "(set probe_anonymize to false in "
                      "~/flame/flame_ccm_settings.json to turn this off)." % n_red)
        # next to the script first, the home folder as the fallback
        candidates = [os.path.join(os.path.dirname(os.path.abspath(__file__)),
                                   "flame_ccm_probe.txt"),
                      os.path.join(os.path.expanduser("~"), "flame_ccm_probe.txt")]
        for path in candidates:
            try:
                with open(path, "w", encoding="utf-8") as f:
                    f.write(out + "\n")
                self._say("Probe written to %s" % path)
                self._say("   → names and paths are anonymized, but it lists "
                          "project and storage ids: review it before sharing.")
                break
            except Exception as e:
                self._say("Probe write to %s failed: %s" % (path, e))
        # echo what was WRITTEN — the anonymized text. Echoing the raw lines
        # would put client names and media paths in the console, and a console
        # paste would leak them even if the file is clean.
        for ln in out.split("\n"):
            self._say(ln)

    # ---------------------------------------------------------------- Settings tab
    def _build_settings_tab(self):
        w = QtWidgets.QWidget()
        form = QtWidgets.QFormLayout(w)
        s = load_settings()

        self.set_hub = QtWidgets.QLineEdit(s.get("sources_seq_name", "Conform_Hub"))
        self.set_hub.setToolTip("Name of the CONFORM HUB — the sources/shots sequence everything is\nconformed and published from. Flame's native \"Sources Sequence\" name is\nalways recognised too, so existing projects keep working.")
        form.addRow("Conform Hub name:", self.set_hub)

        self.set_scope = QtWidgets.QComboBox()
        self.set_scope.addItems(SCOPES)
        self.set_scope.setCurrentText(s.get("scope", "Current Reel"))
        form.addRow("Default scope:", self.set_scope)

        self.set_handle = QtWidgets.QSpinBox()
        self.set_handle.setRange(0, 240)
        self.set_handle.setValue(int(s.get("handle_frames", 8) or 0))
        self.set_handle.setToolTip("Frames padded onto each minimal covering piece. CONFIRM the real number for this facility.")
        form.addRow("Handle frames:", self.set_handle)

        self.set_gap = QtWidgets.QSpinBox()
        self.set_gap.setRange(0, 240)
        self.set_gap.setValue(int(s.get("merge_gap_frames", 0) or 0))
        self.set_gap.setToolTip("Consumed ranges within this many frames of each other merge into one piece.")
        form.addRow("Merge gap frames:", self.set_gap)

        self.set_cam = QtWidgets.QComboBox()
        self.set_cam.addItems(list(CAMERA_STRATEGIES))
        if s.get("camera_token") in CAMERA_STRATEGIES:
            self.set_cam.setCurrentText(s["camera_token"])
        self.set_cam.setToolTip("How the camera token is extracted for Tier-2 identity.")
        form.addRow("Camera token:", self.set_cam)

        self.set_match = QtWidgets.QSpinBox()
        self.set_match.setRange(1, 64)
        self.set_match.setValue(int(s.get("match_chars", 10) or 10))
        self.set_match.setToolTip("Tier-2 compares the first N characters of the camera token.")
        form.addRow("Camera match chars:", self.set_match)

        self.set_refspan = QtWidgets.QSpinBox()
        self.set_refspan.setRange(10, 100)
        self.set_refspan.setSuffix(" %")
        self.set_refspan.setValue(int(s.get("ref_span_pct", 60) or 60))
        self.set_refspan.setToolTip(
            "A segment spanning at least this much of its sequence is auto-"
            "classified as a reference picture (workflows differ — tune freely).")
        form.addRow("Ref span threshold:", self.set_refspan)

        self.set_reftokens = QtWidgets.QLineEdit(s.get("ref_name_tokens", ""))
        self.set_reftokens.setPlaceholderText("ref, reference, offline   (comma-separated)")
        self.set_reftokens.setToolTip(
            "Segment / source / track names containing any of these tokens are "
            "classified as refs regardless of length.")
        form.addRow("Ref name tokens:", self.set_reftokens)

        self.set_snapas = QtWidgets.QComboBox()
        self.set_snapas.addItems(["version", "track"])
        self.set_snapas.setCurrentText(s.get("publish_snapshot_as", "version"))
        self.set_snapas.setToolTip(
            "What holds a publish snapshot: a real VERSION (the workflow's "
            "'new version track'; Flame picks stacking) or a plain track "
            "(bottom-growing position honored).")
        form.addRow("Snapshot into:", self.set_snapas)

        self.set_pubpos = QtWidgets.QComboBox()
        self.set_pubpos.addItems(["bottom", "top"])
        self.set_pubpos.setCurrentText(s.get("publish_track_position", "bottom"))
        self.set_pubpos.setToolTip(
            "Where new Publish NN tracks go: bottom = v000 at the very bottom, "
            "later publishes stacking upward, live track on top (the default); "
            "top = append above.")
        form.addRow("Publish tracks grow:", self.set_pubpos)

        self.set_mutations = QtWidgets.QCheckBox(
            "Allow CCM to CHANGE the project (Prep Publish, Rename, Remove, "
            "Connections)")
        self.set_mutations.setChecked(bool(s.get("enable_mutations")))
        self.set_mutations.setToolTip(
            "Master safety switch. Off = CCM is strictly read-only (default). "
            "On = the Ledger's Prep Publish can write snapshot tracks. Every "
            "mutation still previews a plan and asks first.")
        form.addRow("Mutations:", self.set_mutations)

        prow = QtWidgets.QHBoxLayout()
        self.set_statedir = QtWidgets.QLineEdit(s.get("state_dir", ""))
        self.set_statedir.setPlaceholderText(_default_state_dir() + "   (default)")
        b_browse = QtWidgets.QPushButton("Browse…")
        b_browse.clicked.connect(self._browse_statedir)
        prow.addWidget(self.set_statedir, 1)
        prow.addWidget(b_browse)
        form.addRow("State folder:", self._wrap(prow))

        b_save = QtWidgets.QPushButton("Save Settings")
        b_save.setObjectName("primary")
        b_save.clicked.connect(self._save_settings)
        form.addRow("", b_save)

        note = QtWidgets.QLabel(
            "State is thin on purpose: the scan re-derives placement, version and "
            "identity every time. Only triage outcomes (dup decisions, identity "
            "overrides, sanctioned dupes) are persisted — they're what a scan "
            "can't reconstruct.")
        note.setWordWrap(True)
        note.setStyleSheet("color:#777777;")
        form.addRow("", self._tip(note))
        return w

    def _wrap(self, layout):
        c = QtWidgets.QWidget(); c.setLayout(layout); return c

    def _browse_statedir(self):
        start = self.set_statedir.text().strip() or _default_state_dir()
        d = QtWidgets.QFileDialog.getExistingDirectory(self, "Select state folder", start)
        if d:
            self.set_statedir.setText(d)

    def _save_settings(self):
        s = load_settings()
        s["sources_seq_name"] = self.set_hub.text().strip() or "Conform Hub"
        s["scope"] = self.set_scope.currentText()
        s["handle_frames"] = int(self.set_handle.value())
        s["merge_gap_frames"] = int(self.set_gap.value())
        s["camera_token"] = self.set_cam.currentText()
        s["match_chars"] = int(self.set_match.value())
        s["ref_span_pct"] = int(self.set_refspan.value())
        s["ref_name_tokens"] = self.set_reftokens.text().strip()
        s["enable_mutations"] = bool(self.set_mutations.isChecked())
        s["publish_track_position"] = self.set_pubpos.currentText()
        s["publish_snapshot_as"] = self.set_snapas.currentText()
        s["state_dir"] = self.set_statedir.text().strip()
        save_settings(s)
        if self.scope.currentText() != s["scope"]:
            # block the combo's signal so _scope_changed can't fire a second scan
            self.scope.blockSignals(True)
            self.scope.setCurrentText(s["scope"])
            self.scope.blockSignals(False)
        self._say("Settings saved. State: %s" % state_path())
        self._scan()


# --------------------------------------------------------------------- hooks

_DIALOG = None


def _open(selection):
    # NON-MODAL: dlg.exec() blocks Flame's event loop, so timeline/
    # media-panel changes would only appear after closing CCM. show() lets Flame
    # refresh live and the artist work with CCM open beside the timeline.
    global _DIALOG
    try:
        if _DIALOG is not None:
            _DIALOG.close()
    except Exception:
        pass
    try:
        if flame.get_current_tab() != "Timeline":
            flame.set_current_tab("Timeline")     # scan anchors live here
    except Exception:
        pass
    _DIALOG = ConformManagerDialog(selection if selection else [])
    _DIALOG.setWindowFlags(_DIALOG.windowFlags() | QtCore.Qt.Window)
    _DIALOG.show()
    _DIALOG.raise_()
    _DIALOG.activateWindow()


def _jump(selection):
    _open(selection)
    try:
        segs = [s for s in (selection or []) if type(s).__name__ == "PySegment"]
        if segs and _DIALOG is not None:
            _DIALOG.jump_to_segment(segs[0])
    except Exception:
        pass


def get_timeline_custom_ui_actions():
    return [{"name": "Connected Conform Manager",
             "actions": [{"name": "Open…", "execute": _open,
                          "minimumVersion": "2026.0.0"},
                         {"name": "Jump to Shot in Timelines…", "execute": _jump,
                          "minimumVersion": "2026.0.0"}]}]


def get_media_panel_custom_ui_actions():
    return [{"name": "Connected Conform Manager",
             "actions": [{"name": "Open…", "execute": _open,
                          "minimumVersion": "2026.0.0"}]}]


def get_main_menu_custom_ui_actions():
    return [{"name": "Connected Conform Manager",
             "actions": [{"name": "Open Manager…", "execute": _open}]}]
