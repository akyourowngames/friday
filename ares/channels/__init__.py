"""Persistent remote channels for the local Ares runtime."""

from ares.channels.telegram import TelegramChannel, run_telegram_channel

__all__ = ["TelegramChannel", "run_telegram_channel"]
