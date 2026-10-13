# AI Quant Workspace

本地量化工作区（MCP + 策略回测 + Polymarket/OKX 终端 + DSH AI量化模式插件）。

## 快速开始

```bash
export AIQUANT_HOME=/path/to/this/repo
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
./start-server.sh          # Linux/云端静态页
# 或 Windows: pwsh -File .\start-server.ps1
```

MCP 自检：

```bash
export AIQUANT_HOME=/path/to/this/repo
python mcp/quant_mcp/server.py --selftest
```

环境变量模板见 `.env.example`。上传云端前请阅读 **`UPLOAD_CHECKLIST.md`**（必改项 / 不要上传清单）。

## 主要目录

| 路径 | 说明 |
|------|------|
| `mcp/quant_mcp/` | 量化 MCP 服务器（stdio） |
| `strategies/` | OKX/BTC 策略与回测引擎 |
| `web/poly-terminal/` | Polymarket 前端 |
| `okx-terminal/` | OKX WS 中继与前端 |
| `aiquant-mode/` | DeepSeek Harness 插件 |
| `skills/` | Agent 技能说明 |