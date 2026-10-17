"""Validated read-only conversational tools for drive Q&A."""

from __future__ import annotations

from dataclasses import dataclass

from .local_database import DriveIndex
from .retrieval import ScanEvidence


@dataclass(frozen=True)
class ToolSpec:
    required: tuple[str, ...]
    optional: tuple[str, ...] = ()


class ToolRegistry:
    """Executes only approved read-only tools with strict argument validation."""

    _SPECS: dict[str, ToolSpec] = {
        "search_names": ToolSpec(("query",), ("scope", "limit")),
        "search_content": ToolSpec(("query",), ("scope", "limit")),
        "inspect_path": ToolSpec(("path",)),
        "list_children": ToolSpec(("path",), ("limit",)),
        "summarize_folder": ToolSpec(("path",)),
        "find_large_files": ToolSpec((), ("scope", "limit")),
        "retrieve_context": ToolSpec(("query",), ("scope", "limit")),
        "impact_analysis": ToolSpec(("path", "action")),
    }

    def __init__(self, evidence: ScanEvidence | None, index: DriveIndex | None) -> None:
        self.evidence = evidence
        self.index = index

    def validate(self, name: str, arguments: dict) -> None:
        if name not in self._SPECS:
            raise ValueError(f"Unknown tool: {name}")
        spec = self._SPECS[name]
        missing = [key for key in spec.required if key not in arguments]
        if missing:
            raise ValueError(f"Missing required arguments for {name}: {', '.join(missing)}")
        allowed = set(spec.required) | set(spec.optional)
        extra = [key for key in arguments if key not in allowed]
        if extra:
            raise ValueError(f"Unexpected arguments for {name}: {', '.join(extra)}")

    @staticmethod
    def _limit(value: object, default: int = 8) -> int:
        if value is None:
            return default
        try:
            return max(1, min(int(value), 50))
        except (TypeError, ValueError):
            return default

    def call(self, name: str, arguments: dict) -> dict:
        self.validate(name, arguments)

        if name == "inspect_path":
            return self.evidence.inspect(arguments["path"]) if self.evidence else {"error": "No scan loaded."}

        if name == "list_children":
            if self.index:
                return {"items": self.index.list_children(arguments["path"], limit=self._limit(arguments.get("limit"), 25))}
            if self.evidence:
                scoped = self.evidence.scoped_items(arguments["path"], self._limit(arguments.get("limit"), 8))
                return {"items": scoped.get("items", [])}
            return {"items": []}

        if name == "summarize_folder":
            if self.index:
                return self.index.summarize_folder(arguments["path"])
            if self.evidence:
                return self.evidence.folder_summary(arguments["path"])
            return {"error": "No scan loaded."}

        if name == "find_large_files":
            if self.index:
                return {"items": self.index.find_large_files(scope=arguments.get("scope"), limit=self._limit(arguments.get("limit"), 8))}
            if self.evidence:
                return self.evidence.largest("files", self._limit(arguments.get("limit"), 8), scope=arguments.get("scope"))
            return {"items": []}

        if name == "search_names":
            if self.index:
                return {"items": self.index.search_names(arguments["query"], scope=arguments.get("scope"), limit=self._limit(arguments.get("limit"), 8))}
            if self.evidence:
                return self.evidence.search(arguments["query"], self._limit(arguments.get("limit"), 8))
            return {"items": []}

        if name == "search_content":
            if not self.index:
                return {"items": [], "warning": "Content index is not available."}
            return {"items": self.index.search_content(arguments["query"], scope=arguments.get("scope"), limit=self._limit(arguments.get("limit"), 8))}

        if name == "retrieve_context":
            if self.index:
                return self.index.retrieve_context(arguments["query"], scope=arguments.get("scope"), limit=self._limit(arguments.get("limit"), 8))
            return {"items": [], "content_matches": [], "citations": []}

        if name == "impact_analysis":
            if self.index:
                return self.index.impact_analysis(arguments["path"], arguments["action"])
            return {"path": arguments["path"], "action": arguments["action"], "read_only": True, "risk": "unknown"}

        raise ValueError(f"Unsupported tool: {name}")
