"""
cortexbot/db/base.py

Single source-of-truth for the SQLAlchemy DeclarativeBase.

Both models.py and score_models.py import Base from here to break
the previous circular import:
  models.py → (bottom) imports from score_models
  score_models → imports Base from models   ← cycle

Fix: move Base here; both files import from this module instead.
"""

from sqlalchemy.orm import DeclarativeBase


class Base(DeclarativeBase):
    pass
