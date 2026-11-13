"""写工具清单 —— B3-2 收敛后的**单一事实来源**。

此前 `runtime/state.py` 与 `runtime/task_tracker.py` 各自维护一份
``_WRITE_TOOLS = ("预约挂号", "取消预约挂号")``：B3-2 的 STARTED 强保证
按这份清单决定「落库失败是否拒绝执行」，两个真相来源会让门控规则出现
歧义（清单改了一处漏一处 → 写操作在错误的清单口径下被放行 / 误拒）。

约定：
* 任何「这是不是写操作」的判定（``TurnState.record_write_result`` 的入账
  范围、``TaskTracker.tool_finished`` 的 ``side_effect_committed``、
  ``ToolCoordinator`` 的 STARTED 失败门控）都必须引用本模块；
* ``ToolDefinition.effect`` 声明式标注（方案 §5 B6 工具契约）落地前，
  本清单是唯一权威；届时应把这里改为从注册表派生，而不是继续手工维护。
"""

from __future__ import annotations

WRITE_TOOL_NAMES: frozenset = frozenset({"预约挂号", "取消预约挂号"})
