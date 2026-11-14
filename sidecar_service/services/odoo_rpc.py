"""JSON-RPC bridge back into Odoo: push verified findings to clearpath.discharge."""
import httpx

from config import settings
from schemas import AuditPacketSnapshot


class OdooRPCError(Exception):
    pass


async def push_findings_to_odoo(snapshot: AuditPacketSnapshot) -> dict:
    """Authenticate, then call clearpath.discharge.apply_mcp_findings on the record."""
    base = settings.odoo_url.rstrip("/")
    async with httpx.AsyncClient(timeout=15) as client:
        auth = await client.post(
            f"{base}/web/session/authenticate",
            json={
                "jsonrpc": "2.0",
                "method": "call",
                "params": {
                    "db": settings.odoo_db,
                    "login": settings.odoo_user,
                    "password": settings.odoo_password,
                },
            },
        )
        auth.raise_for_status()
        if auth.json().get("error") or not auth.json().get("result", {}).get("uid"):
            raise OdooRPCError("Odoo authentication failed")

        call = await client.post(
            f"{base}/web/dataset/call_kw/clearpath.discharge/apply_mcp_findings",
            json={
                "jsonrpc": "2.0",
                "method": "call",
                "params": {
                    "model": "clearpath.discharge",
                    "method": "apply_mcp_findings",
                    "args": [[snapshot.discharge_id]],
                    "kwargs": {
                        "revision_id": snapshot.revision_id,
                        "revision_hash": snapshot.revision_hash,
                        "pharmacy_result": snapshot.pharmacy.model_dump(),
                        "billing_result": snapshot.billing.model_dump(),
                        "insurance_result": snapshot.insurance_output.model_dump()
                        if snapshot.insurance_output else None,
                        "audit_packet": snapshot.model_dump(),
                    },
                },
            },
        )
        call.raise_for_status()
        body = call.json()
        if body.get("error"):
            raise OdooRPCError(str(body["error"].get("data", {}).get("message", body["error"])))
        return body.get("result") or {}
