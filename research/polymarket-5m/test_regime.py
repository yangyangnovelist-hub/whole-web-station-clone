import numpy as np
import pandas as pd

import regime as rg


def _series(phi, n=20000, seed=0):
    rng = np.random.default_rng(seed)
    e = rng.normal(0, 1e-4, n)
    r = np.zeros(n)
    for i in range(1, n):
        r[i] = phi * r[i - 1] + e[i]
    close = 60000 * np.exp(np.cumsum(r))
    return pd.DataFrame({"close": close, "vol": 1.0, "buy": 0.5}, index=pd.RangeIndex(1_780_000_000, 1_780_000_000 + n))


def test_variance_ratio_reads_momentum_and_reversion():
    mom = rg.features(_series(0.4))["vr10"].iloc[-1]
    rev = rg.features(_series(-0.4))["vr10"].iloc[-1]
    rw = rg.features(_series(0.0))["vr10"].iloc[-1]
    assert mom > 1.5 and rev < 0.7 and 0.8 < rw < 1.2


def test_a_jump_that_stays_shows_continuation_zero_and_regime_known_before():
    s = _series(0.0, n=5000)
    s.loc[1_780_003_000:, "close"] *= 1.01  # a jump up at second 3000 that stays
    f = rg.add_dvol(rg.features(s), None)
    hit = f.index.get_loc(1_780_003_000)
    assert f["r"].iloc[hit] > 2 * f["sigma"].iloc[hit]
    # the regime at the jump second is computed from earlier seconds only
    assert abs(f["vr10"].iloc[hit] - f["vr10"].iloc[hit - 1]) < 0.05
