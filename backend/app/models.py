import uuid
from datetime import UTC, date, datetime
from decimal import Decimal

from pydantic import EmailStr
from sqlalchemy import JSON, Column, Numeric, Text
from sqlmodel import Field, Relationship, SQLModel


def get_datetime_utc() -> datetime:
    return datetime.now(UTC)


# Shared properties
class UserBase(SQLModel):
    email: EmailStr = Field(unique=True, index=True, max_length=255)
    is_active: bool = True
    is_superuser: bool = False
    full_name: str | None = Field(default=None, max_length=255)
    # 系统角色：manager(客户经理) / approver(审批人) / admin(管理员)
    role: str = Field(default="manager", max_length=16)


# Properties to receive via API on creation
class UserCreate(UserBase):
    password: str = Field(min_length=8, max_length=128)


# Public registration: role is NOT accepted here (new users default to manager)
class UserRegister(SQLModel):
    email: EmailStr = Field(max_length=255)
    password: str = Field(min_length=8, max_length=128)
    full_name: str | None = Field(default=None, max_length=255)


# Properties to receive via API on update, all are optional
class UserUpdate(SQLModel):
    email: EmailStr | None = Field(default=None, max_length=255)
    is_active: bool | None = None
    is_superuser: bool | None = None
    full_name: str | None = Field(default=None, max_length=255)
    role: str | None = Field(default=None, max_length=16)
    password: str | None = Field(default=None, min_length=8, max_length=128)


class UserUpdateMe(SQLModel):
    full_name: str | None = Field(default=None, max_length=255)
    email: EmailStr | None = Field(default=None, max_length=255)


class UpdatePassword(SQLModel):
    current_password: str = Field(min_length=8, max_length=128)
    new_password: str = Field(min_length=8, max_length=128)


# Database model, database table inferred from class name
class User(UserBase, table=True):
    id: uuid.UUID = Field(default_factory=uuid.uuid4, primary_key=True)
    hashed_password: str
    created_at: datetime | None = Field(default_factory=get_datetime_utc)
    items: list[Item] = Relationship(back_populates="owner", cascade_delete=True)


# Properties to return via API, id is always required
class UserPublic(UserBase):
    id: uuid.UUID
    created_at: datetime | None = None


class UsersPublic(SQLModel):
    data: list[UserPublic]
    count: int


# Shared properties
class ItemBase(SQLModel):
    title: str = Field(min_length=1, max_length=255)
    description: str | None = Field(default=None, max_length=255)


# Properties to receive on item creation
class ItemCreate(ItemBase):
    pass


# Properties to receive on item update
class ItemUpdate(SQLModel):
    title: str | None = Field(default=None, min_length=1, max_length=255)
    description: str | None = Field(default=None, max_length=255)


# Database model, database table inferred from class name
class Item(ItemBase, table=True):
    id: uuid.UUID = Field(default_factory=uuid.uuid4, primary_key=True)
    created_at: datetime | None = Field(default_factory=get_datetime_utc)
    owner_id: uuid.UUID = Field(
        foreign_key="user.id", nullable=False, ondelete="CASCADE"
    )
    owner: User | None = Relationship(back_populates="items")


# Properties to return via API, id is always required
class ItemPublic(ItemBase):
    id: uuid.UUID
    owner_id: uuid.UUID
    created_at: datetime | None = None


class ItemsPublic(SQLModel):
    data: list[ItemPublic]
    count: int


# Generic message
class Message(SQLModel):
    message: str


# JSON payload containing access token
class Token(SQLModel):
    access_token: str
    token_type: str = "bearer"


# Contents of JWT token
class TokenPayload(SQLModel):
    sub: str | None = None


class NewPassword(SQLModel):
    token: str
    new_password: str = Field(min_length=8, max_length=128)


# ============================================================
# 审批业务模型（Approval business models）
# ============================================================


# 客户建档
class CustomerBase(SQLModel):
    name: str = Field(index=True, max_length=64)
    id_card_masked: str = Field(max_length=32)  # 脱敏后的证件号
    phone: str = Field(max_length=20)
    city: str | None = Field(default=None, max_length=32)
    occupation: str | None = Field(default=None, max_length=32)
    monthly_income: Decimal | None = Field(
        default=None, sa_type=Numeric(12, 2)
    )  # 缺失时规则引擎转人工


class CustomerCreate(SQLModel):
    """客户建档入参：接收完整证件号，由接口层脱敏后入库"""

    name: str = Field(max_length=64)
    id_card: str = Field(min_length=6, max_length=18)
    phone: str = Field(max_length=20)
    city: str | None = Field(default=None, max_length=32)
    occupation: str | None = Field(default=None, max_length=32)
    monthly_income: Decimal | None = Field(default=None, sa_type=Numeric(12, 2))


class CustomerPublic(CustomerBase):
    id: uuid.UUID
    created_by: uuid.UUID | None = None
    created_at: datetime | None = None


class CustomersPublic(SQLModel):
    data: list[CustomerPublic]
    count: int


class Customer(CustomerBase, table=True):
    __tablename__: str = "customers"

    id: uuid.UUID = Field(default_factory=uuid.uuid4, primary_key=True)
    created_by: uuid.UUID | None = Field(
        default=None, foreign_key="user.id", nullable=True, ondelete="SET NULL"
    )
    created_at: datetime | None = Field(default_factory=get_datetime_utc)

    applications: list["LoanApplication"] = Relationship(
        back_populates="customer", cascade_delete=True
    )


# 贷款申请：状态机
# draft -> submitted -> auto_review -> pending_manual
#   -> approved / rejected -> disbursed -> settled（驳回后可重新提交）
class LoanApplicationBase(SQLModel):
    customer_id: uuid.UUID = Field(foreign_key="customers.id", nullable=False, index=True)
    amount: Decimal = Field(sa_type=Numeric(12, 2))
    term_months: int
    purpose: str = Field(max_length=255)
    status: str = Field(default="draft", max_length=32, index=True)


class LoanApplicationCreate(SQLModel):
    customer_id: uuid.UUID
    amount: Decimal
    term_months: int
    purpose: str = Field(max_length=255)


class LoanApplicationPublic(LoanApplicationBase):
    id: uuid.UUID
    submitted_by: uuid.UUID | None = None
    submitted_at: datetime | None = None
    decided_by: uuid.UUID | None = None
    decided_at: datetime | None = None
    created_at: datetime | None = None


class LoanApplicationsPublic(SQLModel):
    data: list[LoanApplicationPublic]
    count: int


class LoanApplication(LoanApplicationBase, table=True):
    __tablename__: str = "loan_applications"

    id: uuid.UUID = Field(default_factory=uuid.uuid4, primary_key=True)
    submitted_by: uuid.UUID | None = Field(
        default=None, foreign_key="user.id", nullable=True, ondelete="SET NULL"
    )
    submitted_at: datetime | None = None
    decided_by: uuid.UUID | None = Field(
        default=None, foreign_key="user.id", nullable=True, ondelete="SET NULL"
    )
    decided_at: datetime | None = None
    created_at: datetime | None = Field(default_factory=get_datetime_utc)

    customer: Customer | None = Relationship(back_populates="applications")
    approval_records: list["ApprovalRecord"] = Relationship(
        back_populates="application", cascade_delete=True
    )
    repayment_plans: list["RepaymentPlan"] = Relationship(
        back_populates="application", cascade_delete=True
    )
    risk_assessments: list["RiskAssessment"] = Relationship(
        back_populates="application", cascade_delete=True
    )


# 审批留痕（审计核心表）：submit/approve/reject/escalate
class ApprovalRecordBase(SQLModel):
    application_id: uuid.UUID = Field(
        foreign_key="loan_applications.id", nullable=False, index=True
    )
    action: str = Field(max_length=16)
    comment: str | None = Field(default=None, sa_column=Column(Text))


class ApprovalRecordCreate(SQLModel):
    action: str = Field(max_length=16)
    comment: str | None = None


class ApprovalRecordPublic(ApprovalRecordBase):
    id: uuid.UUID
    operator_id: uuid.UUID | None = None
    created_at: datetime | None = None


class ApprovalRecordsPublic(SQLModel):
    data: list[ApprovalRecordPublic]
    count: int


class ApprovalRecord(ApprovalRecordBase, table=True):
    __tablename__: str = "approval_records"

    id: uuid.UUID = Field(default_factory=uuid.uuid4, primary_key=True)
    operator_id: uuid.UUID | None = Field(
        default=None, foreign_key="user.id", nullable=True, ondelete="SET NULL"
    )
    created_at: datetime | None = Field(default_factory=get_datetime_utc)

    application: LoanApplication | None = Relationship(back_populates="approval_records")


# 还款计划（放款时按等额本息生成）
class RepaymentPlanBase(SQLModel):
    application_id: uuid.UUID = Field(
        foreign_key="loan_applications.id", nullable=False, index=True
    )
    period_no: int
    due_date: date
    amount: Decimal = Field(sa_type=Numeric(12, 2))
    status: str = Field(default="pending", max_length=16)


class RepaymentPlanCreate(SQLModel):
    period_no: int
    due_date: date
    amount: Decimal


class RepaymentPlanPublic(RepaymentPlanBase):
    id: uuid.UUID


class RepaymentPlansPublic(SQLModel):
    data: list[RepaymentPlanPublic]
    count: int


class RepaymentPlan(RepaymentPlanBase, table=True):
    __tablename__: str = "repayment_plans"

    id: uuid.UUID = Field(default_factory=uuid.uuid4, primary_key=True)

    application: LoanApplication | None = Relationship(back_populates="repayment_plans")


# 风险评估：规则引擎输出，可解释 reasons 结构化存储
class RiskAssessmentBase(SQLModel):
    application_id: uuid.UUID = Field(
        foreign_key="loan_applications.id", nullable=False, index=True
    )
    engine_version: str = Field(max_length=16)  # 如 "v1"；评分卡上线后插 "v2"
    score: int | None = None
    level: str = Field(max_length=16)  # low / medium / high


class RiskAssessmentCreate(SQLModel):
    engine_version: str = Field(max_length=16)
    score: int | None = None
    level: str = Field(max_length=16)
    reasons: dict | None = None


class RiskAssessmentPublic(RiskAssessmentBase):
    id: uuid.UUID
    reasons: dict | None = Field(default=None, sa_column=Column(JSON))
    created_at: datetime | None = None


class RiskAssessmentsPublic(SQLModel):
    data: list[RiskAssessmentPublic]
    count: int


class RiskAssessment(RiskAssessmentBase, table=True):
    __tablename__: str = "risk_assessments"

    id: uuid.UUID = Field(default_factory=uuid.uuid4, primary_key=True)
    reasons: dict | None = Field(default=None, sa_column=Column(JSON))
    created_at: datetime | None = Field(default_factory=get_datetime_utc)

    application: LoanApplication | None = Relationship(back_populates="risk_assessments")
