"""Compatibility name for the unified application bootstrap."""

from .launcher import is_quick_invocation, main


__all__ = ("is_quick_invocation", "main")


if __name__ == "__main__":
    raise SystemExit(main())
