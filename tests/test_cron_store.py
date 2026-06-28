from ares.cron.store import CronStore

def test_create_list_due_and_logs(tmp_path):
    store=CronStore(tmp_path/'ares')
    job=store.create_job('Test Job','do it','0 * * * *')
    assert job['id']=='test-job'
    assert store.get_job(job['id'])['name']=='Test Job'
    store.update_job(job['id'], next_run_at='2020-01-01T00:00:00Z')
    assert [j['id'] for j in store.get_due_jobs()]==[job['id']]
    assert store.log_dir(job['id']).exists()

def test_disabled_and_running_not_due(tmp_path):
    store=CronStore(tmp_path/'ares')
    a=store.create_job('A','a','0 * * * *'); b=store.create_job('B','b','0 * * * *')
    store.update_job(a['id'], next_run_at='2020-01-01T00:00:00Z', enabled=False)
    store.update_job(b['id'], next_run_at='2020-01-01T00:00:00Z', state='running')
    assert store.get_due_jobs()==[]
