"""区间计划：把带偏移的绝对时间范围转换成门户可确认的单日子区间。

约定：
- 输入为带偏移的 ISO 时间，起点包含、终点不包含。
- 来源记录精度为分钟；非整分钟输入在开始前拒绝，不做舍入。
- 门户单次查询最多覆盖一个当地自然日，且两个端点都包含，
  因此每段的查询终点是“区间终点（不含）减去一分钟”。
- 重复时段（回拨日 01 点）用偏移选项区分：0 第一次，1 第二次。
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, time, timedelta, timezone
from zoneinfo import ZoneInfo

ONE_MINUTE = timedelta(minutes=1)
ONE_DAY = timedelta(days=1)


class InvalidRangeError(ValueError):
    """输入范围在开始前被拒绝。"""


@dataclass(frozen=True)
class Segment:
    """一个当地自然日内的等价查询计划。"""

    index: int
    date: str  # 当地日期 YYYY-MM-DD
    start_time: str  # 当地 HH:MM，含
    start_fold: int  # 重复时段偏移选项：0 第一次，1 第二次；其余时刻门户忽略
    end_time: str  # 当地 HH:MM，含（门户双端包含）
    end_fold: int
    abs_start: datetime  # 段起点（绝对，含）
    abs_end_exclusive: datetime  # 段终点（绝对，不含）
    query_end: datetime  # 提交给门户的包含终点 = abs_end_exclusive - 1 分钟

    @property
    def expected_summary(self) -> str:
        """门户提交后应显示的 UTC 摘要。"""
        return iso_z(self.abs_start) + "|" + iso_z(self.query_end)

    @property
    def expected_minutes(self) -> list[int]:
        """该段应返回的来源分钟（epoch 分钟，含两端）。"""
        first = int(self.abs_start.timestamp()) // 60
        last = int(self.query_end.timestamp()) // 60
        return list(range(first, last + 1))

    def intent(self) -> dict:
        """可核对、可序列化的段意图。"""
        return {
            "index": self.index,
            "date": self.date,
            "start_time": self.start_time,
            "start_fold": self.start_fold,
            "end_time": self.end_time,
            "end_fold": self.end_fold,
            "abs_start": iso_z(self.abs_start),
            "abs_end_exclusive": iso_z(self.abs_end_exclusive),
            "query_end_inclusive": iso_z(self.query_end),
            "expected_utc_summary": self.expected_summary,
            "expected_record_count": len(self.expected_minutes),
        }


def iso_z(value: datetime) -> str:
    """与页面 new Date(ms).toISOString() 一致的 UTC 文本。"""
    return value.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.000Z")


def parse_iso_minute(raw: str, label: str) -> datetime:
    """解析带偏移的 ISO 时间；非整分钟在开始前拒绝，不偷偷舍入。"""
    try:
        parsed = datetime.fromisoformat(raw)
    except ValueError as exc:
        raise InvalidRangeError(f"{label}不是可解析的 ISO 时间: {raw!r}") from exc
    if parsed.tzinfo is None:
        raise InvalidRangeError(f"{label}必须带时区偏移: {raw!r}")
    if parsed.second or parsed.microsecond:
        raise InvalidRangeError(f"{label}不是整分钟（{raw!r}），拒绝开始，不做舍入")
    return parsed


def _local_fields(instant: datetime, tz: ZoneInfo) -> tuple[str, str, int]:
    """绝对时刻 -> (当地日期, 当地 HH:MM, 偏移选项)。

    偏移选项仅在重复时段有意义：0 第一次，1 第二次；其余时刻门户忽略该
    选项，恒填 0。fold 由 astimezone 按绝对时刻给出，不从墙上时间猜测。
    """
    local = instant.astimezone(tz)
    fold = 0
    if local.replace(fold=0).utcoffset() != local.replace(fold=1).utcoffset():
        fold = local.fold
    return (
        local.date().isoformat(),
        f"{local.hour:02d}:{local.minute:02d}",
        fold,
    )


def plan_segments(
    start: datetime,
    end: datetime,
    tz: ZoneInfo,
    supported_year: int | None = None,
) -> list[Segment]:
    """把 [start, end) 切成门户可确认的单日子区间。

    空范围返回空列表（调用方不得发请求）。跨日或跨偏移转换的范围拆成多段，
    每段的当地日期、时间与偏移选项都可与提交后的 UTC 摘要互相核对。
    """
    if end <= start:
        return []
    segments: list[Segment] = []
    day = start.astimezone(tz).date()
    last_day = (end - ONE_MINUTE).astimezone(tz).date()
    while day <= last_day:
        if supported_year is not None and day.year != supported_year:
            raise InvalidRangeError(
                f"门户仅支持 {supported_year} 年，范围涉及 {day.isoformat()}"
            )
        day_start = datetime.combine(day, time.min, tzinfo=tz)
        day_end = day_start + ONE_DAY  # 当地午夜不重复也不缺失，绝对边界安全
        seg_start = max(start, day_start)
        seg_end = min(end, day_end)
        if seg_start < seg_end:
            query_end = seg_end - ONE_MINUTE
            s_date, s_time, s_fold = _local_fields(seg_start, tz)
            e_date, e_time, e_fold = _local_fields(query_end, tz)
            if s_date != day.isoformat() or e_date != day.isoformat():
                raise InvalidRangeError(f"子区间越出当地日 {day.isoformat()}")
            segments.append(
                Segment(
                    index=len(segments),
                    date=s_date,
                    start_time=s_time,
                    start_fold=s_fold,
                    end_time=e_time,
                    end_fold=e_fold,
                    abs_start=seg_start,
                    abs_end_exclusive=seg_end,
                    query_end=query_end,
                )
            )
        day += ONE_DAY
    return segments
