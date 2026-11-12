"""连锁便利店电价套餐比较 API。

入口：POST /api/v1/compare-plans
流程：抓取报价站分页列表与套餐详情 -> 按套餐编号跨页归并（重复只计一次，
内容冲突整组不参与比价并列出各详情来源）-> 地区过滤 -> 资格校验
（缺费率 / 超已公布档位不得参与）-> 峰谷逐档计费 + 月基本费 -> 年度汇总排序。
任何列表或详情抓取失败、或存在内容冲突的套餐详情，都会把最低价结论标记为“待核实”。
"""
from __future__ import annotations

from fastapi import FastAPI

from .comparison import run_comparison
from .schemas import CompareRequest, CompareResponse

app = FastAPI(
    title="连锁便利店电价套餐比较 API",
    version="1.1.0",
    description="按门店峰谷用电预测，比较本地报价站公开电价套餐的年度成本。",
)


@app.get("/health")
def health() -> dict:
    return {"status": "ok"}


@app.post("/api/v1/compare-plans", response_model=CompareResponse)
def compare_plans(request: CompareRequest) -> CompareResponse:
    payload = run_comparison(
        list_url=request.list_url,
        region=request.region,
        monthly_usage=[item.model_dump() for item in request.monthly_usage],
        timeout=request.timeout_seconds,
    )
    return CompareResponse.model_validate(payload)
