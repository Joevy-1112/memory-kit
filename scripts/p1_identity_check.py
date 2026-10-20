#!/usr/bin/env python3
"""P1 回归证明 ①: anchor=False 时与 P0 (f0d3631) 逐字节一致。

做法（2026-10-20）:
  1. `git worktree add --detach <tmp>/p0 <P0_SHA>` 拉出 P0 树；
  2. 同一份 fixture 脚本分别在 P0 树 / 当前树各跑一遍（独立临时库）；
  3. 比较：learn/set/dedupe 的**返回 dict** + 全部表行（时间列归一化后）。

覆盖的路径: 向量快路径合并 / JSON 慢路径 / 实体兜底 / dedupe(0.85/0.92) /
关键事实否决（550 vs 650）/ dry_run / set(derived_from) / 新建条目。

用法:
  ./.venv/bin/python scripts/p1_identity_check.py
  ./.venv/bin/python scripts/p1_identity_check.py --keep-worktree
"""
import argparse
import json
import os
import pathlib
import shutil
import subprocess
import sys
import tempfile

HERE = pathlib.Path(__file__).resolve().parent.parent
P0_SHA = "f0d3631"
TIME_COLS = {
    "created_at", "updated_at", "accessed_at", "merged_at", "stale_since",
    "last_verified_at", "started_at", "ended_at", "last_activity_at",
}
TABLES = ("memories", "merge_events", "entity_links", "episodic")

FIXTURE = r'''
import json, math, os, sqlite3, struct, sys

from memorykit import MemoryDB

ROOT = os.path.realpath(os.environ["MK_ROOT"])
OUT = os.environ["MK_OUT"]
import memorykit
assert os.path.realpath(memorykit.__file__).startswith(ROOT), (
    "导入到了错误的 memorykit: %s (期望在 %s 下)" % (memorykit.__file__, ROOT))

DIM = 64
results = []


def at(cos, dim=DIM):
    cos = max(-1.0, min(1.0, float(cos)))
    return [cos, math.sqrt(max(0.0, 1.0 - cos * cos))] + [0.0] * (dim - 2)


def pack(v):
    return struct.pack(f"{len(v)}f", *v)


class Scripted:
    def __init__(self, mapping):
        self.mapping = {k: list(v) for k, v in mapping.items()}

    def embed(self, text):
        if text in self.mapping:
            return list(self.mapping[text])
        import hashlib
        h = hashlib.sha512((text or "").encode()).digest()
        vals = [b / 255.0 for b in h]
        mean = sum(vals) / len(vals)
        c = [x - mean for x in vals]
        n = math.sqrt(sum(x * x for x in c)) or 1.0
        return [x / n for x in c]

    def is_available(self):
        return True


class Offline:
    def embed(self, text):
        return None

    def is_available(self):
        return True


def insert(db, key, content, vector, with_bin=True, **over):
    f = {"key": key, "content": content, "scope": "permanent",
         "importance": 0.5, "vector": json.dumps(vector)}
    if with_bin:
        f["vector_bin"] = pack(vector)
    f.update(over)
    cols = ", ".join(f.keys())
    db._conn.execute("INSERT INTO memories(%s) VALUES(%s)"
                     % (cols, ",".join("?" * len(f))), tuple(f.values()))
    db._conn.commit()


tmp = os.environ["MK_TMP"]

T1 = "我的猫叫小鸡翅，是只银渐层"
T2 = "部署窗口排在周四凌晨两点"
T3 = "回滚脚本路径在 deploy/rollback.sh"
db = MemoryDB(os.path.join(tmp, "main.db"), embedder=Scripted({
    T1: at(1.0), T2: at(0.88), T3: at(0.84)}))
try:
    results.append(db.learn(T1, source="task", source_session="s1", auto_verify=False))
    results.append(db.learn(T1, source="task", source_session="s2", auto_verify=False))
    results.append(db.learn(T2, source="task", auto_verify=False))
    results.append(db.learn(T3, source="task", anchor=False, auto_verify=False))
    results.append(db.learn("显卡价格550块，显存12G", source="task", auto_verify=False))
    results.append(db.learn("显卡价格650块，显存12G", source="task", auto_verify=False))
    results.append({"set": db.set("general/manual", "手工写入的一条记忆",
                                  derived_from="general/src")})
    results.append(dict(db.deduplicate(threshold=0.92)))
    results.append(dict(db.deduplicate(threshold=0.85, dry_run=True)))
    results.append(dict(db.deduplicate(threshold=0.85)))
finally:
    db.close()

# JSON 慢路径：全库 vector_bin 为 NULL 时才走 JSON 解析
jdb = MemoryDB(os.path.join(tmp, "json.db"), embedder=Scripted({
    "慢路径记忆：JSON 向量回退": at(1.0),
    "慢路径新增：JSON 向量回退": at(0.99)}))
try:
    insert(jdb, "slow/json", "慢路径记忆：JSON 向量回退", at(1.0),
           with_bin=False)
    results.append(jdb.learn("慢路径新增：JSON 向量回退", source="task",
                             auto_verify=False))
finally:
    jdb.close()

ddb = MemoryDB(os.path.join(tmp, "dedupe.db"), embedder=Offline())
try:
    insert(ddb, "d/a", "记忆甲：显卡驱动版本 552.4", at(1.0))
    insert(ddb, "d/b", "记忆乙：桌面壁纸换成深色", at(1.0))
    insert(ddb, "d/c", "记忆丙：另一条低相似记录", at(0.86))
    insert(ddb, "d/e", "预算550块，显示器27寸", at(1.0))
    insert(ddb, "d/f", "预算650块，显示器27寸", at(1.0))
    results.append(dict(ddb.deduplicate(threshold=0.92, dry_run=True)))
    results.append(dict(ddb.deduplicate(threshold=0.92)))
    results.append(dict(ddb.deduplicate(threshold=0.92)))
finally:
    ddb.close()

edb = MemoryDB(os.path.join(tmp, "entity.db"), embedder=Offline())
try:
    ent = "训练深度学习模型时显卡显存不够，预算要加"
    results.append(edb.learn(ent, source="task", auto_verify=False))
    results.append(edb.learn(ent, source="task", auto_verify=False))
    results.append(edb.learn("训练深度学习模型时显卡显存够用，预算不加",
                             source="task", auto_verify=False))
finally:
    edb.close()


def dump(path):
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    out = {}
    for table in ("memories", "merge_events", "entity_links", "episodic"):
        try:
            rows = [dict(r) for r in conn.execute(
                "SELECT * FROM %s ORDER BY rowid" % table).fetchall()]
        except sqlite3.Error:
            rows = None
        for row in rows or []:
            for col in list(row):
                if col in ("created_at", "updated_at", "accessed_at",
                           "merged_at", "stale_since", "last_verified_at",
                           "started_at", "ended_at", "last_activity_at"):
                    row[col] = "<ts>"
        out[table] = rows
    conn.close()
    return out


payload = {
    "memorykit": os.path.realpath(memorykit.__file__),
    "results": results,
    "tables": {name: dump(os.path.join(tmp, name + ".db"))
               for name in ("main", "json", "dedupe", "entity")},
}
with open(OUT, "w", encoding="utf-8") as f:
    json.dump(payload, f, ensure_ascii=False, indent=1, sort_keys=True, default=str)
print(os.path.realpath(memorykit.__file__))
'''


def run(root: pathlib.Path, out: pathlib.Path, workdir: pathlib.Path, py: str):
    fixture = workdir / "fixture.py"
    fixture.write_text(FIXTURE, encoding="utf-8")
    tmp = workdir / ("tmp-" + root.name)
    tmp.mkdir(exist_ok=True)
    env = {k: v for k, v in os.environ.items()
           if not k.startswith("MERGE_SAFETY") and not k.startswith("ANCHOR_GRAY")}
    env.pop("MEMORY_DB_PATH", None)
    env.update({"PYTHONPATH": str(root), "MK_ROOT": str(root),
                "MK_OUT": str(out), "MK_TMP": str(tmp)})
    r = subprocess.run([py, str(fixture)], cwd=str(workdir), env=env,
                       capture_output=True, text=True)
    if r.returncode != 0:
        raise SystemExit("fixture 失败 (%s):\n%s\n%s" % (root, r.stdout, r.stderr))
    return r.stdout.strip()


_EPISODIC_TS = None


def normalize(obj):
    """归一化**时钟派生**字段（不是行为差异）:

      - dedupe 返回的 elapsed_s（墙钟耗时）
      - episodic 的 key（episodic/<sid>_<YYYYmmdd_HHMMSS_mmm>，内含墙钟时刻）

    时间列（created_at 等）已在 fixture dump 时替换成 <ts>。
    """
    import re
    global _EPISODIC_TS
    if _EPISODIC_TS is None:
        _EPISODIC_TS = re.compile(r"^(episodic/.*?)_\d{8}_\d{6}_\d{3}$")
    if isinstance(obj, dict):
        out = {}
        for k, v in obj.items():
            if k == "elapsed_s":
                out[k] = "<elapsed>"
            elif k == "key" and isinstance(v, str) and _EPISODIC_TS.match(v):
                out[k] = _EPISODIC_TS.match(v).group(1) + "_<ts>"
            else:
                out[k] = normalize(v)
        return out
    if isinstance(obj, list):
        return [normalize(x) for x in obj]
    return obj


def diff(a, b, path="$", out=None):
    out = out if out is not None else []
    if len(out) >= 25:
        return out
    if type(a) is not type(b):
        out.append("%s: type %s != %s" % (path, type(a).__name__, type(b).__name__))
        return out
    if isinstance(a, dict):
        for k in sorted(set(a) | set(b)):
            if k not in a:
                out.append("%s.%s: 仅 P1 有" % (path, k))
            elif k not in b:
                out.append("%s.%s: 仅 P0 有" % (path, k))
            else:
                diff(a[k], b[k], "%s.%s" % (path, k), out)
        return out
    if isinstance(a, list):
        if len(a) != len(b):
            out.append("%s: 长度 %d != %d" % (path, len(a), len(b)))
        for i in range(min(len(a), len(b))):
            diff(a[i], b[i], "%s[%d]" % (path, i), out)
        return out
    if a != b:
        out.append("%s: %r != %r" % (path, a, b))
    return out


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--p0-sha", default=P0_SHA)
    ap.add_argument("--keep-worktree", action="store_true")
    args = ap.parse_args()

    work = pathlib.Path(tempfile.mkdtemp(prefix="mk-p1-identity-"))
    worktree = work / "p0"
    print("临时目录:", work)
    subprocess.run(["git", "-C", str(HERE), "worktree", "add", "--detach",
                    str(worktree), args.p0_sha], check=True,
                   capture_output=True, text=True)
    py = str(HERE / ".venv" / "bin" / "python")
    try:
        p0_out, p1_out = work / "p0.json", work / "p1.json"
        p0_file = run(worktree, p0_out, work, py)
        p1_file = run(HERE, p1_out, work, py)
        print("P0 memorykit:", p0_file)
        print("P1 memorykit:", p1_file)
        a = json.loads(p0_out.read_text(encoding="utf-8"))
        b = json.loads(p1_out.read_text(encoding="utf-8"))
        a.pop("memorykit", None)
        b.pop("memorykit", None)
        a, b = normalize(a), normalize(b)
        for name, tbl in a["tables"].items():
            ev = tbl.get("merge_events") or []
            if ev:
                print("  %s.merge_events: %d 行 %s" % (
                    name, len(ev),
                    [(r["keep_key"][:24], r["reason"], r["would_veto"]) for r in ev]))
        diffs = diff(a, b)
        n_rows = sum(len(v or []) for t in a["tables"].values() for v in t.values())
        n_res = len(a["results"])
        print("比较: %d 个返回 dict + %d 行表数据" % (n_res, n_rows))
        print("归一化: 时间列→<ts>; dedupe.elapsed_s→<elapsed>; "
              "episodic.key 内嵌时刻→_<ts>（均为墙钟派生，非行为差异）")
        if diffs:
            print("FAIL: 发现 %d 处差异（最多列 25 处）" % len(diffs))
            for d in diffs:
                print("  -", d)
            return 1
        print("PASS: 返回 dict 全等 + 全表（时间列归一后）零差异")
        return 0
    finally:
        if not args.keep_worktree:
            subprocess.run(["git", "-C", str(HERE), "worktree", "remove",
                            "--force", str(worktree)], capture_output=True)
            shutil.rmtree(work, ignore_errors=True)
        else:
            print("worktree 保留:", worktree)


if __name__ == "__main__":
    sys.exit(main())
