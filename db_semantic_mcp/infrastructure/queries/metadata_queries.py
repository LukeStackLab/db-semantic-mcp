GET_TABLES = """
SELECT TABLE_SCHEMA, TABLE_NAME
FROM INFORMATION_SCHEMA.TABLES
WHERE TABLE_TYPE = 'BASE TABLE'
ORDER BY TABLE_SCHEMA, TABLE_NAME
"""

GET_COLUMNS = """
SELECT
    TABLE_SCHEMA, TABLE_NAME, COLUMN_NAME,
    DATA_TYPE, CHARACTER_MAXIMUM_LENGTH,
    CASE WHEN IS_NULLABLE = 'YES' THEN 1 ELSE 0 END AS IS_NULLABLE,
    COLUMN_DEFAULT, ORDINAL_POSITION,
    NUMERIC_PRECISION, NUMERIC_SCALE, DATETIME_PRECISION, COLLATION_NAME
FROM INFORMATION_SCHEMA.COLUMNS
ORDER BY TABLE_SCHEMA, TABLE_NAME, ORDINAL_POSITION
"""

GET_PRIMARY_KEYS = """
SELECT tc.TABLE_SCHEMA, tc.TABLE_NAME, kcu.COLUMN_NAME
FROM INFORMATION_SCHEMA.TABLE_CONSTRAINTS tc
JOIN INFORMATION_SCHEMA.KEY_COLUMN_USAGE kcu
    ON tc.CONSTRAINT_NAME = kcu.CONSTRAINT_NAME
    AND tc.TABLE_SCHEMA = kcu.TABLE_SCHEMA
WHERE tc.CONSTRAINT_TYPE = 'PRIMARY KEY'
ORDER BY tc.TABLE_SCHEMA, tc.TABLE_NAME, kcu.ORDINAL_POSITION
"""

GET_FOREIGN_KEYS = """
SELECT
    ps.name, pt.name, pc.name,
    rs.name AS REF_SCHEMA, rt.name AS REF_TABLE, rc.name AS REF_COLUMN,
    fk.object_id AS CONSTRAINT_ID, fk.name AS CONSTRAINT_NAME,
    fkc.constraint_column_id,
    CAST(fk.is_disabled AS INT), CAST(fk.is_not_trusted AS INT)
FROM sys.foreign_keys fk
JOIN sys.foreign_key_columns fkc ON fkc.constraint_object_id = fk.object_id
JOIN sys.tables pt ON pt.object_id = fkc.parent_object_id
JOIN sys.schemas ps ON ps.schema_id = pt.schema_id
JOIN sys.columns pc
    ON pc.object_id = fkc.parent_object_id AND pc.column_id = fkc.parent_column_id
JOIN sys.tables rt ON rt.object_id = fkc.referenced_object_id
JOIN sys.schemas rs ON rs.schema_id = rt.schema_id
JOIN sys.columns rc
    ON rc.object_id = fkc.referenced_object_id AND rc.column_id = fkc.referenced_column_id
ORDER BY ps.name, pt.name, fk.object_id, fkc.constraint_column_id
"""

GET_INDEXES = """
SELECT
    s.name AS SCHEMA_NAME,
    t.name AS TABLE_NAME,
    i.name AS INDEX_NAME,
    CAST(CASE WHEN MIN(ic.key_ordinal) > 0 THEN i.is_unique ELSE 0 END AS INT),
    CAST(i.is_primary_key AS INT),
    CONCAT(N'[', STRING_AGG(N'"' + STRING_ESCAPE(c.name, 'json') + N'"', N',')
        WITHIN GROUP (ORDER BY ic.key_ordinal), N']') AS COLS,
    CAST(i.has_filter AS INT), CAST(i.is_disabled AS INT)
FROM sys.indexes i
JOIN sys.tables t  ON i.object_id = t.object_id
JOIN sys.schemas s ON t.schema_id = s.schema_id
JOIN sys.index_columns ic
    ON i.object_id = ic.object_id AND i.index_id = ic.index_id
JOIN sys.columns c
    ON ic.object_id = c.object_id AND ic.column_id = c.column_id
WHERE i.name IS NOT NULL AND ic.is_included_column = 0
    AND (ic.key_ordinal > 0 OR i.type IN (3, 4))
GROUP BY s.name, t.name, i.name, i.is_unique, i.is_primary_key,
         i.has_filter, i.is_disabled
ORDER BY s.name, t.name, i.name
"""

GET_OBJECTS = """
SELECT ROUTINE_SCHEMA, ROUTINE_NAME, ROUTINE_TYPE
FROM INFORMATION_SCHEMA.ROUTINES
UNION ALL
SELECT TABLE_SCHEMA, TABLE_NAME, 'VIEW'
FROM INFORMATION_SCHEMA.VIEWS
ORDER BY 1, 2, 3
"""
