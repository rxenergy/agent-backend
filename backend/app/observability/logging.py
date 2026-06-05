from __future__ import annotations

import logging

import structlog
from opentelemetry import trace


def _add_trace_context(logger, method_name, event_dict):  # noqa: ANN001, ARG001
    """현재 OTel span 의 trace_id/span_id 를 모든 로그 라인에 주입한다(plan §8 레버 5).
    stdout JSON 과 OTLP 로그 양쪽이 trace_id 를 실어 Loki↔Tempo 상호 점프의 전제가
    된다(Loki derived fields 가 이 값을 Tempo 로 링크). 활성 span 이 없으면 무첨가."""
    span = trace.get_current_span()
    ctx = span.get_span_context() if span is not None else None
    if ctx is not None and ctx.is_valid:
        event_dict["trace_id"] = format(ctx.trace_id, "032x")
        event_dict["span_id"] = format(ctx.span_id, "016x")
    return event_dict


def configure_logging(level: str = "INFO") -> None:
    level_no = getattr(logging, level.upper(), logging.INFO)
    # stdlib logging 을 백엔드로 — JSONRenderer 가 만든 문자열이 stdlib 레코드 msg 가
    # 되어 (a) basicConfig StreamHandler 로 stdout JSON, (b) install_logs 가 붙인 OTel
    # LoggingHandler 로 OTLP→Collector→Loki 양쪽에 동일 본문으로 흐른다. (이전 PrintLogger
    # 직출력에선 OTLP 로 가는 경로가 없었다 — W3.)
    logging.basicConfig(format="%(message)s", level=level_no)

    structlog.configure(
        processors=[
            structlog.contextvars.merge_contextvars,
            structlog.processors.add_log_level,
            structlog.processors.TimeStamper(fmt="iso", utc=True),
            _add_trace_context,
            structlog.processors.StackInfoRenderer(),
            structlog.processors.format_exc_info,
            structlog.processors.JSONRenderer(ensure_ascii=False),
        ],
        logger_factory=structlog.stdlib.LoggerFactory(),
        wrapper_class=structlog.make_filtering_bound_logger(level_no),
        cache_logger_on_first_use=True,
    )


def get_logger(name: str | None = None) -> structlog.stdlib.BoundLogger:
    return structlog.get_logger(name) if name else structlog.get_logger()
