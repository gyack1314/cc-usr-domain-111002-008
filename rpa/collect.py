"""采集编排：区间计划 -> 浏览器逐段执行 -> 汇总去重与缺口核对。

- 非整分钟输入在启动浏览器前拒绝；空范围不发任何请求。
- 门户时区以页面显示为准，并与来源约定（/data.json）互相核对。
- 每段先核对选中值与 UTC 摘要，再读取绑定该摘要的结果。
- 任一段出现差异即停止采集，汇总报告缺口，不以去重掩盖。
"""
from __future__ import annotations

import json
import urllib.request
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

from playwright.sync_api import Browser, sync_playwright

from .plan import InvalidRangeError, iso_z, parse_iso_minute, plan_segments
from .portal import PortalDriver, SegmentDiscrepancy


@dataclass
class CollectResult:
    """一次采集的完整结果与核对结论。"""

    status: str  # ok / empty / failed
    start: str
    end: str
    timezone: str | None = None
    browser_timezone: str | None = None
    segments: list[dict] = field(default_factory=list)
    records: list[dict] = field(default_factory=list)
    duplicates_removed: int = 0
    missing_minutes: list[str] = field(default_factory=list)
    extra_minutes: list[str] = field(default_factory=list)
    discrepancy: dict | None = None
    note: str | None = None


def aggregate(
    segment_rows: list[tuple[int, list[dict]]], start: datetime, end: datetime
) -> tuple[list[dict], int, list[str], list[str]]:
    """按（绝对分钟, 来源编号）去重，保留每条命中来自哪个子区间。

    返回 (records, duplicates_removed, missing_minutes, extra_minutes)。
    缺口按绝对分钟穷举对照，如实列出，不以去重掩盖。
    """
    by_key: dict[tuple[int, str], dict] = {}
    records: list[dict] = []
    duplicates = 0
    for seg_index, rows in segment_rows:
        for row in rows:
            minute = int(row["id"][2:])
            key = (minute, row["id"])
            existing = by_key.get(key)
            if existing is not None:
                existing["segments"].append(seg_index)
                duplicates += 1
                continue
            record = {
                "id": row["id"],
                "utc": row["utc"],
                "minute": minute,
                "segments": [seg_index],
            }
            by_key[key] = record
            records.append(record)
    first = int(start.timestamp()) // 60
    stop = int(end.timestamp()) // 60
    expected = set(range(first, stop))
    actual = {rec["minute"] for rec in records}
    missing = [
        iso_z(datetime.fromtimestamp(m * 60, timezone.utc))
        for m in sorted(expected - actual)
    ]
    extra = [
        iso_z(datetime.fromtimestamp(m * 60, timezone.utc))
        for m in sorted(actual - expected)
    ]
    return records, duplicates, missing, extra


def collect(
    base_url: str,
    start_raw: str,
    end_raw: str,
    *,
    browser_timezone: str | None = None,
    headless: bool = True,
    evidence_dir: str | Path | None = None,
    screenshots: bool = False,
    timeout_ms: int = 10000,
    browser: Browser | None = None,
) -> CollectResult:
    """按绝对时间范围 [start, end) 采集门户分钟资料。

    非整分钟输入在启动浏览器前抛出 InvalidRangeError；空范围返回
    status='empty' 且不发任何请求。
    """
    start = parse_iso_minute(start_raw, "起点")
    end = parse_iso_minute(end_raw, "终点")
    if end <= start:
        result = CollectResult(
            status="empty",
            start=start_raw,
            end=end_raw,
            note="起点不早于终点，范围为空，未发请求",
        )
        _write_evidence(evidence_dir, result)
        return result

    contract = _fetch_contract(base_url)
    _check_contract(contract)

    evidence_path = Path(evidence_dir) if evidence_dir else None
    if evidence_path:
        evidence_path.mkdir(parents=True, exist_ok=True)

    result = CollectResult(status="failed", start=start_raw, end=end_raw)
    own_pw = None
    own_browser = None
    if browser is None:
        own_pw = sync_playwright().start()
        own_browser = own_pw.chromium.launch(headless=headless, args=["--no-sandbox"])
        browser = own_browser
    try:
        context_args: dict = {"viewport": {"width": 1280, "height": 900}}
        if browser_timezone:
            context_args["timezone_id"] = browser_timezone
        context = browser.new_context(**context_args)
        try:
            page = context.new_page()
            driver = PortalDriver(page, timeout_ms=timeout_ms)
            displayed_tz = driver.open(base_url)
            result.timezone = displayed_tz
            result.browser_timezone = page.evaluate(
                "() => Intl.DateTimeFormat().resolvedOptions().timeZone"
            )
            if contract["portal_timezone"] != displayed_tz:
                raise InvalidRangeError(
                    f"页面显示时区 {displayed_tz!r} 与来源约定 "
                    f"{contract['portal_timezone']!r} 不一致，拒绝开始"
                )
            try:
                tz = ZoneInfo(displayed_tz)
            except KeyError as exc:
                raise InvalidRangeError(f"页面显示的时区无法识别: {displayed_tz!r}") from exc
            segments = plan_segments(start, end, tz, contract.get("supported_year"))
            result.segments = [seg.intent() for seg in segments]

            segment_rows: list[tuple[int, list[dict]]] = []
            for seg in segments:
                evidence = {"index": seg.index, "intent": seg.intent(), "readback": {}}
                try:
                    driver.fill_segment(seg, evidence)
                    rows = driver.submit_and_verify(seg, evidence)
                except SegmentDiscrepancy as exc:
                    result.segments[seg.index]["execution"] = exc.evidence
                    result.discrepancy = {"segment": seg.index, "message": str(exc)}
                    break
                evidence["status"] = "ok"
                if screenshots and evidence_path:
                    shot = evidence_path / f"segment-{seg.index}.png"
                    page.screenshot(path=str(shot), full_page=True)
                    evidence["screenshot"] = shot.name
                result.segments[seg.index]["execution"] = evidence
                segment_rows.append((seg.index, rows))

            records, duplicates, missing, extra = aggregate(segment_rows, start, end)
            result.records = records
            result.duplicates_removed = duplicates
            result.missing_minutes = missing
            result.extra_minutes = extra
            if result.discrepancy is None and not missing and not extra:
                result.status = "ok"
        finally:
            context.close()
    finally:
        if own_browser is not None:
            own_browser.close()
        if own_pw is not None:
            own_pw.stop()
    _write_evidence(evidence_path, result)
    return result


def _fetch_contract(base_url: str) -> dict:
    """读取页面自身加载的来源约定（非运维入口）。"""
    with urllib.request.urlopen(base_url.rstrip("/") + "/data.json", timeout=10) as resp:
        return json.load(resp)


def _check_contract(contract: dict) -> None:
    if contract.get("record_precision") != "minute":
        raise InvalidRangeError(
            f"来源记录精度不是分钟：{contract.get('record_precision')!r}，拒绝开始"
        )
    if contract.get("inclusive_end") is not True:
        raise InvalidRangeError("门户查询端点约定不是双端包含，等价转换不成立，拒绝开始")
    if not isinstance(contract.get("portal_timezone"), str):
        raise InvalidRangeError("来源约定缺少门户时区，拒绝开始")


def _write_evidence(evidence_dir: str | Path | None, result: CollectResult) -> None:
    if evidence_dir is None:
        return
    target = Path(evidence_dir)
    target.mkdir(parents=True, exist_ok=True)
    plan = {
        "input": {"start": result.start, "end": result.end},
        "portal_timezone": result.timezone,
        "browser_timezone": result.browser_timezone,
        "segments": result.segments,
    }
    (target / "plan.json").write_text(
        json.dumps(plan, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    with (target / "records.jsonl").open("w", encoding="utf-8") as fh:
        for rec in result.records:
            fh.write(json.dumps(rec, ensure_ascii=False) + "\n")
    summary = {
        "status": result.status,
        "note": result.note,
        "record_count": len(result.records),
        "duplicates_removed": result.duplicates_removed,
        "missing_minutes_count": len(result.missing_minutes),
        "missing_minutes_sample": result.missing_minutes[:50],
        "extra_minutes_count": len(result.extra_minutes),
        "extra_minutes_sample": result.extra_minutes[:50],
        "discrepancy": result.discrepancy,
    }
    (target / "summary.json").write_text(
        json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8"
    )
