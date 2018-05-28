# -*- coding: utf-8 -*-

"""Top-level package for aioapp."""

__author__ = """Konstantin Stepanov"""
__version__ = '0.0.1b26'

from . import app, db, http, error, chat, amqp


__all__ = ['app', 'db', 'http', 'error', 'chat', 'amqp']
