from __future__ import annotations

from fastapi import APIRouter, Depends

from app.schemas import OperationsResponse
from app.services.auth import require_api_key
from app.services.operations_catalog import list_operation_specs

router = APIRouter(
    prefix="/operations",
    tags=["operations"],
    dependencies=[Depends(require_api_key)],
)


@router.get("", response_model=OperationsResponse)
def list_operations() -> OperationsResponse:
    return OperationsResponse(operations=list_operation_specs())
