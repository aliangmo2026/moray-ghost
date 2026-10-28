# 致开源社区的公开信

** moray-ghost v0.15.0 · 2026-10-28 **

---

亲爱的开源朋友们：

你们好。我们是一支小团队，过去几天里，我们在一台普通的 Windows 桌面机上，
把一个"能动桌面的脚本工具"一路推进到了"会看、会想、会学、会进化"的雏形
——我们内部把它叫做 **moray-ghost**（幻影）。写这封信，是想把这段旅程
诚实地摊开，也想把踩过的坑回馈给社区。

## 我们这几天走了多远

- **v0.8-v0.11：让它"看得见"。** 本地多模态（Qwen2-VL GGUF + llama-cpp CUDA）
  上了真机，GUI RAG 让模型不再猜坐标——OCR+CV 编号元素表，视觉模型只回一个
  target_id，坐标幻觉从此归零。它学会了用鼠标画像素画：64×64 和 128×128 的
  豆包肖像，逐笔误差 2-4px。
- **v0.12-v0.13：让它"干得成、守得住"。** Ghost Guard 点击前动态高危词门禁
  （409 PENDING_APPROVAL，人不点头它绝不下手）；重型 Tk/浏览器填表实战；
  没有 CAD 软件？它自己搭了个迷你 CAD 控制台，用 10 条键盘命令流画出了带
  尺寸标注的图纸（DXF/像素/VLM 三重验证）。
- **v0.14：让它"扛得住恶劣环境"。** 多屏混合 DPI 坐标对齐（Per-Monitor V2 +
  相对事件像素级修正），48 小时挂机压测零泄漏，MCP Resources 让任意 AI Agent
  即插即用。
- **v0.15（就是今天）：让它"有反射、能跨界、会偷师"。**
  1. **快慢双系统**——本地 OCR 规则反射脑：弹窗特征词与关闭按钮同窗共现时，
     不必等慢速视觉模型，本地直接消解（UAC/黑名单在 OCR 之前零输入拒绝）；
  2. **跨平台驱动抽象**——Windows 真身迁入 `app/platforms/`，macOS/Linux
     骨架就位（绝不假装能跑），一份契约向三平台进军；
  3. **自我进化技能库（行为克隆）**——pynput 静默看人操作，本地 VLM 给每次
     点击标注"这是什么按钮"，存进 SQLite；下次一句话按名回放，且全程走同一
     安全管道。**它开始从人类的操作里学习了。**

## 我们最想分享的三个坑

1. **pynput 回调线程就是 Windows 输入钩子的消息线程。** 回调里做截图（秒级）
   会阻塞整条钩子链，把键盘注入拖到硬超时冻结。回调必须微秒级返回，重活
   交给独立线程。
2. **OCR 的 ONNX 冷启动是 15-25 秒。** 如果等第一次点击才初始化，首击直接
   顶穿动作硬超时。服务启动就该后台预热。
3. **DPI 感知必须发生在 import mss/pyautogui/tkinter 之前。** 否则你以为的
   物理像素坐标全是虚拟化的，150% 缩放下静默错位 ×1.5——这类 bug 在单屏
   100% 环境下永远测不出来。

## 我们的原则（一条不让）

- **锁屏是安全边界。** 解锁需要用户凭据，我们绝不绕过、绝不向锁屏打字。
- **最小权限。** 每一次输入都要声明能力白名单；未声明 = 拒绝，没有旁路。
- **诚实降级。** 视觉模型不可用就挂起等主仓，绝不谎报"已完成"；VLM 标注
  不了就存原始坐标 + 低置信度，绝不编造语义。
- **不宣称项目安全。** 安全扫描没出完整结论，我们就如实写"没有完整结论"。

## 致谢与邀请

感谢 llama.cpp、rapidocr、pynput、FastAPI、mss、pyautogui 等站在巨人肩膀上
的一切。moray-ghost 按开源协议开放，`docs/` 里有完整的接入指南和测试资产
（每个特性都有可复跑的验收脚本——我们相信**可复跑的测试就是最好的文档**）。

如果它帮到了你，或者你发现了坑——欢迎提 Issue。
如果它启发了你去做更酷的东西——那这封信就值了。

此致，

**moray-ghost 团队**
2026 年 10 月 28 日

---
---

# An Open Letter to the Open Source Community

** moray-ghost v0.15.0 · 2026-10-28 **

---

Dear friends in open source,

Hello. We are a small team. Over the past few days, on an ordinary Windows
desktop, we pushed a "desktop-driving script" all the way to a rudimentary
system that can **see, think, learn, and evolve** — internally we call it
**moray-ghost**. We are writing this letter to lay out that journey honestly
and to pay our pitfalls forward to the community.

## How far we traveled in a few days

- **v0.8–v0.11: giving it eyes.** Local multimodal (Qwen2-VL GGUF +
  llama-cpp CUDA) on real hardware. GUI RAG ended coordinate hallucination:
  OCR+CV builds a numbered element table, and the vision model only returns a
  `target_id`. It learned to paint pixel art — 64×64 and 128×128 portraits
  with 2–4 px per-stroke accuracy.
- **v0.12–v0.13: making it capable and safe.** Ghost Guard performs dynamic
  high-risk-word gating before every click (409 PENDING_APPROVAL — it will not
  act until a human approves). Heavyweight native/browser form-filling drills.
  No CAD software installed? It built its own mini CAD console and drew a
  dimensioned technical drawing through 10 keyboard command streams —
  triple-verified via DXF, pixels, and the VLM itself.
- **v0.14: surviving hostile environments.** Multi-monitor mixed-DPI coordinate
  alignment (Per-Monitor V2 + relative mouse-event pixel correction), 48-hour
  soak tests with zero leaks, and MCP Resources so any AI Agent can plug in.
- **v0.15 (today): reflexes, portability, and learning from humans.**
  1. **Fast/slow dual system** — a local OCR-rule reflex brain that resolves
     popups locally when trigger words and dismiss buttons co-occur (UAC and
     blacklisted windows are refused with *zero input*, before OCR even runs);
  2. **Cross-platform driver abstraction** — the Windows implementation moved
     into `app/platforms/`; macOS/Linux skeletons are in place (never pretending
     to work); one contract, three platforms;
  3. **A self-evolving skill library (behavior cloning)** — pynput silently
     watches a human work, a local VLM labels what each click targeted, skills
     go into SQLite, and any of them can be replayed by name — through the
     exact same safety pipeline. **It has started learning from humans.**

## The three bugs we most want to share

1. **The pynput callback thread *is* the Windows hook message thread.**
   Doing screenshot work (seconds-scale) inside a callback blocks the entire
   hook chain and strangles keyboard injection until the hard timeout freezes
   the service. Callbacks must return in microseconds; heavy work belongs on a
   dedicated thread.
2. **OCR's ONNX cold start takes 15–25 seconds.** If you initialize it lazily
   on the first click, that first click blows through the action hard timeout.
   Warm it up in the background at service startup.
3. **DPI awareness must be established *before* importing mss / pyautogui /
   tkinter.** Otherwise your "physical pixel" coordinates are virtualized and
   silently off by ×1.5 at 150% scaling — a class of bug that never reproduces
   on a single 100% monitor.

## Principles we refuse to compromise

- **The lock screen is a security boundary.** Unlocking requires user
  credentials; we never bypass it and never type into it.
- **Least privilege.** Every input must declare a capability whitelist;
  undeclared = denied. No side doors.
- **Honest degradation.** If the vision model is unavailable, the task waits —
  never a false "done." If the VLM cannot label a click, we store raw
  coordinates with low confidence — never invented semantics.
- **No safety claims without evidence.** When our security scanner could not
  produce a complete report, we say exactly that.

## Thanks and an invitation

Thanks to llama.cpp, rapidocr, pynput, FastAPI, mss, pyautogui, and everyone
whose shoulders we stood on. moray-ghost is open sourced; `docs/` contains the
full integration guide and the test assets — every feature ships with a
re-runnable acceptance script, because we believe **re-runnable tests are the
best documentation**.

If it helps you, or if you find our bugs — an Issue is warmly welcomed.
If it inspires you to build something cooler — then this letter was worth it.

Respectfully,

**the moray-ghost team**
October 28, 2026
