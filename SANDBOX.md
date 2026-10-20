# MemoryKit v2 sandbox

本目录是从生产仓库拷贝出来的实验副本。**不在生产路径上运行。**

## 路径

```text
sandbox repo : /home/joe/memory-kit-v2
sandbox db   : /home/joe/memory-kit-v2/var/memory-sandbox.db
live repo    : /home/joe/memory-kit
live db      : /home/joe/.hermes/memory.db
```

## 本次拷贝快照

```text
live db sha256   : 6055f4f0db72c2721b867b9cee8a009c59e48ac0e0c26aba28a95a8a7ba133bd
live db mtime    : 2026-10-20 04:00:01 +0800
live db size     : 120,360,960 bytes

sandbox db sha256: 978dbe2e1fe4a6300ecf8bf1cd2938487d1ab1103038429ce49f71298a7b6199
sandbox db mtime : 2026-10-20 08:12:45 +0800
sandbox db size  : 120,360,960 bytes
```

沙箱库由 SQLite `.backup` 生成，页面布局与 live 文件哈希不同是正常的；行内容应一致。

## 使用规则

1. 所有命令显式指定沙箱库：

```bash
export MEMORY_DB_PATH=/home/joe/memory-kit-v2/var/memory-sandbox.db
```

2. 不在 sandbox 脚本里写 `~/.hermes/memory.db`。
3. 不 `pip install` 到生产 venv；需要时在 sandbox 建自己的 venv。
4. 不改 `/home/joe/memory-kit`、`~/.dsh`、Hermes profile、运行中的服务。
5. 任何写库实验前后记录 live db 的 mtime / sha256，确认没变。

## 刷新沙箱库

生产库仍在被写入时，不要直接 `cp` WAL 库。用 SQLite backup：

```bash
sqlite3 /home/joe/.hermes/memory.db \
  ".backup '/home/joe/memory-kit-v2/var/memory-sandbox.db'"
```

## 文档规则

- 设计文档只追加，不删除旧内容。
- 旧结论需要修正时，在文末追加「更正」，不覆盖原文。
- 新设计稿：`design-notes/memory-pipeline-v2-2026-10.md`。

## 下一步

先读 `design-notes/memory-pipeline-v2-2026-10.md`。  
在 sandbox 里按 P0 → P1 → P2 → P3 实施；生产版本保持不动。

## 更正（2026-10-20）

上文「本次拷贝快照」中的 `sandbox db sha256: 978dbe2e…` **已失效**，以本节为准。

- **原快照 `978dbe2e1fe4a6300ecf8bf1cd2938487d1ab1103038429ce49f71298a7b6199` 失效原因**：
  `tests/test_smoke.py::test_default_path_is_cwd_memory_db` 把它当成测试产物删除了。
  该用例读取 `MEMORY_DB_PATH` 后，在 `finally` 里 `os.remove(db.db_path)`，而当时的
  `db.db_path` 正指向沙箱库；沙箱库被误删，原快照不可恢复。
- **重建来源**：生产库 SQLite `.backup`（08:35 生成，`66428129…`，120,721,408 bytes，2100 行）。
- **当前指纹**（已含 P0 迁移）：`644501350d7fa338753b2e4694247e233055a565cf0331efb53e3092bd3e51ba`
  / 120,721,408 bytes / 2026-10-20 08:44 +0800 / 2100 行。
- **`test_smoke` 已修**：
  1. 测试内临时 `os.environ.pop("MEMORY_DB_PATH", None)`，不再继承外部沙箱库路径；
  2. 显式断言目标路径是 cwd 下的 `memory.db`；
  3. 删除前再次校验绝对路径，绝不删除 `MEMORY_DB_PATH` 指向的库。

本节只记更正，不改写上文原始快照记录。
