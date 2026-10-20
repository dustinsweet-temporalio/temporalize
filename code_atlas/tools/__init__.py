"""
All agent tools organized by category.
"""
from .navigation import ALL_NAVIGATION_TOOLS
from .search import ALL_SEARCH_TOOLS
from .source import ALL_SOURCE_TOOLS
from .system import ALL_SYSTEM_TOOLS
from .report import ALL_REPORT_TOOLS

# Complete tool registry for the agent
ALL_TOOLS = [
    *ALL_NAVIGATION_TOOLS,  # 5 tools: folder, file, list repos, repo summary, dir listing
    *ALL_SEARCH_TOOLS,      # 1 tool: text search
    *ALL_SOURCE_TOOLS,      # 4 tools: 2 source store (primary) + 2 manual (last resort)
    *ALL_SYSTEM_TOOLS,      # 1 tool: version
    *ALL_REPORT_TOOLS,      # 3 tools: the stored Temporal report, its business impact, Temporal docs search
]

__all__ = [
    'ALL_TOOLS',
    'ALL_NAVIGATION_TOOLS',
    'ALL_SEARCH_TOOLS',
    'ALL_SOURCE_TOOLS',
    'ALL_SYSTEM_TOOLS',
    'ALL_REPORT_TOOLS',
]
