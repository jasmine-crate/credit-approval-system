import uuid
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Any

from fastapi import APIRouter, HTTPException, Query
from sqlalchemy import update as sa_update
from sqlmodel import Session, col, func, select

from app.api.deps import ApproverOrAdmin, CurrentUser, ManagerOrAdmin, SessionDep
from app.core.config import settings
from app.models import (
    ApprovalRecord,
    ApprovalRecordPublic,
    ApprovalRecordsPublic,
    Customer,
    LoanApplication,
    LoanApplicationCreate,
    LoanApplicationPublic,
    LoanApplicationsPublic,
    RepaymentPlan,
    RepaymentPlanPublic,
    RepaymentPlansPublic,
    RiskAssessment,
    RiskAssessmentPublic,
    RiskAssessmentsPublic,
    User,
)
from app.services import llm, rule_engine

router = APIRouter(prefix="/applications", tags=["applications"])

# 状态机：draft -> submitted -> (自动预审, 第3周接入) -> pending_manual
#   -> approved / rejected -> disbursed -> settled（驳回后重新提交即新建申请）
ALLOWED_DECISIONS = ("approve", "reject", "escalate")
PENDING_REVIEW_STATUSES = ("submitted", "pending_manual")


def transition(
    session: Session, application_id: uuid.UUID, from_status: str, to_status: str
) -> bool:
    """
    乐观锁状态迁移：单条 UPDATE ... WHERE status = 期望值（原子比较并交换），
    两个审批人并发决定同一申请时只有一个能成功。
    """
    result = session.exec(
        sa_update(LoanApplication)
        .where(col(LoanApplication.id) == application_id)
        .where(col(LoanApplication.status) == from_status)
        .values(status=to_status)
    )
    session.commit()
    return result.rowcount == 1


def _add_record(
    session: Session,
    application_id: uuid.UUID,
    action: str,
    operator_id: uuid.UUID | None,
    comment: str | None = None,
) -> None:
    session.add(
        ApprovalRecord(
            application_id=application_id,
            action=action,
            operator_id=operator_id,
            comment=comment,
        )
    )
    session.commit()


def _get_application_or_404(session: Session, id: uuid.UUID) -> LoanApplication:
    application = session.get(LoanApplication, id)
    if not application:
        raise HTTPException(status_code=404, detail="Application not found")
    return application


def _check_view_permission(current_user: User, application: LoanApplication) -> None:
    if current_user.is_superuser or current_user.role in ("approver", "admin"):
        return
    if application.submitted_by != current_user.id:
        raise HTTPException(status_code=403, detail="Not enough permissions")


@router.post("/", response_model=LoanApplicationPublic, status_code=201)
def create_application(
    session: SessionDep,
    current_user: ManagerOrAdmin,
    application_in: LoanApplicationCreate,
) -> Any:
    """
    创建贷款申请（草稿）。校验：客户存在、金额与期限范围。
    submitted_by 记录发起客户经理，作为"经理只看自己的申请"的归属依据。
    """
    customer = session.get(Customer, application_in.customer_id)
    if not customer:
        raise HTTPException(status_code=404, detail="Customer not found")
    if not (
        settings.LOAN_AMOUNT_MIN
        <= float(application_in.amount)
        <= settings.LOAN_AMOUNT_MAX
    ):
        raise HTTPException(
            status_code=400,
            detail=(
                f"Amount must be between {settings.LOAN_AMOUNT_MIN} "
                f"and {settings.LOAN_AMOUNT_MAX}"
            ),
        )
    if not (
        settings.LOAN_TERM_MONTHS_MIN
        <= application_in.term_months
        <= settings.LOAN_TERM_MONTHS_MAX
    ):
        raise HTTPException(
            status_code=400,
            detail=(
                f"Term must be between {settings.LOAN_TERM_MONTHS_MIN} "
                f"and {settings.LOAN_TERM_MONTHS_MAX} months"
            ),
        )
    application = LoanApplication.model_validate(
        application_in, update={"submitted_by": current_user.id, "status": "draft"}
    )
    session.add(application)
    session.commit()
    session.refresh(application)
    return application


@router.get("/", response_model=LoanApplicationsPublic)
def read_applications(
    session: SessionDep,
    current_user: CurrentUser,
    status_filter: str | None = Query(default=None, alias="status"),
    skip: int = 0,
    limit: int = 100,
) -> Any:
    """
    申请列表（按角色过滤）：客户经理只看自己的申请，
    审批人/管理员看全部；status 参数过滤状态。
    """
    statement = select(LoanApplication)
    count_statement = select(func.count()).select_from(LoanApplication)
    if current_user.role == "manager" and not current_user.is_superuser:
        statement = statement.where(LoanApplication.submitted_by == current_user.id)
        count_statement = count_statement.where(
            LoanApplication.submitted_by == current_user.id
        )
    if status_filter:
        statement = statement.where(LoanApplication.status == status_filter)
        count_statement = count_statement.where(LoanApplication.status == status_filter)
    count = session.exec(count_statement).one()
    applications = session.exec(
        statement.order_by(col(LoanApplication.created_at).desc())
        .offset(skip)
        .limit(limit)
    ).all()
    return LoanApplicationsPublic(
        data=[LoanApplicationPublic.model_validate(a) for a in applications],
        count=count,
    )


@router.get("/{id}", response_model=LoanApplicationPublic)
def read_application(
    session: SessionDep, current_user: CurrentUser, id: uuid.UUID
) -> Any:
    """
    申请详情。
    """
    application = _get_application_or_404(session, id)
    _check_view_permission(current_user, application)
    return application


@router.post("/{id}/submit", response_model=LoanApplicationPublic)
def submit_application(
    session: SessionDep, current_user: ManagerOrAdmin, id: uuid.UUID
) -> Any:
    """
    提交申请进入自动预审：draft -> submitted -> auto_review。
    规则引擎 v1 同步评估（硬规则+评分，输出可解释 reasons）：
    - 硬规则拒绝（如月供收入比超标）：auto_review -> rejected
    - 通过 / 转人工（收入缺失、大额）：auto_review -> pending_manual
    评估结果写入 risk_assessments；LLM 摘要（若配置 Key）附在 reasons.llm_advice。
    """
    application = _get_application_or_404(session, id)
    if application.submitted_by != current_user.id and not current_user.is_superuser:
        raise HTTPException(status_code=403, detail="Not the owner of this application")
    if application.status != "draft":
        raise HTTPException(
            status_code=400,
            detail=(
                f"Only draft applications can be submitted "
                f"(current: {application.status})"
            ),
        )
    if not transition(session, id, "draft", "submitted"):
        raise HTTPException(
            status_code=409, detail="Application was modified concurrently"
        )
    _add_record(session, id, action="submit", operator_id=current_user.id)

    # ---- 自动预审 ----
    if not transition(session, id, "submitted", "auto_review"):
        raise HTTPException(
            status_code=409, detail="Application was modified concurrently"
        )
    outcome = rule_engine.evaluate(session, application)

    customer = session.get(Customer, application.customer_id)
    if customer is None:
        raise HTTPException(status_code=404, detail="Customer not found")
    llm_advice = llm.generate_summary(application, customer, outcome.reasons)
    if llm_advice:
        outcome.reasons["llm_advice"] = llm_advice

    session.add(
        RiskAssessment(
            application_id=id,
            engine_version="v1",
            score=outcome.score,
            level=outcome.level,
            reasons=outcome.reasons,
        )
    )
    session.commit()

    if outcome.action == "reject":
        if not transition(session, id, "auto_review", "rejected"):
            raise HTTPException(
                status_code=409, detail="Application was modified concurrently"
            )
        triggered_detail = next(
            (
                r.get("detail")
                for r in outcome.reasons["hard_rules"]
                if r.get("triggered") and r.get("detail")
            ),
            None,
        )
        _add_record(
            session,
            id,
            action="auto_reject",
            operator_id=None,
            comment=(
                f"规则引擎v1: {triggered_detail}"
                if triggered_detail
                else "规则引擎v1: 自动拒绝"
            ),
        )
    else:
        if not transition(session, id, "auto_review", "pending_manual"):
            raise HTTPException(
                status_code=409, detail="Application was modified concurrently"
            )
        if outcome.action == "escalate":
            _add_record(
                session,
                id,
                action="escalate",
                operator_id=None,
                comment="规则引擎v1: 触发转人工规则，升级人工审批",
            )
    return _get_application_or_404(session, id)


@router.post("/{id}/decide", response_model=LoanApplicationPublic)
def decide_application(
    session: SessionDep,
    current_user: ApproverOrAdmin,
    id: uuid.UUID,
    action: str = Query(..., description="approve / reject / escalate"),
    comment: str | None = Query(default=None, max_length=500),
) -> Any:
    """
    审批操作（仅审批人/管理员）：
    - approve: submitted/pending_manual -> approved
    - reject:  submitted/pending_manual -> rejected
    - escalate: submitted -> pending_manual（升级加签）
    """
    if action not in ALLOWED_DECISIONS:
        raise HTTPException(
            status_code=422,
            detail=f"action must be one of: {', '.join(ALLOWED_DECISIONS)}",
        )
    application = _get_application_or_404(session, id)
    if application.status not in PENDING_REVIEW_STATUSES:
        raise HTTPException(
            status_code=400,
            detail=f"Application is not pending review (current: {application.status})",
        )
    if action == "escalate" and application.status == "pending_manual":
        raise HTTPException(status_code=400, detail="Already escalated")
    target = {
        "approve": "approved",
        "reject": "rejected",
        "escalate": "pending_manual",
    }[action]
    if not transition(session, id, application.status, target):
        raise HTTPException(
            status_code=409, detail="Application was decided concurrently"
        )
    session.exec(
        sa_update(LoanApplication)
        .where(col(LoanApplication.id) == id)
        .values(decided_by=current_user.id, decided_at=datetime.now(UTC))
    )
    session.commit()
    _add_record(
        session, id, action=action, operator_id=current_user.id, comment=comment
    )
    return _get_application_or_404(session, id)


@router.get("/{id}/records", response_model=ApprovalRecordsPublic)
def read_application_records(
    session: SessionDep, current_user: CurrentUser, id: uuid.UUID
) -> Any:
    """
    审批记录时间线（审计留痕，按时间正序）。
    """
    application = _get_application_or_404(session, id)
    _check_view_permission(current_user, application)
    records = session.exec(
        select(ApprovalRecord)
        .where(ApprovalRecord.application_id == id)
        .order_by(col(ApprovalRecord.created_at).asc())
    ).all()
    return ApprovalRecordsPublic(
        data=[ApprovalRecordPublic.model_validate(r) for r in records],
        count=len(records),
    )


@router.post("/{id}/schedule", response_model=RepaymentPlansPublic)
def create_repayment_schedule(
    session: SessionDep, current_user: ApproverOrAdmin, id: uuid.UUID
) -> Any:
    """
    放款并生成等额本息还款计划：approved -> disbursed。
    已生成计划的申请不会重复生成（幂等保护）。
    """
    application = _get_application_or_404(session, id)
    existing = session.exec(
        select(RepaymentPlan).where(RepaymentPlan.application_id == id)
    ).all()
    if existing:
        raise HTTPException(status_code=400, detail="Schedule already generated")
    if application.status != "approved":
        raise HTTPException(
            status_code=400,
            detail=(
                f"Only approved applications can be disbursed "
                f"(current: {application.status})"
            ),
        )
    plans = _build_schedule(
        application_id=id,
        principal=float(application.amount),
        months=application.term_months,
        annual_rate=settings.LOAN_ANNUAL_RATE,
    )
    if not transition(session, id, "approved", "disbursed"):
        raise HTTPException(
            status_code=409, detail="Application was modified concurrently"
        )
    session.add_all(plans)
    session.commit()
    _add_record(session, id, action="disburse", operator_id=current_user.id)
    return RepaymentPlansPublic(
        data=[RepaymentPlanPublic.model_validate(p) for p in plans], count=len(plans)
    )


def _build_schedule(
    *, application_id: uuid.UUID, principal: float, months: int, annual_rate: float
) -> list[RepaymentPlan]:
    """
    等额本息：月供 = P*i*(1+i)^n / ((1+i)^n - 1)，i 为月利率。
    每期月供固定，末期用「总还款额 - 已还月供」倒扣，修正累进舍入误差。
    """
    i = annual_rate / 12
    if i == 0:
        monthly = principal / months
    else:
        factor = (1 + i) ** months
        monthly = principal * i * factor / (factor - 1)
    monthly = round(monthly, 2)
    total = round(monthly * months, 2)
    last_payment = round(total - monthly * (months - 1), 2)

    plans: list[RepaymentPlan] = []
    start = datetime.now(UTC).date()
    for period in range(1, months + 1):
        amount = last_payment if period == months else monthly
        plans.append(
            RepaymentPlan(
                application_id=application_id,
                period_no=period,
                due_date=start + timedelta(days=30 * period),
                amount=Decimal(str(amount)),
                status="pending",
            )
        )
    return plans


@router.post("/{id}/settle", response_model=LoanApplicationPublic)
def settle_application(
    session: SessionDep, current_user: ApproverOrAdmin, id: uuid.UUID
) -> Any:
    """
    结清：disbursed -> settled（全部期次还清后由运营触发，此处简化为直接迁移）。
    """
    application = _get_application_or_404(session, id)
    if application.status != "disbursed":
        raise HTTPException(
            status_code=400,
            detail=f"Only disbursed applications can be settled (current: {application.status})",
        )
    if not transition(session, id, "disbursed", "settled"):
        raise HTTPException(
            status_code=409, detail="Application was modified concurrently"
        )
    _add_record(session, id, action="settle", operator_id=current_user.id)
    return _get_application_or_404(session, id)


@router.get("/{id}/risk", response_model=RiskAssessmentsPublic)
def read_application_risk(
    session: SessionDep, current_user: CurrentUser, id: uuid.UUID
) -> Any:
    """
    风险评估详情（规则引擎第 3 周接入，当前返回历史评估记录）。
    """
    application = _get_application_or_404(session, id)
    _check_view_permission(current_user, application)
    assessments = session.exec(
        select(RiskAssessment)
        .where(RiskAssessment.application_id == id)
        .order_by(col(RiskAssessment.created_at).desc())
    ).all()
    return RiskAssessmentsPublic(
        data=[RiskAssessmentPublic.model_validate(a) for a in assessments],
        count=len(assessments),
    )
