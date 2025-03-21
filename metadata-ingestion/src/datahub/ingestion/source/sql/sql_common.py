import datetime
import logging
import traceback
from collections import OrderedDict
from dataclasses import dataclass, field
from enum import Enum
from typing import (
    TYPE_CHECKING,
    Any,
    Callable,
    Dict,
    Iterable,
    List,
    Optional,
    Set,
    Tuple,
    Type,
    Union,
)

import sqlalchemy.dialects.postgresql.base
from sqlalchemy import create_engine, inspect
from sqlalchemy.engine.reflection import Inspector
from sqlalchemy.exc import ProgrammingError
from sqlalchemy.sql import sqltypes as types
from sqlalchemy.types import TypeDecorator, TypeEngine

from datahub.emitter.mce_builder import (
    make_data_platform_urn,
    make_dataplatform_instance_urn,
    make_dataset_urn_with_platform_instance,
    make_tag_urn,
)
from datahub.emitter.mcp import MetadataChangeProposalWrapper
from datahub.ingestion.api.common import PipelineContext
from datahub.ingestion.api.workunit import MetadataWorkUnit
from datahub.ingestion.source.sql.sql_config import SQLAlchemyConfig
from datahub.ingestion.source.sql.sql_utils import (
    add_table_to_schema_container,
    gen_database_container,
    gen_database_key,
    gen_schema_container,
    gen_schema_key,
    get_domain_wu,
)
from datahub.ingestion.source.state.sql_common_state import (
    BaseSQLAlchemyCheckpointState,
)
from datahub.ingestion.source.state.stale_entity_removal_handler import (
    StaleEntityRemovalHandler,
    StaleEntityRemovalSourceReport,
)
from datahub.ingestion.source.state.stateful_ingestion_base import (
    StatefulIngestionSourceBase,
)
from datahub.metadata.com.linkedin.pegasus2avro.common import StatusClass
from datahub.metadata.com.linkedin.pegasus2avro.dataset import UpstreamLineage
from datahub.metadata.com.linkedin.pegasus2avro.metadata.snapshot import DatasetSnapshot
from datahub.metadata.com.linkedin.pegasus2avro.mxe import MetadataChangeEvent
from datahub.metadata.com.linkedin.pegasus2avro.schema import (
    ArrayTypeClass,
    BooleanTypeClass,
    BytesTypeClass,
    DateTypeClass,
    EnumTypeClass,
    ForeignKeyConstraint,
    MySqlDDL,
    NullTypeClass,
    NumberTypeClass,
    RecordTypeClass,
    SchemaField,
    SchemaFieldDataType,
    SchemaMetadata,
    StringTypeClass,
    TimeTypeClass,
)
from datahub.metadata.schema_classes import (
    ChangeTypeClass,
    DataPlatformInstanceClass,
    DatasetLineageTypeClass,
    DatasetPropertiesClass,
    GlobalTagsClass,
    SubTypesClass,
    TagAssociationClass,
    UpstreamClass,
    ViewPropertiesClass,
)
from datahub.telemetry import telemetry
from datahub.utilities.lossy_collections import LossyList
from datahub.utilities.registries.domain_registry import DomainRegistry
from datahub.utilities.source_helpers import (
    auto_stale_entity_removal,
    auto_status_aspect,
)
from datahub.utilities.sqlalchemy_query_combiner import SQLAlchemyQueryCombinerReport

if TYPE_CHECKING:
    from datahub.ingestion.source.ge_data_profiler import (
        DatahubGEProfiler,
        GEProfilerRequest,
    )

logger: logging.Logger = logging.getLogger(__name__)

MISSING_COLUMN_INFO = "missing column information"


def _platform_alchemy_uri_tester_gen(
    platform: str, opt_starts_with: Optional[str] = None
) -> Tuple[str, Callable[[str], bool]]:
    return platform, lambda x: x.startswith(
        platform if not opt_starts_with else opt_starts_with
    )


PLATFORM_TO_SQLALCHEMY_URI_TESTER_MAP: Dict[str, Callable[[str], bool]] = OrderedDict(
    [
        _platform_alchemy_uri_tester_gen("athena", "awsathena"),
        _platform_alchemy_uri_tester_gen("bigquery"),
        _platform_alchemy_uri_tester_gen("clickhouse"),
        _platform_alchemy_uri_tester_gen("druid"),
        _platform_alchemy_uri_tester_gen("hana"),
        _platform_alchemy_uri_tester_gen("hive"),
        _platform_alchemy_uri_tester_gen("mongodb"),
        _platform_alchemy_uri_tester_gen("mssql"),
        _platform_alchemy_uri_tester_gen("mysql"),
        _platform_alchemy_uri_tester_gen("oracle"),
        _platform_alchemy_uri_tester_gen("pinot"),
        _platform_alchemy_uri_tester_gen("presto"),
        (
            "redshift",
            lambda x: (
                x.startswith(("jdbc:postgres:", "postgresql"))
                and x.find("redshift.amazonaws") > 0
            )
            or x.startswith("redshift"),
        ),
        # Don't move this before redshift.
        _platform_alchemy_uri_tester_gen("postgres", "postgresql"),
        _platform_alchemy_uri_tester_gen("snowflake"),
        _platform_alchemy_uri_tester_gen("trino"),
        _platform_alchemy_uri_tester_gen("vertica"),
    ]
)


def get_platform_from_sqlalchemy_uri(sqlalchemy_uri: str) -> str:
    for platform, tester in PLATFORM_TO_SQLALCHEMY_URI_TESTER_MAP.items():
        if tester(sqlalchemy_uri):
            return platform
    return "external"


class SqlContainerSubTypes(str, Enum):
    DATABASE = "Database"
    SCHEMA = "Schema"


@dataclass
class SQLSourceReport(StaleEntityRemovalSourceReport):
    tables_scanned: int = 0
    views_scanned: int = 0
    entities_profiled: int = 0
    filtered: LossyList[str] = field(default_factory=LossyList)

    query_combiner: Optional[SQLAlchemyQueryCombinerReport] = None

    def report_entity_scanned(self, name: str, ent_type: str = "table") -> None:
        """
        Entity could be a view or a table
        """
        if ent_type == "table":
            self.tables_scanned += 1
        elif ent_type == "view":
            self.views_scanned += 1
        else:
            raise KeyError(f"Unknown entity {ent_type}.")

    def report_entity_profiled(self, name: str) -> None:
        self.entities_profiled += 1

    def report_dropped(self, ent_name: str) -> None:
        self.filtered.append(ent_name)

    def report_from_query_combiner(
        self, query_combiner_report: SQLAlchemyQueryCombinerReport
    ) -> None:
        self.query_combiner = query_combiner_report


class SqlWorkUnit(MetadataWorkUnit):
    pass


_field_type_mapping: Dict[Type[TypeEngine], Type] = {
    types.Integer: NumberTypeClass,
    types.Numeric: NumberTypeClass,
    types.Boolean: BooleanTypeClass,
    types.Enum: EnumTypeClass,
    types._Binary: BytesTypeClass,
    types.LargeBinary: BytesTypeClass,
    types.PickleType: BytesTypeClass,
    types.ARRAY: ArrayTypeClass,
    types.String: StringTypeClass,
    types.Date: DateTypeClass,
    types.DATE: DateTypeClass,
    types.Time: TimeTypeClass,
    types.DateTime: TimeTypeClass,
    types.DATETIME: TimeTypeClass,
    types.TIMESTAMP: TimeTypeClass,
    types.JSON: RecordTypeClass,
    # Because the postgresql dialect is used internally by many other dialects,
    # we add some postgres types here. This is ok to do because the postgresql
    # dialect is built-in to sqlalchemy.
    sqlalchemy.dialects.postgresql.base.BYTEA: BytesTypeClass,
    sqlalchemy.dialects.postgresql.base.DOUBLE_PRECISION: NumberTypeClass,
    sqlalchemy.dialects.postgresql.base.INET: StringTypeClass,
    sqlalchemy.dialects.postgresql.base.MACADDR: StringTypeClass,
    sqlalchemy.dialects.postgresql.base.MONEY: NumberTypeClass,
    sqlalchemy.dialects.postgresql.base.OID: StringTypeClass,
    sqlalchemy.dialects.postgresql.base.REGCLASS: BytesTypeClass,
    sqlalchemy.dialects.postgresql.base.TIMESTAMP: TimeTypeClass,
    sqlalchemy.dialects.postgresql.base.TIME: TimeTypeClass,
    sqlalchemy.dialects.postgresql.base.INTERVAL: TimeTypeClass,
    sqlalchemy.dialects.postgresql.base.BIT: BytesTypeClass,
    sqlalchemy.dialects.postgresql.base.UUID: StringTypeClass,
    sqlalchemy.dialects.postgresql.base.TSVECTOR: BytesTypeClass,
    sqlalchemy.dialects.postgresql.base.ENUM: EnumTypeClass,
    # When SQLAlchemy is unable to map a type into its internal hierarchy, it
    # assigns the NullType by default. We want to carry this warning through.
    types.NullType: NullTypeClass,
}
_known_unknown_field_types: Set[Type[TypeEngine]] = {
    types.Interval,
    types.CLOB,
}


def register_custom_type(tp: Type[TypeEngine], output: Optional[Type] = None) -> None:
    if output:
        _field_type_mapping[tp] = output
    else:
        _known_unknown_field_types.add(tp)


class _CustomSQLAlchemyDummyType(TypeDecorator):
    impl = types.LargeBinary


def make_sqlalchemy_type(name: str) -> Type[TypeEngine]:
    # This usage of type() dynamically constructs a class.
    # See https://stackoverflow.com/a/15247202/5004662 and
    # https://docs.python.org/3/library/functions.html#type.
    sqlalchemy_type: Type[TypeEngine] = type(
        name,
        (_CustomSQLAlchemyDummyType,),
        {
            "__repr__": lambda self: f"{name}()",
        },
    )
    return sqlalchemy_type


def get_column_type(
    sql_report: SQLSourceReport, dataset_name: str, column_type: Any
) -> SchemaFieldDataType:
    """
    Maps SQLAlchemy types (https://docs.sqlalchemy.org/en/13/core/type_basics.html) to corresponding schema types
    """

    TypeClass: Optional[Type] = None
    for sql_type in _field_type_mapping.keys():
        if isinstance(column_type, sql_type):
            TypeClass = _field_type_mapping[sql_type]
            break
    if TypeClass is None:
        for sql_type in _known_unknown_field_types:
            if isinstance(column_type, sql_type):
                TypeClass = NullTypeClass
                break

    if TypeClass is None:
        sql_report.report_warning(
            dataset_name, f"unable to map type {column_type!r} to metadata schema"
        )
        TypeClass = NullTypeClass

    return SchemaFieldDataType(type=TypeClass())


def get_schema_metadata(
    sql_report: SQLSourceReport,
    dataset_name: str,
    platform: str,
    columns: List[dict],
    pk_constraints: Optional[dict] = None,
    foreign_keys: Optional[List[ForeignKeyConstraint]] = None,
    canonical_schema: List[SchemaField] = [],
) -> SchemaMetadata:
    schema_metadata = SchemaMetadata(
        schemaName=dataset_name,
        platform=make_data_platform_urn(platform),
        version=0,
        hash="",
        platformSchema=MySqlDDL(tableSchema=""),
        fields=canonical_schema,
    )
    if foreign_keys is not None and foreign_keys != []:
        schema_metadata.foreignKeys = foreign_keys

    return schema_metadata


# config flags to emit telemetry for
config_options_to_report = [
    "include_views",
    "include_tables",
]


class SQLAlchemySource(StatefulIngestionSourceBase):
    """A Base class for all SQL Sources that use SQLAlchemy to extend"""

    def __init__(self, config: SQLAlchemyConfig, ctx: PipelineContext, platform: str):
        super(SQLAlchemySource, self).__init__(config, ctx)
        self.config = config
        self.platform = platform
        self.report: SQLSourceReport = SQLSourceReport()

        # Create and register the stateful ingestion use-case handlers.
        self.stale_entity_removal_handler = StaleEntityRemovalHandler(
            source=self,
            config=self.config,
            state_type_class=BaseSQLAlchemyCheckpointState,
            pipeline_name=self.ctx.pipeline_name,
            run_id=self.ctx.run_id,
        )

        config_report = {
            config_option: config.dict().get(config_option)
            for config_option in config_options_to_report
        }

        config_report = {
            **config_report,
            "profiling_enabled": config.profiling.enabled,
            "platform": platform,
        }

        telemetry.telemetry_instance.ping(
            "sql_config",
            config_report,
        )

        if config.profiling.enabled:
            telemetry.telemetry_instance.ping(
                "sql_profiling_config",
                config.profiling.config_for_telemetry(),
            )

        self.domain_registry: Optional[DomainRegistry] = None
        if self.config.domain:
            self.domain_registry = DomainRegistry(
                cached_domains=[k for k in self.config.domain], graph=self.ctx.graph
            )

    def warn(self, log: logging.Logger, key: str, reason: str) -> None:
        self.report.report_warning(key, reason)
        log.warning(f"{key} => {reason}")

    def error(self, log: logging.Logger, key: str, reason: str) -> None:
        self.report.report_failure(key, reason)
        log.error(f"{key} => {reason}")

    def get_inspectors(self) -> Iterable[Inspector]:
        # This method can be overridden in the case that you want to dynamically
        # run on multiple databases.

        url = self.config.get_sql_alchemy_url()
        logger.debug(f"sql_alchemy_url={url}")
        engine = create_engine(url, **self.config.options)
        with engine.connect() as conn:
            inspector = inspect(conn)
            yield inspector

    def get_db_name(self, inspector: Inspector) -> str:
        engine = inspector.engine

        if engine and hasattr(engine, "url") and hasattr(engine.url, "database"):
            return str(engine.url.database).strip('"').lower()
        else:
            raise Exception("Unable to get database name from Sqlalchemy inspector")

    def get_schema_names(self, inspector):
        return inspector.get_schema_names()

    def get_platform_instance_id(self) -> Optional[str]:
        """
        The source identifier such as the specific source host address required for stateful ingestion.
        Individual subclasses need to override this method appropriately.
        """
        config_dict = self.config.dict()
        host_port = config_dict.get("host_port", "no_host_port")
        database = config_dict.get("database", "no_database")
        return f"{self.platform}_{host_port}_{database}"

    def get_allowed_schemas(self, inspector: Inspector, db_name: str) -> Iterable[str]:
        # this function returns the schema names which are filtered by schema_pattern.
        for schema in self.get_schema_names(inspector):
            if not self.config.schema_pattern.allowed(schema):
                self.report.report_dropped(f"{schema}.*")
                continue
            else:
                self.add_information_for_schema(inspector, schema)
                yield schema

    def gen_database_containers(
        self,
        database: str,
        extra_properties: Optional[Dict[str, Any]] = None,
    ) -> Iterable[MetadataWorkUnit]:
        database_container_key = gen_database_key(
            database,
            platform=self.platform,
            platform_instance=self.config.platform_instance,
            env=self.config.env,
        )

        yield from gen_database_container(
            database=database,
            database_container_key=database_container_key,
            sub_types=[SqlContainerSubTypes.DATABASE],
            domain_registry=self.domain_registry,
            domain_config=self.config.domain,
            report=self.report,
            extra_properties=extra_properties,
        )

    def gen_schema_containers(
        self,
        schema: str,
        database: str,
        extra_properties: Optional[Dict[str, Any]] = None,
    ) -> Iterable[MetadataWorkUnit]:

        database_container_key = gen_database_key(
            database,
            platform=self.platform,
            platform_instance=self.config.platform_instance,
            env=self.config.env,
        )

        schema_container_key = gen_schema_key(
            db_name=database,
            schema=schema,
            platform=self.platform,
            platform_instance=self.config.platform_instance,
            env=self.config.env,
        )

        yield from gen_schema_container(
            database=database,
            schema=schema,
            schema_container_key=schema_container_key,
            database_container_key=database_container_key,
            sub_types=[SqlContainerSubTypes.SCHEMA],
            domain_registry=self.domain_registry,
            domain_config=self.config.domain,
            report=self.report,
            extra_properties=extra_properties,
        )

    def add_table_to_schema_container(
        self,
        dataset_urn: str,
        db_name: str,
        schema: str,
    ) -> Iterable[MetadataWorkUnit]:

        schema_container_key = gen_schema_key(
            db_name=db_name,
            schema=schema,
            platform=self.platform,
            platform_instance=self.config.platform_instance,
            env=self.config.env,
        )

        yield from add_table_to_schema_container(
            dataset_urn=dataset_urn,
            parent_container_key=schema_container_key,
            report=self.report,
        )

    def get_workunits_internal(self) -> Iterable[Union[MetadataWorkUnit, SqlWorkUnit]]:
        sql_config = self.config
        if logger.isEnabledFor(logging.DEBUG):
            # If debug logging is enabled, we also want to echo each SQL query issued.
            sql_config.options.setdefault("echo", True)

        # Extra default SQLAlchemy option for better connection pooling and threading.
        # https://docs.sqlalchemy.org/en/14/core/pooling.html#sqlalchemy.pool.QueuePool.params.max_overflow
        if sql_config.profiling.enabled:
            sql_config.options.setdefault(
                "max_overflow", sql_config.profiling.max_workers
            )

        for inspector in self.get_inspectors():
            profiler = None
            profile_requests: List["GEProfilerRequest"] = []
            if sql_config.profiling.enabled:
                profiler = self.get_profiler_instance(inspector)

            db_name = self.get_db_name(inspector)
            yield from self.gen_database_containers(
                database=db_name,
            )

            for schema in self.get_allowed_schemas(inspector, db_name):
                self.add_information_for_schema(inspector, schema)

                yield from self.gen_schema_containers(
                    database=db_name,
                    schema=schema,
                    extra_properties=self.get_schema_properties(
                        inspector=inspector, schema=schema, database=db_name
                    ),
                )

                if sql_config.include_tables:
                    yield from self.loop_tables(inspector, schema, sql_config)

                if sql_config.include_views:
                    yield from self.loop_views(inspector, schema, sql_config)

                if profiler:
                    profile_requests += list(
                        self.loop_profiler_requests(inspector, schema, sql_config)
                    )

            if profiler and profile_requests:
                yield from self.loop_profiler(
                    profile_requests, profiler, platform=self.platform
                )

    def get_workunits(self) -> Iterable[MetadataWorkUnit]:
        return auto_stale_entity_removal(
            self.stale_entity_removal_handler,
            auto_status_aspect(self.get_workunits_internal()),
        )

    def standardize_schema_table_names(
        self, schema: str, entity: str
    ) -> Tuple[str, str]:
        # Some SQLAlchemy dialects need a standardization step to clean the schema
        # and table names. See BigQuery for an example of when this is useful.
        return schema, entity

    def get_identifier(
        self, *, schema: str, entity: str, inspector: Inspector, **kwargs: Any
    ) -> str:
        # Many SQLAlchemy dialects have three-level hierarchies. This method, which
        # subclasses can override, enables them to modify the identifiers as needed.
        if hasattr(self.config, "get_identifier"):
            # This path is deprecated and will eventually be removed.
            return self.config.get_identifier(schema=schema, table=entity)  # type: ignore
        else:
            return f"{schema}.{entity}"

    def get_foreign_key_metadata(
        self,
        dataset_urn: str,
        schema: str,
        fk_dict: Dict[str, str],
        inspector: Inspector,
    ) -> ForeignKeyConstraint:
        referred_schema: Optional[str] = fk_dict.get("referred_schema")

        if not referred_schema:
            referred_schema = schema

        referred_dataset_name = self.get_identifier(
            schema=referred_schema,
            entity=fk_dict["referred_table"],
            inspector=inspector,
        )

        source_fields = [
            f"urn:li:schemaField:({dataset_urn},{f})"
            for f in fk_dict["constrained_columns"]
        ]
        foreign_dataset = make_dataset_urn_with_platform_instance(
            platform=self.platform,
            name=referred_dataset_name,
            platform_instance=self.config.platform_instance,
            env=self.config.env,
        )
        foreign_fields = [
            f"urn:li:schemaField:({foreign_dataset},{f})"
            for f in fk_dict["referred_columns"]
        ]

        return ForeignKeyConstraint(
            fk_dict["name"], foreign_fields, source_fields, foreign_dataset
        )

    def normalise_dataset_name(self, dataset_name: str) -> str:
        return dataset_name

    def loop_tables(  # noqa: C901
        self,
        inspector: Inspector,
        schema: str,
        sql_config: SQLAlchemyConfig,
    ) -> Iterable[Union[SqlWorkUnit, MetadataWorkUnit]]:
        tables_seen: Set[str] = set()
        try:
            for table in inspector.get_table_names(schema):
                schema, table = self.standardize_schema_table_names(
                    schema=schema, entity=table
                )
                dataset_name = self.get_identifier(
                    schema=schema, entity=table, inspector=inspector
                )

                dataset_name = self.normalise_dataset_name(dataset_name)

                if dataset_name not in tables_seen:
                    tables_seen.add(dataset_name)
                else:
                    logger.debug(f"{dataset_name} has already been seen, skipping...")
                    continue

                self.report.report_entity_scanned(dataset_name, ent_type="table")
                if not sql_config.table_pattern.allowed(dataset_name):
                    self.report.report_dropped(dataset_name)
                    continue

                try:
                    yield from self._process_table(
                        dataset_name, inspector, schema, table, sql_config
                    )
                except Exception as e:
                    logger.warning(
                        f"Unable to ingest {schema}.{table} due to an exception.\n {traceback.format_exc()}"
                    )
                    self.report.report_warning(
                        f"{schema}.{table}", f"Ingestion error: {e}"
                    )
        except Exception as e:
            self.report.report_failure(f"{schema}", f"Tables error: {e}")

    def add_information_for_schema(self, inspector: Inspector, schema: str) -> None:
        pass

    def get_extra_tags(
        self, inspector: Inspector, schema: str, table: str
    ) -> Optional[Dict[str, List[str]]]:
        return None

    def _process_table(
        self,
        dataset_name: str,
        inspector: Inspector,
        schema: str,
        table: str,
        sql_config: SQLAlchemyConfig,
    ) -> Iterable[Union[SqlWorkUnit, MetadataWorkUnit]]:
        columns = self._get_columns(dataset_name, inspector, schema, table)
        dataset_urn = make_dataset_urn_with_platform_instance(
            self.platform,
            dataset_name,
            self.config.platform_instance,
            self.config.env,
        )
        dataset_snapshot = DatasetSnapshot(
            urn=dataset_urn,
            aspects=[StatusClass(removed=False)],
        )

        description, properties, location_urn = self.get_table_properties(
            inspector, schema, table
        )

        # Tablename might be different from the real table if we ran some normalisation ont it.
        # Getting normalized table name from the dataset_name
        # Table is the last item in the dataset name
        normalised_table = table
        splits = dataset_name.split(".")
        if splits:
            normalised_table = splits[-1]
            if properties and normalised_table != table:
                properties["original_table_name"] = table

        dataset_properties = DatasetPropertiesClass(
            name=normalised_table,
            description=description,
            customProperties=properties,
        )
        dataset_snapshot.aspects.append(dataset_properties)

        if self.config.include_table_location_lineage and location_urn:
            external_upstream_table = UpstreamClass(
                dataset=location_urn,
                type=DatasetLineageTypeClass.COPY,
            )
            lineage_mcpw = MetadataChangeProposalWrapper(
                entityType="dataset",
                changeType=ChangeTypeClass.UPSERT,
                entityUrn=dataset_snapshot.urn,
                aspectName="upstreamLineage",
                aspect=UpstreamLineage(upstreams=[external_upstream_table]),
            )
            lineage_wu = MetadataWorkUnit(
                id=f"{self.platform}-{lineage_mcpw.entityUrn}-{lineage_mcpw.aspectName}",
                mcp=lineage_mcpw,
            )
            self.report.report_workunit(lineage_wu)
            yield lineage_wu

        extra_tags = self.get_extra_tags(inspector, schema, table)
        pk_constraints: dict = inspector.get_pk_constraint(table, schema)
        foreign_keys = self._get_foreign_keys(dataset_urn, inspector, schema, table)
        schema_fields = self.get_schema_fields(
            dataset_name, columns, pk_constraints, tags=extra_tags
        )
        schema_metadata = get_schema_metadata(
            self.report,
            dataset_name,
            self.platform,
            columns,
            pk_constraints,
            foreign_keys,
            schema_fields,
        )
        dataset_snapshot.aspects.append(schema_metadata)
        db_name = self.get_db_name(inspector)

        yield from self.add_table_to_schema_container(
            dataset_urn=dataset_urn, db_name=db_name, schema=schema
        )
        mce = MetadataChangeEvent(proposedSnapshot=dataset_snapshot)
        wu = SqlWorkUnit(id=dataset_name, mce=mce)
        self.report.report_workunit(wu)
        yield wu
        dpi_aspect = self.get_dataplatform_instance_aspect(dataset_urn=dataset_urn)
        if dpi_aspect:
            yield dpi_aspect
        subtypes_aspect = MetadataWorkUnit(
            id=f"{dataset_name}-subtypes",
            mcp=MetadataChangeProposalWrapper(
                entityType="dataset",
                changeType=ChangeTypeClass.UPSERT,
                entityUrn=dataset_urn,
                aspectName="subTypes",
                aspect=SubTypesClass(typeNames=["table"]),
            ),
        )
        self.report.report_workunit(subtypes_aspect)
        yield subtypes_aspect

        if self.config.domain:
            assert self.domain_registry
            yield from get_domain_wu(
                dataset_name=dataset_name,
                entity_urn=dataset_urn,
                domain_config=sql_config.domain,
                domain_registry=self.domain_registry,
                report=self.report,
            )

    def get_database_properties(
        self, inspector: Inspector, database: str
    ) -> Optional[Dict[str, str]]:
        return None

    def get_schema_properties(
        self, inspector: Inspector, database: str, schema: str
    ) -> Optional[Dict[str, str]]:
        return None

    def get_table_properties(
        self, inspector: Inspector, schema: str, table: str
    ) -> Tuple[Optional[str], Dict[str, str], Optional[str]]:
        description: Optional[str] = None
        properties: Dict[str, str] = {}

        # The location cannot be fetched generically, but subclasses may override
        # this method and provide a location.
        location: Optional[str] = None

        try:
            # SQLAlchemy stubs are incomplete and missing this method.
            # PR: https://github.com/dropbox/sqlalchemy-stubs/pull/223.
            table_info: dict = inspector.get_table_comment(table, schema)  # type: ignore
        except NotImplementedError:
            return description, properties, location
        except ProgrammingError as pe:
            # Snowflake needs schema names quoted when fetching table comments.
            logger.debug(
                f"Encountered ProgrammingError. Retrying with quoted schema name for schema {schema} and table {table}",
                pe,
            )
            table_info: dict = inspector.get_table_comment(table, f'"{schema}"')  # type: ignore

        description = table_info.get("text")
        if type(description) is tuple:
            # Handling for value type tuple which is coming for dialect 'db2+ibm_db'
            description = table_info["text"][0]

        # The "properties" field is a non-standard addition to SQLAlchemy's interface.
        properties = table_info.get("properties", {})
        return description, properties, location

    def get_dataplatform_instance_aspect(
        self, dataset_urn: str
    ) -> Optional[SqlWorkUnit]:
        # If we are a platform instance based source, emit the instance aspect
        if self.config.platform_instance:
            mcp = MetadataChangeProposalWrapper(
                entityType="dataset",
                changeType=ChangeTypeClass.UPSERT,
                entityUrn=dataset_urn,
                aspectName="dataPlatformInstance",
                aspect=DataPlatformInstanceClass(
                    platform=make_data_platform_urn(self.platform),
                    instance=make_dataplatform_instance_urn(
                        self.platform, self.config.platform_instance
                    ),
                ),
            )
            wu = SqlWorkUnit(id=f"{dataset_urn}-dataPlatformInstance", mcp=mcp)
            self.report.report_workunit(wu)
            return wu
        else:
            return None

    def _get_columns(
        self, dataset_name: str, inspector: Inspector, schema: str, table: str
    ) -> List[dict]:
        columns = []
        try:
            columns = inspector.get_columns(table, schema)
            if len(columns) == 0:
                self.report.report_warning(MISSING_COLUMN_INFO, dataset_name)
        except Exception as e:
            self.report.report_warning(
                dataset_name,
                f"unable to get column information due to an error -> {e}",
            )
        return columns

    def _get_foreign_keys(
        self, dataset_urn: str, inspector: Inspector, schema: str, table: str
    ) -> List[ForeignKeyConstraint]:
        try:
            foreign_keys = [
                self.get_foreign_key_metadata(dataset_urn, schema, fk_rec, inspector)
                for fk_rec in inspector.get_foreign_keys(table, schema)
            ]
        except KeyError:
            # certain databases like MySQL cause issues due to lower-case/upper-case irregularities
            logger.debug(
                f"{dataset_urn}: failure in foreign key extraction... skipping"
            )
            foreign_keys = []
        return foreign_keys

    def get_schema_fields(
        self,
        dataset_name: str,
        columns: List[dict],
        pk_constraints: Optional[dict] = None,
        tags: Optional[Dict[str, List[str]]] = None,
    ) -> List[SchemaField]:
        canonical_schema = []
        for column in columns:
            column_tags: Optional[List[str]] = None
            if tags:
                column_tags = tags.get(column["name"], [])
            fields = self.get_schema_fields_for_column(
                dataset_name, column, pk_constraints, tags=column_tags
            )
            canonical_schema.extend(fields)
        return canonical_schema

    def get_schema_fields_for_column(
        self,
        dataset_name: str,
        column: dict,
        pk_constraints: Optional[dict] = None,
        tags: Optional[List[str]] = None,
    ) -> List[SchemaField]:
        gtc: Optional[GlobalTagsClass] = None
        if tags:
            tags_str = [make_tag_urn(t) for t in tags]
            tags_tac = [TagAssociationClass(t) for t in tags_str]
            gtc = GlobalTagsClass(tags_tac)
        field = SchemaField(
            fieldPath=column["name"],
            type=get_column_type(self.report, dataset_name, column["type"]),
            nativeDataType=column.get("full_type", repr(column["type"])),
            description=column.get("comment", None),
            nullable=column["nullable"],
            recursive=False,
            globalTags=gtc,
        )
        if (
            pk_constraints is not None
            and isinstance(pk_constraints, dict)  # some dialects (hive) return list
            and column["name"] in pk_constraints.get("constrained_columns", [])
        ):
            field.isPartOfKey = True
        return [field]

    def loop_views(
        self,
        inspector: Inspector,
        schema: str,
        sql_config: SQLAlchemyConfig,
    ) -> Iterable[Union[SqlWorkUnit, MetadataWorkUnit]]:
        try:
            for view in inspector.get_view_names(schema):
                schema, view = self.standardize_schema_table_names(
                    schema=schema, entity=view
                )
                dataset_name = self.get_identifier(
                    schema=schema, entity=view, inspector=inspector
                )
                dataset_name = self.normalise_dataset_name(dataset_name)

                self.report.report_entity_scanned(dataset_name, ent_type="view")

                if not sql_config.view_pattern.allowed(dataset_name):
                    self.report.report_dropped(dataset_name)
                    continue

                try:
                    yield from self._process_view(
                        dataset_name=dataset_name,
                        inspector=inspector,
                        schema=schema,
                        view=view,
                        sql_config=sql_config,
                    )
                except Exception as e:
                    logger.warning(
                        f"Unable to ingest view {schema}.{view} due to an exception.\n {traceback.format_exc()}"
                    )
                    self.report.report_warning(
                        f"{schema}.{view}", f"Ingestion error: {e}"
                    )
        except Exception as e:
            self.report.report_failure(f"{schema}", f"Views error: {e}")

    def _process_view(
        self,
        dataset_name: str,
        inspector: Inspector,
        schema: str,
        view: str,
        sql_config: SQLAlchemyConfig,
    ) -> Iterable[Union[SqlWorkUnit, MetadataWorkUnit]]:
        try:
            columns = inspector.get_columns(view, schema)
        except KeyError:
            # For certain types of views, we are unable to fetch the list of columns.
            self.report.report_warning(
                dataset_name, "unable to get schema for this view"
            )
            schema_metadata = None
        else:
            schema_fields = self.get_schema_fields(dataset_name, columns)
            schema_metadata = get_schema_metadata(
                self.report,
                dataset_name,
                self.platform,
                columns,
                canonical_schema=schema_fields,
            )
        description, properties, _ = self.get_table_properties(inspector, schema, view)
        try:
            view_definition = inspector.get_view_definition(view, schema)
            if view_definition is None:
                view_definition = ""
            else:
                # Some dialects return a TextClause instead of a raw string,
                # so we need to convert them to a string.
                view_definition = str(view_definition)
        except NotImplementedError:
            view_definition = ""
        properties["view_definition"] = view_definition
        properties["is_view"] = "True"
        dataset_urn = make_dataset_urn_with_platform_instance(
            self.platform,
            dataset_name,
            self.config.platform_instance,
            self.config.env,
        )
        dataset_snapshot = DatasetSnapshot(
            urn=dataset_urn,
            aspects=[StatusClass(removed=False)],
        )
        db_name = self.get_db_name(inspector)
        yield from self.add_table_to_schema_container(
            dataset_urn=dataset_urn,
            db_name=db_name,
            schema=schema,
        )

        dataset_properties = DatasetPropertiesClass(
            name=view,
            description=description,
            customProperties=properties,
        )
        dataset_snapshot.aspects.append(dataset_properties)
        if schema_metadata:
            dataset_snapshot.aspects.append(schema_metadata)
        mce = MetadataChangeEvent(proposedSnapshot=dataset_snapshot)
        wu = SqlWorkUnit(id=dataset_name, mce=mce)
        self.report.report_workunit(wu)
        yield wu
        dpi_aspect = self.get_dataplatform_instance_aspect(dataset_urn=dataset_urn)
        if dpi_aspect:
            yield dpi_aspect
        subtypes_aspect = MetadataWorkUnit(
            id=f"{view}-subtypes",
            mcp=MetadataChangeProposalWrapper(
                entityType="dataset",
                changeType=ChangeTypeClass.UPSERT,
                entityUrn=dataset_urn,
                aspectName="subTypes",
                aspect=SubTypesClass(typeNames=["view"]),
            ),
        )
        self.report.report_workunit(subtypes_aspect)
        yield subtypes_aspect
        if "view_definition" in properties:
            view_definition_string = properties["view_definition"]
            view_properties_aspect = ViewPropertiesClass(
                materialized=False, viewLanguage="SQL", viewLogic=view_definition_string
            )
            view_properties_wu = MetadataChangeProposalWrapper(
                entityUrn=dataset_urn,
                aspect=view_properties_aspect,
            ).as_workunit()
            self.report.report_workunit(view_properties_wu)
            yield view_properties_wu

        if self.config.domain and self.domain_registry:
            yield from get_domain_wu(
                dataset_name=dataset_name,
                entity_urn=dataset_urn,
                domain_config=sql_config.domain,
                domain_registry=self.domain_registry,
                report=self.report,
            )

    def get_profiler_instance(self, inspector: Inspector) -> "DatahubGEProfiler":
        from datahub.ingestion.source.ge_data_profiler import DatahubGEProfiler

        return DatahubGEProfiler(
            conn=inspector.bind,
            report=self.report,
            config=self.config.profiling,
            platform=self.platform,
        )

    def get_profile_args(self) -> Dict:
        """Passed down to GE profiler"""
        return {}

    # Override if needed
    def generate_partition_profiler_query(
        self, schema: str, table: str, partition_datetime: Optional[datetime.datetime]
    ) -> Tuple[Optional[str], Optional[str]]:
        return None, None

    def is_table_partitioned(
        self, database: Optional[str], schema: str, table: str
    ) -> Optional[bool]:
        return None

    # Override if needed
    def generate_profile_candidates(
        self,
        inspector: Inspector,
        threshold_time: Optional[datetime.datetime],
        schema: str,
    ) -> Optional[List[str]]:
        raise NotImplementedError()

    # Override if you want to do additional checks
    def is_dataset_eligible_for_profiling(
        self,
        dataset_name: str,
        sql_config: SQLAlchemyConfig,
        inspector: Inspector,
        profile_candidates: Optional[List[str]],
    ) -> bool:
        return (
            sql_config.table_pattern.allowed(dataset_name)
            and sql_config.profile_pattern.allowed(dataset_name)
        ) and (
            profile_candidates is None
            or (profile_candidates is not None and dataset_name in profile_candidates)
        )

    def loop_profiler_requests(
        self,
        inspector: Inspector,
        schema: str,
        sql_config: SQLAlchemyConfig,
    ) -> Iterable["GEProfilerRequest"]:
        from datahub.ingestion.source.ge_data_profiler import GEProfilerRequest

        tables_seen: Set[str] = set()
        profile_candidates = None  # Default value if profile candidates not available.
        if (
            sql_config.profiling.profile_if_updated_since_days is not None
            or sql_config.profiling.profile_table_size_limit is not None
            or sql_config.profiling.profile_table_row_limit is None
        ):
            try:
                threshold_time: Optional[datetime.datetime] = None
                if sql_config.profiling.profile_if_updated_since_days is not None:
                    threshold_time = datetime.datetime.now(
                        datetime.timezone.utc
                    ) - datetime.timedelta(
                        sql_config.profiling.profile_if_updated_since_days
                    )
                profile_candidates = self.generate_profile_candidates(
                    inspector, threshold_time, schema
                )
            except NotImplementedError:
                logger.debug("Source does not support generating profile candidates.")

        for table in inspector.get_table_names(schema):
            schema, table = self.standardize_schema_table_names(
                schema=schema, entity=table
            )
            dataset_name = self.get_identifier(
                schema=schema, entity=table, inspector=inspector
            )
            if not self.is_dataset_eligible_for_profiling(
                dataset_name, sql_config, inspector, profile_candidates
            ):
                if self.config.profiling.report_dropped_profiles:
                    self.report.report_dropped(f"profile of {dataset_name}")
                continue

            dataset_name = self.normalise_dataset_name(dataset_name)

            if dataset_name not in tables_seen:
                tables_seen.add(dataset_name)
            else:
                logger.debug(f"{dataset_name} has already been seen, skipping...")
                continue

            missing_column_info_warn = self.report.warnings.get(MISSING_COLUMN_INFO)
            if (
                missing_column_info_warn is not None
                and dataset_name in missing_column_info_warn
            ):
                continue

            (partition, custom_sql) = self.generate_partition_profiler_query(
                schema, table, self.config.profiling.partition_datetime
            )

            if partition is None and self.is_table_partitioned(
                database=None, schema=schema, table=table
            ):
                self.report.report_warning(
                    "profile skipped as partitioned table is empty or partition id was invalid",
                    dataset_name,
                )
                continue

            if (
                partition is not None
                and not self.config.profiling.partition_profiling_enabled
            ):
                logger.debug(
                    f"{dataset_name} and partition {partition} is skipped because profiling.partition_profiling_enabled property is disabled"
                )
                continue

            self.report.report_entity_profiled(dataset_name)
            logger.debug(
                f"Preparing profiling request for {schema}, {table}, {partition}"
            )
            yield GEProfilerRequest(
                pretty_name=dataset_name,
                batch_kwargs=self.prepare_profiler_args(
                    inspector=inspector,
                    schema=schema,
                    table=table,
                    partition=partition,
                    custom_sql=custom_sql,
                ),
            )

    def loop_profiler(
        self,
        profile_requests: List["GEProfilerRequest"],
        profiler: "DatahubGEProfiler",
        platform: Optional[str] = None,
    ) -> Iterable[MetadataWorkUnit]:
        for request, profile in profiler.generate_profiles(
            profile_requests,
            self.config.profiling.max_workers,
            platform=platform,
            profiler_args=self.get_profile_args(),
        ):
            if profile is None:
                continue
            dataset_name = request.pretty_name
            dataset_urn = make_dataset_urn_with_platform_instance(
                self.platform,
                dataset_name,
                self.config.platform_instance,
                self.config.env,
            )
            mcp = MetadataChangeProposalWrapper(
                entityType="dataset",
                entityUrn=dataset_urn,
                changeType=ChangeTypeClass.UPSERT,
                aspectName="datasetProfile",
                aspect=profile,
            )
            wu = MetadataWorkUnit(id=f"profile-{dataset_name}", mcp=mcp)
            self.report.report_workunit(wu)

            yield wu

    def prepare_profiler_args(
        self,
        inspector: Inspector,
        schema: str,
        table: str,
        partition: Optional[str],
        custom_sql: Optional[str] = None,
    ) -> dict:
        return dict(
            schema=schema, table=table, partition=partition, custom_sql=custom_sql
        )

    def get_report(self):
        return self.report
