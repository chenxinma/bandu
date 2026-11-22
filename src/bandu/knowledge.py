"""学科知识图谱检索：只读访问 LadybugDB 图谱，运行时给教学 Agent 供学科资料。

图谱模式见 `graph/graph-schema.md`：`Topic` 知识点挂在 `Domain` / `Book` 下，
`REQUIRES` 是先修、`ALIGNS_TO` 对齐课标、`TESTS` 挂题库；数学（math）与英语（english）
两个学科各自成图，任何关系都不跨学科。

约定：
- 图谱只读（`read_only=True`），运行时绝不写入；
- 连接懒加载、进程内复用；`Connection` 非线程安全，查询用锁串行；
- 学生的话不拼进查询串：字符串走参数化（`$kw` / `$id`）；
  `LIMIT` 与变长路径深度这类不支持参数化的位置，在 Python 侧 `_clamp` 钳好再内联整数；
- 图里空值是空串不是 NULL（`verbatim` 除外，本层不用它），判空一律 `= ''`。
"""

from __future__ import annotations

import os
import threading
from pathlib import Path
from typing import Any

import ladybug as lb

_DEFAULT_DB = Path(__file__).resolve().parents[2] / "graph" / "cn-math-taxonomy.lbdb"
SUBJECTS = ("", "math", "english")  # "" = 不过滤，两个学科都搜
DIFFICULTIES = ("", "basic", "standard", "advanced")
MAX_CHAIN_DEPTH = 6  # 先修链最深往回走几层（图里最长链也就 5 上下）
MAX_LIMIT = 10  # 单次检索返回条数上限，工具结果要塞进 LLM 上下文，别放太宽

_TOPIC_COLUMNS = """t.id AS id, t.name AS name, t.subject AS subject, t.grade AS grade,
       t.stage AS stage, t.domain AS domain, t.chapter_title AS chapter_title,
       t.description AS description"""


def _clamp(value: int, low: int, high: int) -> int:
    return max(low, min(high, value))


class KnowledgeGraph:
    """图谱库的只读句柄。

    `path` 缺省取环境变量 `BANDU_GRAPH_DB`，再缺省取仓库内 `graph/cn-math-taxonomy.lbdb`。
    """

    def __init__(self, path: str | Path | None = None) -> None:
        self._path = Path(path or os.environ.get("BANDU_GRAPH_DB") or _DEFAULT_DB)
        self._db: lb.Database | None = None
        self._conn: lb.Connection | None = None
        self._lock = threading.Lock()  # Connection 非线程安全：查询串行化

    def close(self) -> None:
        """释放连接与库句柄（测试与进程收尾用）。"""
        with self._lock:
            if self._conn is not None:
                self._conn.close()
                self._conn = None
            if self._db is not None:
                self._db.close()
                self._db = None

    # ---- 检索能力（对应 Agent 工具）------------------------------------

    def search_topics(
        self, keyword: str, subject: str = "", limit: int = 5
    ) -> list[dict[str, Any]]:
        """模糊检索知识点：名称命中优先，其次说明，最后旧 id 别名跳转（§4.6）。

        Args:
            keyword: 知识点关键词，如「配方法」「一般现在时」。
            subject: 学科过滤：`math` / `english`，留空两个学科都搜。
            limit: 最多返回几条（钳在 1..MAX_LIMIT）。
        """
        if subject not in SUBJECTS:
            raise ValueError(f"subject 只能是 {SUBJECTS!r}，收到 {subject!r}")
        kw = keyword.strip()
        if not kw:
            return []
        limit = _clamp(limit, 1, MAX_LIMIT)
        rows = self._rows(self._search_sql("t.name", limit), kw=kw, subject=subject)
        if not rows:
            rows = self._rows(self._search_sql("t.description", limit), kw=kw, subject=subject)
        if rows:
            return rows
        # 一个旧 id 可能跳到多个新点，按 REDIRECTS_TO.position 排序取先
        return self._rows(
            f"""MATCH (a:Alias)-[r:REDIRECTS_TO]->(t:Topic)
WHERE a.name CONTAINS $kw AND ($subject = '' OR t.subject = $subject)
RETURN {_TOPIC_COLUMNS}, a.name AS matched_alias
ORDER BY r.position
LIMIT {limit}""",
            kw=kw,
            subject=subject,
        )

    def topic_profile(self, topic_id: str) -> dict[str, Any] | None:
        """知识点全景：基本信息 + 直接先修/后继 + 课标对齐 + 诊断题。找不到返回 None。"""
        rows = self._rows(
            """MATCH (t:Topic {id: $id})
RETURN t.id AS id, t.name AS name, t.subject AS subject, t.grade AS grade,
       t.semester AS semester, t.stage AS stage, t.domain AS domain, t.book AS book,
       t.chapter AS chapter, t.type AS type, t.description AS description,
       t.mastery_criteria AS mastery_criteria""",
            id=topic_id,
        )
        if not rows:
            return None
        topic: dict[str, Any] = rows[0]
        topic["prerequisites"] = self._rows(
            """MATCH (t:Topic {id: $id})-[r:REQUIRES]->(p:Topic)
RETURN p.id AS id, p.name AS name, r.strength AS strength, r.reason AS reason""",
            id=topic_id,
        )
        topic["dependents"] = self._rows(
            """MATCH (d:Topic)-[r:REQUIRES]->(t:Topic {id: $id})
RETURN d.id AS id, d.name AS name, r.strength AS strength""",
            id=topic_id,
        )
        topic["standards"] = self._rows(
            """MATCH (t:Topic {id: $id})-[a:ALIGNS_TO]->(s:Standard)
RETURN s.id AS id, s.text AS text, a.confidence AS confidence""",
            id=topic_id,
        )
        diagnostic = self._rows(
            """MATCH (q:Question)-[r:TESTS]->(t:Topic {id: $id})
WHERE r.is_primary
RETURN q.prompt AS prompt, q.answer AS answer, q.type AS type""",
            id=topic_id,
        )
        topic["diagnostic_question"] = diagnostic[0] if diagnostic else None
        return topic

    def prerequisite_chain(self, topic_id: str, depth: int = 3) -> list[dict[str, Any]]:
        """沿 `REQUIRES` 往回取先修知识点，`depth` 是最早先修深度（同点取最小）。

        变长路径按路径重复返回同一节点（§9），这里用 `min(length(p))` 聚合成一层一次。
        """
        depth = _clamp(depth, 1, MAX_CHAIN_DEPTH)
        return self._rows(
            f"""MATCH p = (t:Topic {{id: $id}})-[:REQUIRES*1..{depth}]->(a:Topic)
RETURN a.id AS id, a.name AS name, a.stage AS stage, a.grade AS grade,
       a.description AS description, min(length(p)) AS depth
ORDER BY depth, a.grade""",
            id=topic_id,
        )

    def practice_questions(
        self, topic_id: str, difficulty: str = "", limit: int = 3
    ) -> list[dict[str, Any]]:
        """取某知识点的练习题（题干 + 答案 + 解析），可按难度过滤。

        题目 id 形如 `知识点id_q序号`，按 id 排序时 `_q1`（诊断题）永远在最前。
        """
        if difficulty not in DIFFICULTIES:
            raise ValueError(f"difficulty 只能是 {DIFFICULTIES!r}，收到 {difficulty!r}")
        limit = _clamp(limit, 1, MAX_LIMIT)
        return self._rows(
            f"""MATCH (q:Question)-[:TESTS]->(t:Topic {{id: $id}})
WHERE $difficulty = '' OR q.difficulty = $difficulty
RETURN q.id AS id, q.type AS type, q.difficulty AS difficulty,
       q.prompt AS prompt, q.answer AS answer, q.explanation AS explanation
ORDER BY q.id
LIMIT {limit}""",
            id=topic_id,
            difficulty=difficulty,
        )

    # ---- 内部 ----------------------------------------------------------

    def _search_sql(self, column: str, limit: int) -> str:
        """按 `column`（name / description）构造知识点检索查询，返回条数用钳好的整数。"""
        return f"""MATCH (t:Topic)
WHERE {column} CONTAINS $kw AND ($subject = '' OR t.subject = $subject)
RETURN {_TOPIC_COLUMNS}
ORDER BY t.grade, t.id
LIMIT {limit}"""

    def _rows(self, cypher: str, **params: Any) -> list[dict[str, Any]]:
        """执行查询并把结果整成 `list[dict]`（列名取查询里的 RETURN 别名）。"""
        with self._lock:
            if self._conn is None:
                self._db = lb.Database(str(self._path), read_only=True)
                self._conn = lb.Connection(self._db)
            result = self._conn.execute(cypher, params)
            columns = result.get_column_names()
            rows = result.get_all()
            result.close()
        return [dict(zip(columns, row, strict=True)) for row in rows]


_graph: KnowledgeGraph | None = None
_graph_lock = threading.Lock()


def get_graph() -> KnowledgeGraph:
    """进程级共享的图谱句柄（Agent 工具用；测试自己 new `KnowledgeGraph`）。"""
    global _graph
    with _graph_lock:
        if _graph is None:
            _graph = KnowledgeGraph()
        return _graph
