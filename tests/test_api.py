"""接口与判定逻辑测试。"""

import itertools
import math
import random
from datetime import datetime, timedelta, timezone
from fractions import Fraction

from fastapi.testclient import TestClient

from app.core import reconcile_meter_total, solve_daily_segment
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

def _meter_payload(total_usage, old_mod=1000, new_mod=1500, rate=10, secs=1000,
                   old_values=(900, 50), new_values=(1200, 300),
                   junction="2026-09-24T00:16:40Z",
                   old_start="2026-09-24T00:00:00Z",
                   new_end="2026-09-24T00:33:20Z"):
    return {
        "total_usage": total_usage,
        "old_meter": {
            "modulus": old_mod,
            "max_rate_per_second": rate,
            "start": {"timestamp": old_start, "value": old_values[0]},
            "end": {"timestamp": junction, "value": old_values[1]},
        },
        "new_meter": {
            "modulus": new_mod,
            "max_rate_per_second": rate,
            "start": {"timestamp": junction, "value": new_values[0]},
            "end": {"timestamp": new_end, "value": new_values[1]},
        },
    }


def test_meter_change_unique_both_segments():
    """B=2250：旧表 1 圈(150)、新表 2 圈(2100)，两段同时唯一。"""
    resp = _meter_change(_meter_payload(2250))
    assert resp.status_code == 200
    body = resp.json()
    assert body["status"] == "ok"
    assert body["unique"] is True
    assert body["conflicts"] == []
    old, new = body["segments"]
    assert old["meter"] == "old" and new["meter"] == "new"
    assert (old["wraps"], old["usage"]) == (1, 150)
    assert (new["wraps"], new["usage"]) == (2, 2100)
    assert old["min_usage"] is None and old["max_usage"] is None
    assert new["min_usage"] is None and new["max_usage"] is None
    assert old["elapsed_seconds"] == 1000
    assert old["modulus"] == 1000 and new["modulus"] == 1500


def test_meter_change_ambiguous_ranges_sum_to_bill():
    """B=4750：两表模数不同（1000/1500，gcd=500），各段仍有多解。

    旧表可行圈数 1..7（步长 3），用量 [150,6150]∩账单 -> [1150,4150]；
    新表可行圈数 1..3（步长 2），用量 [600,3600]；任一可行组合两段
    用量之和恒为 4750。
    """
    resp = _meter_change(_meter_payload(4750))
    body = resp.json()
    assert body["status"] == "ok"
    assert body["unique"] is False
    old, new = body["segments"]
    assert old["wraps"] is None and old["usage"] is None
    assert new["wraps"] is None and new["usage"] is None
    assert (old["min_usage"], old["max_usage"]) == (1150, 4150)
    assert (new["min_usage"], new["max_usage"]) == (600, 3600)
    assert (old["min_wraps"], old["max_wraps"], old["wraps_step"]) == (2, 5, 3)
    assert (new["min_wraps"], new["max_wraps"], new["wraps_step"]) == (1, 3, 2)
    # 端点组合严格对账
    assert old["min_usage"] + new["max_usage"] == 4750
    assert old["max_usage"] + new["min_usage"] == 4750


def test_meter_change_equal_moduli_matches_single_modulus():
    """两表模数相同时，与统一模数的总量对账同余规则一致。"""
    resp = _meter_change(
        _meter_payload(2250, old_mod=1000, new_mod=1000, new_values=(200, 300))
    )
    body = resp.json()
    assert body["status"] == "ok"
    old, new = body["segments"]
    # 旧 d=-850、新 d=100，B=2250 => k1+k2=3；边界内多解
    assert (old["min_usage"], old["max_usage"]) == (150, 2150)
    assert (new["min_usage"], new["max_usage"]) == (100, 2100)
    assert old["min_usage"] + new["max_usage"] == 2250


def test_meter_change_incompatible_modulus():
    """B 不被 gcd(1000,1500)=500 整除：total_incompatible_modulus。"""
    resp = _meter_change(_meter_payload(4751))
    body = resp.json()
    assert body["status"] == "conflict"
    assert body["segments"] == []
    conflict = body["conflicts"][0]
    assert conflict["kind"] == "total"
    assert conflict["reason"] == "total_incompatible_modulus"
    assert conflict["modulus_gcd"] == 500
    assert conflict["old_modulus"] == 1000
    assert conflict["new_modulus"] == 1500
    # d_old + d_new = -850 - 900 = -1750 ≡ 250 (mod 500)
    assert conflict["expected_remainder"] == 250
    assert conflict["feasible_min_total"] == 750
    assert conflict["feasible_max_total"] == 18750


def test_meter_change_out_of_range_above_box():
    """B 与 gcd 相容但远超可行区间：total_out_of_range。

    相容值须 B ≡ d1+d2 = -1750 ≡ 250 (mod 500)；19250 相容但大于
    可行上界 18750。
    """
    resp = _meter_change(_meter_payload(19250))
    body = resp.json()
    conflict = body["conflicts"][0]
    assert body["status"] == "conflict"
    assert conflict["kind"] == "total"
    assert conflict["reason"] == "total_out_of_range"
    assert (conflict["feasible_min_total"], conflict["feasible_max_total"]) == (
        750,
        18750,
    )
    assert conflict["expected_remainder"] is None


def test_meter_change_in_box_but_no_integer_pair():
    """B=1250 落在 [750,18750] 内且被 500 整除，但联立无整数组合。

    方程 1000k1 + 1500k2 = 3000 即 2k1+3k2=6，解族 k1∈{0,3}（k2={2,0}），
    两段均要求 k>=1，故全部落在可行区间之外。
    """
    resp = _meter_change(_meter_payload(1250))
    body = resp.json()
    conflict = body["conflicts"][0]
    assert conflict["reason"] == "total_out_of_range"
    # detail 需明确指出"数值在区间内但无整数组合"这一可区分情形
    assert "落在上述区间内" in conflict["detail"]


def test_meter_change_old_segment_no_solution():
    """旧表段本身超过速率上限：分段冲突，不做总量联立。"""
    payload = _meter_payload(2250, rate=10, secs=1)
    payload["old_meter"]["end"] = {
        "timestamp": "2026-09-24T00:00:01Z",
        "value": 50,
    }
    payload["new_meter"]["start"] = {
        "timestamp": "2026-09-24T00:00:01Z",
        "value": 1200,
    }
    payload["new_meter"]["end"] = {
        "timestamp": "2026-09-24T00:16:41Z",
        "value": 300,
    }
    resp = _meter_change(payload)
    body = resp.json()
    assert body["status"] == "conflict"
    assert body["segments"] == []
    conflict = body["conflicts"][0]
    assert conflict["kind"] == "segment"
    assert conflict["meter"] == "old"
    assert conflict["reason"] == "no_solution"
    assert conflict["max_wraps"] < conflict["min_wraps"]


def test_meter_change_both_segments_no_solution_listed():
    """两段都无解时一并列出。"""
    payload = _meter_payload(2250, rate=1, secs=1)
    payload["old_meter"]["end"] = {
        "timestamp": "2026-09-24T00:00:01Z",
        "value": 50,
    }
    payload["new_meter"]["start"] = {
        "timestamp": "2026-09-24T00:00:01Z",
        "value": 1200,
    }
    payload["new_meter"]["end"] = {
        "timestamp": "2026-09-24T00:00:02Z",
        "value": 300,
    }
    resp = _meter_change(payload)
    body = resp.json()
    assert body["status"] == "conflict"
    assert [c["meter"] for c in body["conflicts"]] == ["old", "new"]
    assert all(c["reason"] == "no_solution" for c in body["conflicts"])


def test_meter_change_junction_must_be_same_instant():
    """旧表终读与新表初读不同刻：422。"""
    payload = _meter_payload(2250)
    payload["new_meter"]["start"]["timestamp"] = "2026-09-24T00:16:41Z"
    resp = _meter_change(payload)
    assert resp.status_code == 422


def test_meter_change_timezone_junction_normalized():
    """交界刻以不同时区表达但同一时刻：换算 UTC 后通过，结果不受影响。"""
    payload = _meter_payload(2250)
    # 2026-09-24T08:16:40+08:00 == 2026-09-24T00:16:40Z
    payload["old_meter"]["end"]["timestamp"] = "2026-09-24T08:16:40+08:00"
    resp = _meter_change(payload)
    assert resp.status_code == 200
    assert resp.json()["status"] == "ok"


def test_meter_change_reuses_reading_validation():
    """复用现有校验：读数越界、时间不递增、缺时区、亚秒、负速率均 422。"""
    base = _meter_payload(2250)

    def expect_422(mut):
        import copy

        payload = copy.deepcopy(base)
        mut(payload)
        assert _meter_change(payload).status_code == 422

    # 旧表读数越界
    expect_422(lambda p: p["old_meter"]["start"].update(value=1000))
    # 新表读数越界（M=1500）
    expect_422(lambda p: p["new_meter"]["end"].update(value=1500))
    # 旧表内部时间不递增
    expect_422(
        lambda p: p["old_meter"]["start"].update(
            timestamp="2026-09-24T00:16:40Z"
        )
    )
    # 缺时区
    expect_422(
        lambda p: p["old_meter"]["start"].update(
            timestamp="2026-09-24T00:00:00"
        )
    )
    # 亚秒
    expect_422(
        lambda p: p["old_meter"]["start"].update(
            timestamp="2026-09-24T00:00:00.5Z"
        )
    )
    # 负速率（各自表独立校验）
    expect_422(lambda p: p["new_meter"].update(max_rate_per_second=-1))
    # 模数 < 1
    expect_422(lambda p: p["old_meter"].update(modulus=0))
    # 账单总量为负
    expect_422(lambda p: p.update(total_usage=-1))


# ---------------------------------------------------------------------------
# reconcile_meter_total 精确性：与全枚举暴力结果比对（固定随机种子）
# ---------------------------------------------------------------------------

def test_reconcile_meter_total_exact_by_brute_force():
    """随机小参数下，丢番图通解区间与枚举全部 (k1,k2) 的结果一致。"""
    rng = random.Random(20260925)
    checked = 0
    for _ in range(4000):
        m1 = rng.choice([2, 3, 4, 5, 6, 7])
        m2 = rng.choice([2, 3, 4, 5, 6, 7, 9])
        d1 = rng.randrange(m1) - rng.randrange(m1)
        d2 = rng.randrange(m2) - rng.randrange(m2)
        lo1 = 0 if d1 >= 0 else math.ceil(-d1 / m1)
        lo2 = 0 if d2 >= 0 else math.ceil(-d2 / m2)
        hi1 = lo1 + rng.randint(0, 5)
        hi2 = lo2 + rng.randint(0, 5)

        feasible_B = sorted(
            {
                d1 + k1 * m1 + d2 + k2 * m2
                for k1 in range(lo1, hi1 + 1)
                for k2 in range(lo2, hi2 + 1)
            }
        )
        # 抽测可行 B（含全部最小/最大值）与若干不可行 B
        targets = set(feasible_B)
        for _ in range(3):
            if feasible_B:
                base_b = rng.choice(feasible_B)
                targets.add(base_b + rng.choice([-3, -2, -1, 1, 2, 3]))
        targets.update(
            [feasible_B[0] - 1, feasible_B[-1] + 1] if feasible_B else [d1 + d2]
        )

        for B in targets:
            r = reconcile_meter_total(
                d1, (lo1, hi1), m1, d2, (lo2, hi2), m2, B
            )
            pairs = [
                (k1, k2)
                for k1 in range(lo1, hi1 + 1)
                for k2 in range(lo2, hi2 + 1)
                if d1 + k1 * m1 + d2 + k2 * m2 == B
            ]
            if pairs:
                checked += 1
                assert r.status == "ok", (m1, m2, d1, d2, B, r.conflicts)
                k1s = sorted({p[0] for p in pairs})
                k2s = sorted({p[1] for p in pairs})
                os_, ns = r.old_segment, r.new_segment
                assert (os_.min_wraps, os_.max_wraps) == (k1s[0], k1s[-1])
                assert (ns.min_wraps, ns.max_wraps) == (k2s[0], k2s[-1])
                assert os_.unique == ns.unique == (len(pairs) == 1)
                if len(k1s) > 1:
                    assert os_.wraps_step == k1s[1] - k1s[0]
                    assert ns.wraps_step == k2s[1] - k2s[0]
                # 每个端点都严格对账
                assert (
                    d1 + os_.min_wraps * m1 + d2 + ns.max_wraps * m2 == B
                )
                assert (
                    d1 + os_.max_wraps * m1 + d2 + ns.min_wraps * m2 == B
                )
            else:
                g = math.gcd(m1, m2)
                expect = (
                    "total_incompatible_modulus"
                    if (B - d1 - d2) % g != 0
                    else "total_out_of_range"
                )
                assert r.status == "conflict"
                assert r.conflicts == (expect,), (m1, m2, d1, d2, B)
                assert r.old_segment is None and r.new_segment is None
    assert checked > 500
