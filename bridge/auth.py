"""OAuth 2.1 via official MCP SDK; encrypted local state, secrets in Keychain."""
import hashlib
import html
import json
import secrets
import sqlite3
import time
from urllib.parse import urlencode, urlsplit
from cryptography.fernet import Fernet
import keyring
import sys
from mcp.server.auth.provider import AccessToken, AuthorizationCode, RefreshToken, AuthorizeError, TokenError, RegistrationError
from mcp.shared.auth import OAuthClientInformationFull, OAuthToken
from starlette.responses import HTMLResponse, RedirectResponse

SERVICE = "org.codex.ai-bridge"


class Vault:
    def __init__(self, instance):
        if sys.platform != "darwin": raise ValueError("This profile requires macOS Keychain")
        from keyring.backends.macOS import Keyring
        self.instance = instance
        self.backend = Keyring()
    def get(self, name):
        value = self.backend.get_password(SERVICE, self.instance + ":" + name)
        if not value: raise ValueError(f"Keychain item missing: {name}. Run keychain-init locally.")
        return value
    def set(self, name, value):
        self.backend.set_password(SERVICE, self.instance + ":" + name, value)
    def init(self):
        # Preserve keys across restarts; rotate only through explicit local operation.
        for name, factory in [("encryption", lambda: Fernet.generate_key().decode()), ("owner", lambda: secrets.token_urlsafe(32))]:
            if not self.backend.get_password(SERVICE, self.instance + ":" + name):
                self.set(name,factory())


class OAuth:
    def __init__(self, runtime, vault, base, projects):
        self.base=base.rstrip("/"); self.resource=self.base+"/mcp"
        self.projects=projects
        self.owner=vault.get("owner")
        self.crypto=Fernet(vault.get("encryption").encode())
        runtime.mkdir(mode=0o700,parents=True,exist_ok=True)
        self.path=runtime/"oauth.sqlite3"
        if self.path.is_symlink(): raise ValueError("Symlink auth database forbidden")
        with self.db() as db:
            db.execute("CREATE TABLE IF NOT EXISTS items(kind TEXT, key TEXT, value BLOB, PRIMARY KEY(kind,key))")
        self.path.chmod(0o600)
        self.attempts={}

    def db(self): return sqlite3.connect(self.path,timeout=20)
    @staticmethod
    def digest(value): return hashlib.sha256(value.encode()).hexdigest()
    def put(self,kind,key,value):
        with self.db() as db: db.execute("INSERT OR REPLACE INTO items VALUES(?,?,?)",(kind,self.digest(key),self.crypto.encrypt(json.dumps(value).encode())))
    def get(self,kind,key,consume=False):
        with self.db() as db:
            db.execute("BEGIN IMMEDIATE")
            row=db.execute("SELECT value FROM items WHERE kind=? AND key=?",(kind,self.digest(key))).fetchone()
            if consume: db.execute("DELETE FROM items WHERE kind=? AND key=?",(kind,self.digest(key)))
        if not row: return None
        return json.loads(self.crypto.decrypt(row[0]))

    async def get_client(self,client_id):
        value=self.get("client",client_id)
        return OAuthClientInformationFull.model_validate(value) if value else None

    async def register_client(self,client_info):
        for uri in client_info.redirect_uris:
            p=urlsplit(str(uri))
            if p.username or p.password or p.fragment or p.scheme!="https" and not (p.scheme=="http" and p.hostname in {"127.0.0.1","localhost"}):
                raise RegistrationError("invalid_redirect_uri","HTTPS redirect required")
        self.put("client",client_info.client_id,client_info.model_dump(mode="json"))

    async def authorize(self,client,params):
        if params.resource and params.resource!=self.resource: raise AuthorizeError("invalid_target","Resource mismatch")
        scopes=params.scopes or ["bridge"]
        if any(s!="bridge" and s not in {"project:"+p for p in self.projects} for s in scopes): raise AuthorizeError("invalid_scope","Unknown project scope")
        pending=secrets.token_urlsafe(32)
        self.put("pending",pending,{"client":client.client_id,"params":params.model_dump(mode="json"),"expires":time.time()+600})
        return self.base+"/owner?"+urlencode({"request":pending})

    async def owner_page(self,request):
        pending=request.query_params.get("request","") if request.method=="GET" else (await request.form()).get("request","")
        value=self.get("pending",pending)
        if not value or value["expires"]<time.time(): return HTMLResponse("Authorization request expired",400)
        headers={"Cache-Control":"no-store","Content-Security-Policy":"default-src 'none'; form-action 'self'; frame-ancestors 'none'","X-Frame-Options":"DENY","Referrer-Policy":"no-referrer"}
        if request.method=="POST":
            form=await request.form()
            origin=request.headers.get("origin")
            if origin and origin!=self.base: return HTMLResponse("Invalid origin",403,headers=headers)
            now=time.time(); ip=request.client.host
            failures=[x for x in self.attempts.get(ip,[]) if now-x<300]
            if len(failures)>=5: return HTMLResponse("Try again later",429,headers=headers)
            if not secrets.compare_digest(str(form.get("owner","")),self.owner):
                self.attempts[ip]=failures+[now]
                return HTMLResponse("Authorization denied",403,headers=headers)
            pid=str(form.get("project",""))
            if pid not in self.projects: return HTMLResponse("Unknown project",400,headers=headers)
            params=value["params"]
            requested=params.get("scopes") or ["bridge"]
            if any(s.startswith("project:") and s!="project:"+pid for s in requested): return HTMLResponse("Requested project mismatch",403,headers=headers)
            if not self.get("pending",pending,consume=True): return HTMLResponse("Request already used",400,headers=headers)
            code=secrets.token_urlsafe(32)
            auth=AuthorizationCode(code=code,scopes=["bridge","project:"+pid],expires_at=now+120,client_id=value["client"],code_challenge=params["code_challenge"],redirect_uri=params["redirect_uri"],redirect_uri_provided_explicitly=params["redirect_uri_provided_explicitly"],resource=self.resource)
            self.put("code",code,auth.model_dump(mode="json"))
            pairs={"code":code}
            if params.get("state") is not None: pairs["state"]=params["state"]
            redirect=params["redirect_uri"]
            return RedirectResponse(redirect+("&" if "?" in redirect else "?")+urlencode(pairs),status_code=303,headers=headers)
        options="".join(f'<option value="{html.escape(pid)}">{html.escape(p.get("name",pid))}</option>' for pid,p in self.projects.items())
        return HTMLResponse(f'''<!doctype html><html lang="ru"><meta charset="utf-8"><title>AI Bridge</title><h1>Доступ Architect</h1><p>Разрешается чтение исходников и обмен задачами/ревью для одного проекта.</p><p>Shell и запись исходников недоступны. Выберите проект для этого подключения.</p><form method="post"><input type="hidden" name="request" value="{html.escape(pending)}"><label>Проект <select name="project">{options}</select></label><p><label>Пароль владельца из Keychain <input type="password" name="owner" required autocomplete="current-password"></label></p><button>Разрешить</button></form></html>''',headers=headers)

    async def load_authorization_code(self,client,authorization_code):
        value=self.get("code",authorization_code)
        if not value or value["client_id"]!=client.client_id: return None
        return AuthorizationCode.model_validate(value)

    def issue(self,client_id,scopes,resource):
        access=secrets.token_urlsafe(32); refresh=secrets.token_urlsafe(32); now=int(time.time())
        self.put("access",access,{"token":access,"client_id":client_id,"scopes":scopes,"expires_at":now+3600,"resource":resource,"refresh_digest":self.digest(refresh)})
        self.put("refresh",refresh,{"token":refresh,"client_id":client_id,"scopes":scopes,"expires_at":now+30*86400,"resource":resource,"access":access})
        return OAuthToken(access_token=access,refresh_token=refresh,expires_in=3600,scope=" ".join(scopes))

    async def exchange_authorization_code(self,client,authorization_code):
        value=self.get("code",authorization_code.code,consume=True)
        if not value or value["expires_at"]<time.time(): raise TokenError("invalid_grant","Expired or consumed code")
        return self.issue(client.client_id,authorization_code.scopes,authorization_code.resource)

    async def load_access_token(self,token):
        value=self.get("access",token)
        if not value or value["expires_at"]<time.time() or value["resource"]!=self.resource: return None
        if not any(s=="project:"+p for p in self.projects for s in value["scopes"]): return None
        return AccessToken.model_validate(value)

    async def load_refresh_token(self,client,refresh_token):
        value=self.get("refresh",refresh_token)
        if not value or value["client_id"]!=client.client_id or value["expires_at"]<time.time(): return None
        return RefreshToken.model_validate(value)

    async def exchange_refresh_token(self,client,refresh_token,scopes):
        value=self.get("refresh",refresh_token.token,consume=True)
        if not value or value["expires_at"]<time.time() or value["resource"]!=self.resource: raise TokenError("invalid_grant","Expired/consumed refresh token")
        if not set(scopes)<=set(value["scopes"]): raise TokenError("invalid_scope","Scope expansion forbidden")
        self.get("access",value["access"],consume=True)
        return self.issue(client.client_id,scopes,value["resource"])

    async def revoke_token(self,token):
        value=self.get("refresh" if isinstance(token,RefreshToken) else "access",token.token,consume=True)
        if not value: return
        with self.db() as db:
            if "access" in value: db.execute("DELETE FROM items WHERE kind='access' AND key=?",(self.digest(value["access"]),))
            if "refresh_digest" in value: db.execute("DELETE FROM items WHERE kind='refresh' AND key=?",(value["refresh_digest"],))
