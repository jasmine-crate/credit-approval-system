import uuid
from typing import Any

from fastapi import APIRouter, HTTPException
from sqlmodel import col, func, select

from app.api.deps import CurrentUser, ManagerOrAdmin, SessionDep
from app.models import (
    Customer,
    CustomerCreate,
    CustomerPublic,
    CustomersPublic,
    Message,
)

router = APIRouter(prefix="/customers", tags=["customers"])


def mask_id_card(id_card: str) -> str:
    """证件号脱敏：保留前 4 位与后 3 位，中间用 * 填充"""
    if len(id_card) < 8:
        return "*" * len(id_card)
    return id_card[:4] + "*" * (len(id_card) - 7) + id_card[-3:]


@router.post("/", response_model=CustomerPublic, status_code=201)
def create_customer(
    session: SessionDep, current_user: ManagerOrAdmin, customer_in: CustomerCreate
) -> Any:
    """
    客户建档（客户经理/管理员）。证件号脱敏后存储。
    """
    customer = Customer.model_validate(
        customer_in,
        update={
            "id_card_masked": mask_id_card(customer_in.id_card),
            "created_by": current_user.id,
        },
    )
    session.add(customer)
    session.commit()
    session.refresh(customer)
    return customer


@router.get("/", response_model=CustomersPublic)
def read_customers(
    session: SessionDep,
    current_user: ManagerOrAdmin,
    skip: int = 0,
    limit: int = 100,
) -> Any:
    """
    客户列表：客户经理只看自己建档的客户，管理员看全部。
    """
    statement = select(Customer)
    count_statement = select(func.count()).select_from(Customer)
    if not current_user.is_superuser:
        statement = statement.where(Customer.created_by == current_user.id)
        count_statement = count_statement.where(Customer.created_by == current_user.id)
    count = session.exec(count_statement).one()
    customers = session.exec(statement.offset(skip).limit(limit)).all()
    return CustomersPublic(
        data=[CustomerPublic.model_validate(c) for c in customers], count=count
    )


@router.get("/{id}", response_model=CustomerPublic)
def read_customer(
    session: SessionDep, current_user: CurrentUser, id: uuid.UUID
) -> Any:
    """
    客户详情：建档人、审批人（复核场景）或管理员可见。
    """
    customer = session.get(Customer, id)
    if not customer:
        raise HTTPException(status_code=404, detail="Customer not found")
    allowed = current_user.is_superuser or current_user.role in ("approver", "admin")
    if not allowed and customer.created_by != current_user.id:
        raise HTTPException(status_code=403, detail="Not enough permissions")
    return customer


@router.delete("/{id}")
def delete_customer(
    session: SessionDep, current_user: CurrentUser, id: uuid.UUID
) -> Message:
    """
    删除客户：仅建档人或管理员。有贷款申请的客户禁止删除（合规要求）。
    """
    customer = session.get(Customer, id)
    if not customer:
        raise HTTPException(status_code=404, detail="Customer not found")
    if not current_user.is_superuser and customer.created_by != current_user.id:
        raise HTTPException(status_code=403, detail="Not enough permissions")
    if customer.applications:
        raise HTTPException(
            status_code=400,
            detail="Customer has loan applications and cannot be deleted",
        )
    session.delete(customer)
    session.commit()
    return Message(message="Customer deleted successfully")
