"""A deterministic 500-candidate, single-leg research registry.

These are 50 rule templates in 9 overlapping economic families, each with
10 predeclared parameter variants. They are NOT 500 independent mechanisms,
trained models, completed backtests, or profitable strategies. No PnL or
settlement outcome is consumed here. All variants count as research trials.

The caller must construct every feature from messages received by the
decision time. Signal generation neither grants execution nor checks future
book health. An executor must freeze the decision limit, account for all
attempts, delay, fees, partial fills, failures and shared available cash.
Maker and paired-leg inventory strategies require separate execution engines.

An additional 220 predeclared candidates are available through
``make_supplementary_candidates`` after discovering that the published tape
lacks flow/model features. Their design uses data-schema availability only,
never PnL. They preserve all original 500 specifications. All 720 registrations
remain part of the research-trial universe; blocked rules are not backtests.
"""
from __future__ import annotations

import hashlib
import json
import math
from numbers import Real


VERSION = "mine500-v1"
FEATURE_DEFINITIONS = {
    "tau_s": "Seconds from decision receipt-clock time until official market end; 0 < tau <= 300.",
    "r_5s": "10000*log(external_price_now/external_price_5s_ago), using as-of local receipts; bps.",
    "r_15s": "10000*log(external_price_now/external_price_15s_ago), using as-of local receipts; bps.",
    "r_30s": "10000*log(external_price_now/external_price_30s_ago), using as-of local receipts; bps.",
    "r_60s": "10000*log(external_price_now/external_price_60s_ago), using as-of local receipts; bps.",
    "flow_15s": "(aggressive_buy_quote_volume-aggressive_sell_quote_volume)/total_quote_volume over trailing 15 receipt-clock seconds, external venue, [-1,1]; missing if total volume zero or trade coverage incomplete.",
    "flow_60s": "Same signed external aggressive quote-volume imbalance over trailing 60 receipt-clock seconds, [-1,1]; missing if total volume zero or coverage incomplete.",
    "rv_15s": "sqrt(sum of squared 1-second external log returns in bps over trailing 15 seconds)); requires complete causal grid; >=0.",
    "rv_60s": "sqrt(sum of squared 1-second external log returns in bps over trailing 60 seconds)); requires complete causal grid; >=0.",
    "pm_r_5s": "Up-token midpoint now minus midpoint 5 seconds ago, probability units (0.01 = 1 cent), same market and receipt clock.",
    "pm_r_15s": "Up-token midpoint now minus midpoint 15 seconds ago, probability units, same market and receipt clock.",
    "mid_up": "Decision-time valid direct Up-token bid/ask midpoint in (0,1); not a complement inferred from Down.",
    "fair_up": "Causal fixed-model Up probability in [0,1], with official opening/final TWAP convention and parameters trained only on consumed development data. Missing unless that model and metadata exist.",
    "imbalance_up": "(best_Up_bid_size-best_Up_ask_size)/(sum of those sizes), [-1,1], valid positive direct-book sizes only.",
    "tail_gap_bps": "10000*log(latest locally received settlement-reference price / required remaining arithmetic TWAP mean), where required mean=(N*official_price_to_beat-known_sample_sum)/(N-known_sample_count); missing if sample protocol, all due samples, positive remaining threshold or endpoint is unverified.",
    "tail_sigma_bps": "Positive ex-ante standard deviation in log-bps of the remaining arithmetic TWAP mean, from a separately fixed causal forecast. Must reflect averaging covariance, not spot volatility divided by sqrt(sample count) without justification.",
    "twap_known_fraction": "Count of due final-TWAP samples already received divided by required total sample count, [0,1). Missing if any due sample or actual market sample schedule is unknown.",
}

# Intervals are disjoint within a template. Tail rules need actual remaining
# samples, so they use only the last 60 seconds; empty early-tail variants
# would otherwise inflate the candidate count without creating executable rules.
GENERAL_WINDOWS = ((10., 30.), (30., 60.), (60., 120.), (120., 180.), (180., 295.))
TAIL_WINDOWS = ((1., 5.), (5., 10.), (10., 20.), (20., 40.), (40., 60.))
THRESHOLD_MULTIPLIERS = (1., 1.5)


def _template(key, family, name, features, threshold, rule, *, tail=False,
              interaction=False):
    return {
        "template": key, "family": family, "name": name,
        "required_features": tuple(features.split()), "base_threshold": threshold,
        "causal_rule": rule, "tail": tail,
        "novelty": ("existing_family_new_feature_interaction" if interaction
                    else "existing_family_new_prespecified_grid"),
    }


TEMPLATES = (
    *(_template(f"momentum_{w}s", "price_trend", f"External {w}s momentum",
                f"r_{w}s", b, f"score=r_{w}s; buy its sign if abs(score)>=threshold")
      for w, b in ((5, .5), (15, 1.), (30, 1.5), (60, 2.))),
    *(_template(f"reversal_{w}s", "price_reversal", f"External {w}s reversal",
                f"r_{w}s", b, f"score=-r_{w}s; buy its sign if abs(score)>=threshold")
      for w, b in ((5, .5), (15, 1.), (30, 1.5), (60, 2.))),
    _template("acceleration_5s", "trend_shape", "Recent 5s acceleration versus preceding 10s",
              "r_5s r_15s", .75, "score=r_5s-(r_15s-r_5s)/2", interaction=True),
    _template("acceleration_15s", "trend_shape", "Recent 15s acceleration versus preceding 45s",
              "r_15s r_60s", 1.5, "score=r_15s-(r_60s-r_15s)/3", interaction=True),
    _template("nested_5_15", "trend_shape", "Nested 5s and 15s continuation",
              "r_5s r_15s", .75, "require r_5s*r_15s>0; score=r_5s"),
    _template("nested_15_60", "trend_shape", "Nested 15s and 60s continuation",
              "r_15s r_60s", 1.5, "require r_15s*r_60s>0; score=r_15s"),
    _template("pullback_5_60", "trend_shape", "5s pullback inside 60s trend",
              "r_5s r_60s", 2., "require r_5s*r_60s<0 and abs(r_5s)<abs(r_60s); score=r_60s"),
    _template("pullback_15_60", "trend_shape", "15s pullback inside 60s trend",
              "r_15s r_60s", 2.5, "require r_15s*r_60s<0 and abs(r_15s)<abs(r_60s); score=r_60s"),
    _template("flow_follow_15", "order_flow", "External aggressive 15s flow continuation",
              "flow_15s", .2, "score=flow_15s"),
    _template("flow_follow_60", "order_flow", "External aggressive 60s flow continuation",
              "flow_60s", .2, "score=flow_60s"),
    _template("flow_fade_15", "order_flow", "External aggressive 15s flow reversal control",
              "flow_15s", .35, "score=-flow_15s"),
    _template("flow_fade_60", "order_flow", "External aggressive 60s flow reversal control",
              "flow_60s", .35, "score=-flow_60s"),
    _template("flow_persistence", "order_flow", "15s and 60s aggressive flow persistence",
              "flow_15s flow_60s", .15, "require flow_15s*flow_60s>0; score=sign(flow_15s)*min(abs(flow_15s),abs(flow_60s))", interaction=True),
    _template("flow_acceleration", "order_flow", "Fresh aggressive-flow imbalance acceleration",
              "flow_15s flow_60s", .15, "require flow_15s*(flow_15s-flow_60s)>0; score=flow_15s-flow_60s; this is an imbalance contrast, not incremental signed volume", interaction=True),
    _template("flow_exhaustion", "order_flow", "Persistent aggressive flow weakening",
              "flow_15s flow_60s", .2, "require flow_15s*flow_60s>0 and abs(flow_15s)<0.5*abs(flow_60s); score=-flow_60s", interaction=True),
    _template("flow_flip", "order_flow", "Aggressive-flow direction reversal",
              "flow_15s flow_60s", .2, "require flow_15s*flow_60s<0; score=flow_15s", interaction=True),
    _template("flow_price_confirm_15", "flow_price_interaction", "15s price and aggressive flow agreement",
              "r_15s flow_15s", 1., "require r_15s*flow_15s>0; score=r_15s", interaction=True),
    _template("flow_price_confirm_60", "flow_price_interaction", "60s price and aggressive flow agreement",
              "r_60s flow_60s", 2., "require r_60s*flow_60s>0; score=r_60s", interaction=True),
    _template("flow_absorption_15", "flow_price_interaction", "15s price moving against aggressive flow",
              "r_15s flow_15s", .2, "require r_15s*flow_15s<0; score=-flow_15s", interaction=True),
    _template("flow_absorption_60", "flow_price_interaction", "60s price moving against aggressive flow",
              "r_60s flow_60s", .2, "require r_60s*flow_60s<0; score=-flow_60s", interaction=True),
    _template("flow_flat_price_15", "flow_price_interaction", "15s flow pressure before price response",
              "r_15s flow_15s", .3, "require abs(r_15s)<=0.5 bps; score=flow_15s", interaction=True),
    _template("flow_flat_price_60", "flow_price_interaction", "60s flow pressure before price response",
              "r_60s flow_60s", .3, "require abs(r_60s)<=1 bps; score=flow_60s", interaction=True),
    _template("trend_exhaustion_15", "flow_price_interaction", "15s trend challenged by opposite flow",
              "r_15s flow_15s", 1., "require r_15s*flow_15s<0 and abs(flow_15s)>=0.2; score=-r_15s", interaction=True),
    _template("trend_exhaustion_60", "flow_price_interaction", "60s trend challenged by opposite flow",
              "r_60s flow_60s", 2., "require r_60s*flow_60s<0 and abs(flow_60s)>=0.2; score=-r_60s", interaction=True),
    _template("vol_scaled_15", "volatility_state", "15s realized-variation-scaled momentum",
              "r_15s rv_15s", .8, "require rv_15s>0; score=r_15s/rv_15s"),
    _template("vol_scaled_60", "volatility_state", "60s realized-variation-scaled momentum",
              "r_60s rv_60s", .8, "require rv_60s>0; score=r_60s/rv_60s"),
    _template("vol_expansion_follow", "volatility_state", "Momentum during recent volatility expansion",
              "r_15s rv_15s rv_60s", 1., "require rv_15s/sqrt(15)>1.25*rv_60s/sqrt(60)>0; score=r_15s"),
    _template("vol_compression_follow", "volatility_state", "Momentum during recent volatility compression",
              "r_15s rv_15s rv_60s", 1., "require 0<rv_15s/sqrt(15)<0.75*rv_60s/sqrt(60); score=r_15s"),
    _template("vol_expansion_fade", "volatility_state", "Reversal during recent volatility expansion",
              "r_15s rv_15s rv_60s", 1.5, "require rv_15s/sqrt(15)>1.25*rv_60s/sqrt(60)>0; score=-r_15s"),
    _template("vol_compression_flow", "volatility_state", "Aggressive flow in volatility compression",
              "flow_15s rv_15s rv_60s", .2, "require 0<rv_15s/sqrt(15)<0.75*rv_60s/sqrt(60); score=flow_15s", interaction=True),
    _template("pm_follow_5", "pm_relative_value", "Polymarket 5s midpoint continuation",
              "pm_r_5s", .01, "score=pm_r_5s"),
    _template("pm_fade_5", "pm_relative_value", "Polymarket 5s midpoint reversal",
              "pm_r_5s", .015, "score=-pm_r_5s"),
    _template("pm_follow_15", "pm_relative_value", "Polymarket 15s midpoint continuation",
              "pm_r_15s", .02, "score=pm_r_15s"),
    _template("pm_fade_15", "pm_relative_value", "Polymarket 15s midpoint reversal",
              "pm_r_15s", .025, "score=-pm_r_15s"),
    _template("fair_gap", "pm_relative_value", "Causal settlement-model probability gap",
              "fair_up mid_up", .03, "score=fair_up-mid_up; probability-model error and execution costs remain to be tested"),
    _template("fair_gap_confirmed", "pm_relative_value", "Model gap agreed by external 15s return",
              "fair_up mid_up r_15s", .02, "require (fair_up-mid_up)*r_15s>0; score=fair_up-mid_up"),
    _template("pm_external_divergence", "pm_relative_value", "PM move opposed by external price",
              "pm_r_15s r_15s", .02, "require pm_r_15s*r_15s<0 and abs(r_15s)>=1 bps; score=-pm_r_15s"),
    _template("external_lead", "pm_relative_value", "External 5s move while PM midpoint is quiet",
              "r_5s pm_r_5s", 1., "require abs(pm_r_5s)<=0.005; score=r_5s"),
    _template("book_pressure", "book_pressure", "Direct Up-book queue imbalance",
              "imbalance_up", .25, "score=imbalance_up; top-book pressure is not a maker fill model"),
    _template("book_external_confirm", "book_pressure", "Book pressure agreed by external momentum",
              "imbalance_up r_15s", .2, "require imbalance_up*r_15s>0; score=imbalance_up"),
    _template("book_against_pm", "book_pressure", "Book pressure opposing recent PM movement",
              "imbalance_up pm_r_5s", .25, "require imbalance_up*pm_r_5s<0; score=imbalance_up", interaction=True),
    _template("twap_tail_standardized", "twap_tail", "Remaining-TWAP standardized gap",
              "tail_gap_bps tail_sigma_bps twap_known_fraction", 1., "require 0<twap_known_fraction<1 and tail_sigma_bps>0; score=tail_gap_bps/tail_sigma_bps", tail=True, interaction=True),
    _template("twap_tail_flow", "twap_tail", "Remaining-TWAP gap agreed by fresh flow",
              "tail_gap_bps tail_sigma_bps twap_known_fraction flow_15s", .8, "require 0<twap_known_fraction<1, tail_sigma_bps>0, tail_gap_bps*flow_15s>0; score=tail_gap_bps/tail_sigma_bps", tail=True, interaction=True),
    _template("twap_tail_pm_gap", "twap_tail", "Gaussian remaining-TWAP probability discrepancy",
              "tail_gap_bps tail_sigma_bps twap_known_fraction mid_up", .03, "require 0<twap_known_fraction<1 and tail_sigma_bps>0; score=Phi(tail_gap_bps/tail_sigma_bps)-mid_up; Gaussian approximation is an unvalidated research model", tail=True, interaction=True),
)
_TEMPLATE_MAP = {t["template"]: t for t in TEMPLATES}

SUPPLEMENTARY_TEMPLATES = (
    _template("prior_momentum_10s", "price_trend", "Preceding 10s continuation excluding recent 5s",
              "r_5s r_15s", 1., "score=r_15s-r_5s; window is (decision-15s,decision-5s]", interaction=True),
    _template("prior_momentum_45s", "price_trend", "Preceding 45s continuation excluding recent 15s",
              "r_15s r_60s", 2., "score=r_60s-r_15s; window is (decision-60s,decision-15s]", interaction=True),
    _template("prior_reversal_10s", "price_reversal", "Preceding 10s reversal control",
              "r_5s r_15s", 1., "score=-(r_15s-r_5s)", interaction=True),
    _template("prior_reversal_45s", "price_reversal", "Preceding 45s reversal control",
              "r_15s r_60s", 2., "score=-(r_60s-r_15s)", interaction=True),
    _template("acceleration_fade_5s", "trend_shape", "Fade recent 5s acceleration",
              "r_5s r_15s", .75, "score=-(r_5s-(r_15s-r_5s)/2); opposing-direction control", interaction=True),
    _template("acceleration_fade_15s", "trend_shape", "Fade recent 15s acceleration",
              "r_15s r_60s", 1.5, "score=-(r_15s-(r_60s-r_15s)/3); opposing-direction control", interaction=True),
    _template("triple_consensus", "trend_shape", "5s 15s 60s trend agreement",
              "r_5s r_15s r_60s", .75, "require r_5s*r_15s>0 and r_15s*r_60s>0; score=r_5s", interaction=True),
    _template("recent_turn_5_15", "trend_shape", "Recent 5s reversal of preceding 10s direction",
              "r_5s r_15s", .75, "require r_5s*(r_15s-r_5s)<0; score=r_5s", interaction=True),
    _template("return_curvature", "trend_shape", "Fast acceleration relative to intermediate acceleration",
              "r_5s r_15s r_60s", 1., "score=(r_5s-(r_15s-r_5s)/2)-(r_15s-(r_60s-r_15s)/3)/3", interaction=True),
    _template("pm_book_confirm_5", "pm_relative_value", "5s PM momentum with matching book pressure",
              "pm_r_5s imbalance_up", .01, "require pm_r_5s*imbalance_up>0; score=pm_r_5s", interaction=True),
    _template("pm_book_confirm_15", "pm_relative_value", "15s PM momentum with matching book pressure",
              "pm_r_15s imbalance_up", .02, "require pm_r_15s*imbalance_up>0; score=pm_r_15s", interaction=True),
    _template("pm_book_fade_5", "pm_relative_value", "5s PM reversal with opposing book pressure",
              "pm_r_5s imbalance_up", .015, "require pm_r_5s*imbalance_up<0; score=-pm_r_5s", interaction=True),
    _template("pm_book_fade_15", "pm_relative_value", "15s PM reversal with opposing book pressure",
              "pm_r_15s imbalance_up", .025, "require pm_r_15s*imbalance_up<0; score=-pm_r_15s", interaction=True),
    _template("external_book_confirm_5", "book_pressure", "5s external move with matching book pressure",
              "r_5s imbalance_up", .5, "require r_5s*imbalance_up>0; score=r_5s", interaction=True),
    _template("external_book_confirm_60", "book_pressure", "60s external move with matching book pressure",
              "r_60s imbalance_up", 2., "require r_60s*imbalance_up>0; score=r_60s", interaction=True),
    _template("book_veto_fade", "book_pressure", "Fade book pressure challenged by external price",
              "imbalance_up r_15s", .25, "require imbalance_up*r_15s<0; score=-imbalance_up", interaction=True),
    _template("pm_acceleration_follow", "pm_relative_value", "PM 5s acceleration versus preceding 10s",
              "pm_r_5s pm_r_15s", .01, "score=pm_r_5s-(pm_r_15s-pm_r_5s)/2", interaction=True),
    _template("pm_acceleration_fade", "pm_relative_value", "Fade PM 5s acceleration",
              "pm_r_5s pm_r_15s", .015, "score=-(pm_r_5s-(pm_r_15s-pm_r_5s)/2)", interaction=True),
    _template("pm_nested_5_15", "pm_relative_value", "PM 5s and 15s continuation agreement",
              "pm_r_5s pm_r_15s", .01, "require pm_r_5s*pm_r_15s>0; score=pm_r_5s", interaction=True),
    _template("pm_pullback_5_15", "pm_relative_value", "PM 5s pullback inside 15s move",
              "pm_r_5s pm_r_15s", .02, "require pm_r_5s*pm_r_15s<0 and abs(pm_r_5s)<abs(pm_r_15s); score=pm_r_15s", interaction=True),
    _template("vol_pm_expansion", "volatility_state", "PM continuation during external volatility expansion",
              "pm_r_5s rv_15s rv_60s", .01, "require rv_15s/sqrt(15)>1.25*rv_60s/sqrt(60)>0; score=pm_r_5s", interaction=True),
    _template("vol_pm_compression_fade", "volatility_state", "PM reversal during external volatility compression",
              "pm_r_5s rv_15s rv_60s", .015, "require 0<rv_15s/sqrt(15)<0.75*rv_60s/sqrt(60); score=-pm_r_5s", interaction=True),
)
_TEMPLATE_MAP.update({t["template"]: t for t in SUPPLEMENTARY_TEMPLATES})


def rule_fingerprint(candidate):
    """Hash executable identity, excluding cosmetic id/name and research status."""
    core = {k: candidate[k] for k in ("version", "template", "params", "required_features", "execution_kind")}
    return hashlib.sha256(json.dumps(core, sort_keys=True, separators=(",", ":"),
                                     allow_nan=False).encode()).hexdigest()


def _make_registry(templates, offset=0):
    candidates = []
    for t in templates:
        windows = TAIL_WINDOWS if t["tail"] else GENERAL_WINDOWS
        for window_index, (lo, hi) in enumerate(windows):
            for multiplier in THRESHOLD_MULTIPLIERS:
                number = len(candidates) + offset + 1
                c = {
                    "id": f"M500-{number:03d}", "version": VERSION,
                    "name": f"{t['name']} / tau {lo:g}-{hi:g}s / strength {multiplier:g}",
                    "family": t["family"], "template": t["template"],
                    "parameter_variant_of": t["template"],
                    "params": {"tau_min_s": lo, "tau_max_s": hi,
                               "tau_upper_inclusive": window_index == len(windows) - 1,
                               "threshold": t["base_threshold"] * multiplier,
                               "threshold_multiplier": multiplier},
                    "required_features": ["tau_s", *t["required_features"]],
                    "novelty": t["novelty"],
                    "prior_art": (["settlement_proxy.py", "zoo100.py"] if t["tail"] else
                                  ["zoo100.py", "strategy_zoo.py"]),
                    "causal_rule": "Use only available receipt-time features in the declared tau interval. " + t["causal_rule"] + "; positive score buys Up, negative score buys Down, abs(score)>=threshold required.",
                    "execution_kind": "single_leg_taker_research",
                    "research_status": "registered_not_backtested",
                    "independent_mechanism_claim": False,
                }
                c["rule_fingerprint"] = rule_fingerprint(c)
                candidates.append(c)
    return candidates


def make_candidates() -> list[dict]:
    """The original 500 specifications, unchanged by the supplementary set."""
    candidates = _make_registry(TEMPLATES)
    if len(candidates) != 500 or len({c["rule_fingerprint"] for c in candidates}) != 500:
        raise RuntimeError("Registry must contain exactly 500 unique executable specifications")
    return candidates


def make_supplementary_candidates() -> list[dict]:
    """220 additional price/book rules, designed from schema gaps, not outcomes."""
    candidates = _make_registry(SUPPLEMENTARY_TEMPLATES, offset=500)
    if len(candidates) != 220 or len({c["rule_fingerprint"] for c in candidates}) != 220:
        raise RuntimeError("Supplement must contain exactly 220 executable specifications")
    return candidates


def _valid_feature(name, value):
    if isinstance(value, bool) or not isinstance(value, Real) or not math.isfinite(value):
        return False
    if name == "tau_s":
        return 0 < value <= 300
    if name in ("flow_15s", "flow_60s", "imbalance_up", "pm_r_5s", "pm_r_15s"):
        return -1 <= value <= 1
    if name in ("rv_15s", "rv_60s"):
        return value >= 0
    if name == "tail_sigma_bps":
        return value > 0
    if name == "mid_up":
        return 0 < value < 1
    if name == "fair_up":
        return 0 <= value <= 1
    if name == "twap_known_fraction":
        return 0 <= value < 1
    return True


def _score(key, f):
    if key.startswith("momentum_"):
        return f["r_" + key.removeprefix("momentum_")]
    if key.startswith("reversal_"):
        return -f["r_" + key.removeprefix("reversal_")]
    if key == "acceleration_5s":
        return f["r_5s"] - (f["r_15s"] - f["r_5s"]) / 2
    if key == "acceleration_15s":
        return f["r_15s"] - (f["r_60s"] - f["r_15s"]) / 3
    if key.startswith("nested_"):
        short, long = ("r_5s", "r_15s") if key == "nested_5_15" else ("r_15s", "r_60s")
        return f[short] if f[short] * f[long] > 0 else None
    if key.startswith("pullback_"):
        short = "r_5s" if key == "pullback_5_60" else "r_15s"
        return f["r_60s"] if f[short] * f["r_60s"] < 0 and abs(f[short]) < abs(f["r_60s"]) else None
    if key.startswith("flow_follow_") or key.startswith("flow_fade_"):
        x = f["flow_" + key.rsplit("_", 1)[-1] + "s"]
        return -x if key.startswith("flow_fade_") else x
    if key in ("flow_persistence", "flow_acceleration", "flow_exhaustion", "flow_flip"):
        short, long = f["flow_15s"], f["flow_60s"]
        if key == "flow_persistence":
            return math.copysign(min(abs(short), abs(long)), short) if short * long > 0 else None
        if key == "flow_acceleration":
            return short - long if short * (short - long) > 0 else None
        if key == "flow_exhaustion":
            return -long if short * long > 0 and abs(short) < .5 * abs(long) else None
        return short if short * long < 0 else None
    if key.startswith(("flow_price_confirm_", "flow_absorption_", "flow_flat_price_", "trend_exhaustion_")):
        window = key.rsplit("_", 1)[-1]
        r, flow = f[f"r_{window}s"], f[f"flow_{window}s"]
        if key.startswith("flow_price_confirm_"):
            return r if r * flow > 0 else None
        if key.startswith("flow_absorption_"):
            return -flow if r * flow < 0 else None
        if key.startswith("flow_flat_price_"):
            return flow if abs(r) <= (.5 if window == "15" else 1.) else None
        return -r if r * flow < 0 and abs(flow) >= .2 else None
    if key.startswith("vol_scaled_"):
        window = key.rsplit("_", 1)[-1]
        return f[f"r_{window}s"] / f[f"rv_{window}s"] if f[f"rv_{window}s"] > 0 else None
    if key.startswith(("vol_expansion_", "vol_compression_")):
        short, long = f["rv_15s"] / math.sqrt(15), f["rv_60s"] / math.sqrt(60)
        gate = short > 1.25 * long > 0 if key.startswith("vol_expansion_") else 0 < short < .75 * long
        if not gate:
            return None
        if key == "vol_compression_flow":
            return f["flow_15s"]
        return -f["r_15s"] if key == "vol_expansion_fade" else f["r_15s"]
    if key.startswith(("pm_follow_", "pm_fade_")):
        x = f["pm_r_" + key.rsplit("_", 1)[-1] + "s"]
        return -x if key.startswith("pm_fade_") else x
    if key in ("fair_gap", "fair_gap_confirmed"):
        gap = f["fair_up"] - f["mid_up"]
        return gap if key == "fair_gap" or gap * f["r_15s"] > 0 else None
    if key == "pm_external_divergence":
        return -f["pm_r_15s"] if f["pm_r_15s"] * f["r_15s"] < 0 and abs(f["r_15s"]) >= 1. else None
    if key == "external_lead":
        return f["r_5s"] if abs(f["pm_r_5s"]) <= .005 else None
    if key == "book_pressure":
        return f["imbalance_up"]
    if key == "book_external_confirm":
        return f["imbalance_up"] if f["imbalance_up"] * f["r_15s"] > 0 else None
    if key == "book_against_pm":
        return f["imbalance_up"] if f["imbalance_up"] * f["pm_r_5s"] < 0 else None
    if key.startswith("twap_tail_"):
        if not 0 < f["twap_known_fraction"] < 1:
            return None
        z = f["tail_gap_bps"] / f["tail_sigma_bps"]
        if key == "twap_tail_flow" and f["tail_gap_bps"] * f["flow_15s"] <= 0:
            return None
        if key == "twap_tail_pm_gap":
            return .5 * (1 + math.erf(z / math.sqrt(2))) - f["mid_up"]
        return z
    if key.startswith(("prior_momentum_", "prior_reversal_")):
        x = f["r_15s"] - f["r_5s"] if key.endswith("10s") else f["r_60s"] - f["r_15s"]
        return -x if key.startswith("prior_reversal_") else x
    if key == "acceleration_fade_5s":
        return -(f["r_5s"] - (f["r_15s"] - f["r_5s"]) / 2)
    if key == "acceleration_fade_15s":
        return -(f["r_15s"] - (f["r_60s"] - f["r_15s"]) / 3)
    if key == "triple_consensus":
        return f["r_5s"] if f["r_5s"] * f["r_15s"] > 0 and f["r_15s"] * f["r_60s"] > 0 else None
    if key == "recent_turn_5_15":
        return f["r_5s"] if f["r_5s"] * (f["r_15s"] - f["r_5s"]) < 0 else None
    if key == "return_curvature":
        return (f["r_5s"] - (f["r_15s"] - f["r_5s"]) / 2) - (f["r_15s"] - (f["r_60s"] - f["r_15s"]) / 3) / 3
    if key.startswith(("pm_book_confirm_", "pm_book_fade_")):
        x = f["pm_r_" + key.rsplit("_", 1)[-1] + "s"]
        if key.startswith("pm_book_confirm_"):
            return x if x * f["imbalance_up"] > 0 else None
        return -x if x * f["imbalance_up"] < 0 else None
    if key.startswith("external_book_confirm_"):
        x = f["r_" + key.rsplit("_", 1)[-1] + "s"]
        return x if x * f["imbalance_up"] > 0 else None
    if key == "book_veto_fade":
        return -f["imbalance_up"] if f["imbalance_up"] * f["r_15s"] < 0 else None
    if key in ("pm_acceleration_follow", "pm_acceleration_fade"):
        x = f["pm_r_5s"] - (f["pm_r_15s"] - f["pm_r_5s"]) / 2
        return -x if key.endswith("fade") else x
    if key == "pm_nested_5_15":
        return f["pm_r_5s"] if f["pm_r_5s"] * f["pm_r_15s"] > 0 else None
    if key == "pm_pullback_5_15":
        return f["pm_r_15s"] if f["pm_r_5s"] * f["pm_r_15s"] < 0 and abs(f["pm_r_5s"]) < abs(f["pm_r_15s"]) else None
    if key == "vol_pm_expansion":
        return f["pm_r_5s"] if f["rv_15s"] / math.sqrt(15) > 1.25 * f["rv_60s"] / math.sqrt(60) > 0 else None
    if key == "vol_pm_compression_fade":
        return -f["pm_r_5s"] if 0 < f["rv_15s"] / math.sqrt(15) < .75 * f["rv_60s"] / math.sqrt(60) else None
    return None


def signal(candidate: dict, features: dict) -> str | None:
    """Return Up/Down only with finite, correctly ranged required features.

    No fallback model, forward fill, timestamp repair or settlement-label
    substitution occurs here. Feature provenance and freshness belong to the
    causal feature adapter; a numeric dict alone cannot prove causality.
    """
    try:
        template = _TEMPLATE_MAP[candidate["template"]]
        needed = ("tau_s", *template["required_features"])
        if any(name not in features or not _valid_feature(name, features[name]) for name in needed):
            return None
        p = candidate["params"]
        lo, hi, threshold = p["tau_min_s"], p["tau_max_s"], p["threshold"]
        if any(isinstance(v, bool) or not isinstance(v, Real) or not math.isfinite(v) for v in (lo, hi, threshold)):
            return None
        if not 0 < lo < hi <= 300 or threshold <= 0 or not isinstance(p["tau_upper_inclusive"], bool):
            return None
        tau = features["tau_s"]
        if not lo <= tau or not (tau <= hi if p["tau_upper_inclusive"] else tau < hi):
            return None
        score = _score(template["template"], features)
        if score is None or not math.isfinite(score) or score == 0 or abs(score) < threshold:
            return None
        return "Up" if score > 0 else "Down"
    except (KeyError, TypeError, ValueError, ZeroDivisionError, OverflowError):
        return None


if __name__ == "__main__":
    print(json.dumps({"version": VERSION, "candidate_count": 500,
                      "template_count": len(TEMPLATES),
                      "family_count": len({t["family"] for t in TEMPLATES}),
                      "feature_definitions": FEATURE_DEFINITIONS,
                      "candidates": make_candidates()}, indent=2))
