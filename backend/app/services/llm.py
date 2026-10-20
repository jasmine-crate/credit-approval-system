"""LLM 审批意见摘要（DeepSeek）。

边界原则（面试表述）：LLM 只有建议权，决定权在审批人。
- 无 API Key / 调用失败 / 超时 → 返回 None，主流程不受影响（可降级模块）
- temperature=0.3，输出模板化，便于审批人快速扫读
"""

from typing import Any

import httpx

from app.core.config import settings
from app.models import Customer, LoanApplication

_TIMEOUT_SECONDS = 10.0

_SYSTEM_PROMPT = (
    "你是智能审批助理。基于给定的申请信息与规则引擎评估结果，"
    "用不超过 120 字生成一段给审批人看的意见摘要。"
    "要求：只陈述事实与风险点，给出关注建议，不得替审批人做决定，"
    "不得使用营销话术。输出纯文本。"
)


def generate_summary(
    application: LoanApplication, customer: Customer, outcome_reasons: dict[str, Any]
) -> str | None:
    if not settings.DEEPSEEK_API_KEY:
        return None
    prompt = (
        f"申请：金额 {application.amount} 元，期限 {application.term_months} 个月，"
        f"用途 {application.purpose}。"
        f"客户：{customer.name}，城市 {customer.city or '未知'}，"
        f"职业 {customer.occupation or '未知'}，"
        f"月收入 {outcome_reasons.get('monthly_income')} 元。"
        f"月供 {outcome_reasons.get('monthly_payment')} 元，"
        f"月供收入比 {outcome_reasons.get('payment_income_ratio')}，"
        f"历史申请 {outcome_reasons.get('prior_applications')} 次。"
        f"规则引擎硬规则：{outcome_reasons.get('hard_rules')}。"
        f"评分明细：{outcome_reasons.get('score_breakdown')}，"
        f"风险等级：{outcome_reasons.get('engine')}。"
    )
    try:
        response = httpx.post(
            f"{settings.DEEPSEEK_BASE_URL}/chat/completions",
            headers={"Authorization": f"Bearer {settings.DEEPSEEK_API_KEY}"},
            json={
                "model": settings.DEEPSEEK_MODEL,
                "messages": [
                    {"role": "system", "content": _SYSTEM_PROMPT},
                    {"role": "user", "content": prompt},
                ],
                "temperature": 0.3,
                "max_tokens": 300,
            },
            timeout=_TIMEOUT_SECONDS,
        )
        response.raise_for_status()
        content = response.json()["choices"][0]["message"]["content"]
        return str(content).strip()
    except Exception:
        # 降级：LLM 不可用不影响审批主流程
        return None
