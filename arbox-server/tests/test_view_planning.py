import pytest
from test_membership_policy import engine, raw

@pytest.mark.asyncio
async def test_read_only_quota_detects_changed_plan_without_recording_it(engine):
    await engine.store.watch(77,membership_user_id=20)
    await engine.store.upsert_sessions([raw(77,cat=2)],box_id=73)
    before=engine.store.db.total_changes
    q=await engine.quota_status(read_only=True)
    assert q['plan_states']['77']['state']=='session_changed'
    assert engine.store.db.total_changes==before
    engine.client.book.assert_not_awaited()
