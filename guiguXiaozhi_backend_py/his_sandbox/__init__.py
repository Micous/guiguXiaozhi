"""HIS 本地沙箱：以 参考项目/his 的领域模型为蓝本的独立可运行 HIS 服务。

子模块按需导入，避免 ``python -m his_sandbox.seed`` / ``.server`` 时
被包级 import 重复加载（会产生 RuntimeWarning 与不可预期的重复初始化）。
使用方法：

.. code-block:: python

    from his_sandbox.config import SandboxConfig
    from his_sandbox.store import HisStore
    from his_sandbox.server import create_server
"""

__all__ = ["SandboxConfig", "HisStore", "create_server", "seed"]
