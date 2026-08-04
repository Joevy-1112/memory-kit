"""验证 learn() 混合维度向量不再崩溃 (2026-08-04 修复)"""
import sys, os, tempfile, json
sys.path.insert(0, '/home/joe/memory-kit')
from memorykit import MemoryDB

fd, path = tempfile.mkstemp(suffix=".db")
os.close(fd); os.remove(path)

db = MemoryDB(path)

# 伪造嵌入：1024d
db._embed = lambda text: [0.01] * 1024
db.learn("这是一个旧的测试记忆，用来验证维度问题", source="task")

# 换嵌入模型：2048d
db._embed = lambda text: [0.02] * 2048
db.learn("这是另一条测试记忆，不同的维度", source="task")

# 再次用 2048d 学习——之前会在这里崩（批量拼接混合维度）
db._embed = lambda text: [0.03] * 2048
r = db.learn("第三条记忆，验证混合维度下的去重不崩溃", source="task")

print("learn() 返回:", json.dumps(r, ensure_ascii=False)[:200] if isinstance(r, dict) else r)
print("✅ 混合维度 learn() 未崩溃")
db.close()
