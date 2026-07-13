"""FastAPI dependencies that pull shared resources off app.state."""

from collections.abc import AsyncIterator
from typing import Annotated

from fastapi import Depends, Request
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from .approvals import ApprovalNotifier
from .evalqueue import EvalQueue
from .github_checks import GitHubStatusReporter
from .k8s import PodLister, PodLogReader
from .killswitch import KillSwitch
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


def get_pod_lister(request: Request) -> PodLister:
    lister: PodLister = request.app.state.pod_lister
    return lister


def get_kill_switch(request: Request) -> KillSwitch:
    kill_switch: KillSwitch = request.app.state.kill_switch
    return kill_switch


def get_approval_notifier(request: Request) -> ApprovalNotifier:
    notifier: ApprovalNotifier = request.app.state.approval_notifier
    return notifier


def get_eval_queue(request: Request) -> EvalQueue:
    eval_queue: EvalQueue = request.app.state.eval_queue
    return eval_queue


def get_github_reporter(request: Request) -> GitHubStatusReporter:
    reporter: GitHubStatusReporter = request.app.state.github_reporter
    return reporter


SessionDep = Annotated[AsyncSession, Depends(get_session)]
LangfuseDep = Annotated[LangfuseClient, Depends(get_langfuse)]
StoreDep = Annotated[BundleStore, Depends(get_store)]
PodLogReaderDep = Annotated[PodLogReader, Depends(get_pod_log_reader)]
PodListerDep = Annotated[PodLister, Depends(get_pod_lister)]
KillSwitchDep = Annotated[KillSwitch, Depends(get_kill_switch)]
ApprovalNotifierDep = Annotated[ApprovalNotifier, Depends(get_approval_notifier)]
EvalQueueDep = Annotated[EvalQueue, Depends(get_eval_queue)]
GitHubReporterDep = Annotated[GitHubStatusReporter, Depends(get_github_reporter)]
