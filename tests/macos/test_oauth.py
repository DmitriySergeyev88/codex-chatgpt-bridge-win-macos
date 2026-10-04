import asyncio
import base64
import hashlib
import json
import secrets
from urllib.parse import urlsplit,parse_qs
from cryptography.fernet import Fernet
import httpx
import pytest
from mcp.server.auth.provider import AuthorizationParams, TokenError
from mcp.shared.auth import OAuthClientInformationFull
from bridge.auth import OAuth
from bridge.server import build
from test_core import project, result
from bridge.core import Queue


class TestVault:
    __test__=False
    def __init__(self): self.values={"owner":secrets.token_urlsafe(32),"encryption":Fernet.generate_key().decode()}
    def get(self,key): return self.values[key]


def test_oauth_pkce_and_scoped_mcp(tmp_path):
    async def check():
        a=project(tmp_path,"PROJECT-AAA");b=project(tmp_path,"PROJECT-BBB")
        config=tmp_path/"config/projects";config.mkdir(parents=True)
        for p in [a,b]: (config/(p["bridge_project_id"]+".json")).write_text(json.dumps({k:v for k,v in p.items() if k!="root"}))
        vault=TestVault(); instance={"instance_id":"test","public_base_url":"http://127.0.0.1:8765","port":8765}
        mcp,oauth=build(config,instance,tmp_path/"runtime",vault)
        app=mcp.streamable_http_app()
        async with app.router.lifespan_context(app):
            async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app),base_url=instance["public_base_url"],follow_redirects=False) as client:
                headers={"Content-Type":"application/json","Accept":"application/json, text/event-stream"}
                request={"jsonrpc":"2.0","id":1,"method":"initialize","params":{"protocolVersion":"2025-06-18","capabilities":{},"clientInfo":{"name":"test","version":"1"}}}
                assert (await client.post("/mcp",json=request,headers=headers)).status_code==401
                registration=await client.post("/register",json={"redirect_uris":["http://127.0.0.1:9999/callback"],"token_endpoint_auth_method":"none","grant_types":["authorization_code","refresh_token"],"response_types":["code"]})
                assert registration.status_code==201,registration.text
                cid=registration.json()["client_id"]
                verifier=secrets.token_urlsafe(40)
                challenge=base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).rstrip(b"=").decode()
                response=await client.get("/authorize",params={"response_type":"code","client_id":cid,"redirect_uri":"http://127.0.0.1:9999/callback","code_challenge":challenge,"code_challenge_method":"S256","scope":"bridge","state":"test-state","resource":oauth.resource})
                assert response.status_code==302,response.text
                location=response.headers["location"]; pending=parse_qs(urlsplit(location).query)["request"][0]
                denied=await client.post("/owner",data={"request":pending,"owner":"wrong","project":"PROJECT-AAA"})
                assert denied.status_code==403
                approved=await client.post("/owner",data={"request":pending,"owner":vault.get("owner"),"project":"PROJECT-AAA"})
                assert approved.status_code==303,approved.text
                code=parse_qs(urlsplit(approved.headers["location"]).query)["code"][0]
                token_body={"grant_type":"authorization_code","client_id":cid,"code":code,"redirect_uri":"http://127.0.0.1:9999/callback","code_verifier":"bad-verifier","resource":oauth.resource}
                assert (await client.post("/token",data=token_body)).status_code==400
                token_body["code_verifier"]=verifier
                issued=await client.post("/token",data=token_body)
                assert issued.status_code==200,issued.text
                assert (await client.post("/token",data=token_body)).status_code==400
                credentials=issued.json(); headers["Authorization"]="Bearer "+credentials["access_token"]
                assert (await client.post("/mcp",json=request,headers=headers)).status_code==200
                async def call(name,args):
                    r=await client.post("/mcp",json={"jsonrpc":"2.0","id":2,"method":"tools/call","params":{"name":name,"arguments":args}},headers=headers)
                    assert r.status_code==200,r.text
                    return r.json()["result"]
                listing=await client.post("/mcp",json={"jsonrpc":"2.0","id":3,"method":"tools/list","params":{}},headers=headers)
                names={x["name"] for x in listing.json()["result"]["tools"]}
                assert names=={"project_info","read_file","list_files","search_files","git_status","git_diff","submit_task","task_result","review_task"}
                assert not (await call("read_file",{"bridge_project_id":"PROJECT-AAA","path":"README.md"})).get("isError",False)
                assert (await call("read_file",{"bridge_project_id":"PROJECT-BBB","path":"README.md"}))["isError"]
                assert (await call("read_file",{"bridge_project_id":"PROJECT-AAA","path":".env"}))["isError"]
                assert (await call("run_shell",{"command":"echo bad"}))["isError"]
                await call("submit_task",{"bridge_project_id":"PROJECT-AAA","title":"Smoke","instructions":"Test","acceptance":["Works"],"idempotency_key":"smoke-0001"})
                queue=Queue(a); task=queue.claim(); queue.finish(task,result(task))
                fix=await call("review_task",{"bridge_project_id":"PROJECT-AAA","task_id":"TASK-001","revision":1,"verdict":"FIX","feedback":"Improve test evidence","idempotency_key":"review-fix-0001"})
                assert not fix.get("isError",False)
                stale=await call("review_task",{"bridge_project_id":"PROJECT-AAA","task_id":"TASK-001","revision":1,"verdict":"ACCEPT","feedback":"stale","idempotency_key":"review-stale-0001"})
                assert stale["isError"]
                task=queue.claim(); assert task["revision"]==2;queue.finish(task,result(task))
                accepted=await call("review_task",{"bridge_project_id":"PROJECT-AAA","task_id":"TASK-001","revision":2,"verdict":"ACCEPT","feedback":"Verified","idempotency_key":"review-accept-0001"})
                assert not accepted.get("isError",False)
                next_task=await call("submit_task",{"bridge_project_id":"PROJECT-AAA","title":"Next","instructions":"Next","acceptance":["Works"],"idempotency_key":"smoke-0002"})
                assert not next_task.get("isError",False)
                assert queue.status()["tasks"][1]["id"]=="TASK-002"
                # Persisted OAuth credentials remain valid after reconstructing provider.
                restarted=OAuth(tmp_path/"runtime",vault,instance["public_base_url"],{p["bridge_project_id"]:p for p in [a,b]})
                assert await restarted.load_access_token(credentials["access_token"])
                refresh={"grant_type":"refresh_token","client_id":cid,"refresh_token":credentials["refresh_token"],"resource":oauth.resource}
                newer=await client.post("/token",data=refresh); assert newer.status_code==200,newer.text
                assert not await oauth.load_access_token(credentials["access_token"])
                assert (await client.post("/token",data=refresh)).status_code==400
                revoke=await client.post("/revoke",data={"client_id":cid,"client_secret":"","token":newer.json()["access_token"],"token_type_hint":"access_token"})
                assert revoke.status_code==200,revoke.text
                assert not await oauth.load_access_token(newer.json()["access_token"])
    asyncio.run(check())
