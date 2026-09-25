"""用量复核核心判定逻辑。

对每段相邻读数 (r1, r2)，真实增量为 delta = (r2 - r1) + k * modulus，
其中 k 为整圈回绕次数（非负整数）。约束条件：

    delta >= 0                          （非负增量）
    delta <= max_rate * elapsed_seconds （速率上限）

所有满足约束的 k 构成一个连续整数区间 [k_min, k_max]：

    k_min = 满足 diff + k*M >= 0 的最小非负整数
    k_max = 满足 diff + k*M <= max_delta 的最大整数

判定：
    k_max <  k_min  -> 无解 (no_solution)
    k_max == k_min  -> 唯一解，取该 k
    k_max >  k_min  -> 多解 (ambiguous)，不猜测圈数

速率上限使用 Fraction 精确计算，避免浮点边界误差。

账单总量对账（reconcile_total）：
    设账单总用量为 B，各段首尾读数差之和望远镜相消为首尾读数差：
        B = Σ delta_i = Σ diff_i + M * Σ k_i = (r_last - r_first) + M * K
    故总回绕次数 K = (B - (r_last - r_first)) / M 必须为非负整数
    （账单总量与模数不相容），且满足 Σ k_min <= K <= Σ k_max
    （超出可行总量）。固定 K 后，各段仍可行的圈数范围完全由整数区间
    运算给出，无需枚举组合：

        lo_i' = max(lo_i, K - Σ_j hi_j + hi_i)
        hi_i' = min(hi_i, K - Σ_j lo_j + lo_i)

    lo_i' == hi_i' 的段圈数唯一，可确定用量；其余段仅保留范围，不猜测。

换表账单复核（reconcile_meter_change）：
    旧表终读与新表初读同刻，两表模数 M1、M2 可不同。账单总量 B 跨两表，
    联立条件为 k1*M1 + k2*M2 = B - (diff1 + diff2)。该二元一次不定方程
    有整数解当且仅当 gcd(M1, M2) 整除右端；有解时由扩展欧几里得给出
    通解，两段各自的圈数盒约束化为参数 t 的连续整数区间并直接求交，
    同样不枚举圈数组合。t 唯一时两段用量同时确定，否则各给上下界。
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta, timezone
from fractions import Fraction
from typing import Optional, Sequence, Tuple

STATUS_OK = "ok"
STATUS_NO_SOLUTION = "no_solution"
STATUS_AMBIGUOUS = "ambiguous"
STATUS_CONFLICT = "conflict"

# 账单总量对账冲突原因
REASON_TOTAL_INCOMPATIBLE_MODULUS = "total_incompatible_modulus"
REASON_TOTAL_OUT_OF_RANGE = "total_out_of_range"
# 跨日分配冲突原因：不存在可行的子段整数分配
REASON_NO_FEASIBLE_ALLOCATION = "no_feasible_allocation"


@dataclass(frozen=True)
class SegmentSolution:
    """单个相邻读数区间的判定结果。"""

    index: int
    status: str
    elapsed_seconds: int
    # 唯一解时填充
    wraps: Optional[int]
    usage: Optional[int]
    # 可行回绕次数区间 [min_wraps, max_wraps]；无解时 max_wraps < min_wraps
    min_wraps: int
    max_wraps: int
    max_allowed: Fraction  # 该区间允许的最大增量（速率上限 * 秒数）


def solve_segment(
    index: int,
    start_value: int,
    end_value: int,
    elapsed_seconds: int,
    modulus: int,
    max_rate_per_second: Fraction,
) -> SegmentSolution:
    """判定一段相邻读数的回绕次数是否唯一。"""
    diff = end_value - start_value
    max_allowed = max_rate_per_second * elapsed_seconds

    # k_min: diff + k*M >= 0 的最小非负整数 k
    if diff >= 0:
        k_min = 0
    else:
        # ceil(-diff / M)，此时 0 < -diff < M，结果恒为 1
        k_min = (-diff + modulus - 1) // modulus

    # k_max: diff + k*M <= max_allowed 的最大整数 k
    k_max = math.floor((max_allowed - diff) / modulus)

    if k_max < k_min:
        status, wraps, usage = STATUS_NO_SOLUTION, None, None
    elif k_max == k_min:
        status, wraps = STATUS_OK, k_min
        usage = diff + k_min * modulus
    else:
        status, wraps, usage = STATUS_AMBIGUOUS, None, None

    return SegmentSolution(
        index=index,
        status=status,
        elapsed_seconds=elapsed_seconds,
        wraps=wraps,
        usage=usage,
        min_wraps=k_min,
        max_wraps=k_max,
        max_allowed=max_allowed,
    )


@dataclass(frozen=True)
class NarrowedSegment:
    """账单总量约束后，单段仍可行的回绕次数整数区间。"""

    index: int
    diff: int
    min_wraps: int
    max_wraps: int

    @property
    def unique(self) -> bool:
        """圈数是否已被总量约束唯一确定。"""
        return self.min_wraps == self.max_wraps

    @property
    def wraps(self) -> Optional[int]:
        return self.min_wraps if self.unique else None

    def usage(self, modulus: int) -> Optional[int]:
        """圈数唯一时该段确定用量，否则为 None。"""
        return self.diff + self.min_wraps * modulus if self.unique else None

    def usage_bounds(self, modulus: int) -> Tuple[int, int]:
        """该区间用量的最小/最大可能值。"""
        return (
            self.diff + self.min_wraps * modulus,
            self.diff + self.max_wraps * modulus,
        )


@dataclass(frozen=True)
class ReconcileResult:
    """账单总量对账结果。"""

    status: str  # STATUS_OK 或 STATUS_CONFLICT
    total_wraps: Optional[int]  # 总量隐含的总回绕次数 K（不相容时为 None）
    narrowed: Tuple[NarrowedSegment, ...]
    conflicts: Tuple[str, ...]  # REASON_TOTAL_* 原因
    feasible_min_total: int
    feasible_max_total: int


def reconcile_total(
    diffs: Sequence[int],
    wrap_bounds: Sequence[Tuple[int, int]],
    total_usage: int,
    modulus: int,
) -> ReconcileResult:
    """以账单总用量约束整条读数序列的回绕次数总和。

    :param diffs: 各段首尾读数差 r2 - r1
    :param wrap_bounds: 各段在非负增量与速率上限下的可行圈数区间
        ``(k_min, k_max)``，调用方需保证每段均可行（k_max >= k_min）
    :param total_usage: 账单总用量（非负整数）
    :param modulus: 计数器模数
    :return: 对账结果；可行时 ``narrowed`` 给出各段收紧后的圈数区间
    """
    if len(diffs) != len(wrap_bounds):
        raise ValueError("diffs 与 wrap_bounds 数量必须一致")

    lows = [lo for lo, _ in wrap_bounds]
    highs = [hi for _, hi in wrap_bounds]
    sum_low, sum_high = sum(lows), sum(highs)
    end_minus_start = sum(diffs)  # 望远镜相消：末读数 - 首读数

    feasible_min_total = end_minus_start + modulus * sum_low
    feasible_max_total = end_minus_start + modulus * sum_high

    numerator = total_usage - end_minus_start
    conflicts = []
    # 总量必须与首尾读数差模 M 同余（等价于总回绕次数为非负整数）
    if numerator % modulus != 0:
        conflicts.append(REASON_TOTAL_INCOMPATIBLE_MODULUS)
        total_wraps: Optional[int] = None
    else:
        total_wraps = numerator // modulus
        if not (sum_low <= total_wraps <= sum_high):
            conflicts.append(REASON_TOTAL_OUT_OF_RANGE)

    if conflicts:
        return ReconcileResult(
            status=STATUS_CONFLICT,
            total_wraps=total_wraps,
            narrowed=(),
            conflicts=tuple(conflicts),
            feasible_min_total=feasible_min_total,
            feasible_max_total=feasible_max_total,
        )

    # K 固定：其余各段圈数之和为 K - k_i，由其余段的区间和反推出 k_i 的范围
    narrowed = []
    for i, (lo, hi) in enumerate(wrap_bounds):
        other_high = sum_high - hi  # 除 i 外各段最大圈数之和
        other_low = sum_low - lo  # 除 i 外各段最小圈数之和
        new_lo = max(lo, total_wraps - other_high)
        new_hi = min(hi, total_wraps - other_low)
        narrowed.append(
            NarrowedSegment(
                index=i,
                diff=diffs[i],
                min_wraps=new_lo,
                max_wraps=new_hi,
            )
        )

    return ReconcileResult(
        status=STATUS_OK,
        total_wraps=total_wraps,
        narrowed=tuple(narrowed),
        conflicts=(),
        feasible_min_total=feasible_min_total,
        feasible_max_total=feasible_max_total,
    )


# ---------------------------------------------------------------------------
# 换表账单复核（meter-change reconcile）
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class MeterChangeResult:
    """换表账单复核结果：两段不同模数读数区间与账单总量的联立解。"""

    status: str  # STATUS_OK 或 STATUS_CONFLICT
    modulus_gcd: int  # 两表模数的最大公约数 g
    # 账单总量约束后两段各自的圈数区间（旧表、新表）；冲突时为空
    narrowed: Tuple[NarrowedSegment, ...]
    conflicts: Tuple[str, ...]  # REASON_TOTAL_* 原因
    feasible_min_total: int
    feasible_max_total: int


def _extended_gcd(a: int, b: int) -> Tuple[int, int, int]:
    """扩展欧几里得：返回 (g, u, v)，使 u*a + v*b = g = gcd(a, b)（a, b > 0）。"""
    old_r, r = a, b
    old_s, s = 1, 0
    old_t, t = 0, 1
    while r:
        q = old_r // r
        old_r, r = r, old_r - q * r
        old_s, s = s, old_s - q * s
        old_t, t = t, old_t - q * t
    return old_r, old_s, old_t


def _ceil_div(n: int, d: int) -> int:
    """n / d 向上取整（d > 0，n 可为负）。"""
    return -((-n) // d)


def reconcile_meter_change(
    diffs: Tuple[int, int],
    wrap_bounds: Tuple[Tuple[int, int], Tuple[int, int]],
    moduli: Tuple[int, int],
    total_usage: int,
) -> MeterChangeResult:
    """联立两段（旧表、新表）用量之和与跨表账单总量，模数可不同。

    设两段读数差为 diff1、diff2，模数为 M1、M2，真实增量为
    ``delta_i = diff_i + k_i * M_i``（k_i 为非负整数，且各自落在满足
    非负增量与速率上限的区间 ``[lo_i, hi_i]`` 内，调用方需保证两段均可行）。
    账单总量 B 要求：

        k1 * M1 + k2 * M2 = B - (diff1 + diff2) =: R

    这是二元一次不定方程。记 g = gcd(M1, M2)：

    * R 不是 g 的倍数 -> 无整数解（total_incompatible_modulus）；
    * 否则由扩展欧几里得得特解 (k1_0, k2_0)，通解为
      ``k1 = k1_0 + (M2/g) * t``，``k2 = k2_0 - (M1/g) * t``（t 为整数）。
      两段的盒约束 ``lo_i <= k_i <= hi_i`` 各自化为 t 的线性不等式，
      交集仍是连续整数区间 [t_lo, t_hi]，**闭式求交，不枚举圈数组合**；
    * t_lo > t_hi -> 盒内无解（total_out_of_range，含 R < 0 的情形）。

    t 唯一当且仅当两段圈数同时唯一（k1、k2 均随 t 严格单调），此时两段
    用量同时确定；否则各自给出可行用量上下界。所有可行 t 下两段用量之和
    恒为 B，故一段的上界与另一段的下界互补（delta1_max + delta2_min = B）。
    """
    diff1, diff2 = diffs
    (lo1, hi1), (lo2, hi2) = wrap_bounds
    m1, m2 = moduli

    g = math.gcd(m1, m2)
    base = diff1 + diff2  # 两段读数差之和
    feasible_min_total = base + lo1 * m1 + lo2 * m2
    feasible_max_total = base + hi1 * m1 + hi2 * m2
    remainder = total_usage - base  # R = k1*M1 + k2*M2 须等于它

    def _conflict(reason: str) -> MeterChangeResult:
        return MeterChangeResult(
            status=STATUS_CONFLICT,
            modulus_gcd=g,
            narrowed=(),
            conflicts=(reason,),
            feasible_min_total=feasible_min_total,
            feasible_max_total=feasible_max_total,
        )

    # 总量必须与读数差之和模 g 同余，否则不定方程无整数解
    if remainder % g != 0:
        return _conflict(REASON_TOTAL_INCOMPATIBLE_MODULUS)

    # 贝祖特解：u*M1 + v*M2 = g，两边乘 R/g 得一组 (k1_0, k2_0)
    _, u, v = _extended_gcd(m1, m2)
    n = remainder // g
    k1_0, k2_0 = u * n, v * n
    step1, step2 = m2 // g, m1 // g  # k1 = k1_0 + step1*t；k2 = k2_0 - step2*t

    # 盒约束化为 t 的双侧不等式（step1、step2 均为正）
    t_lo = max(
        _ceil_div(lo1 - k1_0, step1),  # lo1 <= k1_0 + step1*t
        _ceil_div(k2_0 - hi2, step2),  # k2_0 - step2*t <= hi2
    )
    t_hi = min(
        (hi1 - k1_0) // step1,  # k1_0 + step1*t <= hi1（// 即向下取整）
        (k2_0 - lo2) // step2,  # lo2 <= k2_0 - step2*t
    )

    if t_lo > t_hi:
        return _conflict(REASON_TOTAL_OUT_OF_RANGE)

    # t 增大时 k1 增、k2 减，端点处分别取得两段各自的圈数界
    narrowed = (
        NarrowedSegment(
            index=0,
            diff=diff1,
            min_wraps=k1_0 + step1 * t_lo,
            max_wraps=k1_0 + step1 * t_hi,
        ),
        NarrowedSegment(
            index=1,
            diff=diff2,
            min_wraps=k2_0 - step2 * t_hi,
            max_wraps=k2_0 - step2 * t_lo,
        ),
    )
    return MeterChangeResult(
        status=STATUS_OK,
        modulus_gcd=g,
        narrowed=narrowed,
        conflicts=(),
        feasible_min_total=feasible_min_total,
        feasible_max_total=feasible_max_total,
    )


# ---------------------------------------------------------------------------
# 跨日用量上下界（daily-range）
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class DaySubsegment:
    """读数区间按 UTC 午夜切出的一个子段（完全落在单个 UTC 自然日内）。"""

    day: date
    seconds: int
    capacity: int  # floor(max_rate_per_second * seconds)


@dataclass(frozen=True)
class DailySegmentResult:
    """一个读数区间跨日切分后的可行分配结果。"""

    index: int
    feasible: bool
    # 该区间可行的回绕次数范围；无解时 max_wraps < min_wraps
    min_wraps: int
    max_wraps: int
    subsegments: Tuple[DaySubsegment, ...]
    # 每个子段的精确边际上下界（与 subsegments 一一对应）；无解时为空
    min_usages: Tuple[int, ...]
    max_usages: Tuple[int, ...]

    @property
    def total_capacity(self) -> int:
        return sum(s.capacity for s in self.subsegments)


def split_utc_days(start: datetime, end: datetime) -> Tuple[Tuple[date, int], ...]:
    """将 [start, end) 按 UTC 午夜切成 ``(UTC 日期, 该日子段秒数)`` 序列。

    端点均为秒精度、带时区的严格递增时间戳。子段在交界处首尾相接
    （零长度子段被丢弃），秒数之和恰为区间总时长。
    """
    start = start.astimezone(timezone.utc)
    end = end.astimezone(timezone.utc)
    pieces = []
    cursor = start
    while cursor < end:
        next_midnight = datetime.combine(
            cursor.date() + timedelta(days=1), time.min, tzinfo=timezone.utc
        )
        boundary = min(end, next_midnight)
        seconds = int((boundary - cursor).total_seconds())
        if seconds > 0:
            pieces.append((cursor.date(), seconds))
        cursor = boundary
    return tuple(pieces)


def solve_daily_segment(
    index: int,
    start_value: int,
    end_value: int,
    start: datetime,
    end: datetime,
    modulus: int,
    max_rate_per_second: Fraction,
) -> DailySegmentResult:
    """求单个读数区间跨各 UTC 日子段用量的精确边际上下界。

    区间真实增量为 ``delta = diff + k*M``（diff = r2 - r1，k >= 0 整数），
    切日子段容量为 ``c_j = floor(rate * s_j)``。需存在非负整数分配
    ``(x_1, ..., x_n)`` 满足 ``0 <= x_j <= c_j`` 且 ``Σ x_j = delta``。

    该类分配存在当且仅当 ``delta <= C``（``C = Σ c_j``）：因为所有容量为
    整数，``[0, C]`` 内任意整数总量都可逐子段填满。故可行 k 仍为连续
    整数区间 ``[k_min, k_max]``，其中

        k_min = ceil(max(0, -diff) / M)
        k_max = floor((C - diff) / M)

    对固定子段 j，给定可行 k 时其边际范围为

        max(0, delta - (C - c_j)) <= x_j <= min(c_j, delta)

    下界随 delta 单调增，故全区间最小值在 k_min 处取得；上界随 delta
    单调增，最大值在 k_max 处取得。两端点均确实可由完整分配达到
    （箱型可行域与固定总量超平面之交的逐分量极值）。
    """
    diff = end_value - start_value

    subsegments = tuple(
        DaySubsegment(
            day=day,
            seconds=seconds,
            # Fraction 精确取整，避免浮点边界误差
            capacity=math.floor(max_rate_per_second * seconds),
        )
        for day, seconds in split_utc_days(start, end)
    )
    total_capacity = sum(s.capacity for s in subsegments)

    k_min = 0 if diff >= 0 else (-diff + modulus - 1) // modulus
    k_max = (total_capacity - diff) // modulus

    if k_max < k_min:
        return DailySegmentResult(
            index=index,
            feasible=False,
            min_wraps=k_min,
            max_wraps=k_max,
            subsegments=subsegments,
            min_usages=(),
            max_usages=(),
        )

    delta_min = diff + k_min * modulus
    delta_max = diff + k_max * modulus

    min_usages, max_usages = [], []
    for s in subsegments:
        other_capacity = total_capacity - s.capacity
        # x_j >= delta - 其余子段容量之和；delta 取最小时下界最小
        low = max(0, delta_min - other_capacity)
        # x_j <= delta（且不超本段子容量）；delta 取最大时上界最大
        high = min(s.capacity, delta_max)
        min_usages.append(low)
        max_usages.append(high)

    return DailySegmentResult(
        index=index,
        feasible=True,
        min_wraps=k_min,
        max_wraps=k_max,
        subsegments=subsegments,
        min_usages=tuple(min_usages),
        max_usages=tuple(max_usages),
    )
