"""Background workers (Procrastinate).

The singleton ``App`` lives in ``workers/app.py``. Task modules under
``workers/tasks/`` import that ``app`` and decorate their functions with
``@app.task`` / ``@app.periodic``. The worker process is started by
``docker-entrypoint.sh worker`` as
``procrastinate -a aggrigator.workers.app.app worker``.
"""
