"""命令行入口：区间计划、浏览器填写与结果核对。

用法：
    python -m rpa --portal http://127.0.0.1:8090 \
        --start 2026-11-01T00:30:00-04:00 --end 2026-11-01T03:30:00-05:00 \
        --browser-timezone Asia/Shanghai --evidence-dir evidence/run1 --screenshots

    python -m rpa --portal http://127.0.0.1:8090 \
        --start ... --end ... --plan-only   # 只输出区间计划，不打开浏览器
"""
from __future__ import annotations

import argparse
import json
import sys
from zoneinfo import ZoneInfo

from .collect import _fetch_contract, collect
from .plan import InvalidRangeError, parse_iso_minute, plan_segments

_FOLD_LABEL = {0: "第一次", 1: "第二次"}


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(
        prog="python -m rpa",
        description="把带偏移的绝对时间范围（起点含、终点不含）转换成门户可确认的查询并采集",
    )
    parser.add_argument("--portal", default="http://127.0.0.1:8090", help="门户地址")
    parser.add_argument("--start", required=True, help="起点 ISO 时间（带偏移，整分钟，含）")
    parser.add_argument("--end", required=True, help="终点 ISO 时间（带偏移，整分钟，不含）")
    parser.add_argument("--browser-timezone", default=None, help="浏览器本机时区，默认同系统")
    parser.add_argument("--evidence-dir", default=None, help="边界证据输出目录")
    parser.add_argument("--screenshots", action="store_true", help="每段提交后截图存入证据目录")
    parser.add_argument("--headed", action="store_true", help="显示浏览器窗口（默认无头）")
    parser.add_argument("--plan-only", action="store_true", help="只输出区间计划，不打开浏览器")
    args = parser.parse_args(argv)

    try:
        if args.plan_only:
            return _plan_only(args)
        result = collect(
            args.portal,
            args.start,
            args.end,
            browser_timezone=args.browser_timezone,
            headless=not args.headed,
            evidence_dir=args.evidence_dir,
            screenshots=args.screenshots,
        )
    except InvalidRangeError as exc:
        print(f"拒绝开始：{exc}", file=sys.stderr)
        return 2

    _print_result(result)
    if args.evidence_dir:
        print(f"证据目录 {args.evidence_dir}（plan.json / records.jsonl / summary.json）")
    return 0 if result.status in ("ok", "empty") else 1


def _plan_only(args) -> int:
    contract = _fetch_contract(args.portal)
    start = parse_iso_minute(args.start, "起点")
    end = parse_iso_minute(args.end, "终点")
    tz = ZoneInfo(contract["portal_timezone"])
    segments = plan_segments(start, end, tz, contract.get("supported_year"))
    plan = {
        "input": {"start": args.start, "end": args.end},
        "portal_timezone": contract["portal_timezone"],
        "empty": not segments,
        "segments": [seg.intent() for seg in segments],
    }
    print(json.dumps(plan, ensure_ascii=False, indent=2))
    return 0


def _print_result(result) -> None:
    if result.status == "empty":
        print(f"范围为空：{result.note}")
        return
    print(f"门户时区 {result.timezone}，浏览器时区 {result.browser_timezone}")
    for seg in result.segments:
        intent = seg
        print(
            "段 {index}：{date} {start_time}({sf}) -> {end_time}({ef})，"
            "预期摘要 {summary}".format(
                index=intent["index"],
                date=intent["date"],
                start_time=intent["start_time"],
                sf=_FOLD_LABEL[intent["start_fold"]],
                end_time=intent["end_time"],
                ef=_FOLD_LABEL[intent["end_fold"]],
                summary=intent["expected_utc_summary"],
            )
        )
        execution = seg.get("execution")
        if execution and execution.get("status") == "ok":
            print(
                f"  已核对：读回一致，UTC 摘要 {execution['utc_summary']}，"
                f"{execution['record_count']} 条"
            )
        elif execution:
            print(f"  差异：{json.dumps(execution, ensure_ascii=False)}")
        else:
            print("  未执行（前段已停止）")
    print(
        f"汇总：{len(result.records)} 条命中，去重 {result.duplicates_removed} 条，"
        f"缺口 {len(result.missing_minutes)} 分钟，多出 {len(result.extra_minutes)} 分钟，"
        f"状态 {result.status}"
    )
    if result.discrepancy:
        print(f"差异：段 {result.discrepancy['segment']}：{result.discrepancy['message']}")
    if result.missing_minutes:
        print(f"缺口样例：{result.missing_minutes[:5]}")


if __name__ == "__main__":
    sys.exit(main())
