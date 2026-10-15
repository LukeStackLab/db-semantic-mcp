GET_OBJECT_DEFINITION = """
SELECT OBJECT_DEFINITION(OBJECT_ID(%s))
"""

GET_OBJECT_DEPENDENCIES = """
SELECT
    OBJECT_SCHEMA_NAME(d.referenced_id) AS REF_SCHEMA,
    OBJECT_NAME(d.referenced_id)        AS REF_NAME,
    o.type_desc                         AS REF_TYPE
FROM sys.sql_expression_dependencies d
LEFT JOIN sys.objects o ON d.referenced_id = o.object_id
WHERE d.referencing_id = OBJECT_ID(%s)
  AND d.referenced_id IS NOT NULL
ORDER BY REF_SCHEMA, REF_NAME
"""
