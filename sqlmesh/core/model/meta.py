from __future__ import annotations

import typing as t
from enum import Enum

from croniter import croniter
from pydantic import Field, root_validator, validator
from sqlglot import exp, maybe_parse

from sqlmesh.core import dialect as d
from sqlmesh.core.model.kind import (
    IncrementalByTimeRange,
    IncrementalByUniqueKey,
    ModelKind,
    ModelKindName,
    TimeColumn,
)
from sqlmesh.utils import unique
from sqlmesh.utils.date import TimeLike, preserve_time_like_kind, to_datetime
from sqlmesh.utils.errors import ConfigError
from sqlmesh.utils.pydantic import PydanticModel


class IntervalUnit(str, Enum):
    """IntervalUnit is the inferred granularity of an incremental model.

    IntervalUnit can be one of 4 types, DAY, HOUR, MINUTE. The unit is inferred
    based on the cron schedule of a model. The minimum time delta between a sample set of dates
    is used to determine which unit a model's schedule is.
    """

    DAY = "day"
    HOUR = "hour"
    MINUTE = "minute"


class ModelMeta(PydanticModel):
    """Metadata for models which can be defined in SQL."""

    name: str
    kind: ModelKind = IncrementalByTimeRange()
    dialect: str = ""
    cron: str = "@daily"
    owner: t.Optional[str]
    description: t.Optional[str]
    start: t.Optional[TimeLike]
    batch_size: t.Optional[int]
    storage_format: t.Optional[str]
    partitioned_by_: t.Optional[t.List[str]] = Field(
        default=None, alias="partitioned_by"
    )
    depends_on_: t.Optional[t.Set[str]] = Field(default=None, alias="depends_on")
    columns_to_types_: t.Optional[t.Dict[str, exp.DataType]] = Field(
        default=None, alias="columns"
    )
    _croniter: t.Optional[croniter] = None

    @validator("partitioned_by_", pre=True)
    def _value_or_tuple_validator(cls, v):
        if isinstance(v, exp.Tuple):
            return [i.name for i in v.expressions]
        if isinstance(v, exp.Expression):
            return [v.name]
        return v

    @validator("kind", pre=True)
    def _model_kind_validator(cls, v: t.Any) -> ModelKind:
        if isinstance(v, ModelKind):
            return v

        if isinstance(v, d.ModelKind):
            name = v.this
            props = {prop.name: prop.args.get("value") for prop in v.expressions}
            klass: t.Type[ModelKind] = ModelKind
            if name == ModelKindName.INCREMENTAL_BY_TIME_RANGE:
                klass = IncrementalByTimeRange
            elif name == ModelKindName.INCREMENTAL_BY_UNIQUE_KEY:
                klass = IncrementalByUniqueKey
            else:
                props["name"] = ModelKindName(name)
            return klass(**props)

        if isinstance(v, dict):
            if v.get("name") == ModelKindName.INCREMENTAL_BY_TIME_RANGE:
                klass = IncrementalByTimeRange
            elif v.get("name") == ModelKindName.INCREMENTAL_BY_UNIQUE_KEY:
                klass = IncrementalByUniqueKey
            else:
                klass = ModelKind
            return klass(**v)

        name = v.name if isinstance(v, exp.Expression) else str(v)
        try:
            return ModelKind(name=ModelKindName(name))
        except ValueError:
            raise ConfigError(f"Invalid model kind '{name}'")

    @validator("dialect", "owner", "storage_format", "description", pre=True)
    def _string_validator(cls, v: t.Any) -> t.Optional[str]:
        if isinstance(v, exp.Expression):
            return v.name
        return str(v) if v is not None else None

    @validator("cron", pre=True)
    def _cron_validator(cls, v: t.Any) -> t.Optional[str]:
        cron = cls._string_validator(v)
        if cron:
            try:
                croniter(cron)
            except Exception:
                raise ConfigError(f"Invalid cron expression '{cron}'")
        return cron

    @validator("columns_to_types_", pre=True)
    def _columns_validator(cls, v: t.Any) -> t.Optional[t.Dict[str, exp.DataType]]:
        if isinstance(v, exp.Schema):
            return {column.name: column.args["kind"] for column in v.expressions}
        if isinstance(v, dict):
            return {
                k: maybe_parse(data_type, into=exp.DataType)  # type: ignore
                for k, data_type in v.items()
            }
        return v

    @validator("depends_on_", pre=True)
    def _depends_on_validator(cls, v: t.Any) -> t.Optional[t.Set[str]]:
        if isinstance(v, (exp.Array, exp.Tuple)):
            return {
                exp.table_name(table.name if table.is_string else table.sql())
                for table in v.expressions
            }
        if isinstance(v, exp.Expression):
            return {exp.table_name(v.sql())}
        return v

    @validator("start", pre=True)
    def _date_validator(cls, v: t.Any) -> t.Optional[TimeLike]:
        if isinstance(v, exp.Expression):
            v = v.name
        if not to_datetime(v):
            raise ConfigError(f"'{v}' not a valid date time")
        return v

    @validator("batch_size", pre=True)
    def _int_validator(cls, v: t.Any) -> t.Optional[int]:
        if not isinstance(v, exp.Expression):
            batch_size = int(v) if v is not None else None
        else:
            batch_size = int(v.name)
        if batch_size is not None and batch_size <= 0:
            raise ConfigError(
                f"Invalid batch size {batch_size}. The value should be greater than 0"
            )
        return batch_size

    @root_validator
    def _kind_validator(cls, values: t.Dict[str, t.Any]) -> t.Dict[str, t.Any]:
        kind = values.get("kind")
        if kind and not kind.is_materialized:
            if values.get("partitioned_by_"):
                raise ValueError(
                    f"partitioned_by field cannot be set for {kind} models"
                )
        return values

    @property
    def time_column(self) -> t.Optional[TimeColumn]:
        if isinstance(self.kind, IncrementalByTimeRange):
            return self.kind.time_column
        return None

    @property
    def partitioned_by(self) -> t.List[str]:
        time_column = [self.time_column.column] if self.time_column else []
        return unique([*time_column, *(self.partitioned_by_ or [])])

    def interval_unit(self, sample_size: int = 10) -> IntervalUnit:
        """Returns the IntervalUnit of the model

        The interval unit is used to determine the lag applied to start_date and end_date for model rendering and intervals.

        Args:
            sample_size: The number of samples to take from the cron to infer the unit.

        Returns:
            The IntervalUnit enum.
        """
        schedule = croniter(self.cron)
        samples = [schedule.get_next() for _ in range(sample_size)]
        min_interval = min(b - a for a, b in zip(samples, samples[1:]))
        if min_interval >= 86400:
            return IntervalUnit.DAY
        elif min_interval >= 3600:
            return IntervalUnit.HOUR
        return IntervalUnit.MINUTE

    def normalized_cron(self) -> str:
        """Returns the UTC normalized cron based on sampling heuristics.

        SQLMesh supports 3 interval units, daily, hourly, and minutes. If a job is scheduled
        daily at 1PM, the actual intervals are shifted back to midnight UTC.

        Returns:
            The cron string representing either daily, hourly, or minutes.
        """
        unit = self.interval_unit()
        if unit == IntervalUnit.MINUTE:
            return "* * * * *"
        if unit == IntervalUnit.HOUR:
            return "0 * * * *"
        if unit == IntervalUnit.DAY:
            return "0 0 * * *"
        return ""

    def croniter(self, value: TimeLike) -> croniter:
        if self._croniter is None:
            self._croniter = croniter(self.normalized_cron())
        self._croniter.set_current(to_datetime(value))
        return self._croniter

    def cron_next(self, value: TimeLike) -> TimeLike:
        """
        Get the next timestamp given a time-like value and the model's cron.

        Args:
            value: A variety of date formats.

        Returns:
            The timestamp for the next run.
        """
        return preserve_time_like_kind(value, self.croniter(value).get_next())

    def cron_prev(self, value: TimeLike) -> TimeLike:
        """
        Get the previous timestamp given a time-like value and the model's cron.

        Args:
            value: A variety of date formats.

        Returns:
            The timestamp for the previous run.
        """
        return preserve_time_like_kind(value, self.croniter(value).get_prev())

    def cron_floor(self, value: TimeLike) -> TimeLike:
        """
        Get the floor timestamp given a time-like value and the model's cron.

        Args:
            value: A variety of date formats.

        Returns:
            The timestamp floor.
        """
        return preserve_time_like_kind(
            value, self.croniter(self.cron_next(value)).get_prev()
        )