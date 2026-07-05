"""FastAPI dependencies that pull shared resources off app.state."""

from collections.abc import AsyncIterator
from typing import Annotated

from fastapi import Depends, Request
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from .k8s import PodLogReader
from .langfuse import LangfuseClient
from .storage import BundleStore


async def get_session(request: Request) -> AsyncIterator[AsyncSession]:
    sessionmaker: async_sessionmaker[AsyncSession] = request.app.state.sessionmaker
    async with sessionmaker() as session:
        yield session


def get_langfuse(request: Request) -> LangfuseClient:
    client: LangfuseClient = request.app.state.langfuse
    return client


def get_store(request: Request) -> BundleStore:
    store: BundleStore = request.app.state.bundle_store
    return store


def get_pod_log_reader(request: Request) -> PodLogReader:
    reader: PodLogReader = request.app.state.pod_log_reader
    return reader


SessionDep = Annotated[AsyncSession, Depends(get_session)]
LangfuseDep = Annotated[LangfuseClient, Depends(get_langfuse)]
StoreDep = Annotated[BundleStore, Depends(get_store)]
PodLogReaderDep = Annotated[PodLogReader, Depends(get_pod_log_reader)]
