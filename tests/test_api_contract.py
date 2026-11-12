"""API 层契约测试：把 run_comparison 的输出交给响应模型校验。

本环境未安装 FastAPI 依赖时自动跳过；镜像内（requirements.txt 已装）会执行。
"""
from __future__ import annotations

import unittest

try:  # pragma: no cover - 仅在装好依赖的环境执行
    from app.main import compare_plans
    from app.schemas import CompareRequest, CompareResponse
except ModuleNotFoundError:  # pragma: no cover
    compare_plans = None
    CompareRequest = None
    CompareResponse = None

from tests.test_cross_page_grouping import SAMPLE_LIST_URL, USAGE


@unittest.skipIf(compare_plans is None, "未安装 FastAPI/Pydantic，跳过 API 契约测试")
class ApiContractTest(unittest.TestCase):
    def _request(self, list_url: str) -> "CompareRequest":
        return CompareRequest(list_url=list_url, region="上海", monthly_usage=USAGE)

    def test_sample_site_response_validates_against_schema(self) -> None:
        response = compare_plans(self._request(SAMPLE_LIST_URL))
        self.assertIsInstance(response, CompareResponse)
        self.assertEqual(response.conclusion["verification_status"], "pending_verification")
        self.assertEqual(
            [p.plan_id for p in response.ranked_plans], ["P001", "P002"]
        )
        self.assertEqual(response.conflicting_plans[0].plan_id, "P003")
        self.assertEqual(len(response.conflicting_plans[0].sources), 2)
        self.assertEqual(response.merged_duplicate_plans[0].plan_id, "P001")

    def test_main_site_response_unchanged_shape(self) -> None:
        from tests.test_cross_page_grouping import MAIN_LIST_URL

        response = compare_plans(self._request(MAIN_LIST_URL))
        self.assertEqual(response.conclusion["lowest_plan_id"], "P003")
        self.assertEqual(response.conclusion["lowest_annual_total_cny"], 6899.4)
        self.assertEqual(response.conflicting_plans, [])
        self.assertEqual(len(response.failures), 1)


if __name__ == "__main__":
    unittest.main()
