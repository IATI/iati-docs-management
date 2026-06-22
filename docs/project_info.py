# Project-specific configuration for Sphinx documentation.
# This file contains settings that vary per repository.
# The main conf.py imports these values and can be synced across all repos.

# Project name (used for titles, headers, and Sphinx internals)
project = "IATI Documentation Management"

# URL of the live tool this repo documents. None: the docs themselves are
# the deliverable (this is the estate control plane, not a deployed tool).
tool_url = None

# Short label used in the nav. Defaults to ``project`` when None.
nav_label = None

# Eyebrow text: the smaller text that appears directly above the website title
eyebrow_text = "IATI Documentation"

# GitHub repository URL (used by the theme for the "Source code at GitHub" footer link)
github_repository = "https://github.com/IATI/iati-docs-management"

# Plausible analytics domain. None: this repo's docs are not tracked.
plausible_domain = None

# Supported languages for the documentation
languages = ["en", "fr", "es"]

redoc = []
