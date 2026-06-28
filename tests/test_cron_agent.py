from ares.agent import Agent
from ares.memory import MemoryStore
from ares.models import AppConfig

def test_cron_session_filters_cron_tools(tmp_path):
    cfg=AppConfig(data_dir=str(tmp_path/'data'))
    agent=Agent(MemoryStore(db_path=tmp_path/'m.db'), config=cfg, is_cron_session=True)
    names={t['function']['name'] for t in agent.tools}
    assert 'create_cron_job' not in names
    assert 'web_search' in names
