"""Workshop endpoint (tab "Risiko-Index-Werkstatt", risk index 7/7).

GET /workshop/topics/{id}/status -> the five steps with status (open | in_progress | done), ready flag, open tasks with counts,
                                    key figures and what the AI proposes in that step; 404 unknown topic

Everything else the tab does goes through the existing endpoints of tickets 1-6 (directory, risk, agent, metrics, builder,
forecast); every AI proposal there takes effect only after a human click.
"""

import logging

from fastapi import APIRouter, Depends, HTTPException

from backend.app.deps import get_session
from backend.app.risk.service import ensure_pilot, get_topic
from backend.app.workshop.service import workshop_status

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/workshop", tags=["workshop"])


@router.get("/topics/{topic_id}/status")
def status(topic_id: int, session=Depends(get_session)) -> dict:
    ensure_pilot(session)
    try:
        topic = get_topic(session, topic_id)
    except LookupError as exc:
        logger.warning("workshop status refused (404): topic %d unknown", topic_id)
        raise HTTPException(404, str(exc)) from exc
    return workshop_status(session, topic)
