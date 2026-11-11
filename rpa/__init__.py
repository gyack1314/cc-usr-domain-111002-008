"""区间采集 RPA：绝对时间范围 -> 门户本地墙上时间查询。"""
from .collect import CollectResult, aggregate, collect
from .plan import InvalidRangeError, Segment, parse_iso_minute, plan_segments
from .portal import PortalDriver, SegmentDiscrepancy

__all__ = [
    "CollectResult",
    "InvalidRangeError",
    "PortalDriver",
    "Segment",
    "SegmentDiscrepancy",
    "aggregate",
    "collect",
    "parse_iso_minute",
    "plan_segments",
]
