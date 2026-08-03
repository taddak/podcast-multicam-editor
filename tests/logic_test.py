"""Synthetic tests for the decision logic of Podcast Multicam Editor."""
import random

import os
SCRIPT = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                      "Podcast Multicam Editor.py")
src = open(SCRIPT, encoding="utf-8").read().replace(
    "\ntry:\n    main()", "\ntry:\n    pass  # main()")
# do not connect to Resolve - pure logic tests
src = src.replace("RESOLVE, FUSION, BMD = get_resolve_env()",
                  "RESOLVE = FUSION = BMD = None")
src = src.replace("UI = FUSION.UIManager", "UI = None")
src = src.replace("DISP = BMD.UIDispatcher(UI)", "DISP = None")
ns = {"__name__": "pme_test"}
exec(compile(src, SCRIPT, "exec"), ns)

HOP = ns["HOP_S"]
random.seed(42)

def hops(sec):
    return int(sec / HOP)

def synth_levels(total_s, segments, n_spk=2):
    """segments: list of (start_s, end_s, speaking_set).
    Speaking mic: -18 dB, bleed on other mics: -30 dB, floor: -58 dB."""
    n = hops(total_s)
    levels = [[-58.0 + random.uniform(-2, 2) for _ in range(n)]
              for _ in range(n_spk)]
    for a, b, who in segments:
        for h in range(hops(a), min(hops(b), n)):
            for s in range(n_spk):
                if s in who:
                    levels[s][h] = -18.0 + random.uniform(-4, 4)
                elif who:
                    levels[s][h] = max(levels[s][h],
                                       -30.0 + random.uniform(-4, 4))
    return levels

def describe(shots, cams_by_idx):
    return [(round(a * HOP, 1), round(b * HOP, 1), cams_by_idx[c["trackIndex"]])
            for a, b, c in shots]

def assert_no_short_wides(d, min_s=3.0):
    shorts = [x for x in d if x[2] == "wide" and (x[1] - x[0]) < min_s - 0.15]
    assert not shorts, "too short wide shots: %s" % shorts

CAM1 = {"trackIndex": 1, "speakers": frozenset({0})}
CAM2 = {"trackIndex": 2, "speakers": frozenset({1})}
WIDE = {"trackIndex": 3, "speakers": frozenset({0, 1})}
NAMES = {1: "cam1(A)", 2: "cam2(B)", 3: "wide"}

print("=== Test 1: conversation with bleed, wide available ===")
segs = [(0, 30, {0}), (30, 60, {1}), (60, 70, {0, 1}), (75, 120, {0})]
levels = synth_levels(120, segs)
activity, dominant, stats = ns["compute_activity"](levels, "Medium", 0.3)
for s in stats:
    print(" ", s)
pctA = 100 * sum(activity[0]) / len(activity[0])
pctB = 100 * sum(activity[1]) / len(activity[1])
assert 65 <= pctA <= 85, "A activity out of range: %.0f%%" % pctA  # ~71%
assert 30 <= pctB <= 42, "B activity out of range: %.0f%%" % pctB  # ~33%
shots = ns["plan_shots"](activity, dominant, [CAM1, CAM2, WIDE], WIDE,
                         len(activity[0]), 2.0, 30.0)
d = describe(shots, NAMES)
print("  shots:", d)
assert d[0][2] == "cam1(A)" and abs(d[0][1] - 30) < 1.5, d[0]
assert d[1][2] == "cam2(B)", d[1]
# overlap 60-70: first a reaction cut to cam1 (~60), wide only after ~4 s
assert any(x[2] == "cam1(A)" and abs(x[0] - 60) < 1.5 for x in d), d
wide_shots = [x for x in d if x[2] == "wide"]
assert any(62.5 <= x[0] <= 66.5 for x in wide_shots), wide_shots
# monologue A 75-120 must come back to cam1
tail = [x for x in d if x[0] >= 69]
assert any(x[2] == "cam1(A)" for x in tail), tail
assert len(d) >= 5, "too few cuts: %s" % d
assert_no_short_wides(d)
print("  OK")

print("=== Test 2: no wide -> overlap goes to the dominant speaker ===")
shots = ns["plan_shots"](activity, dominant, [CAM1, CAM2], None,
                         len(activity[0]), 2.0, None)
d = describe(shots, NAMES)
print("  shots:", d)
assert all(x[2] in ("cam1(A)", "cam2(B)") for x in d)
assert len(d) >= 3, d
assert d[0][2] == "cam1(A)" and d[1][2] == "cam2(B)", d[:2]
print("  OK")

print("=== Test 3: talkative speaker (85 % of time) - old bug ===")
segs = [(0, 85, {0}), (88, 100, {1})]
levels = synth_levels(100, segs)
activity, dominant, stats = ns["compute_activity"](levels, "Medium", 0.3)
for s in stats:
    print(" ", s)
pctA = 100 * sum(activity[0]) / len(activity[0])
assert pctA > 75, "Otsu failed, A only %.0f%%" % pctA
shots = ns["plan_shots"](activity, dominant, [CAM1, CAM2, WIDE], WIDE,
                         len(activity[0]), 2.0, 30.0)
d = describe(shots, NAMES)
print("  shots:", d)
assert any(x[2] == "cam2(B)" and abs(x[0] - 88) < 1.5 for x in d), d
assert_no_short_wides(d)
print("  OK")

print("=== Test 4: bleed-only track gets dropped ===")
levels = synth_levels(60, [(0, 25, {0}), (30, 60, {0})])
activity, dominant, stats = ns["compute_activity"](levels, "Medium", 0.3)
for s in stats:
    print(" ", s)
pctA = 100 * sum(activity[0]) / len(activity[0])
assert pctA > 80, "A too inactive: %.0f%%" % pctA
assert sum(activity[1]) == 0, "B (bleed) should have been dropped"
print("  OK")

print("=== Test 5: fast dialogue - cuts follow the speakers ===")
segs = []
t = 0
who = 0
while t < 120:
    dur = random.choice([4, 5, 6, 8, 10])
    segs.append((t, min(t + dur, 120), {who}))
    t += dur
    who = 1 - who
levels = synth_levels(120, segs)
activity, dominant, stats = ns["compute_activity"](levels, "Medium", 0.3)
for s in stats:
    print(" ", s)
shots = ns["plan_shots"](activity, dominant, [CAM1, CAM2, WIDE], WIDE,
                         len(activity[0]), 2.0, 30.0)
d = describe(shots, NAMES)
print("  shots ({}):".format(len(d)), d)
n_turns = len(segs)
assert n_turns * 0.6 <= len(d) <= n_turns * 1.4, \
    "expected ~%d shots, got %d" % (n_turns, len(d))
bounds = [s[0] for s in segs[1:]]
matched = sum(1 for a, _, _ in d[1:] if any(abs(a - b) < 1.0 for b in bounds))
assert matched >= len(d[1:]) * 0.7, \
    "only %d/%d cuts on turn boundaries" % (matched, len(d[1:]))
print("  OK ({} turns -> {} shots, {}/{} cuts exact)".format(
    n_turns, len(d), matched, len(d[1:])))

print("=== Test 6: early cut into the pause ===")
# A stops at 30 s, 3 s pause, B starts at 33 s -> cut should be ~31.8 s
levels = synth_levels(60, [(0, 30, {0}), (33, 60, {1})])
activity, dominant, stats = ns["compute_activity"](levels, "Medium", 0.3)
shots = ns["plan_shots"](activity, dominant, [CAM1, CAM2], None,
                         len(activity[0]), 2.0, None)
d = describe(shots, NAMES)
print("  shots:", d)
assert len(d) == 2 and d[0][2] == "cam1(A)" and d[1][2] == "cam2(B)", d
cut = d[1][0]
assert 30.2 <= cut <= 32.5, "cut should be inside the pause, is at %.1f" % cut
assert cut <= 32.2, "cut is not ahead of B's speech (%.1f s)" % cut
print("  OK (B starts ~33 s, camera on him at %.1f s)" % cut)

levels = synth_levels(60, [(0, 30, {0}), (30, 60, {1})])
activity, dominant, stats = ns["compute_activity"](levels, "Medium", 0.3)
shots = ns["plan_shots"](activity, dominant, [CAM1, CAM2], None,
                         len(activity[0]), 2.0, None)
d = describe(shots, NAMES)
print("  no pause:", d)
assert abs(d[1][0] - 30) < 0.8, "without a pause cut should sit at 30: %s" % d
print("  OK")

print("=== Test 7: 6 speakers, 6 cameras + wide, 90 minutes ===")
import time

N_SPK = 6
TOTAL_S = 90 * 60

segs = []
t = 0
prev = -1
while t < TOTAL_S:
    who = random.choice([s for s in range(N_SPK) if s != prev])
    dur = random.uniform(4, 20)
    if random.random() < 0.1:  # occasional crosstalk
        other = random.choice([s for s in range(N_SPK) if s != who])
        segs.append((t, min(t + dur, TOTAL_S), {who, other}))
    else:
        segs.append((t, min(t + dur, TOTAL_S), {who}))
    if random.random() < 0.3:  # occasional pause
        t += dur + random.uniform(1, 4)
    else:
        t += dur
    prev = who

cams = [{"trackIndex": i + 1, "speakers": frozenset({i})} for i in range(N_SPK)]
wide6 = {"trackIndex": 7, "speakers": frozenset(range(N_SPK))}
cams.append(wide6)
names6 = {i + 1: "cam%d" % (i + 1) for i in range(N_SPK)}
names6[7] = "wide"

t0 = time.time()
levels = synth_levels(TOTAL_S, segs, N_SPK)
t0 = time.time()
activity, dominant, stats = ns["compute_activity"](levels, "Medium", 0.3)
t_act = time.time() - t0
for s in stats:
    print(" ", s)
t0 = time.time()
shots = ns["plan_shots"](activity, dominant, cams, wide6,
                         len(activity[0]), 2.0, 30.0)
t_plan = time.time() - t0

used = {c["trackIndex"] for _, _, c in shots}
durs = [(b - a) * HOP for a, b, c in shots]
print("  {} turns -> {} shots in {:.1f}s (activity {:.1f}s, plan {:.1f}s)"
      .format(len(segs), len(shots), t_act + t_plan, t_act, t_plan))
print("  cameras used: {}, min shot {:.1f}s, avg {:.1f}s".format(
    sorted(names6[i] for i in used), min(durs), sum(durs) / len(durs)))
assert len(used) == 7, "not all cameras used: %s" % used
assert len(shots) > len(segs) * 0.5, "too few shots"
assert min(durs) >= 1.9, "shot below min length: %.2f s" % min(durs)
assert_no_short_wides([(a * HOP, b * HOP, names6[c["trackIndex"]])
                       for a, b, c in shots])
assert (t_act + t_plan) < 90, "too slow: %.1f s" % (t_act + t_plan)
for s in range(N_SPK):
    pct = 100 * sum(activity[s]) / len(activity[s])
    assert 5 < pct < 40, "speaker %d: suspicious %% of time: %.0f" % (s + 1, pct)
print("  OK")

print("=== Test 8: short overlap -> reaction close-up, not wide ===")
# A talks 0-40, B talks over him 20-22 -> cut to B's close-up, no wide
levels = synth_levels(40, [(0, 20, {0}), (20, 22, {0, 1}), (22, 40, {0})])
activity, dominant, stats = ns["compute_activity"](levels, "Medium", 0.3)
shots = ns["plan_shots"](activity, dominant, [CAM1, CAM2, WIDE], WIDE,
                         len(activity[0]), 2.0, None)
d = describe(shots, NAMES)
print("  shots:", d)
assert not any(x[2] == "wide" for x in d), "wide used for a 2s overlap: %s" % d
assert any(x[2] == "cam2(B)" and abs(x[0] - 20) < 1.5 for x in d), \
    "no reaction cut to cam2: %s" % d
assert d[-1][2] == "cam1(A)", d[-1]
print("  OK (reaction cut to B's close-up instead of the wide)")

print("=== Test 9: quiet mic with loud bleed (Investory case) ===")
# Sp1 speech only -31 dB, Sp2 bleed into Sp1's mic at -44 dB: the
# threshold re-split must put Sp1's threshold above the bleed
n = hops(120)
lv1 = []
lv2 = []
for h in range(n):
    t = h * HOP
    sp1_talks = (t % 40) < 8            # Sp1 talks 8 s out of every 40
    sp2_talks = not sp1_talks
    if sp1_talks:
        lv1.append(-31.0 + random.uniform(-3, 3))
        lv2.append(-38.0 + random.uniform(-3, 3))   # bleed into Sp2
    else:
        lv1.append(-44.0 + random.uniform(-3, 3))   # loud bleed of Sp2
        lv2.append(-23.0 + random.uniform(-3, 3))
levels = [lv1, lv2]
activity, dominant, stats = ns["compute_activity"](levels, "Medium", 0.3)
for s in stats:
    print(" ", s)
pct1 = 100 * sum(activity[0]) / n
both = 100 * sum(1 for h in range(n)
                 if activity[0][h] and activity[1][h]) / n
print("  Sp1 active {:.0f} % (truth 20 %), overlap {:.1f} %".format(
    pct1, both))
assert pct1 < 32, "Sp1 activity inflated by bleed: %.0f%%" % pct1
assert both < 5, "phantom overlap: %.1f%%" % both
print("  OK")

print("\nALL TESTS PASSED")
