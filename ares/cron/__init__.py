"""Cron job scheduling for Ares."""

__all__ = ["CronStore", "CronScheduler", "CronRunner"]

def __getattr__(name: str):
    if name == "CronStore":
        from ares.cron.store import CronStore
        return CronStore
    if name == "CronScheduler":
        from ares.cron.scheduler import CronScheduler
        return CronScheduler
    if name == "CronRunner":
        from ares.cron.runner import CronRunner
        return CronRunner
    raise AttributeError(name)
