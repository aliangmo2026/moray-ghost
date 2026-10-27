# Ghost MCP 接入指南（Cursor / Claude Desktop / 任意 MCP 客户端）

> 版本：v0.14.0+ · 服务端安全门禁在 MCP 模式下 100% 生效（令牌 / 能力白名单 /
> UAC·窗口·键盘黑名单 / 频率熔断 / 硬超时 / JSONL 审计，无一条旁路）。

## 0. 一分钟看懂

```
你的 AI 客户端（Cursor / Claude Desktop / 自研 Agent）
   │  MCP over Stdio（JSON-RPC，本地管道，不走网络端口）
   ▼
python E:\moray-ghost\mcp_server.py     ← 客户端按下面的配置拉起这个进程
   │  HTTP 127.0.0.1:8030 + X-Ghost-Token（身份来自启动环境，tool 参数改不宽）
   ▼
Ghost 服务（需先在运行：.\start.ps1 或 .venv\Scripts\python.exe run.py）
```

5 个工具：`analyze_screen`（看窗口控件）、`click_element`（按文本点击）、
`type_text`（键入）、`draw_trajectory`（平滑鼠标绘制，可先取色板颜色）、
`execute_task`（安全队列执行任务）。

2 个资源（v0.14，MCP 规范只读 Resources——**不调工具就能"看到"环境**）：
`screen://current`（实时主屏截图，JPEG blob）、`ui://elements`（当前
GUI RAG 编号元素表，JSON，物理像素 bbox）。

## 1. 前置条件

1. Ghost 服务在跑（`http://127.0.0.1:8030/api/ghost/health` 返回 200）；
2. 拿到你的 token：打开 `E:\moray-ghost\.env`，复制 `GHOST_TOKEN=` 后面的值
   （本机凭据，**不要**发到聊天记录/截图里）；
3. Python 解释器：`E:\moray-ghost\.venv\Scripts\python.exe`（依赖已装齐，包括官方
   `mcp` SDK 2.3.0）。

## 2. Cursor 配置

编辑项目级 `.cursor\mcp.json`（或全局 `%USERPROFILE%\.cursor\mcp.json`）：

```json
{
  "mcpServers": {
    "moray-ghost": {
      "command": "E:\\moray-ghost\\.venv\\Scripts\\python.exe",
      "args": ["E:\\moray-ghost\\mcp_server.py"],
      "env": {
        "GHOST_TOKEN": "<把你 .env 里 GHOST_TOKEN 的值贴在这里>",
        "GHOST_AGENT_ID": "cursor",
        "GHOST_CAPABILITIES": "[\"notepad.exe\",\"mspaint.exe\"]"
      }
    }
  }
}
```

- Windows 路径必须**双反斜杠**（JSON 转义）；
- `GHOST_CAPABILITIES` 是**你授权 AI 摸哪些进程**的白名单（进程名小写，
  Ghost 服务端强制核对）。**留空或写 `[]` 表示不允许任何输入类动作**
  （analyze_screen 只读仍可用；点击/键入/任务会被服务端 403 拒绝）——
  这是故意的默认拒绝语义；
- 保存后重启 Cursor，工具列表里应出现 `moray-ghost` 的 5 个工具。

## 3. Claude Desktop 配置

编辑 `%APPDATA%\Claude\claude_desktop_config.json`（不存在就新建），内容同上
的 `mcpServers` 段。保存后**完全退出** Claude Desktop（托盘右键退出）再开。
开发者模式下可在 设置→开发者 看到该服务器与工具列表；服务器日志在
`%APPDATA%\Claude\logs\mcp-server-moray-ghost.log`（stdio 诊断走 stderr，
会被客户端收集到这里）。

## 4. 挂载真实视觉模型（可选但推荐；不挂则用内置 OCR 视觉）

Ghost 的感知后端 `auto` 顺序：llama（真 VL 模型）→ ocr（内置，已可用）→ native。
**现在就能用**（ocr 读像素文本）；想上 Qwen2-VL 级真模型：

### 4.1 下载（链接均经本机 HEAD 实测存在，2026-10-26）

国内镜像直链（本机实测可达；huggingface.co 直连在本网络超时）：

| 文件 | hf-mirror 直链 | 实测大小 |
|---|---|---|
| 2B 主模型 Q4_K_M（推荐） | `https://hf-mirror.com/bartowski/Qwen2-VL-2B-Instruct-GGUF/resolve/main/Qwen2-VL-2B-Instruct-Q4_K_M.gguf` | 986,047,232 B（≈940MB） |
| 2B 视觉投影器（**必需**） | `https://hf-mirror.com/bartowski/Qwen2-VL-2B-Instruct-GGUF/resolve/main/mmproj-Qwen2-VL-2B-Instruct-f16.gguf` | 1,331,656,192 B（≈1.24GB） |
| 2B 高一档量化 Q5_K_M（可选） | 同仓库 `Qwen2-VL-2B-Instruct-Q5_K_M.gguf` | 1,125,049,088 B（≈1.05GB） |
| 7B 主模型 Q4_K_M（画质升级，8GB 显存放得下） | `https://hf-mirror.com/bartowski/Qwen2-VL-7B-Instruct-GGUF/resolve/main/Qwen2-VL-7B-Instruct-Q4_K_M.gguf` | 4,683,072,672 B（≈4.36GB） |
| 7B 投影器 | 同仓库 `mmproj-Qwen2-VL-7B-Instruct-f16.gguf` | 同模式命名 |

官方源（同文件）：`https://huggingface.co/bartowski/Qwen2-VL-2B-Instruct-GGUF`
（仓库共 27 个 GGUF：Q2_K…Q8_0/IQ 系/f16 全量化 + mmproj f16/f32，文件清单经
镜像 API 实测）。

### 4.2 放置与启用

```
E:\moray-ghost\models\Qwen2-VL-2B-Instruct-Q4_K_M.gguf
E:\moray-ghost\models\mmproj-Qwen2-VL-2B-Instruct-f16.gguf
```

`E:\moray-ghost\.env` 追加（models/ 与 *.gguf 已 gitignore，权重永不入库）：

```ini
GHOST_VL_MODEL_PROFILE=2b
GHOST_VL_MODEL_PATH_2B=E:\moray-ghost\models\Qwen2-VL-2B-Instruct-Q4_K_M.gguf
GHOST_VL_MMPROJ_PATH_2B=E:\moray-ghost\models\mmproj-Qwen2-VL-2B-Instruct-f16.gguf
```

7B 下载完成后，只改档位与对应路径（文件未就位时 `auto` 会如实降级到 OCR）：

```ini
GHOST_VL_MODEL_PROFILE=7b
GHOST_VL_MODEL_PATH_7B=E:\moray-ghost\models\Qwen2-VL-7B-Instruct-Q4_K_M.gguf
GHOST_VL_MMPROJ_PATH_7B=E:\moray-ghost\models\mmproj-Qwen2-VL-7B-Instruct-f16.gguf
```

**混合图标定位**（v0.9，实验性）：`GHOST_VL_HYBRID=auto` 仅对 7B 档启用——
CV（Otsu + 连通域）先粗筛小图标，生成带编号的 contact sheet，再让 VL 只回答
“几号是目标”；`GHOST_VL_HYBRID_MIN_CONF`（默认 0.55）以下直接 404，绝不
把低置信度选号变成误点。**2B/7B 实测都会把直角矩形选成圆角矩形或菱形
（7B confidence 0.99），且点击后拖拽可能 0 像素**——因此示例 `.env` 默认
`GHOST_VL_HYBRID=0`；只有在做实验/接入 CV 形状校验时才改 `1`。

重启 Ghost 服务后 `analyze_screen` 的 `backend` 字段应变成 `llama`（首次调用
加载模型约 5–20s，之后进程内常驻）。

### 4.3 RTX 5060 8GB / CUDA 12.8 实测

- 已切 CUDA：`llama-cpp-python 0.4.5+cu128`（社区 wheel，SHA256
  `b894e5a0…`）+ `nvidia-cuda-runtime-cu12==12.8.90` +
  `nvidia-cublas-cu12==12.8.5.5` + `nvidia-cuda-nvrtc-cu12==12.8.93`
  提供 `cudart64_12.dll`/`cublas64_12.dll`（wheel 只带 `ggml-cuda.dll`），
  并把 `numpy` 对齐到 2.3.2（0.4.5 元数据上限）。安装见
  `requirements-gpu.txt`。`pip check` 仍会报
  `rapidocr → opencv-python` 缺失——这是项目共用
  `opencv-python-headless` 的已知刻意配置，不是真缺。
- **0.4.5 的 CUDA 后端在首次构造 `Llama` 时才注册**；驱动用 vocab-only
  探针预热后端，`/health` 的 `vl.gpu_offload` 才回 `true`。0.4.5 的 mmproj
  参数名是 `mmproj_path`（0.3.36 是 `clip_model_path`），驱动两版都兼容；
  `GHOST_VL_IMAGE_MIN_TOKENS=1024` 对应 Qwen-VL grounding 建议。
- 2B 实测（Ollama 同时占用显卡）：靶窗 582x356 / 1024x626 定向定位
  **TTFT 0.68–2.65s、总 2.8–5.1s**，按钮中心误差 ≤1px；全屏
  2560x1600→1024x616 的“列全控件”会被 256 token 截断（20–32s），
  `auto` 模式会如实降级 OCR——2B 不适合全屏密集 UI 的通用列表。
  显存峰值 5157MiB（含 Ollama），Ghost 服务 RSS ~1.8GB；无 OOM/掉卡。
- 7B Q4_K_M + mmproj ≈ 6.0GB：8GB 卡需先停 Ollama/其他 GPU 程序；
  24/28 层 + mmproj 上 GPU 实测：靶窗通用 TTFT 6.60s/总 12.79s，定向
  TTFT 2.17s/总 5.43s，中心误差 1px；显存峰值 7298MiB（剩 513MiB）。
  但**无文字形状仍不可靠**：「矩形」被选成圆角矩形 #11（confidence 0.99），
  「直角矩形」选成 #27（0.99）；画图测试 9/11（选中后拖拽 0 像素）。
  7B 档当前只适合文本/大控件；混合定位默认 0，下一步需要 CV 形状校验
  （角点/矩形度）或更强模型。
- 回滚 CPU：`pip install llama-cpp-python==0.3.36 --extra-index-url
  https://abetlen.github.io/llama-cpp-python/whl/cpu`。

## 4.4 v0.12 GUI RAG + Ghost Guard（Agent 侧行为变化）

- `click_element` 现在**优先走 GUI RAG**：服务端 OCR+CV+win32 检索带编号的
  元素表，文本精确命中直接返回（`method=text_exact`）；模糊目标交给 VLM，
  只回 `{"target_id": N}`，再由服务端映射回真实 bbox——**模型不生成坐标**。
  响应新增 `target_id` / `method`；未命中时才回落到旧的 analyze/target/hybrid
  路径，Tools 仍为 5 个，客户端无需改配置。
- 目标标签命中动态高危词（转账/付款/删除/格式化/退出登录/注销/卸载/重置…）
  时，服务端在鼠标移动前返回 **409 `PENDING_APPROVAL`**（带 `hits` /
  `requires_approval` / `target_id`），动作**零输入**。`click_element` 的 MCP
  层不会读取或透传审批令牌——**Agent 不能自批**。
- 人类审批走独立控制台：在**人工进程**环境配置 `GHOST_APPROVAL_TOKEN`，
  调用 `/api/ghost/execute_action` 或 `/browser/action` 时带
  `"approval_token": "<令牌>"`。令牌不要放进 MCP/Agent 的启动环境。
- 原生点击前会用 CV 定位落点控件并只 OCR 控件内部文本（避免相邻「删除」
  误伤）；OCR 引擎不可用时默认 **fail-closed**（503
  `VISUAL_GUARD_UNAVAILABLE`），可设 `GHOST_GUARD_FAIL_CLOSED=false` 显式降级。
- CDP 浏览器点击检查目标 DOM 文本（`source=cdp_dom`），同样支持
  `PENDING_APPROVAL` / `approval_token`。不抓浏览器窗口也能用
  `op=screenshot` 取页面截图做录屏/审计。

## 4.5 v0.14 Resources（不调工具也能"看到"环境）

MCP 规范的 Resources 是只读拉取：规划循环里每一步先 `resources/read` 拿到
环境，再决定调哪个 Tool——省掉「先调 analyze 才能看」的一轮往返。

| URI | 内容 | mimeType |
|---|---|---|
| `screen://current` | 实时主屏截图（物理像素，与 bbox 同一坐标系；每次 read 都是新抓的） | `image/jpeg` |
| `ui://elements` | 当前编号元素表（OCR+CV+win32 检索，零模型参与坐标；含 id/bbox/center） | `application/json` |

- 两者都是**只读端点**（`/screenshot`、`/vl/elements`）：冻结期照常可用、
  不产生任何输入；鉴权与 Tools 同一 `X-Ghost-Token`；
- 截图体积旋钮（启动环境变量）：`GHOST_MCP_SCREEN_SCALE`（默认 `1.0`）、
  `GHOST_MCP_SCREEN_QUALITY`（默认 `80`）；
- `ui://elements` 读回的 `id` 可直接用于 `click_element`/ground 选号——
  元素表与选择层同源，不会出现两套坐标；
- 自动化验证：`scripts/test_v14_mcp_resources.py`（握手/列表/blob 魔数/
  元素表命中/工具回归/错误路径，8 checks）。

## 5. 验证（不依赖任何客户端的自动化）

```powershell
.venv\Scripts\python scripts\test_v08_mcp_vl.py     # MCP+VL 全链 21 项（含门禁透传）
.venv\Scripts\python scripts\test_v08_paint_precision.py  # 画图工具像素级精度综合
.venv\Scripts\python scripts\test_v12_gui_rag.py    # GUI RAG 准确率 + Guard 拦截/审批（44 checks）
.venv\Scripts\python scripts\test_v12_heavy_gui.py  # Tk/浏览器填表实战 + GIF 录制（28 checks）
.venv\Scripts\python scripts\test_v14_mcp_resources.py  # Resources 协议（8 checks）
```

手工冒烟（客户端里说一句话即可）："打开记事本并在里面输入 hello"——
预期 AI 会调用 `analyze_screen` / `click_element` / `type_text`；越权进程
（不在 GHOST_CAPABILITIES 里）会被 403 `CAPABILITY_DENIED` 顶回来。

## 6. 排障速查

| 现象 | 原因与处置 |
|---|---|
| 客户端看不到工具 | 配置 JSON 语法错（Windows 路径要双反斜杠）/ 没重启客户端 |
| 工具返回 `GHOST_UNREACHABLE` | Ghost 服务没跑：先 `.\start.ps1` |
| 返回 `UNAUTHORIZED` | GHOST_TOKEN 不对（以 .env 为准，注意复制别带引号/空格） |
| 返回 `CAPABILITY_DENIED` | 目标进程不在 `GHOST_CAPABILITIES` 白名单——改配置并重启客户端 |
| 返回 `VL_ELEMENT_NOT_FOUND` | 屏幕上没有该文本的控件（窗口没打开/文本不同），先 `analyze_screen` 看看 |
| 返回 `WINDOW_NOT_FOUND` / `VL_AMBIGUOUS` | window 子串没命中/命中多个——换成更精确的标题 |
| 返回 `PENDING_APPROVAL` | 目标标签命中动态高危词（转账/删除/格式化/退出登录…）。动作未执行；由**人工控制台**带 `approval_token` 重新发起，或改点安全目标。Agent 无法自批 |
| 返回 `VISUAL_GUARD_UNAVAILABLE` | 原生点击前 OCR 不可用且默认 fail-closed。装好 rapidocr/onnxruntime，或显式设 `GHOST_GUARD_FAIL_CLOSED=false` |
| `click_element` 变慢（首次 1.5–3.5s） | GUI RAG 首次加载 OCR 引擎；之后元素检索 ~0.2–1.4s，VLM 目录选号 ~0.4s（GPU）。连续选号有 2s 缓存 |
| 网页没反应 | 网页目标走 CDP 分支：需先以 `--remote-debugging-port=9222` 启动浏览器，否则 503 `BROWSER_NOT_CONNECTED` |
| `analyze_screen` 很慢（首次 10–20s） | llama 模型首载；等一次就好（或未挂模型时用 ocr，~2s） |

## 7. 安全须知（给运维）

- `mcp_server.py` 自身**不监听任何端口**、不存凭据（token 只在进程内存，
  来自环境/.env）；
- 白名单在**拉起时**定死：AI 在对话里说"帮我点某某系统窗口"，只要它不在
  `GHOST_CAPABILITIES` 里，服务端一律 403——想扩大权限需要**人工**改配置
  并重启客户端；
- 每一次动作照常写 `data/ghost_audit.jsonl`（谁、何时、哪个 token 指纹、
  坐标明细）与前后审计截图，MCP 通道与 SDK/HTTP 通道同账本；
- **审批令牌绝不下发 Agent**：`GHOST_APPROVAL_TOKEN` 只放在人工控制台/审批
  服务环境；MCP 进程不读取它，`click_element` 遇到危险目标只会返回
  `PENDING_APPROVAL`。审计另有 `guard_pending` / `guard_approved` 事件；
- 急停三件套照常可用：网页大红按钮 / `POST /emergency_stop` / 物理
  **Ctrl+Alt+Q**（进程级退出 + 释放全部按键）。
