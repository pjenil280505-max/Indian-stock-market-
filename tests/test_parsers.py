"""Parser tests against the real file shapes observed in Phase 0."""
import io
import zipfile
from datetime import date

from src.sources.nse import (
    parse_corporate_actions,
    parse_delivery_bhavcopy,
    parse_equity_list,
    parse_udiff_bhavcopy,
)
from src.sources.upstox import instrument_key, parse_candles, split_window

# Real header and rows as served by NSE (note the leading spaces in headers).
EQUITY_L = (
    b"SYMBOL,NAME OF COMPANY, SERIES, DATE OF LISTING, PAID UP VALUE,"
    b" MARKET LOT, ISIN NUMBER, FACE VALUE\n"
    b"20MICRONS,20 Microns Limited,EQ,06-OCT-2008,5,1,INE144J01027,5\n"
    b"RELIANCE,Reliance Industries Limited,EQ,29-NOV-1995,10,1,INE002A01018,10\n"
    b"SOMEBE,Some BE Co,BE,01-JAN-2010,10,1,INE999Z01011,10\n"
)

DELIVERY = (
    b"SYMBOL, SERIES, DATE1, PREV_CLOSE, OPEN_PRICE, HIGH_PRICE, LOW_PRICE,"
    b" LAST_PRICE, CLOSE_PRICE, AVG_PRICE, TTL_TRD_QNTY, TURNOVER_LACS,"
    b" NO_OF_TRADES, DELIV_QTY, DELIV_PER\n"
    b"RELIANCE, EQ, 25-Oct-2024, 2679.60, 2687.00, 2688.70, 2644.00, 2656.30,"
    b" 2655.70, 2659.80, 9298748, 247328.26, 388238, 5955260, 64.04\n"
    b"20MICRONS, EQ, 25-Oct-2024, 205.71, 207.77, 209.00, 204.62, 206.50,"
    b" 208.28, 207.02, 45744, 94.70, 1711, 21567, 47.15\n"
    b"NIFTYFUT, FUTIDX, 25-Oct-2024, 1, 1, 1, 1, 1, 1, 1, 1, 1, 1, 1, 1\n"
)


class TestEquityList:
    def test_parses_all_equity_rows(self):
        rows = parse_equity_list(EQUITY_L)
        assert len(rows) == 3

    def test_extracts_isin_and_listing_date(self):
        rows = {r.symbol: r for r in parse_equity_list(EQUITY_L)}
        assert rows["RELIANCE"].isin == "INE002A01018"
        assert rows["RELIANCE"].listing_date == date(1995, 11, 29)
        assert rows["20MICRONS"].face_value == 5.0

    def test_keeps_series_distinction(self):
        rows = {r.symbol: r for r in parse_equity_list(EQUITY_L)}
        assert rows["SOMEBE"].series == "BE"

    def test_skips_rows_without_isin(self):
        broken = EQUITY_L + b"BADROW,No Isin Co,EQ,01-JAN-2020,10,1,,10\n"
        assert len(parse_equity_list(broken)) == 3

    def test_empty_input(self):
        assert parse_equity_list(b"") == []


class TestDeliveryBhavcopy:
    def test_parses_equity_rows_only(self):
        bars = parse_delivery_bhavcopy(DELIVERY, date(2024, 10, 25))
        assert len(bars) == 2, "FUTIDX row must be excluded"

    def test_reads_the_real_reliance_values(self):
        bars = {b.symbol: b for b in parse_delivery_bhavcopy(DELIVERY, date(2024, 10, 25))}
        r = bars["RELIANCE"]
        assert (r.open, r.high, r.low, r.close) == (2687.00, 2688.70, 2644.00, 2655.70)
        assert r.volume == 9298748

    def test_captures_delivery_percentage(self):
        """Delivery data is available from NSE and from no broker API tested."""
        bars = {b.symbol: b for b in parse_delivery_bhavcopy(DELIVERY, date(2024, 10, 25))}
        assert bars["RELIANCE"].deliv_pct == 64.04
        assert bars["RELIANCE"].deliv_qty == 5955260

    def test_turnover_converted_from_lakhs_to_rupees(self):
        bars = {b.symbol: b for b in parse_delivery_bhavcopy(DELIVERY, date(2024, 10, 25))}
        assert bars["RELIANCE"].turnover == 247328.26 * 100_000

    def test_assigns_the_requested_trade_date(self):
        bars = parse_delivery_bhavcopy(DELIVERY, date(2024, 10, 25))
        assert all(b.trade_date == date(2024, 10, 25) for b in bars)


class TestUdiffBhavcopy:
    @staticmethod
    def _zip(csv_text: bytes) -> bytes:
        buf = io.BytesIO()
        with zipfile.ZipFile(buf, "w") as z:
            z.writestr("BhavCopy.csv", csv_text)
        return buf.getvalue()

    CSV = (
        b"TradDt,FinInstrmTp,ISIN,TckrSymb,SctySrs,OpnPric,HghPric,LwPric,"
        b"ClsPric,LastPric,PrvsClsgPric,TtlTradgVol,TtlTrfVal,TtlNbOfTxsExctd\n"
        b"2026-09-18,STK,INE002A01018,RELIANCE,EQ,1245.0,1247.3,1226.4,1226.4,"
        b"1226.4,1245.0,15122715,1000000,50000\n"
        b"2026-09-18,IDX,INE000000000,NIFTY,EQ,1,1,1,1,1,1,1,1,1\n"
    )

    def test_parses_stock_rows_and_skips_indices(self):
        bars = parse_udiff_bhavcopy(self._zip(self.CSV), date(2026, 9, 18))
        assert len(bars) == 1
        assert bars[0].symbol == "RELIANCE"

    def test_carries_isin(self):
        bars = parse_udiff_bhavcopy(self._zip(self.CSV), date(2026, 9, 18))
        assert bars[0].isin == "INE002A01018"


class TestCorporateActions:
    def test_parses_ex_dates(self):
        body = (
            b'[{"symbol":"AMBIKCO","comp":"Ambika Cotton Mills Limited",'
            b'"exDate":"22-Sep-2026","faceVal":"10","isin":"INE540G01014",'
            b'"subject":"Dividend Rs 15"}]'
        )
        actions = parse_corporate_actions(body)
        assert len(actions) == 1
        assert actions[0].ex_date == date(2026, 9, 22)
        assert actions[0].isin == "INE540G01014"

    def test_skips_placeholder_dates(self):
        assert parse_corporate_actions(b'[{"symbol":"X","exDate":"-"}]') == []

    def test_malformed_json_is_not_fatal(self):
        assert parse_corporate_actions(b"not json") == []


class TestUpstoxParsing:
    def test_instrument_key_format(self):
        assert instrument_key("INE002A01018") == "NSE_EQ|INE002A01018"

    def test_parses_candles_oldest_first(self):
        body = (
            b'{"status":"success","data":{"candles":['
            b'["2026-09-18T00:00:00+05:30",1245.0,1247.3,1226.4,1226.4,15122715,0],'
            b'["2026-09-17T00:00:00+05:30",1244.8,1253.4,1238.5,1243.9,7752895,0]]}}'
        )
        bars = parse_candles(body, "INE002A01018")
        assert [b.trade_date for b in bars] == [date(2026, 9, 17), date(2026, 9, 18)]
        assert bars[1].close == 1226.4

    def test_error_response_yields_nothing(self):
        assert parse_candles(b'{"status":"error","errors":[]}', "X") == []

    def test_malformed_candle_rows_are_skipped(self):
        body = b'{"data":{"candles":[["bad"],["2026-09-18T00:00:00+05:30",1,2,0.5,1.5,10,0]]}}'
        assert len(parse_candles(body, "X")) == 1


class TestWindowSplitting:
    def test_short_range_is_one_chunk(self):
        chunks = split_window(date(2026, 1, 1), date(2026, 3, 1))
        assert len(chunks) == 1

    def test_long_range_is_chunked_under_the_cap(self):
        """Upstox rejects a >5y window for days/1 with UDAPI1148."""
        chunks = split_window(date(2000, 1, 1), date(2026, 9, 1))
        assert len(chunks) > 1
        for start, end in chunks:
            assert (end - start).days <= 1800

    def test_chunks_are_contiguous_and_cover_the_range(self):
        start, end = date(2000, 1, 1), date(2026, 9, 1)
        chunks = split_window(start, end)
        assert chunks[0][0] == start
        assert chunks[-1][1] == end
        for earlier, later in zip(chunks, chunks[1:]):
            assert (later[0] - earlier[1]).days == 1

    def test_inverted_range_yields_nothing(self):
        assert split_window(date(2026, 1, 2), date(2026, 1, 1)) == []
