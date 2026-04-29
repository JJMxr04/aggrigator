"""Operator dashboard — cron-runner UI + JSON API + Redis lock + ARQ recorder.

Replaces the v0 ``api/admin_crons.py`` and ``api/ops_console.py`` modules
(plan §2.1.10). Same admin-only auth path; persistent run history; HTMX-driven
HTML page with the same per-cron Run-now button the v0 page had.
"""
