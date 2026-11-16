from datetime import timedelta

from guardian.clock import ManualClock
from guardian.storage.retention import purge_old
from test_api import T0, add_incident, make_env


def test_old_incidents_are_deleted_but_protected_and_recent_stay(tmp_path):
    env = make_env(tmp_path)
    assert env.cfg.storage.max_age_days == 7  # default
    old_uid, old_id, old_folder = add_incident(env, 1)
    prot_uid, prot_id, prot_folder = add_incident(env, 2, protected=1)
    rec_uid, rec_id, rec_folder = add_incident(env, 3, status="recording")
    clock = ManualClock(T0 + timedelta(days=10))
    new_uid, new_id, new_folder = add_incident(env, 60 * 24 * 9 + 5)  # 9 days after T0, 1 day old
    assert purge_old(env.cfg.storage, env.db, clock) == 1
    assert not old_folder.exists() and env.db.get_incident(old_id) is None
    assert prot_folder.exists() and env.db.get_incident(prot_id) is not None
    assert rec_folder.exists() and env.db.get_incident(rec_id) is not None
    assert new_folder.exists() and env.db.get_incident(new_id) is not None


def test_zero_keeps_everything(tmp_path):
    env = make_env(tmp_path)
    env.cfg.storage.max_age_days = 0
    _, iid, folder = add_incident(env, 1)
    assert purge_old(env.cfg.storage, env.db, ManualClock(T0 + timedelta(days=400))) == 0
    assert folder.exists() and env.db.get_incident(iid) is not None
