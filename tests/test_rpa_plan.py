"""区间计划的纯逻辑检查：解析拒绝、切分、重复时段偏移、摘要与分钟穷举。"""
import unittest
from datetime import datetime, timezone
from zoneinfo import ZoneInfo

from rpa.collect import aggregate
from rpa.plan import InvalidRangeError, parse_iso_minute, plan_segments

TZ = ZoneInfo("America/New_York")


def exhaustive_minutes(start_raw, end_raw):
    """对短区间穷举绝对分钟作为对照真值。"""
    first = int(datetime.fromisoformat(start_raw).timestamp()) // 60
    stop = int(datetime.fromisoformat(end_raw).timestamp()) // 60
    return list(range(first, stop))


class ParseTests(unittest.TestCase):
    def test_valid_offset_and_z(self):
        self.assertEqual(0, parse_iso_minute("2026-11-01T01:00:00-04:00", "起点").second)
        self.assertIsNotNone(parse_iso_minute("2026-11-01T06:00:00Z", "起点").tzinfo)

    def test_reject_naive(self):
        with self.assertRaises(InvalidRangeError):
            parse_iso_minute("2026-11-01T01:00", "起点")

    def test_reject_non_minute(self):
        for raw in (
            "2026-11-01T01:00:30-04:00",
            "2026-11-01T01:00:00.5-04:00",
            "2026-11-01T01:00:00.000001Z",
        ):
            with self.assertRaises(InvalidRangeError, msg=raw):
                parse_iso_minute(raw, "终点")

    def test_reject_garbage(self):
        with self.assertRaises(InvalidRangeError):
            parse_iso_minute("不是时间", "起点")


class PlanTests(unittest.TestCase):
    def plan(self, start_raw, end_raw):
        return plan_segments(
            parse_iso_minute(start_raw, "起点"),
            parse_iso_minute(end_raw, "终点"),
            TZ,
            supported_year=2026,
        )

    def test_empty_range_no_segments(self):
        self.assertEqual([], self.plan("2026-06-10T08:00:00-04:00", "2026-06-10T08:00:00-04:00"))
        self.assertEqual([], self.plan("2026-06-10T09:00:00-04:00", "2026-06-10T08:00:00-04:00"))

    def test_single_day_exclusive_end(self):
        (seg,) = self.plan("2026-06-10T08:15:00-04:00", "2026-06-10T09:00:00-04:00")
        self.assertEqual("2026-06-10", seg.date)
        self.assertEqual(("08:15", "08:59"), (seg.start_time, seg.end_time))
        self.assertEqual((0, 0), (seg.start_fold, seg.end_fold))
        self.assertEqual(
            "2026-06-10T12:15:00.000Z|2026-06-10T12:59:00.000Z", seg.expected_summary
        )
        self.assertEqual(45, len(seg.expected_minutes))

    def test_cross_local_midnight(self):
        segs = self.plan("2026-06-10T23:30:00-04:00", "2026-06-11T00:30:00-04:00")
        self.assertEqual(2, len(segs))
        self.assertEqual(("2026-06-10", "23:30", "23:59"), (segs[0].date, segs[0].start_time, segs[0].end_time))
        self.assertEqual(("2026-06-11", "00:00", "00:29"), (segs[1].date, segs[1].start_time, segs[1].end_time))

    def test_fallback_first_occurrence(self):
        # 回拨日第一个 01 点小时（UTC−04）
        (seg,) = self.plan("2026-11-01T01:00:00-04:00", "2026-11-01T01:00:00-05:00")
        self.assertEqual(("01:00", 0, "01:59", 0), (seg.start_time, seg.start_fold, seg.end_time, seg.end_fold))
        self.assertEqual("2026-11-01T05:00:00.000Z|2026-11-01T05:59:00.000Z", seg.expected_summary)
        self.assertEqual(60, len(seg.expected_minutes))

    def test_fallback_second_occurrence(self):
        # 回拨日第二个 01 点小时（UTC−05）
        (seg,) = self.plan("2026-11-01T01:00:00-05:00", "2026-11-01T02:00:00-05:00")
        self.assertEqual(("01:00", 1, "01:59", 1), (seg.start_time, seg.start_fold, seg.end_time, seg.end_fold))
        self.assertEqual("2026-11-01T06:00:00.000Z|2026-11-01T06:59:00.000Z", seg.expected_summary)

    def test_fallback_spanning_both_occurrences(self):
        (seg,) = self.plan("2026-11-01T00:30:00-04:00", "2026-11-01T01:30:00-05:00")
        self.assertEqual(("00:30", 0), (seg.start_time, seg.start_fold))
        self.assertEqual(("01:29", 1), (seg.end_time, seg.end_fold))
        self.assertEqual(120, len(seg.expected_minutes))

    def test_full_fallback_day_has_1500_minutes(self):
        (seg,) = self.plan("2026-11-01T00:00:00-04:00", "2026-11-02T00:00:00-05:00")
        self.assertEqual(("00:00", "23:59"), (seg.start_time, seg.end_time))
        self.assertEqual(1500, len(seg.expected_minutes))

    def test_spring_forward_day_has_1380_minutes(self):
        (seg,) = self.plan("2026-03-08T00:00:00-05:00", "2026-03-09T00:00:00-04:00")
        self.assertEqual(("00:00", "23:59"), (seg.start_time, seg.end_time))
        self.assertEqual(1380, len(seg.expected_minutes))
        self.assertEqual("2026-03-08T05:00:00.000Z|2026-03-09T03:59:00.000Z", seg.expected_summary)

    def test_cross_transition_three_segments(self):
        segs = self.plan("2026-03-07T12:00:00-05:00", "2026-03-09T12:00:00-04:00")
        self.assertEqual(["2026-03-07", "2026-03-08", "2026-03-09"], [s.date for s in segs])

    def test_unsupported_year_rejected(self):
        with self.assertRaises(InvalidRangeError):
            self.plan("2027-01-01T00:00:00-05:00", "2027-01-01T01:00:00-05:00")

    def test_segments_tile_range_without_gaps_or_overlap(self):
        # 跨回拨、跨春拨、重复时段内部的范围：分段分钟拼起来必须恰好等于区间分钟
        for start_raw, end_raw in (
            ("2026-10-31T23:00:00-04:00", "2026-11-02T00:30:00-05:00"),
            ("2026-03-07T23:30:00-05:00", "2026-03-09T00:30:00-04:00"),
            ("2026-11-01T01:15:00-04:00", "2026-11-01T01:45:00-05:00"),
        ):
            segs = self.plan(start_raw, end_raw)
            minutes = [m for seg in segs for m in seg.expected_minutes]
            self.assertEqual(
                exhaustive_minutes(start_raw, end_raw), minutes, f"{start_raw} ~ {end_raw}"
            )


class AggregateTests(unittest.TestCase):
    def test_dedupe_keeps_provenance(self):
        rows_a = [{"id": "M-100", "utc": "1970-01-01T01:40:00.000Z"}]
        rows_b = [
            {"id": "M-100", "utc": "1970-01-01T01:40:00.000Z"},
            {"id": "M-101", "utc": "1970-01-01T01:41:00.000Z"},
        ]
        start = datetime.fromtimestamp(100 * 60, tz=timezone.utc)
        end = datetime.fromtimestamp(102 * 60, tz=timezone.utc)
        records, duplicates, missing, extra = aggregate(
            [(0, rows_a), (1, rows_b)], start, end
        )
        self.assertEqual(1, duplicates)
        self.assertEqual(["M-100", "M-101"], [r["id"] for r in records])
        self.assertEqual([0, 1], records[0]["segments"])
        self.assertEqual([1], records[1]["segments"])
        self.assertEqual([], missing)
        self.assertEqual([], extra)

    def test_gap_is_reported_not_masked(self):
        rows = [{"id": "M-100", "utc": "1970-01-01T01:40:00.000Z"}]
        start = datetime.fromtimestamp(100 * 60, tz=timezone.utc)
        end = datetime.fromtimestamp(103 * 60, tz=timezone.utc)
        records, duplicates, missing, extra = aggregate([(0, rows)], start, end)
        self.assertEqual(2, len(missing))
        self.assertEqual([], extra)


if __name__ == "__main__":
    unittest.main()
