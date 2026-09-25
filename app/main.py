"""累计水表读数用量复核 API。"""

from __future__ import annotations

from datetime import date as date_cls, datetime, timezone
from decimal import Decimal
from fractions import Fraction
from typing import List, Literal, Optional, Union

from fastapi import FastAPI
from pydantic import BaseModel, Field, field_validator, model_validator

from app.core import (
    REASON_NO_FEASIBLE_ALLOCATION,
    REASON_TOTAL_INCOMPATIBLE_MODULUS,
    REASON_TOTAL_OUT_OF_RANGE,
    STATUS_AMBIGUOUS,
    STATUS_CONFLICT,
    STATUS_NO_SOLUTION,
    STATUS_OK,
    reconcile_meter_total,
    reconcile_total,
    solve_daily_segment,
    solve_segment,
)

app = FastAPI(
    title="Water Meter Usage Verifier",
    version="1.3.0",
    description=(
        "累计水表读数用量复核：判定相邻读数间的整圈回绕次数是否唯一，"
        "唯一时给出分段与总用量；另支持账单总量对账，以账单总用量约束"
        "整条序列的回绕次数总和，仅确定圈数已唯一的分段用量；"
        "还支持按 UTC 自然日给出覆盖时段内每日最小/最大可行整数用量；"
        "并支持换表账单复核：跨旧表/新表两段（模数可不同）以账单总用量"
        "精确联立两段回绕次数，不枚举圈数组合。"
    ),
)


# ---------------------------------------------------------------------------
# 请求模型
# ---------------------------------------------------------------------------

class Reading(BaseModel):
    """一条秒精度 UTC 时间戳及其对应读数。"""

    timestamp: datetime = Field(
        description="秒精度时间戳，须带时区（建议 UTC，如 2026-09-24T00:00:00Z）"
    )
    value: int = Field(ge=0, description="计数器读数，须满足 0 <= value < modulus")

    @field_validator("timestamp")
    @classmethod
    def _normalize_timestamp(cls, v: datetime) -> datetime:
        if v.tzinfo is None or v.utcoffset() is None:
            raise ValueError("timestamp 必须携带时区信息（UTC 时间戳）")
        if v.microsecond != 0:
            raise ValueError("timestamp 必须为秒精度，不允许亚秒部分")
        return v.astimezone(timezone.utc)


class _BaseUsageRequest(BaseModel):
    """读数复核 / 账单对账共用的读数与校验参数。"""

    modulus: int = Field(ge=1, description="计数器模数（回绕周期）")
    max_rate_per_second: Decimal = Field(
        ge=0, description="每秒最大计数增量，支持小数（按精确分数计算）"
    )
    readings: List[Reading] = Field(
        min_length=2, description="按时间排列的读数序列，至少 2 条"
    )

    @model_validator(mode="after")
    def _check_readings(self) -> "_BaseUsageRequest":
        for r in self.readings:
            if r.value >= self.modulus:
                raise ValueError(
                    f"读数 {r.value} 超出模数范围 [0, {self.modulus})"
                )
        for prev, cur in zip(self.readings, self.readings[1:]):
            if cur.timestamp <= prev.timestamp:
                raise ValueError(
                    "时间戳必须严格递增："
                    f"{prev.timestamp.isoformat()} 之后出现 {cur.timestamp.isoformat()}"
                )
        return self


class VerifyRequest(_BaseUsageRequest):
    """分段用量复核请求。"""


class ReconcileRequest(_BaseUsageRequest):
    """账单总量对账请求。"""

    total_usage: int = Field(
        ge=0, description="账单总用量（非负整数）"
    )


class DailyRangeRequest(_BaseUsageRequest):
    """按 UTC 自然日的每日用量上下界请求（读数/模数/速率校验与其他接口一致）。"""


# ---------------------------------------------------------------------------
# 换表账单复核请求模型
# ---------------------------------------------------------------------------


class MeterBlock(BaseModel):
    """单块表（旧表或新表）的参数与其段内首尾读数。"""

    modulus: int = Field(ge=1, description="该表计数器模数（回绕周期）")
    max_rate_per_second: Decimal = Field(
        ge=0, description="该表每秒最大计数增量，支持小数（按精确分数计算）"
    )
    start: Reading = Field(description="段内首读数（旧表初读 / 新表初读）")
    end: Reading = Field(description="段内终读数（旧表终读 / 新表终读）")

    @model_validator(mode="after")
    def _check_block(self) -> "MeterBlock":
        for r in (self.start, self.end):
            if r.value >= self.modulus:
                raise ValueError(
                    f"读数 {r.value} 超出模数范围 [0, {self.modulus})"
                )
        if self.end.timestamp <= self.start.timestamp:
            raise ValueError(
                "时间戳必须严格递增："
                f"{self.start.timestamp.isoformat()} 之后出现 "
                f"{self.end.timestamp.isoformat()}"
            )
        return self


class MeterChangeRequest(BaseModel):
    """换表账单复核请求：跨旧表、新表两段，模数与速率可各自不同。"""

    total_usage: int = Field(ge=0, description="跨两表的账单总用量（非负整数）")
    old_meter: MeterBlock = Field(description="旧表参数与起止读数")
    new_meter: MeterBlock = Field(description="新表参数与起止读数")

    @model_validator(mode="after")
    def _check_junction(self) -> "MeterChangeRequest":
        # 换表刻：旧表终读与新表初读必须为同一时刻（读数本身可不同）
        if self.old_meter.end.timestamp != self.new_meter.start.timestamp:
            raise ValueError(
                "旧表终读与新表初读必须为同一时刻（换表刻）：旧表终读 "
                f"{self.old_meter.end.timestamp.isoformat()} 与新表初读 "
                f"{self.new_meter.start.timestamp.isoformat()} 不一致"
            )
        return self


# ---------------------------------------------------------------------------
# 复核接口响应模型
# ---------------------------------------------------------------------------

class SegmentOut(BaseModel):
    index: int
    start_timestamp: datetime
    end_timestamp: datetime
    start_reading: int
    end_reading: int
    elapsed_seconds: int
    wraps: int = Field(description="判定的整圈回绕次数")
    usage: int = Field(description="该区间用量（计数增量）")


class SegmentErrorOut(BaseModel):
    index: int
    start_timestamp: datetime
    end_timestamp: datetime
    start_reading: int
    end_reading: int
    elapsed_seconds: int
    reason: Literal["no_solution", "ambiguous"]
    detail: str
    min_wraps: int = Field(description="满足非负增量约束的最小回绕次数")
    max_wraps: int = Field(description="满足速率上限约束的最大回绕次数")


class VerifyResponse(BaseModel):
    status: Literal["ok", "error"]
    modulus: int
    segments: List[SegmentOut] = Field(description="回绕次数唯一的区间")
    errors: List[SegmentErrorOut] = Field(description="无解或多解的出错区间")
    total_usage: Optional[int] = Field(
        description="全部区间唯一时的总用量；存在出错区间时为 null"
    )


# ---------------------------------------------------------------------------
# 账单对账接口响应模型
# ---------------------------------------------------------------------------

class SegmentRangeOut(BaseModel):
    index: int
    start_timestamp: datetime
    end_timestamp: datetime
    start_reading: int
    end_reading: int
    elapsed_seconds: int
    min_wraps: int = Field(description="账单总量约束后仍可行的最小回绕次数")
    max_wraps: int = Field(description="账单总量约束后仍可行的最大回绕次数")
    wraps: Optional[int] = Field(
        default=None, description="圈数唯一时的回绕次数；仍有多解时为 null"
    )
    usage: Optional[int] = Field(
        default=None, description="圈数唯一时该段的确定用量；仍有多解时为 null"
    )
    min_usage: Optional[int] = Field(
        default=None, description="仍有多解时该段用量的下界；圈数唯一时为 null"
    )
    max_usage: Optional[int] = Field(
        default=None, description="仍有多解时该段用量的上界；圈数唯一时为 null"
    )


class ReconcileSegmentErrorOut(BaseModel):
    kind: Literal["segment"] = Field(description="冲突层级：分段")
    index: int
    start_timestamp: datetime
    end_timestamp: datetime
    start_reading: int
    end_reading: int
    elapsed_seconds: int
    reason: Literal["no_solution"]
    detail: str
    min_wraps: int
    max_wraps: int


class ReconcileTotalErrorOut(BaseModel):
    kind: Literal["total"] = Field(description="冲突层级：账单总量")
    reason: Literal[
        "total_incompatible_modulus", "total_out_of_range"
    ]
    detail: str
    feasible_min_total: int = Field(description="可行账单总量的下界（含）")
    feasible_max_total: int = Field(description="可行账单总量的上界（含）")
    expected_remainder: Optional[int] = Field(
        default=None, description="与模数相容时账单总量应有的模 M 余数"
    )
    total_wraps: Optional[int] = Field(
        default=None,
        description="账单总量隐含的总回绕次数 K；与模数不相容时为 null",
    )


class ReconcileResponse(BaseModel):
    status: Literal["ok", "conflict"]
    modulus: int
    total_usage: int = Field(description="请求给出的账单总用量")
    total_wraps: Optional[int] = Field(
        default=None,
        description="账单总量隐含的总回绕次数 K；冲突时可能为 null",
    )
    segments: List[SegmentRangeOut] = Field(
        description="账单总量约束后的各段结果：圈数唯一者给出确定用量，其余给出范围"
    )
    conflicts: List[
        Union[ReconcileSegmentErrorOut, ReconcileTotalErrorOut]
    ] = Field(description="冲突明细：某段原本无解，或账单总量与模数不相容/超界")


# ---------------------------------------------------------------------------
# 每日用量上下界接口响应模型
# ---------------------------------------------------------------------------

# 每日边界的独立性说明：逐日边际极值，不保证跨日同时达到
DAILY_BOUNDARY_NOTE = (
    "每个 UTC 自然日的 min_usage/max_usage 均可由某个可行的回绕次数及"
    "子段整数分配分别达到；不同日期（以及同一读数区间内各子段）的上界"
    "不保证存在同一个分配同时成立，请勿将各日上界相加后作为整体上界使用"
)


class DailyUsageOut(BaseModel):
    date: date_cls = Field(description="UTC 自然日")
    min_usage: int = Field(
        description="当日所有落在该日的子段用量下界之和（非负整数，可达到）"
    )
    max_usage: int = Field(
        description="当日所有落在该日的子段用量上界之和（非负整数，可达到）"
    )
    segment_count: int = Field(
        description="当日覆盖到的读数区间子段数（同一区间至多贡献一个子段）"
    )


class DailyRangeConflictOut(BaseModel):
    index: int
    start_timestamp: datetime
    end_timestamp: datetime
    start_reading: int
    end_reading: int
    elapsed_seconds: int
    reason: Literal["no_feasible_allocation"]
    detail: str
    min_wraps: int = Field(description="满足非负增量约束的最小回绕次数")
    max_wraps: int = Field(
        description="满足跨日子段总容量约束的最大回绕次数；小于 min_wraps 表示无解"
    )
    total_capacity: int = Field(
        description="区间内各 UTC 日子段容量 floor(速率×秒数) 之和"
    )


class DailyRangeResponse(BaseModel):
    status: Literal["ok", "conflict"]
    modulus: int
    days: List[DailyUsageOut] = Field(
        description="覆盖时段内各 UTC 自然日的最小/最大可行整数用量；冲突时为空"
    )
    boundary_note: str = Field(
        description="每日边界可分别达到、不保证各日上界同时成立的说明"
    )
    conflicts: List[DailyRangeConflictOut] = Field(
        description="不存在可行子段分配的读数区间及冲突原因；非空时 days 为空"
    )


# ---------------------------------------------------------------------------
# 换表账单复核接口响应模型
# ---------------------------------------------------------------------------


class MeterSegmentRangeOut(BaseModel):
    meter: Literal["old", "new"] = Field(description="所属表：旧表 / 新表")
    start_timestamp: datetime
    end_timestamp: datetime
    start_reading: int
    end_reading: int
    elapsed_seconds: int
    modulus: int = Field(description="该表模数")
    min_wraps: int = Field(description="账单总量联立后仍可行的最小回绕次数")
    max_wraps: int = Field(description="账单总量联立后仍可行的最大回绕次数")
    wraps_step: int = Field(
        description="可行回绕次数序列的公差（模 gcd 归约后的对侧模数）"
    )
    wraps: Optional[int] = Field(
        default=None, description="圈数唯一时的回绕次数；仍有多解时为 null"
    )
    usage: Optional[int] = Field(
        default=None, description="圈数唯一时该段的确定用量；仍有多解时为 null"
    )
    min_usage: Optional[int] = Field(
        default=None, description="仍有多解时该段用量的下界；圈数唯一时为 null"
    )
    max_usage: Optional[int] = Field(
        default=None, description="仍有多解时该段用量的上界；圈数唯一时为 null"
    )


class MeterSegmentErrorOut(BaseModel):
    kind: Literal["segment"] = Field(description="冲突层级：分段")
    meter: Literal["old", "new"]
    start_timestamp: datetime
    end_timestamp: datetime
    start_reading: int
    end_reading: int
    elapsed_seconds: int
    modulus: int
    reason: Literal["no_solution"]
    detail: str
    min_wraps: int
    max_wraps: int


class MeterTotalErrorOut(BaseModel):
    kind: Literal["total"] = Field(description="冲突层级：账单总量")
    reason: Literal[
        "total_incompatible_modulus", "total_out_of_range"
    ]
    detail: str
    old_modulus: int
    new_modulus: int
    modulus_gcd: int = Field(description="两表模数的最大公约数 gcd(M_old, M_new)")
    feasible_min_total: int = Field(
        description="不联立账单时两段可行用量之和的下界（含）"
    )
    feasible_max_total: int = Field(
        description="不联立账单时两段可行用量之和的上界（含）"
    )
    expected_remainder: Optional[int] = Field(
        default=None,
        description="与两表模数相容时账单总量应有的模 gcd 余数",
    )


class MeterChangeResponse(BaseModel):
    status: Literal["ok", "conflict"]
    total_usage: int = Field(description="请求给出的跨两表账单总用量")
    unique: bool = Field(
        description="两段用量是否都被账单总量唯一确定（同时唯一）"
    )
    segments: List[MeterSegmentRangeOut] = Field(
        description="联立后的两段结果，按旧表（meter=old）、新表（meter=new）顺序；冲突时为空"
    )
    conflicts: List[
        Union[MeterSegmentErrorOut, MeterTotalErrorOut]
    ] = Field(description="冲突明细：某段原本无解，或账单总量不相容/超界")


# ---------------------------------------------------------------------------
# 公共逻辑
# ---------------------------------------------------------------------------

def _no_solution_detail(diff: int, modulus: int, sol, elapsed: int) -> str:
    min_delta = diff + sol.min_wraps * modulus
    return (
        f"无解：最可能增量 {min_delta} 已超出该区间 "
        f"{elapsed}s 内允许的最大增量 {sol.max_allowed}"
        f"（k 需同时满足 k>={sol.min_wraps} 与 k<={sol.max_wraps}，区间为空）"
    )


def _solve_all(req: _BaseUsageRequest):
    """对每段执行现有读数/模数/速率校验，返回 (段上下文, 判定结果) 列表。"""
    max_rate = Fraction(req.max_rate_per_second)
    results = []
    for i, (a, b) in enumerate(zip(req.readings, req.readings[1:])):
        elapsed = int((b.timestamp - a.timestamp).total_seconds())
        sol = solve_segment(i, a.value, b.value, elapsed, req.modulus, max_rate)
        results.append((a, b, elapsed, sol))
    return results


# ---------------------------------------------------------------------------
# 端点
# ---------------------------------------------------------------------------

@app.get("/health")
def health() -> dict:
    return {"status": "ok"}


@app.post("/v1/usage/verify", response_model=VerifyResponse)
def verify_usage(req: VerifyRequest) -> VerifyResponse:
    segments: List[SegmentOut] = []
    errors: List[SegmentErrorOut] = []

    for a, b, elapsed, sol in _solve_all(req):
        if sol.status == STATUS_OK:
            segments.append(
                SegmentOut(
                    index=sol.index,
                    start_timestamp=a.timestamp,
                    end_timestamp=b.timestamp,
                    start_reading=a.value,
                    end_reading=b.value,
                    elapsed_seconds=elapsed,
                    wraps=sol.wraps,
                    usage=sol.usage,
                )
            )
            continue

        diff = b.value - a.value
        if sol.status == STATUS_NO_SOLUTION:
            detail = _no_solution_detail(diff, req.modulus, sol, elapsed)
        else:  # STATUS_AMBIGUOUS
            detail = (
                f"多解：回绕次数 k 在 [{sol.min_wraps}, {sol.max_wraps}] 内均满足"
                f"非负增量与速率上限约束，无法唯一确定用量，不做猜测"
            )
        errors.append(
            SegmentErrorOut(
                index=sol.index,
                start_timestamp=a.timestamp,
                end_timestamp=b.timestamp,
                start_reading=a.value,
                end_reading=b.value,
                elapsed_seconds=elapsed,
                reason=sol.status,  # type: ignore[arg-type]
                detail=detail,
                min_wraps=sol.min_wraps,
                max_wraps=sol.max_wraps,
            )
        )

    has_error = bool(errors)
    return VerifyResponse(
        status="error" if has_error else "ok",
        modulus=req.modulus,
        segments=segments,
        errors=errors,
        total_usage=None if has_error else sum(s.usage for s in segments),
    )


@app.post("/v1/usage/reconcile", response_model=ReconcileResponse)
def reconcile_usage(req: ReconcileRequest) -> ReconcileResponse:
    """账单总量对账：复用逐段读数/模数/速率校验，再以账单总量收紧圈数范围。"""
    solved = _solve_all(req)

    # 1) 某段原本无解：直接构成冲突，不进行总量推断
    segment_conflicts: List[ReconcileSegmentErrorOut] = []
    feasible = []
    for a, b, elapsed, sol in solved:
        if sol.status == STATUS_NO_SOLUTION:
            segment_conflicts.append(
                ReconcileSegmentErrorOut(
                    kind="segment",
                    index=sol.index,
                    start_timestamp=a.timestamp,
                    end_timestamp=b.timestamp,
                    start_reading=a.value,
                    end_reading=b.value,
                    elapsed_seconds=elapsed,
                    reason=STATUS_NO_SOLUTION,
                    detail=_no_solution_detail(b.value - a.value, req.modulus, sol, elapsed),
                    min_wraps=sol.min_wraps,
                    max_wraps=sol.max_wraps,
                )
            )
        else:
            feasible.append((a, b, elapsed, sol))

    if segment_conflicts:
        return ReconcileResponse(
            status=STATUS_CONFLICT,
            modulus=req.modulus,
            total_usage=req.total_usage,
            total_wraps=None,
            segments=[],
            conflicts=segment_conflicts,
        )

    # 2) 全部段本身可行（唯一或多解），以整数区间运算做总量对账
    diffs = [b.value - a.value for a, b, _, _ in feasible]
    bounds = [(sol.min_wraps, sol.max_wraps) for *_, sol in feasible]
    result = reconcile_total(diffs, bounds, req.total_usage, req.modulus)

    if result.status == STATUS_CONFLICT:
        end_minus_start = sum(diffs)
        expected_remainder = end_minus_start % req.modulus
        total_conflicts = []
        for reason in result.conflicts:
            if reason == REASON_TOTAL_INCOMPATIBLE_MODULUS:
                detail = (
                    f"账单总量与模数不相容：总量 B 必须满足 "
                    f"B ≡ {expected_remainder} (mod {req.modulus})"
                    f"（首尾读数差为 {end_minus_start}），收到 "
                    f"{req.total_usage}（余数 {req.total_usage % req.modulus}），"
                    f"总回绕次数不是整数"
                )
            else:  # REASON_TOTAL_OUT_OF_RANGE
                detail = (
                    f"账单总量超出可行范围：各段回绕次数均为非负整数且满足"
                    f"速率上限时，总用量只能在 [{result.feasible_min_total}, "
                    f"{result.feasible_max_total}]（模数 {req.modulus}）之内，"
                    f"收到 {req.total_usage}"
                )
            total_conflicts.append(
                ReconcileTotalErrorOut(
                    kind="total",
                    reason=reason,  # type: ignore[arg-type]
                    detail=detail,
                    feasible_min_total=result.feasible_min_total,
                    feasible_max_total=result.feasible_max_total,
                    expected_remainder=(
                        expected_remainder
                        if reason == REASON_TOTAL_INCOMPATIBLE_MODULUS
                        else None
                    ),
                    total_wraps=result.total_wraps,
                )
            )
        return ReconcileResponse(
            status=STATUS_CONFLICT,
            modulus=req.modulus,
            total_usage=req.total_usage,
            total_wraps=result.total_wraps,
            segments=[],
            conflicts=total_conflicts,
        )

    # 3) 可行：圈数已唯一的段确定用量，其余仅给范围，不猜测
    segments: List[SegmentRangeOut] = []
    for (a, b, elapsed, _), ns in zip(feasible, result.narrowed):
        min_usage, max_usage = ns.usage_bounds(req.modulus)
        segments.append(
            SegmentRangeOut(
                index=ns.index,
                start_timestamp=a.timestamp,
                end_timestamp=b.timestamp,
                start_reading=a.value,
                end_reading=b.value,
                elapsed_seconds=elapsed,
                min_wraps=ns.min_wraps,
                max_wraps=ns.max_wraps,
                wraps=ns.wraps,
                usage=ns.usage(req.modulus),
                min_usage=None if ns.unique else min_usage,
                max_usage=None if ns.unique else max_usage,
            )
        )

    return ReconcileResponse(
        status=STATUS_OK,
        modulus=req.modulus,
        total_usage=req.total_usage,
        total_wraps=result.total_wraps,
        segments=segments,
        conflicts=[],
    )


@app.post("/v1/usage/daily-range", response_model=DailyRangeResponse)
def daily_usage_range(req: DailyRangeRequest) -> DailyRangeResponse:
    """按 UTC 自然日给出覆盖时段内每日最小/最大可行整数用量。

    复用读数、模数、最大每秒速率的请求校验；相邻读数跨 UTC 午夜时按
    午夜切成子段，子段容量为 ``floor(max_rate_per_second × 秒数)``，
    区间总增量 ``读数差 + k×模数`` 须能以非负整数分配到各子段。
    """
    max_rate = Fraction(req.max_rate_per_second)
    conflicts: List[DailyRangeConflictOut] = []
    # day -> [下界之和, 上界之和, 子段数]
    day_acc: "dict[date_cls, list[int]]" = {}

    for i, (a, b) in enumerate(zip(req.readings, req.readings[1:])):
        result = solve_daily_segment(
            i, a.value, b.value, a.timestamp, b.timestamp, req.modulus, max_rate
        )
        elapsed = int((b.timestamp - a.timestamp).total_seconds())

        if not result.feasible:
            diff = b.value - a.value
            min_delta = diff + result.min_wraps * req.modulus
            detail = (
                f"无可行分配：该区间按 UTC 午夜切成 {len(result.subsegments)} 段，"
                f"各子段容量 floor(速率×秒数) 之和为 {result.total_capacity}，"
                f"而满足非负增量的最小总增量已为 {min_delta}"
                f"（读数差 {diff} + {result.min_wraps}×{req.modulus}）；"
                f"回绕次数需同时满足 k>={result.min_wraps} 与 k<={result.max_wraps}，"
                f"区间为空，无法把整数增量分配到各日子段"
            )
            conflicts.append(
                DailyRangeConflictOut(
                    index=i,
                    start_timestamp=a.timestamp,
                    end_timestamp=b.timestamp,
                    start_reading=a.value,
                    end_reading=b.value,
                    elapsed_seconds=elapsed,
                    reason=REASON_NO_FEASIBLE_ALLOCATION,  # type: ignore[arg-type]
                    detail=detail,
                    min_wraps=result.min_wraps,
                    max_wraps=result.max_wraps,
                    total_capacity=result.total_capacity,
                )
            )
            continue

        for seg, low, high in zip(
            result.subsegments, result.min_usages, result.max_usages
        ):
            acc = day_acc.setdefault(seg.day, [0, 0, 0])
            acc[0] += low
            acc[1] += high
            acc[2] += 1

    if conflicts:
        return DailyRangeResponse(
            status=STATUS_CONFLICT,
            modulus=req.modulus,
            days=[],
            boundary_note=DAILY_BOUNDARY_NOTE,
            conflicts=conflicts,
        )

    days = [
        DailyUsageOut(
            date=day,
            min_usage=acc[0],
            max_usage=acc[1],
            segment_count=acc[2],
        )
        for day, acc in sorted(day_acc.items())
    ]
    return DailyRangeResponse(
        status=STATUS_OK,
        modulus=req.modulus,
        days=days,
        boundary_note=DAILY_BOUNDARY_NOTE,
        conflicts=[],
    )


# ---------------------------------------------------------------------------
# 换表账单复核
# ---------------------------------------------------------------------------


def _solve_meter_block(meter: str, block: MeterBlock):
    """对一块表的段执行现有读数/模数/速率判定，返回 (上下文, 判定结果)。"""
    a, b = block.start, block.end
    elapsed = int((b.timestamp - a.timestamp).total_seconds())
    max_rate = Fraction(block.max_rate_per_second)
    sol = solve_segment(
        0 if meter == "old" else 1,
        a.value,
        b.value,
        elapsed,
        block.modulus,
        max_rate,
    )
    return a, b, elapsed, sol


@app.post("/v1/usage/meter-change/reconcile", response_model=MeterChangeResponse)
def reconcile_meter_change_usage(req: MeterChangeRequest) -> MeterChangeResponse:
    """换表账单复核：旧表/新表两段（模数可不同）以账单总量精确联立。

    复用现有秒级时间戳、读数范围与速率上限校验；另要求旧表终读与新表
    初读同刻。两段先各自求满足非负增量与速率上限的回绕整数区间，再对
    线性丢番图方程 ``M_old*k1 + M_new*k2 = B - d1 - d2`` 求单参数整数
    通解并由两段区间夹出参数范围，不枚举圈数组合。
    """
    solved = {
        "old": _solve_meter_block("old", req.old_meter),
        "new": _solve_meter_block("new", req.new_meter),
    }

    # 1) 某段原本无解：以分段冲突返回，不进行总量联立
    blocks = {"old": req.old_meter, "new": req.new_meter}
    segment_conflicts: List[MeterSegmentErrorOut] = []
    feasible: dict = {}
    for meter in ("old", "new"):
        block = blocks[meter]
        a, b, elapsed, sol = solved[meter]
        if sol.status == STATUS_NO_SOLUTION:
            segment_conflicts.append(
                MeterSegmentErrorOut(
                    kind="segment",
                    meter=meter,  # type: ignore[arg-type]
                    start_timestamp=a.timestamp,
                    end_timestamp=b.timestamp,
                    start_reading=a.value,
                    end_reading=b.value,
                    elapsed_seconds=elapsed,
                    modulus=block.modulus,
                    reason=STATUS_NO_SOLUTION,
                    detail=_no_solution_detail(
                        b.value - a.value, block.modulus, sol, elapsed
                    ),
                    min_wraps=sol.min_wraps,
                    max_wraps=sol.max_wraps,
                )
            )
        else:
            feasible[meter] = (block, a, b, elapsed, sol)

    if segment_conflicts:
        return MeterChangeResponse(
            status=STATUS_CONFLICT,
            total_usage=req.total_usage,
            unique=False,
            segments=[],
            conflicts=segment_conflicts,
        )

    # 2) 两段本身可行（唯一或多解），模数可不同，精确联立总量
    old_block, old_start, old_end, _, old_sol = feasible["old"]
    new_block, new_start, new_end, _, new_sol = feasible["new"]
    result = reconcile_meter_total(
        old_diff=old_end.value - old_start.value,
        old_bounds=(old_sol.min_wraps, old_sol.max_wraps),
        old_modulus=old_block.modulus,
        new_diff=new_end.value - new_start.value,
        new_bounds=(new_sol.min_wraps, new_sol.max_wraps),
        new_modulus=new_block.modulus,
        total_usage=req.total_usage,
    )

    if result.status == STATUS_CONFLICT:
        conflicts: List[MeterTotalErrorOut] = []
        for reason in result.conflicts:
            if reason == REASON_TOTAL_INCOMPATIBLE_MODULUS:
                detail = (
                    f"账单总量与两表模数不相容：总量 B 必须满足 "
                    f"B ≡ d_old + d_new (mod gcd)，其中 gcd("
                    f"{old_block.modulus}, {new_block.modulus}) = {result.gcd}；"
                    f"收到 {req.total_usage}，除以 {result.gcd} 不整除，"
                    f"两段回绕次数无法同时取整数"
                )
                expected_remainder = (
                    old_end.value
                    - old_start.value
                    + new_end.value
                    - new_start.value
                ) % result.gcd
            else:  # REASON_TOTAL_OUT_OF_RANGE
                detail = (
                    f"账单总量联立后无可行整数解：两段回绕次数各自为非负整数"
                    f"且满足速率上限时，分段用量之和只能在 "
                    f"[{result.feasible_min_total}, {result.feasible_max_total}] "
                    f"内取值（旧表模数 {old_block.modulus}、新表模数 "
                    f"{new_block.modulus}，gcd {result.gcd}）；收到 "
                    f"{req.total_usage}。"
                )
                if (
                    result.feasible_min_total
                    <= req.total_usage
                    <= result.feasible_max_total
                ):
                    detail += (
                        "总量虽落在上述区间内，但不存在同时满足两表圈数区间的"
                        "整数组合（模 gcd 相容但联立通解落在可行范围之外）"
                    )
                expected_remainder = None
            conflicts.append(
                MeterTotalErrorOut(
                    kind="total",
                    reason=reason,  # type: ignore[arg-type]
                    detail=detail,
                    old_modulus=old_block.modulus,
                    new_modulus=new_block.modulus,
                    modulus_gcd=result.gcd,
                    feasible_min_total=result.feasible_min_total,
                    feasible_max_total=result.feasible_max_total,
                    expected_remainder=expected_remainder,
                )
            )
        return MeterChangeResponse(
            status=STATUS_CONFLICT,
            total_usage=req.total_usage,
            unique=False,
            segments=[],
            conflicts=conflicts,
        )

    # 3) 可行：圈数唯一者给出确定用量，其余仅给范围，不猜测
    segments: List[MeterSegmentRangeOut] = []
    for meter, ns, ctx in (
        ("old", result.old_segment, feasible["old"]),
        ("new", result.new_segment, feasible["new"]),
    ):
        block, a, b, elapsed, _ = ctx
        min_usage, max_usage = ns.usage_bounds()
        segments.append(
            MeterSegmentRangeOut(
                meter=meter,  # type: ignore[arg-type]
                start_timestamp=a.timestamp,
                end_timestamp=b.timestamp,
                start_reading=a.value,
                end_reading=b.value,
                elapsed_seconds=elapsed,
                modulus=block.modulus,
                min_wraps=ns.min_wraps,
                max_wraps=ns.max_wraps,
                wraps_step=ns.wraps_step,
                wraps=ns.wraps,
                usage=min_usage if ns.unique else None,
                min_usage=None if ns.unique else min_usage,
                max_usage=None if ns.unique else max_usage,
            )
        )

    both_unique = result.old_segment.unique and result.new_segment.unique
    return MeterChangeResponse(
        status=STATUS_OK,
        total_usage=req.total_usage,
        unique=both_unique,
        segments=segments,
        conflicts=[],
    )
