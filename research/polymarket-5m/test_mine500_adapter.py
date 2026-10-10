"""Causal and lifecycle regressions; synthetic fixtures are not PnL evidence."""
import math
import gzip
import json

import pytest

from mine500_adapter import Adapter, Book, adapt_events, first_raw_metadata


def market():
    return {'market_id':'m','recv_ms':0,'start_ms':0,'end_ms':300000,
            'up_token_id':'u','down_token_id':'d','fee_rate':.07,'fee_provenance':'assumed'}


def snapshot(token, now, price=.5, source_ms=None):
    return {'kind':'clob_snapshot','market_id':'m','asset_id':token,'recv_ms':now,
            'source_ts_ms':now if source_ms is None else source_ms,
            'bids':[{'price':price-.01,'size':5}], 'asks':[{'price':price+.01,'size':5}]}


def spot(now, price, side=None):
    return {'kind':'spot_trade','recv_ms':now,'source_ts_ms':now+5000,
            'price':price,'size':2,'aggressor_side':side}


def decision(rows, now):
    return next(row for row in rows if row['type']=='decision' and row['recv_ms']==now)


def test_source_event_clock_cannot_pull_future_receipt_backwards():
    events=[{'kind':'spot_connection','recv_ms':0}]
    events += [spot(t,100 if t<=5000 else 120) for t in range(0,7001,1000)]
    rows=list(adapt_events(events,[market()],start_ms=0,end_ms=7000))
    at5=decision(rows,5000)
    assert at5['features']['r_5s']==0
    assert at5['feature_available_ms']['r_5s']==5000
    assert decision(rows,6000)['features']['r_5s']==pytest.approx(10000*math.log(1.2))


def test_grid_consumes_all_same_receipt_frames_before_decision():
    events=[snapshot('u',1000),snapshot('d',1000)]
    rows=list(adapt_events(events,[market()],start_ms=1000,end_ms=1000))
    assert [row['type'] for row in rows]==['book','book','decision']
    assert rows[1]['healthy'] is True
    assert rows[2]['features']['mid_up']==.5


def test_double_token_clock_is_not_refreshed_by_other_token():
    adapter=Adapter([market()])
    list(adapter.consume(snapshot('u',0)))
    rows=list(adapter.consume(snapshot('d',251)))
    assert rows[0]['recv_ms']==251
    assert rows[0]['up_recv_ms']==0
    assert rows[0]['down_recv_ms']==251
    assert rows[0]['healthy'] is False


def test_disconnect_clears_books_and_pm_rolling_history():
    adapter=Adapter([market()])
    list(adapter.consume(snapshot('u',0)))
    list(adapter.consume(snapshot('d',0)))
    list(adapter.decisions(0))
    assert adapter.pm_prices['m']
    rows=list(adapter.consume({'kind':'clob_error','recv_ms':1}))
    assert rows[0]['healthy'] is False and rows[0]['up_asks']==[]
    assert not adapter.pm_prices['m']
    assert not adapter.books


def test_missing_aggressor_is_missing_flow_not_zero_and_fair_tail_missing():
    events=[{'kind':'spot_connection','recv_ms':0}]
    events += [spot(t,100+t/10000) for t in range(0,16001,1000)]
    row=decision(list(adapt_events(events,[market()],start_ms=0,end_ms=16000)),16000)
    assert row['features']['flow_15s'] is None
    assert row['feature_available_ms']['flow_15s'] is None
    assert row['features']['rv_15s']>0
    assert row['features']['fair_up'] is None
    assert row['features']['tail_gap_bps'] is None


def test_documented_aggressor_flow_and_disconnect_reset():
    adapter=Adapter([market()])
    list(adapter.consume({'kind':'spot_connection','recv_ms':0}))
    for t in range(0,16001,1000):
        list(adapter.consume(spot(t,100,'BUY')))
    row=list(adapter.decisions(16000))[0]
    assert row['features']['flow_15s']==1
    list(adapter.consume({'kind':'spot_disconnect','recv_ms':16001}))
    row=list(adapter.decisions(17000))[0]
    assert row['features']['flow_15s'] is None
    assert row['features']['r_5s'] is None


def test_depleted_or_crossed_book_cannot_heal_without_snapshot():
    book=Book()
    book.apply(snapshot('u',0))
    book.apply({'kind':'clob_price_change','source_ts_ms':1,'recv_ms':1,
                'side':'SELL','price':.51,'size':0})
    assert not book.ready
    book.apply({'kind':'clob_price_change','source_ts_ms':2,'recv_ms':2,
                'side':'SELL','price':.51,'size':9})
    assert not book.ready
    book.apply(snapshot('u',3))
    assert book.ready


def test_late_venue_update_cannot_refresh_token_or_rewind_price():
    book=Book()
    book.apply(snapshot('u',1000,.6,source_ms=900))
    assert not book.apply(snapshot('u',1100,.3,source_ms=800))
    assert book.recv_ms==1000 and min(book.asks)==.61


def test_regressed_input_receipt_and_past_end_fail_closed():
    with pytest.raises(ValueError,match='backwards'):
        list(adapt_events([snapshot('u',1000),snapshot('d',999)],
                          [market()],start_ms=0,end_ms=2000))
    with pytest.raises(ValueError,match='coverage end'):
        list(adapt_events([snapshot('u',2001)],[market()],start_ms=0,end_ms=2000))


def test_feature_availability_no_future_and_metadata_not_preannounced():
    m=market(); m['recv_ms']=2000
    events=[{'kind':'spot_connection','recv_ms':0},spot(0,100),snapshot('u',1999)]
    rows=list(adapt_events(events,[m],start_ms=0,end_ms=2000))
    decisions=[row for row in rows if row['type']=='decision']
    assert len(decisions)==1 and decisions[0]['recv_ms']==2000
    assert all(value is None or value<=2000
               for value in decisions[0]['feature_available_ms'].values())


def test_raw_metadata_uses_first_local_receipt_without_outcome_backdating(tmp_path):
    base={'slug':'btc-updown-5m-300','conditionId':'m','outcomes':'["Up","Down"]',
          'clobTokenIds':'["u","d"]','feeSchedule':{'rate':.07,'exponent':1,'takerOnly':True},
          '_recorded_at_ms':310000,'outcomePrices':'["0.5","0.5"]'}
    final=dict(base,_recorded_at_ms=900000,outcomePrices='["1","0"]',closed=True)
    path=tmp_path/'markets.jsonl.gz'
    with gzip.open(path,'wt') as stream:
        for row in (final,base):
            stream.write(json.dumps(row)+'\n')
    meta=first_raw_metadata(path)['m']
    assert meta['recv_ms']==310000
    assert meta['fee_rate']==.07 and meta['fee_provenance']=='raw_gamma_feeSchedule'
    assert 'winner' not in meta and 'outcomePrices' not in meta


def test_bbo_cannot_mix_with_trade_returns_and_flow_is_quote_weighted():
    adapter=Adapter([market()])
    list(adapter.consume({'kind':'spot_connection','recv_ms':0}))
    list(adapter.consume(spot(14000,100,'BUY')))
    list(adapter.consume(spot(15000,200,'SELL')))
    list(adapter.consume({'kind':'spot_bbo','recv_ms':15999,'bid':999,'ask':1001}))
    row=list(adapter.decisions(16000))[0]
    assert adapter.source_price==200
    assert row['features']['flow_15s']==pytest.approx(-1/3)


def test_projection_processes_raw_updates_at_exact_predeclared_match_times():
    events=[snapshot('u',0,.5),snapshot('d',0,.5),
            snapshot('u',450,.6),snapshot('d',450,.4),
            snapshot('u',550,.7),snapshot('d',600,.3)]
    rows=list(adapt_events(events,[market()],start_ms=0,end_ms=1000,latency_projection=True))
    books={row['recv_ms']:row for row in rows if row['type']=='book'}
    assert set(books)=={0,500,600,1000}
    assert books[500]['up_asks'][0][0]==.61
    assert books[500]['up_recv_ms']==450
    assert books[600]['up_asks'][0][0]==.71
    assert books[600]['down_recv_ms']==600
    assert books[1000]['healthy'] is False  # sample does not refresh independent clocks
    assert [row['recv_ms'] for row in rows if row['type']=='decision']==[0,1000]
