"""智能审批管理系统 · Streamlit 运营工作台

登录走 FastAPI JWT（/login/access-token），不自建用户体系。
- 审批人/管理员：待审列表 → 风险详情 + 审批时间线 + 审批操作
- 客户经理：我的申请 + 新建申请 + 提交预审

运行：streamlit run workbench/app.py
"""

from __future__ import annotations

import os

import requests
import streamlit as st

API_BASE = os.environ.get("CREDIT_API_BASE", "http://localhost:8000/api/v1")

st.set_page_config(page_title="智能审批系统", page_icon="🏦", layout="wide")

ROLE_NAMES = {"manager": "客户经理", "approver": "审批人", "admin": "管理员"}
STATUS_NAMES = {
    "draft": "草稿",
    "submitted": "已提交",
    "auto_review": "自动预审中",
    "pending_manual": "待人工审批",
    "approved": "已批准",
    "rejected": "已驳回",
    "disbursed": "已放款",
    "settled": "已结清",
}
LEVEL_STYLE = {"low": "🟢 低风险", "medium": "🟡 中风险", "high": "🔴 高风险"}


def api(method: str, path: str, token: str | None = None, **kwargs) -> dict:
    headers = kwargs.pop("headers", {})
    if token:
        headers["Authorization"] = f"Bearer {token}"
    resp = requests.request(
        method, f"{API_BASE}{path}", headers=headers, timeout=15, **kwargs
    )
    if resp.status_code >= 400:
        try:
            detail = resp.json().get("detail", resp.text)
        except Exception:
            detail = resp.text
        st.error(f"{resp.status_code}: {detail}")
        st.stop()
    return resp.json()


def login_screen() -> None:
    st.title("🏦 智能审批管理系统")
    st.subheader("运营工作台登录")
    email = st.text_input("邮箱", key="login_email", placeholder="user@creditdemo.com")
    password = st.text_input("密码", key="login_password", type="password")
    if st.button("登录", type="primary", use_container_width=True):
        resp = requests.post(
            f"{API_BASE}/login/access-token",
            data={"username": email, "password": password},
            timeout=15,
        )
        if resp.status_code != 200:
            st.error(f"登录失败 [{resp.status_code}]: {resp.text[:200]}")
            st.stop()
        token = resp.json()["access_token"]
        me = api("GET", "/users/me", token=token)
        st.session_state["token"] = token
        st.session_state["me"] = me
        st.rerun()


def logout() -> None:
    st.session_state.clear()


def show_risk_detail(token: str, application_id: str) -> None:
    risk = api("GET", f"/applications/{application_id}/risk", token=token)
    st.subheader("风险评估（规则引擎）")
    if risk["count"] == 0:
        st.info("暂无评估记录")
        return
    for a in risk["data"]:
        st.markdown(f"**引擎 {a['engine_version']}** · {LEVEL_STYLE.get(a['level'], a['level'])} · 评分 {a['score']}")
        reasons = a.get("reasons") or {}
        for rule in reasons.get("hard_rules", []):
            if rule.get("triggered") and rule.get("detail"):
                st.warning(f"⚠️ {rule['detail']}")
            elif rule.get("rule") == "payment_ratio_exceeded":
                st.success("✅ 月供收入比在允许范围内")
        bd = reasons.get("score_breakdown", {})
        col1, col2, col3 = st.columns(3)
        col1.metric("月供收入比评分", bd.get("payment_ratio", "-"))
        col2.metric("职业评分", bd.get("occupation", "-"))
        col3.metric("历史申请评分", bd.get("history", "-"))
        st.caption(
            f"月供 {reasons.get('monthly_payment')} 元 · "
            f"月供收入比 {reasons.get('payment_income_ratio')} · "
            f"历史申请 {reasons.get('prior_applications')} 次"
        )
        if reasons.get("llm_advice"):
            st.info(f"🤖 LLM 审批意见摘要（仅供参考）：{reasons['llm_advice']}")


def show_records(token: str, application_id: str) -> None:
    st.subheader("审批时间线（审计留痕）")
    records = api("GET", f"/applications/{application_id}/records", token=token)
    for r in reversed(records["data"]):
        operator = "系统" if r.get("operator_id") is None else "审批人"
        comment = f" · {r['comment']}" if r.get("comment") else ""
        created = (r.get("created_at") or "")[:19].replace("T", " ")
        st.markdown(f"- `{created}` **{r['action']}**（{operator}）{comment}")


def approver_view(token: str, is_admin: bool) -> None:
    st.header("待人工审批")
    apps = api("GET", "/applications/", token=token, params={"limit": 100})
    pending = [
        a for a in apps["data"] if a["status"] in ("pending_manual", "submitted")
    ]
    if not pending:
        st.success("🎉 当前没有待审申请")
        return
    for a in pending:
        with st.expander(
            f"#{a['id'][:8]} · {a['amount']} 元 / {a['term_months']} 期 · {a['purpose']}"
        ):
            api_get = api("GET", f"/applications/{a['id']}", token=token)
            st.markdown(
                f"状态：**{STATUS_NAMES.get(a['status'], a['status'])}** · "
                f"客户ID：`{a['customer_id'][:8]}…` · "
                f"创建：{(a.get('created_at') or '')[:10]}"
            )
            show_risk_detail(token, a["id"])
            show_records(token, a["id"])
            comment = st.text_input(
                "审批意见（可选）", key=f"comment-{a['id']}"
            )
            col1, col2, col3 = st.columns(3)
            if col1.button("✅ 批准", key=f"ok-{a['id']}", type="primary"):
                api(
                    "POST",
                    f"/applications/{a['id']}/decide",
                    token=token,
                    params={"action": "approve", "comment": comment or None},
                )
                st.rerun()
            if col2.button("❌ 驳回", key=f"rej-{a['id']}"):
                api(
                    "POST",
                    f"/applications/{a['id']}/decide",
                    token=token,
                    params={"action": "reject", "comment": comment or None},
                )
                st.rerun()
            if col3.button("⬆️ 升级加签", key=f"esc-{a['id']}"):
                api(
                    "POST",
                    f"/applications/{a['id']}/decide",
                    token=token,
                    params={"action": "escalate", "comment": comment or None},
                )
                st.rerun()

    st.divider()
    st.header("全部申请")
    for a in apps["data"]:
        st.markdown(
            f"- `#{a['id'][:8]}` {STATUS_NAMES.get(a['status'], a['status'])} · "
            f"{a['amount']} 元 / {a['term_months']} 期 · {a['purpose']}"
        )


def manager_view(token: str) -> None:
    st.header("新建贷款申请")
    customers = api("GET", "/customers/", token=token, params={"limit": 100})
    if customers["count"] == 0:
        st.info("请先为客户建档")
    else:
        options = {
            f"{c['name']}（{c['id_card_masked']}）": c["id"]
            for c in customers["data"]
        }
        choice = st.selectbox("选择客户", list(options.keys()))
        col1, col2 = st.columns(2)
        amount = col1.number_input(
            "金额（元）", min_value=1000.0, max_value=5_000_000.0, value=120000.0, step=1000.0
        )
        term = col2.selectbox("期限（月）", [3, 6, 12, 24, 36, 60], index=3)
        purpose = st.text_input("用途", value="装修贷款")
        if st.button("创建申请", type="primary"):
            created = api(
                "POST",
                "/applications/",
                token=token,
                json={
                    "customer_id": options[choice],
                    "amount": amount,
                    "term_months": term,
                    "purpose": purpose,
                },
            )
            st.success(f"已创建草稿 #{created['id'][:8]}")
            if st.button("🚀 提交自动预审"):
                result = api(
                    "POST", f"/applications/{created['id']}/submit", token=token
                )
                st.info(f"预审完成，当前状态：{STATUS_NAMES[result['status']]}")

    st.divider()
    st.header("我的申请")
    apps = api("GET", "/applications/", token=token, params={"limit": 100})
    for a in apps["data"]:
        st.markdown(
            f"- `#{a['id'][:8]}` {STATUS_NAMES.get(a['status'], a['status'])} · "
            f"{a['amount']} 元 / {a['term_months']} 期 · {a['purpose']}"
        )


def main_screen() -> None:
    me = st.session_state["me"]
    token = st.session_state["token"]
    role = me["role"]

    with st.sidebar:
        st.title("🏦 智能审批系统")
        st.markdown(f"**{me['full_name'] or me['email']}**")
        st.caption(f"角色：{ROLE_NAMES.get(role, role)}")
        st.divider()
        if st.button("退出登录", use_container_width=True):
            logout()
            st.rerun()

    if role == "manager":
        manager_view(token)
    else:
        approver_view(token, is_admin=(role == "admin"))


if "token" not in st.session_state:
    login_screen()
else:
    main_screen()
