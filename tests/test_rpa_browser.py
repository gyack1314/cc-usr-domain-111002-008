"""浏览器联调检查：真实点击日期控件完成三个场景，并穷举绝对分钟对照。

- 回拨日：重复 01 点的第一次／第二次偏移选择。
- 跨当地午夜：跨春拨日的多日子区间与月份导航。
- 浏览器时区不同：浏览器本机时区与门户时区不一致时结果不变。
另覆盖：空范围不发请求、非整分钟拒绝、不存在时间停止该段、旧结果不冒充成功。
"""
import json
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

from playwright.sync_api import sync_playwright

from portal.__main__ import running
from rpa.collect import collect
from rpa.plan import InvalidRangeError, Segment, plan_segments
from rpa.portal import PortalDriver, SegmentDiscrepancy

TZ = ZoneInfo("America/New_York")
UTC = timezone.utc


def exhaustive_ids(start_raw, end_raw):
    """对短区间穷举绝对分钟作为对照真值（不代替浏览器联调，仅用于核对）。"""
    first = int(datetime.fromisoformat(start_raw).timestamp()) // 60
    stop = int(datetime.fromisoformat(end_raw).timestamp()) // 60
    return [f"M-{m}" for m in range(first, stop)]


class BrowserCollectTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.driver = sync_playwright().start()
        cls.browser = cls.driver.chromium.launch(headless=True, args=["--no-sandbox"])

    @classmethod
    def tearDownClass(cls):
        cls.browser.close()
        cls.driver.stop()

    def setUp(self):
        self.server = running()
        self.base, self.state = self.server.__enter__()

    def tearDown(self):
        self.server.__exit__(None, None, None)

    def run_collect(self, start, end, **kwargs):
        return collect(self.base, start, end, browser=self.browser, **kwargs)

    def assert_exhaustive(self, result, start, end):
        self.assertEqual("ok", result.status)
        self.assertEqual(exhaustive_ids(start, end), [r["id"] for r in result.records])
        self.assertEqual([], result.missing_minutes)
        self.assertEqual([], result.extra_minutes)

    # 场景一：回拨日，重复 01 点的第一次／第二次选择
    def test_fallback_day_repeated_hour(self):
        cases = [
            # (起点, 终点(不含), 每段期望的 (start_fold, end_fold))
            ("2026-11-01T01:00:00-04:00", "2026-11-01T01:00:00-05:00", [(0, 0)]),
            ("2026-11-01T01:00:00-05:00", "2026-11-01T02:00:00-05:00", [(1, 1)]),
            ("2026-11-01T00:30:00-04:00", "2026-11-01T01:30:00-05:00", [(0, 1)]),
            ("2026-10-31T23:00:00-04:00", "2026-11-01T03:30:00-05:00", [(0, 0), (0, 0)]),
        ]
        for start, end, folds in cases:
            with self.subTest(start=start, end=end):
                result = self.run_collect(start, end)
                self.assert_exhaustive(result, start, end)
                self.assertEqual(len(folds), len(result.segments))
                for seg, (sf, ef) in zip(result.segments, folds):
                    self.assertEqual(sf, seg["start_fold"])
                    self.assertEqual(ef, seg["end_fold"])
                    execution = seg["execution"]
                    self.assertEqual("ok", execution["status"])
                    self.assertEqual(seg["expected_utc_summary"], execution["utc_summary"])
                    self.assertEqual(execution["utc_summary"], execution["results_signature"])
                    label = execution["readback"]["end_fold_label"]
                    self.assertIn("第一次" if ef == 0 else "第二次", label)
        # 1+1+1+2 段，每次成功查询都应在来源动作记录中留一条
        queries = [a for a in self.state["actions"] if a.get("name") == "query"]
        self.assertEqual(5, len(queries))

    # 场景二：跨当地午夜（含春拨日，02 点不存在）
    def test_cross_local_midnight_spring_forward(self):
        start, end = "2026-03-07T23:30:00-05:00", "2026-03-09T00:30:00-04:00"
        result = self.run_collect(start, end)
        self.assert_exhaustive(result, start, end)
        self.assertEqual(
            ["2026-03-07", "2026-03-08", "2026-03-09"],
            [seg["date"] for seg in result.segments],
        )
        counts = [seg["execution"]["record_count"] for seg in result.segments]
        self.assertEqual([30, 1380, 30], counts)  # 春拨日只有 23 小时
        # 日历默认停在 11 月，必须真实逐月导航到 3 月
        navigation = result.segments[0]["execution"]["readback"]["start_month_navigation"]
        self.assertEqual("2026-11", navigation["from"])
        self.assertEqual("2026-03", navigation["to"])
        self.assertEqual(8, navigation["clicks"])
        # 没有任何一段请求了不存在的 02 点
        for seg in result.segments:
            if seg["date"] == "2026-03-08":
                self.assertNotEqual("02", seg["start_time"][:2])
                self.assertNotEqual("02", seg["end_time"][:2])

    # 场景三：浏览器本机时区与门户时区不同
    def test_browser_timezone_differs(self):
        start, end = "2026-11-01T00:30:00-04:00", "2026-11-01T01:30:00-05:00"
        result = self.run_collect(start, end, browser_timezone="Asia/Shanghai")
        self.assert_exhaustive(result, start, end)
        self.assertEqual("Asia/Shanghai", result.browser_timezone)
        self.assertEqual("America/New_York", result.timezone)
        # 选中的仍是门户当地（纽约）的日期与偏移，不受浏览器时区影响
        seg = result.segments[0]
        self.assertEqual("2026-11-01", seg["date"])
        self.assertEqual("01:29", seg["end_time"])
        self.assertEqual(1, seg["end_fold"])
        self.assertEqual("2026-11-01", seg["execution"]["readback"]["end_date"])

    def test_empty_range_sends_no_request(self):
        for start, end in (
            ("2026-06-10T08:00:00-04:00", "2026-06-10T08:00:00-04:00"),
            ("2026-06-10T09:00:00-04:00", "2026-06-10T08:00:00-04:00"),
        ):
            result = self.run_collect(start, end)
            self.assertEqual("empty", result.status)
        self.assertEqual([], self.state["actions"])

    def test_non_minute_input_rejected_before_any_request(self):
        with self.assertRaises(InvalidRangeError):
            self.run_collect("2026-06-10T08:00:30-04:00", "2026-06-10T09:00:00-04:00")
        with self.assertRaises(InvalidRangeError):
            self.run_collect("2026-06-10T08:00:00-04:00", "2026-06-10T09:00:00.5-04:00")
        self.assertEqual([], self.state["actions"])

    def test_evidence_files_written(self):
        start, end = "2026-11-01T01:00:00-04:00", "2026-11-01T01:00:00-05:00"
        with tempfile.TemporaryDirectory() as tmp:
            result = self.run_collect(start, end, evidence_dir=tmp, screenshots=True)
            self.assertEqual("ok", result.status)
            plan = json.loads((Path(tmp) / "plan.json").read_text(encoding="utf-8"))
            self.assertEqual("America/New_York", plan["portal_timezone"])
            self.assertEqual(1, len(plan["segments"]))
            summary = json.loads((Path(tmp) / "summary.json").read_text(encoding="utf-8"))
            self.assertEqual("ok", summary["status"])
            self.assertEqual(60, summary["record_count"])
            lines = (Path(tmp) / "records.jsonl").read_text(encoding="utf-8").splitlines()
            self.assertEqual(60, len(lines))
            first = json.loads(lines[0])
            expected_first = f"M-{int(datetime.fromisoformat(start).timestamp()) // 60}"
            self.assertEqual(expected_first, first["id"])
            self.assertEqual([0], first["segments"])
            self.assertTrue((Path(tmp) / "segment-0.png").is_file())


class BrowserDiscrepancyTests(unittest.TestCase):
    """差异场景：不凭输入值判断成功，停止该段并记录。"""

    @classmethod
    def setUpClass(cls):
        cls.driver = sync_playwright().start()
        cls.browser = cls.driver.chromium.launch(headless=True, args=["--no-sandbox"])

    @classmethod
    def tearDownClass(cls):
        cls.browser.close()
        cls.driver.stop()

    def setUp(self):
        self.server = running()
        self.base, self.state = self.server.__enter__()
        self.context = self.browser.new_context(viewport={"width": 1280, "height": 900})
        self.page = self.context.new_page()
        self.driver = PortalDriver(self.page, timeout_ms=4000)
        self.driver.open(self.base)

    def tearDown(self):
        self.context.close()
        self.server.__exit__(None, None, None)

    def make_segment(self, start_raw, end_raw):
        segs = plan_segments(
            datetime.fromisoformat(start_raw),
            datetime.fromisoformat(end_raw),
            TZ,
            supported_year=2026,
        )
        self.assertEqual(1, len(segs))
        return segs[0]

    def run_segment(self, seg):
        evidence = {"index": seg.index, "intent": seg.intent(), "readback": {}}
        self.driver.fill_segment(seg, evidence)
        rows = self.driver.submit_and_verify(seg, evidence)
        return evidence, rows

    def test_nonexistent_local_time_stops_segment(self):
        # 先成功一段，让页面留下旧的结果绑定
        good = self.make_segment("2026-11-01T01:00:00-04:00", "2026-11-01T01:30:00-04:00")
        _, rows = self.run_segment(good)
        self.assertEqual(30, len(rows))

        # 2026-03-08 02 点不存在于绝对时间轴，计划器永远不会生成；
        # 手工构造该段，验证门户报错时执行停止该段并记录差异，
        # 且证据里能看到旧结果绑定没有被当作本段结果。
        bad = Segment(
            index=1,
            date="2026-03-08",
            start_time="02:00",
            start_fold=0,
            end_time="02:29",
            end_fold=0,
            abs_start=datetime(2026, 3, 8, 7, 0, tzinfo=UTC),
            abs_end_exclusive=datetime(2026, 3, 8, 7, 30, tzinfo=UTC),
            query_end=datetime(2026, 3, 8, 7, 29, tzinfo=UTC),
        )
        evidence = {"index": 1, "intent": bad.intent(), "readback": {}}
        self.driver.fill_segment(bad, evidence)  # 控件接受填写，不代表成功
        with self.assertRaises(SegmentDiscrepancy) as ctx:
            self.driver.submit_and_verify(bad, evidence)
        self.assertIn("当地时间不存在", str(ctx.exception))
        self.assertEqual("当地时间不存在", ctx.exception.evidence["status_text"])
        # 结果区仍绑定上一段的旧摘要，明确记录，不冒充本段成功
        self.assertEqual(good.expected_summary, ctx.exception.evidence["results_signature"])

    def test_unconfirmed_query_is_not_accepted(self):
        # 第一段成功；第二段提交后门户未确认（拦截 /api/action），
        # 状态不会变为完成，必须判为差异而不是凭输入值认为成功。
        first = self.make_segment("2026-11-01T01:00:00-04:00", "2026-11-01T01:30:00-04:00")
        self.run_segment(first)

        self.page.route("**/api/action", lambda route: route.abort())
        second = self.make_segment("2026-11-01T02:00:00-05:00", "2026-11-01T02:30:00-05:00")
        evidence = {"index": 1, "intent": second.intent(), "readback": {}}
        self.driver.fill_segment(second, evidence)
        with self.assertRaises(SegmentDiscrepancy) as ctx:
            self.driver.submit_and_verify(second, evidence)
        self.assertIn("未在限时内确认", str(ctx.exception))
        self.assertNotEqual("查询完成，包含两个端点", ctx.exception.evidence["status_text"])


if __name__ == "__main__":
    unittest.main()
