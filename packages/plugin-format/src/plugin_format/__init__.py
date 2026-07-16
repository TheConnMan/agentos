"""Claude Code plugin bundle format (verbatim) and its validators.

The plugin bundle format is the Claude Code plugin shape unchanged: compatibility
is the distribution wedge, so this package does not invent format extensions. It
is a frozen interface (see the package README); a change stops the task and
escalates to the orchestrator.
"""

from .archive import UnsupportedArchive, bundle_root, safe_extract
from .models import (
    ApprovalGate,
    ApprovalPolicy,
    Author,
    HookDefinition,
    HookMatcherConfig,
    McpConfig,
    McpServer,
    PluginManifest,
    SkillFrontmatter,
    TriggerDeclaration,
)
from .reserved_env import RESERVED_BOOT_ENV, is_reserved_boot_env_name
from .validate import ValidationIssue, ValidationResult, validate_bundle

__version__ = "0.0.0"

__all__ = [
    "__version__",
    "RESERVED_BOOT_ENV",
    "is_reserved_boot_env_name",
    "PluginManifest",
    "Author",
    "SkillFrontmatter",
    "McpServer",
    "McpConfig",
    "HookDefinition",
    "HookMatcherConfig",
    "TriggerDeclaration",
    "ApprovalPolicy",
    "ApprovalGate",
    "validate_bundle",
    "ValidationResult",
    "ValidationIssue",
    "safe_extract",
    "bundle_root",
    "UnsupportedArchive",
]
