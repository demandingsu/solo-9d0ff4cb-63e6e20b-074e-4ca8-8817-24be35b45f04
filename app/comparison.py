"""比较编排（纯函数，不依赖 FastAPI / Pydantic）。

这样抓取 + 归并 + 计费的完整链路可用标准库离线运行与回归测试；
`app.main` 仅负责把本模块的结果交给 FastAPI 的响应模型。
"""
from __future__ import annotations

from .billing import annual_total, bill_plan, check_eligibility
from .grouping import (
    CONFLICTING_DETAILS_REASON,
    PlanGrouping,
    PlanSource,
    group_plans,
)
from .scraper import PlanDetail, crawl_quote_site


def _tier_dicts(charges) -> list[dict]:
    return [vars(charge) for charge in charges]


def _monthly_dicts(bills) -> list[dict]:
    return [
        {
            "month": bill.month,
            "peak_kwh": bill.peak_kwh,
            "valley_kwh": bill.valley_kwh,
            "peak_charges": _tier_dicts(bill.peak_charges),
            "valley_charges": _tier_dicts(bill.valley_charges),
            "peak_energy_fee_cny": bill.peak_energy_fee_cny,
            "valley_energy_fee_cny": bill.valley_energy_fee_cny,
            "base_fee_cny": bill.base_fee_cny,
            "monthly_total_cny": bill.monthly_total_cny,
        }
        for bill in bills
    ]


def _sources_dicts(sources: list[PlanSource]) -> list[dict]:
    return [{"detail_url": source.detail_url, "list_url": source.list_url} for source in sources]


def _ranked_out(plan: PlanDetail, grouping: PlanGrouping, usage: list[dict]) -> dict:
    bills = bill_plan(plan, usage)
    return {
        "rank": 0,  # 排序后统一回填
        "plan_id": plan.plan_id,
        "plan_name": plan.name,
        "supplier": plan.supplier,
        "regions": plan.regions,
        "annual_total_cny": annual_total(bills),
        "detail_url": plan.detail_url,
        "list_url": plan.list_url,
        "sources": _sources_dicts(grouping.sources_for(plan)),
        "monthly_bills": _monthly_dicts(bills),
    }


def _excluded_out(plan: PlanDetail, grouping: PlanGrouping, reason: str, detail: str) -> dict:
    return {
        "plan_id": plan.plan_id,
        "plan_name": plan.name,
        "supplier": plan.supplier,
        "regions": plan.regions,
        "reason": reason,
        "detail": detail,
        "detail_url": plan.detail_url,
        "list_url": plan.list_url,
        "sources": _sources_dicts(grouping.sources_for(plan)),
    }


def build_conclusion(ranked: list[dict], has_failure: bool, conflict_ids: list[str]) -> dict:
    has_conflict = bool(conflict_ids)
    pending = has_failure or has_conflict or not ranked
    if not ranked:
        note = "没有可参与比较的有效套餐，无法给出最低价结论，待核实"
        if has_conflict:
            note = "存在内容冲突的套餐详情且没有可参与比较的有效套餐，无法给出最低价结论，待核实"
        return {
            "lowest_plan_id": None,
            "lowest_plan_name": None,
            "lowest_annual_total_cny": None,
            "verification_status": "pending_verification",
            "note": note,
            "conflicting_plan_ids": conflict_ids,
        }

    cheapest = ranked[0]
    if has_conflict and has_failure:
        note = "存在同一套餐编号的内容冲突详情与抓取失败，冲突组未参与比价，最低价结论待核实"
    elif has_conflict:
        note = "存在同一套餐编号的内容冲突详情，整组未参与比价，最低价结论待核实"
    elif has_failure:
        note = "存在抓取失败的列表页或套餐详情，最低价结论待核实"
    else:
        note = "全部列表页与套餐详情抓取成功，最低价结论已核实"
    return {
        "lowest_plan_id": cheapest["plan_id"],
        "lowest_plan_name": cheapest["plan_name"],
        "lowest_annual_total_cny": cheapest["annual_total_cny"],
        "verification_status": "pending_verification" if pending else "verified",
        "note": note,
        "conflicting_plan_ids": conflict_ids,
    }


def run_comparison(
    list_url: str,
    region: str,
    monthly_usage: list[dict],
    timeout: float = 8.0,
) -> dict:
    """抓取报价站并完成归并、筛选、计费与排序，返回可直接序列化的响应字典。"""
    crawl_result = crawl_quote_site(list_url, timeout=timeout)
    grouping = group_plans(crawl_result.plans)

    ranked: list[dict] = []
    excluded: list[dict] = []

    for plan in grouping.plans:
        eligibility = check_eligibility(plan, region, monthly_usage)
        if not eligibility.eligible:
            excluded.append(
                _excluded_out(
                    plan,
                    grouping,
                    eligibility.reason or "unknown",
                    eligibility.detail or "",
                )
            )
            continue
        ranked.append(_ranked_out(plan, grouping, monthly_usage))

    # 内容冲突的同编号详情整组不参与比价，并逐份列出详情来源。
    for conflict in grouping.conflicts:
        representative = conflict.representative
        excluded.append(
            {
                "plan_id": representative.plan_id,
                "plan_name": representative.name,
                "supplier": representative.supplier,
                "regions": representative.regions,
                "reason": CONFLICTING_DETAILS_REASON,
                "detail": conflict.detail_text(),
                "detail_url": representative.detail_url,
                "list_url": representative.list_url,
                "sources": _sources_dicts(conflict.sources),
            }
        )

    # 金额相同时按套餐编号排序，保证响应稳定可复核。
    ranked.sort(key=lambda item: (item["annual_total_cny"], item["plan_id"]))
    for index, item in enumerate(ranked, start=1):
        item["rank"] = index

    failures = [
        {"url": f.url, "stage": f.stage, "reason": f.reason, "status": f.status}
        for f in crawl_result.failures
    ]
    conflict_ids = [conflict.plan_id for conflict in grouping.conflicts]

    merged_duplicate_plans = [
        {
            "plan_id": duplicate.plan_id,
            "plan_name": duplicate.canonical.name,
            "sources": _sources_dicts(duplicate.sources),
        }
        for duplicate in grouping.duplicates
    ]
    conflicting_plans = [
        {
            "plan_id": conflict.plan_id,
            "plan_name": conflict.representative.name,
            "conflict_fields": conflict.conflict_fields,
            "sources": _sources_dicts(conflict.sources),
        }
        for conflict in grouping.conflicts
    ]

    return {
        "request_region": region,
        "list_url": list_url,
        "crawl": {
            "visited_list_urls": crawl_result.visited_list_urls,
            "plans_crawled": len(crawl_result.plans),
            "distinct_plan_ids": len(grouping.plans) + len(grouping.conflicts),
            "merged_duplicate_count": sum(len(g.plans) - 1 for g in grouping.duplicates),
            "conflicting_group_count": len(grouping.conflicts),
            "eligible_count": len(ranked),
            "excluded_count": len(excluded),
            "failure_count": len(failures),
        },
        "conclusion": build_conclusion(ranked, bool(failures), conflict_ids),
        "ranked_plans": ranked,
        "excluded_plans": excluded,
        "merged_duplicate_plans": merged_duplicate_plans,
        "conflicting_plans": conflicting_plans,
        "failures": failures,
    }
