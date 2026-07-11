"""The OpenCode harness :class:`BundleInstaller`: an explicit, documented no-op.

The OpenCode spike has no bundle-to-config compiler yet, so its session runs from
a bare temp dir with no plugin bundle. This stub exists so that bundle-less
behavior is an explicit decision made at the ``BundleInstaller`` port
(Decision 3 of the harness-neutral runner seams ADR, PR #306) rather than an
accident of the spike never wiring a bundle in: the
installer always contributes an empty (``None``) OpenCode session config, and
warns when a bundle is configured so an operator is not silently surprised that
their bundle was ignored. The real bundle-to-OpenCode-config compiler is issue
#310.
"""

from __future__ import annotations

import logging

logger = logging.getLogger(__name__)


class OpenCodeBundleInstaller:
    """Return the OpenCode harness's empty session config; today always ``None``.

    A truthy ``plugin_dir`` is honored as a no-op with a warning: the OpenCode
    harness does not yet support bundles, so the session runs bundle-less until
    the compiler (issue #310) lands.
    """

    def install(self, plugin_dir: str | None) -> None:
        if plugin_dir:
            logger.warning(
                "plugin bundle configured at %s but the OpenCode harness does not "
                "yet support bundles; the session runs bundle-less (compiler is "
                "issue #310)",
                plugin_dir,
            )
        return None
