from __future__ import annotations

from dataclasses import dataclass, field


@dataclass
class DependencyInfo:
    name: str
    version: str | None = None
    source: str | None = None

    @classmethod
    def from_dict(cls, d: dict) -> DependencyInfo:
        return cls(name=d["name"], version=d.get("version"), source=d.get("source"))


@dataclass
class EnvironmentInfo:
    language: str | None = None
    language_version: str | None = None
    dependencies: list[DependencyInfo] = field(default_factory=list)
    build_commands: list[str] = field(default_factory=list)
    test_commands: list[str] = field(default_factory=list)
    os_hint: str | None = None

    @classmethod
    def from_dict(cls, d: dict) -> EnvironmentInfo:
        return cls(
            language=d.get("language"),
            language_version=d.get("language_version"),
            dependencies=[DependencyInfo.from_dict(x) for x in d.get("dependencies", [])],
            build_commands=d.get("build_commands", []),
            test_commands=d.get("test_commands", []),
            os_hint=d.get("os_hint"),
        )
