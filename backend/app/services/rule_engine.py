"""规则引擎 v1：硬规则 + 加权评分，输出可解释的 reasons。

设计原则（面试表述）：
- 纯 Python 规则，不用任何模型：可解释、稳定、合规
- 所有阈值显式配置（config.py），可审计
- 输出结构化 reasons_json，记录"为什么"，与 LLM 黑箱互补
- 接口化：evaluate(session, application) -> RuleOutcome，
  未来评分卡模型作为 engine_version="v2" 平替，调用方不变
"""

from dataclasses import dataclass, field
from decimal import Decimal

from sqlmodel import Session, func, select

from app.core.config import settings
from app.models import Customer, LoanApplication

# 稳定职业白名单（评分加权用，v1 简化为字符串匹配）
STABLE_OCCUPATIONS = {"工程师", "教师", "医生", "公务员", "护士", "律师"}


@dataclass
class RuleOutcome:
    action: str  # pass / reject / escalate
    score: int  # 风险分 0-100，越高越危险
    level: str  # low / medium / high
    reasons: dict = field(default_factory=dict)


def monthly_payment(principal: float, months: int, annual_rate: float) -> Decimal:
    """等额本息月供（与放款计划同一公式，保证口径一致）"""
    i = annual_rate / 12
    if i == 0:
        return Decimal(str(round(principal / months, 2)))
    factor = (1 + i) ** months
    return Decimal(str(round(principal * i * factor / (factor - 1), 2)))


def _score_payment_ratio(ratio: float) -> int:
    if ratio <= 0.2:
        return 0
    if ratio <= 0.3:
        return 15
    if ratio <= 0.4:
        return 30
    return 45


def _score_occupation(occupation: str | None) -> int:
    if occupation and occupation in STABLE_OCCUPATIONS:
        return 0
    if occupation:
        return 20
    return 30


def _score_history(count: int) -> int:
    if count == 0:
        return 0
    if count == 1:
        return 15
    if count == 2:
        return 30
    return 50


def evaluate(session: Session, application: LoanApplication) -> RuleOutcome:
    """
    评估一次申请。动作优先级：reject > escalate > pass。
    评分与硬规则独立计算：即便硬规则触发，评分明细仍然完整输出（可解释性）。
    """
    customer = session.get(Customer, application.customer_id)
    payment = monthly_payment(
        float(application.amount),
        application.term_months,
        settings.LOAN_ANNUAL_RATE,
    )
    income = (
        float(customer.monthly_income) if customer.monthly_income is not None else None
    )
    ratio = round(payment / Decimal(str(income)), 2) if income else None
    prior = (
        session.exec(
            select(func.count())
            .select_from(LoanApplication)
            .where(LoanApplication.customer_id == application.customer_id)
            .where(LoanApplication.id != application.id)
        ).one()
    )

    hard_rules = []

    # 硬规则 1：收入缺失 → 转人工
    if income is None:
        hard_rules.append(
            {
                "rule": "income_missing",
                "triggered": True,
                "detail": "客户月收入缺失，需人工核实收入证明",
            }
        )
    # 硬规则 2：月收入 < 月供2倍（即月供收入比 > 0.5）→ 拒
    elif ratio is not None and ratio > settings.RULE_PAYMENT_RATIO_REJECT:
        hard_rules.append(
            {
                "rule": "payment_ratio_exceeded",
                "triggered": True,
                "detail": (
                    f"月供 {payment} 元占月收入 {income} 元的 "
                    f"{float(ratio):.0%}，超过 {settings.RULE_PAYMENT_RATIO_REJECT:.0%} 上限"
                ),
            }
        )
    else:
        hard_rules.append({"rule": "payment_ratio_exceeded", "triggered": False})
    # 硬规则 3：大额 → 升级加签
    if float(application.amount) > settings.RULE_ESCALATE_AMOUNT:
        hard_rules.append(
            {
                "rule": "large_amount",
                "triggered": True,
                "detail": (
                    f"申请金额 {application.amount} 元超过大额审批阈值 "
                    f"{settings.RULE_ESCALATE_AMOUNT:.0f} 元，需升级加签"
                ),
            }
        )
    else:
        hard_rules.append({"rule": "large_amount", "triggered": False})

    # 加权评分
    s_ratio = _score_payment_ratio(float(ratio)) if ratio is not None else 45
    s_occupation = _score_occupation(customer.occupation)
    s_history = _score_history(int(prior))
    score = s_ratio + s_occupation + s_history
    if score >= settings.RULE_SCORE_HIGH:
        level = "high"
    elif score >= settings.RULE_SCORE_MEDIUM:
        level = "medium"
    else:
        level = "low"

    # 动作合成：reject > escalate > pass
    if any(r["rule"] == "payment_ratio_exceeded" and r["triggered"] for r in hard_rules):
        action = "reject"
    elif income is None or any(
        r["rule"] == "large_amount" and r["triggered"] for r in hard_rules
    ):
        action = "escalate"
    else:
        action = "pass"

    return RuleOutcome(
        action=action,
        score=score,
        level=level,
        reasons={
            "hard_rules": hard_rules,
            "score_breakdown": {
                "payment_ratio": s_ratio,
                "occupation": s_occupation,
                "history": s_history,
            },
            "monthly_payment": str(payment),
            "payment_income_ratio": float(ratio) if ratio is not None else None,
            "monthly_income": income,
            "prior_applications": int(prior),
            "engine": "v1",
        },
    )
