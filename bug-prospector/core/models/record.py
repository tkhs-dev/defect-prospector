from dataclasses import dataclass


@dataclass
class Record:
    id: int
    project_id: str
    buggy_version: str
    fixed_version: str