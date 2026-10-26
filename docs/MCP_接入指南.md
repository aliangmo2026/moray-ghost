# Ghost MCP 接入指南（Cursor / Claude Desktop / 任意 MCP 客户端）

> 版本：v0.8.0+ · 服务端安全门禁在 MCP 模式下 100% 生效（令牌 / 能力白名单 /
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

4 个工具：`analyze_screen`（看窗口控件）、`click_element`（按文本点击）、
`type_text`（键入）、`execute_task`（安全队列执行任务）。

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
- 保存后重启 Cursor，工具列表里应出现 `moray-ghost` 的 4 个工具。

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

**混合图标定位**（v0.9）：`GHOST_VL_HYBRID=auto` 默认只对 7B 档启用——
CV（Otsu + 连通域）先粗筛小图标，生成带编号的 contact sheet，再让 VL 只回答
“几号是目标”；`GHOST_VL_HYBRID_MIN_CONF`（默认 0.55）以下直接 404，绝不
把低置信度选号变成误点。2B 档默认关闭（实测 2B 对直角/圆角矩形语义不可靠）。

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
  `GHOST_VL_MODEL_PROFILE=7b` 后混合定位 `auto` 自动启用。
- 回滚 CPU：`pip install llama-cpp-python==0.3.36 --extra-index-url
  https://abetlen.github.io/llama-cpp-python/whl/cpu`。

## 5. 验证（不依赖任何客户端的自动化）

```powershell
.venv\Scripts\python scripts\test_v08_mcp_vl.py     # MCP+VL 全链 20 项（含门禁透传）
.venv\Scripts\python scripts\test_v08_paint_precision.py  # 画图工具像素级精度综合
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
- 急停三件套照常可用：网页大红按钮 / `POST /emergency_stop` / 物理
  **Ctrl+Alt+Q**（进程级退出 + 释放全部按键）。
