from __future__ import annotations

import logging

from opentelemetry import metrics, trace
from opentelemetry._logs import set_logger_provider
from opentelemetry.exporter.otlp.proto.grpc._log_exporter import OTLPLogExporter
from opentelemetry.exporter.otlp.proto.grpc.metric_exporter import OTLPMetricExporter
from opentelemetry.exporter.otlp.proto.grpc.trace_exporter import OTLPSpanExporter
from opentelemetry.instrumentation.fastapi import FastAPIInstrumentor
from opentelemetry.sdk._logs import LoggerProvider, LoggingHandler
from opentelemetry.sdk._logs.export import BatchLogRecordProcessor
from opentelemetry.sdk.metrics import MeterProvider
from opentelemetry.sdk.metrics.export import PeriodicExportingMetricReader
from opentelemetry.sdk.resources import Resource
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import BatchSpanProcessor

_INSTALLED = False
_METRICS_INSTALLED = False
_LOGS_INSTALLED = False


def install_tracing(
    *,
    service_name: str,
    service_version: str,
    otlp_endpoint: str,
    enabled: bool,
) -> None:
    global _INSTALLED
    if not enabled or _INSTALLED:
        return

    resource = Resource.create(
        {
            "service.name": service_name,
            "service.version": service_version,
        }
    )
    provider = TracerProvider(resource=resource)
    exporter = OTLPSpanExporter(endpoint=otlp_endpoint, insecure=True)
    provider.add_span_processor(BatchSpanProcessor(exporter))
    trace.set_tracer_provider(provider)
    _INSTALLED = True


def install_metrics(
    *,
    service_name: str,
    service_version: str,
    otlp_endpoint: str,
    enabled: bool,
) -> None:
    """경로 B — 앱이 OTLP metrics 를 Collector(:4317)로 emit → Collector 의
    prometheus exporter(:8889)가 Prometheus 에 노출. trace 와 동일 파이프라인이라
    histogram exemplar 가 trace_id 를 실어 metric→trace 점프를 가능케 한다(plan §4).
    `/metrics` 엔드포인트(경로 A)는 채택하지 않는다."""
    global _METRICS_INSTALLED
    if not enabled or _METRICS_INSTALLED:
        return

    resource = Resource.create(
        {
            "service.name": service_name,
            "service.version": service_version,
        }
    )
    reader = PeriodicExportingMetricReader(
        OTLPMetricExporter(endpoint=otlp_endpoint, insecure=True)
    )
    provider = MeterProvider(resource=resource, metric_readers=[reader])
    metrics.set_meter_provider(provider)
    _METRICS_INSTALLED = True


# 관측 가치 없는 주기/프로브 엔드포인트 — health check·readiness·모델 목록 폴링은
# 클라이언트(OpenWebUI·로드밸런서·k8s probe)가 수초마다 때려 trace 를 노이즈로 채운다.
# 정규식(쉼표 구분, ASGI 표준 excluded_urls 형식). OTEL_EXCLUDED_URLS 로 override.
_DEFAULT_EXCLUDED_URLS = "health,ready,v1/models"


def instrument_fastapi(app) -> None:  # noqa: ANN001 (fastapi.FastAPI lazy-imported)
    import os
    # excluded_urls — 주기 호출(/health·/ready·/v1/models)은 server 스팬 자체를 안 만든다.
    # 실제 작업(POST /v1/chat/completions)만 trace. 빈값이면 제외 없음(전부 수집).
    excluded = os.getenv("OTEL_EXCLUDED_URLS", _DEFAULT_EXCLUDED_URLS).strip() or None
    # exclude_spans=["send","receive"] — ASGI 자동계측이 *모든 http.send 이벤트*마다 자식
    # 스팬(`POST /v1/chat/completions http send`)을 만든다. 일반 JSON 응답은 1~2개지만,
    # SSE 스트리밍(/v1/chat/completions)은 토큰 프레임마다 send 이벤트가 발생해 답변 한 건이
    # 수백 개의 `http send` 스팬을 낳는다 → trace 비대·Phoenix 지연. 이 send/receive 스팬은
    # 우리 워크플로우 관측에 가치가 없다(server 스팬 1개 + agent.run 자식이면 충분). 끈다.
    # 부모 server 스팬(요청당 1개)·agent.run·노드 스팬은 그대로 유지된다.
    FastAPIInstrumentor.instrument_app(
        app, excluded_urls=excluded, exclude_spans=["send", "receive"])


def install_logs(
    *,
    service_name: str,
    service_version: str,
    otlp_endpoint: str,
    enabled: bool,
) -> None:
    """structlog 이 stdlib logging 으로 렌더한 JSON 레코드를 OTel LoggingHandler 가
    OTLP 로그로 변환해 Collector(:4317)→Loki 로 보낸다. LoggingHandler 가 현재 span
    의 trace context 를 OTLP 레코드에 자동 부착(logging.configure 의 _add_trace_context
    는 stdout JSON 본문에도 동일 trace_id 를 싣는다). stdout JSON 출력은 그대로 유지."""
    global _LOGS_INSTALLED
    if not enabled or _LOGS_INSTALLED:
        return

    resource = Resource.create(
        {
            "service.name": service_name,
            "service.version": service_version,
        }
    )
    provider = LoggerProvider(resource=resource)
    provider.add_log_record_processor(
        BatchLogRecordProcessor(OTLPLogExporter(endpoint=otlp_endpoint, insecure=True))
    )
    set_logger_provider(provider)
    handler = LoggingHandler(level=logging.NOTSET, logger_provider=provider)
    # 의도적 root 부착 — 앱 노드 로그뿐 아니라 httpx/asyncpg/opensearch/uvicorn 까지
    # 모두 Loki 로 보낸다. 실험 플랫폼이라 볼륨 부담이 작고, 도구 실패의 하부 원인
    # (예: httpx timeout)을 같은 trace_id 로 상관해 실패 귀인(§5)에 보탬이 된다.
    # 앱 로그만 원하면 logging.getLogger("app") 같은 네임스페이스에 붙일 것.
    logging.getLogger().addHandler(handler)
    _LOGS_INSTALLED = True


def get_tracer(name: str = "agent"):  # noqa: ANN201
    return trace.get_tracer(name)


def get_meter(name: str = "agent"):  # noqa: ANN201
    # provider 설치 전 호출되면 proxy meter 를 반환하고, set_meter_provider 후 첫
    # 측정에서 실제 meter 로 lazily resolve 된다(tracer proxy 와 동일). 따라서
    # 모듈-레벨에서 instrument 를 만들어도 안전하다(metrics.py).
    return metrics.get_meter(name)
