"""Lightweight catalog-only fingerprint queries (L1 schema probe).

These touch only sys.* catalog views — never table data — so a full probe
costs a few milliseconds even on large databases. Column type / length /
precision / nullability changes are captured by the column checksum;
index changes (which do not bump a table's modify_date) by the index
checksum; view/procedure/function body changes by a hash of the module
definition (HASHBYTES over nvarchar(max) requires SQL Server 2016+,
already implied by STRING_AGG usage elsewhere).
"""

PROBE_COLUMN_FINGERPRINTS = """
SELECT
    c.object_id,
    CHECKSUM_AGG(BINARY_CHECKSUM(
        c.column_id, c.system_type_id, c.max_length,
        c.precision, c.scale, c.is_nullable, c.collation_name
    )) AS col_fp,
    COUNT(*) AS col_count
FROM sys.columns c
GROUP BY c.object_id
"""

PROBE_INDEX_FINGERPRINTS = """
SELECT
    i.object_id,
    CHECKSUM_AGG(BINARY_CHECKSUM(
        i.index_id, i.is_unique, i.is_primary_key,
        ic.column_id, ic.key_ordinal, ic.is_included_column
    )) AS idx_fp,
    COUNT(*) AS idx_count
FROM sys.indexes i
JOIN sys.index_columns ic
    ON ic.object_id = i.object_id AND ic.index_id = i.index_id
GROUP BY i.object_id
"""

PROBE_TABLES = """
SELECT
    t.object_id,
    s.name AS schema_name,
    t.name AS table_name,
    CONVERT(varchar(33), t.modify_date, 126) AS modify_date
FROM sys.tables t
JOIN sys.schemas s ON s.schema_id = t.schema_id
"""

PROBE_MODULES = """
SELECT
    o.object_id,
    s.name AS schema_name,
    o.name AS object_name,
    RTRIM(o.type) AS type_code,
    CONVERT(varchar(33), o.modify_date, 126) AS modify_date,
    CONVERT(varchar(64), HASHBYTES('SHA2_256', m.definition), 2) AS def_hash
FROM sys.objects o
JOIN sys.schemas s ON s.schema_id = o.schema_id
LEFT JOIN sys.sql_modules m ON m.object_id = o.object_id
WHERE o.type IN ('V','P','FN','IF','TF')
"""

PROBE_DEPENDENCIES = """
SELECT DISTINCT d.referencing_id, d.referenced_id
FROM sys.sql_expression_dependencies d
WHERE d.referenced_id IS NOT NULL
"""
