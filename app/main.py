import asyncio, re
from datetime import datetime, timezone
from fastapi import FastAPI, Depends, HTTPException, Header, UploadFile, File
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session
from sqlalchemy import desc
from .db import Base, engine, get_db, SessionLocal
from .models import User, Team, TgAccount, Customer, Assignment, Message, Tag, AuditLog, ReadState
from .security import hash_password, verify_password, create_token, decode_token
from .config import settings
from . import telegram_service as tg

Base.metadata.create_all(bind=engine)
app = FastAPI(title='Telegram CRM Management', version='1.0.0')
app.add_middleware(CORSMiddleware, allow_origins=settings.cors_origins.split(','), allow_credentials=False, allow_methods=['*'], allow_headers=['*'])
app.mount('/static', StaticFiles(directory='static'), name='static')

class LoginIn(BaseModel): username: str; password: str
class UserIn(BaseModel): username: str; password: str = Field(min_length=10); role: str = 'employee'; team_id: int | None = None
class TeamIn(BaseModel): name: str
class PhoneIn(BaseModel): phone: str
class CodeIn(BaseModel): phone_code_hash: str; code: str; password: str | None = None
class AssignIn(BaseModel): customer_id: int; user_id: int
class BulkAssignIn(BaseModel): customer_ids: list[int]; user_id: int
class SendIn(BaseModel): text: str = Field(min_length=1, max_length=4096)
class TagIn(BaseModel): key: str; value: str; scope: str = 'private'

@app.on_event('startup')
async def startup():
    db = SessionLocal()
    try:
        if settings.admin_password and not db.query(User).filter_by(username=settings.admin_username).first():
            db.add(User(username=settings.admin_username, password_hash=hash_password(settings.admin_password), role='superadmin', active=True)); db.commit()
    finally: db.close()
    if settings.telegram_api_id and settings.telegram_api_hash and settings.session_encryption_key:
        asyncio.create_task(tg.restore_clients())

@app.get('/')
def home(): return FileResponse('static/index.html')

@app.post('/api/auth/login')
def login(data: LoginIn, db: Session = Depends(get_db)):
    user = db.query(User).filter_by(username=data.username).first()
    if not user or not user.active or not verify_password(data.password, user.password_hash): raise HTTPException(401, 'Invalid username or password')
    user.last_seen = datetime.now(timezone.utc); db.commit()
    return {'token': create_token(user.id,user.role), 'user': {'id':user.id,'username':user.username,'role':user.role,'team_id':user.team_id}}

def current_user(authorization: str | None = Header(default=None), db: Session = Depends(get_db)):
    if not authorization or not authorization.startswith('Bearer '): raise HTTPException(401,'Login required')
    payload = decode_token(authorization[7:])
    if not payload: raise HTTPException(401,'Invalid or expired session')
    user = db.get(User, int(payload['sub']))
    if not user or not user.active: raise HTTPException(401,'Account disabled')
    return user

def admin(user: User = Depends(current_user)):
    if user.role not in ('superadmin','admin'): raise HTTPException(403,'Administrator only')
    return user

def audit(db, actor, action, typ='', target='', detail=''):
    db.add(AuditLog(actor_user_id=actor.id if actor else None, action=action, target_type=typ, target_id=str(target), detail=detail)); db.commit()

def can_access(db: Session, user: User, customer_id: int):
    if user.role in ('superadmin','admin'): return True
    return db.query(Assignment).filter_by(customer_id=customer_id,user_id=user.id,active=True).first() is not None

def safe_customer(c: Customer):
    return {'id':c.id,'alias':c.alias or f'Customer-{c.id:05d}','created_at':c.created_at.isoformat()}

def redact(text: str):
    # Best-effort privacy filter. It cannot reliably identify all personal data, names, photos, voice notes or vCards.
    text = re.sub(r'(?<!\w)@[A-Za-z][A-Za-z0-9_]{4,}', '[TELEGRAM_USERNAME_HIDDEN]', text)
    text = re.sub(r'(?<!\w)\+?\d[\d ()-]{7,}\d', '[PHONE_NUMBER_HIDDEN]', text)
    return text

@app.get('/api/me')
def me(user: User = Depends(current_user)):
    return {'id':user.id,'username':user.username,'role':user.role,'team_id':user.team_id}

@app.get('/api/admin/users')
def list_users(db: Session = Depends(get_db), actor: User = Depends(admin)):
    return [{'id':u.id,'username':u.username,'role':u.role,'team_id':u.team_id,'active':u.active,'created_at':u.created_at.isoformat(),'last_seen':u.last_seen.isoformat() if u.last_seen else None} for u in db.query(User).order_by(User.id).all()]

@app.post('/api/admin/users')
def create_user(data: UserIn, db: Session = Depends(get_db), actor: User = Depends(admin)):
    if data.role not in ('admin','employee'): raise HTTPException(400,'Role must be admin or employee')
    if db.query(User).filter_by(username=data.username).first(): raise HTTPException(409,'Username already exists')
    if data.team_id and not db.get(Team,data.team_id): raise HTTPException(404,'Team not found')
    u=User(username=data.username,password_hash=hash_password(data.password),role=data.role,team_id=data.team_id); db.add(u); db.commit(); db.refresh(u); audit(db,actor,'user.create','user',u.id); return {'id':u.id,'username':u.username}

@app.patch('/api/admin/users/{uid}')
def update_user(uid:int, active:bool, db:Session=Depends(get_db), actor:User=Depends(admin)):
    u=db.get(User,uid)
    if not u: raise HTTPException(404,'User not found')
    u.active=active; db.commit(); audit(db,actor,'user.active','user',uid,str(active)); return {'ok':True}

@app.post('/api/admin/users/{uid}/password')
def reset_user_password(uid:int, data:dict, db:Session=Depends(get_db), actor:User=Depends(admin)):
    u=db.get(User,uid)
    password=data.get('password','')
    if not u: raise HTTPException(404,'User not found')
    if len(password)<10: raise HTTPException(400,'Password must be at least 10 characters')
    u.password_hash=hash_password(password); db.commit(); audit(db,actor,'user.password_reset','user',uid)
    return {'ok':True}

@app.delete('/api/admin/users/{uid}')
def delete_user(uid:int, db:Session=Depends(get_db), actor:User=Depends(admin)):
    u=db.get(User,uid)
    if not u: raise HTTPException(404,'User not found')
    if u.id==actor.id: raise HTTPException(400,'Cannot delete yourself')
    u.active=False; u.username=f'deleted_{u.id}_{u.username}'; db.commit(); audit(db,actor,'user.soft_delete','user',uid); return {'ok':True,'note':'Soft deleted; historical records retained'}

@app.post('/api/admin/teams')
def create_team(data:TeamIn, db:Session=Depends(get_db), actor:User=Depends(admin)):
    if db.query(Team).filter_by(name=data.name).first(): raise HTTPException(409,'Team exists')
    t=Team(name=data.name); db.add(t); db.commit(); db.refresh(t); audit(db,actor,'team.create','team',t.id); return {'id':t.id,'name':t.name}

@app.get('/api/admin/teams')
def list_teams(db:Session=Depends(get_db), actor:User=Depends(admin)):
    return [{'id':t.id,'name':t.name} for t in db.query(Team).all()]

@app.post('/api/admin/telegram/begin')
async def telegram_begin(data:PhoneIn, actor:User=Depends(admin)):
    try: return {'phone_code_hash':await tg.begin_login(data.phone)}
    except Exception as e: raise HTTPException(400,str(e))

@app.post('/api/admin/telegram/complete')
async def telegram_complete(data:CodeIn, db:Session=Depends(get_db), actor:User=Depends(admin)):
    try:
        result=await tg.finish_login(data.phone_code_hash,data.code,data.password)
        if result.get('account_id'): audit(db,actor,'telegram.account.connect','telegram_account',result['account_id'])
        return result
    except Exception as e: raise HTTPException(400,str(e))

@app.get('/api/admin/telegram/accounts')
def telegram_accounts(db:Session=Depends(get_db), actor:User=Depends(admin)):
    return [{'id':a.id,'label':a.label,'phone_masked':a.phone_masked,'status':a.status,'created_at':a.created_at.isoformat()} for a in db.query(TgAccount).all()]

@app.get('/api/admin/customers')
def admin_customers(db:Session=Depends(get_db), actor:User=Depends(admin)):
    return [{'id':c.id,'alias':c.alias,'telegram_username':c.real_username,'telegram_user_id':c.telegram_peer_id,'name':c.real_name,'phone':c.phone,'account_id':c.account_id,'assigned_to':[a.user_id for a in db.query(Assignment).filter_by(customer_id=c.id,active=True).all()]} for c in db.query(Customer).all()]

@app.post('/api/admin/assign')
def assign(data:AssignIn, db:Session=Depends(get_db), actor:User=Depends(admin)):
    c=db.get(Customer,data.customer_id); u=db.get(User,data.user_id)
    if not c or not u or not u.active: raise HTTPException(404,'Customer or active user not found')
    # Reassignment revokes old active assignments but never deletes history/messages/tags.
    old=db.query(Assignment).filter_by(customer_id=c.id,active=True).all()
    for a in old: a.active=False; a.revoked_at=datetime.now(timezone.utc)
    db.add(Assignment(customer_id=c.id,user_id=u.id,assigned_by=actor.id,active=True)); db.commit(); audit(db,actor,'customer.assign','customer',c.id,f'to_user={u.id}'); return {'ok':True,'history_preserved':True}

@app.post('/api/admin/assign/bulk')
def bulk_assign(data:BulkAssignIn, db:Session=Depends(get_db), actor:User=Depends(admin)):
    u=db.get(User,data.user_id)
    if not u or not u.active: raise HTTPException(404,'Active user not found')
    for cid in data.customer_ids:
        c=db.get(Customer,cid)
        if not c: continue
        for a in db.query(Assignment).filter_by(customer_id=cid,active=True).all(): a.active=False; a.revoked_at=datetime.now(timezone.utc)
        db.add(Assignment(customer_id=cid,user_id=u.id,assigned_by=actor.id,active=True))
    db.commit(); audit(db,actor,'customer.bulk_assign','user',u.id,f'count={len(data.customer_ids)}'); return {'ok':True,'count':len(data.customer_ids)}

@app.post('/api/admin/revoke/{customer_id}')
def revoke(customer_id:int, db:Session=Depends(get_db), actor:User=Depends(admin)):
    rows=db.query(Assignment).filter_by(customer_id=customer_id,active=True).all()
    for a in rows: a.active=False; a.revoked_at=datetime.now(timezone.utc)
    db.commit(); audit(db,actor,'customer.revoke','customer',customer_id); return {'ok':True,'revoked':len(rows),'history_preserved':True}

@app.get('/api/inbox')
def inbox(filter:str='all', db:Session=Depends(get_db), user:User=Depends(current_user)):
    q=db.query(Customer)
    if user.role not in ('superadmin','admin'):
        ids=[a.customer_id for a in db.query(Assignment).filter_by(user_id=user.id,active=True).all()]
        q=q.filter(Customer.id.in_(ids) if ids else False)
    result=[]
    for c in q.all():
        last=db.query(Message).filter_by(customer_id=c.id).order_by(desc(Message.sent_at)).first()
        unread=0
        if user.role not in ('superadmin','admin'):
            state=db.query(ReadState).filter_by(customer_id=c.id,user_id=user.id).first()
            last_read=state.last_read_message_id if state else 0
            unread=db.query(Message).filter(Message.customer_id==c.id,Message.direction=='in',Message.id>last_read).count()
        if filter=='unread' and unread==0: continue
        if filter=='unreplied' and (not last or last.direction!='in'): continue
        result.append({**safe_customer(c),'last_message':redact(last.body)[:180] if last else '', 'last_message_at':last.sent_at.isoformat() if last else None,'unread':unread,'account_id':c.account_id})
    return sorted(result,key=lambda x:x['last_message_at'] or '',reverse=True)

@app.get('/api/customers/{cid}/messages')
def messages(cid:int, limit:int=200, before:int|None=None, db:Session=Depends(get_db), user:User=Depends(current_user)):
    c=db.get(Customer,cid)
    if not c or not can_access(db,user,cid): raise HTTPException(404,'Customer not found')
    q=db.query(Message).filter_by(customer_id=cid)
    if before: q=q.filter(Message.id<before)
    rows=q.order_by(desc(Message.id)).limit(min(max(limit,1),500)).all()[::-1]
    # Never serialize Telegram IDs, peer IDs, usernames, phone numbers or access hashes to employees.
    return [{'id':m.id,'direction':m.direction,'text':redact(m.body) if user.role not in ('admin','superadmin') else m.body,'sent_at':m.sent_at.isoformat(),'sender':m.sender_label,'media_available':bool(m.media_path)} for m in rows]

@app.post('/api/customers/{cid}/send')
async def send(cid:int,data:SendIn, db:Session=Depends(get_db), user:User=Depends(current_user)):
    c=db.get(Customer,cid)
    if not c or not can_access(db,user,cid): raise HTTPException(404,'Customer not found')
    try: sent=await tg.send_message(c.account_id,c,data.text)
    except Exception as e: raise HTTPException(400,str(e))
    m=Message(customer_id=cid,telegram_message_id=str(sent.id),direction='out',body=data.text,sent_at=sent.date,sender_label=user.username); db.add(m); db.commit(); db.refresh(m)
    return {'ok':True,'message':{'id':m.id,'direction':'out','text':m.body,'sent_at':m.sent_at.isoformat(),'sender':m.sender_label}}

@app.post('/api/customers/{cid}/read')
def mark_read(cid:int, db:Session=Depends(get_db), user:User=Depends(current_user)):
    if not can_access(db,user,cid): raise HTTPException(404,'Customer not found')
    latest=db.query(Message).filter_by(customer_id=cid).order_by(desc(Message.id)).first()
    state=db.query(ReadState).filter_by(customer_id=cid,user_id=user.id).first()
    if not state: state=ReadState(customer_id=cid,user_id=user.id); db.add(state)
    state.last_read_message_id=latest.id if latest else 0; db.commit(); return {'ok':True}

@app.get('/api/customers/{cid}/tags')
def get_tags(cid:int, db:Session=Depends(get_db), user:User=Depends(current_user)):
    c=db.get(Customer,cid)
    if not c or not can_access(db,user,cid): raise HTTPException(404,'Customer not found')
    q=db.query(Tag).filter(Tag.customer_id==cid)
    if user.role not in ('admin','superadmin'):
        q=q.filter((Tag.scope=='private') & (Tag.owner_user_id==user.id) | (Tag.scope=='team') & (Tag.team_id==user.team_id))
    return [{'id':t.id,'key':t.key,'value':t.value,'scope':t.scope,'updated_at':t.updated_at.isoformat()} for t in q.all()]

@app.post('/api/customers/{cid}/tags')
def upsert_tag(cid:int,data:TagIn, db:Session=Depends(get_db), user:User=Depends(current_user)):
    c=db.get(Customer,cid)
    if not c or not can_access(db,user,cid): raise HTTPException(404,'Customer not found')
    if data.scope not in ('private','team'): raise HTTPException(400,'scope must be private or team')
    if data.scope=='team' and not user.team_id and user.role=='employee': raise HTTPException(400,'Join a team before creating team tags')
    if user.role in ('admin','superadmin') and data.scope=='private': owner=None
    else: owner=user.id if data.scope=='private' else None
    team_id=user.team_id if data.scope=='team' else None
    tag=db.query(Tag).filter_by(customer_id=cid,scope=data.scope,owner_user_id=owner,team_id=team_id,key=data.key).first()
    if tag: tag.value=data.value
    else: tag=Tag(customer_id=cid,scope=data.scope,owner_user_id=owner,team_id=team_id,key=data.key,value=data.value); db.add(tag)
    db.commit(); db.refresh(tag); audit(db,user,'tag.upsert','tag',tag.id,data.scope); return {'id':tag.id,'key':tag.key,'value':tag.value,'scope':tag.scope}

@app.delete('/api/tags/{tag_id}')
def delete_tag(tag_id:int, db:Session=Depends(get_db), user:User=Depends(current_user)):
    tag=db.get(Tag,tag_id)
    if not tag: raise HTTPException(404,'Tag not found')
    allowed=user.role in ('admin','superadmin') or (tag.scope=='private' and tag.owner_user_id==user.id) or (tag.scope=='team' and tag.team_id==user.team_id)
    if not allowed: raise HTTPException(403,'Cannot delete another employee private tag')
    db.delete(tag); db.commit(); audit(db,user,'tag.delete','tag',tag_id); return {'ok':True}

@app.get('/api/admin/stats')
def stats(db:Session=Depends(get_db), actor:User=Depends(admin)):
    today=datetime.now(timezone.utc).date()
    users=db.query(User).all(); active_assignments=db.query(Assignment).filter_by(active=True).count()
    return {'employees':len([u for u in users if u.role=='employee']),'online_last_5m':len([u for u in users if u.last_seen and (datetime.now(timezone.utc)-u.last_seen).total_seconds()<300]),'active_conversations':active_assignments,'today_received':db.query(Assignment).filter(Assignment.assigned_at>=datetime.combine(today,datetime.min.time(),tzinfo=timezone.utc)).count(),'telegram_accounts':db.query(TgAccount).count()}

@app.get('/api/admin/audit')
def audit_logs(limit:int=100,db:Session=Depends(get_db),actor:User=Depends(admin)):
    rows=db.query(AuditLog).order_by(desc(AuditLog.created_at)).limit(min(limit,500)).all()
    return [{'actor_user_id':r.actor_user_id,'action':r.action,'target_type':r.target_type,'target_id':r.target_id,'detail':r.detail,'created_at':r.created_at.isoformat()} for r in rows]

@app.get('/api/health')
def health(): return {'ok':True,'service':'telegram-crm'}
