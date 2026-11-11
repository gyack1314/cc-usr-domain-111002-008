"""浏览器填写与核对：真实点击日期控件，读回核对，不调用页面内部函数。

填写路径与人工一致：月份导航 -> 日期格 -> 时间选项 -> 偏移选项。每一步都
读回控件实际值；提交后先核对 UTC 摘要与结果绑定（data-signature），再读取
结果记录。任何不一致都抛出 SegmentDiscrepancy 并携带差异证据，调用方停止
该段，不凭输入值判断已经成功。
"""
from __future__ import annotations

import json
import threading
from datetime import datetime, timezone
from time import monotonic

from playwright.sync_api import Page, TimeoutError as PlaywrightTimeoutError

from .plan import Segment

LOADING_STATUS = "页面加载中"
SUCCESS_STATUS = "查询完成，包含两个端点"
ERROR_STATUSES = ("当地时间不存在", "单次查询只能一个当地自然日", "终点早于起点")

# 等待门户确认：结果绑定到预期摘要且状态为完成；或门户明确报错。
# 旧结果的 signature 与预期不同，不会被误认为成功。
_CONFIRM_JS = (
    """
(expected) => {
    const status = document.querySelector('#status').textContent;
    const summary = document.querySelector('#utc-summary').textContent;
    const signature = document.querySelector('#results').dataset.signature || '';
    const errors = %s;
    if (errors.includes(status)) return {outcome: 'error', status};
    if (status === %s && summary === expected && signature === expected)
        return {outcome: 'ok', status};
    return null;
}
"""
    % (json.dumps(list(ERROR_STATUSES), ensure_ascii=False), json.dumps(SUCCESS_STATUS, ensure_ascii=False))
)


class SegmentDiscrepancy(RuntimeError):
    """页面表现与段意图不一致：停止该段并携带差异证据。"""

    def __init__(self, message: str, evidence: dict):
        super().__init__(message)
        self.evidence = evidence


def _minute_of_z(text: str) -> int:
    parsed = datetime.strptime(text, "%Y-%m-%dT%H:%M:%S.000Z").replace(tzinfo=timezone.utc)
    return int(parsed.timestamp()) // 60


class PortalDriver:
    """在门户页面上执行一个子区间：填写、提交、核对、读取。"""

    def __init__(self, page: Page, timeout_ms: int = 10000):
        self.page = page
        self.timeout_ms = timeout_ms

    def open(self, base_url: str) -> str:
        """打开门户，返回页面显示的门户时区名。"""
        page = self.page
        page.goto(base_url)
        page.wait_for_function(
            "(loading) => document.querySelector('#status').textContent !== loading",
            arg=LOADING_STATUS,
            timeout=self.timeout_ms,
        )
        tz_text = (page.locator("#timezone").text_content() or "").strip()
        # 页面显示形如 “门户时区 America/New_York”
        return tz_text.split()[-1] if tz_text else ""

    def fill_segment(self, seg: Segment, evidence: dict) -> None:
        """通过月份导航、日期格、时间与偏移选项填写两个端点并读回核对。"""
        for target, hhmm, fold in (
            ("start", seg.start_time, seg.start_fold),
            ("end", seg.end_time, seg.end_fold),
        ):
            self._pick_date(target, seg.date, evidence)
            self._select_option(f"#{target}-time", hhmm, evidence)
            self._select_option(f"#{target}-fold", str(fold), evidence)
            # 记录偏移选项的可见文本（第一次 UTC−04／第二次 UTC−05）
            evidence["readback"][f"{target}_fold_label"] = self.page.locator(
                f"#{target}-fold"
            ).evaluate("el => el.options[el.selectedIndex].text")

    def submit_and_verify(self, seg: Segment, evidence: dict) -> list[dict]:
        """提交查询；核对 UTC 摘要与结果绑定后，再读取结果记录。

        门户先更新摘要与结果绑定、再发 /api/action、最后才置完成状态，
        而连续两段的成功状态文本相同，仅凭 DOM 无法区分“本段已确认”与
        “旧成功状态 + 新摘要”的瞬时组合。因此分两个阶段：先等摘要/绑定/
        状态组合或报错，再等本查询的 /api/action 响应到达（网络层观察，
        不调用页面内部函数，也不借用运维入口）。
        """
        expected = seg.expected_summary
        evidence["expected_utc_summary"] = expected
        action_seen = threading.Event()

        def on_response(response):
            if response.request.method == "POST" and response.url.endswith("/api/action"):
                action_seen.set()

        self.page.on("response", on_response)
        started = monotonic()
        try:
            self.page.click("#query")
            try:
                handle = self.page.wait_for_function(
                    _CONFIRM_JS, arg=expected, timeout=self.timeout_ms
                )
                outcome = handle.json_value()
            except PlaywrightTimeoutError as exc:
                self._capture_page_state(evidence)
                raise SegmentDiscrepancy(
                    "门户未在限时内确认查询（页面上可能仍是旧结果）", evidence
                ) from exc
            evidence["status_text"] = outcome["status"]
            self._capture_page_state(evidence)
            if outcome["outcome"] != "ok":
                raise SegmentDiscrepancy(f"门户拒绝该段：{outcome['status']}", evidence)
            remaining = self.timeout_ms / 1000 - (monotonic() - started)
            if not action_seen.wait(timeout=max(0.05, remaining)):
                self._capture_page_state(evidence)
                raise SegmentDiscrepancy(
                    "门户未在限时内确认来源动作（/api/action 未完成），"
                    "结果不作为成功",
                    evidence,
                )
        finally:
            self.page.remove_listener("response", on_response)
        if evidence["utc_summary"] != expected or evidence["results_signature"] != expected:
            raise SegmentDiscrepancy("UTC 摘要或结果绑定与意图不符", evidence)
        records = self.page.eval_on_selector_all(
            "#results p",
            "rows => rows.map(row => ({id: row.dataset.recordId, utc: row.dataset.utc}))",
        )
        evidence["record_count"] = len(records)
        evidence["first_record"] = records[0] if records else None
        evidence["last_record"] = records[-1] if records else None
        expected_ids = [f"M-{minute}" for minute in seg.expected_minutes]
        actual_ids = [row["id"] for row in records]
        if actual_ids != expected_ids:
            raise SegmentDiscrepancy(
                f"结果记录与区间不符：期望 {len(expected_ids)} 条，实际 {len(actual_ids)} 条",
                evidence,
            )
        for row in records:
            if _minute_of_z(row["utc"]) != int(row["id"][2:]):
                raise SegmentDiscrepancy(
                    f"记录 {row['id']} 的 UTC 标注 {row['utc']} 与编号不一致", evidence
                )
        return records

    def _capture_page_state(self, evidence: dict) -> None:
        """把当前页面确认状态原样记入证据（可能仍是旧结果）。"""
        evidence["status_text"] = self.page.locator("#status").text_content()
        evidence["utc_summary"] = self.page.locator("#utc-summary").text_content()
        evidence["results_signature"] = self.page.locator("#results").get_attribute(
            "data-signature"
        )

    def _pick_date(self, target: str, date: str, evidence: dict) -> None:
        """打开日历对话框，逐月导航到目标月，点击日期格，读回日期框。"""
        year, month, day = date.split("-")
        wanted = f"{year}-{month}"
        page = self.page
        page.click(f"[data-open-date={target}]")
        page.wait_for_selector("#calendar[open]", timeout=self.timeout_ms)
        shown = (page.locator("#month").text_content() or "").strip()
        navigation = {"from": shown, "clicks": 0}
        evidence["readback"][f"{target}_month_navigation"] = navigation
        for _ in range(24):  # 一年 12 个月，24 次足够收敛
            if shown == wanted:
                break
            page.click("#prev" if shown > wanted else "#next")
            navigation["clicks"] += 1
            now = (page.locator("#month").text_content() or "").strip()
            if now == shown:
                raise SegmentDiscrepancy(
                    f"月份导航停在 {shown}，无法到达 {wanted}", evidence
                )
            shown = now
        else:
            raise SegmentDiscrepancy(f"月份导航未收敛到 {wanted}", evidence)
        navigation["to"] = shown
        page.click(f"[data-day='{int(day)}']")
        readback = page.locator(f"#{target}-date").input_value()
        evidence["readback"][f"{target}_date"] = readback
        if readback != date:
            raise SegmentDiscrepancy(
                f"{target} 日期读回不符：意图 {date}，实际 {readback}"
                "（切月后可能定位到错误日期）",
                evidence,
            )

    def _select_option(self, selector: str, value: str, evidence: dict) -> None:
        """选择下拉项并读回；控件自动纠正会体现为读回值不符。"""
        try:
            self.page.select_option(selector, value, timeout=self.timeout_ms)
        except PlaywrightTimeoutError as exc:
            raise SegmentDiscrepancy(
                f"选项 {value} 在 {selector} 中不可选", evidence
            ) from exc
        readback = self.page.locator(selector).input_value()
        key = selector.lstrip("#").replace("-", "_")
        evidence["readback"][key] = readback
        if readback != value:
            raise SegmentDiscrepancy(
                f"{selector} 读回不符：意图 {value}，实际 {readback}（控件可能自动纠正）",
                evidence,
            )
