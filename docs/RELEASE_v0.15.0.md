# moray-ghost v0.15.0 — AGI 物理躯体三大进化

**Tag**: `v0.15.0` · **Date**: 2026-10-28 · **Commits**: `c11c7c9`（阶段一）→ `50bec13`（阶段二）→ `2e7b57f`（阶段三）→ `b16435f`（封板）

> 从"高级脚本"向"真正的 AGI 物理躯体"跨越的关键版本。
> 三大进化一次交付：**快慢双系统**（本地反射脑）、**跨平台驱动抽象**（向 Mac/Linux 进军的桥头堡）、
> **自我进化技能库**（行为克隆 + VLM 语义标注——AGI 自我进化的雏形）。

---

## 一、System1 微型反射脑（快慢双系统）`47/47`

慢系统（VLM → degraded 挂起）之前，新增**本地快思考反射**：

- **规则库**（`safety_gate`）：18 个弹窗特征词（软件更新/广告/隐私协议/每日提示…）
  × 22 个关闭类按钮词（关闭/×/稍后/同意…，按安全优先级排序，"同意"永远最后），
  **同窗 AND 共现**才命中——仅特征词→`no_button`、仅按钮→`no_trigger`，正文零误伤
  （按钮尺寸启发 10-360×8-90px）。
- **编排**（`vl_driver.system1_reflex`）：标题零 OCR 预筛控延迟；**UAC/黑名单窗口在
  OCR 之前零输入拒绝**（锁屏下同样有效）；点击走 `run_action` 全门禁；**点击后复检
  才算 dismissed**（窗口消失或规则不再命中），否则如实回退挂起，绝不谎报。
- **挂点**：任务 `require_vision` 步骤命中 → 本地消解、任务继续、不返回 degraded。
- **开关**：`GHOST_SYSTEM1_AUTO_DISMISS`（默认 true；false = 退回 v0.14 行为）。

## 二、跨平台驱动抽象 `app/platforms/` `21/21`

- `BaseScreen` / `BaseInput` 抽象契约 + Windows 真身全量迁入 `windows.py`
  （抓屏线程/编码修剪 + FAILSAFE/mouse_event/PyAutoGUIBackend 一道不少）。
- `macos.py` / `linux.py`：**NotImplementedError 骨架——绝不假装能跑**。
- 按 `sys.platform` 动态加载（懒加载、未知平台 `PlatformError`、单例缓存）。
- `core/backend.py`、`core/screen_mss.py` 变**再导出 shim**：全量 `is` 同一性 =
  单一模块状态零分裂，**旧引用零回归**。
- 启动日志与 `/environment` 回显 `platform` 字段。

## 三、自我进化技能库（行为克隆）`28/28`

**偷学链路**：`/skill/record/start` → pynput 全局静默录制（点击/打字/滚动 +
点击现场截图节流落盘）→ `/skill/record/stop` → 本地 VLM 逐点击语义标注
`target_text`（llama 不可用时**诚实降级**：原始坐标 + confidence=0.3）→
SQLite `skills` 表 → `/skill/run` 按名回放。

- 回放走 `controller.run_action` **同一安全管道**（能力令牌/冻结/UAC/黑名单/
  Fail-Safe/熔断/审计截图一道不少）。
- `continue_on_error=true` 噪声容忍：录制混入的人类鼠标噪声点击被能力白名单
  **零输入拒绝**，目标步骤照常执行。
- 配置：`GHOST_SKILL_RECORD` / `GHOST_SKILL_MAX_EVENTS` / `GHOST_SKILL_SHOT_INTERVAL`
  / `GHOST_SKILL_ANNOTATE_CALLS`。

## 顺手修掉的两个底层坑

1. **pynput 钩子线程阻塞**：回调线程就是 Windows 输入钩子消息线程，回调里做
   BitBlt/JPEG（秒级）会拖死 `keyboard.write` 注入 → 22.5s 硬超时冻结。
   修复：回调只入队（微秒级），帧采集独立线程。
2. **OCR ONNX 冷启动顶穿首击**：rapidocr 会话创建 15-25s，服务起来后第一次点击
   直接 408 冻结。修复：lifespan 后台预热（日志 `ocr warmed`）。

---

## 封板回归（全绿）

| 套件 | 结果 | | 套件 | 结果 |
|---|---|---|---|---|
| test_v15_system1 | **47/47** | | smoke | **14/14** |
| test_v15_platforms | **21/21** | | test_v12_gui_rag | **44/44** |
| test_v15_skills | **28/28** | | test_v072_extreme | **40/40** |
| test_v14_dpi（坐标红线） | **20/20** | | | |

> 如实声明：Mimosa 安全扫描在本版本所有 commit 时均无完整结论
> （python_ast_unavailable），按兼容策略提交——**不宣称项目安全**。

## 升级

```bash
git checkout v0.15.0
.venv\Scripts\python run.py   # /health 应显示 version 0.15.0
```

新配置均有默认值，不设 `.env` 也即刻生效（System1 开、技能录制开、OCR 预热开）。
