"""Web front end for the blog generator.

A thin Flask layer over the existing pipeline (retrieval -> verification ->
synthesis -> QA). It adds no new generation logic; it only exposes the same
functions the CLIs use over HTTP so anyone on the internal network can generate
a draft from a browser.

Run (dev):     flask --app webapp run
Run (prod):    gunicorn "webapp:create_app()" -b 0.0.0.0:8000
"""
from .app import create_app

__all__ = ["create_app"]
