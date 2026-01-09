from __future__ import annotations

project = "Podcast Pipeline"
author = "Podcast Pipeline"
release = "0.1.0"

extensions = ["myst_parser"]
source_suffix = {".md": "markdown"}
root_doc = "index"

templates_path = ["_templates"]
exclude_patterns = ["_build", "Thumbs.db", ".DS_Store"]

html_theme = "furo"
html_static_path = ["_static"]

myst_heading_anchors = 3
