#!/usr/bin/env bash
# 启动 Agent 搜索引擎服务(REST + 网页端)。
#
# 用法:
#   scripts/serve.sh                 # 前台启动(Ctrl+C 退出)
#   scripts/serve.sh -d              # 后台启动(nohup,日志写 /tmp/se.log)
#   HOST=127.0.0.1 PORT=8080 scripts/serve.sh   # 自定义监听地址/端口
#   RERANK_ENABLED=true scripts/serve.sh        # 质量优先(开 cross-encoder 重排)
#
# 远端访问:EC2 无公网 IP,在你本地电脑开 SSH 隧道后用浏览器访问 localhost:
#   ssh -i <密钥.pem> -N -L ${PORT:-8000}:localhost:${PORT:-8000} ec2-user@<EC2地址>
#   浏览器打开 http://localhost:${PORT:-8000}/
# 🔐 鉴权:在 .env 配 API_AUTH_TOKEN 后,/search 与 /mcp 强制 Bearer/X-API-Key 校验
#    (网页端在「高级选项 → API Token」填同一 token)。留空则不鉴权;裸绑公网务必先配 token。
set -euo pipefail

# 项目根目录(本脚本在 scripts/ 下)
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT"

HOST="${HOST:-0.0.0.0}"
PORT="${PORT:-8000}"
LOG="${LOG:-/tmp/se.log}"
# 优先用 3.11 venv(进程内 MCP 需要 Python ≥3.10);回退旧 3.9 .venv(无 MCP)
if [[ -x "$ROOT/.venv311/bin/uvicorn" ]]; then
  UVICORN="$ROOT/.venv311/bin/uvicorn"
else
  UVICORN="$ROOT/.venv/bin/uvicorn"
fi

if [[ ! -x "$UVICORN" ]]; then
  echo "✗ 找不到 uvicorn ($UVICORN) —— 请确认虚拟环境已建好(.venv311 或 .venv)" >&2
  exit 1
fi

DAEMON=0
[[ "${1:-}" == "-d" || "${1:-}" == "--daemon" ]] && DAEMON=1

if [[ "$DAEMON" == "1" ]]; then
  nohup "$UVICORN" src.api:app --host "$HOST" --port "$PORT" > "$LOG" 2>&1 &
  PID=$!
  sleep 2
  if kill -0 "$PID" 2>/dev/null; then
    echo "✓ 服务已后台启动 (pid=$PID),监听 $HOST:$PORT,日志 $LOG"
    echo "  健康检查: curl -s localhost:$PORT/health"
    echo "  MCP 端点: http://localhost:$PORT/mcp (Streamable HTTP)"
    echo "  停止:     kill $PID"
  else
    echo "✗ 启动失败,见日志 $LOG" >&2
    tail -n 20 "$LOG" >&2 || true
    exit 1
  fi
else
  echo "→ 前台启动,监听 $HOST:$PORT (Ctrl+C 退出)"
  exec "$UVICORN" src.api:app --host "$HOST" --port "$PORT"
fi
