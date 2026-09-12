import pytest
from storage import Storage
from permissions import LOCAL_OWNER
from roles import Roles


def test_roles_precedence_and_revision(tmp_path):
    s=Storage(tmp_path); s.register_targets(['Synthetic'],[])
    r=Roles(s); chat=s.chat_id('private','Synthetic')
    original=r.resolve(chat)
    role=r.save({'name':'Synthetic role','system_prompt':'Synthetic prompt'},LOCAL_OWNER)
    r.bind(role,chat,None,LOCAL_OWNER)
    assert r.resolve(chat)['id']==role
    r.save({'system_prompt':'Changed'},LOCAL_OWNER,role)
    r.rollback(role,1,LOCAL_OWNER)
    assert r.resolve(chat)['system_prompt']=='Synthetic prompt'
    r.save({'enabled':False},LOCAL_OWNER,role)
    assert r.resolve(chat)['id']==original['id']
    with pytest.raises(ValueError): r.delete(original['id'],LOCAL_OWNER)
    with pytest.raises(ValueError): r.save({'name':'Invalid','temperature':20},LOCAL_OWNER)
