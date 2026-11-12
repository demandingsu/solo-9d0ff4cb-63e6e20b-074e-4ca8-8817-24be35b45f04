#!/usr/bin/env bash
# 离线演示脚本：启动本地静态报价站（仅需 Python 标准库）并调用比较 API。
# 前置：API 已在 8000 端口运行
#   uvicorn app.main:app --host 0.0.0.0 --port 8000
# 或直接使用 docker compose up --build（报价站在 quote-site:80）。
#
# 用法：
#   ./demo/run_demo.sh                                  # 主站列表（demo/request.json）
#   ./demo/run_demo.sh demo/request-duplicate-conflict.json
#                                                       # 重复 / 冲突样例站
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
PORT="${QUOTE_SITE_PORT:-8080}"
API_URL="${API_URL:-http://127.0.0.1:8000/api/v1/compare-plans}"
REQUEST="${1:-$ROOT/demo/request.json}"

python3 -m http.server "$PORT" --directory "$ROOT/quote-site" >/tmp/quote-site.log 2>&1 &
SERVER_PID=$!
trap 'kill "$SERVER_PID" 2>/dev/null || true' EXIT
sleep 1

PORT="$PORT" API_URL="$API_URL" python3 - "$ROOT" "$REQUEST" <<'PY'
import json, os, sys, urllib.parse, urllib.request

root, request_path = sys.argv[1], sys.argv[2]
with open(request_path, encoding="utf-8") as handle:
    payload = json.load(handle)

# 请求里的 list_url 只需给出报价站内的页面路径（如 /index.html 或
# /samples/duplicate-conflict/index.html）；脚本取其中的 path 并改写为本地服务地址。
site_path = urllib.parse.urlsplit(payload.get("list_url", "/index.html")).path or "/index.html"
if not site_path.startswith("/"):
    site_path = "/" + site_path
payload["list_url"] = f"http://127.0.0.1:{os.environ['PORT']}{site_path}"
print(f"# {root} -> {payload['list_url']}", file=sys.stderr)

request = urllib.request.Request(
    os.environ["API_URL"],
    data=json.dumps(payload).encode("utf-8"),
    headers={"Content-Type": "application/json"},
    method="POST",
)
with urllib.request.urlopen(request, timeout=15) as resp:
    print(json.dumps(json.loads(resp.read()), ensure_ascii=False, indent=2))
PY
