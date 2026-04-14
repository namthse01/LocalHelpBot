"""
Plugin package — every module here is auto-loaded by
`core.tool_schema.load_plugins()` during registry construction.

Contract:  each plugin module must expose
    def register(registry: ToolRegistry) -> None:
        registry.register(Tool(...))

Plugins may freely import from `core.tools`, `core.permissions`, etc.
Drop a new .py file in this folder and it becomes available to every
agent whose profile lists the tool name.
"""
