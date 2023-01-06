from __future__ import annotations

import copy
from typing import Any
from uuid import uuid4

from delta import DeltaTable
from pyspark.sql import DataFrame
from pyspark.sql import functions as F
from pyspark.sql import Window

from hydro import _humanize_bytes
from hydro import _humanize_number


def scd(
    delta_table: DeltaTable,
    source: DataFrame,
    keys: list[str] | str,
    effective_ts: str,
    end_ts: str = None,
    scd_type: int = 2,
) -> DeltaTable:
    """

    :param delta_table:
    :param source:
    :param keys:
    :param effective_ts:
    :param end_ts:
    :param scd_type:
    :return:
    """
    if isinstance(keys, str):  # pragma: no cover
        keys = [keys]

    def _scd2(
        delta_table: DeltaTable,
        source: DataFrame,
        keys: list[str] | str,
        effective_ts: str,
        end_ts: str,
    ):
        """

        :param delta_table:
        :param source:
        :param keys:
        :param effective_ts:
        :param end_ts:
        :return:
        """
        if not end_ts:
            raise ValueError(
                '`end_ts` parameter not provided, type 2 scd requires this',
            )

        updated_rows = (
            delta_table.toDF()
            .join(source, keys, 'left_semi')
            .filter(F.col(end_ts).isNull())
        )
        combined_rows = updated_rows.unionByName(source, allowMissingColumns=True)
        window = Window.partitionBy(keys).orderBy(effective_ts)
        final_payload = combined_rows.withColumn(
            end_ts,
            F.lead(effective_ts).over(window),
        )
        merge_keys = keys + [effective_ts]
        merge_key_condition = ' AND '.join(
            [f'source.{key} = target.{key}' for key in merge_keys],
        )
        delta_table.alias('target').merge(
            final_payload.alias('source'),
            merge_key_condition,
        ).whenMatchedUpdateAll().whenNotMatchedInsertAll().execute()
        return delta_table

    def _scd1(
        delta_table: DeltaTable,
        source: DataFrame,
        keys: list[str] | str,
        effective_ts: str,
    ):
        window = Window.partitionBy(keys).orderBy(F.col(effective_ts).desc())
        row_number_uuid = uuid4().hex  # avoid column collisions by using uuid
        final_payload = (
            source.withColumn(
                row_number_uuid,
                F.row_number().over(window),
            )
            .filter(F.col(row_number_uuid) == 1)
            .drop(row_number_uuid)
        )
        merge_key_condition = ' AND '.join(
            [f'source.{key} = target.{key}' for key in keys],
        )
        delta_table.alias('target').merge(
            final_payload.alias('source'),
            merge_key_condition,
        ).whenMatchedUpdateAll().whenNotMatchedInsertAll().execute()
        return delta_table

    if scd_type == 2:
        return _scd2(delta_table, source, keys, effective_ts, end_ts)
    elif scd_type == 1:
        return _scd1(delta_table, source, keys, effective_ts)
    else:
        raise ValueError('`scd_type` not of (1,2)')


def bootstrap_scd2(
    source_df: DataFrame,
    keys: list[str] | str,
    effective_ts: str,
    end_ts: str,
    table_properties: dict[str, str] = None,
    partition_columns: list[str] = [],
    comment: str = None,
    path: str = None,
    table_identifier: str = None,
) -> DeltaTable:
    """

    :param source_df:
    :param keys:
    :param effective_ts:
    :param end_ts:
    :param table_properties:
    :param partition_columns:
    :param comment:
    :param path:
    :param table_identifier:
    :return:
    """
    if not path and not table_identifier:
        raise ValueError(
            'Need to specify one (or both) of `path` and `table_identifier`',
        )
    window = Window.partitionBy(keys).orderBy(effective_ts)
    final_payload = source_df.withColumn(
        end_ts,
        F.lead(effective_ts).over(window),
    )

    builder = DeltaTable.createOrReplace(
        source_df.sparkSession,
    )  # TODO change to createIfNotExists?
    if table_properties:  # pragma: no cover
        for k, v in table_properties.items():
            builder = builder.property(k, v)
    builder = builder.addColumns(source_df.schema)
    builder = builder.partitionedBy(*partition_columns)
    if comment:  # pragma: no cover
        builder = builder.comment(comment)
    if path:
        builder = builder.location(path)
    if table_identifier:
        builder = builder.tableName(table_identifier)
    builder.execute()
    delta_table = None
    if table_identifier:
        final_payload.write.format('delta').option('mergeSchema', 'true').mode(
            'append',
        ).saveAsTable(table_identifier)
        delta_table = DeltaTable.forName(source_df.sparkSession, table_identifier)
    elif path:  # pragma: no cover
        # literally no idea why coverage is failing here
        final_payload.write.format('delta').option('mergeSchema', 'true').mode(
            'append',
        ).save(path)
        delta_table = DeltaTable.forPath(source_df.sparkSession, path)
    return delta_table


def deduplicate(
    delta_table: DeltaTable,
    temp_path: str,
    keys: list[str] | str,
    tiebreaking_columns: list[str] = None,
) -> DeltaTable:
    """
    Removes duplicates from an existing Delta Lake table.

    :param delta_table: The target Delta table that contains duplicates.
    :param temp_path: A temporary location
    :param keys: A list of column names used to distinguish rows. The order of this list does not matter.
    :param tiebreaking_columns: A list of column names used for ordering. The order of this list matters, with earlier elements weighing more than lesser ones.
    :return: The same Delta table as **delta_table**
    """
    if tiebreaking_columns is None:
        tiebreaking_columns = []

    if isinstance(keys, str):  # pragma: no cover
        keys = [keys]
    detail_object = detail(delta_table)
    target_location = detail_object['location']
    count_col = uuid4().hex
    df = delta_table.toDF()
    spark = df.sparkSession
    window = Window.partitionBy(keys)

    dupes = (
        df.withColumn(count_col, F.count('*').over(window))
        .filter(F.col(count_col) > 1)
        .drop(count_col)
    )
    if tiebreaking_columns:
        row_number_col = uuid4().hex
        tiebreaking_desc = [F.col(col).desc() for col in tiebreaking_columns]
        window = window.orderBy(tiebreaking_desc)
        deduped = (
            dupes.withColumn(row_number_col, F.row_number().over(window))
            .filter(F.col(row_number_col) == 1)
            .drop(row_number_col)
        )
        deduped.write.format('delta').save(
            temp_path,
        )
    else:
        dupes.drop_duplicates(keys).write.format('delta').save(
            temp_path,
        )
    merge_key_condition = ' AND '.join(
        [f'source.{key} = target.{key}' for key in keys],
    )
    delta_table.alias('target').merge(
        dupes.select(keys).distinct().alias('source'),
        merge_key_condition,
    ).whenMatchedDelete().execute()
    spark.read.format('delta').load(temp_path).write.format('delta').mode(
        'append',
    ).save(target_location)
    return delta_table


def partial_update_set(
    fields: list[str],
    source_alias: str,
    target_alias: str,
) -> F.col:
    """

    :param fields:
    :param source_alias:
    :param target_alias:
    :return:
    """
    return {
        field: F.coalesce(f'{source_alias}.{field}', f'{target_alias}.{field}')
        for field in fields
    }


def partition_stats(delta_table: DeltaTable) -> DataFrame:
    """

    :param delta_table:
    :return:
    """
    allfiles = _snapshot_allfiles(delta_table)
    detail = DetailOutput(delta_table)
    partition_columns = [f'partitionValues.{col}' for col in detail.partition_columns]
    return allfiles.groupBy(*partition_columns).agg(
        F.sum('size').alias('total_bytes'),
        F.percentile_approx('size', [0, 0.25, 0.5, 0.75, 1.0]).alias('bytes_quantiles'),
        F.sum(F.get_json_object('stats', '$.numRecords')).alias('num_records'),
        F.count('*').alias('num_files'),
    )


def get_table_zordering(delta_table: DeltaTable) -> DataFrame:
    """

    :param delta_table:
    :return:
    """
    return (
        delta_table.history()
        .filter("operation == 'OPTIMIZE'")
        .filter('operationParameters.zOrderBy IS NOT NULL')
        .select('operationParameters.zOrderBy')
        .groupBy('zOrderBy')
        .count()
    )


def detail(delta_table: DeltaTable) -> dict[Any, Any]:
    """

    :param delta_table:
    :return:
    """
    detail_output = DetailOutput(delta_table)
    detail_output.humanize()
    return detail_output.to_dict()


def detail_enhanced(delta_table: DeltaTable) -> dict[Any, Any]:
    """

    :param delta_table:
    :return:
    """
    details = detail(delta_table)
    allfiles = _snapshot_allfiles(delta_table)
    partition_columns = [
        f'partitionValues.{col}' for col in details['partition_columns']
    ]

    num_records = (
        allfiles.select(
            F.get_json_object('stats', '$.numRecords').alias('num_records'),
        )
        .agg(F.sum('num_records').alias('num_records'))
        .collect()[0]['num_records']
    )
    details['numRecords'] = _humanize_number(num_records)

    stats_percentage = (
        allfiles.agg(
            F.avg(
                F.when(F.col('stats').isNotNull(), F.lit(1)).otherwise(F.lit(0)),
            ).alias('stats_percentage'),
        )
    ).collect()[0]['stats_percentage']
    details['stats_percentage'] = stats_percentage * 100

    partition_count = allfiles.select(*partition_columns).distinct().count()
    details['partition_count'] = _humanize_number(partition_count)
    return details


def _snapshot_allfiles(delta_table: DeltaTable) -> DataFrame:
    # this is kinda hacky but oh well
    spark = delta_table.toDF().sparkSession
    location = delta_table.detail().collect()[0]['location']

    delta_log = spark._jvm.org.apache.spark.sql.delta.DeltaLog.forTable(
        spark._jsparkSession,
        location,
    )
    return DataFrame(delta_log.snapshot().allFiles(), spark)


class DetailOutput:
    def __init__(self, delta_table: DeltaTable):
        detail_output = delta_table.detail().collect()[0].asDict()
        self.created_at = detail_output['createdAt']
        self.description: str = detail_output['description']
        self.format = detail_output['format']
        self.id = detail_output['id']
        self.last_modified = detail_output['lastModified']
        self.location = detail_output['location']
        self.min_reader_version = detail_output['minReaderVersion']
        self.min_writer_version = detail_output['minWriterVersion']
        self.name: str = detail_output['name']
        self.num_files = detail_output['numFiles']
        self.partition_columns = detail_output['partitionColumns']
        self.properties = detail_output['properties']
        self.size = detail_output['sizeInBytes']

    def humanize(self):
        self.num_files = _humanize_number(self.num_files)
        self.size = _humanize_bytes(self.size)

    def to_dict(self):
        return copy.deepcopy(self.__dict__)