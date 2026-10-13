# 量化项目 · 云端上传检查清单

生成时间：2026-10-13（Asia/Shanghai）  
仓库根：`D:\量化`（上传后请设 `AIQUANT_HOME` 指向云端解压目录）  
审计结论：**可以上传代码骨架，但不能整包无损上传。** 缺根 README / 依赖锁 / `.git`，大量 Windows 硬编码路径，且含浏览器 Cookie、钱包持仓快照、日志与行情缓存。

---

## 一、必改项（上传前必须处理）

| # | 问题 | 证据 / 路径 | 怎么做 |
|---|------|-------------|--------|
| 1 | **无 `.git`、原先无 `.gitignore`** | 根目录无 `.git` | 已补 `.gitignore`。上传前用 git init，或按下方「可上传清单」打包，勿整盘 zip。 |
| 2 | **Chrome Cookie / 浏览器配置绝不上传** | `okx-terminal\web\_selftest\_chromeprofile\`（约 20.4 MB，含 `Default\Network\Cookies`） | 删除或排除该目录后再上传。 |
| 3 | **Polymarket 钱包持仓快照** | `data\poly\cache\wallet_positions.json`、`wallet_fees.parquet`、`data\poly\raw\positions_0x*.json` | 地址为链上公开地址，但仍属研究目标数据；云端可重新拉取，默认不要上传。 |
| 4 | **硬编码本机路径仍大量存在** | 全库大量 `D:\量化`、`C:\Users\33096\...`（skills / README / probe / vbs / 测试） | 已修入口：`mcp/quant_mcp/server.py`、`start-server.ps1`、`start-server.sh`、`data/poly/fetch_poly.py`、`strategies/run_all.py`、`strategies/_data/fetch_okx.py`、`strategies/_engine/selfcheck.py`。其余文档与探测脚本仍写死路径，云端靠 `AIQUANT_HOME`，勿依赖文档里的绝对路径复制粘贴。 |
| 5 | **缺根级依赖声明（原先）** | 无 `requirements.txt` / `pyproject` | 已补 `requirements.txt`、`requirements-min.txt`。云端建议 `pip install -r requirements.txt`。 |
| 6 | **缺环境变量模板（原先）** | 无 `.env` / `.env.example` | 已补 `.env.example`。复制为 `.env` 后只在本机/云端私密配置里填，**不要提交 `.env`**。 |
| 7 | **Windows 专用启动器** | `*.ps1` / `*.vbs` / `*.cmd` | 云端用已补的 `start-server.sh` + `python -m`；DSH/aiquant-mode 仍依赖本机 DeepSeek Harness，云端未必能跑插件模式。 |
| 8 | **代理强依赖** | 多处默认 `127.0.0.1:7890`（Clash） | 云端若在海外机房通常不需要；在国内 VPS 需自备代理并设 `QUANT_MCP_PROXY` / `POLY_PROXY` / `OKX_PROXY`。 |

---

## 二、建议项（强烈建议，不阻塞最小上传）

1. **补根 `README.md`（云端怎么跑）** — 可参考本文「五、云端怎么跑」。
2. **继续把硬编码路径改成相对路径 / `AIQUANT_HOME`**：优先  
   - `terminal/poly_terminal/run.ps1` / `run.cmd`  
   - `okx-terminal/relay/run-relay.ps1` / `start-relay.vbs` / `relay-supervisor.ps1`  
   - `aiquant-mode/lib/index.js` 的 `DEFAULT_WORKSPACE`（已支持 env，默认仍是 `D:/量化`）  
   - `data/poly/probe2.py`～`probe4.py`、`strategies/_data/cvd_probe*.py`  
   - `skills/*/SKILL.md` 文档路径
3. **不要上传大体量可再生数据**（约 50+ MB）：`data/cache`、`data/poly/cache|raw`、`okx-terminal/data`、`logs`、`strategies/_data/*.parquet`。
4. **`aiquant-mode` 无 `node_modules`、无 npm 依赖声明** — 依赖宿主 DSH（`engines.dsh >= 0.1.7`）。云端若只跑 Python MCP/策略，可不装 DSH；要跑「AI 量化模式」面板需先装 DeepSeek Harness。
5. **CTP / 实盘凭据**：`skills/quant-live` 要求只从环境变量读。确认 `strategies/` 下无明文密码（本次扫描未发现 `sk-` / PEM / 明文 apiKey）。
6. **初始化 git + 首次提交前再扫一遍 secrets**。
7. **锁版本**：在干净 venv 里 `pip freeze > requirements.lock.txt` 后再上云，避免 akshare/ccxt 漂版本。

---

## 三、可上传清单（建议纳入版本库 / 云端包）

```
.gitignore
.env.example
requirements.txt
requirements-min.txt
UPLOAD_CHECKLIST.md
start-server.ps1
start-server.sh
README.md                 # 若已写

aiquant-mode/             # 全部源码（lib/presets/scripts/test/tools/*.yml/*.md/package.json）
mcp/quant_mcp/server.py
skills/                   # 各 SKILL.md
strategies/*.py
strategies/REPORT.md
strategies/_engine/backtest.py
strategies/_engine/indicators.py
strategies/_engine/selfcheck.py
strategies/_data/*.py     # 仅脚本，不含 parquet
terminal/poly_terminal/   # app.py feeds.py README run.* _probe_*.py
web/poly-terminal/        # index.html app.js feeds.js styles.css README.md
                          # 排除 _evidence/
okx-terminal/relay/relay.py
okx-terminal/relay/README.md
okx-terminal/relay/*.ps1 *.vbs   # 可选；云端更常用 python relay.py
okx-terminal/relay/tools/        # 探测/单测脚本
okx-terminal/web/                # app.js index.html styles.css README.md
                                 # 排除 _selftest/ _evidence/
data/poly/CONTRACT.md
data/poly/fetch_poly.py
data/poly/probe*.py
data/poly/edges.json             # 可选：前端热插 feed；或上云后重拉
scripts/mcp_smoke.ps1            # 可选（Windows）
notebooks/                       # 空目录可保留
```

约估纯代码包：**数 MB 级**（远小于当前整树 ~59 MB）。

---

## 四、不要上传清单

| 路径 | 原因 |
|------|------|
| `okx-terminal/web/_selftest/_chromeprofile/` | **Cookies / Trust Tokens / 浏览器画像** |
| `.env`（若已创建） | 真实密钥 |
| `logs/`、`data/logs/`、`okx-terminal/relay/logs/` | 本机调试日志 |
| `data/cache/` | A 股 parquet 缓存，可重拉 |
| `data/poly/cache/`、`data/poly/raw/` | 含钱包持仓、探针原始响应 |
| `data/poly/cache/wallet_positions.json` 等 | 跟踪钱包快照 |
| `okx-terminal/data/*.jsonl` | OKX tick/trade 落盘（约 25 MB） |
| `strategies/_data/*.parquet`、`*.meta.json` | K 线缓存 |
| `strategies/_engine/_debug/` | 本机调试脚本 |
| `__pycache__/`、`*.pyc` | 字节码 |
| `.playwright-mcp/`、`.tmp/`、`reports/*.png` | 截图与临时文件 |
| `web/poly-terminal/_evidence/`、`okx-terminal/web/_evidence/` | 本地验收截图 |
| `scripts/verify/selftest_verbose.txt` | 含本机绝对路径的自检输出 |
| Clash / Mihomo / 系统代理配置（不在本仓库，但勿打包进旁路目录） | 代理配置可能含订阅 URL |

本次扫描：**未发现** 明文 OKX/交易所 API Key、PEM 私钥、`sk-` 云厂商密钥写在源码里。代理仅本机 `127.0.0.1:7890` 地址。

---

## 五、云端怎么跑

### 5.1 最小：静态 POLY 终端 + 策略回测

```bash
# 1. 解压到例如 /workspace/aiquant
export AIQUANT_HOME=/workspace/aiquant
cd "$AIQUANT_HOME"

# 2. Python 3.11+ 建议
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt          # 或 requirements-min.txt

# 3. 静态页（edges.json 可选；无代理则 Polymarket 面板 OFFLINE）
chmod +x start-server.sh
./start-server.sh
# 浏览器打开 http://127.0.0.1:8899/web/poly-terminal/index.html

# 4. 回测（需先有 parquet，或先跑 fetch）
export OKX_PROXY=          # 海外可不设
python strategies/_data/fetch_okx.py     # 按脚本参数拉 BTC 等
python strategies/_engine/selfcheck.py
python strategies/run_all.py
```

### 5.2 MCP 服务器（给 Cursor / DSH 用）

```bash
export AIQUANT_HOME=/workspace/aiquant
export AIQUANT_PYTHON=$(which python)
# 如需代理：
# export QUANT_MCP_PROXY=http://127.0.0.1:7890
python mcp/quant_mcp/server.py --selftest
# stdio MCP：在客户端配置 command=$AIQUANT_PYTHON args=[.../server.py] env.AIQUANT_HOME=...
```

### 5.3 OKX WebSocket 中继

```bash
pip install aiohttp orjson
export AIQUANT_HOME=/workspace/aiquant
# export OKX_PROXY=...   # 国内需要
python okx-terminal/relay/relay.py --help
```

### 5.4 AI 量化模式（aiquant-mode）

依赖本机 **DeepSeek Harness (dsh) ≥ 0.1.7**，不是纯云函数项目。云端若无 DSH，请按 `aiquant-mode/README.md` 用 `AIQUANT_HOME` / `AIQUANT_PYTHON` 覆盖；不要照抄 `C:\Users\33096\...`。

### 5.5 推荐环境变量

见 `.env.example`：`AIQUANT_HOME`、`AIQUANT_PYTHON`、`QUANT_MCP_PROXY`、`POLY_PROXY`、`OKX_PROXY`、CTP 相关（仅私密存储）。

---

## 六、结构速览（审计时实测）

| 目录 | 约大小 | 用途 |
|------|--------|------|
| `aiquant-mode/` | 0.3 MB | DSH 插件：看板 / SQLite 事实库 / 补丁 |
| `mcp/quant_mcp/` | 0.1 MB | 自研 stdio MCP（pandas/akshare 等懒加载） |
| `skills/` | <0.1 MB | 数据/因子/策略/回测/风控/实盘/报告 skill |
| `strategies/` | 2.2 MB | OKX BTC 策略 + 引擎（含 parquet） |
| `web/poly-terminal/` | 0.5 MB | Polymarket 前端 |
| `terminal/poly_terminal/` | 0.2 MB | 同主题 Rich TUI |
| `okx-terminal/` | 25.8 MB | 中继 + Web + **行情 jsonl + chromeprofile** |
| `data/` | 29.3 MB | A 股缓存 + poly 契约/缓存 |
| `logs/` | 0.3 MB | 运行日志 |
| 合计 | **~59.4 MB / 713 文件** | |

**依赖（本机 conda `aiquant` 已验证）**：Python 3.11；pandas、numpy、pyarrow、requests、akshare、baostock、efinance、ccxt、yfinance、duckdb、rich、aiohttp、websockets、vnpy。  
**MCP 顶层**仅标准库；pandas/akshare/numpy 在工具函数内 import。  
**relay** 需要 `aiohttp`（可选 `orjson`）。  
**poly TUI** 需要 `requests`、`rich`。

---

## 七、本次已落地的安全修复（未删用户数据、未 push）

| 文件 | 动作 |
|------|------|
| `.gitignore` | 新建：排除密钥、缓存、日志、chromeprofile、parquet、jsonl 等 |
| `.env.example` | 新建：云端/本机环境变量模板 |
| `requirements.txt` / `requirements-min.txt` | 新建 |
| `start-server.ps1` | 改为基于 `$PSScriptRoot` / `AIQUANT_HOME` / `AIQUANT_PYTHON` |
| `start-server.sh` | 新建 Linux/云端启动 |
| `mcp/quant_mcp/server.py` | `ROOT` 改为 `AIQUANT_HOME` 或相对仓库根 |
| `data/poly/fetch_poly.py` | `BASE`/`LOGDIR` 环境变量化 |
| `strategies/run_all.py` | `LOGDIR` 环境变量化 |
| `strategies/_data/fetch_okx.py` | `OUTDIR`/`LOGDIR` 环境变量化 |
| `strategies/_engine/selfcheck.py` | `DATA`/`OUT`/`LOGDIR` 环境变量化 |
| `UPLOAD_CHECKLIST.md` | 本文件 |

**未做**：`git init` / remote push、删除 chromeprofile 或任何数据文件、改写全部文档中的 `D:\量化` 字样、上传到任何云。

---

## 八、建议上传步骤（实操）

1. 确认 `.gitignore` 生效；不要手动把 `cache`/`chromeprofile` 加进压缩包。  
2. （可选）`git init` → 只 add「可上传清单」→ 本地 commit。  
3. 打包示例（PowerShell，在仓库根）：

```powershell
# 示例：打一个不含缓存/cookie 的 zip（按需调整）
$dest = "$env:USERPROFILE\Desktop\aiquant-cloud.zip"
# 推荐用 git archive；若无 git，用资源管理器手动勾选「可上传清单」中的目录
```

4. 云端解压 → `export AIQUANT_HOME=...` → `pip install -r requirements.txt` → `./start-server.sh` 或 `python mcp/quant_mcp/server.py --selftest`。  
5. 需要 POLY/OKX 行情时再配置代理与重拉数据。

---

*本清单基于本机真实文件枚举与内容扫描，未臆造密钥命中。*