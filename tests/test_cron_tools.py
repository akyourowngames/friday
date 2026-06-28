from ares.cron.store import CronStore
from ares.cron.tools import CronToolHandlers

def test_cron_tools_crud(tmp_path):
    tools=CronToolHandlers(CronStore(tmp_path/'ares'))
    assert 'Created cron job' in tools.create_cron_job({'name':'Daily','prompt':'x','cron':'every day at 9am'})
    assert 'daily' in tools.list_cron_jobs({})
    assert 'Updated cron job' in tools.update_cron_job({'job_id':'daily','enabled':False})
    assert 'Deleted cron job' in tools.delete_cron_job({'job_id':'daily'})
