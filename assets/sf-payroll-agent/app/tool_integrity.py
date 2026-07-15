"""Tool Content Hashing — agent-os ContentHashInterceptor integration.

Implements the tool-identity-via-content-hashing pattern from:
https://microsoft.github.io/agent-governance-toolkit/packages/agent-os/#tool-content-hashing

Why this matters
----------------
Policy rules guard tool *names*. An attacker (or a compromised dependency)
could register a wrapper function under the same name as a legitimate tool,
bypassing all name-based policy checks. Content hashing defeats this:

  1. At startup, every write tool's source code is SHA-256 hashed and stored
     in a manifest (``governance/tool-hashes.json``).
  2. Before each write-tool call, the *current* hash is recomputed and compared
     against the manifest. If it differs, the call is blocked.
  3. If ``agent-governance-toolkit-core`` is installed, the real
     ``ContentHashInterceptor`` from ``agent_os.integrations.base`` is used.
     Otherwise, a self-contained fallback is used — same semantics, no dep.

Usage (called from agent_executor.py)
--------------------------------------
    from tool_integrity import build_tool_manifest, verify_tool_integrity

    # At startup: generate manifest from current source
    manifest = build_tool_manifest(tools)

    # Per call: verify before executing
    ok, reason = verify_tool_integrity(tool, manifest)
    if not ok:
        raise GovernanceDenied(reason)
"""

from __future__ import annotations

import hashlib
import inspect
import json
import logging
import marshal
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

# Path where the tool-hash manifest is written at startup
_MANIFEST_PATH = Path(__file__).parent.parent / "governance" / "tool-hashes.json"

# Try to use the real ContentHashInterceptor from agent_os
_AGENT_OS_AVAILABLE = False
_ContentHashInterceptor: Any = None

try:
    from agent_os.integrations.base import ContentHashInterceptor as _CHI  # type: ignore[import-untyped]
    _ContentHashInterceptor = _CHI
    _AGENT_OS_AVAILABLE = True
    logger.info("tool_integrity: agent_os.ContentHashInterceptor loaded")
except ImportError:
    logger.info(
        "tool_integrity: agent_os not installed — using built-in hash implementation"
    )


# ── Hash computation ──────────────────────────────────────────────────────────

def _hash_callable(fn: Any) -> str:
    """SHA-256 of a callable's source code + marshalled bytecode.

    Source code is the primary signal (human-readable, stable across
    platforms). The marshalled bytecode is added as a secondary signal
    for compiled extensions where source may not be available.
    Falls back to hash of the repr string if neither is accessible.
    """
    h = hashlib.sha256()

    # Layer 1: source code (most stable, human-verifiable)
    try:
        source = inspect.getsource(fn)
        h.update(source.encode("utf-8"))
    except (OSError, TypeError):
        pass

    # Layer 2: compiled code object (catches renamed-but-same-code tricks)
    try:
        code = fn.__code__
        h.update(marshal.dumps(code))
    except AttributeError:
        pass

    # Layer 3: fallback repr
    if h.digest() == hashlib.sha256(b"").digest():
        h.update(repr(fn).encode("utf-8"))

    return h.hexdigest()


def hash_tool(tool: Any) -> str:
    """Compute the content hash of a LangChain tool.

    For StructuredTool / decorated functions, hashes the underlying
    coroutine or sync function. For class-based tools, hashes the
    _run method source.
    """
    # Try coroutine first (most of our tools are async)
    for attr in ("coroutine", "func", "_run", "_arun", "run"):
        fn = getattr(tool, attr, None)
        if callable(fn) and not isinstance(fn, type):
            return _hash_callable(fn)

    # Fallback: hash the tool class itself
    return _hash_callable(type(tool))


# ── Manifest management ───────────────────────────────────────────────────────

def build_tool_manifest(tools: list[Any]) -> dict[str, str]:
    """Compute SHA-256 content hashes for all tools and save to manifest file.

    Called once at agent startup. The manifest is written to
    ``governance/tool-hashes.json`` and used for all subsequent
    integrity checks.

    Args:
        tools: List of LangChain tools to hash.

    Returns:
        Mapping of {tool_name: sha256_hex}.
    """
    manifest: dict[str, str] = {}
    for tool in tools:
        name = getattr(tool, "name", repr(tool))
        try:
            manifest[name] = hash_tool(tool)
            logger.debug("tool_integrity: hashed '%s' → %s…", name, manifest[name][:12])
        except Exception:
            logger.exception("tool_integrity: failed to hash tool '%s'", name)

    # Persist the manifest
    _MANIFEST_PATH.parent.mkdir(parents=True, exist_ok=True)
    _MANIFEST_PATH.write_text(json.dumps(manifest, indent=2))
    logger.info(
        "tool_integrity: manifest written with %d tool(s) → %s", len(manifest), _MANIFEST_PATH
    )
    return manifest


def load_tool_manifest() -> dict[str, str]:
    """Load a previously saved tool-hash manifest.

    Returns empty dict if no manifest exists (first run).
    """
    if not _MANIFEST_PATH.exists():
        return {}
    try:
        return json.loads(_MANIFEST_PATH.read_text())
    except Exception:
        logger.exception("tool_integrity: failed to load manifest from %s", _MANIFEST_PATH)
        return {}


# ── Verification ──────────────────────────────────────────────────────────────

def verify_tool_integrity(
    tool: Any,
    manifest: dict[str, str],
    strict: bool = True,
) -> tuple[bool, str]:
    """Verify a tool's current hash against the startup manifest.

    Args:
        tool:     LangChain tool to verify.
        manifest: The manifest built at startup by build_tool_manifest().
        strict:   If True, tools with no manifest entry are blocked.
                  If False, they are warned but allowed.

    Returns:
        (passed: bool, reason: str)
        If passed=False, reason contains the denial message.
    """
    name = getattr(tool, "name", repr(tool))

    expected = manifest.get(name)
    if expected is None:
        if strict:
            return (
                False,
                f"Tool '{name}' has no registered content hash — "
                "possible alias or unregistered wrapper (blocked in strict mode)",
            )
        logger.warning("tool_integrity: no hash for '%s' — allowing (non-strict)", name)
        return True, "allowed (non-strict, no hash registered)"

    actual = hash_tool(tool)
    if actual != expected:
        return (
            False,
            f"Tool '{name}' content hash mismatch — "
            f"expected {expected[:12]}… got {actual[:12]}… "
            "(possible tampering or wrapper injection)",
        )

    return True, f"verified (hash={actual[:12]}…)"


# ── ContentHashInterceptor wrapper ────────────────────────────────────────────

class ToolIntegrityGuard:
    """Thin wrapper around ContentHashInterceptor (or built-in fallback).

    Provides a single ``check(tool)`` method that verifies the tool's
    content hash and raises ``ToolIntegrityError`` on failure.

    Uses the real ``agent_os.integrations.base.ContentHashInterceptor``
    when agent-governance-toolkit-core is installed. Falls back to the
    built-in implementation when it is not.

    Example::

        # At startup
        guard = ToolIntegrityGuard.from_tools(write_tools)

        # Per tool call
        guard.check(tool)  # raises ToolIntegrityError if tampered
    """

    class ToolIntegrityError(Exception):
        """Raised when a tool's content hash does not match the manifest."""

    def __init__(self, manifest: dict[str, str], strict: bool = True) -> None:
        self._manifest = manifest
        self._strict = strict

        # Wire up the real ContentHashInterceptor if available
        self._interceptor: Any = None
        if _AGENT_OS_AVAILABLE and _ContentHashInterceptor is not None:
            self._interceptor = _ContentHashInterceptor(
                tool_hashes=manifest, strict=strict
            )
            logger.info(
                "ToolIntegrityGuard: using agent_os.ContentHashInterceptor "
                "with %d tool hash(es)", len(manifest)
            )
        else:
            logger.info(
                "ToolIntegrityGuard: using built-in hash verification "
                "with %d tool hash(es)", len(manifest)
            )

    @classmethod
    def from_tools(cls, tools: list[Any], strict: bool = True) -> "ToolIntegrityGuard":
        """Build a guard by hashing all provided tools.

        Args:
            tools:  Write tools to protect (typically update_salary,
                    update_bonus_eligibility, and any MCP write tools).
            strict: Block unknown tools if True.

        Returns:
            A configured ToolIntegrityGuard.
        """
        manifest = build_tool_manifest(tools)
        return cls(manifest, strict=strict)

    def check(self, tool: Any) -> None:
        """Verify tool integrity. Raises ToolIntegrityError on failure.

        Args:
            tool: LangChain tool to verify before execution.

        Raises:
            ToolIntegrityError: If the tool's hash does not match.
        """
        if self._interceptor is not None:
            # Use agent_os ContentHashInterceptor path
            # Build a minimal ToolCallRequest-compatible object
            name = getattr(tool, "name", repr(tool))
            actual_hash = hash_tool(tool)

            # ContentHashInterceptor reads content_hash from request.metadata
            class _FakeRequest:
                tool_name = name
                metadata = {"content_hash": actual_hash}

            result = self._interceptor.intercept(_FakeRequest())
            if not result.allowed:
                raise self.ToolIntegrityError(result.reason)
        else:
            # Built-in fallback
            passed, reason = verify_tool_integrity(tool, self._manifest, self._strict)
            if not passed:
                raise self.ToolIntegrityError(reason)

    def register_tool(self, tool: Any) -> None:
        """Add a tool to the manifest at runtime (e.g. dynamically loaded tools)."""
        name = getattr(tool, "name", repr(tool))
        h = hash_tool(tool)
        self._manifest[name] = h
        if self._interceptor is not None:
            self._interceptor.register_hash(name, h)
        logger.info("ToolIntegrityGuard: registered '%s' hash=%s…", name, h[:12])

    @property
    def manifest(self) -> dict[str, str]:
        """The current tool-hash manifest."""
        return dict(self._manifest)

    def summary(self) -> str:
        """Human-readable summary of protected tools."""
        lines = [
            f"ToolIntegrityGuard — {len(self._manifest)} tool(s) protected",
            f"  Backend: {'agent_os.ContentHashInterceptor' if self._interceptor else 'built-in'}",
            f"  Strict mode: {self._strict}",
            f"  Manifest: {_MANIFEST_PATH}",
        ]
        for name, h in sorted(self._manifest.items()):
            lines.append(f"  · {name}: {h[:16]}…")
        return "\n".join(lines)
