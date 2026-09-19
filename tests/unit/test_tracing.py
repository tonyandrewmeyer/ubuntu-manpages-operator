# Copyright 2025 Canonical
# See LICENSE file for licensing details.

"""Unit tests for charm tracing.

The charm opts in to `ops[tracing]`, so Ops emits a root span per dispatch and
a span per observer. These tests assert the relations are declared as the
tracing backend expects, that the charm still reconciles with and without a
tracing destination, and that the one outbound call Ops cannot see -- the
charm's own HTTP request to the workload admin listener -- is instrumented.
"""

from collections.abc import Generator
from pathlib import Path

import pytest
import yaml
from opentelemetry.sdk.trace import ReadableSpan
from ops.pebble import CheckLevel, CheckStatus, Layer, ServiceStatus
from ops.testing import ActiveStatus, CheckInfo, Context, MaintenanceStatus, Relation, State
from scenario import Container

from charm import ManpagesCharm

CHECKS_LAYER = Layer(
    {
        "checks": {
            "ready": {
                "override": "replace",
                "period": "1m",
                "http": {"url": "http://localhost:9090/_/healthz"},
                "startup": "enabled",
                "threshold": 3,
            },
        },
    }
)


@pytest.fixture
def charm():
    yield ManpagesCharm


@pytest.fixture
def container():
    return Container(name="manpages", can_connect=True)


def ancestors(span: ReadableSpan, spans: list[ReadableSpan]) -> Generator[ReadableSpan]:
    """Walk the parent chain of a span, following the how-to's recipe."""
    spans_by_id = {s.context.span_id: s for s in spans if s.context}
    while span.parent:
        span = spans_by_id[span.parent.span_id]
        yield span


def scope_of(span: ReadableSpan) -> str:
    """Name of the tracer that emitted a span.

    `ReadableSpan.instrumentation_scope` is typed optional, so the how-to's
    `span.instrumentation_scope.name` does not pass a type checker as printed.
    """
    scope = span.instrumentation_scope
    assert scope is not None
    return scope.name


def test_tracing_relations_declared():
    """The tracing backend requires both relations to be singular and optional."""
    metadata = yaml.safe_load(Path("charmcraft.yaml").read_text())
    requires = metadata["requires"]

    assert requires["charm-tracing"] == {
        "interface": "tracing",
        "limit": 1,
        "optional": True,
    }
    assert requires["receive-ca-cert"] == {
        "interface": "certificate_transfer",
        "limit": 1,
        "optional": True,
    }


def test_ops_emits_a_root_span(charm, container):
    """Ops traces the dispatch even with no tracing relation established."""
    ctx = Context(charm)
    state = State(containers=[container], config={"releases": "noble"})

    ctx.run(ctx.on.update_status(), state)

    main_span = next(s for s in ctx.trace_data if s.name == "ops.main")
    assert scope_of(main_span) == "ops"


def test_observer_span_is_a_child_of_the_root_span(charm, container):
    """The observer span Ops creates is scoped under the root span."""
    ctx = Context(charm)
    state = State(containers=[container], config={"releases": "noble"})

    ctx.run(ctx.on.update_status(), state)

    main_span = next(s for s in ctx.trace_data if s.name == "ops.main")
    observer = next(
        s for s in ctx.trace_data if "_on_update_status" in s.name or "update_status" in s.name
    )
    assert main_span in list(ancestors(observer, list(ctx.trace_data)))


def test_charm_reconciles_with_a_tracing_relation(charm):
    """Relating to a tracing provider does not disturb the charm's own behaviour.

    The charm observes no tracing events itself -- the vendored requirer does
    that -- so the assertion is that an ordinary event still reconciles to the
    same status it reaches without the relation.
    """
    ctx = Context(charm)
    tracing = Relation(
        "charm-tracing",
        remote_app_data={
            "receivers": (
                '[{"protocol": {"name": "otlp_http", "type": "http"}, '
                '"url": "http://tempo:4318/v1/traces"}]'
            )
        },
    )
    check_info = CheckInfo("ready", level=CheckLevel.UNSET, status=CheckStatus.UP, threshold=3)
    container = Container(
        name="manpages",
        can_connect=True,
        layers={"manpages": CHECKS_LAYER},
        check_infos={check_info},
        service_statuses={"ingest": ServiceStatus.INACTIVE},
    )
    state = State(
        containers=[container], relations=[tracing], config={"releases": "noble"}, leader=True
    )

    result = ctx.run(ctx.on.update_status(), state)

    assert result.unit_status == ActiveStatus()
    assert any(s.name == "ops.main" for s in ctx.trace_data)


def test_health_error_fetch_is_instrumented(charm):
    """The charm's direct HTTP call to the admin listener gets its own span."""
    ctx = Context(charm)
    check_info = CheckInfo(
        "ready", level=CheckLevel.UNSET, failures=3, status=CheckStatus.DOWN, threshold=3
    )
    container = Container(
        name="manpages",
        can_connect=True,
        layers={"manpages": CHECKS_LAYER},
        check_infos={check_info},
    )
    state = State(containers=[container], config={"releases": "noble"})

    result = ctx.run(ctx.on.pebble_check_failed(container, info=check_info), state)

    # Nothing is listening on the admin port under test, so the call fails and
    # the charm reports the generic message -- but the span is still recorded.
    assert result.unit_status == MaintenanceStatus("health check failed")
    span = next(s for s in ctx.trace_data if s.name == "fetch-health-error")
    assert scope_of(span) == "manpages"
    assert [e.name for e in span.events] == ["healthz-unreachable"]
    main_span = next(s for s in ctx.trace_data if s.name == "ops.main")
    assert main_span in list(ancestors(span, list(ctx.trace_data)))
