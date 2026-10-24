import csv
import gzip
import json
from datetime import UTC, datetime
from decimal import Decimal
from urllib.parse import parse_qs, urlparse

import pytest
import reward_dust as dust

CID = "0xmarket"
UP = "up-token"
DOWN = "down-token"


def _record(
    *,
    ts=1_791_590_401,
    side="BUY",
    asset=UP,
    price=0.01,
    size=0.01,
    tx="0xtx",
    wallet="0xwallet",
):
    return [ts, side, asset, price, size, 0 if asset == UP else 1, tx, wallet]


def test_fee_free_classification_is_strict_and_decimal_exact():
    assert dust.raw_taker_fee(Decimal("0.01"), Decimal("0.01")) == Decimal("0.00000693")
    assert dust.is_fee_free(Decimal("0.01"), Decimal("0.01"))
    assert not dust.is_fee_free(Decimal("0.02"), Decimal("0.01"))
    assert dust.is_below_fee_quantum(Decimal("0.00000999"))
    assert not dust.is_below_fee_quantum(Decimal("0.00001000"))


def test_normalization_preserves_legitimate_exact_multiplicity_with_occurrence_ids():
    repeated = _record()

    rows = dust.normalise_public_rows(
        [repeated, repeated],
        market_id=CID,
        market_start_s=1_791_590_400,
        token_up=UP,
        token_down=DOWN,
        up_won=True,
    )

    assert len(rows) == 2
    assert rows[0].public_row_id != rows[1].public_row_id
    assert [row.occurrence for row in rows] == [0, 1]
    assert all(row.outcome == "Up" and row.settlement_value == Decimal(1) for row in rows)


def test_normalization_rejects_unknown_tokens_instead_of_guessing_outcome():
    with pytest.raises(ValueError, match="unknown token"):
        dust.normalise_public_rows(
            [_record(asset="mystery")],
            market_id=CID,
            market_start_s=1_791_590_400,
            token_up=UP,
            token_down=DOWN,
            up_won=False,
        )


def test_capacity_summary_is_diagnostic_only_and_charges_settlement_risk():
    records = [
        _record(asset=DOWN, price=0.01, size=0.01, tx="0xzero"),
        _record(price=0.02, size=0.01, tx="0xcontrol"),
        _record(side="SELL", asset=UP, price=0.99, size=0.01, tx="0xsell"),
    ]
    rows = dust.normalise_public_rows(
        records,
        market_id=CID,
        market_start_s=1_791_590_400,
        token_up=UP,
        token_down=DOWN,
        up_won=True,
    )

    summary = dust.summarise_capacity(rows, complete_days=1, complete_markets=1)

    assert summary.public_rows == 3
    assert summary.theoretical_zero_fee_rows == 2
    assert summary.zero_fee_weighted_volume == Decimal("0.00045540")
    assert summary.zero_fee_settlement_pnl == Decimal("-0.0002")
    assert summary.nonzero_fee_rows == 1
    assert summary.bronze_reached is False
    assert summary.evidence_class == "public_row_diagnostic_only"


def test_ideal_dust_needs_millions_of_fills_to_reach_bronze():
    per_fill = dust.weighted_volume(Decimal("0.01"), Decimal("0.01"))

    assert per_fill == Decimal("0.0002277")
    assert dust.fills_required(Decimal(2000), per_fill) == 8_783_488


def test_fetcher_explicitly_requests_taker_only_and_rejects_truncation():
    seen = []

    class FakeHttp:
        def get(self, url, slim=None):
            seen.append(url)
            payload = [{
                "timestamp": 1_791_590_401,
                "side": "BUY",
                "asset": UP,
                "price": 0.01,
                "size": 0.01,
                "outcomeIndex": 0,
                "transactionHash": "0xtx",
                "proxyWallet": "0xwallet",
            }] if "offset=0" in url else []
            return slim(url, payload) if slim is not None else payload

    records, info = dust.fetch_taker_records(
        FakeHttp(), CID, start_s=1_791_590_400, end_s=1_791_590_699, page=10
    )

    assert len(records) == 1
    assert not info["truncated"]
    assert seen and all("takerOnly=true" in url for url in seen)

    with pytest.raises(RuntimeError, match="truncated"):
        dust.require_complete_fetch(records, {**info, "truncated": True})


def test_complete_utc_day_requires_exactly_288_unique_five_minute_markets():
    start = 1_791_590_400
    complete = [start + 300 * i for i in range(288)]

    assert dust.validate_complete_day(start, complete) == complete
    with pytest.raises(ValueError, match="288"):
        dust.validate_complete_day(start, complete[:-1])
    with pytest.raises(ValueError, match="unique"):
        dust.validate_complete_day(start, complete[:-1] + [complete[-2]])


def _event(start, up_won=True):
    return {
        "slug": f"btc-updown-5m-{start}",
        "startTime": datetime.fromtimestamp(start, UTC).isoformat(),
        "markets": [{
            "eventStartTime": datetime.fromtimestamp(start, UTC).isoformat(),
            "conditionId": f"condition-{start}",
            "outcomes": json.dumps(["Down", "Up"]),
            "outcomePrices": json.dumps(["0", "1"] if up_won else ["1", "0"]),
            "clobTokenIds": json.dumps([f"down-{start}", f"up-{start}"]),
            "resolutionSource": "https://data.chain.link/streams/btc-usd-twap-60s",
        }],
    }


def test_market_fetch_reuses_batch_endpoint_and_binds_resolved_token_outcomes():
    start = 1_791_590_400
    calls = []

    class FakeHttp:
        def get(self, url):
            calls.append(url)
            slugs = parse_qs(urlparse(url).query)["slug"]
            return [_event(int(slug.rsplit("-", 1)[1])) for slug in slugs]

    markets = dust.fetch_btc5m_markets(FakeHttp(), start, batch=100)

    assert len(markets) == 288
    assert len(calls) == 3
    assert markets[0].condition_id == f"condition-{start}"
    assert markets[0].token_up == f"up-{start}"
    assert markets[0].token_down == f"down-{start}"
    assert markets[0].up_won is True


def test_market_fetch_fails_closed_when_gamma_omits_one_slot():
    start = 1_791_590_400

    class FakeHttp:
        def get(self, url):
            if "/slug/" in url:
                return {}
            slugs = parse_qs(urlparse(url).query)["slug"]
            rows = [_event(int(slug.rsplit("-", 1)[1])) for slug in slugs]
            return rows[1:] if len(slugs) < 100 else rows

    with pytest.raises(ValueError, match="288"):
        dust.fetch_btc5m_markets(FakeHttp(), start, batch=100)


def test_market_fetch_recovers_a_transient_partial_batch_with_singleton_lookup():
    start = 1_791_590_400
    omitted = start + 287 * 300
    calls = []

    class FakeHttp:
        def get(self, url):
            calls.append(url)
            if "/slug/" in url:
                return _event(int(url.rsplit("-", 1)[1]))
            slugs = parse_qs(urlparse(url).query)["slug"]
            rows = [_event(int(slug.rsplit("-", 1)[1])) for slug in slugs]
            if len(slugs) > 1:
                return [row for row in rows if int(row["slug"].rsplit("-", 1)[1]) != omitted]
            return rows

    markets = dust.fetch_btc5m_markets(FakeHttp(), start, batch=100)

    assert len(markets) == 288
    assert len(calls) == 4
    assert markets[-1].start_s == omitted


def test_market_fetch_uses_authoritative_fallback_for_stale_gamma_outcome():
    start = 1_791_590_400
    stale = start + 17 * 300
    resolved = []

    class FakeHttp:
        def get(self, url):
            if "/slug/" in url:
                return _event(int(url.rsplit("-", 1)[1]))
            slugs = parse_qs(urlparse(url).query)["slug"]
            rows = [_event(int(slug.rsplit("-", 1)[1])) for slug in slugs]
            for row in rows:
                if int(row["slug"].rsplit("-", 1)[1]) == stale:
                    row["markets"][0]["outcomePrices"] = json.dumps(["0.005", "0.995"])
            return rows

    def resolve_unsettled(condition_id, up_index):
        resolved.append((condition_id, up_index))
        return True

    markets = dust.fetch_btc5m_markets(
        FakeHttp(),
        start,
        batch=100,
        resolve_unsettled=resolve_unsettled,
    )

    assert len(markets) == 288
    assert markets[17].up_won is True
    assert resolved == [(f"condition-{stale}", 1)]


def test_certified_json_get_passes_an_explicit_ca_context(monkeypatch):
    seen = {}

    class Response:
        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

        def read(self):
            return b'{"ok": true}'

    def fake_urlopen(request, timeout, context):
        seen.update(url=request.full_url, timeout=timeout, context=context)
        return Response()

    monkeypatch.setattr(dust, "urlopen", fake_urlopen)

    assert dust.certified_json_get("https://example.test/data") == {"ok": True}
    assert seen["url"] == "https://example.test/data"
    assert seen["timeout"] == 30
    assert seen["context"] is not None


def test_market_audit_retains_only_dust_rows_and_fixed_nonzero_control():
    market = dust.MarketMeta(
        start_s=1_791_590_400,
        slug="btc-updown-5m-1791590400",
        condition_id=CID,
        token_up=UP,
        token_down=DOWN,
        up_won=True,
        source="btc-usd-twap-60s",
        source_window_s=60,
    )
    records = [
        _record(asset=DOWN, price=0.01, size=0.01, tx="0xzero"),
        _record(price=0.02, size=0.01, tx="0xcontrol"),
        _record(side="SELL", asset=UP, price=0.99, size=0.01, tx="0xsell"),
    ]

    audit = dust.audit_public_records(
        market, records, {"truncated": False, "pages": 1, "windows": 1, "note": ""}
    )

    assert audit.public_rows == 3
    assert len(audit.dust_rows) == 2
    assert audit.dust_weighted_volume == Decimal("0.00045540")
    assert audit.dust_settlement_pnl == Decimal("-0.0002")
    assert audit.cent_size_nonzero_rows == 1
    assert audit.cent_size_nonzero_settlement_pnl == Decimal("0.0098")


def test_report_refuses_to_promote_a_public_row_diagnostic_that_misses_bronze():
    market = dust.MarketMeta(
        start_s=1_791_590_400,
        slug="btc-updown-5m-1791590400",
        condition_id=CID,
        token_up=UP,
        token_down=DOWN,
        up_won=True,
        source="btc-usd-twap-60s",
        source_window_s=60,
    )
    audit = dust.audit_public_records(
        market,
        [_record(asset=DOWN, price=0.01, size=0.01, tx="0xzero")],
        {"truncated": False, "pages": 1, "windows": 1, "note": ""},
    )

    report = dust.render_report(
        observed_at="2026-10-11T01:00:00Z",
        start_day="2026-10-03",
        requested_days=30,
        complete_days=1,
        audits=[audit],
    )

    assert "INCOMPLETE" in report
    assert "public-row diagnostic only" in report
    assert "0.0002277" in report
    assert "8,783,488" in report
    assert "parent order" in report and "actual charged fee" in report
    assert "Bronze was not reached" in report
    assert "unreachable within a rolling 30-day window" in report
    assert "cannot support formal capacity rejection" in report


def test_thirty_public_days_cannot_switch_the_public_report_to_a_formal_verdict():
    public_only = dust.render_report(
        observed_at="2026-10-11T01:00:00Z",
        start_day="2026-09-11",
        requested_days=30,
        complete_days=30,
        audits=[],
    )

    assert "**Verdict:** **INCOMPLETE**" in public_only
    assert "REJECTED_CAPACITY" not in public_only


def test_candidate_csv_is_a_single_gzip_current_artifact(tmp_path):
    rows = dust.normalise_public_rows(
        [_record(asset=DOWN, price=0.01, size=0.01, tx="0xzero")],
        market_id=CID,
        market_start_s=1_791_590_400,
        token_up=UP,
        token_down=DOWN,
        up_won=True,
    )
    out = tmp_path / "reward-dust.csv.gz"

    dust.write_candidate_csv(out, rows)

    with gzip.open(out, "rt", newline="") as handle:
        saved = list(csv.DictReader(handle))
    assert len(saved) == 1
    assert saved[0]["transaction_hash"] == "0xzero"
    assert saved[0]["raw_fee"] == "0.00000693"
    assert saved[0]["nominal_weighted_volume"] == "0.0002277"
    assert not list(tmp_path.glob("*.tmp"))
