from sentry_sdk.consts import SPANDATA, SPANSTATUS
from sentry_sdk.integrations import DidNotEnable, Integration, _check_minimum_version
from sentry_sdk.traces import SpanStatus, StreamedSpan
from sentry_sdk.tracing import Span
from sentry_sdk.tracing_utils import (
    add_query_source,
    record_sql_queries,
)
from sentry_sdk.utils import (
    capture_internal_exceptions,
    ensure_integration_enabled,
    parse_version,
)

try:
    from sqlalchemy import __version__ as SQLALCHEMY_VERSION  # type: ignore
    from sqlalchemy.engine import Engine  # type: ignore
    from sqlalchemy.event import listen  # type: ignore
except ImportError:
    raise DidNotEnable("SQLAlchemy not installed.")

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from typing import Any, ContextManager, Optional, Union


class SqlalchemyIntegration(Integration):
    identifier = "sqlalchemy"
    origin = f"auto.db.{identifier}"

    @staticmethod
    def setup_once() -> None:
        version = parse_version(SQLALCHEMY_VERSION)
        _check_minimum_version(SqlalchemyIntegration, version)

        listen(Engine, "before_cursor_execute", _before_cursor_execute)
        listen(Engine, "after_cursor_execute", _after_cursor_execute)
        listen(Engine, "handle_error", _handle_error)


@ensure_integration_enabled(SqlalchemyIntegration)
def _before_cursor_execute(
    conn: "Any",
    cursor: "Any",
    statement: "Any",
    parameters: "Any",
    context: "Any",
    executemany: bool,
    *args: "Any",
) -> None:
    ctx_mgr = record_sql_queries(
        cursor,
        statement,
        parameters,
        paramstyle=context and context.dialect and context.dialect.paramstyle or None,
        executemany=executemany,
        span_origin=SqlalchemyIntegration.origin,
    )
    context._sentry_sql_span_manager = ctx_mgr

    span = ctx_mgr.__enter__()

    if span is not None:
        _set_db_data(span, conn)
        context._sentry_sql_span = span


@ensure_integration_enabled(SqlalchemyIntegration)
def _after_cursor_execute(
    conn: "Any",
    cursor: "Any",
    statement: "Any",
    parameters: "Any",
    context: "Any",
    *args: "Any",
) -> None:
    ctx_mgr: "Optional[ContextManager[Any]]" = getattr(
        context, "_sentry_sql_span_manager", None
    )

    # Record query source immediately before span is finished: accurate end timestamp and before the span is flushed.
    span: "Optional[Union[Span, StreamedSpan]]" = getattr(
        context, "_sentry_sql_span", None
    )
    if isinstance(span, StreamedSpan):
        with capture_internal_exceptions():
            add_query_source(span)

    if ctx_mgr is not None:
        context._sentry_sql_span_manager = None
        ctx_mgr.__exit__(None, None, None)

    if isinstance(span, Span):
        with capture_internal_exceptions():
            add_query_source(span)


def _handle_error(context: "Any", *args: "Any") -> None:
    execution_context = context.execution_context
    if execution_context is None:
        return

    span: "Optional[Span]" = getattr(execution_context, "_sentry_sql_span", None)

    if span is not None:
        if isinstance(span, StreamedSpan):
            span.status = SpanStatus.ERROR
        else:
            span.set_status(SPANSTATUS.INTERNAL_ERROR)

    # _after_cursor_execute does not get called for crashing SQL stmts. Judging
    # from SQLAlchemy codebase it does seem like any error coming into this
    # handler is going to be fatal.
    ctx_mgr: "Optional[ContextManager[Any]]" = getattr(
        execution_context, "_sentry_sql_span_manager", None
    )

    if ctx_mgr is not None:
        execution_context._sentry_sql_span_manager = None
        ctx_mgr.__exit__(None, None, None)


# See: https://opentelemetry.io/docs/specs/semconv/registry/attributes/db/
_DIALECT_TO_OTEL_SYSTEM_NAMES = {
    "ingres": "actian.ingres",
    "dynamodb": "aws.dynamodb",
    "redshift": "aws.redshift",
    # "": "azure.cosmosdb",
    # "": "couchbase",
    # "": "couchdb",
    # "": "derby",
    "firebird": "firebirdsql",
    # "": "gcp.spanner",
    # "": "h2database",
    # "": "hbase",
    # "": "hive",
    "db2+ibm_db": "ibm.db2",
    "ibm_db_sa": "ibm.db2",
    # "": "ibm.informix",
    "netezza+pyodbc": "ibm.netezza",
    # "": "influxdb",
    # "": "instantdb",
    # "": "intersystems.cache",
    # "": "memcached",
    # "": "neo4j",
    # "": "opensearch",
    # "": "other_sql",
    "postgres": "postgresql",
    # "": "sap.hana",
    # "": "sap.maxdb",
    # "": "softwareag.adabas",
    # "": "teradata",
    # "": "trino",
}

# See: https://docs.sqlalchemy.org/en/20/dialects/index.html
_SQLALCHEMY_DIALECTS = [
    "access",
    "athena",
    "aurora",
    "drill",
    "druid",
    "hive",
    "cassandra",
    "clickhouse",
    "cockroachdb",
    "cratedb",
    "databend",
    "databricks",
    "denodo",
    "exasolution",
    "elasticsearch",
    "firebolt",
    "bigquery",
    "gsheets",
    "greenplum",
    "hsqldb",
    "impala",
    "kinetica",
    "mariadb",
    "mssql",
    "mysql",
    "oracle",
    "postgresql",
    "sqlite",
    "solr",
]


def _get_db_system(name: str) -> "Optional[str]":
    name = str(name)

    # If name is mapped from SQLAlchemy dialect to OTel well-known name, use the mapped value.
    otel_system_name = _DIALECT_TO_OTEL_SYSTEM_NAMES.get(name)
    if otel_system_name:
        return otel_system_name

    # If name is a known SQLAlchemy dialect without a mapping, use the dialect name.
    matches = [dialect for dialect in _SQLALCHEMY_DIALECTS if name in dialect]
    if matches:
        return matches[0]

    return None


def _set_db_data(span: "Union[Span, StreamedSpan]", conn: "Any") -> None:
    db_system = _get_db_system(conn.engine.name)

    if isinstance(span, StreamedSpan):
        if db_system is not None:
            span.set_attribute(SPANDATA.DB_SYSTEM_NAME, db_system)
    else:
        if db_system is not None:
            span.set_data(SPANDATA.DB_SYSTEM, db_system)

    if isinstance(span, StreamedSpan):
        set_on_span = span.set_attribute
    else:
        set_on_span = span.set_data

    try:
        driver = conn.dialect.driver
        if driver:
            set_on_span(SPANDATA.DB_DRIVER_NAME, driver)
    except Exception:
        pass

    if conn.engine.url is None:
        return

    db_name = conn.engine.url.database
    if isinstance(span, StreamedSpan):
        if db_name is not None:
            span.set_attribute(SPANDATA.DB_NAMESPACE, db_name)
    else:
        if db_name is not None:
            span.set_data(SPANDATA.DB_NAME, db_name)

    server_address = conn.engine.url.host
    if server_address is not None:
        set_on_span(SPANDATA.SERVER_ADDRESS, server_address)

    server_port = conn.engine.url.port
    if server_port is not None:
        set_on_span(SPANDATA.SERVER_PORT, server_port)
