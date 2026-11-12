"""跨页套餐归并与内容冲突的回归测试（离线，file:// 抓取）。

样例站：quote-site/samples/duplicate-conflict/
- 列表第 1 页：P001 -> 主站 plans/p001.html，P002 -> 主站 plans/p002.html，
  P003 -> 主站 plans/p003.html
- 列表第 2 页：P001 -> 内容完全一致的镜像详情（应归并只计一次），
  P003 -> 同编号但月基本费与峰谷费率不同的另一版本（整组应不参与比价）
"""
from __future__ import annotations

import unittest
from pathlib import Path

from app.billing import annual_total, bill_plan, check_eligibility
from app.comparison import run_comparison
from app.grouping import group_plans
from app.scraper import crawl_quote_site

ROOT = Path(__file__).resolve().parent.parent
SAMPLE_LIST_URL = (ROOT / "quote-site" / "samples" / "duplicate-conflict" / "index.html").as_uri()
MAIN_LIST_URL = (ROOT / "quote-site" / "index.html").as_uri()

USAGE = [
    {"month": 1, "peak_kwh": 260, "valley_kwh": 360},
    {"month": 2, "peak_kwh": 240, "valley_kwh": 340},
    {"month": 3, "peak_kwh": 280, "valley_kwh": 380},
    {"month": 4, "peak_kwh": 300, "valley_kwh": 400},
    {"month": 5, "peak_kwh": 330, "valley_kwh": 430},
    {"month": 6, "peak_kwh": 380, "valley_kwh": 480},
    {"month": 7, "peak_kwh": 420, "valley_kwh": 520},
    {"month": 8, "peak_kwh": 410, "valley_kwh": 510},
    {"month": 9, "peak_kwh": 360, "valley_kwh": 460},
    {"month": 10, "peak_kwh": 310, "valley_kwh": 410},
    {"month": 11, "peak_kwh": 280, "valley_kwh": 380},
    {"month": 12, "peak_kwh": 270, "valley_kwh": 370},
]


class CrossPageGroupingTest(unittest.TestCase):
    def setUp(self) -> None:
        self.crawl = crawl_quote_site(SAMPLE_LIST_URL)
        self.grouping = group_plans(self.crawl.plans)
        self.response = run_comparison(SAMPLE_LIST_URL, "上海", USAGE)

    def test_both_list_pages_crawled_and_duplicate_details_fetched(self) -> None:
        self.assertEqual(len(self.crawl.visited_list_urls), 2)
        # 5 份详情：P001 两份（其中一份镜像）、P002、P003 两份（其中一份冲突版本）
        self.assertEqual(self.crawl.plans and len(self.crawl.plans), 5)
        self.assertEqual(self.crawl.failures, [])
        p001_details = {p.detail_url for p in self.crawl.plans if p.plan_id == "P001"}
        self.assertEqual(len(p001_details), 2)
        p003_details = {p.detail_url for p in self.crawl.plans if p.plan_id == "P003"}
        self.assertEqual(len(p003_details), 2)

    def test_identical_duplicate_grouped_once_with_all_sources(self) -> None:
        self.assertEqual(len(self.grouping.duplicates), 1)
        duplicate = self.grouping.duplicates[0]
        self.assertEqual(duplicate.plan_id, "P001")
        self.assertEqual(len(duplicate.plans), 2)
        self.assertEqual(len(duplicate.sources), 2)
        self.assertNotEqual(duplicate.sources[0].detail_url, duplicate.sources[1].detail_url)
        # 归并后 P001 只出现一次，且代表详情保留了全部来源。
        p001_ranked = [p for p in self.response["ranked_plans"] if p["plan_id"] == "P001"]
        self.assertEqual(len(p001_ranked), 1)
        self.assertEqual(len(p001_ranked[0]["sources"]), 2)
        self.assertEqual(self.response["crawl"]["merged_duplicate_count"], 1)

    def test_conflicting_identical_plan_id_excluded_with_sources(self) -> None:
        self.assertEqual(len(self.grouping.conflicts), 1)
        conflict = self.grouping.conflicts[0]
        self.assertEqual(conflict.plan_id, "P003")
        self.assertEqual(set(conflict.conflict_fields), {"base_fee_cny", "peak_tiers", "valley_tiers"})

        excluded = {item["plan_id"]: item for item in self.response["excluded_plans"]}
        self.assertIn("P003", excluded)
        self.assertEqual(excluded["P003"]["reason"], "conflicting_details")
        self.assertEqual(len(excluded["P003"]["sources"]), 2)
        self.assertEqual(
            {source["detail_url"] for source in excluded["P003"]["sources"]},
            {p.detail_url for p in conflict.plans},
        )
        # 冲突组整组不参与排序。
        self.assertNotIn("P003", {p["plan_id"] for p in self.response["ranked_plans"]})
        self.assertEqual(self.response["conflicting_plans"][0]["plan_id"], "P003")

    def test_other_valid_plans_still_ranked_with_annual_totals(self) -> None:
        ranked = self.response["ranked_plans"]
        self.assertEqual([p["plan_id"] for p in ranked], ["P001", "P002"])
        self.assertEqual([p["rank"] for p in ranked], [1, 2])
        self.assertEqual([p["annual_total_cny"] for p in ranked], [7247.40, 7842.00])
        self.assertEqual(self.response["crawl"]["eligible_count"], 2)

    def test_lowest_price_marked_pending_verification_on_conflict(self) -> None:
        conclusion = self.response["conclusion"]
        self.assertEqual(conclusion["verification_status"], "pending_verification")
        self.assertEqual(conclusion["lowest_plan_id"], "P001")
        self.assertEqual(conclusion["lowest_annual_total_cny"], 7247.40)
        self.assertEqual(conclusion["conflicting_plan_ids"], ["P003"])
        self.assertIn("冲突", conclusion["note"])

    def test_main_site_still_has_no_duplicates_or_conflicts(self) -> None:
        response = run_comparison(MAIN_LIST_URL, "上海", USAGE)
        self.assertEqual(response["merged_duplicate_plans"], [])
        self.assertEqual(response["conflicting_plans"], [])
        self.assertEqual(response["crawl"]["merged_duplicate_count"], 0)
        # 原有复核金额与待核实结论不受影响。
        self.assertEqual(
            [(p["plan_id"], p["annual_total_cny"]) for p in response["ranked_plans"]],
            [("P003", 6899.40), ("P001", 7247.40), ("P002", 7842.00)],
        )
        self.assertEqual(response["conclusion"]["verification_status"], "pending_verification")

    def test_conflicting_variants_would_differ_in_cost(self) -> None:
        """证明冲突版本确实会产生不同年度金额，因此不可择一计入比价。"""
        crawl = crawl_quote_site(SAMPLE_LIST_URL)
        p003 = [p for p in crawl.plans if p.plan_id == "P003"]
        totals = set()
        for plan in p003:
            self.assertTrue(check_eligibility(plan, "上海", USAGE).eligible)
            totals.add(annual_total(bill_plan(plan, USAGE)))
        self.assertEqual(len(totals), 2)


if __name__ == "__main__":
    unittest.main()
