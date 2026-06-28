from ares.cron.runner import CronRunner
from ares.cron.store import CronStore
from ares.models import AppConfig

def test_latest_summary_extracts_summary_section(tmp_path):
    store=CronStore(tmp_path/'ares')
    job=store.create_job('Runner','prompt','0 * * * *')
    log=store.log_dir(job['id'])/'2026-01-01T00-00-00Z.md'
    log.write_text('# Run\n\n## Summary\nImportant summary.\n\n## Run Metadata\n- x\n', encoding='utf-8')
    runner=CronRunner(store=store, config=AppConfig(data_dir=str(tmp_path/'data')))
    assert runner.latest_summary(job['id']) == 'Important summary.'

def test_write_log_creates_markdown(tmp_path):
    store=CronStore(tmp_path/'ares')
    job=store.create_job('Runner','prompt','0 * * * *')
    runner=CronRunner(store=store, config=AppConfig(data_dir=str(tmp_path/'data')))
    path=runner._write_log(job, '2026-01-01T00:00:00Z', 'completed', 1.2, 'hello\n\nworld')
    text=path.read_text(encoding='utf-8')
    assert '**Status:** completed' in text
    assert '## Agent Output' in text
    assert '## Summary' in text
