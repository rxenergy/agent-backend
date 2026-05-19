from __future__ import annotations

from typing import Iterable, Protocol

from app.domain.classification import ClassificationResult
from app.domain.interaction import ChatTurn


class ClassifierPort(Protocol):
    backend: str

    async def classify(
        self,
        query_text: str,
        chat_history: Iterable[ChatTurn] = (),
    ) -> ClassificationResult: ...
