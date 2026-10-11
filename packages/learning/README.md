# 学习领域服务

这是 Study Agent 的业务包，复用原 Study System 的成熟实现，供 `packages/agent/` 调用。它不是另一个独立产品；产品说明与发布状态统一见仓库根目录 README。

- `src/study_agent/`：任务、计划、材料、课程状态、作答、错点、复测、备份与交付检查；SQL migrations 随包保存。
- `skills/`：助理处理学习业务时读取的质量标准，不替用户规定学习顺序。
- `tools/`：交付核验、编译辅助、清理与可选 macOS 日历工具。
- `hooks/`：兼容原执行方式的生命周期提醒，不是云端的另一套模型循环。
- `tests/`：使用隔离数据的领域检查。
- `study`：内部兼容命令。对外统一通过根目录 `study-agent service --data ROOT` 调用。

数据位于明确指定的学期目录，沿用 `.study-system/` 布局；源码调整不会迁移或覆盖真实资料。整体记忆、通用模型流程与学期切换由应用核心管理，领域服务继续核验实际学习证据。

维护检查：在本目录执行 `PYTHONPATH=src python3 -m unittest discover -s tests -t .`，或使用根目录 `./study-agent check`。可选工具的环境限制如实报告。不要在原自用工作区运行开发检查。
