"""同一套餐编号跨列表页的详情归并与冲突判定。

报价站的不同列表页可能把同一 `plan_id` 指向两份不同的详情页：
- 若各详情的名称、供应商、适用地区、月基本费、峰谷档位完全一致，视为同一套餐，
  只保留一份（首次发现的详情作为代表），其余来源记录在案但不再重复参与比价。
- 若任一上述字段不一致，整组不可信：不参与比价，并保留每一份详情的来源地址，
  由上层写入排除列表并把最低价结论标记为待核实。
"""
from __future__ import annotations

import math
from dataclasses import dataclass, field

from .scraper import PlanDetail, RateTier

CONFLICTING_DETAILS_REASON = "conflicting_details"

# 判定详情是否一致时比较的字段及其中文名称。
FIELD_ORDER = ("name", "supplier", "regions", "base_fee_cny", "peak_tiers", "valley_tiers")
FIELD_LABELS = {
    "name": "套餐名称",
    "supplier": "供应商",
    "regions": "适用地区",
    "base_fee_cny": "月基本费",
    "peak_tiers": "峰段档位",
    "valley_tiers": "谷段档位",
}


@dataclass(frozen=True)
class PlanSource:
    """一份套餐详情的来源：详情地址 + 发现它的列表页。"""

    detail_url: str
    list_url: str


@dataclass
class DuplicateGroup:
    """同编号且内容一致的多份详情；canonical 为代表（只计一次）。"""

    plan_id: str
    canonical: PlanDetail
    plans: list[PlanDetail]

    @property
    def sources(self) -> list[PlanSource]:
        return [_source_of(plan) for plan in self.plans]


@dataclass
class ConflictGroup:
    """同编号但内容冲突的多份详情；整组不参与比价。"""

    plan_id: str
    plans: list[PlanDetail]
    conflict_fields: list[str]

    @property
    def representative(self) -> PlanDetail:
        return self.plans[0]

    @property
    def sources(self) -> list[PlanSource]:
        return [_source_of(plan) for plan in self.plans]

    def detail_text(self) -> str:
        labels = "、".join(FIELD_LABELS.get(name, name) for name in self.conflict_fields)
        sources = "；".join(
            f"{source.detail_url}（列表页 {source.list_url}）" for source in self.sources
        )
        return (
            f"同一套餐编号 {self.plan_id or '(空)'} 的 {len(self.plans)} 份详情内容不一致"
            f"（{labels}），整组不参与比价；详情来源：{sources}"
        )


@dataclass
class PlanGrouping:
    plans: list[PlanDetail] = field(default_factory=list)
    duplicates: list[DuplicateGroup] = field(default_factory=list)
    conflicts: list[ConflictGroup] = field(default_factory=list)
    detail_count: int = 0

    def sources_for(self, plan: PlanDetail) -> list[PlanSource]:
        """代表详情对应的全部来源；普通套餐返回其唯一来源。"""
        for group in self.duplicates:
            if group.canonical is plan:
                return group.sources
        return [_source_of(plan)]


def _source_of(plan: PlanDetail) -> PlanSource:
    return PlanSource(detail_url=plan.detail_url, list_url=plan.list_url)


def _norm_text(value: str | None) -> str | None:
    if value is None:
        return None
    return " ".join(value.split()).casefold()


def _norm_regions(regions: list[str]) -> tuple[str, ...]:
    normalized = {
        " ".join(region.split()).casefold()
        for region in regions
        if region and region.strip()
    }
    return tuple(sorted(normalized))


def _norm_fee(value: float | None) -> float | str | None:
    # 详情页缺失月基本费时 parse 层用 NaN 表示；NaN 不能参与相等比较，
    # 统一映射为哨兵值，使“两份都缺失”被视为一致。
    if value is None:
        return None
    if isinstance(value, float) and math.isnan(value):
        return "missing"
    return round(float(value), 6)


def _normalized_tier(tier: RateTier) -> tuple[float, float | None, float]:
    # 取整到 1e-6，吸收文本解析浮点表示的末位差异，避免同值被误判冲突。
    return (
        round(float(tier.lower_kwh), 6),
        None if tier.upper_kwh is None else round(float(tier.upper_kwh), 6),
        round(float(tier.rate_cny_per_kwh), 6),
    )


def _norm_tiers(tiers: list[RateTier]) -> tuple[tuple[float, float | None, float], ...]:
    """按起始电量排序后比较，避免同一费率表在不同详情页行序不同被误判为冲突。"""
    return tuple(
        _normalized_tier(tier)
        for tier in sorted(
            tiers,
            key=lambda tier: (
                tier.lower_kwh,
                0 if tier.upper_kwh is None else 1,
                tier.upper_kwh if tier.upper_kwh is not None else 0.0,
                tier.rate_cny_per_kwh,
            ),
        )
    )


def plan_signature(plan: PlanDetail) -> dict[str, object]:
    """参与“同编号详情是否一致”判定的五个内容维度。"""
    return {
        "name": _norm_text(plan.name),
        "supplier": _norm_text(plan.supplier),
        "regions": _norm_regions(plan.regions),
        "base_fee_cny": _norm_fee(plan.base_fee_cny),
        "peak_tiers": _norm_tiers(plan.peak_tiers),
        "valley_tiers": _norm_tiers(plan.valley_tiers),
    }


def _conflict_fields(plans: list[PlanDetail]) -> list[str]:
    signatures = [plan_signature(plan) for plan in plans]
    fields: list[str] = []
    for name in FIELD_ORDER:
        first = signatures[0][name]
        if any(signature[name] != first for signature in signatures[1:]):
            fields.append(name)
    return fields


def _group_key(plan: PlanDetail) -> str:
    # 缺失编号的详情无法按编号归并，按详情地址各自成组，避免误合并。
    plan_id = (plan.plan_id or "").strip()
    return plan_id if plan_id else f"#detail:{plan.detail_url}"


def group_plans(plans: list[PlanDetail]) -> PlanGrouping:
    """按套餐编号归并详情，返回代表套餐、重复组与冲突组。"""
    grouped: dict[str, list[PlanDetail]] = {}
    for plan in plans:
        grouped.setdefault(_group_key(plan), []).append(plan)

    result = PlanGrouping(detail_count=len(plans))
    for key, members in grouped.items():
        if len(members) == 1:
            result.plans.append(members[0])
            continue
        fields = _conflict_fields(members)
        if fields:
            result.conflicts.append(
                ConflictGroup(plan_id=members[0].plan_id, plans=members, conflict_fields=fields)
            )
        else:
            result.duplicates.append(
                DuplicateGroup(plan_id=members[0].plan_id, canonical=members[0], plans=members)
            )
            result.plans.append(members[0])
    return result
