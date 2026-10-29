
from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Any

from langgraph.graph import END, START, StateGraph

from ..settings import Settings
from .nodes import InvoiceNodes
from .state import InvoiceState

if TYPE_CHECKING:
    from ..runtime import Runtime

logger = logging.getLogger(__name__)

TERMINAL_STATES = {"skipped", "failed"}

_checkpointer = None
_checkpointer_pool = None


def build_checkpointer(settings: Settings):
    """LangGraph's PostgreSQL checkpointer, confined to the langgraph schema.

    A dedicated pool is used because the application pool resolves `audit`
    first on its search_path, and the checkpointer creates its tables in
    whichever schema comes first.
    """
    global _checkpointer, _checkpointer_pool
    if _checkpointer is not None:
        return _checkpointer

    from langgraph.checkpoint.postgres import PostgresSaver
    from psycopg.rows import dict_row
    from psycopg_pool import ConnectionPool

    kwargs = settings.db_connection_kwargs()
    kwargs["options"] = (
        f"-c statement_timeout={settings.db_statement_timeout_ms} -c search_path=langgraph"
    )

    _checkpointer_pool = ConnectionPool(
        open=False,
        min_size=1,
        max_size=4,
        timeout=10,
        kwargs={**kwargs, "autocommit": True, "row_factory": dict_row},
    )
    _checkpointer_pool.open(wait=True, timeout=30)

    saver = PostgresSaver(_checkpointer_pool)
    saver.setup()

    _checkpointer = saver
    logger.info("langgraph checkpointer ready", extra={"schema": "langgraph"})
    return _checkpointer


def close_checkpointer() -> None:
    global _checkpointer, _checkpointer_pool
    if _checkpointer_pool is not None:
        _checkpointer_pool.close()
    _checkpointer = None
    _checkpointer_pool = None


def _route(state: InvoiceState) -> str:
    return "stop" if state.get("status") in TERMINAL_STATES else "continue"


def build_invoice_graph(runtime: "Runtime"):
    nodes = InvoiceNodes(runtime)

    graph = StateGraph(InvoiceState)
    graph.add_node("monitor", nodes.monitor)
    graph.add_node("extractor", nodes.extractor)
    graph.add_node("translator", nodes.translator)
    graph.add_node("data_validator", nodes.data_validator)
    graph.add_node("business_validator", nodes.business_validator)
    graph.add_node("reporter", nodes.reporter)
    graph.add_node("indexer", nodes.indexer)

    graph.add_edge(START, "monitor")
    graph.add_conditional_edges(
        "monitor", _route, {"continue": "extractor", "stop": END}
    )
    graph.add_conditional_edges(
        "extractor", _route, {"continue": "translator", "stop": END}
    )
    graph.add_edge("translator", "data_validator")
    graph.add_edge("data_validator", "business_validator")
    graph.add_conditional_edges(
        "business_validator", _route, {"continue": "reporter", "stop": END}
    )
    graph.add_edge("reporter", "indexer")
    graph.add_edge("indexer", END)

    compiled = graph.compile(checkpointer=build_checkpointer(runtime.settings))
    logger.info("invoice workflow compiled")
    return compiled


def run_invoice(runtime: "Runtime", file_path: str, **overrides: Any) -> InvoiceState:
    """Execute one invoice end to end.

    The thread id is derived from the file content, so resuming a run and
    reprocessing the same file land on the same checkpoint thread.
    """
    from ..extraction import checksum_file
    from pathlib import Path

    path = Path(file_path)
    thread_id = overrides.pop("thread_id", None) or f"invoice:{checksum_file(path)[:16]}"

    initial: InvoiceState = {
        "file_path": str(path),
        "file_name": path.name,
        "thread_id": thread_id,
        "status": "pending",
        **overrides,
    }

    return runtime.invoice_graph.invoke(
        initial,
        config={"configurable": {"thread_id": thread_id}, "recursion_limit": 25},
    )
