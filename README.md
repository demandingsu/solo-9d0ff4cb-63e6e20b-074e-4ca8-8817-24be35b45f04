# 连锁便利店电价套餐比较 API

能源采购员按门店未来 12 个月的峰谷用电预测，比较公开电价套餐的年度成本，辅助续签决策。

- **Web 框架**：Python 3.11 + FastAPI（`app/main.py`）
- **抓取与计费核心**：仅依赖 Python 标准库（`urllib` + `html.parser` + `decimal`），离线可测
- **本地报价站**：随仓库提供的纯静态站点（`quote-site/`），nginx 容器托管，无需外网
- **部署**：Docker Compose 一键启动 API + 报价站

## 目录结构

```
.
├── app/
│   ├── main.py        # FastAPI 路由（薄封装）
│   ├── comparison.py  # 抓取 -> 归并 -> 筛选 -> 计费 -> 排序（纯函数，可离线调用）
│   ├── grouping.py    # 跨页同编号归并与内容冲突判定
│   ├── schemas.py     # 请求/响应 Pydantic 模型
│   ├── scraper.py     # 分页列表 + 套餐详情抓取解析
│   ├── fetcher.py     # http/https/file 统一抓取层与错误归一化
│   ├── html_dom.py    # 标准库实现的轻量 HTML DOM
│   └── billing.py     # 峰谷逐档计费、资格校验、年度汇总
├── quote-site/        # 本地报价站（静态 HTML，虚构演示数据）
│   ├── index.html         # 主站列表第 1 页（P001-P004，含 next 分页链接）
│   ├── index-2.html       # 主站列表第 2 页（P005-P007）
│   ├── plans/p001..p006.html
│   ├── 404.html           # p007.html 故意缺失，演示详情抓取失败
│   ├── samples/duplicate-conflict/   # 重复 / 冲突样例站
│   │   ├── index.html         # 第 1 页：P001、P002、P003
│   │   ├── index-2.html       # 第 2 页：P001 镜像详情、P003 另一版本
│   │   └── plans/
│   │       ├── p001.html      # 与主站 P001 内容完全一致 -> 归并只计一次
│   │       └── p003-alt.html  # 与主站 P003 同编号但金额/费率不同 -> 整组排除
│   └── nginx.conf
├── tests/             # 标准库 unittest 离线测试
├── demo/request.json  # 示例请求（12 个月峰谷用电）
├── demo/request-duplicate-conflict.json  # 样例站请求
├── Dockerfile
└── docker-compose.yml
```

## 快速开始（Docker Compose，推荐）

```bash
docker compose up --build
```

- 报价站：http://localhost:8080/index.html
- API 文档（Swagger UI）：http://localhost:8000/docs
- 健康检查：http://localhost:8000/health

发起比较请求（容器内通过 `http://quote-site/index.html` 访问报价站）：

```bash
curl -X POST http://localhost:8000/api/v1/compare-plans \
  -H "Content-Type: application/json" \
  --data @demo/request.json

# 重复 / 冲突样例站
curl -X POST http://localhost:8000/api/v1/compare-plans \
  -H "Content-Type: application/json" \
  --data @demo/request-duplicate-conflict.json
```

## 无 Docker 的离线运行

核心抓取/计费逻辑仅用标准库，可直接运行测试：

```bash
python3 -I -m unittest discover -s tests -t . -v
```

也可以用标准库直接跑完整比较链路（无需安装 FastAPI）：

```bash
python3 -c "
from app.comparison import run_comparison
import json
usage = [{'month': i['month'], 'peak_kwh': i['peak_kwh'], 'valley_kwh': i['valley_kwh']}
         for i in json.load(open('demo/request-duplicate-conflict.json'))['monthly_usage']]
print(json.dumps(run_comparison(
    'file:///workspace/quote-site/samples/duplicate-conflict/index.html', '上海', usage),
    ensure_ascii=False, indent=2))
"
```

手动启动报价站与 API（需自行安装 `requirements.txt`）：

```bash
python3 -m http.server 8080 --directory quote-site
uvicorn app.main:app --host 0.0.0.0 --port 8000
# 请求中将 list_url 改为 http://127.0.0.1:8080/index.html
# 也支持 file:// 绝对路径，完全离线：
#   "list_url": "file:///workspace/quote-site/index.html"
```

## 请求格式

`POST /api/v1/compare-plans`

| 字段 | 说明 |
| --- | --- |
| `list_url` | 报价站套餐列表页 URL，支持 `http(s)://` 与 `file://` |
| `region` | 门店地区，需与套餐详情页地区标签精确匹配（如 `上海`） |
| `monthly_usage` | 恰好 12 条记录，每条含 `month`(1-12)、`peak_kwh`、`valley_kwh`（非负） |
| `timeout_seconds` | 可选，单次抓取超时 1-30 秒，默认 8 |

## 计费与筛选规则

0. **跨页套餐归并（先于筛选）**：按套餐编号 `plan_id` 把不同列表页发现的详情归组。
   - 同编号详情的**名称、供应商、适用地区、月基本费、峰段档位、谷段档位**全部一致
     → 视为同一套餐，只计一次（取首次发现的详情为代表），全部来源记录在
     `merged_duplicate_plans[].sources`，参与排序的套餐在 `sources` 中给出全部详情来源。
     比较时忽略文本首尾空白/大小写差异、地区标签顺序差异与费率表行序差异。
   - 同编号但上述任一字段不一致 → 内容冲突，**整组不参与比价**：
     在 `excluded_plans` 中以 `conflicting_details` 排除并列出每一份详情的
     `detail_url` + 所在 `list_url`，同时在 `conflicting_plans` 中给出冲突字段与全部来源；
     最低价结论强制标记为 `pending_verification`（待核实）。
1. **地区过滤**：套餐适用地区不包含请求地区 → 排除（`region_not_applicable`）。
2. **费率完整性**：缺峰段或谷段费率表、费率档位无法解析、月基本费缺失 → 排除
   （`missing_rates` / `invalid_tier_structure` / `missing_base_fee`）。
3. **档位覆盖校验**：任一月峰或谷用电量超过该费率表已公布的最高档位上限
   → 排除（`usage_exceeds_published_tiers`，响应中注明月份与段别）。无上限档（止为空）可承接任意电量。
4. **逐档计费**：每月峰、谷电量分别沿各自费率表按 `[下限, 上限)` 区间逐档计费，
   金额用 `Decimal` 保留两位小数（四舍五入），再加固定月基本费得月合计。
5. **年度汇总与排序**：12 个月月合计求和为年度成本，按从低到高排序返回。

## 抓取失败处理

- 列表页（含分页）或套餐详情抓取失败（连接错误、超时、HTTP 4xx/5xx、结构无法解析）时，
  在响应 `failures` 中列出失败地址、阶段（`list`/`detail`）、原因与 HTTP 状态码。
- 只要存在任何抓取失败，或存在任何内容冲突的同编号套餐详情，
  `conclusion.verification_status` 标记为 `pending_verification`（待核实）；
  全部成功、无内容冲突且存在有效套餐时为 `verified`。无有效套餐时同样为 `pending_verification`。
- `crawl` 汇总同时给出 `plans_crawled`（抓取到的详情份数）、`distinct_plan_ids`
  （归并后的编号数）、`merged_duplicate_count`（归并掉的重复份数）与
  `conflicting_group_count`（冲突组数），便于核对归并结果。
- 分页跟随 `a[rel=next]`，并设有 50 页安全上限与去重，避免异常站点造成无限抓取。

## 演示数据覆盖的场景

| 套餐 | 地区 | 演示中的处理结果 |
| --- | --- | --- |
| P001 安能 峰谷A | 上海/江苏/浙江/华东 | 有效，参与排序 |
| P002 安能 峰谷B | 上海/华东 | 有效，参与排序 |
| P003 沪电 阶梯标准版 | 上海 | 有效，示例数据下年度最低价 |
| P004 通商 华北套餐 | 北京/天津/华北 | 地区不匹配，排除 |
| P005 安能 A+ | 上海/江苏/华东 | 缺谷段费率，排除 |
| P006 沪电 封顶套餐 | 上海 | 第 1 月峰用电即超 150 kWh 封顶档，排除 |
| P007 安能 C | 上海/华东 | 详情页 404，进入 `failures`，最低价标为待核实 |

示例请求下的复核年度成本（元）：**P003 6899.40 < P001 7247.40 < P002 7842.00**。

### 重复 / 冲突样例站（`quote-site/samples/duplicate-conflict/`）

模拟“不同列表页把同一套餐编号指向两份不同详情”：

| 编号 | 第 1 页详情 | 第 2 页详情 | 归并结果 |
| --- | --- | --- | --- |
| P001 | `plans/p001.html` | `samples/duplicate-conflict/plans/p001.html`（内容完全一致） | 归并为一处，只计一次，年度 7247.40，`sources` 列出两份来源 |
| P002 | `plans/p002.html` | — | 正常参与排序，年度 7842.00 |
| P003 | `plans/p003.html`（月基本费 80、峰 1.00/1.15/1.28、谷 0.35/0.45/0.58） | `samples/duplicate-conflict/plans/p003-alt.html`（月基本费 70、峰 0.98/1.12/1.25、谷 0.34/0.44/0.56） | 内容冲突，**整组不参与比价**，`excluded_plans` 列出两份详情来源；最低价标为待核实 |

样例请求 `demo/request-duplicate-conflict.json` 的响应要点：P001 与 P002 仍返回
年度费用与排序（1、2），P003 被排除且 `conclusion.conflicting_plan_ids = ["P003"]`，
`verification_status = "pending_verification"`。

## 响应结构（摘要）

```json
{
  "conclusion": {
    "lowest_plan_id": "P003",
    "lowest_plan_name": "沪电零售 · 峰谷阶梯标准版",
    "lowest_annual_total_cny": 6899.4,
    "verification_status": "pending_verification",
    "note": "存在抓取失败的列表页或套餐详情，最低价结论待核实",
    "conflicting_plan_ids": []
  },
  "crawl": { "plans_crawled": 6, "distinct_plan_ids": 6,
             "merged_duplicate_count": 0, "conflicting_group_count": 0,
             "eligible_count": 3, "excluded_count": 3, "failure_count": 1 },
  "ranked_plans": [ { "rank": 1, "annual_total_cny": 6899.4,
                      "sources": [ { "detail_url": "...", "list_url": "..." } ],
                      "monthly_bills": [ { "peak_charges": [...], "valley_charges": [...] } ] } ],
  "excluded_plans": [ { "plan_id": "P006", "reason": "usage_exceeds_published_tiers",
                        "detail": "...", "sources": [ { "detail_url": "...", "list_url": "..." } ] } ],
  "merged_duplicate_plans": [ { "plan_id": "P001", "plan_name": "...", "sources": [ ... , ... ] } ],
  "conflicting_plans": [ { "plan_id": "P003", "conflict_fields": ["base_fee_cny"],
                           "sources": [ { "detail_url": "a.html", "list_url": "第1页" },
                                        { "detail_url": "b.html", "list_url": "第2页" } ] } ],
  "failures": [ { "url": ".../p007.html", "stage": "detail", "reason": "HTTP 404", "status": 404 } ]
}
```

每个参与排序的套餐均带 `detail_url`（代表详情来源）与 `list_url`（首次发现该套餐的列表页），
`sources` 列出该套餐编号去重后的全部详情来源；冲突组在 `excluded_plans` 与
`conflicting_plans` 中均逐份列出详情来源。月度账单逐档列出 charged_kwh 与 charge_cny，便于采购员复核。
