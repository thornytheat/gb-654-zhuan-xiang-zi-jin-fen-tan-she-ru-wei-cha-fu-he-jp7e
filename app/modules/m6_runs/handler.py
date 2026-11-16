from urllib.parse import quote

from fastapi import APIRouter, Body, Request
from fastapi.responses import Response

from . import service
router = APIRouter(tags=["M6-批次复核"])


@router.get("/runs")
def page(request: Request):
    store = request.app.state.store
    return request.app.state.templates.TemplateResponse(
        request, "m6_runs.html",
        {"title": "批次复核", "data": service.execute(), "batches": store.list_batches()})


# -- 遗留样例流程（保持向后兼容） -------------------------------------------
@router.get("/api/runs")
def runs(request: Request):
    service.execute({"store": request.app.state.store})
    return {"runs": request.app.state.store.all()}


@router.get("/api/export/allocations.csv")
def export(request: Request):
    data = service.execute({"store": request.app.state.store})
    return Response(data["csv"], media_type="text/csv",
                    headers={"Content-Disposition": "attachment; filename=evidence.csv"})


# -- 批次拨款更正台账 -------------------------------------------------------
@router.get("/api/batches")
def list_batches(request: Request):
    return service.list_batches(request.app.state.store)


@router.post("/api/batches")
def create_batch(request: Request, payload: dict = Body(default={})):
    return service.create_batch(request.app.state.store, payload or {})


@router.get("/api/batches/{batch_id}")
def view_batch(request: Request, batch_id: str):
    return service.view_batch(request.app.state.store, batch_id)


@router.post("/api/batches/{batch_id}/disbursements")
def register_disbursement(request: Request, batch_id: str, payload: dict = Body(default={})):
    payload = {"batch_id": batch_id, **(payload or {})}
    return service.register_disbursement(request.app.state.store, payload)


@router.post("/api/batches/{batch_id}/recoveries")
def register_recovery(request: Request, batch_id: str, payload: dict = Body(default={})):
    payload = {"batch_id": batch_id, **(payload or {})}
    return service.register_recovery(request.app.state.store, payload)


@router.post("/api/batches/{batch_id}/corrections")
def submit_correction(request: Request, batch_id: str, payload: dict = Body(default={})):
    payload = {"batch_id": batch_id, **(payload or {})}
    return service.submit_correction(request.app.state.store, payload)


@router.post("/api/corrections/{correction_id}/publish")
def publish_correction(request: Request, correction_id: str, payload: dict = Body(default={})):
    payload = {"correction_id": correction_id, **(payload or {})}
    return service.publish_correction(request.app.state.store, payload)


@router.get("/api/batches/{batch_id}/export.csv")
def export_batch(request: Request, batch_id: str):
    result = service.export_batch(request.app.state.store, batch_id)
    filename = quote(result["filename"])
    return Response(
        result["csv"], media_type="text/csv; charset=utf-8",
        headers={"Content-Disposition": f"attachment; filename={filename}; filename*=UTF-8''{filename}"})
