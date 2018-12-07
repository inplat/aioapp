# -*- coding: utf-8 -*-

"""Top-level package for aioapp."""

__author__ = """Konstantin Stepanov"""
__version__ = '0.0.2b6'

from . import app, error
from .app import Component, Application
from .tracer import Span
from .config import Config

__all__ = ['app', 'error', 'Component', 'Application', 'Span', 'Config']
