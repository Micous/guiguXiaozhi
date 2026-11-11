"""仓储层异常分类 —— Phase 3a（审计 P0-04）。

**为什么要有这个模块**

改造前 `repositories/appointment_repo.py` 有 4 处 ``except Exception`` 静默
``return None`` / ``return False``：数据库不可用、连接超时、唯一键冲突、
字段超长 —— 全都变成"看起来正常"的结果：

.. code-block:: text

    save() 返回 None  → 工具报「预约写入失败，请稍后重试」
    find_duplicate() 返回 None → 查重**形同虚设**，本该拦下的重复预约被放行
    delete() 返回 False → 工具报「取消预约失败」（但真实原因可能是库挂了）

上层无法区分"业务上没有"和"数据库坏了"，于是：

* 业务失败被当成系统失败 → 用户看到的是误导文案；
* 系统失败被当成业务失败 → **失败被静默吞掉**，写操作可能根本没发生，
  而用户以为约上了（这正是审计 P0-04「无异常分类」的核心危害）。

**分类原则**：按"调用方该怎么反应"分类，而不是按"底层出了什么事"分类。

======================  =========================================================
异常                    调用方应该做什么
======================  =========================================================
``NotFound``            业务上没有这条记录 → 可以如实告诉用户"没有这条预约"
``UniqueConflict``      业务规则冲突（重复预约）→ 如实告诉用户"已有预约"
``RepositoryUnavailable`` 基础设施不可用 → 明确说"系统暂时不可用，请稍后重试"，
                        **不能**说成"业务失败"（更不能当成成功）
``InvalidState``        状态机不允许的操作 → 如实说明当前状态
``PersistenceError``    其他写入失败 → 归入"系统错误"，不编造业务结论
======================  =========================================================

**兼容性**：本模块是**新增**的，异常只在仓储内部抛出、由调用方显式捕获。
默认行为（工具层把异常转成对应的 ``ToolResult.fail``）保持不变，
不需要开关 —— 因为原来的"吞异常"本来就��错误行为被掩盖，
现在只是把它**显式化**。
"""

from __future__ import annotations


class RepositoryError(Exception):
    """仓储层异常基类。

    带 ``reason_code`` 便于上层按错误码分支（与 ``ApprovalError`` 契约一致），
    也便于日志聚合时按码统计。
    """

    def __init__(self, reason_code: str, message: str = "") -> None:
        self.reason_code = reason_code
        super().__init__(message or reason_code)


class NotFound(RepositoryError):
    """业务上不存在这条记录（不是基础设施问题）。"""

    def __init__(self, message: str = "记录不存在") -> None:
        super().__init__("not_found", message)


class UniqueConflict(RepositoryError):
    """唯一约束冲突 —— 并发下同一幂等键/同一预约键重复写入。

    这是**并发下防重复的最后一道防线**：工具层的 ``find_duplicate`` 查重
    与写入是两次独立操作，中间存在竞态窗口，唯一约束负责把窗口关掉。
    """

    def __init__(self, message: str = "记录已存在（唯一约束冲突）") -> None:
        super().__init__("unique_conflict", message)


class RepositoryUnavailable(RepositoryError):
    """基础设施不可用（连接失败、超时、库不存在等）。

    **绝不能**被当成"业务失败"或"成功"——上层必须如实告诉用户系统问题。
    """

    def __init__(self, message: str = "数据存储暂时不可用") -> None:
        super().__init__("repository_unavailable", message)


class InvalidState(RepositoryError):
    """状态机不允许当前操作（如取消一条已取消的预约）。"""

    def __init__(self, message: str = "当前状态不允许该操作") -> None:
        super().__init__("invalid_state", message)


class PersistenceError(RepositoryError):
    """其他写入失败（字段超长、约束冲突、数据损坏等）。"""

    def __init__(self, message: str = "写入失败") -> None:
        super().__init__("persistence_error", message)
