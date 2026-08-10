"""Durable workflow persistence with optimistic concurrency."""

from __future__ import annotations

import asyncio
import json
from abc import ABC, abstractmethod
from collections.abc import Callable
from contextlib import contextmanager
from contextvars import ContextVar
from typing import Any

from azure.core.pipeline.policies import SansIOHTTPPolicy

from .models import WorkflowRecord, utc_now


class WorkflowNotFoundError(KeyError):
    pass


class WorkflowConflictError(RuntimeError):
    pass


Mutation = Callable[[WorkflowRecord], Any]
_foundry_call_id: ContextVar[str | None] = ContextVar("foundry_call_id", default=None)


@contextmanager
def foundry_request_context(call_id: str | None):  # noqa: ANN201
    token = _foundry_call_id.set(call_id)
    try:
        yield
    finally:
        _foundry_call_id.reset(token)


class FoundryCallIdPolicy(SansIOHTTPPolicy):
    def on_request(self, request: Any) -> None:
        if call_id := _foundry_call_id.get():
            request.http_request.headers["x-agent-foundry-call-id"] = call_id


class WorkflowStore(ABC):
    @abstractmethod
    async def create(self, record: WorkflowRecord) -> WorkflowRecord:
        raise NotImplementedError

    @abstractmethod
    async def get(self, workflow_id: str) -> WorkflowRecord:
        raise NotImplementedError

    @abstractmethod
    async def mutate(self, workflow_id: str, expected_version: int, mutation: Mutation) -> WorkflowRecord:
        raise NotImplementedError


def _apply_mutation(record: WorkflowRecord, mutation: Mutation) -> WorkflowRecord:
    mutation(record)
    record.updated_at = utc_now()
    record.version += 1
    return record


class InMemoryWorkflowStore(WorkflowStore):
    def __init__(self) -> None:
        self._records: dict[str, WorkflowRecord] = {}
        self._lock = asyncio.Lock()

    async def create(self, record: WorkflowRecord) -> WorkflowRecord:
        async with self._lock:
            if record.workflow_id in self._records:
                raise WorkflowConflictError(f"workflow already exists: {record.workflow_id}")
            stored = record.model_copy(deep=True)
            self._records[record.workflow_id] = stored
            return stored.model_copy(deep=True)

    async def get(self, workflow_id: str) -> WorkflowRecord:
        async with self._lock:
            record = self._records.get(workflow_id)
            if record is None:
                raise WorkflowNotFoundError(workflow_id)
            return record.model_copy(deep=True)

    async def mutate(self, workflow_id: str, expected_version: int, mutation: Mutation) -> WorkflowRecord:
        async with self._lock:
            current = self._records.get(workflow_id)
            if current is None:
                raise WorkflowNotFoundError(workflow_id)
            if current.version != expected_version:
                raise WorkflowConflictError(
                    f"workflow version changed: expected {expected_version}, found {current.version}"
                )
            updated = _apply_mutation(current.model_copy(deep=True), mutation)
            self._records[workflow_id] = updated
            return updated.model_copy(deep=True)


class BlobWorkflowStore(WorkflowStore):
    """Store each workflow as one JSON blob using blob ETags for compare-and-swap."""

    def __init__(self, account_url: str, credential: Any, container_name: str = "agent-workflows") -> None:
        from azure.storage.blob.aio import BlobServiceClient

        self._service = BlobServiceClient(
            account_url=account_url,
            credential=credential,
            per_call_policies=[FoundryCallIdPolicy()],
        )
        self._container = self._service.get_container_client(container_name)
        self._initialized = False
        self._init_lock = asyncio.Lock()

    async def _ensure_container(self) -> None:
        if self._initialized:
            return
        async with self._init_lock:
            if self._initialized:
                return
            from azure.core.exceptions import ResourceExistsError

            try:
                await self._container.create_container()
            except ResourceExistsError:
                pass
            self._initialized = True

    def _blob(self, workflow_id: str):  # noqa: ANN202
        return self._container.get_blob_client(f"{workflow_id}.json")

    async def create(self, record: WorkflowRecord) -> WorkflowRecord:
        from azure.core.exceptions import ResourceExistsError
        from azure.storage.blob import ContentSettings

        await self._ensure_container()
        try:
            await self._blob(record.workflow_id).upload_blob(
                record.model_dump_json(),
                overwrite=False,
                content_settings=ContentSettings(content_type="application/json"),
            )
        except ResourceExistsError as exc:
            raise WorkflowConflictError(f"workflow already exists: {record.workflow_id}") from exc
        return record.model_copy(deep=True)

    async def _download(self, workflow_id: str) -> tuple[WorkflowRecord, str]:
        from azure.core.exceptions import ResourceNotFoundError

        await self._ensure_container()
        try:
            downloader = await self._blob(workflow_id).download_blob()
            payload = await downloader.readall()
            etag = downloader.properties.etag
        except ResourceNotFoundError as exc:
            raise WorkflowNotFoundError(workflow_id) from exc
        return WorkflowRecord.model_validate_json(payload), etag

    async def get(self, workflow_id: str) -> WorkflowRecord:
        record, _ = await self._download(workflow_id)
        return record

    async def mutate(self, workflow_id: str, expected_version: int, mutation: Mutation) -> WorkflowRecord:
        from azure.core import MatchConditions
        from azure.core.exceptions import ResourceModifiedError
        from azure.storage.blob import ContentSettings

        current, etag = await self._download(workflow_id)
        if current.version != expected_version:
            raise WorkflowConflictError(
                f"workflow version changed: expected {expected_version}, found {current.version}"
            )
        updated = _apply_mutation(current, mutation)
        try:
            await self._blob(workflow_id).upload_blob(
                json.dumps(updated.model_dump(mode="json")),
                overwrite=True,
                etag=etag,
                match_condition=MatchConditions.IfNotModified,
                content_settings=ContentSettings(content_type="application/json"),
            )
        except ResourceModifiedError as exc:
            raise WorkflowConflictError("workflow changed concurrently") from exc
        return updated

    async def close(self) -> None:
        await self._service.close()