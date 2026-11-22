"""学科知识图谱检索测试：打真库 `graph/cn-math-taxonomy.lbdb`，没有库文件就整文件跳过。"""

from pathlib import Path

import pytest

from bandu.agent import build_agent
from bandu.knowledge import MAX_CHAIN_DEPTH, MAX_LIMIT, KnowledgeGraph

DB = Path(__file__).resolve().parents[1] / "graph" / "cn-math-taxonomy.lbdb"
pytestmark = pytest.mark.skipif(
    not DB.exists(), reason="图库文件不存在（构建流程见 graph/graph-schema.md）"
)


@pytest.fixture(scope="module")
def graph():
    g = KnowledgeGraph(DB)
    yield g
    g.close()


# ---- search_topics ---------------------------------------------------


def test_search_matches_topic_name(graph):
    hits = graph.search_topics("配方")
    assert hits[0]["id"] == "cnm_221"
    assert hits[0]["name"] == "配方法"
    assert hits[0]["subject"] == "math"
    assert hits[0]["stage"] == "初中"


def test_search_respects_subject_filter(graph):
    english = graph.search_topics("字母", subject="english")
    assert english and all(row["subject"] == "english" for row in english)
    math = graph.search_topics("字母", subject="math")
    assert all(row["subject"] == "math" for row in math)


def test_search_falls_back_to_description(graph):
    # 名称里没有这句，只在「整数四则混合运算」的 description 里
    hits = graph.search_topics("括号，再乘除")
    assert hits and hits[0]["id"] == "cnm_p112"


def test_search_resolves_alias_redirects(graph):
    # 旧 id 别名（§4.6）：名称/说明都搜不到时按 Alias 跳转，一个旧点带出多个新点
    hits = graph.search_topics("整数四则运算与运算律")
    assert hits
    assert all(row["matched_alias"] == "整数四则运算与运算律" for row in hits)
    assert {row["id"] for row in hits} >= {"cnm_p033", "cnm_p116"}


def test_search_edge_cases(graph):
    assert graph.search_topics("   ") == []
    with pytest.raises(ValueError):
        graph.search_topics("配方", subject="physics")


# ---- topic_profile ---------------------------------------------------


def test_topic_profile_returns_full_context(graph):
    profile = graph.topic_profile("cnm_221")
    assert profile["name"] == "配方法"
    assert profile["mastery_criteria"]
    assert {p["name"] for p in profile["prerequisites"]} == {"直接开平方法", "完全平方公式"}
    assert all(p["strength"] == "hard" for p in profile["prerequisites"])
    assert profile["standards"][0]["confidence"] == "exact"
    assert "配方" in profile["diagnostic_question"]["prompt"]


def test_topic_profile_unknown_id_is_none(graph):
    assert graph.topic_profile("no_such_topic") is None


def test_topic_profile_covers_english_subject(graph):
    profile = graph.topic_profile("cne_p001")
    assert profile["name"] == "英语字母表"
    assert profile["subject"] == "english"


# ---- prerequisite_chain ----------------------------------------------


def test_prerequisite_chain_orders_by_depth(graph):
    chain = graph.prerequisite_chain("cnm_221", depth=2)
    depths = [row["depth"] for row in chain]
    assert depths == sorted(depths)
    assert max(depths) <= 2
    by_name = {row["name"]: row["depth"] for row in chain}
    assert by_name["直接开平方法"] == 1
    assert by_name["完全平方公式"] == 1
    assert "一元二次方程的概念" in by_name  # 二层先修


def test_prerequisite_chain_clamps_depth(graph):
    chain = graph.prerequisite_chain("cnm_221", depth=99)  # 钳到 MAX_CHAIN_DEPTH，不报错
    assert chain and max(row["depth"] for row in chain) <= MAX_CHAIN_DEPTH


# ---- practice_questions ----------------------------------------------


def test_practice_questions_default_and_order(graph):
    questions = graph.practice_questions("cnm_221")
    assert len(questions) == 3
    assert questions[0]["id"] == "cnm_221_q1"  # 诊断题排最前
    assert all(q["prompt"] and q["answer"] for q in questions)


def test_practice_questions_filters_difficulty(graph):
    advanced = graph.practice_questions("cnm_221", difficulty="advanced")
    assert [q["id"] for q in advanced] == ["cnm_221_q4"]
    with pytest.raises(ValueError):
        graph.practice_questions("cnm_221", difficulty="hell")


def test_practice_questions_clamps_limit(graph):
    questions = graph.practice_questions("cnm_221", limit=99)  # 钳到 MAX_LIMIT
    assert 0 < len(questions) <= MAX_LIMIT


# ---- Agent 工具挂载 ---------------------------------------------------


def test_agent_registers_graph_tools():
    names: set[str] = set()
    for toolset in build_agent().toolsets:
        tools = getattr(toolset, "tools", None)
        if isinstance(tools, dict):
            names.update(tools)
    assert {"search_topics", "topic_profile", "prerequisite_chain", "practice_questions"} <= names
