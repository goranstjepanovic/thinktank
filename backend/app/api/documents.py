from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import PlainTextResponse
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.engine import get_session
from app.db.models import Document

router = APIRouter(prefix="/ideas/{idea_id}/branches/{branch_id}/documents", tags=["documents"])


class DocumentMeta(BaseModel):
    id: str
    doc_type: str
    file_path: str
    created_at: str

    model_config = {"from_attributes": True}


@router.get("", response_model=list[DocumentMeta])
async def list_documents(idea_id: str, branch_id: str, session: AsyncSession = Depends(get_session)):
    result = await session.execute(
        select(Document)
        .where(Document.idea_id == idea_id, Document.branch_id == branch_id)
        .order_by(Document.doc_type)
    )
    docs = result.scalars().all()
    return [DocumentMeta(id=d.id, doc_type=d.doc_type, file_path=d.file_path,
                         created_at=d.created_at.isoformat()) for d in docs]


@router.get("/{doc_type}")
async def get_document(idea_id: str, branch_id: str, doc_type: str,
                       session: AsyncSession = Depends(get_session)):
    result = await session.execute(
        select(Document).where(
            Document.idea_id == idea_id,
            Document.branch_id == branch_id,
            Document.doc_type == doc_type.upper(),
        )
    )
    doc = result.scalar_one_or_none()
    if not doc:
        raise HTTPException(status_code=404, detail="Document not found")
    try:
        content = open(doc.file_path, encoding="utf-8").read()
    except FileNotFoundError:
        raise HTTPException(status_code=404, detail="Document file missing from disk")
    return {"doc_type": doc.doc_type, "content": content}
