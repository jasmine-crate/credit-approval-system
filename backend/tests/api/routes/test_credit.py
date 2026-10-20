"""审批业务接口测试：客户建档 / 申请状态机 / 角色权限 / 等额本息"""

import uuid
from collections.abc import Generator

import pytest
from fastapi.testclient import TestClient
from sqlmodel import Session, select

from app import crud
from app.core.config import settings
from app.core.db import engine
from app.models import User, UserCreate

PREFIX = settings.API_V1_STR


def _unique_email(prefix: str) -> str:
    return f"{prefix}-{uuid.uuid4().hex[:8]}@creditdemo.com"


def _ensure_user(email: str, password: str, role: str, full_name: str) -> None:
    """按 email 幂等创建用户并强制设定角色（测试库持久化，需容忍重复运行）"""
    with Session(engine) as session:
        user = session.exec(select(User).where(User.email == email)).first()
        if not user:
            crud.create_user(
                session=session,
                user_create=UserCreate(
                    email=email, password=password, full_name=full_name, role=role
                ),
            )
        else:
            user.role = role
            session.add(user)
            session.commit()


def _login(client: TestClient, email: str, password: str) -> dict[str, str]:
    response = client.post(
        f"{PREFIX}/login/access-token",
        data={"username": email, "password": password},
    )
    assert response.status_code == 200, response.text
    return {"Authorization": f"Bearer {response.json()['access_token']}"}


@pytest.fixture(scope="module")
def manager_headers(client: TestClient) -> dict[str, str]:
    email = _unique_email("t-manager")
    _ensure_user(email, "TestMgr@2026", "manager", "测试经理")
    return _login(client, email, "TestMgr@2026")


@pytest.fixture(scope="module")
def manager2_headers(client: TestClient) -> dict[str, str]:
    email = _unique_email("t-manager2")
    _ensure_user(email, "TestMgr@2026", "manager", "测试经理二号")
    return _login(client, email, "TestMgr@2026")


@pytest.fixture(scope="module")
def approver_headers(client: TestClient) -> dict[str, str]:
    email = _unique_email("t-approver")
    _ensure_user(email, "TestApp@2026", "approver", "测试审批人")
    return _login(client, email, "TestApp@2026")


@pytest.fixture(scope="module")
def customer_id(client: TestClient, manager_headers: dict[str, str]) -> str:
    response = client.post(
        f"{PREFIX}/customers/",
        headers=manager_headers,
        json={
            "name": "王五",
            "id_card": "330102199001011234",
            "phone": "13800000001",
            "city": "杭州",
            "occupation": "工程师",
            "monthly_income": 20000,
        },
    )
    assert response.status_code == 201, response.text
    return response.json()["id"]


def _create_application(
    client: TestClient, manager_headers: dict[str, str], customer_id: str
) -> dict:
    response = client.post(
        f"{PREFIX}/applications/",
        headers=manager_headers,
        json={
            "customer_id": customer_id,
            "amount": 120000,
            "term_months": 24,
            "purpose": "装修贷款",
        },
    )
    assert response.status_code == 201, response.text
    return response.json()


def _submit(
    client: TestClient, manager_headers: dict[str, str], application_id: str
) -> None:
    response = client.post(
        f"{PREFIX}/applications/{application_id}/submit", headers=manager_headers
    )
    assert response.status_code == 200, response.text


# ============================================================
# 客户建档
# ============================================================


def test_create_customer_masks_id_card(
    client: TestClient, manager_headers: dict[str, str], customer_id: str
) -> None:
    response = client.get(
        f"{PREFIX}/customers/{customer_id}", headers=manager_headers
    )
    assert response.status_code == 200
    body = response.json()
    assert body["id_card_masked"].startswith("3301")
    assert body["id_card_masked"].endswith("234")
    assert "*" in body["id_card_masked"]
    assert "199001011234" not in body["id_card_masked"]


def test_create_customer_forbidden_for_approver(
    client: TestClient, approver_headers: dict[str, str]
) -> None:
    response = client.post(
        f"{PREFIX}/customers/",
        headers=approver_headers,
        json={"name": "赵六", "id_card": "110101200001010011", "phone": "13900000000"},
    )
    assert response.status_code == 403


def test_create_customer_requires_auth(client: TestClient) -> None:
    response = client.post(
        f"{PREFIX}/customers/",
        json={"name": "路人", "id_card": "110101200001010011", "phone": "13900000000"},
    )
    assert response.status_code == 401


# ============================================================
# 创建申请：校验客户存在 / 金额与期限范围
# ============================================================


def test_create_application_as_draft(
    client: TestClient, manager_headers: dict[str, str], customer_id: str
) -> None:
    body = _create_application(client, manager_headers, customer_id)
    assert body["status"] == "draft"
    assert body["submitted_by"]


def test_create_application_unknown_customer_404(
    client: TestClient, manager_headers: dict[str, str]
) -> None:
    response = client.post(
        f"{PREFIX}/applications/",
        headers=manager_headers,
        json={
            "customer_id": str(uuid.uuid4()),
            "amount": 120000,
            "term_months": 24,
            "purpose": "x",
        },
    )
    assert response.status_code == 404


def test_create_application_amount_out_of_range(
    client: TestClient, manager_headers: dict[str, str], customer_id: str
) -> None:
    response = client.post(
        f"{PREFIX}/applications/",
        headers=manager_headers,
        json={
            "customer_id": customer_id,
            "amount": settings.LOAN_AMOUNT_MAX + 1,
            "term_months": 24,
            "purpose": "x",
        },
    )
    assert response.status_code == 400


# ============================================================
# 提交：draft -> submitted
# ============================================================


def test_submit_and_double_submit(
    client: TestClient, manager_headers: dict[str, str], customer_id: str
) -> None:
    """提交后进入规则引擎预审：低风险客户应落在待人工（pending_manual）"""
    app = _create_application(client, manager_headers, customer_id)
    _submit(client, manager_headers, app["id"])
    detail = client.get(
        f"{PREFIX}/applications/{app['id']}", headers=manager_headers
    ).json()
    assert detail["status"] == "pending_manual"

    # 重复提交被状态机拒绝
    response = client.post(
        f"{PREFIX}/applications/{app['id']}/submit", headers=manager_headers
    )
    assert response.status_code == 400


def test_submit_forbidden_for_approver(
    client: TestClient,
    manager_headers: dict[str, str],
    approver_headers: dict[str, str],
    customer_id: str,
) -> None:
    app = _create_application(client, manager_headers, customer_id)
    response = client.post(
        f"{PREFIX}/applications/{app['id']}/submit", headers=approver_headers
    )
    # 审批人不是申请归属人，提交被拒
    assert response.status_code == 403


# ============================================================
# 审批决策：角色权限 + 状态迁移
# ============================================================


def test_decide_forbidden_for_manager(
    client: TestClient,
    manager_headers: dict[str, str],
    customer_id: str,
) -> None:
    app = _create_application(client, manager_headers, customer_id)
    _submit(client, manager_headers, app["id"])
    response = client.post(
        f"{PREFIX}/applications/{app['id']}/decide?action=approve",
        headers=manager_headers,
    )
    assert response.status_code == 403


def test_approve_flow_with_record(
    client: TestClient,
    manager_headers: dict[str, str],
    approver_headers: dict[str, str],
    customer_id: str,
) -> None:
    app = _create_application(client, manager_headers, customer_id)
    _submit(client, manager_headers, app["id"])
    response = client.post(
        f"{PREFIX}/applications/{app['id']}/decide?action=approve&comment=ok",
        headers=approver_headers,
    )
    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "approved"
    assert body["decided_by"]

    records = client.get(
        f"{PREFIX}/applications/{app['id']}/records", headers=approver_headers
    ).json()
    assert [r["action"] for r in records["data"]] == ["submit", "approve"]
    assert records["data"][1]["comment"] == "ok"


def test_pending_manual_then_reject(
    client: TestClient,
    manager_headers: dict[str, str],
    approver_headers: dict[str, str],
    customer_id: str,
) -> None:
    """低风险提交后落在待人工；待人工可直接驳回；驳回后不可再决策"""
    app = _create_application(client, manager_headers, customer_id)
    _submit(client, manager_headers, app["id"])
    detail = client.get(
        f"{PREFIX}/applications/{app['id']}", headers=manager_headers
    ).json()
    assert detail["status"] == "pending_manual"

    response = client.post(
        f"{PREFIX}/applications/{app['id']}/decide?action=reject&comment=risk",
        headers=approver_headers,
    )
    assert response.status_code == 200
    assert response.json()["status"] == "rejected"

    response = client.post(
        f"{PREFIX}/applications/{app['id']}/decide?action=approve",
        headers=approver_headers,
    )
    assert response.status_code == 400


def test_decide_invalid_action_422(
    client: TestClient,
    manager_headers: dict[str, str],
    approver_headers: dict[str, str],
    customer_id: str,
) -> None:
    app = _create_application(client, manager_headers, customer_id)
    _submit(client, manager_headers, app["id"])
    response = client.post(
        f"{PREFIX}/applications/{app['id']}/decide?action=maybe",
        headers=approver_headers,
    )
    assert response.status_code == 422


# ============================================================
# 列表的角色过滤：经理只看自己的申请
# ============================================================


def test_list_manager_sees_only_own(
    client: TestClient,
    manager_headers: dict[str, str],
    manager2_headers: dict[str, str],
    customer_id: str,
) -> None:
    app = _create_application(client, manager_headers, customer_id)
    _submit(client, manager_headers, app["id"])

    mine = client.get(f"{PREFIX}/applications/", headers=manager_headers).json()
    others = client.get(f"{PREFIX}/applications/", headers=manager2_headers).json()

    ids_mine = {a["id"] for a in mine["data"]}
    ids_other = {a["id"] for a in others["data"]}
    assert app["id"] in ids_mine
    assert app["id"] not in ids_other


# ============================================================
# 放款：等额本息
# ============================================================


def test_repayment_schedule_math(
    client: TestClient,
    manager_headers: dict[str, str],
    approver_headers: dict[str, str],
    customer_id: str,
) -> None:
    app = _create_application(client, manager_headers, customer_id)
    _submit(client, manager_headers, app["id"])
    client.post(
        f"{PREFIX}/applications/{app['id']}/decide?action=approve",
        headers=approver_headers,
    )
    response = client.post(
        f"{PREFIX}/applications/{app['id']}/schedule", headers=approver_headers
    )
    assert response.status_code == 200
    body = response.json()
    assert body["count"] == 24
    amounts = [float(p["amount"]) for p in body["data"]]
    total = round(sum(amounts), 2)
    # 有利息、月供固定、末期仅修正舍入分差
    assert total > 120000
    assert len({round(a, 2) for a in amounts[:-1]}) == 1
    assert abs(amounts[-1] - amounts[0]) < 0.05
    # 幂等：重复放款被拒绝
    response = client.post(
        f"{PREFIX}/applications/{app['id']}/schedule", headers=approver_headers
    )
    assert response.status_code == 400


def test_schedule_requires_approved_status(
    client: TestClient,
    manager_headers: dict[str, str],
    approver_headers: dict[str, str],
    customer_id: str,
) -> None:
    app = _create_application(client, manager_headers, customer_id)
    response = client.post(
        f"{PREFIX}/applications/{app['id']}/schedule", headers=approver_headers
    )
    assert response.status_code == 400


# ============================================================
# 风险评估端点（规则引擎第 3 周接入前返回空列表）
# ============================================================


def test_risk_endpoint_returns_empty_list(
    client: TestClient,
    manager_headers: dict[str, str],
    approver_headers: dict[str, str],
    customer_id: str,
) -> None:
    app = _create_application(client, manager_headers, customer_id)
    response = client.get(
        f"{PREFIX}/applications/{app['id']}/risk", headers=approver_headers
    )
    assert response.status_code == 200
    assert response.json() == {"data": [], "count": 0}

    response = client.get(
        f"{PREFIX}/applications/{uuid.uuid4()}/risk", headers=approver_headers
    )
    assert response.status_code == 404


# ============================================================
# 规则引擎 v1：硬规则 + 评分 + 可解释 reasons
# ============================================================


def _create_customer(
    client: TestClient, manager_headers: dict[str, str], **overrides
) -> str:
    payload = {
        "name": "测试客户",
        "id_card": "330102199001011234",
        "phone": "13800000002",
        **overrides,
    }
    response = client.post(
        f"{PREFIX}/customers/", headers=manager_headers, json=payload
    )
    assert response.status_code == 201, response.text
    return response.json()["id"]


def test_engine_low_risk_pass(
    client: TestClient,
    manager_headers: dict[str, str],
    approver_headers: dict[str, str],
) -> None:
    """工程师/月入2万/12万24期：月供收入比0.26 → 低风险 → 待人工"""
    cid = _create_customer(
        client, manager_headers, occupation="工程师", monthly_income=20000
    )
    app = _create_application(client, manager_headers, cid)
    _submit(client, manager_headers, app["id"])

    detail = client.get(
        f"{PREFIX}/applications/{app['id']}", headers=approver_headers
    ).json()
    assert detail["status"] == "pending_manual"

    risk = client.get(
        f"{PREFIX}/applications/{app['id']}/risk", headers=approver_headers
    ).json()
    assert risk["count"] == 1
    assessment = risk["data"][0]
    assert assessment["engine_version"] == "v1"
    assert assessment["level"] == "low"
    reasons = assessment["reasons"]
    assert reasons["score_breakdown"]["occupation"] == 0
    assert reasons["prior_applications"] == 0


def test_engine_auto_reject_high_ratio(
    client: TestClient,
    manager_headers: dict[str, str],
    approver_headers: dict[str, str],
) -> None:
    """月入5千申请12万：月供收入比远超0.5 → 自动拒绝并留痕"""
    cid = _create_customer(
        client, manager_headers, occupation="工程师", monthly_income=5000
    )
    app = _create_application(client, manager_headers, cid)
    _submit(client, manager_headers, app["id"])

    detail = client.get(
        f"{PREFIX}/applications/{app['id']}", headers=approver_headers
    ).json()
    assert detail["status"] == "rejected"

    records = client.get(
        f"{PREFIX}/applications/{app['id']}/records", headers=approver_headers
    ).json()
    actions = [r["action"] for r in records["data"]]
    assert "auto_reject" in actions
    auto_reject = next(r for r in records["data"] if r["action"] == "auto_reject")
    assert auto_reject["operator_id"] is None
    assert "规则引擎v1" in auto_reject["comment"]


def test_engine_escalate_income_missing(
    client: TestClient,
    manager_headers: dict[str, str],
    approver_headers: dict[str, str],
) -> None:
    """收入缺失 → 转人工（pending_manual）+ reasons 记录 income_missing"""
    cid = _create_customer(
        client, manager_headers, occupation="自由职业", monthly_income=None
    )
    app = _create_application(client, manager_headers, cid)
    _submit(client, manager_headers, app["id"])

    detail = client.get(
        f"{PREFIX}/applications/{app['id']}", headers=approver_headers
    ).json()
    assert detail["status"] == "pending_manual"

    risk = client.get(
        f"{PREFIX}/applications/{app['id']}/risk", headers=approver_headers
    ).json()
    reasons = risk["data"][0]["reasons"]
    income_rule = next(
        r for r in reasons["hard_rules"] if r["rule"] == "income_missing"
    )
    assert income_rule["triggered"] is True
    assert reasons["payment_income_ratio"] is None


def test_engine_counts_prior_applications(
    client: TestClient,
    manager_headers: dict[str, str],
    approver_headers: dict[str, str],
) -> None:
    """同一客户第 2 笔申请：reasons.prior_applications >= 1 且历史评分增加"""
    cid = _create_customer(
        client, manager_headers, occupation="教师", monthly_income=30000
    )
    first = _create_application(client, manager_headers, cid)
    _submit(client, manager_headers, first["id"])

    second = _create_application(client, manager_headers, cid)
    _submit(client, manager_headers, second["id"])

    risk = client.get(
        f"{PREFIX}/applications/{second['id']}/risk", headers=approver_headers
    ).json()
    reasons = risk["data"][0]["reasons"]
    assert reasons["prior_applications"] >= 1
    assert reasons["score_breakdown"]["history"] >= 15
