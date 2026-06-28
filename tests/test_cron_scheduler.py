from ares.cron.scheduler import CronScheduler
from ares.cron.store import CronStore

class Runner:
    def __init__(self): self.ran=[]
    async def run_job(self, jid): self.ran.append(jid)

async def test_scheduler_tick_runs_due_job(tmp_path):
    store=CronStore(tmp_path/'ares'); job=store.create_job('Due','x','0 * * * *')
    store.update_job(job['id'], next_run_at='2020-01-01T00:00:00Z')
    runner=Runner(); sched=CronScheduler(store, runner=runner, tick_seconds=999)
    await sched.tick(); await sched.stop()
    assert runner.ran==[job['id']]
