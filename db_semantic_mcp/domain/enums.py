from enum import Enum


class RiskLevel(str, Enum):
    LOW = "low"
    MEDIUM = "medium"
    HIGH = "high"
    CRITICAL = "critical"


class SqlOperation(str, Enum):
    SELECT = "SELECT"
    INSERT = "INSERT"
    UPDATE = "UPDATE"
    DELETE = "DELETE"
    TRUNCATE = "TRUNCATE"
    CREATE = "CREATE"
    ALTER = "ALTER"
    DROP = "DROP"
    EXECUTE = "EXECUTE"
    EXEC = "EXEC"
    MERGE = "MERGE"
    UNKNOWN = "UNKNOWN"
