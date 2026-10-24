# moray-ghost

Windows 桌面自动化本地服务（v0.2）：**mss 极速截图 + 授权式 pyautogui 动作 + 宏系统骨架**。

只监听 `127.0.0.1:8030`；绝不触碰 `D:\MoRay`（主仓）。

## 安全模型

- **token 鉴权**：所有执行动作接口（`/execute_action`、`/macro/*`）强校验请求头
  `X-Ghost-Token`，缺失或不匹配一律 401，动作不执行。`/health`、`/screenshot`
  为探活放行。
- **Fail-Safe 常开**：`pyautogui.FAILSAFE=True` 硬编码、无开关。鼠标甩到屏幕角落
  立即中止整个动作/宏 → HTTP 409 + 现场截图。
- **审计无盲点**：任何动作执行前、后各拍一张全分辨率截图落盘
  （`data/screenshots/`），并随响应返回路径。
- **全局串行**：桌面动作持进程内锁串行执行（宏重放 v0.3 也必须走同一管道）。
- **坐标防线**：越界 / 落入 Fail-Safe 角落（保护半径 2px）/ moveTo 后漂移 >3px
  一律 400 `SAFETY_BLOCKED`。
- **DPI 感知**：Windows 下在 `import mss` / `import pyautogui` 之前调用
  `SetProcessDpiAwareness(2)`，保证截图分辨率与点击坐标一致（物理像素）。
- **mss 实例管理**：BitBlt 抓屏全部收口到单一专用 capture 线程（一个 mss 实例、
  队列化、失败自愈 + 退避重试）。实测多线程各自持 mss 实例时 BitBlt 会间歇性
  失败（拒绝访问/no error），单线程收口彻底消除，同时满足"禁止请求线程反复
  new mss（GDI 句柄泄漏）"红线；`monitors()` 枚举保留 thread-local 缓存。
- **token 来源**：只从 `.env`/环境变量读取；缺失时首次启动生成随机 token 写回
  `.env`（已 gitignore），源码与示例中不含可用凭据。

## 快速开始

```powershell
.\start.ps1                                   # 首次运行自动建 .venv 装依赖并启动
.venv\Scripts\python scripts\smoke.py         # 冒烟：health / screenshot / 401 / macro 占位
.venv\Scripts\python scripts\smoke.py --failsafe   # 追加：Fail-Safe 409 现场测试
```

冒烟脚本会把鼠标甩到屏幕左上角再还原（`--failsafe` 时）；`--no-click` 可跳过真实点击。

## 接口契约（v0.2）

### GET /health（免 token）

```json
{"status":"ok","service":"moray-ghost","version":"0.2.0","fail_safe_enabled":true,
 "token_required":true,"backends":{"screenshot":"mss","input":"pyautogui"},
 "monitors":[{"index":1,"left":0,"top":0,"width":2560,"height":1440,"primary":true}],
 "cursor":[x,y],"db_path":"...\\data\\ghost.db"}
```

### GET /screenshot（免 token）

query：`monitor=1|0|all`（0=全屏联合区，all=各屏数组）、`fmt=png|jpeg`、
`quality=10..95`、`scale=0.1..1.0`、`save=true|false`

```json
{"ok":true,"monitor":1,"monitor_origin":[0,0],
 "original_width":2560,"original_height":1600,"width":1280,"height":800,
 "format":"jpeg","scale":0.5,"screenshot_path":"...\\after.jpg",
 "cursor":[x,y],"elapsed_ms":24,"image_b64":"..."}
```

`original_width/height` 为真实屏幕尺寸，`width/height` 为缩放后尺寸；`monitor=all` 返回 `{"ok":true,"monitor":"all","shots":[每屏一条同构记录],"elapsed_ms":...}`。

### POST /execute_action（必须 `X-Ghost-Token`）

```json
{"action":"click|double_click|right_click|move|type|hotkey",
 "x":800,"y":500,"text":"hello","monitor":1,
 "settle_seconds":0.3,"include_before_b64":false,
 "fmt":"jpeg","quality":70,"scale":0.5,
 "source_width":1280,"source_height":800}
```

- `source_width/height` 可选（须同时给出）：给出时 x/y 视为缩放源图上的坐标，服务端换算为
  物理像素（400 `BAD_SOURCE_SIZE`：源尺寸非法或大于屏幕）；缺省时 x/y 即物理像素，行为不变。
- 精度：非整数倍缩放时换算精度为 ±1px（round 取整），需要亚像素精度请传 source_width = original_width（即不缩放）。
- 200：`{ok, action, executed_at, cursor_before, cursor_after, x, y, coord_source
  ("physical"|"scaled"), resolved_physical, monitor, monitor_origin,
  before_screenshot_path, after_screenshot_path, elapsed_ms, after_image_b64, before_image_b64?}`
- 401 `UNAUTHORIZED`：token 缺失/错误
- 400 `SAFETY_BLOCKED`：坐标越界 / 落入 Fail-Safe 角落 / 鼠标漂移 >3px
- 409 `FAIL_SAFE_TRIGGERED`：用户甩鼠标到角落，附 `scene_screenshot_path` +
  `scene_image_b64` 现场截图
- `hotkey` 的 x/y 可省略；`type` 前会把光标 moveTo 到 (x,y) 并做漂移校验

### 宏系统（骨架）

| 方法 | 路径 | 状态 |
|---|---|---|
| GET | /macro | 已可用（读 SQLite，初始返回空数组） |
| GET | /macro/{id} | 已可用（404 或详情） |
| POST | /macro/record/start / stop | 501（v0.3） |
| POST | /macro/{id}/replay | 501（v0.3；body 含 dry_run / allow_fallback / task_id） |
| POST | /macro/{id}/heal | 501（v0.3；主仓回填修补锚点） |

所有 `/macro/*` 同样强校验 `X-Ghost-Token`（无 token 先 401）。重放落地时必须走
`app/core/controller.py` 的同一安全管道。存储：`data/ghost.db`（macros /
macro_runs / used_nonces 三张表，全部参数化查询）。

#### POST /macro/probe（锚点匹配查询，只读，不执行任何动作）

请求：`{"screenshot_crop":"<base64 PNG（物理像素裁剪）>","match_threshold":0.8,"monitor":1}`

三态响应（**抓屏失败 ≠ 匹配失败**）：

- 200 `{"ok":true,"status":"matched","center":[x,y],"score":0.93,"bbox":[x,y,w,h],"elapsed_ms":...}`
  —— center 为物理像素，可直接喂 `/execute_action`（不带 source_width）
- 200 `{"ok":true,"status":"not_found","best_score":0.42,"elapsed_ms":...}`
  —— 抓屏成功但屏上没有锚点，这才允许降级让主仓重新识图
- 503 `{"ok":false,"status":"capture_failed","error":"...","elapsed_ms":...}`
  —— 抓屏本身失败，主仓应重试当前步骤，绝不降级识图

坐标铁律：裁剪与匹配全程物理像素（scale=1.0），内部零缩放；锚点中心只决定
"在哪裁"，不参与匹配坐标计算，重放坐标一律来自匹配框中心。零方差（纯色）模板
会被直接判 not_found（TM_CCOEFF_NORMED 对其数学上无定义，实测会吐伪高分）。

鉴权支持两种 token：静态 token（默认），以及 `GHOST_INTENT_TOKEN_ENABLED=true`
时启用 HMAC Intent Token（`intent.<payload_b64>.<sig_b64>`，nonce 落
`used_nonces` 防重放）。

## 目录结构

```
app/main.py                 入口 + token 中间件 + lifespan
app/api/routes.py           /health /screenshot /execute_action
app/api/macro_routes.py     /macro/*（501 占位 + 只读列表/详情）
app/core/config.py          .env、常量、DB 路径
app/core/screen_mss.py      thread-local mss + DPI + 编码
app/core/backend.py         InputBackend 抽象 + PyAutoGUIBackend
app/core/controller.py      统一安全管道（锁 + Fail-Safe + 前后截图）
app/core/auth.py            token 校验（静态 + HMAC Intent Token 可开关）
app/core/macro_store.py     SQLite 读写 + schema 初始化
app/core/macro_runner.py    重放骨架（v0.3 落地）
scripts/smoke.py            冒烟：health / screenshot / 401 / click / failsafe / macro
run.py  start.ps1  .env.example  requirements.txt
```

## 已知限制（v0.2）

- `type` 走 `pyautogui.write`，仅支持 ASCII 文本；中文输入需要剪贴板/UNICODE
  注入方案（列入 v0.3）。
- 主屏（primary）判定采用虚拟屏 (0,0) 原点启发式（mss 不提供主屏标记）。
- `coordinates`/`screenshot` 返回的 `width/height` 为缩放后最终图像尺寸；
  审计落盘文件始终是全分辨率。
