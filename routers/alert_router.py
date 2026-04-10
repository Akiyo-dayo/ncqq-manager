"""
告警管理路由 - 规则 CRUD + 历史查询
"""
import uuid as uuid_mod

from fastapi import APIRouter, HTTPException, Depends
from pydantic import BaseModel
from typing import Optional, Dict

from middleware.auth import require_admin
from services.alert_manager import alert_manager
import services.database as db

router = APIRouter(prefix="/api", tags=["alerts"])


class AlertRuleRequest(BaseModel):
    name: str
    type: str = "container_stop"
    config: Dict = {}
    webhook_url: str = ""


class AlertRuleUpdate(BaseModel):
    name: Optional[str] = None
    enabled: Optional[bool] = None
    config: Optional[Dict] = None
    webhook_url: Optional[str] = None


class AlertSettingsUpdate(BaseModel):
    allow_local_webhook: Optional[bool] = None
    webhook_base_url: Optional[str] = None


# ============ 告警全局设置 ============

@router.get("/alerts/settings")
def get_alert_settings(session: dict = Depends(require_admin)):
    return {
        "status": "ok",
        "allow_local_webhook": db.get_setting("allow_local_webhook", False),
        "webhook_base_url": db.get_setting("webhook_base_url", ""),
    }


@router.put("/alerts/settings")
def update_alert_settings(req: AlertSettingsUpdate, session: dict = Depends(require_admin)):
    if req.allow_local_webhook is not None:
        db.set_setting("allow_local_webhook", req.allow_local_webhook)
    if req.webhook_base_url is not None:
        db.set_setting("webhook_base_url", req.webhook_base_url.rstrip("/"))
    return {"status": "ok"}


# ============ 告警规则 CRUD ============

@router.get("/alerts/rules")
def list_alert_rules(session: dict = Depends(require_admin)):
    rules = alert_manager.list_rules()
    return {"status": "ok", "rules": rules}


@router.post("/alerts/rules")
def create_alert_rule(req: AlertRuleRequest, session: dict = Depends(require_admin)):
    rule_id = "alert-" + uuid_mod.uuid4().hex[:8]
    try:
        success = alert_manager.create_rule(
            rule_id, req.name, req.type, req.config, req.webhook_url,
        )
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    if not success:
        raise HTTPException(status_code=500, detail="Failed to create alert rule")
    return {"status": "ok", "rule_id": rule_id}


@router.put("/alerts/rules/{rule_id}")
def update_alert_rule(
    rule_id: str, req: AlertRuleUpdate,
    session: dict = Depends(require_admin),
):
    try:
        alert_manager.update_rule(
            rule_id, req.name, req.enabled, req.config, req.webhook_url,
        )
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    return {"status": "ok"}


@router.delete("/alerts/rules/{rule_id}")
def delete_alert_rule(rule_id: str, session: dict = Depends(require_admin)):
    alert_manager.delete_rule(rule_id)
    return {"status": "ok"}


@router.get("/alerts/history")
def get_alert_history(limit: int = 50, session: dict = Depends(require_admin)):
    if limit < 1 or limit > 200:
        limit = 50
    history = alert_manager.get_history(limit)
    return {"status": "ok", "history": history}

