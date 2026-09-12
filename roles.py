"""Persistent role resolution and versioned changes; no UI/network dependencies."""
import json
from audit import record_audit, bump_permission_revision
from permissions import PermissionDenied, _decision


def require_manager(c, actor, owner=False):
    if actor.source != 'web':
        raise PermissionDenied('仅允许已登录的 Web 管理员')
    if actor.principal_id is None and actor.access_level == 'owner':
        return  # temporary loopback owner, removed at the HTTP boundary in step 8
    current = _decision(c, actor.principal_id, None)
    if not current.allowed or current.access_level not in (('owner',) if owner else ('owner', 'admin')):
        raise PermissionDenied('需要 owner' if owner else '需要管理员权限')


class Roles:
    FIELDS = {'name','description','system_prompt','model','temperature','max_tokens','max_reply_chars',
              'enabled','user_selectable','is_default','knowledge_mode'}

    def __init__(self, storage):
        self.storage = storage

    def list(self):
        with self.storage._connection() as c:
            return [dict(r) for r in c.execute('SELECT * FROM roles ORDER BY is_default DESC,id')]

    def resolve(self, chat_id, principal_id=None):
        with self.storage._connection() as c:
            row = c.execute('''SELECT r.* FROM role_bindings b JOIN roles r ON r.id=b.role_id
                WHERE r.enabled=1 AND (b.chat_id=? OR b.chat_id IS NULL)
                AND (b.principal_id=? OR b.principal_id IS NULL)
                ORDER BY (b.principal_id IS NOT NULL) DESC,(b.chat_id IS NOT NULL) DESC LIMIT 1''',
                (chat_id, principal_id)).fetchone()
            if not row:
                row = c.execute('SELECT * FROM roles WHERE is_default=1 AND enabled=1').fetchone()
            if not row:
                raise ValueError('没有启用的默认角色')
            return dict(row)

    def save(self, data, actor, role_id=None):
        if not isinstance(data, dict) or set(data) - self.FIELDS:
            raise ValueError('角色字段无效')
        with self.storage.transaction() as c:
            require_manager(c, actor)
            old = c.execute('SELECT * FROM roles WHERE id=?',(role_id,)).fetchone() if role_id else None
            if role_id and not old:
                raise ValueError('角色不存在')
            merged = dict(old) if old else dict(name='',description='',system_prompt='',model='',temperature=.7,
                max_tokens=600,max_reply_chars=1200,enabled=1,user_selectable=0,is_default=0,knowledge_mode='auto')
            merged.update(data)
            for key, limit in [('name',80),('description',2000),('system_prompt',32000),('model',200)]:
                if not isinstance(merged[key],str) or len(merged[key])>limit:
                    raise ValueError('角色文本长度无效')
            if not merged['name'].strip() or not merged['system_prompt'].strip():
                raise ValueError('名称和提示词不能为空')
            for key,lo,hi in [('temperature',0,2),('max_tokens',1,8000),('max_reply_chars',1,4000)]:
                if type(merged[key]) not in (int,float) or not lo<=merged[key]<=hi:
                    raise ValueError('角色数值超出范围')
            for key in ('max_tokens','max_reply_chars'):
                if type(merged[key]) is not int:
                    raise ValueError('长度必须为整数')
            for key in ('enabled','user_selectable','is_default'):
                if merged[key] not in (True,False,0,1):
                    raise ValueError('角色开关无效')
            if merged['knowledge_mode'] not in ('off','auto','tool'):
                raise ValueError('知识模式无效')
            if merged['is_default'] and not merged['enabled']:
                raise ValueError('不能禁用默认角色')
            if old and old['is_default'] and not merged['is_default']:
                raise ValueError('请将另一角色设为默认后再修改')
            if merged['is_default']:
                c.execute('UPDATE roles SET is_default=0 WHERE id IS NOT ?', (role_id,))
            fields=sorted(self.FIELDS)
            if old:
                c.execute('INSERT INTO role_revisions(role_id,revision,payload) VALUES(?,?,?)',
                          (role_id,old['revision'],json.dumps(dict(old),ensure_ascii=False)))
                c.execute('UPDATE roles SET '+','.join(k+'=?' for k in fields)+
                          ',revision=revision+1,updated_at=CURRENT_TIMESTAMP WHERE id=?',
                          [merged[k] for k in fields]+[role_id])
            else:
                role_id=c.execute('INSERT INTO roles('+','.join(fields)+') VALUES('+','.join('?' for _ in fields)+')',
                                  [merged[k] for k in fields]).lastrowid
            record_audit(c,'role.save',source=actor.source,actor_id=actor.principal_id,target_type='role',target_id=role_id)
            bump_permission_revision(c)
            return role_id

    def revisions(self, role_id):
        with self.storage._connection() as c:
            return [dict(r) for r in c.execute('SELECT * FROM role_revisions WHERE role_id=? ORDER BY revision DESC',(role_id,))]

    def rollback(self, role_id, revision, actor):
        with self.storage._connection() as c:
            row=c.execute('SELECT payload FROM role_revisions WHERE role_id=? AND revision=?',(role_id,revision)).fetchone()
        if not row:
            raise ValueError('修订不存在')
        data=json.loads(row[0])
        # Default designation is a current invariant, not historical prompt content.
        data.pop('is_default',None)
        return self.save({k:v for k,v in data.items() if k in self.FIELDS},actor,role_id)

    def delete(self, role_id, actor, replacement=None):
        with self.storage.transaction() as c:
            require_manager(c,actor)
            old=c.execute('SELECT * FROM roles WHERE id=?',(role_id,)).fetchone()
            if not old or old['is_default']:
                raise ValueError('默认角色不能删除，或角色不存在')
            if c.execute('SELECT 1 FROM role_bindings WHERE role_id=?',(role_id,)).fetchone():
                if not replacement or replacement==role_id or not c.execute('SELECT 1 FROM roles WHERE id=? AND enabled=1',(replacement,)).fetchone():
                    raise ValueError('使用中的角色必须指定有效替代角色')
                c.execute('UPDATE role_bindings SET role_id=? WHERE role_id=?',(replacement,role_id))
            c.execute('DELETE FROM roles WHERE id=?',(role_id,))
            record_audit(c,'role.delete',source=actor.source,actor_id=actor.principal_id,target_type='role',target_id=role_id)
            bump_permission_revision(c)

    def bind(self, role_id, chat_id, principal_id, actor, *, self_select=False):
        with self.storage.transaction() as c:
            role=c.execute('SELECT * FROM roles WHERE id=? AND enabled=1',(role_id,)).fetchone()
            if not role:
                raise ValueError('角色不存在或已禁用')
            if principal_id is not None:
                p=c.execute('SELECT chat_id FROM principals WHERE id=?',(principal_id,)).fetchone()
                if not p or p[0]!=chat_id:
                    raise ValueError('成员不属于当前聊天')
            if self_select:
                current=_decision(c,actor.principal_id,chat_id)
                if not current.allowed or actor.source!='wechat':
                    raise PermissionDenied('身份未批准')
                if principal_id==actor.principal_id:
                    if not role['user_selectable']:
                        raise PermissionDenied('该角色不允许用户选择')
                elif principal_id is not None or current.access_level!='admin':
                    raise PermissionDenied('不能改变其他成员的角色')
            else:
                require_manager(c,actor)
            c.execute('DELETE FROM role_bindings WHERE chat_id IS ? AND principal_id IS ?',(chat_id,principal_id))
            c.execute('INSERT INTO role_bindings(role_id,chat_id,principal_id) VALUES(?,?,?)',(role_id,chat_id,principal_id))
            record_audit(c,'role.bind',source=actor.source,actor_id=actor.principal_id,target_type='role',target_id=role_id,
                         details={'chat_id':chat_id,'principal_id':principal_id})
            bump_permission_revision(c)
