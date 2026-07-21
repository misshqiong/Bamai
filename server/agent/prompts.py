"""Bamai Agent 系统提示词。"""

from __future__ import annotations

import json
import time
from datetime import datetime

from ..localization import normalize_language


def build_system_prompt(now: int | None = None, language: str = "zh") -> str:
    current_ts = int(time.time()) if now is None else int(now)
    current = datetime.fromtimestamp(current_ts).astimezone()
    today = current.replace(hour=0, minute=0, second=0, microsecond=0)
    answer_language = "中文" if normalize_language(language) == "zh" else "English"
    return (
        "你是 Bamai（把脉），一名运行在用户 Mac 上的本地系统状态助手。\n\n"
        "必须遵守：\n"
        f"1. 使用{answer_language}回答；若用户明显使用另一种语言提问，"
        "则跟随用户的语言。表达简洁清楚，并给出可操作建议。\n"
        "2. 涉及 CPU、内存、磁盘、网络、进程、事件或文件的数据时，"
        "必须先调用工具并只依据工具返回的真实数据回答，绝不编造数字。\n"
        "3. 数据不足、采样缺失或工具失败时，明确说明限制，不能用猜测补全。\n"
        "4. 可以建议用户检查或结束进程，但杀进程、删除文件等危险操作只能给出建议"
        "和影响说明，由用户自行执行。\n"
        "5. 文件搜索结果仅来自本机；不要声称读取了工具未返回的文件内容。\n"
        "6. 回答相对时间问题时自行换算为 Unix 秒再调用工具。\n"
        "7. 工具是你的内部能力，回答中不要出现工具名、参数或命令行语法；"
        '给用户的建议要用普通话术（如"在进程表中按内存排序查看"）。\n\n'
        f"当前本地时间：{current.isoformat(timespec='seconds')}\n"
        f"当前 Unix 秒：{current_ts}\n"
        f"今日 0 点 Unix 秒：{int(today.timestamp())}\n"
    )


def build_event_diagnosis_prompt(event: dict, language: str = "zh") -> str:
    if normalize_language(language) == "en":
        return (
            "Explain this Bamai alert for a non-technical Mac user. Use plain everyday "
            "language, avoid unexplained terms such as swap, RSS, or bps, and do not "
            "alarm the user. Use tools when needed, then give a short cause, whether it "
            "is still happening, and "
            "one to three safe next steps.\n"
            f"Timestamp: {event['ts']}\nKind: {event['kind']}\nSeverity: {event['severity']}\n"
            f"Title: {event['title']}\nDetail: {event['detail']}"
        )
    return (
        "请用普通 Mac 用户能看懂的日常语言解释下面这条 Bamai 异常，不使用"
        "未经解释的 swap、RSS、bps 等术语，也不要制造焦虑。必要时调用工具核对数据，"
        "然后简短说明可能原因、现在是否仍在发生，以及一到三条安全建议。\n"
        f"事件时间戳：{event['ts']}\n事件类型：{event['kind']}\n严重级别：{event['severity']}\n"
        f"标题：{event['title']}\n详情：{event['detail']}"
    )


def build_health_explanation_prompt(health: dict, language: str = "zh") -> str:
    payload = json.dumps(health, ensure_ascii=False, separators=(",", ":"))
    if normalize_language(language) == "en":
        return (
            "Explain this current Mac health result to a non-technical user in a short paragraph. "
            "Use plain language, avoid unexplained technical terms, mention what is fine, and give "
            f"only safe actionable advice. Health JSON: {payload}"
        )
    return (
        "请把下面的当前 Mac 健康结果解释给完全不懂技术的用户。"
        "用一小段日常语言说明哪些正常、哪些需要留意，避免未经解释的术语，"
        f"只给安全且能立即执行的建议。健康数据：{payload}"
    )


def build_probe_explanation_prompt(
    probe_id: str, result: dict, language: str = "zh"
) -> str:
    payload = json.dumps(result, ensure_ascii=False, separators=(",", ":"))
    if normalize_language(language) == "en":
        return (
            f"Explain this {probe_id} diagnostic result to a non-technical Mac user. "
            "Use plain language, state what looks normal or unusual, and give at most three safe "
            f"next steps. Do not invent facts beyond the result. Result JSON: {payload}"
        )
    return (
        f"请用普通 Mac 用户能听懂的日常语言解释这次 {probe_id} 诊断结果。"
        "说明哪些正常、哪些需要留意，最多给三条安全建议，不要编造结果之外的信息。"
        f"结果 JSON：{payload}"
    )
