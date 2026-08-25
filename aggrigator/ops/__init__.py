"""Operator dashboard — cron-runner UI + JSON API + Procrastinate task recorder.

Replaces the v0 ``api/admin_crons.py`` and ``api/ops_console.py`` modules.
Same admin-only auth path; persistent run history; HTMX-driven
HTML page with the same per-cron Run-now button the v0 page had.
"""
