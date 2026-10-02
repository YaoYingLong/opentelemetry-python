# Copyright The OpenTelemetry Authors
# SPDX-License-Identifier: Apache-2.0
#

"""
OpenTelemetry SDK Configurator for Easy Instrumentation with Distros
"""

from __future__ import annotations

import logging
import logging.config
import os
import warnings
from abc import ABC, abstractmethod
from collections.abc import Callable, Mapping, Sequence
from os import environ
from typing import Any, Literal, Protocol

from opentelemetry._logs import set_logger_provider
from opentelemetry.environment_variables import (
    OTEL_LOGS_EXPORTER,
    OTEL_METRICS_EXPORTER,
    OTEL_PYTHON_ID_GENERATOR,
    OTEL_TRACES_EXPORTER,
)
from opentelemetry.metrics import set_meter_provider
from opentelemetry.sdk._logs import (
    LoggerProvider,
    LoggingHandler,
    LogRecordProcessor,
)
from opentelemetry.sdk._logs._internal import _LoggerConfiguratorT
from opentelemetry.sdk._logs.export import (
    BatchLogRecordProcessor,
    LogRecordExporter,
)
from opentelemetry.sdk.environment_variables import (
    _OTEL_PYTHON_LOGGING_AUTO_INSTRUMENTATION_ENABLED,
    OTEL_CONFIG_FILE,
    OTEL_EXPORTER_OTLP_LOGS_PROTOCOL,
    OTEL_EXPORTER_OTLP_METRICS_PROTOCOL,
    OTEL_EXPORTER_OTLP_PROTOCOL,
    OTEL_EXPORTER_OTLP_TRACES_PROTOCOL,
    OTEL_PYTHON_LOGGER_CONFIGURATOR,
    OTEL_PYTHON_METER_CONFIGURATOR,
    OTEL_PYTHON_TRACER_CONFIGURATOR,
    OTEL_TRACES_SAMPLER,
    OTEL_TRACES_SAMPLER_ARG,
)
from opentelemetry.sdk.metrics import MeterProvider
from opentelemetry.sdk.metrics._internal import _MeterConfiguratorT
from opentelemetry.sdk.metrics.export import (
    MetricExporter,
    MetricReader,
    PeriodicExportingMetricReader,
)
from opentelemetry.sdk.resources import Attributes, Resource
from opentelemetry.sdk.trace import (
    SpanProcessor,
    TracerProvider,
    _TracerConfiguratorT,
)
from opentelemetry.sdk.trace.export import BatchSpanProcessor, SpanExporter
from opentelemetry.sdk.trace.id_generator import IdGenerator
from opentelemetry.sdk.trace.sampling import Sampler
from opentelemetry.semconv.resource import ResourceAttributes
from opentelemetry.trace import set_tracer_provider
from opentelemetry.util._importlib_metadata import entry_points

_EXPORTER_OTLP = "otlp"
_EXPORTER_OTLP_PROTO_GRPC = "otlp_proto_grpc"
_EXPORTER_OTLP_PROTO_HTTP = "otlp_proto_http"

_EXPORTER_BY_OTLP_PROTOCOL = {
    "grpc": _EXPORTER_OTLP_PROTO_GRPC,
    "http/protobuf": _EXPORTER_OTLP_PROTO_HTTP,
}

_EXPORTER_ENV_BY_SIGNAL_TYPE = {
    "traces": OTEL_TRACES_EXPORTER,
    "metrics": OTEL_METRICS_EXPORTER,
    "logs": OTEL_LOGS_EXPORTER,
}

_PROTOCOL_ENV_BY_SIGNAL_TYPE = {
    "traces": OTEL_EXPORTER_OTLP_TRACES_PROTOCOL,
    "metrics": OTEL_EXPORTER_OTLP_METRICS_PROTOCOL,
    "logs": OTEL_EXPORTER_OTLP_LOGS_PROTOCOL,
}

_RANDOM_ID_GENERATOR = "random"
_DEFAULT_ID_GENERATOR = _RANDOM_ID_GENERATOR

_OTEL_SAMPLER_ENTRY_POINT_GROUP = "opentelemetry_traces_sampler"

_logger = logging.getLogger(__name__)

ExporterArgsMap = Mapping[
    type[SpanExporter] | type[MetricExporter] | type[MetricReader] | type[LogRecordExporter],
    Mapping[str, Any],
]


class _ConfigurationExporterSpanProcessorT(Protocol):
    def __call__(self, span_exporter: SpanExporter, *args, **kwargs) -> SpanProcessor: ...


class _ConfigurationExporterLogRecordProcessorT(Protocol):
    def __call__(self, exporter: LogRecordExporter, *args, **kwargs) -> LogRecordProcessor: ...


def _import_config_components(selected_components: Sequence[str], entry_point_name: str) -> list[tuple[str, type]]:
    component_implementations = []
    # selected_components为传入的exporter_names
    for selected_component in selected_components:
        try:
            component_implementations.append(
                (
                    selected_component,
                    # entry_point_name为opentelemetry_traces_exporter、opentelemetry_metrics_exporter、opentelemetry_log_exporter
                    # 其实就是加载/Volumes/extra/SourceCode/opentelemetry-python/exporter中匹配的exporter names对应的组件
                    next(iter(entry_points(group=entry_point_name, name=selected_component))).load(),
                )
            )
        except KeyError:
            raise RuntimeError(f"Requested entry point '{entry_point_name}' not found")

        except StopIteration:
            raise RuntimeError(
                f"Requested component '{selected_component}' not found in entry point '{entry_point_name}'"
            )

    return component_implementations


def _get_sampler() -> str | None:
    return environ.get(OTEL_TRACES_SAMPLER, None)


def _get_id_generator() -> str:
    return environ.get(OTEL_PYTHON_ID_GENERATOR, _DEFAULT_ID_GENERATOR)


def _get_tracer_configurator() -> str | None:
    return environ.get(OTEL_PYTHON_TRACER_CONFIGURATOR, None)


def _get_meter_configurator() -> str | None:
    return environ.get(OTEL_PYTHON_METER_CONFIGURATOR, None)


def _get_logger_configurator() -> str | None:
    return environ.get(OTEL_PYTHON_LOGGER_CONFIGURATOR, None)


def _get_exporter_entry_point(exporter_name: str, signal_type: Literal["traces", "metrics", "logs"]):
    # 如果exporter_name不属于_EXPORTER_OTLP、_EXPORTER_OTLP_PROTO_GRPC、_EXPORTER_OTLP_PROTO_HTTP就直接返回原始名称
    if exporter_name not in (
        _EXPORTER_OTLP,
        _EXPORTER_OTLP_PROTO_GRPC,
        _EXPORTER_OTLP_PROTO_HTTP,
    ):
        return exporter_name

    # Checking env vars for OTLP protocol (grpc/http).
    # 通过类型获取对应的环境变量产量值，然后从环境变量中获取对应的otlp_protocol
    # 如果没有配置兜底使用OTEL_EXPORTER_OTLP_PROTOCOL配置的内容
    otlp_protocol = environ.get(_PROTOCOL_ENV_BY_SIGNAL_TYPE[signal_type]) or environ.get(OTEL_EXPORTER_OTLP_PROTOCOL)
    # 如果没有配置otlp_protocol
    if not otlp_protocol:
        if exporter_name == _EXPORTER_OTLP:
            # 如果exporter_name配置的是otlp，返回otlp_proto_grpc
            return _EXPORTER_OTLP_PROTO_GRPC
        # 如果有配置otlp_protocol，但是exporter_name不是_EXPORTER_OTLP，直接返回传入的exporter_name
        return exporter_name

    # 如果有配置otlp_protocol，去掉首位空白
    otlp_protocol = otlp_protocol.strip()
    # 如果exporter_name配置的是otlp
    if exporter_name == _EXPORTER_OTLP:
        # 如果配置的otlp_protocol既不是grpc也不是http/protobuf，直接抛出异常
        if otlp_protocol not in _EXPORTER_BY_OTLP_PROTOCOL:
            # Invalid value was set by the env var
            raise RuntimeError(f"Unsupported OTLP protocol '{otlp_protocol}' is configured")
        # 返回对应的grpc或http/protobuf对应的otlp_proto_grpc或otlp_proto_http
        return _EXPORTER_BY_OTLP_PROTOCOL[otlp_protocol]

    # grpc/http already specified by exporter_name, only add a warning in case
    # of a conflict.
    exporter_name_by_env = _EXPORTER_BY_OTLP_PROTOCOL.get(otlp_protocol)
    if exporter_name_by_env and exporter_name != exporter_name_by_env:
        _logger.warning(
            "Conflicting values for %s OTLP exporter protocol, using '%s'",
            signal_type,
            exporter_name,
        )

    return exporter_name

# 获取exporter_name列表
def _get_exporter_names(
    signal_type: Literal["traces", "metrics", "logs"],
) -> list[str]:
    # 这里传入的signal_type是：traces、metrics、logs之一，其实就是读取对应的环境变量
    names = environ.get(_EXPORTER_ENV_BY_SIGNAL_TYPE.get(signal_type, ""))

    # 如果没有配置或者配置none就返回空列表
    if not names or names.lower().strip() == "none":
        return []
    # 函数表达式，将names按照逗号拆分开，然后返回exporter_name列表，仅仅支持otlp、otlp_proto_grpc、otlp_proto_http
    return [_get_exporter_entry_point(_exporter.strip(), signal_type) for _exporter in names.split(",")]


def _init_tracing(
    exporters: dict[str, type[SpanExporter]],
    id_generator: IdGenerator | None = None,
    sampler: Sampler | None = None,
    resource: Resource | None = None,
    exporter_args_map: ExporterArgsMap | None = None,
    span_processors: Sequence[SpanProcessor] | None = None,
    export_span_processor: _ConfigurationExporterSpanProcessorT | None = None,
    tracer_configurator: _TracerConfiguratorT | None = None,
):
    # 自动配置创建SDK Provider，并尝试将其注册为全局TracerProvider。
    provider = TracerProvider(
        id_generator=id_generator,
        sampler=sampler,
        resource=resource,
        _tracer_configurator=tracer_configurator,
    )
    # 将provider赋值给全局变量_TRACER_PROVIDER，且即使多次执行也仅仅赋值一次
    set_tracer_provider(provider)

    exporter_args_map = exporter_args_map or {}
    # 默认是BatchSpanProcessor
    export_processor = export_span_processor or BatchSpanProcessor

    span_processors = span_processors or []
    for span_processor in span_processors:
        provider.add_span_processor(span_processor)

    # 每个 exporter 实例交给导出处理器；默认使用 BatchSpanProcessor，也可通过 export_span_processor 指定。
    for _, exporter_class in exporters.items():
        exporter_args = exporter_args_map.get(exporter_class, {})
        # 执行BatchSpanProcessor的构造方法，然后将实例添加到provider中
        provider.add_span_processor(export_processor(exporter_class(**exporter_args)))


def _init_metrics(
    exporters_or_readers: dict[str, type[MetricExporter | MetricReader]],
    resource: Resource | None = None,
    exporter_args_map: ExporterArgsMap | None = None,
    meter_configurator: _MeterConfiguratorT | None = None,
):
    metric_readers = []

    exporter_args_map = exporter_args_map or {}
    for _, exporter_or_reader_class in exporters_or_readers.items():
        exporter_args = exporter_args_map.get(exporter_or_reader_class, {})
        if issubclass(exporter_or_reader_class, MetricReader):
            metric_readers.append(exporter_or_reader_class(**exporter_args))
        else:
            metric_readers.append(PeriodicExportingMetricReader(exporter_or_reader_class(**exporter_args)))

    provider = MeterProvider(
        resource=resource,
        metric_readers=metric_readers,
        _meter_configurator=meter_configurator,
    )
    set_meter_provider(provider)


# pylint: disable-next=too-many-locals
def _init_logging(
    exporters: dict[str, type[LogRecordExporter]],
    resource: Resource | None = None,
    setup_logging_handler: bool = True,
    exporter_args_map: ExporterArgsMap | None = None,
    log_record_processors: Sequence[LogRecordProcessor] | None = None,
    export_log_record_processor: _ConfigurationExporterLogRecordProcessorT | None = None,
    logger_configurator: _LoggerConfiguratorT | None = None,
):
    provider = LoggerProvider(resource=resource, _logger_configurator=logger_configurator)
    set_logger_provider(provider)

    exporter_args_map = exporter_args_map or {}
    export_processor = export_log_record_processor or BatchLogRecordProcessor

    log_record_processors = log_record_processors or []
    for log_record_processor in log_record_processors:
        provider.add_log_record_processor(log_record_processor)

    for _, exporter_class in exporters.items():
        exporter_args = exporter_args_map.get(exporter_class, {})
        provider.add_log_record_processor(export_processor(exporter_class(**exporter_args)))

    if setup_logging_handler:
        warnings.warn(
            "The `OTEL_PYTHON_LOGGING_AUTO_INSTRUMENTATION_ENABLED` environment variable "
            "and the `LoggingHandler` in `opentelemetry-sdk` that it controls are deprecated."
            "Install `opentelemetry-instrumentation-logging` package instead.",
            DeprecationWarning,
        )

        # Add OTel handler
        handler = LoggingHandler(level=logging.NOTSET, logger_provider=provider)
        logging.getLogger().addHandler(handler)
        _overwrite_logging_config_fns(handler)


def _overwrite_logging_config_fns(handler: LoggingHandler) -> None:
    root = logging.getLogger()

    def wrapper(config_fn: Callable) -> Callable:
        def overwritten_config_fn(*args, **kwargs):
            removed_handler = False
            # We don't want the OTLP handler to be modified or deleted by the logging config functions.
            # So we remove it and then add it back after the function call.
            if handler in root.handlers:
                removed_handler = True
                root.handlers.remove(handler)
            try:
                config_fn(*args, **kwargs)
            finally:
                # Ensure handler is added back if logging function throws exception.
                if removed_handler:
                    root.addHandler(handler)

        return overwritten_config_fn

    logging.config.fileConfig = wrapper(logging.config.fileConfig)
    logging.config.dictConfig = wrapper(logging.config.dictConfig)
    logging.basicConfig = wrapper(logging.basicConfig)


def _import_logger_configurator(
    logger_configurator_name: str | None,
) -> _LoggerConfiguratorT | None:
    if not logger_configurator_name:
        return None

    try:
        _, logger_configurator_impl = _import_config_components(
            [logger_configurator_name.strip()],
            "_opentelemetry_logger_configurator",
        )[0]
    except Exception as exc:  # pylint: disable=broad-exception-caught
        _logger.warning(
            "Using default logger configurator. Failed to load logger configurator, %s: %s",
            logger_configurator_name,
            exc,
        )
        return None
    return logger_configurator_impl


def _import_tracer_configurator(
    tracer_configurator_name: str | None,
) -> _TracerConfiguratorT | None:
    if not tracer_configurator_name:
        return None

    try:
        _, tracer_configurator_impl = _import_config_components(
            [tracer_configurator_name.strip()],
            "_opentelemetry_tracer_configurator",
        )[0]
    except Exception as exc:  # pylint: disable=broad-exception-caught
        _logger.warning(
            "Using default tracer configurator. Failed to load tracer configurator, %s: %s",
            tracer_configurator_name,
            exc,
        )
        return None
    return tracer_configurator_impl


def _import_meter_configurator(
    meter_configurator_name: str | None,
) -> _MeterConfiguratorT | None:
    if not meter_configurator_name:
        return None

    try:
        _, meter_configurator_impl = _import_config_components(
            [meter_configurator_name.strip()],
            "_opentelemetry_meter_configurator",
        )[0]
    except Exception as exc:  # pylint: disable=broad-exception-caught
        _logger.warning(
            "Using default meter configurator. Failed to load meter configurator, %s: %s",
            meter_configurator_name,
            exc,
        )
        return None
    return meter_configurator_impl


def _import_exporters(
    trace_exporter_names: Sequence[str],
    metric_exporter_names: Sequence[str],
    log_exporter_names: Sequence[str],
) -> tuple[
    dict[str, type[SpanExporter]],
    dict[str, type[MetricExporter | MetricReader]],
    dict[str, type[LogRecordExporter]],
]:
    trace_exporters = {}
    metric_exporters = {}
    log_exporters = {}

    for (
        exporter_name,
        exporter_impl,
    ) in _import_config_components(trace_exporter_names, "opentelemetry_traces_exporter"):
        if issubclass(exporter_impl, SpanExporter):
            trace_exporters[exporter_name] = exporter_impl
        else:
            raise RuntimeError(f"{exporter_name} is not a trace exporter")

    for (
        exporter_name,
        exporter_impl,
    ) in _import_config_components(metric_exporter_names, "opentelemetry_metrics_exporter"):
        # The metric exporter components may be push MetricExporter or pull exporters which
        # subclass MetricReader directly
        if issubclass(exporter_impl, (MetricExporter, MetricReader)):
            metric_exporters[exporter_name] = exporter_impl
        else:
            raise RuntimeError(f"{exporter_name} is not a metric exporter")

    for (
        exporter_name,
        exporter_impl,
    ) in _import_config_components(log_exporter_names, "opentelemetry_logs_exporter"):
        if issubclass(exporter_impl, LogRecordExporter):
            log_exporters[exporter_name] = exporter_impl
        else:
            raise RuntimeError(f"{exporter_name} is not a log exporter")

    return trace_exporters, metric_exporters, log_exporters


def _import_sampler_factory(
    sampler_name: str,
) -> Callable[[float | str | None], Sampler]:
    _, sampler_impl = _import_config_components([sampler_name.strip()], _OTEL_SAMPLER_ENTRY_POINT_GROUP)[0]
    return sampler_impl


def _import_sampler(sampler_name: str | None) -> Sampler | None:
    if not sampler_name:
        return None
    try:
        sampler_factory = _import_sampler_factory(sampler_name)
        arg = None
        if sampler_name in ("traceidratio", "parentbased_traceidratio"):
            try:
                rate = float(os.getenv(OTEL_TRACES_SAMPLER_ARG, ""))
            except (ValueError, TypeError):
                _logger.warning("Could not convert TRACES_SAMPLER_ARG to float. Using default value 1.0.")
                rate = 1.0
            arg = rate
        else:
            arg = os.getenv(OTEL_TRACES_SAMPLER_ARG)

        sampler = sampler_factory(arg)
        if not isinstance(sampler, Sampler):
            message = f"Sampler factory, {sampler_factory}, produced output, {sampler}, which is not a Sampler."
            _logger.warning(message)
            raise ValueError(message)
        return sampler
    except Exception as exc:  # pylint: disable=broad-exception-caught
        _logger.warning(
            "Using default sampler. Failed to initialize sampler, %s: %s",
            sampler_name,
            exc,
        )
        return None


def _import_id_generator(id_generator_name: str) -> IdGenerator:
    id_generator_name, id_generator_impl = _import_config_components(
        [id_generator_name.strip()], "opentelemetry_id_generator"
    )[0]

    if issubclass(id_generator_impl, IdGenerator):
        return id_generator_impl()

    raise RuntimeError(f"{id_generator_name} is not an IdGenerator")


def _import_opamp(
    name: Literal["pre_sdk_init_function", "post_sdk_init_function"],
) -> Callable[[Resource], None] | None:
    """Helper for OpAMP entry points loading

    This in development, at the moment we are looking for a callable that takes
    the resource and instantiate an OpAMP agent.
    Since configuration is not specified every implementer may have its own.
    Refer to the opentelemetry-opamp-client package on how to setup the OpAMP agent.
    """
    entry_point = None
    try:
        entry_point = next(iter(entry_points(group="_opentelemetry_opamp", name=name)))
        return entry_point.load()
    except StopIteration:
        _logger.debug("No OpAMP init function found")
    except AttributeError as exc:
        _logger.warning(
            "Failed to load OpAMP init function from entry point, %s: %s",
            entry_point,
            exc,
        )

    return None


def _initialize_components(
    auto_instrumentation_version: str | None = None,
    trace_exporter_names: list[str] | None = None,
    metric_exporter_names: list[str] | None = None,
    log_exporter_names: list[str] | None = None,
    sampler: Sampler | None = None,
    resource_attributes: Attributes | None = None,
    id_generator: IdGenerator | None = None,
    setup_logging_handler: bool | None = None,
    exporter_args_map: ExporterArgsMap | None = None,
    span_processors: Sequence[SpanProcessor] | None = None,
    export_span_processor: _ConfigurationExporterSpanProcessorT | None = None,
    log_record_processors: Sequence[LogRecordProcessor] | None = None,
    export_log_record_processor: _ConfigurationExporterLogRecordProcessorT | None = None,
    tracer_configurator: _TracerConfiguratorT | None = None,
    meter_configurator: _MeterConfiguratorT | None = None,
    logger_configurator: _LoggerConfiguratorT | None = None,
):
    # pylint: disable=too-many-locals,too-many-branches
    if resource_attributes is None:
        resource_attributes = {}
    # populate version if using auto-instrumentation
    if auto_instrumentation_version:
        resource_attributes[ResourceAttributes.TELEMETRY_AUTO_VERSION] = (  # type: ignore[reportIndexIssue]
            auto_instrumentation_version
        )
    # if env var OTEL_RESOURCE_ATTRIBUTES is given, it will read the service_name
    # from the env variable else defaults to "unknown_service"
    resource = Resource.create(resource_attributes)

    # OpAMP is a system created to configure OpenTelemetry SDKs with a remote config.
    # This is different than other init helpers because setting up OpAMP requires distro
    # provided code as it's not strictly specified. We have two entry points for OpAMP:
    # one called early for people that want it blocking to get an updated config before
    # setting up the rest of the SDK and the other after for people that want the
    # SDK already setup.
    _init_opamp = _import_opamp("pre_sdk_init_function")
    if _init_opamp is not None:
        _init_opamp(resource)

    if trace_exporter_names is None:
        trace_exporter_names = []
    if metric_exporter_names is None:
        metric_exporter_names = []
    if log_exporter_names is None:
        log_exporter_names = []
    span_exporters, metric_exporters, log_exporters = _import_exporters(
        # 获取traces的exporter_name列表
        trace_exporter_names + _get_exporter_names("traces"),
        # 获取metrics的exporter_name列表
        metric_exporter_names + _get_exporter_names("metrics"),
        # 获取logs的exporter_name列表
        log_exporter_names + _get_exporter_names("logs"),
    )
    if sampler is None:
        sampler_name = _get_sampler()
        sampler = _import_sampler(sampler_name)
    if id_generator is None:
        id_generator_name = _get_id_generator()
        id_generator = _import_id_generator(id_generator_name)

    if tracer_configurator is None:
        tracer_configurator_name = _get_tracer_configurator()
        tracer_configurator = _import_tracer_configurator(tracer_configurator_name)
    if meter_configurator is None:
        meter_configurator_name = _get_meter_configurator()
        meter_configurator = _import_meter_configurator(meter_configurator_name)
    if logger_configurator is None:
        logger_configurator_name = _get_logger_configurator()
        logger_configurator = _import_logger_configurator(logger_configurator_name)

    _init_tracing(
        exporters=span_exporters,
        id_generator=id_generator,
        sampler=sampler,
        resource=resource,
        exporter_args_map=exporter_args_map,
        span_processors=span_processors,
        export_span_processor=export_span_processor,
        tracer_configurator=tracer_configurator,
    )
    _init_metrics(
        exporters_or_readers=metric_exporters,
        resource=resource,
        exporter_args_map=exporter_args_map,
        meter_configurator=meter_configurator,
    )
    if setup_logging_handler is None:
        setup_logging_handler = (
            os.getenv(_OTEL_PYTHON_LOGGING_AUTO_INSTRUMENTATION_ENABLED, "false").strip().lower() == "true"
        )
    _init_logging(
        log_exporters,
        resource,
        setup_logging_handler,
        exporter_args_map=exporter_args_map,
        log_record_processors=log_record_processors,
        export_log_record_processor=export_log_record_processor,
        logger_configurator=logger_configurator,
    )

    _init_opamp = _import_opamp("post_sdk_init_function")
    if _init_opamp is not None:
        _init_opamp(resource)


class _BaseConfigurator(ABC):
    """An ABC for configurators

    Configurators are used to configure
    SDKs (i.e. TracerProvider, MeterProvider, Processors...)
    to reduce the amount of manual configuration required.
    """

    _instance = None
    _is_instrumented = False

    def __new__(cls, *args, **kwargs):
        if cls._instance is None:
            cls._instance = super().__new__(cls)

        return cls._instance

    @abstractmethod
    def _configure(self, **kwargs):
        """Configure the SDK"""

    def configure(self, **kwargs):
        """Configure the SDK"""
        self._configure(**kwargs)


class _OTelSDKConfigurator(_BaseConfigurator):
    """A basic Configurator by OTel Python for initializing OTel SDK components

    Initializes several crucial OTel SDK components (i.e. TracerProvider,
    MeterProvider, Processors...) according to a default implementation. Other
    Configurators can subclass and slightly alter this initialization.

    NOTE: This class should not be instantiated nor should it become an entry
    point on the `opentelemetry-sdk` package. Instead, distros should subclass
    this Configurator and enhance it as needed.
    """

    def _configure(self, **kwargs):
        # 读取环境变量OTEL_CONFIG_FILE赋值给config_file，判断变量是否真值（不为None、非空字符串）
        if config_file := environ.get(OTEL_CONFIG_FILE):
            # Declarative configuration lives in the separate
            # ``opentelemetry-configuration`` package. Import lazily so the
            # SDK has no runtime dependency on it; users who don't set
            # ``OTEL_CONFIG_FILE`` never pay the import cost.
            try:
                # opentelemetry-configuration is an optional runtime dep
                # and is not installed in this package's lint env, so
                # silence the static-analysis no-name-in-module on the
                # conditional import.
                # pylint: disable=import-outside-toplevel,no-name-in-module
                from opentelemetry.configuration import (  # noqa: PLC0415
                    configure_sdk,
                    load_config_file,
                )
            except ImportError as exc:
                raise RuntimeError(
                    f"{OTEL_CONFIG_FILE} is set but "
                    "opentelemetry-configuration is not installed. "
                    "Install it with: pip install opentelemetry-configuration"
                ) from exc

            if kwargs:
                _logger.warning(
                    "%s is set; ignoring configurator kwargs: %s",
                    OTEL_CONFIG_FILE,
                    sorted(kwargs),
                )
            # 通过load_config_file加载配置文件并将内容解析为OpenTelemetryConfiguration
            # 通过configure_sdk完成_initialize_components后直接退出
            configure_sdk(load_config_file(config_file))
            return
        # 不论是否配置了配置文件，初始化组件
        _initialize_components(**kwargs)
