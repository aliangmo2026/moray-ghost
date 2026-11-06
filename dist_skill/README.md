# Moray-Ghost Desktop Controller — 技能插件使用说明

这是一个**本地技能插件**：平台云端不执行任何 PyAutoGUI 代码，只通过 HTTP
调用运行在用户电脑上的 [Moray-Ghost](https://github.com/) 服务（默认
`127.0.0.1:8030`）。接口定义见 `openapi.json`（37 条路由，由 FastAPI
自动生成导出）。

## 工作原理

```
云端平台大模型 ──HTTP(+Token)──> 内网穿透公网地址 ──> 用户本机 Ghost(8030)
                                                     │
                                              PyAutoGUI / OCR / VLM
                                              真实点击、截图、视觉定位
```

## 用户前置步骤

1. **本机启动 Ghost**（需要 Python 3.12 + 项目依赖）：

   ```powershell
   cd moray-ghost
   .\.venv\Scripts\Activate.ps1
   python run.py        # 服务监听 127.0.0.1:8030
   ```

   首次启动会在 `data/` 下自动生成访问令牌（token 文件），
   启动日志里可见 `token=`，或者从 `data/` 目录的 token 文件读取。

2. **内网穿透**：把本机 8030 端口映射到公网，例如：

   ```powershell
   cpolar http 8030
   # 或
   ngrok http 8030
   ```

3. **配置技能**：
   - `base_url`：填入上一步得到的公网地址（如 `https://xxxx.cpolar.top`）
   - Token：填入第 1 步的令牌。请求会以 `X-Ghost-Token: <token>` 头发出；
     若平台只支持 Bearer，代理入口（`main.py`）会自动改写为该头。

## 常用接口速查

| 能力 | 方法 + 路径 | 说明 |
| --- | --- | --- |
| 执行动作 | `POST /api/ghost/execute_action` | 点击/键入/滚动/热键，全部走安全门禁 |
| 截屏 | `POST /api/ghost/screenshot` | 返回桌面截图（JPEG base64） |
| 视觉分析 | `POST /api/ghost/vl/analyze` | VLM 全屏分析 |
| 元素定位 | `POST /api/ghost/vl/ground` | 按自然语言描述定位像素坐标 |
| 紧急停止 | `POST /api/ghost/emergency_stop` | 立即冻结所有物理输入（也可用热键） |
| 健康检查 | `GET /api/ghost/health` | 服务状态（注意返回字段是 `status:"ok"`） |

## 代理入口（`main.py`，可选）

如果平台强制要求提供可执行入口文件，可运行本目录的代理壳：

```powershell
$env:GHOST_LOCAL_URL = "http://127.0.0.1:8030"   # 本机 Ghost 地址（默认值）
$env:GHOST_TOKEN     = "<你的 token>"            # 可选，透传给 Ghost
python main.py                                     # 监听 0.0.0.0:8080
```

代理把收到的 `/api/ghost/*` 请求原样转发到本机 Ghost（自动透传
`X-Ghost-Token` / `Authorization: Bearer`），适合平台要求"访问一个固定
公网入口"的场景——此时把 `base_url` 填成代理的公网映射地址即可。

## 安全须知

- Ghost 自带多层门禁：冻结开关、动作黑名单、四角 Fail-Safe、UAC/危险弹窗
  拒绝（System1 反射脑）。公网暴露前请务必保管好 token——**持有 token
  即可控制你的桌面**。
- 不要把 token 写进任何要提交的文件；平台侧请用其密钥管理功能保存。
- 本插件不包含 `models/`、`.venv/`、`data/`，全部执行逻辑都在用户本机的
  Ghost 仓库内。
