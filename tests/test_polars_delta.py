import shutil
from typing import Dict

import polars as pl
import polars.testing as pl_testing
from dagster import OpExecutionContext, StaticPartitionsDefinition, asset, materialize
from deltalake import DeltaTable
from hypothesis import given, settings
from polars.testing.parametric import dataframes

from dagster_polars import PolarsDeltaIOManager

# TODO: remove pl.Time once it's supported
# TODO: remove pl.Duration pl.Duration once it's supported
# https://github.com/pola-rs/polars/issues/9631
# TODO: remove UInt types once they are fixed:
#  https://github.com/pola-rs/polars/issues/9627


@given(
    df=dataframes(
        excluded_dtypes=[
            pl.Categorical,
            pl.Duration,
            pl.Time,
            pl.UInt8,
            pl.UInt16,
            pl.UInt32,
            pl.UInt64,
            pl.Datetime("ns", None),
        ],
        min_size=5,
        allow_infinities=False,
    )
)
@settings(max_examples=500, deadline=None)
def test_polars_delta_io_manager(session_polars_delta_io_manager: PolarsDeltaIOManager, df: pl.DataFrame):
    @asset(io_manager_def=session_polars_delta_io_manager, metadata={"overwrite_schema": True})
    def upstream() -> pl.DataFrame:
        return df

    @asset(io_manager_def=session_polars_delta_io_manager, metadata={"overwrite_schema": True})
    def downstream(upstream: pl.LazyFrame) -> pl.DataFrame:
        return upstream.collect(streaming=True)

    result = materialize(
        [upstream, downstream],
    )

    handled_output_events = list(filter(lambda evt: evt.is_handled_output, result.events_for_node("upstream")))

    saved_path = handled_output_events[0].event_specific_data.metadata["path"].value  # type: ignore[index,union-attr]
    assert isinstance(saved_path, str)
    pl_testing.assert_frame_equal(df, pl.read_delta(saved_path))
    shutil.rmtree(saved_path)  # cleanup manually because of hypothesis


def test_polars_delta_io_manager_append(polars_delta_io_manager: PolarsDeltaIOManager):
    df = pl.DataFrame(
        {
            "a": [1, 2, 3],
        }
    )

    @asset(io_manager_def=polars_delta_io_manager, metadata={"mode": "append"})
    def append_asset() -> pl.DataFrame:
        return df

    result = materialize(
        [append_asset],
    )

    handled_output_events = list(filter(lambda evt: evt.is_handled_output, result.events_for_node("append_asset")))
    saved_path = handled_output_events[0].event_specific_data.metadata["path"].value  # type: ignore[index,union-attr]
    assert isinstance(saved_path, str)

    materialize(
        [append_asset],
    )

    pl_testing.assert_frame_equal(pl.concat([df, df]), pl.read_delta(saved_path))


def test_polars_delta_io_manager_overwrite_schema(polars_delta_io_manager: PolarsDeltaIOManager):
    @asset(io_manager_def=polars_delta_io_manager)
    def overwrite_schema_asset() -> pl.DataFrame:  # type: ignore
        return pl.DataFrame(
            {
                "a": [1, 2, 3],
            }
        )

    result = materialize(
        [overwrite_schema_asset],
    )

    handled_output_events = list(
        filter(lambda evt: evt.is_handled_output, result.events_for_node("overwrite_schema_asset"))
    )
    saved_path = handled_output_events[0].event_specific_data.metadata["path"].value  # type: ignore[index,union-attr]
    assert isinstance(saved_path, str)

    @asset(io_manager_def=polars_delta_io_manager, metadata={"overwrite_schema": True, "mode": "overwrite"})
    def overwrite_schema_asset() -> pl.DataFrame:
        return pl.DataFrame(
            {
                "b": ["1", "2", "3"],
            }
        )

    materialize(
        [overwrite_schema_asset],
    )

    pl_testing.assert_frame_equal(
        pl.DataFrame(
            {
                "b": ["1", "2", "3"],
            }
        ),
        pl.read_delta(saved_path),
    )


def test_polars_delta_native_partitioning(polars_delta_io_manager: PolarsDeltaIOManager, df_for_delta: pl.DataFrame):
    manager = polars_delta_io_manager
    df = df_for_delta

    partitions_def = StaticPartitionsDefinition(["a", "b"])

    @asset(io_manager_def=manager, partitions_def=partitions_def, metadata={"partition_by": "partition"})
    def upstream_partitioned(context: OpExecutionContext) -> pl.DataFrame:
        return df.with_columns(pl.lit(context.partition_key).alias("partition"))

    @asset(io_manager_def=manager)
    def downstream_load_multiple_partitions(upstream_partitioned: Dict[str, pl.LazyFrame]) -> None:
        for _df in upstream_partitioned.values():
            assert isinstance(_df, pl.LazyFrame), type(_df)
        assert set(upstream_partitioned.keys()) == {"a", "b"}, upstream_partitioned.keys()

    for partition_key in ["a", "b"]:
        result = materialize(
            [upstream_partitioned],
            partition_key=partition_key,
        )

        handled_output_events = list(
            filter(lambda evt: evt.is_handled_output, result.events_for_node("upstream_partitioned"))
        )
        saved_path = handled_output_events[0].event_specific_data.metadata["path"].value  # type: ignore
        assert isinstance(saved_path, str)
        assert saved_path.endswith("upstream_partitioned.delta"), saved_path  # DeltaLake should handle partitioning!
        assert DeltaTable(saved_path).metadata().partition_columns == ["partition"]

    materialize(
        [
            upstream_partitioned.to_source_asset(),
            downstream_load_multiple_partitions,
        ],
    )
