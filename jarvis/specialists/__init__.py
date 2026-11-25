"""Agents: specialised, importable agent plugins (docs/agent-plugins.md).

    manifest.py      agent.toml parsing and validation
    records.py       the tables an agent fills (JSON per table)
    templating.py    action prompts and export templates
    plugin_tools.py  records tools and a plugin's own tools/*.py
    packaging.py     zip / GitHub / folder import and zip export
    manager.py       AgentManager: everything above, per installed agent
"""

from .manager import AgentError, AgentManager, Plugin
from .manifest import ManifestError, load_manifest
from .packaging import ImportProblem

__all__ = ["AgentManager", "AgentError", "Plugin", "ManifestError", "ImportProblem", "load_manifest"]
