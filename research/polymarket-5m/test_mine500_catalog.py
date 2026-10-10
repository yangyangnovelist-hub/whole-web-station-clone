"""Determinism, semantic identity, reachability and fail-closed contracts."""
import copy
import json
import math

from mine500_catalog import (FEATURE_DEFINITIONS, TEMPLATES, SUPPLEMENTARY_TEMPLATES,
                             make_candidates, make_supplementary_candidates,
                             rule_fingerprint, signal)


CANDIDATES = make_candidates()
SUPPLEMENTARY = make_supplementary_candidates()


def witness(c):
    """Synthetic Up-triggering input for every template, never market evidence."""
    f = {"tau_s": (c["params"]["tau_min_s"] + c["params"]["tau_max_s"]) / 2,
         "r_5s": 5., "r_15s": 7., "r_30s": 9., "r_60s": 12.,
         "flow_15s": .8, "flow_60s": .8, "rv_15s": 2., "rv_60s": 5.,
         "pm_r_5s": .08, "pm_r_15s": .12, "mid_up": .4,
         "fair_up": .8, "imbalance_up": .8,
         "tail_gap_bps": 5., "tail_sigma_bps": 1., "twap_known_fraction": .5}
    key = c["template"]
    if key.startswith("reversal_"):
        f["r_" + key.removeprefix("reversal_")] *= -1
    if key == "acceleration_5s":
        f.update(r_5s=9., r_15s=10.)
    if key == "acceleration_15s":
        f.update(r_15s=10., r_60s=12.)
    if key == "pullback_5_60":
        f["r_5s"] = -2.
    if key == "pullback_15_60":
        f["r_15s"] = -2.
    if key.startswith("flow_fade_"):
        f["flow_" + key.rsplit("_", 1)[-1] + "s"] = -.8
    if key == "flow_acceleration":
        f.update(flow_15s=.9, flow_60s=.2)
    if key == "flow_exhaustion":
        f.update(flow_15s=-.1, flow_60s=-.8)
    if key == "flow_flip":
        f["flow_60s"] = -.8
    if key.startswith("flow_absorption_"):
        f["flow_" + key.rsplit("_", 1)[-1] + "s"] = -.8
    if key.startswith("flow_flat_price_"):
        f["r_" + key.rsplit("_", 1)[-1] + "s"] = 0.
    if key.startswith("trend_exhaustion_"):
        f["r_" + key.rsplit("_", 1)[-1] + "s"] = -5.
    if key.startswith("vol_expansion_"):
        f.update(rv_15s=5., rv_60s=6.)
    if key.startswith("vol_compression_"):
        f.update(rv_15s=1., rv_60s=10., r_15s=2.)
    if key == "vol_expansion_fade":
        f["r_15s"] = -4.
    if key.startswith("pm_fade_"):
        f["pm_r_" + key.rsplit("_", 1)[-1] + "s"] *= -1
    if key == "pm_external_divergence":
        f["pm_r_15s"] = -.1
    if key == "external_lead":
        f["pm_r_5s"] = 0.
    if key == "book_against_pm":
        f["pm_r_5s"] = -.1
    if key.startswith("twap_tail_"):
        f["twap_known_fraction"] = 1 - f["tau_s"] / 60.
    return f


def mirrored(f):
    out = dict(f)
    for name in ("r_5s", "r_15s", "r_30s", "r_60s", "flow_15s", "flow_60s",
                 "pm_r_5s", "pm_r_15s", "imbalance_up", "tail_gap_bps"):
        out[name] *= -1
    for name in ("mid_up", "fair_up"):
        out[name] = 1 - out[name]
    return out


def test_exactly_500_unique_executable_specs_and_stable_serialization():
    assert len(CANDIDATES) == 500
    assert len(TEMPLATES) == 50
    assert len({c["family"] for c in CANDIDATES}) == 9
    assert len({c["id"] for c in CANDIDATES}) == 500
    assert len({c["rule_fingerprint"] for c in CANDIDATES}) == 500
    assert make_candidates() == CANDIDATES
    assert json.loads(json.dumps(CANDIDATES)) == CANDIDATES
    for c in CANDIDATES:
        assert c["research_status"] == "registered_not_backtested"
        assert not c["independent_mechanism_claim"]
        assert all(f in FEATURE_DEFINITIONS for f in c["required_features"])
        assert c["rule_fingerprint"] == rule_fingerprint(c)
        changed_name = dict(c, name="only presentation changed", id="other")
        assert rule_fingerprint(changed_name) == c["rule_fingerprint"]


def test_every_candidate_reachable_and_directionally_symmetric():
    for c in CANDIDATES:
        f = witness(c)
        assert signal(c, f) == "Up", c["id"]
        assert signal(c, mirrored(f)) == "Down", c["id"]


def test_all_missing_and_nonfinite_required_features_fail_closed():
    for c in CANDIDATES:
        assert signal(c, {}) is None
        for field in c["required_features"]:
            f = witness(c)
            del f[field]
            assert signal(c, f) is None, (c["id"], field)
            for bad in (None, True, "0.5", math.nan, math.inf, -math.inf):
                f[field] = bad
                assert signal(c, f) is None, (c["id"], field, bad)


def test_signal_does_not_trust_edited_required_features_list():
    for c in CANDIDATES:
        changed = dict(c, required_features=[])
        assert signal(changed, {"tau_s": 20.}) is None


def test_windows_do_not_overlap_and_tail_variants_are_late_only():
    for template in TEMPLATES:
        rows = [c for c in CANDIDATES if c["template"] == template["template"]]
        assert len(rows) == 10
        for index in range(0, 8, 2):
            previous, following = rows[index], rows[index + 2]
            edge = previous["params"]["tau_max_s"]
            assert following["params"]["tau_min_s"] == edge
            f = witness(previous)
            f["tau_s"] = edge
            assert signal(previous, f) is None
            assert signal(following, f) == "Up"
        last = rows[-1]
        f = witness(last)
        f["tau_s"] = last["params"]["tau_max_s"]
        assert signal(last, f) == "Up"
        f["tau_s"] += .001
        assert signal(last, f) is None
        if template["tail"]:
            assert max(c["params"]["tau_max_s"] for c in rows) <= 60


def test_two_strength_levels_change_executable_threshold():
    # Every pair has exactly the same rule/window but a genuine threshold
    # difference. This does not claim independence of the resulting PnLs.
    for first, second in zip(CANDIDATES[::2], CANDIDATES[1::2]):
        assert first["template"] == second["template"]
        assert first["params"]["tau_min_s"] == second["params"]["tau_min_s"]
        assert second["params"]["threshold"] == 1.5 * first["params"]["threshold"]


def test_each_strength_pair_has_a_behavioral_distinguishing_witness():
    # Not merely distinct IDs or fingerprints: a score between the two
    # thresholds actually causes an order only for the lower-threshold rule.
    for first, second in zip(CANDIDATES[::2], CANDIDATES[1::2]):
        f = witness(first)
        key = first["template"]
        target = (first["params"]["threshold"] + second["params"]["threshold"]) / 2
        if key.startswith(("momentum_", "reversal_")):
            f["r_" + key.split("_", 1)[1]] = target if key.startswith("momentum_") else -target
        elif key == "acceleration_5s":
            f.update(r_5s=target, r_15s=target)
        elif key == "acceleration_15s":
            f.update(r_15s=target, r_60s=target)
        elif key.startswith("nested_"):
            f["r_5s" if key == "nested_5_15" else "r_15s"] = target
        elif key.startswith("pullback_"):
            f["r_60s"] = target
            f["r_5s" if key == "pullback_5_60" else "r_15s"] = -target / 2
        elif key.startswith(("flow_follow_", "flow_fade_")):
            f["flow_" + key.rsplit("_", 1)[-1] + "s"] = -target if key.startswith("flow_fade_") else target
        elif key == "flow_persistence":
            f.update(flow_15s=target, flow_60s=target)
        elif key == "flow_acceleration":
            f.update(flow_15s=target + .1, flow_60s=.1)
        elif key == "flow_exhaustion":
            f.update(flow_15s=-target / 4, flow_60s=-target)
        elif key == "flow_flip":
            f.update(flow_15s=target, flow_60s=-target)
        elif key.startswith(("flow_price_confirm_", "trend_exhaustion_")):
            f["r_" + key.rsplit("_", 1)[-1] + "s"] = -target if key.startswith("trend_exhaustion_") else target
        elif key.startswith(("flow_absorption_", "flow_flat_price_")):
            f["flow_" + key.rsplit("_", 1)[-1] + "s"] = -target if key.startswith("flow_absorption_") else target
        elif key.startswith("vol_scaled_"):
            w = key.rsplit("_", 1)[-1]
            f[f"r_{w}s"], f[f"rv_{w}s"] = target, 1.
        elif key == "vol_compression_flow":
            f["flow_15s"] = target
        elif key.startswith(("vol_expansion_", "vol_compression_")):
            f["r_15s"] = -target if key == "vol_expansion_fade" else target
        elif key.startswith(("pm_follow_", "pm_fade_")):
            f["pm_r_" + key.rsplit("_", 1)[-1] + "s"] = -target if key.startswith("pm_fade_") else target
        elif key.startswith("fair_gap"):
            f.update(mid_up=.4, fair_up=.4 + target)
        elif key == "pm_external_divergence":
            f["pm_r_15s"] = -target
        elif key == "external_lead":
            f["r_5s"] = target
        elif key.startswith("book_"):
            f["imbalance_up"] = target
        elif key == "twap_tail_pm_gap":
            f.update(tail_gap_bps=0., mid_up=.5 - target)
        elif key.startswith("twap_tail_"):
            f.update(tail_gap_bps=target, tail_sigma_bps=1.)
        else:
            raise AssertionError(key)
        assert signal(first, f) == "Up", first["id"]
        assert signal(second, f) is None, second["id"]


def test_boundary_and_direction_examples():
    def c(key):
        return next(x for x in CANDIDATES if x["template"] == key)

    momentum = c("momentum_5s")
    assert signal(momentum, {"tau_s": 20., "r_5s": .5}) == "Up"
    assert signal(momentum, {"tau_s": 20., "r_5s": -.5}) == "Down"
    assert signal(momentum, {"tau_s": 20., "r_5s": .499999}) is None
    assert signal(c("acceleration_5s"), {"tau_s": 20., "r_5s": 2., "r_15s": 8.}) == "Down"
    assert signal(c("flow_exhaustion"), {"tau_s": 20., "flow_15s": .1, "flow_60s": .6}) == "Down"
    assert signal(c("flow_exhaustion"), {"tau_s": 20., "flow_15s": .4, "flow_60s": .6}) is None
    assert signal(c("fair_gap"), {"tau_s": 20., "fair_up": .4, "mid_up": .6}) == "Down"
    assert signal(c("twap_tail_pm_gap"), {"tau_s": 3., "tail_gap_bps": 0., "tail_sigma_bps": 1., "twap_known_fraction": .9, "mid_up": .6}) == "Down"


def test_out_of_domain_features_and_zero_denominators_fail_closed():
    invalid = {"tau_s": [-1., 0., 301.], "flow_15s": [-1.01, 1.01],
               "flow_60s": [-1.01, 1.01], "rv_15s": [-.01], "rv_60s": [-.01],
               "pm_r_5s": [-1.01, 1.01], "pm_r_15s": [-1.01, 1.01],
               "mid_up": [0., 1., 1.01], "fair_up": [-.01, 1.01],
               "imbalance_up": [-1.01, 1.01], "tail_sigma_bps": [0., -1.],
               "twap_known_fraction": [-.01, 1., 1.01]}
    for c in CANDIDATES:
        for name in c["required_features"]:
            for value in invalid.get(name, []):
                f = witness(c)
                f[name] = value
                assert signal(c, f) is None
        if c["template"].startswith("vol_scaled_"):
            f = witness(c)
            f["rv_" + c["template"].rsplit("_", 1)[-1] + "s"] = 0.
            assert signal(c, f) is None


def test_malformed_candidate_fails_closed_and_no_input_mutation():
    assert signal({}, {}) is None
    assert signal({"template": "unimplemented_rl_model"}, {}) is None
    c = copy.deepcopy(CANDIDATES[0])
    f = witness(c)
    old_c, old_f = copy.deepcopy(c), dict(f)
    assert signal(c, f) == "Up"
    assert c == old_c and f == old_f
    for bad in (0., -1., math.nan, math.inf, "1", True):
        c["params"]["threshold"] = bad
        assert signal(c, f) is None


def supplement_witness(c, *, target=None):
    f = witness(c)
    key = c["template"]
    if key.startswith(("prior_momentum_", "prior_reversal_")):
        x = 5. if target is None else target
        x = -x if key.startswith("prior_reversal_") else x
        if key.endswith("10s"):
            f.update(r_5s=0., r_15s=x)
        else:
            f.update(r_15s=0., r_60s=x)
    elif key == "acceleration_fade_5s":
        x = 5. if target is None else target
        f.update(r_5s=-x, r_15s=-x)
    elif key == "acceleration_fade_15s":
        x = 5. if target is None else target
        f.update(r_15s=-x, r_60s=-x)
    elif key == "triple_consensus":
        f["r_5s"] = 5. if target is None else target
    elif key == "recent_turn_5_15":
        f.update(r_5s=5. if target is None else target, r_15s=0.)
    elif key == "return_curvature":
        x = 5. if target is None else target
        f.update(r_5s=2 * x / 3, r_15s=0., r_60s=0.)
    elif key.startswith(("pm_book_confirm_", "pm_book_fade_")):
        x = .1 if target is None else target
        f["pm_r_" + key.rsplit("_", 1)[-1] + "s"] = -x if key.startswith("pm_book_fade_") else x
    elif key.startswith("external_book_confirm_"):
        f["r_" + key.rsplit("_", 1)[-1] + "s"] = 5. if target is None else target
    elif key == "book_veto_fade":
        f["imbalance_up"] = -.8 if target is None else -target
    elif key in ("pm_acceleration_follow", "pm_acceleration_fade"):
        x = .1 if target is None else target
        x = -x if key.endswith("fade") else x
        f.update(pm_r_5s=x, pm_r_15s=x)
    elif key == "pm_nested_5_15":
        x = .1 if target is None else target
        f.update(pm_r_5s=x, pm_r_15s=x)
    elif key == "pm_pullback_5_15":
        x = .1 if target is None else target
        f.update(pm_r_5s=-x/2, pm_r_15s=x)
    elif key == "vol_pm_expansion":
        f.update(pm_r_5s=.1 if target is None else target, rv_15s=5., rv_60s=6.)
    elif key == "vol_pm_compression_fade":
        f.update(pm_r_5s=-.1 if target is None else -target, rv_15s=1., rv_60s=10.)
    else:
        raise AssertionError(key)
    return f


def test_supplement_is_220_unique_specs_preserving_original_500():
    assert len(SUPPLEMENTARY_TEMPLATES) == 22
    assert len(SUPPLEMENTARY) == 220
    assert [c["id"] for c in SUPPLEMENTARY] == [f"M500-{n:03d}" for n in range(501, 721)]
    combined = CANDIDATES + SUPPLEMENTARY
    assert len({c["rule_fingerprint"] for c in combined}) == 720
    assert len({c["id"] for c in combined}) == 720
    assert make_candidates() == CANDIDATES
    assert make_supplementary_candidates() == SUPPLEMENTARY
    unsupported = {"flow_15s", "flow_60s", "fair_up", "tail_gap_bps", "tail_sigma_bps", "twap_known_fraction"}
    assert not any(set(c["required_features"]) & unsupported for c in SUPPLEMENTARY)
    assert sum(not (set(c["required_features"]) & unsupported) for c in combined) == 500


def test_all_220_supplement_rules_reachable_symmetric_and_fail_closed():
    for c in SUPPLEMENTARY:
        f = supplement_witness(c)
        assert signal(c, f) == "Up", c["id"]
        assert signal(c, mirrored(f)) == "Down", c["id"]
        assert signal(c, {}) is None
        for field in c["required_features"]:
            missing = dict(f)
            del missing[field]
            assert signal(c, missing) is None
            for bad in (None, True, "0.5", math.nan, math.inf, -math.inf):
                assert signal(c, dict(f, **{field: bad})) is None


def test_each_supplementary_strength_pair_behaves_differently():
    for low, high in zip(SUPPLEMENTARY[::2], SUPPLEMENTARY[1::2]):
        target = (low["params"]["threshold"] + high["params"]["threshold"]) / 2
        f = supplement_witness(low, target=target)
        assert signal(low, f) == "Up", low["id"]
        assert signal(high, f) is None, high["id"]
        f["tau_s"] = low["params"]["tau_min_s"] - .001
        assert signal(low, f) is None


def test_supplementary_contrast_rules_have_intended_direction():
    by = {c["template"]: c for c in SUPPLEMENTARY[::10]}
    f = supplement_witness(by["prior_momentum_10s"])
    assert signal(by["prior_momentum_10s"], f) == "Up"
    assert signal(by["prior_reversal_10s"], f) == "Down"
    f = supplement_witness(by["pm_acceleration_follow"])
    assert signal(by["pm_acceleration_follow"], f) == "Up"
    assert signal(by["pm_acceleration_fade"], f) == "Down"
    f.update(pm_r_5s=.04, pm_r_15s=-.1)
    assert signal(by["pm_nested_5_15"], f) is None
    assert signal(by["pm_pullback_5_15"], f) == "Down"
