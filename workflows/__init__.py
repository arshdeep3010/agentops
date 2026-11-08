"""Importing this package registers every workflow, its code steps, tools and schemas."""

from workflows import chaos, invoice  # noqa: F401
from sdlc import workflow as sdlc_workflow  # noqa: E402,F401 - the SDLC pipeline
