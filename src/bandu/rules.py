"""教学智能体确定性规则：意图分类 + 预置引导语（不调 LLM）。"""

from enum import StrEnum


class Intent(StrEnum):
    ASK_ANSWER = "ask_answer"
    OFF_TOPIC = "off_topic"
    UNCLEAR = "unclear"
    NORMAL = "normal"


_ASK_ANSWER_KEYWORDS = ("答案", "直接告诉我", "怎么做", "等于多少", "是多少", "给我做", "写答案")
_OFF_TOPIC_KEYWORDS = ("游戏", "动画", "明星", "偶像", "电视剧", "抖音", "快手")

_CANNED_REPLIES = {
    Intent.ASK_ANSWER: "我们先不急着看答案，你觉得题目里哪个条件还没用上？",
    Intent.OFF_TOPIC: "这个话题挺有意思的，我们先把手头这道题做完，再聊好不好？",
    Intent.UNCLEAR: "我还没听清楚，你能说说题目给了哪些条件，你卡在哪一步吗？",
}


def classify(text: str) -> Intent:
    """按关键词/长度做确定性意图分类。"""
    text = text.strip()
    if not text:
        return Intent.UNCLEAR
    if any(k in text for k in _ASK_ANSWER_KEYWORDS):
        return Intent.ASK_ANSWER
    if any(k in text for k in _OFF_TOPIC_KEYWORDS):
        return Intent.OFF_TOPIC
    if len(text) < 4:
        return Intent.UNCLEAR
    return Intent.NORMAL


def canned_reply(intent: Intent) -> str | None:
    """返回该意图对应的预置引导语，无则返回 None。"""
    return _CANNED_REPLIES.get(intent)
