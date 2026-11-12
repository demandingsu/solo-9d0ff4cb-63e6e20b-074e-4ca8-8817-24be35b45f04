"""套餐跨页归并的单元测试（不依赖报价站页面）。

覆盖：完全一致归并、逐字段冲突、地区集合顺序/空白差异不算冲突、
档位行序差异不算冲突、缺失月基本费的一致性判定、缺失编号不被误合并。
"""
from __future__ import annotations

import math
import unittest

from app.grouping import group_plans, plan_signature
from app.scraper import PlanDetail, RateTier


def tier(index: int, lower: float, upper: float | None, rate: float) -> RateTier:
    return RateTier(index=index, lower_kwh=lower, upper_kwh=upper, rate_cny_per_kwh=rate)


def make_plan(**overrides) -> PlanDetail:
    defaults = dict(
        plan_id="P100",
        name="测试峰谷套餐",
        supplier="测试电力",
        regions=["上海", "华东"],
        base_fee_cny=50.0,
        peak_tiers=[tier(1, 0, 200, 1.10), tier(2, 200, None, 1.25)],
        valley_tiers=[tier(1, 0, 300, 0.40), tier(2, 300, None, 0.52)],
        detail_url="file:///tmp/a.html",
        list_url="file:///tmp/list-1.html",
    )
    defaults.update(overrides)
    return PlanDetail(**defaults)


class GroupingUnitTest(unittest.TestCase):
    def test_single_plan_is_passed_through(self) -> None:
        grouping = group_plans([make_plan()])
        self.assertEqual(len(grouping.plans), 1)
        self.assertEqual(grouping.duplicates, [])
        self.assertEqual(grouping.conflicts, [])

    def test_identical_details_merged_and_first_kept(self) -> None:
        first = make_plan(detail_url="file:///tmp/a.html", list_url="file:///tmp/list-1.html")
        second = make_plan(detail_url="file:///tmp/b.html", list_url="file:///tmp/list-2.html")
        grouping = group_plans([first, second])
        self.assertEqual(len(grouping.plans), 1)
        self.assertIs(grouping.plans[0], first)
        self.assertEqual(len(grouping.duplicates), 1)
        self.assertEqual(
            [(s.detail_url, s.list_url) for s in grouping.duplicates[0].sources],
            [("file:///tmp/a.html", "file:///tmp/list-1.html"),
             ("file:///tmp/b.html", "file:///tmp/list-2.html")],
        )

    def test_case_and_whitespace_difference_still_same_plan(self) -> None:
        first = make_plan()
        second = make_plan(
            name="  测试峰谷套餐 ",
            supplier="测试电力 ",
            regions=["华东", "上海", "上海 "],
        )
        grouping = group_plans([first, second])
        self.assertEqual(len(grouping.duplicates), 1)
        self.assertEqual(grouping.conflicts, [])

    def test_tier_row_order_difference_still_same_plan(self) -> None:
        first = make_plan()
        second = make_plan(
            peak_tiers=list(reversed(first.peak_tiers)),
            valley_tiers=list(reversed(first.valley_tiers)),
        )
        grouping = group_plans([first, second])
        self.assertEqual(len(grouping.duplicates), 1)

    def test_both_missing_base_fee_counted_as_consistent(self) -> None:
        first = make_plan(base_fee_cny=float("nan"))
        second = make_plan(base_fee_cny=float("nan"))
        self.assertTrue(math.isnan(first.base_fee_cny))
        grouping = group_plans([first, second])
        self.assertEqual(len(grouping.duplicates), 1)

    def test_each_field_conflict_is_detected(self) -> None:
        cases = {
            "name": dict(name="另一个名称"),
            "supplier": dict(supplier="另一家供应商"),
            "regions": dict(regions=["上海", "江苏"]),
            "base_fee_cny": dict(base_fee_cny=55.0),
            "peak_tiers": dict(peak_tiers=[tier(1, 0, 200, 1.12), tier(2, 200, None, 1.25)]),
            "valley_tiers": dict(valley_tiers=[tier(1, 0, 300, 0.42), tier(2, 300, None, 0.52)]),
        }
        for field, override in cases.items():
            with self.subTest(field=field):
                grouping = group_plans([make_plan(), make_plan(**override)])
                self.assertEqual(grouping.plans, [])
                self.assertEqual(len(grouping.conflicts), 1)
                self.assertEqual(grouping.conflicts[0].conflict_fields, [field])

    def test_conflict_detail_lists_every_source(self) -> None:
        first = make_plan(base_fee_cny=50.0, detail_url="file:///tmp/a.html")
        second = make_plan(base_fee_cny=60.0, detail_url="file:///tmp/b.html")
        grouping = group_plans([first, second])
        detail = grouping.conflicts[0].detail_text()
        self.assertIn("file:///tmp/a.html", detail)
        self.assertIn("file:///tmp/b.html", detail)
        self.assertIn("月基本费", detail)

    def test_multiple_plan_ids_are_separated(self) -> None:
        grouping = group_plans([
            make_plan(plan_id="P100"),
            make_plan(plan_id="P200", name="另一个套餐", base_fee_cny=70.0),
        ])
        self.assertEqual({p.plan_id for p in grouping.plans}, {"P100", "P200"})
        self.assertEqual(grouping.conflicts, [])

    def test_missing_plan_id_not_merged_by_detail_url(self) -> None:
        grouping = group_plans([
            make_plan(plan_id="", detail_url="file:///tmp/a.html"),
            make_plan(plan_id="", detail_url="file:///tmp/b.html"),
        ])
        self.assertEqual(len(grouping.plans), 2)
        self.assertEqual(grouping.duplicates, [])
        self.assertEqual(grouping.conflicts, [])

    def test_signature_dimensions(self) -> None:
        signature = plan_signature(make_plan())
        self.assertEqual(
            set(signature),
            {"name", "supplier", "regions", "base_fee_cny", "peak_tiers", "valley_tiers"},
        )
        self.assertEqual(signature["regions"], ("上海", "华东"))


if __name__ == "__main__":
    unittest.main()
