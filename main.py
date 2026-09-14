"""Compatibility entry point for the Timeweb FastAPI preset (main:app)."""

from app.portal import app as app
from app.serve import main

if __name__ == "__main__":
    main()
