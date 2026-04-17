from fastapi import APIRouter

from app.api import branches, documents, failure_analyses, ideas, model_calls, phase2, phase3

api_router = APIRouter(prefix="/api/v1")
api_router.include_router(ideas.router)
api_router.include_router(branches.router)
api_router.include_router(documents.router)
api_router.include_router(model_calls.router)
api_router.include_router(failure_analyses.router)
api_router.include_router(phase2.router)
api_router.include_router(phase3.router)
