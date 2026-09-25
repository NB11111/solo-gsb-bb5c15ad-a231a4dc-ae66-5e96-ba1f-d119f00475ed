"""接口与判定逻辑测试。"""

import itertools
import math
import random
from datetime import datetime, timedelta, timezone
from fractions import Fraction

from fastapi.testclient import TestClient

from app.core import reconcile_meter_change, solve_daily_segment, solve_segment
from app.main import app

client = TestClient(app)


def _post(payload, path="/v1/usage/verify"):
    return client.post(path, json=payload)


def _reconcile(payload):
    return _post(payload, path="/v1/usage/reconcile")


def _daily_range(payload):
    return _post(payload, path="/v1/usage/daily-range")


def _meter_change(payload):
    return _post(payload, path="/v1/usage/meter-change/reconcile")


# ---------------------------------------------------------------------------
# 正常场景
# ---------------------------------------------------------------------------

def test_health():
    resp = client.get("/health")
    assert resp.status_code == 200
    assert resp.json() == {"status": "ok"}


def test_no_wrap_unique():
    """无回绕，增量在速率上限内（60s * 10/s = 600，不足以多绕一圈）。"""
    resp = _post(
        {
            "modulus": 1000,
            "max_rate_per_second": 10,
            "readings": [
                {"timestamp": "2026-09-24T00:00:00Z", "value": 100},
                {"timestamp": "2026-09-24T00:01:00Z", "value": 600},
            ],
        }
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["status"] == "ok"
    assert body["total_usage"] == 500
    assert body["errors"] == []
    assert body["segments"][0]["wraps"] == 0
    assert body["segments"][0]["usage"] == 500


def test_single_wrap_unique():
    """一次回绕且唯一：900 -> 100，M=1000，10s 内上限 200。"""
    resp = _post(
        {
            "modulus": 1000,
            "max_rate_per_second": 20,
            "readings": [
                {"timestamp": "2026-09-24T00:00:00Z", "value": 900},
                {"timestamp": "2026-09-24T00:00:10Z", "value": 100},
            ],
        }
    )
    body = resp.json()
    assert body["status"] == "ok"
    assert body["segments"][0]["wraps"] == 1
    assert body["segments"][0]["usage"] == 200
    assert body["total_usage"] == 200


def test_multi_segment_total():
    """多段混合（回绕 + 不回绕）全部唯一，总用量为各段之和。"""
    resp = _post(
        {
            "modulus": 1000,
            "max_rate_per_second": 80,
            "readings": [
                {"timestamp": "2026-09-24T00:00:00Z", "value": 100},
                {"timestamp": "2026-09-24T00:00:10Z", "value": 250},  # +150
                {"timestamp": "2026-09-24T00:00:20Z", "value": 50},   # 回绕 +800
            ],
        }
    )
    body = resp.json()
    assert body["status"] == "ok"
    assert [s["usage"] for s in body["segments"]] == [150, 800]
    assert body["segments"][1]["wraps"] == 1
    assert body["total_usage"] == 950


def test_zero_rate_equal_readings():
    """速率为 0 且读数不变：唯一解，用量 0。"""
    resp = _post(
        {
            "modulus": 1000,
            "max_rate_per_second": 0,
            "readings": [
                {"timestamp": "2026-09-24T00:00:00Z", "value": 42},
                {"timestamp": "2026-09-24T01:00:00Z", "value": 42},
            ],
        }
    )
    body = resp.json()
    assert body["status"] == "ok"
    assert body["total_usage"] == 0


def test_fractional_rate_boundary():
    """小数速率按精确分数判定：0.5/s * 10s = 5，增量 5 恰好唯一。"""
    resp = _post(
        {
            "modulus": 100,
            "max_rate_per_second": 0.5,
            "readings": [
                {"timestamp": "2026-09-24T00:00:00Z", "value": 10},
                {"timestamp": "2026-09-24T00:00:10Z", "value": 15},
            ],
        }
    )
    body = resp.json()
    assert body["status"] == "ok"
    assert body["total_usage"] == 5


def test_timezone_offset_normalized_to_utc():
    """带偏移的时区会被换算为 UTC 后再判定。"""
    resp = _post(
        {
            "modulus": 1000,
            "max_rate_per_second": 10,
            "readings": [
                {"timestamp": "2026-09-24T08:00:00+08:00", "value": 0},
                {"timestamp": "2026-09-24T00:00:30Z", "value": 5},  # 同一时刻后 30s
            ],
        }
    )
    body = resp.json()
    assert body["status"] == "ok"
    assert body["segments"][0]["elapsed_seconds"] == 30
    assert body["total_usage"] == 5


# ---------------------------------------------------------------------------
# 无解 / 多解
# ---------------------------------------------------------------------------

def test_no_solution():
    """增量超过速率上限：1s 内最多 10，但需 800。"""
    resp = _post(
        {
            "modulus": 1000,
            "max_rate_per_second": 10,
            "readings": [
                {"timestamp": "2026-09-24T00:00:00Z", "value": 100},
                {"timestamp": "2026-09-24T00:00:01Z", "value": 900},
            ],
        }
    )
    body = resp.json()
    assert body["status"] == "error"
    assert body["total_usage"] is None
    assert body["segments"] == []
    err = body["errors"][0]
    assert err["reason"] == "no_solution"
    assert err["index"] == 0
    assert err["max_wraps"] < err["min_wraps"]


def test_ambiguous():
    """时间跨度过大导致多个回绕次数都可行。"""
    resp = _post(
        {
            "modulus": 1000,
            "max_rate_per_second": 10,
            "readings": [
                {"timestamp": "2026-09-24T00:00:00Z", "value": 100},
                {"timestamp": "2026-09-24T00:16:40Z", "value": 200},  # 1000s, 上限 10000
            ],
        }
    )
    body = resp.json()
    assert body["status"] == "error"
    assert body["total_usage"] is None
    err = body["errors"][0]
    assert err["reason"] == "ambiguous"
    assert err["min_wraps"] == 0
    assert err["max_wraps"] == 9  # k=0..9 均满足


def test_mixed_segments_partial_errors():
    """部分区间唯一、部分出错：返回已判定分段与出错区间，总用量为 null。"""
    resp = _post(
        {
            "modulus": 1000,
            "max_rate_per_second": 20,
            "readings": [
                {"timestamp": "2026-09-24T00:00:00Z", "value": 100},
                {"timestamp": "2026-09-24T00:00:10Z", "value": 300},   # 唯一 +200
                {"timestamp": "2026-09-24T00:00:11Z", "value": 900},   # 无解
            ],
        }
    )
    body = resp.json()
    assert body["status"] == "error"
    assert body["total_usage"] is None
    assert len(body["segments"]) == 1
    assert body["segments"][0]["usage"] == 200
    assert len(body["errors"]) == 1
    assert body["errors"][0]["index"] == 1
    assert body["errors"][0]["reason"] == "no_solution"


# ---------------------------------------------------------------------------
# 请求校验（422）
# ---------------------------------------------------------------------------

def test_timestamps_not_strictly_increasing():
    resp = _post(
        {
            "modulus": 1000,
            "max_rate_per_second": 10,
            "readings": [
                {"timestamp": "2026-09-24T00:00:10Z", "value": 100},
                {"timestamp": "2026-09-24T00:00:10Z", "value": 200},
            ],
        }
    )
    assert resp.status_code == 422


def test_reading_out_of_modulus_range():
    resp = _post(
        {
            "modulus": 1000,
            "max_rate_per_second": 10,
            "readings": [
                {"timestamp": "2026-09-24T00:00:00Z", "value": 100},
                {"timestamp": "2026-09-24T00:00:01Z", "value": 1000},
            ],
        }
    )
    assert resp.status_code == 422


def test_naive_timestamp_rejected():
    resp = _post(
        {
            "modulus": 1000,
            "max_rate_per_second": 10,
            "readings": [
                {"timestamp": "2026-09-24T00:00:00", "value": 100},
                {"timestamp": "2026-09-24T00:00:01Z", "value": 200},
            ],
        }
    )
    assert resp.status_code == 422


def test_subsecond_timestamp_rejected():
    resp = _post(
        {
            "modulus": 1000,
            "max_rate_per_second": 10,
            "readings": [
                {"timestamp": "2026-09-24T00:00:00.500Z", "value": 100},
                {"timestamp": "2026-09-24T00:00:01Z", "value": 200},
            ],
        }
    )
    assert resp.status_code == 422


def test_too_few_readings():
    resp = _post(
        {
            "modulus": 1000,
            "max_rate_per_second": 10,
            "readings": [{"timestamp": "2026-09-24T00:00:00Z", "value": 100}],
        }
    )
    assert resp.status_code == 422


def test_negative_rate_rejected():
    resp = _post(
        {
            "modulus": 1000,
            "max_rate_per_second": -1,
            "readings": [
                {"timestamp": "2026-09-24T00:00:00Z", "value": 100},
                {"timestamp": "2026-09-24T00:00:01Z", "value": 200},
            ],
        }
    )
    assert resp.status_code == 422


# ---------------------------------------------------------------------------
# 账单总量对账 /v1/usage/reconcile
# ---------------------------------------------------------------------------

def test_reconcile_resolves_ambiguous_segment():
    """单段多解 [0,9]：账单 3100 隐含 K=3，圈数唯一确定。"""
    resp = _reconcile(
        {
            "modulus": 1000,
            "max_rate_per_second": 10,
            "total_usage": 3100,
            "readings": [
                {"timestamp": "2026-09-24T00:00:00Z", "value": 100},
                {"timestamp": "2026-09-24T00:16:40Z", "value": 200},
            ],
        }
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["status"] == "ok"
    assert body["total_wraps"] == 3
    assert body["conflicts"] == []
    seg = body["segments"][0]
    assert seg["wraps"] == 3
    assert seg["usage"] == 3100
    assert seg["min_wraps"] == seg["max_wraps"] == 3
    assert seg["min_usage"] is None and seg["max_usage"] is None


def test_reconcile_mixed_unique_and_ranges():
    """两段多解，K=5：段1 仍为范围 [0,4]，段2 为范围 [1,5]，均不给确定用量。"""
    resp = _reconcile(
        {
            "modulus": 1000,
            "max_rate_per_second": 10,
            "total_usage": 5000,
            "readings": [
                {"timestamp": "2026-09-24T00:00:00Z", "value": 100},
                {"timestamp": "2026-09-24T00:16:40Z", "value": 200},
                {"timestamp": "2026-09-24T00:33:20Z", "value": 100},
            ],
        }
    )
    body = resp.json()
    assert body["status"] == "ok"
    assert body["total_wraps"] == 5
    s1, s2 = body["segments"]
    assert (s1["min_wraps"], s1["max_wraps"]) == (0, 4)
    assert s1["wraps"] is None and s1["usage"] is None
    assert (s1["min_usage"], s1["max_usage"]) == (100, 4100)
    assert (s2["min_wraps"], s2["max_wraps"]) == (1, 5)
    assert (s2["min_usage"], s2["max_usage"]) == (900, 4900)


def test_reconcile_feasible_endpoint_all_unique():
    """K 取区间端点时各段圈数全部唯一，确定用量之和恰为账单总量。"""
    resp = _reconcile(
        {
            "modulus": 1000,
            "max_rate_per_second": 10,
            "total_usage": 1000,  # K = sum_min = 1
            "readings": [
                {"timestamp": "2026-09-24T00:00:00Z", "value": 100},
                {"timestamp": "2026-09-24T00:16:40Z", "value": 200},
                {"timestamp": "2026-09-24T00:33:20Z", "value": 100},
            ],
        }
    )
    body = resp.json()
    assert body["status"] == "ok"
    assert [s["usage"] for s in body["segments"]] == [100, 900]
    assert sum(s["usage"] for s in body["segments"]) == 1000


def test_reconcile_incompatible_modulus():
    """总量与首尾读数差模 M 不同余：返回不相容冲突及可行区间。"""
    resp = _reconcile(
        {
            "modulus": 1000,
            "max_rate_per_second": 10,
            "total_usage": 5500,  # 应为 1000 的整数倍
            "readings": [
                {"timestamp": "2026-09-24T00:00:00Z", "value": 100},
                {"timestamp": "2026-09-24T00:16:40Z", "value": 200},
                {"timestamp": "2026-09-24T00:33:20Z", "value": 100},
            ],
        }
    )
    body = resp.json()
    assert resp.status_code == 200
    assert body["status"] == "conflict"
    assert body["segments"] == []
    assert body["total_wraps"] is None
    conflict = body["conflicts"][0]
    assert conflict["kind"] == "total"
    assert conflict["reason"] == "total_incompatible_modulus"
    assert conflict["expected_remainder"] == 0
    assert conflict["feasible_min_total"] == 1000
    assert conflict["feasible_max_total"] == 19000


def test_reconcile_out_of_range():
    """总量同余但总圈数超出可行和：返回超界冲突。"""
    resp = _reconcile(
        {
            "modulus": 1000,
            "max_rate_per_second": 10,
            "total_usage": 0,  # 首尾相同要求 K=0，但回绕段至少 1 圈
            "readings": [
                {"timestamp": "2026-09-24T00:00:00Z", "value": 100},
                {"timestamp": "2026-09-24T00:16:40Z", "value": 200},
                {"timestamp": "2026-09-24T00:33:20Z", "value": 100},
            ],
        }
    )
    body = resp.json()
    assert body["status"] == "conflict"
    conflict = body["conflicts"][0]
    assert conflict["reason"] == "total_out_of_range"
    assert conflict["total_wraps"] == 0
    assert conflict["feasible_min_total"] == 1000


def test_reconcile_segment_no_solution_conflict():
    """某段本身无解（超过速率上限）：以分段冲突返回，不做总量推断。"""
    resp = _reconcile(
        {
            "modulus": 1000,
            "max_rate_per_second": 20,
            "total_usage": 300,
            "readings": [
                {"timestamp": "2026-09-24T00:00:00Z", "value": 100},
                {"timestamp": "2026-09-24T00:00:10Z", "value": 300},  # 可行
                {"timestamp": "2026-09-24T00:00:11Z", "value": 900},  # 无解
            ],
        }
    )
    body = resp.json()
    assert body["status"] == "conflict"
    assert body["segments"] == []
    assert len(body["conflicts"]) == 1
    conflict = body["conflicts"][0]
    assert conflict["kind"] == "segment"
    assert conflict["index"] == 1
    assert conflict["reason"] == "no_solution"
    assert conflict["max_wraps"] < conflict["min_wraps"]


def test_reconcile_negative_total_rejected():
    """账单总用量为负数：422。"""
    resp = _reconcile(
        {
            "modulus": 1000,
            "max_rate_per_second": 10,
            "total_usage": -1,
            "readings": [
                {"timestamp": "2026-09-24T00:00:00Z", "value": 100},
                {"timestamp": "2026-09-24T00:00:10Z", "value": 200},
            ],
        }
    )
    assert resp.status_code == 422


def test_reconcile_total_must_be_integer():
    """账单总用量必须为整数：422。"""
    resp = _reconcile(
        {
            "modulus": 1000,
            "max_rate_per_second": 10,
            "total_usage": 12.5,
            "readings": [
                {"timestamp": "2026-09-24T00:00:00Z", "value": 100},
                {"timestamp": "2026-09-24T00:00:10Z", "value": 200},
            ],
        }
    )
    assert resp.status_code == 422


def test_reconcile_zero_total_with_flat_readings():
    """首尾相同且全部段都允许 0 圈时，账单 0 可行且各段唯一为 0。"""
    resp = _reconcile(
        {
            "modulus": 1000,
            "max_rate_per_second": 10,
            "total_usage": 0,
            "readings": [
                {"timestamp": "2026-09-24T00:00:00Z", "value": 100},
                {"timestamp": "2026-09-24T00:00:10Z", "value": 100},
            ],
        }
    )
    body = resp.json()
    assert body["status"] == "ok"
    seg = body["segments"][0]
    assert seg["wraps"] == 0 and seg["usage"] == 0


def test_reconcile_reuses_reading_validation():
    """新接口复用读数校验：读数越界、时间戳不递增均 422。"""
    base = {
        "modulus": 1000,
        "max_rate_per_second": 10,
        "total_usage": 100,
    }
    resp = _reconcile(
        {
            **base,
            "readings": [
                {"timestamp": "2026-09-24T00:00:00Z", "value": 100},
                {"timestamp": "2026-09-24T00:00:01Z", "value": 1000},
            ],
        }
    )
    assert resp.status_code == 422
    resp = _reconcile(
        {
            **base,
            "readings": [
                {"timestamp": "2026-09-24T00:00:10Z", "value": 100},
                {"timestamp": "2026-09-24T00:00:10Z", "value": 200},
            ],
        }
    )
    assert resp.status_code == 422


# ---------------------------------------------------------------------------
# 每日用量上下界 /v1/usage/daily-range
# ---------------------------------------------------------------------------

def test_daily_range_single_day_unique():
    """区间不跨日：唯一增量，当日 min == max。"""
    resp = _daily_range(
        {
            "modulus": 1000,
            "max_rate_per_second": 10,
            "readings": [
                {"timestamp": "2026-09-24T00:00:00Z", "value": 100},
                {"timestamp": "2026-09-24T00:01:00Z", "value": 600},
            ],
        }
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["status"] == "ok"
    assert body["conflicts"] == []
    assert body["days"] == [
        {
            "date": "2026-09-24",
            "min_usage": 500,
            "max_usage": 500,
            "segment_count": 1,
        }
    ]
    assert "分别达到" in body["boundary_note"]


def test_daily_range_cross_midnight_wrap():
    """跨午夜 + 一次回绕：250 总量在两个容量 500 的子段间自由分配。"""
    resp = _daily_range(
        {
            "modulus": 1000,
            "max_rate_per_second": 100,
            "readings": [
                {"timestamp": "2026-09-24T23:59:55Z", "value": 800},
                {"timestamp": "2026-09-25T00:00:05Z", "value": 50},
            ],
        }
    )
    body = resp.json()
    assert body["status"] == "ok"
    days = body["days"]
    assert [d["date"] for d in days] == ["2026-09-24", "2026-09-25"]
    assert [(d["min_usage"], d["max_usage"]) for d in days] == [(0, 250), (0, 250)]
    assert all(d["segment_count"] == 1 for d in days)
    # 每日上界 250 可分别达到（(250,0) 或 (0,250)），但不能同时为 250
    assert days[0]["max_usage"] + days[1]["max_usage"] > 250


def test_daily_range_upper_bounds_not_simultaneous():
    """两个容量 5 的子段承担总量 8：每日 [3,5]，上界无法同时达到。"""
    resp = _daily_range(
        {
            "modulus": 1000,
            "max_rate_per_second": 1,
            "readings": [
                {"timestamp": "2026-09-24T23:59:55Z", "value": 992},
                {"timestamp": "2026-09-25T00:00:05Z", "value": 0},
            ],
        }
    )
    body = resp.json()
    days = body["days"]
    assert [(d["min_usage"], d["max_usage"]) for d in days] == [(3, 5), (3, 5)]
    # 可行分配只有 (3,5)/(4,4)/(5,3)：任何分配下两日用量之和恒为 8
    assert sum(d["max_usage"] for d in days) == 10  # 上界之和不可同时达到
    assert sum(d["min_usage"] for d in days) == 6   # 下界之和同样不可同时达到


def test_daily_range_aggregates_multiple_intervals():
    """同一天两个独立区间的子段界求和，segment_count 计数。"""
    resp = _daily_range(
        {
            "modulus": 1000,
            "max_rate_per_second": 100,
            "readings": [
                {"timestamp": "2026-09-24T00:00:00Z", "value": 100},
                {"timestamp": "2026-09-24T00:00:10Z", "value": 100},  # k∈[0,1]
                {"timestamp": "2026-09-24T00:00:20Z", "value": 100},  # k∈[0,1]
            ],
        }
    )
    body = resp.json()
    assert body["days"] == [
        {
            "date": "2026-09-24",
            "min_usage": 0,
            "max_usage": 2000,
            "segment_count": 2,
        }
    ]


def test_daily_range_multi_day_span():
    """跨三天：每天一条子段，容量按整日秒数取整。"""
    resp = _daily_range(
        {
            "modulus": 1000,
            "max_rate_per_second": 1,
            "readings": [
                {"timestamp": "2026-09-24T12:00:00Z", "value": 0},
                {"timestamp": "2026-09-26T12:00:00Z", "value": 0},
            ],
        }
    )
    body = resp.json()
    days = body["days"]
    assert [d["date"] for d in days] == [
        "2026-09-24",
        "2026-09-25",
        "2026-09-26",
    ]
    assert [(d["min_usage"], d["max_usage"]) for d in days] == [
        (0, 43200),
        (0, 86400),
        (0, 43200),
    ]


def test_daily_range_fractional_rate_floors_capacity():
    """0.5/s × 3s 容量 floor 为 1：总量 2 迫使两子段各为 1。"""
    resp = _daily_range(
        {
            "modulus": 1000,
            "max_rate_per_second": 0.5,
            "readings": [
                {"timestamp": "2026-09-24T23:59:57Z", "value": 998},
                {"timestamp": "2026-09-25T00:00:03Z", "value": 0},
            ],
        }
    )
    body = resp.json()
    assert [(d["min_usage"], d["max_usage"]) for d in body["days"]] == [
        (1, 1),
        (1, 1),
    ]


def test_daily_range_no_feasible_allocation_conflict():
    """整段速率判定可行、但切日取整后总容量不足：返回区间索引与冲突原因。"""
    resp = _daily_range(
        {
            "modulus": 1000,
            "max_rate_per_second": 1,
            "readings": [
                {"timestamp": "2026-09-24T23:59:55Z", "value": 950},
                {"timestamp": "2026-09-25T00:00:05Z", "value": 100},
            ],
        }
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["status"] == "conflict"
    assert body["days"] == []
    conflict = body["conflicts"][0]
    assert conflict["index"] == 0
    assert conflict["reason"] == "no_feasible_allocation"
    assert conflict["total_capacity"] == 10
    assert conflict["min_wraps"] == 1
    assert conflict["max_wraps"] == 0


def test_daily_range_conflict_skips_no_daily_totals():
    """存在冲突区间时不返回任何日用量，但列出全部冲突区间。"""
    resp = _daily_range(
        {
            "modulus": 1000,
            "max_rate_per_second": 1,
            "readings": [
                {"timestamp": "2026-09-24T23:59:55Z", "value": 950},
                {"timestamp": "2026-09-25T00:00:05Z", "value": 100},  # 区间0冲突
                {"timestamp": "2026-09-25T00:00:06Z", "value": 900},  # 区间1也冲突
            ],
        }
    )
    body = resp.json()
    assert body["status"] == "conflict"
    assert body["days"] == []
    assert [c["index"] for c in body["conflicts"]] == [0, 1]
    assert all(c["reason"] == "no_feasible_allocation" for c in body["conflicts"])


def test_daily_range_timezone_offset_normalized_to_utc():
    """带 +08:00 偏移的时间戳换算为 UTC 后切日。"""
    resp = _daily_range(
        {
            "modulus": 1000,
            "max_rate_per_second": 1,
            "readings": [
                {"timestamp": "2026-09-25T07:59:55+08:00", "value": 995},
                {"timestamp": "2026-09-25T00:00:05Z", "value": 0},
            ],
        }
    )
    body = resp.json()
    assert body["status"] == "ok"
    # 即 UTC 2026-09-24T23:59:55 -> 2026-09-25T00:00:05
    assert [d["date"] for d in body["days"]] == ["2026-09-24", "2026-09-25"]
    assert [(d["min_usage"], d["max_usage"]) for d in body["days"]] == [
        (0, 5),
        (0, 5),
    ]


def test_daily_range_reuses_reading_validation():
    """新接口复用读数/模数/速率校验：非法输入一律 422。"""
    base = {"modulus": 1000, "max_rate_per_second": 10}
    # 读数越界
    resp = _daily_range(
        {
            **base,
            "readings": [
                {"timestamp": "2026-09-24T00:00:00Z", "value": 100},
                {"timestamp": "2026-09-24T00:00:01Z", "value": 1000},
            ],
        }
    )
    assert resp.status_code == 422
    # 时间戳不递增
    resp = _daily_range(
        {
            **base,
            "readings": [
                {"timestamp": "2026-09-24T00:00:01Z", "value": 100},
                {"timestamp": "2026-09-24T00:00:01Z", "value": 200},
            ],
        }
    )
    assert resp.status_code == 422
    # 缺时区
    resp = _daily_range(
        {
            **base,
            "readings": [
                {"timestamp": "2026-09-24T00:00:00", "value": 100},
                {"timestamp": "2026-09-24T00:00:01Z", "value": 200},
            ],
        }
    )
    assert resp.status_code == 422
    # 亚秒
    resp = _daily_range(
        {
            **base,
            "readings": [
                {"timestamp": "2026-09-24T00:00:00.5Z", "value": 100},
                {"timestamp": "2026-09-24T00:00:01Z", "value": 200},
            ],
        }
    )
    assert resp.status_code == 422
    # 只有 1 条读数
    resp = _daily_range(
        {
            **base,
            "readings": [{"timestamp": "2026-09-24T00:00:00Z", "value": 100}],
        }
    )
    assert resp.status_code == 422
    # 负速率
    resp = _daily_range(
        {
            "modulus": 1000,
            "max_rate_per_second": -1,
            "readings": [
                {"timestamp": "2026-09-24T00:00:00Z", "value": 100},
                {"timestamp": "2026-09-24T00:00:01Z", "value": 200},
            ],
        }
    )
    assert resp.status_code == 422


# ---------------------------------------------------------------------------
# daily-range 核心界的精确性：与全枚举暴力结果比对（固定随机种子）
# ---------------------------------------------------------------------------

def test_daily_segment_bounds_exact_by_brute_force():
    """随机小参数下，闭式边际界与枚举全部 (k, 子段分配) 的结果一致。"""

    def brute(r1, r2, secs, modulus, rate):
        caps = [math.floor(rate * s) for s in secs]
        total_cap = sum(caps)
        diff = r2 - r1
        lows = [None] * len(secs)
        highs = [None] * len(secs)
        feasible = False
        k = 0
        while diff + k * modulus <= total_cap:
            delta = diff + k * modulus
            if delta >= 0:
                feasible = True
                spans = [range(min(caps[j], delta) + 1) for j in range(len(secs))]
                for xs in itertools.product(*spans):
                    if sum(xs) == delta:
                        for j, x in enumerate(xs):
                            lows[j] = x if lows[j] is None else min(lows[j], x)
                            highs[j] = x if highs[j] is None else max(highs[j], x)
            k += 1
        return feasible, caps, lows, highs

    rng = random.Random(20260925)
    checked = 0
    for _ in range(4000):
        modulus = rng.choice([3, 5, 7, 10])
        rate = Fraction(rng.randint(0, 20), rng.randint(1, 3))
        start = datetime(2026, 9, 24, tzinfo=timezone.utc) + timedelta(
            seconds=rng.randint(0, 86399)
        )
        end = start + timedelta(seconds=rng.randint(1, 30))
        r1, r2 = rng.randrange(modulus), rng.randrange(modulus)
        result = solve_daily_segment(0, r1, r2, start, end, modulus, rate)
        secs = [s.seconds for s in result.subsegments]
        if sum(math.floor(rate * s) for s in secs) > 25:
            continue  # 控制枚举规模
        checked += 1
        feasible, caps, lows, highs = brute(r1, r2, secs, modulus, rate)
        assert result.feasible == feasible
        if feasible:
            assert [s.capacity for s in result.subsegments] == caps
            assert list(result.min_usages) == lows
            assert list(result.max_usages) == highs
    assert checked > 200  # 确认确实跑了足够多的用例


# ---------------------------------------------------------------------------
# 换表账单复核 /v1/usage/meter-change/reconcile
# ---------------------------------------------------------------------------

_T0 = datetime(2026, 9, 1, tzinfo=timezone.utc)


def _z(seconds: int) -> str:
    return (_T0 + timedelta(seconds=seconds)).strftime("%Y-%m-%dT%H:%M:%SZ")


def test_meter_change_unique_different_moduli():
    """两表模数不同且各段圈数唯一：账单下两段用量同时确定。"""
    resp = _meter_change(
        {
            "old_meter": {
                # 900 -> 100 一次回绕，10s 上限 200，k1 唯一为 1，用量 200
                "modulus": 1000,
                "max_rate_per_second": 20,
                "start": {"timestamp": _z(0), "value": 900},
                "end": {"timestamp": _z(10), "value": 100},
            },
            "new_meter": {
                # 250 -> 50 一次回绕（M=300），10s 上限 100，k2 唯一为 1，用量 100
                "modulus": 300,
                "max_rate_per_second": 10,
                "start": {"timestamp": _z(10), "value": 250},
                "end": {"timestamp": _z(20), "value": 50},
            },
            "total_usage": 300,  # 1×1000 + 1×300 + (-800) + (-200) = 300
        }
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["status"] == "ok"
    assert body["modulus_gcd"] == 100
    assert body["conflicts"] == []
    old, new = body["segments"]
    assert old["meter"] == "old" and old["modulus"] == 1000
    assert (old["min_wraps"], old["max_wraps"]) == (1, 1)
    assert old["wraps"] == 1 and old["usage"] == 200
    assert old["min_usage"] is None and old["max_usage"] is None
    assert new["meter"] == "new" and new["modulus"] == 300
    assert new["wraps"] == 1 and new["usage"] == 100
    assert old["usage"] + new["usage"] == 300


def test_meter_change_ranges_without_guessing():
    """圈数多解：两段各给用量上下界，且一段上界与另一段下界互补为账单总量。"""
    resp = _meter_change(
        {
            "old_meter": {
                # 100 -> 200，10000s 上限 100000：k1 ∈ [0, 99]
                "modulus": 1000,
                "max_rate_per_second": 10,
                "start": {"timestamp": _z(0), "value": 100},
                "end": {"timestamp": _z(10000), "value": 200},
            },
            "new_meter": {
                # 10 -> 20（M=300），同速率同时长：k2 ∈ [0, 333]
                "modulus": 300,
                "max_rate_per_second": 10,
                "start": {"timestamp": _z(10000), "value": 10},
                "end": {"timestamp": _z(20000), "value": 20},
            },
            # k1*1000 + k2*300 = 10000 -> (k1,k2) ∈ {(1,30),(4,20),(7,10),(10,0)}
            "total_usage": 10110,
        }
    )
    body = resp.json()
    assert body["status"] == "ok"
    old, new = body["segments"]
    assert (old["min_wraps"], old["max_wraps"]) == (1, 10)
    assert old["wraps"] is None and old["usage"] is None
    assert (old["min_usage"], old["max_usage"]) == (1100, 10100)
    assert (new["min_wraps"], new["max_wraps"]) == (0, 30)
    assert (new["min_usage"], new["max_usage"]) == (10, 9010)
    # 上下界互补：任何可行组合下两段用量之和恒为账单总量
    assert old["min_usage"] + new["max_usage"] == 10110
    assert old["max_usage"] + new["min_usage"] == 10110


def test_meter_change_same_modulus_matches_reconcile():
    """模数相同（g=M）时，两段结果与既有总量对账接口完全一致。"""
    payload = {
        "old_meter": {
            "modulus": 1000,
            "max_rate_per_second": 10,
            "start": {"timestamp": _z(0), "value": 100},
            "end": {"timestamp": _z(1000), "value": 200},
        },
        "new_meter": {
            "modulus": 1000,
            "max_rate_per_second": 10,
            "start": {"timestamp": _z(1000), "value": 200},
            "end": {"timestamp": _z(2000), "value": 100},
        },
        "total_usage": 5000,
    }
    body = _meter_change(payload).json()
    assert body["status"] == "ok"
    old, new = body["segments"]
    assert (old["min_wraps"], old["max_wraps"]) == (0, 4)
    assert (old["min_usage"], old["max_usage"]) == (100, 4100)
    assert (new["min_wraps"], new["max_wraps"]) == (1, 5)
    assert (new["min_usage"], new["max_usage"]) == (900, 4900)

    recon = _reconcile(
        {
            "modulus": 1000,
            "max_rate_per_second": 10,
            "total_usage": 5000,
            "readings": [
                {"timestamp": _z(0), "value": 100},
                {"timestamp": _z(1000), "value": 200},
                {"timestamp": _z(2000), "value": 100},
            ],
        }
    ).json()
    for seg, ref in zip(body["segments"], recon["segments"]):
        assert (seg["min_wraps"], seg["max_wraps"]) == (
            ref["min_wraps"],
            ref["max_wraps"],
        )
        assert (seg["min_usage"], seg["max_usage"]) == (
            ref["min_usage"],
            ref["max_usage"],
        )


def test_meter_change_segment_conflicts_distinguished_by_meter():
    """旧表 / 新表各自无解：以 kind=segment 与 meter 字段区分，不做总量推断。"""
    base = {
        "total_usage": 300,
        "old_meter": {
            "modulus": 1000,
            "max_rate_per_second": 20,
            "start": {"timestamp": _z(0), "value": 100},
            "end": {"timestamp": _z(10), "value": 300},
        },
        "new_meter": {
            "modulus": 1000,
            "max_rate_per_second": 20,
            "start": {"timestamp": _z(10), "value": 0},
            "end": {"timestamp": _z(20), "value": 200},
        },
    }
    # 旧表无解：100 -> 900，1s 上限 20，最小增量 800
    payload = {
        **base,
        "old_meter": {**base["old_meter"], "end": {"timestamp": _z(1), "value": 900}},
        "new_meter": {
            **base["new_meter"],
            "start": {"timestamp": _z(1), "value": 0},
        },
    }
    body = _meter_change(payload).json()
    assert body["status"] == "conflict"
    assert body["segments"] == []
    assert body["modulus_gcd"] is None
    conflict = body["conflicts"][0]
    assert conflict["kind"] == "segment"
    assert conflict["meter"] == "old"
    assert conflict["reason"] == "no_solution"
    assert conflict["max_wraps"] < conflict["min_wraps"]

    # 新表无解
    payload = {
        **base,
        "new_meter": {
            **base["new_meter"],
            "end": {"timestamp": _z(11), "value": 900},
        },
    }
    body = _meter_change(payload).json()
    conflict = body["conflicts"][0]
    assert conflict["kind"] == "segment"
    assert conflict["meter"] == "new"
    assert conflict["reason"] == "no_solution"


def test_meter_change_incompatible_modulus_conflict():
    """账单总量与 gcd(M1,M2) 不同余：可区分的总量冲突原因及可行范围。"""
    resp = _meter_change(
        {
            "old_meter": {
                "modulus": 1000,
                "max_rate_per_second": 10,
                "start": {"timestamp": _z(0), "value": 100},
                "end": {"timestamp": _z(10000), "value": 200},
            },
            "new_meter": {
                "modulus": 300,
                "max_rate_per_second": 10,
                "start": {"timestamp": _z(10000), "value": 10},
                "end": {"timestamp": _z(20000), "value": 20},
            },
            "total_usage": 10115,  # 读数差之和 110，应 ≡ 10 (mod 100)
        }
    )
    body = resp.json()
    assert body["status"] == "conflict"
    conflict = body["conflicts"][0]
    assert conflict["kind"] == "total"
    assert conflict["reason"] == "total_incompatible_modulus"
    assert conflict["modulus_gcd"] == 100
    assert conflict["expected_remainder"] == 10
    assert conflict["feasible_min_total"] == 110
    assert conflict["feasible_max_total"] == 199010


def test_meter_change_out_of_range_conflict():
    """同余但超出两段可行圈数盒：total_out_of_range，与不相容原因可区分。"""
    resp = _meter_change(
        {
            "old_meter": {
                "modulus": 1000,
                "max_rate_per_second": 10,
                "start": {"timestamp": _z(0), "value": 100},
                "end": {"timestamp": _z(10000), "value": 200},
            },
            "new_meter": {
                "modulus": 300,
                "max_rate_per_second": 10,
                "start": {"timestamp": _z(10000), "value": 10},
                "end": {"timestamp": _z(20000), "value": 20},
            },
            "total_usage": 200110,  # ≡ 10 (mod 100)，但 R=200000 超出盒和
        }
    )
    body = resp.json()
    conflict = body["conflicts"][0]
    assert conflict["reason"] == "total_out_of_range"
    assert conflict["modulus_gcd"] == 100
    assert conflict["expected_remainder"] is None
    assert conflict["feasible_min_total"] == 110
    assert conflict["feasible_max_total"] == 199010


def test_meter_change_switch_instant_validation():
    """旧表终读与新表初读不同刻：422；用不同时区表达同一时刻：接受。"""
    resp = _meter_change(
        {
            "old_meter": {
                "modulus": 1000,
                "max_rate_per_second": 20,
                "start": {"timestamp": _z(0), "value": 900},
                "end": {"timestamp": _z(10), "value": 100},
            },
            "new_meter": {
                "modulus": 1000,
                "max_rate_per_second": 20,
                "start": {"timestamp": _z(11), "value": 0},
                "end": {"timestamp": _z(20), "value": 100},
            },
            "total_usage": 300,
        }
    )
    assert resp.status_code == 422

    # 同一时刻以 +08:00 / Z 两种偏移表达，归一化后相等，应通过
    resp = _meter_change(
        {
            "old_meter": {
                "modulus": 1000,
                "max_rate_per_second": 20,
                "start": {"timestamp": "2026-09-01T07:59:50+08:00", "value": 900},
                "end": {"timestamp": "2026-09-01T08:00:00+08:00", "value": 100},
            },
            "new_meter": {
                "modulus": 1000,
                "max_rate_per_second": 20,
                "start": {"timestamp": "2026-09-01T00:00:00Z", "value": 0},
                "end": {"timestamp": "2026-09-01T00:00:10Z", "value": 100},
            },
            "total_usage": 300,
        }
    )
    assert resp.status_code == 200
    assert resp.json()["status"] == "ok"


def test_meter_change_reuses_reading_validation():
    """新接口复用时间/读数校验：各表按各自模数校验，非法输入一律 422。"""
    valid = {
        "old_meter": {
            "modulus": 1000,
            "max_rate_per_second": 20,
            "start": {"timestamp": _z(0), "value": 900},
            "end": {"timestamp": _z(10), "value": 100},
        },
        "new_meter": {
            "modulus": 300,
            "max_rate_per_second": 10,
            "start": {"timestamp": _z(10), "value": 250},
            "end": {"timestamp": _z(20), "value": 50},
        },
        "total_usage": 300,
    }

    def post(**changes):
        payload = {**valid, **{k: v for k, v in changes.items() if k in ("total_usage",)}}
        if "old" in changes:
            payload["old_meter"] = {**valid["old_meter"], **changes["old"]}
        if "new" in changes:
            payload["new_meter"] = {**valid["new_meter"], **changes["new"]}
        if "total_usage" in changes:
            payload["total_usage"] = changes["total_usage"]
        return _meter_change(payload)

    # 旧表读数越界（按旧表模数 1000）
    assert post(old={"start": {"timestamp": _z(0), "value": 1000}}).status_code == 422
    # 新表读数越界（按新表模数 300，300 在 1000 模数下合法）
    assert post(new={"end": {"timestamp": _z(20), "value": 300}}).status_code == 422
    # 单表内时间戳不递增
    assert post(old={"end": {"timestamp": _z(0), "value": 100}}).status_code == 422
    # 缺时区
    assert post(old={"end": {"timestamp": "2026-09-01T00:00:10", "value": 100}}).status_code == 422
    # 亚秒
    assert post(new={"end": {"timestamp": "2026-09-01T00:00:20.5Z", "value": 50}}).status_code == 422
    # 负速率 / 模数为 0
    assert post(old={"max_rate_per_second": -1}).status_code == 422
    assert post(new={"modulus": 0}).status_code == 422
    # 账单总量负数 / 非整数
    assert post(total_usage=-1).status_code == 422
    assert _meter_change({**valid, "total_usage": 12.5}).status_code == 422


def test_meter_change_core_exact_by_brute_force():
    """随机小参数下，闭式联立结果与枚举全部 (k1, k2) 圈数组合完全一致。"""
    rng = random.Random(20260925)
    checked_feasible = 0
    checked_conflict = 0
    for _ in range(5000):
        m1 = rng.choice([3, 5, 7, 10])
        m2 = rng.choice([3, 4, 6, 9, 10])
        r1, r2 = rng.randrange(m1), rng.randrange(m1)
        r3, r4 = rng.randrange(m2), rng.randrange(m2)
        secs1 = rng.randint(1, 15)
        secs2 = rng.randint(1, 15)
        rate1 = Fraction(rng.randint(0, 8), rng.randint(1, 3))
        rate2 = Fraction(rng.randint(0, 8), rng.randint(1, 3))
        s1 = solve_segment(0, r1, r2, secs1, m1, rate1)
        s2 = solve_segment(1, r3, r4, secs2, m2, rate2)
        if s1.max_wraps < s1.min_wraps or s2.max_wraps < s2.min_wraps:
            continue  # 单段无解由端点先拦截，不属于联立函数契约

        diff1, diff2 = r2 - r1, r4 - r3
        b1, b2 = (s1.min_wraps, s1.max_wraps), (s2.min_wraps, s2.max_wraps)
        g = math.gcd(m1, m2)
        feas_min = diff1 + diff2 + b1[0] * m1 + b2[0] * m2
        feas_max = diff1 + diff2 + b1[1] * m1 + b2[1] * m2

        # 暴力枚举盒内全部 (k1, k2)：账单总量 -> [min_k1, max_k1, min_k2, max_k2, n]
        totals: dict = {}
        for k1 in range(b1[0], b1[1] + 1):
            for k2 in range(b2[0], b2[1] + 1):
                B = diff1 + diff2 + k1 * m1 + k2 * m2
                if B < 0:
                    continue
                agg = totals.setdefault(B, [k1, k1, k2, k2, 0])
                agg[0] = min(agg[0], k1)
                agg[1] = max(agg[1], k1)
                agg[2] = min(agg[2], k2)
                agg[3] = max(agg[3], k2)
                agg[4] += 1

        # 每个可实现账单总量：闭式结果必须与枚举的逐段圈数界完全一致
        for B, (lo1, hi1, lo2, hi2, count) in totals.items():
            result = reconcile_meter_change(
                (diff1, diff2), (b1, b2), (m1, m2), B
            )
            assert result.status == "ok"
            assert result.modulus_gcd == g
            n1, n2 = result.narrowed
            assert (n1.min_wraps, n1.max_wraps) == (lo1, hi1)
            assert (n2.min_wraps, n2.max_wraps) == (lo2, hi2)
            assert n1.unique == n2.unique == (count == 1)
            u1l, u1h = n1.usage_bounds(m1)
            u2l, u2h = n2.usage_bounds(m2)
            assert u1l == diff1 + lo1 * m1
            assert u1h == diff1 + hi1 * m1
            assert u2l == diff2 + lo2 * m2
            assert u2h == diff2 + hi2 * m2
            # 一段上界与另一段下界互补为账单总量
            assert u1l + u2h == B
            assert u1h + u2l == B
            checked_feasible += 1

        # 冲突样本：同余但在盒外（上下各一），以及一个不同余的值
        candidates = []
        low = feas_min - g
        high = feas_max + g
        if low >= 0:
            candidates.append(low)
        candidates.append(high)
        if feas_min + 1 >= 0:
            candidates.append(feas_min + 1)
        for B in set(candidates):
            if B in totals:
                continue
            result = reconcile_meter_change(
                (diff1, diff2), (b1, b2), (m1, m2), B
            )
            assert result.status == "conflict"
            assert (
                result.conflicts == ("total_incompatible_modulus",)
                if (B - diff1 - diff2) % g != 0
                else result.conflicts == ("total_out_of_range",)
            )
            assert result.feasible_min_total == feas_min
            assert result.feasible_max_total == feas_max
            checked_conflict += 1

    assert checked_feasible > 1000
    assert checked_conflict > 500
