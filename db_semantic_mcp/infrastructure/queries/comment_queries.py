GET_COMMENTS = """
SELECT
    s.name AS SCHEMA_NAME,
    o.name AS OBJECT_NAME,
    COALESCE(c.name, '') AS COLUMN_NAME,
    CAST(ep.value AS NVARCHAR(MAX)) AS DESCRIPTION
FROM sys.extended_properties ep
JOIN sys.objects  o ON ep.major_id = o.object_id
JOIN sys.schemas  s ON o.schema_id = s.schema_id
LEFT JOIN sys.columns c
    ON ep.major_id = c.object_id AND ep.minor_id = c.column_id
WHERE ep.name = 'MS_Description' AND ep.class = 1
ORDER BY s.name, o.name, ep.minor_id
"""

GET_OBJECT_DEFINITION = """
SELECT OBJECT_DEFINITION(OBJECT_ID(%s))
"""
