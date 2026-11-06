# BACKLOG — 底层优化点记录

> 实战排障中发现、暂未修但必须留痕的改进项。修复后在此打勾并链到提交。

## [window_locator] UWP 根父窗提取加超时重试机制

- **来源**：2026-10-28 地狱级任务实战（`test_hell_dynamic_task.py`）。
- **现象**：UWP 计算器（CalculatorApp.exe）的内容子窗先出现、
  ApplicationFrameHost 父链**稍后**才挂上——绑定瞬间 `GetAncestor(GA_ROOT)`
  返回子窗自身；拿子窗坐标系去点击必被 SAFETY_BLOCKED（root mismatch）。
  测试侧已用 `wait_root()` 轮询兜底（0.2s 间隔、4s 超时），但这属于
  **底层窗口定位的通用问题**，应下沉到 `app/core/window_locator`：
- **优化目标**：
  1. `point_root` / 根窗归一化时，对「顶层但疑为 UWP 内容子窗」的句柄做
     有界重试（建议 3~5 次 × 200ms），父链挂稳后再返回；
  2. 超时仍无父 → 按顶层窗放行（Edge 类真·顶层窗不受影响），并把
     `root_resolution="pending_parent"|"timeout_top"` 写进审计日志，
     方便回放定位同类问题。
- **验收**：新建 UWP 计算器实例连续 20 次绑定零 root-mismatch；普通 Win32
  窗口路径零额外延迟（首次 GetAncestor 即有父/无子，不进重试）。
