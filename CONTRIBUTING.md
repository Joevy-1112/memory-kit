# 贡献指南

感谢你有兴趣改进 memory-kit！本项目很小，规则也很少。

## 跑测试

```bash
cd memory-kit

# 全部测试（50 项，unittest，无需 pytest）
~/.hermes/hermes-agent/venv/bin/python run_tests.py
# 或任意 Python 3.10+ 环境：
python run_tests.py

# 只跑某一部分
python run_tests.py smoke      # 冒烟测试
python run_tests.py rings      # 三环回归
python run_tests.py dimension  # 混合维度回归
```

提交前请确保 `run_tests.py` 全绿。测试全部使用临时库（tempfile），不会碰正式库。

## 提 issue / 提 PR

- **Bug 报告**：附上最小复现（代码片段 + 报错栈），说明 Python 版本和是否启动了嵌入服务。
- **功能建议**：先说明使用场景（谁、在什么情况下会用到），再提方案。
- **PR**：小步提交，一个 PR 一个主题；代码风格跟随现有文件（中文注释、`_` 前缀私有方法、不做过度工程）。

## 开发说明

- 核心 API 只依赖 `numpy` + 标准库 `sqlite3`（FTS5），不要引入新依赖。
- 嵌入服务（`http://127.0.0.1:8082`）是可选的：**任何新代码在服务缺失时都必须优雅降级**，不允许抛异常阻断主流程。
- 新功能必须带测试，放在 `tests/` 下，命名 `test_*.py`。
- 涉及用户可见行为的改动，同步更新 README 的对应章节和「更新日志」。

## 版本号与发布

- 语义化版本（SemVer）。行为不变只修 bug → patch；新功能/重构 → minor。
- 发布流程：改 `pyproject.toml` 与 `memorykit/__init__.py` 的版本号 → README 更新日志加条目 → `git tag vX.Y.Z` → push。
