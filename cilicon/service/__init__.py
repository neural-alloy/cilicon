"""cilicon hosted service — the layer that turns the `cilicon` engine into CI.

  * a GitHub App webhook that runs the matrix on every push / PR and reports
    back a single check-run (cilicon/service/github.py + orchestrator.py)
  * persistence of every run, log, and artifact to Supabase
    (cilicon/service/db.py over Supabase Postgres + Storage)
  * a server-rendered dashboard with GitHub-OAuth login
    (cilicon/service/app.py + auth.py + templates/)

The pure `cilicon` engine (config/runner/presets/report) knows nothing about any
of this; the service imports it and adds the product around it.
"""
