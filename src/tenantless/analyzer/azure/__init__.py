"""Azure Resource Graph (ARG) direct-tenant ingestion subpackage.

The ONLY azure-specific code in the analyzer (D-12): authentication, paging,
projection, and normalization of ARG rows into the existing scan schema. Every
module here that does NOT wrap the live SDK (``executor`` seam, the pure
materializer) imports nothing from ``azure-*`` and runs under the core CI
install; only the real client wrapper imports the optional ``azure`` extra,
behind a guarded import.
"""

from __future__ import annotations
