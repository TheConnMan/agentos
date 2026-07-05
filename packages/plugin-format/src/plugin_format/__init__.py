"""Claude Code plugin bundle format (verbatim) and its validators.

The plugin bundle format is the Claude Code plugin shape unchanged: compatibility
is the distribution wedge, so this package does not invent format extensions. It
is a frozen interface (see the package README); a change stops the task and
escalates to the orchestrator.
"""

from .models import (
    Author,
    McpConfig,
    McpServer,
    PluginManifest,
    SkillFrontmatter,
)
from .validate import ValidationIssue, ValidationResult, validate_bundle

__version__ = "0.0.0"

__all__ = [
    "__version__",
    "PluginManifest",
    "Author",
    "SkillFrontmatter",
    "McpServer",
    "McpConfig",
    "validate_bundle",
    "ValidationResult",
    "ValidationIssue",
]
